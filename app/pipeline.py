# -*- coding: utf-8 -*-
"""raw -> clean + quarantine -> gold, in DuckDB, driven entirely by the contract.

The layer names and the reasoning behind them:

    raw         every source row as TEXT, nothing coerced. If raw cleaned or
                dropped anything, the quality gate would have no evidence left
                to measure -- you could not report "3% of this export was
                unusable" because raw would already have hidden it.
    clean       typed, normalised, de-duplicated, one row per invoice_id.
    quarantine  rejected rows, each with a NAMED reason. Never deleted:
                deleting them makes the output look immaculate while concealing
                that part of CloudNova's business was discarded.
    gold_*      one table per stakeholder question in BUSINESS_CONTEXT.md.
"""
from __future__ import annotations

import duckdb

from .contract import Contract


def build(con: duckdb.DuckDBPyConnection, csv_path: str, c: Contract) -> None:
    _raw(con, csv_path)
    _typed(con, c)
    _quarantine(con, c)
    _clean(con, c)
    _gold(con, c)


# ---------------------------------------------------------------------------
def _raw(con, csv_path: str) -> None:
    # all_varchar: read EVERY column as text. The CSV has amounts as
    # "$4,900.00", five date formats and ten boolean spellings; letting the
    # reader guess types would discard exactly the rows we need to count.
    con.execute(
        """
        CREATE OR REPLACE TABLE raw AS
        SELECT *, row_number() OVER () AS _source_row
        FROM read_csv(?, all_varchar = true, header = true)
        """,
        [csv_path],
    )


def _typed(con, c: Contract) -> None:
    """One pass turning text into values. Nothing is rejected here -- failures
    become NULL so the quarantine step can name a reason for each."""
    status_case = c.sql_case("status", "status")
    plan_case = c.sql_case("plan", "plan")
    cycle_case = c.sql_case("billing_cycle", "billing_cycle")
    churn_case = c.sql_case("churned", "churned")
    fx_case = c.sql_map(c.fx, "upper(trim(currency))")
    price_case = c.sql_map(c.list_price, "plan_canonical")

    # try_strptime returns NULL rather than raising. One unparseable date must
    # not abort the run before the gate has had the chance to COUNT it.
    date_expr = lambda col: "COALESCE(%s)" % ", ".join(      # noqa: E731
        "try_strptime(trim(%s), %s)" % (col, "'%s'" % f) for f in c.date_formats
    )

    con.execute(f"""
        CREATE OR REPLACE TABLE typed AS
        WITH base AS (
            SELECT
                trim(invoice_id)                              AS invoice_id,
                trim(account_id)                              AS account_id,
                trim(account_name)                            AS account_name,
                nullif(trim(region), '')                      AS region,
                nullif(trim(industry), '')                    AS industry,
                upper(trim(currency))                         AS currency,
                {status_case}                                 AS status_canonical,
                {plan_case}                                   AS plan_canonical,
                {cycle_case}                                  AS billing_cycle_canonical,
                {churn_case}                                  AS churned_text,
                try_cast(trim(seats) AS INTEGER)              AS seats,
                coalesce(try_cast(trim(discount_pct) AS DOUBLE), 0.0) AS discount_pct,
                try_cast(nullif(trim(csat_score), '') AS INTEGER)     AS csat_score,
                try_cast(nullif(trim(support_tickets), '') AS INTEGER) AS support_tickets,
                nullif(trim(payment_method), '')              AS payment_method_raw,
                -- "$4,900.00" / "1,200.00 EUR" / "31432" / "$-4,900.00"
                try_cast(
                    regexp_replace(
                        regexp_replace(trim(amount), '(USD|EUR|GBP)$', ''),
                        '[$, ]', '', 'g')
                    AS DECIMAL(18,2))                         AS amount_local,
                -- a few emails use _at_ instead of @; repair, then validate.
                CASE WHEN replace(trim(contact_email), '_at_', '@')
                          SIMILAR TO '[^@ ]+@[^@ ]+\\.[^@ ]+'
                     THEN replace(trim(contact_email), '_at_', '@')
                     ELSE NULL END                            AS contact_email,
                {date_expr('invoice_date')}::DATE             AS invoice_date,
                {date_expr('signup_date')}::DATE              AS signup_date,
                _source_row
            FROM raw
        )
        SELECT
            *,
            {fx_case}                                         AS fx_rate,
            round(amount_local * {fx_case}, 2)                AS amount_usd,
            {price_case}                                      AS list_price_usd,
            -- MRR excludes billing_cycle on purpose: an annual invoice maps to
            -- the same underlying monthly figure, so the lump sum is not
            -- double-counted. Contract: mrr.includes_billing_cycle = false.
            round({price_case} * seats * (1 - discount_pct / 100.0), 2) AS mrr_usd,
            CASE churned_text WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END AS churned
        FROM base
    """)


def _quarantine(con, c: Contract) -> None:
    """Every rejection carries a named reason, taken from the contract."""
    con.execute("""
        CREATE OR REPLACE TABLE quarantine AS
        SELECT *, CASE
            WHEN invoice_id IS NULL OR invoice_id = '' THEN 'invoice_id_missing'
            WHEN status_canonical IS NULL              THEN 'status_unknown'
            WHEN plan_canonical IS NULL               THEN 'plan_unknown'
            WHEN billing_cycle_canonical IS NULL      THEN 'billing_cycle_unknown'
            WHEN amount_usd IS NULL                   THEN 'amount_unparseable'
            WHEN invoice_date IS NULL                 THEN 'invoice_date_unparseable'
            WHEN seats IS NULL OR seats <= 0          THEN 'seats_not_positive'
            WHEN fx_rate IS NULL                      THEN 'currency_unknown'
        END AS reject_reason
        FROM typed
        WHERE invoice_id IS NULL OR invoice_id = ''
           OR status_canonical IS NULL OR plan_canonical IS NULL
           OR billing_cycle_canonical IS NULL OR amount_usd IS NULL
           OR invoice_date IS NULL OR seats IS NULL OR seats <= 0
           OR fx_rate IS NULL
    """)


def _clean(con, c: Contract) -> None:
    """De-duplicate to one row per invoice_id.

    124 invoice_ids repeat. Full duplicates are harmless; NEAR-duplicates carry
    a different status or amount because the billing tool and the CRM disagree.
    Tie-break: the row furthest along the payment lifecycle wins, because
    finance systems move forward (a `paid` record supersedes `pending` for the
    same invoice), then the later invoice_date, then the larger absolute amount
    as a deterministic last resort. This is a BUSINESS decision made by an
    engineer and CloudNova should ratify it -- see ASSUMPTIONS A2.
    """
    order = " ".join(
        "WHEN %s THEN %d" % ("status_canonical = '%s'" % s, i)
        for i, s in enumerate(c.status_precedence)
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE clean AS
        SELECT * EXCLUDE (_rank) FROM (
            SELECT *, row_number() OVER (
                PARTITION BY invoice_id
                ORDER BY CASE {order} ELSE 99 END ASC,
                         invoice_date DESC,
                         abs(amount_usd) DESC
            ) AS _rank
            FROM typed
            WHERE invoice_id IS NOT NULL AND invoice_id <> ''
              AND status_canonical IS NOT NULL AND plan_canonical IS NOT NULL
              AND billing_cycle_canonical IS NOT NULL AND amount_usd IS NOT NULL
              AND invoice_date IS NOT NULL AND seats IS NOT NULL AND seats > 0
              AND fx_rate IS NOT NULL
        ) WHERE _rank = 1
    """)


def _gold(con, c: Contract) -> None:
    """One table per stakeholder question. Recognised revenue = `paid` only;
    refunds net separately and are reported, never silently folded in."""
    paid = ", ".join("'%s'" % s for s in c.recognised_status)
    refunded = ", ".join("'%s'" % s for s in c.netting_status)

    # Q1
    con.execute(f"""
        CREATE OR REPLACE TABLE gold_revenue_by_year AS
        SELECT year(invoice_date) AS year,
               round(sum(amount_usd), 2) AS recognised_revenue_usd,
               count(*) AS paid_invoices
        FROM clean WHERE status_canonical IN ({paid})
        GROUP BY 1 ORDER BY 1
    """)
    # Q2 -- MRR is a property of an account, so collapse to the account's
    # latest invoice first; averaging raw invoice rows would weight accounts
    # by how often they were billed.
    con.execute("""
        CREATE OR REPLACE TABLE gold_mrr_by_region AS
        WITH latest AS (
            SELECT * EXCLUDE (_r) FROM (
                SELECT *, row_number() OVER (PARTITION BY account_id
                          ORDER BY invoice_date DESC) AS _r
                FROM clean) WHERE _r = 1
        )
        SELECT region,
               count(*) AS accounts,
               round(avg(mrr_usd), 2) AS avg_mrr_usd,
               round(sum(mrr_usd), 2) AS total_mrr_usd
        FROM latest WHERE region IS NOT NULL
        GROUP BY 1 ORDER BY avg_mrr_usd DESC
    """)
    # Q3
    con.execute(f"""
        CREATE OR REPLACE TABLE gold_refunds AS
        SELECT round(sum(amount_usd), 2) AS refunded_usd, count(*) AS refund_invoices
        FROM clean WHERE status_canonical IN ({refunded})
    """)
    # Q4
    con.execute("""
        CREATE OR REPLACE TABLE gold_churn_by_plan AS
        WITH acct AS (
            SELECT account_id, any_value(plan_canonical) AS plan,
                   max(churned)::BOOLEAN AS churned
            FROM clean GROUP BY account_id
        )
        SELECT plan, count(*) AS accounts,
               sum(CASE WHEN churned THEN 1 ELSE 0 END) AS churned_accounts,
               round(100.0 * sum(CASE WHEN churned THEN 1 ELSE 0 END) / count(*), 2)
                   AS churn_rate_pct
        FROM acct GROUP BY 1 ORDER BY churn_rate_pct DESC
    """)
    # Q5
    con.execute(f"""
        CREATE OR REPLACE TABLE gold_top_accounts AS
        SELECT account_id, any_value(account_name) AS account_name,
               round(sum(amount_usd), 2) AS revenue_usd,
               max(csat_score) AS csat_score,
               (max(csat_score) IS NOT NULL AND max(csat_score) <= 2) AS low_csat_flag
        FROM clean WHERE status_canonical IN ({paid})
        GROUP BY account_id ORDER BY revenue_usd DESC
    """)
    # Q6
    con.execute("""
        CREATE OR REPLACE TABLE gold_exposed_pipeline AS
        SELECT status_canonical AS status, count(*) AS invoices,
               round(sum(amount_usd), 2) AS exposed_usd
        FROM clean WHERE status_canonical IN ('pending', 'failed')
        GROUP BY 1 ORDER BY exposed_usd DESC
    """)
