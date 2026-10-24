# SPEC — the client Revenue Intelligence

**Status:** written before the transforms, and binding on them.
**Machine-readable twin:** [`specs/contract.yaml`](contract.yaml) (`contract_version: 1.0.0`).
**Assumption register:** [`specs/ASSUMPTIONS.md`](ASSUMPTIONS.md).

This document is the prose version of the contract. Where this document and
`contract.yaml` describe the same rule, `contract.yaml` is what the code reads at
run time and is therefore the operative version; this document carries the
*reasoning*, which a YAML file cannot. If the two ever disagree, that is a defect
in this repository and `evals/test_contract_drift.py` exists to make it loud.

---

## 1. Problem statement

the client is a B2B SaaS company selling a per-seat workflow product. Finance
exports its invoice ledger to `data/invoices.csv` — one row per invoice,
`source_rows_expected: 5125` (contract.yaml). The export is stitched together from
three systems: an old billing tool, a CRM, and a spreadsheet the finance team
maintains by hand. Nobody has owned data quality, so the file disagrees with
itself: the same invoice appears twice with two different statuses, dates arrive in
five formats, amounts arrive as strings like `"$4,900.00"` and `"1,200.00 EUR"`,
and plan and status names arrive in whatever casing each system used.

The finance lead's stated ask is to *"ask questions about our revenue and
customers in plain English and actually trust the answers."*

So the deliverable has two halves, and the second is worthless without the first:

1. **A pipeline** that turns the dirty export into a small set of typed,
   de-duplicated, auditable tables — one table per question the business actually
   asks — and that **refuses to publish** when the data cannot support an answer.
2. **A natural-language layer** that takes the question in plain English, answers
   it from those tables, and **shows the SQL it ran**, so the answer can be
   checked rather than merely believed.

Six questions define "done". They are listed in `data/BUSINESS_CONTEXT.md` and
mapped to tables in §9 below.

### 1.1 Why a modelling pipeline and not retrieval

All six questions are **aggregations**: a sum, an average, a rate, a ranking, a
count. A retrieval system returns passages that *resemble* the question; it does
not add up 5,125 rows. Answering "what was our 2024 revenue" by retrieving
invoice text and letting a model total it produces a number that looks right and
is not, which is the precise opposite of the finance lead's ask. Arithmetic over a
typed table is the only mechanism that gets these answers *right*; the
natural-language layer sits on top of it and provides the plain-English interface,
not the arithmetic.

## 2. Non-goals

Stated so that their absence reads as a decision rather than an omission.

1. **Not general text-to-SQL.** The `--ask` layer matches a question to one of a
   fixed set of known intents and runs a reviewed, parameterised query. An
   unrecognised question is **declined**, not guessed at (§10.2, Assumption A9).
2. **Not a BI tool.** No dashboard, no scheduler, no web service. One CLI, one
   DuckDB database, one JSON report.
3. **Not incremental.** Every run is a full refresh from the CSV. There is no
   watermark, no change-data-capture, no late-arriving-fact handling.
4. **Not a reconciliation engine.** Where the stored invoice amount disagrees
   with what the pricing rules imply, the pipeline **reports** the disagreement
   and does not correct it (§5.4). Choosing which side is wrong is the client's
   decision, not an engineer's.
5. **Not time-series FX.** Rates are the fixed constants in the contract (§5.3).
6. **Not a customer-facing surface.** No authentication, no multi-tenancy, no PII
   handling beyond nulling out unusable email addresses.
7. **No forecasting, attribution or cohort modelling.** The brief asks six
   descriptive questions; this answers those six.

## 3. Source contract

### 3.1 The file

| Property | Value | Source |
|---|---|---|
| Path | `data/invoices.csv` | brief |
| Format | CSV, header row, one row per invoice | `BUSINESS_CONTEXT.md` |
| Expected rows | 5,125 | `contract.yaml: source_rows_expected` |
| Grain after cleaning | one row per `invoice_id` | `contract.yaml: grain` |
| Upstream systems | billing tool, CRM, hand-maintained spreadsheet | `BUSINESS_CONTEXT.md` |

### 3.2 Columns as delivered

Every column is read as **text** into the `raw` layer. Nothing is coerced on the
way in, because a coercion at read time is a silent row loss: a CSV reader that
cannot parse `"$4,900.00"` as a number either errors on the whole file or nulls the
cell, and in both cases the evidence of what was actually delivered is gone.

| Column | Meaning (from `BUSINESS_CONTEXT.md`) | Known dirt |
|---|---|---|
| `invoice_id` | Invoice key, unique after de-duplication | duplicated across source systems |
| `account_id` | Customer account key | — |
| `account_name` | Customer account display name | — |
| `contact_email` | Billing contact | blank; `_at_` instead of `@` |
| `region` | NA / EMEA / APAC / LATAM | — |
| `industry` | Account industry | blank |
| `plan` | Starter / Pro / Enterprise | casing, spellings, `Tier 2` style |
| `billing_cycle` | monthly / annual | `annual`/`Annual`/`annually`/`yearly` |
| `seats` | Licensed seats on the invoice | — |
| `currency` | USD / EUR / GBP | — |
| `amount` | Invoice amount **in `currency`** | `"$4,900.00"`, `"31432"`, `"1,200.00 EUR"`, negative for refunds |
| `discount_pct` | Percentage discount applied | — |
| `status` | Payment status | casing and many spellings |
| `payment_method` | credit_card / ACH / wire / invoice | dirty, may be blank |
| `signup_date` | Account signup date | five date formats |
| `invoice_date` | Invoice issue date | five date formats, some ambiguous |
| `churned` | Whether the account has since churned | `true/TRUE/Yes/1/Y`, `false/FALSE/No/0/N` |
| `csat_score` | Latest CSAT, 1–5 | blank |
| `support_tickets` | Tickets opened by the account | — |

### 3.3 Grain note that matters for the answers

`invoice_id` is the grain, but several columns are **account** attributes carried
on every one of that account's invoices: `region`, `industry`, `churned`,
`csat_score`, `support_tickets`, `account_name`. Summing or averaging an
account-level attribute across invoice rows over-weights accounts that invoice
often. Any gold table that reports per-account (§9: Q2, Q4, Q5) must therefore
reduce to one row per account **first**, and the reduction rule is stated with each
table.

## 4. Cleaned schema

`clean_invoices` — typed, normalised, one row per `invoice_id`.

| Column | Type | Constraint |
|---|---|---|
| `invoice_id` | `VARCHAR` | **PK.** NOT NULL, unique (enforced by gate check DQ05) |
| `account_id` | `VARCHAR` | NOT NULL |
| `account_name` | `VARCHAR` | nullable |
| `contact_email` | `VARCHAR` | nullable — invalid addresses are nulled, not rejected (§5.5) |
| `region` | `VARCHAR` | nullable; expected one of NA / EMEA / APAC / LATAM |
| `industry` | `VARCHAR` | nullable |
| `plan_canonical` | `VARCHAR` | NOT NULL, one of `Starter` / `Pro` / `Enterprise` |
| `billing_cycle_canonical` | `VARCHAR` | NOT NULL, one of `monthly` / `annual` |
| `seats` | `INTEGER` | NOT NULL, `> 0` |
| `currency` | `VARCHAR` | NOT NULL, one of USD / EUR / GBP |
| `amount_source` | `DECIMAL(18,2)` | NOT NULL — the parsed invoice amount, in `currency` |
| `fx_rate` | `DECIMAL(10,4)` | NOT NULL — from `contract.yaml: fx_to_usd` |
| `amount_usd` | `DECIMAL(18,2)` | NOT NULL — `amount_source * fx_rate` |
| `discount_pct` | `DECIMAL(5,2)` | defaults to `0` when blank |
| `status_canonical` | `VARCHAR` | NOT NULL, one of `paid` / `pending` / `failed` / `refunded` / `void` |
| `payment_method` | `VARCHAR` | nullable |
| `signup_date` | `DATE` | nullable |
| `invoice_date` | `DATE` | NOT NULL |
| `invoice_year` | `INTEGER` | derived from `invoice_date` |
| `churned` | `BOOLEAN` | nullable |
| `csat_score` | `INTEGER` | nullable, 1–5; blank means **unknown**, never `0` (A6) |
| `support_tickets` | `INTEGER` | nullable |
| `mrr_usd` | `DECIMAL(18,2)` | derived, §5.6 |
| `amount_expected_usd` | `DECIMAL(18,2)` | derived diagnostic only, §5.4 — **never** used as revenue |

A `NOT NULL` column in this table is not an aspiration. Every one of them
corresponds to a quarantine rule in §8: a row that cannot satisfy it leaves the
cleaned table and lands in `quarantine_invoices` with a named reason.

`quarantine_invoices` carries **every original source column as text**, plus
`quarantine_reason` (one of the named reasons in §8) and enough provenance to find
the row in the delivered file. Rejected rows keep their original text precisely
because the point of the table is to show the client what was unusable *as
delivered*.

## 5. Parsing rules

### 5.1 Order of operations

Trim whitespace → lower-case for lookup → normalise categories (§6) → parse dates
(§5.2) → parse amounts (§5.3) → derive (§5.4, §5.6) → quarantine (§8) →
de-duplicate (§7). Normalisation happens **before** quarantine so that a row is
only ever rejected for being genuinely unusable, not for being capitalised oddly.
De-duplication happens **last** so that the tie-break in §7 compares canonical
statuses rather than raw spellings.

### 5.2 Dates — and the ambiguity decision

`contract.yaml: date_formats` declares an **ordered** list; the first format that
parses a value wins:

```
1. %Y-%m-%d     2024-01-05
2. %m/%d/%Y     01/05/2024
3. %Y/%m/%d     2024/01/05
4. %b %d %Y     Jan 5 2024
5. %d-%m-%Y     05-01-2024
```

Formats 1, 3 and 4 are unambiguous. Formats 2 and 5 are where the judgement lives,
because a value like `05/01/2024` or `05-01-2024` is a valid date under both
month-first and day-first reading, and **no amount of validation can tell them
apart**. There is no internal evidence in a single row, and the gate cannot catch
it: both readings produce a real date that passes every type check. A date parsed
to the wrong day is simply wrong and silent.

**The declared house rule is month-first.** The reasoning, from
`contract.yaml` §3 and `BUSINESS_CONTEXT.md`: the client is a US SaaS company,
US-origin billing exports conventionally write month first, and the rows in the
file that *are* unambiguous (those with a day component above 12) are consistent
with a month-first reading. That is circumstantial support for a convention, not
proof about any individual ambiguous row.

**Open discrepancy, stated rather than smoothed over.** The ordered list above
implements month-first for the **slash** form (`%m/%d/%Y` at position 2 beats any
day-first slash reading). It does **not** implement it for the **dash** form: the
list contains no `%m-%d-%Y`, so `05-01-2024` falls through to position 5,
`%d-%m-%Y`, and is read **day-first** as 5 January 2024. The declared assumption
and the declared format list therefore disagree for exactly the values
`BUSINESS_CONTEXT.md` calls out as ambiguous. This is a contract defect, not a
code defect, and it must be settled by **editing `contract.yaml`** — either by
inserting `%m-%d-%Y` ahead of `%d-%m-%Y` to make the rule uniformly month-first,
or by amending the contract comment to state that dash-separated dates are
day-first by design. It is tracked as Assumption **A1** and must be put to
the client. It is not resolved by a special case in Python; see §12.2.

`invoice_date` is mandatory — a row with no parseable invoice date cannot be
placed in a reporting period, so it is quarantined (`invoice_date_unparseable`).
`signup_date` is not mandatory: it feeds no gold table in this scope, so an
unparseable signup date is nulled and the invoice is kept. Throwing away real
revenue because a secondary date is malformed would be the worse error.

### 5.3 Amounts and currency

Per `contract.yaml: amount`: strip a trailing currency code (`USD`, `EUR`, `GBP`),
then retain **only digits, a single minus sign and the decimal point**. Negatives
are **allowed and meaningful** — a refund is genuinely a negative amount and must
not be absolute-valued. The result is typed `DECIMAL(18,2)`, not a float, because
binary floating point cannot represent money exactly and a cent of drift fails
gate check DQ06 for the wrong reason.

**The rule is an allow-list of characters to keep, not a deny-list of characters
to strip, and that distinction is load-bearing.** The contract's
`strip_characters` list names `$`, `,` and a space, which is what
`BUSINESS_CONTEXT.md` illustrates. The delivered file also contains **euro and
pound symbols**, which a deny-list silently fails to handle: 396 rows — 7.7% —
were quarantined as `amount_unparseable` on the first run for exactly that reason.
A deny-list has to anticipate every symbol a human might type; an allow-list
cannot be surprised by one. Note that the failure was *safe* — the rows were
quarantined with a named reason and counted in the report, not dropped — which is
the quarantine policy in §8 doing its job.

Two consequences worth naming:

- `"31432"` has no decimal part. It is read as **31,432.00**, whole currency units
   — not 314.32 cents. Assumption **A7**.
- `"1,200.00 EUR"` carries a currency code inside the amount string. The code is
  stripped as formatting noise and the **`currency` column remains authoritative**
  for the FX conversion. Assumption **A8**.

FX is applied as `amount_usd = amount_source * fx_rate`, where `fx_rate` comes
from `contract.yaml: fx_to_usd` — `USD 1.00`, `EUR 1.08`, `GBP 1.27`. These are
the fixed reference rates used to generate the data (`BUSINESS_CONTEXT.md`), and
`BUSINESS_CONTEXT.md` explicitly permits treating them as fixed for this exercise.
A currency with no rate in the contract is quarantined (`currency_unknown`) rather
than converted at 1.0, because converting at 1.0 silently understates or overstates
revenue and looks like a successful conversion.

### 5.4 Stored amount versus derived price — the stored amount wins

The pricing rules imply an expected invoice amount:

```
amount_expected_usd = list_price_usd[plan] * seats
                      * (1 - discount_pct/100)
                      * months_billed          -- annual: 10, monthly: 1
```

`contract.yaml: pricing` supplies `Starter 49`, `Pro 99`, `Enterprise 299`,
`annual_months_billed: 10` (annual bills ten months — two months free as the
annual incentive), `monthly_months_billed: 1`.

**The stored `amount` does not always agree with this.** A concrete example from
the delivered file, row 1: a `STARTER`, `yearly`, 100-seat, `EUR` invoice stored
at `52920.00`. The pricing rules imply $49 × 100 seats × 10 months = **$49,000**,
while the stored amount converts to EUR 52,920 × 1.08 ≈ **$57,154**. Neither
figure is obviously the error.

**Decision: the stored amount is the source of truth for revenue.** An invoice is
a record of what was actually billed and, for `paid` rows, what the customer
actually paid. The pricing rules are a model of what *should* have been billed;
they do not know about mid-term seat changes, proration, negotiated one-offs or
credits. Recomputing revenue from the model would quietly replace the client's
books with an engineer's reconstruction of them.

So `amount_expected_usd` is computed and carried **as a diagnostic only** and is
reported in `out/_dq_report.json`. It never enters a revenue figure. The mismatch
is surfaced, not reconciled (§2 non-goal 4). Assumption **A3**.

### 5.5 Email

Per `contract.yaml: email`: repair `_at_` → `@`, then validate. An address that
still fails validation is **nulled out, not quarantined** (`on_invalid: null_out`),
because a malformed billing contact does not make the invoice's money unreal.
Quarantining the row would delete revenue over a typo.

The intended validation is: no whitespace, exactly one `@`, and at least one dot
in the domain part. *Implementation note for the code owner:* the regex currently
in `contract.yaml` reads `^[^@sv]+@[^@sv]+\.[^@sv]+$`, whose character classes
appear to have lost the backslashes from `\s` and `\v`. As written it excludes the
literal letters `s` and `v` from addresses. That looks like an escaping accident
and should be confirmed with whoever owns the contract before the code relies on
it; **fix it in `contract.yaml`**, not in Python.

### 5.6 MRR — deliberately independent of billing cycle

```
mrr_usd = list_price_usd[plan] * seats * (1 - discount_pct/100)
```

`contract.yaml: mrr.includes_billing_cycle: false`. This is the single most
important modelling decision in the pipeline. MRR is a *monthly run-rate*: the
recurring monthly value of a subscription. An annual invoice is ten months of that
run-rate collected in one payment, so multiplying it in — or taking the invoice
amount as MRR — would state an annual customer's monthly run-rate at roughly ten
times a monthly customer's for the identical subscription. Every regional MRR
comparison would then be a measure of which region happens to buy annually.
`BUSINESS_CONTEXT.md` says it directly: *"don't double-count the annual lump
sum."* Assumption **A4**.

Note the consequence: **MRR is derived from the pricing rules, revenue is taken
from the stored amount.** Those two sentences are deliberately different (§5.4),
and it is the right asymmetry — revenue must match the books, run-rate must be
comparable across billing cycles.

### 5.7 Revenue recognition

From `contract.yaml: revenue` and `BUSINESS_CONTEXT.md`:

| Canonical status | Treatment |
|---|---|
| `paid` | **recognised** — counts as revenue |
| `refunded` | **nets** — carries a negative amount, subtracted from revenue |
| `pending`, `failed`, `void` | **excluded** — not revenue |

One canonical measure is used everywhere so that no two tables can disagree:

```
net_recognised_revenue_usd = SUM(amount_usd)
                             WHERE status_canonical IN ('paid','refunded')
```

Refunded amounts are already negative, so the sum nets them. Gross paid revenue
and total refunds are each also reported, so the netting is visible rather than
assumed. Gate check DQ06 reconciles this sum against the gold table to the cent.
Refunds **net** rather than being excluded because the client gave the money back:
revenue that was returned is not revenue. Assumption **A5**.

## 6. Normalisation tables

Values are whitespace-stripped and lower-cased before lookup, so `" Starter "`,
`"STARTER"` and `"starter"` collapse to one key. The full mappings live in
`contract.yaml: normalise` and are reproduced here for the reader:

**Status** → `paid`: paid, complete, completed, success · `pending`: pending,
in_review, awaiting · `failed`: failed, declined, error · `refunded`: refunded,
refund, charged_back · `void`: void, cancelled, canceled

**Plan** → `Starter`: starter, start, tier 1 · `Pro`: pro, professional, tier 2 ·
`Enterprise`: enterprise, ent, tier 3

**Billing cycle** → `monthly`: monthly, month · `annual`: annual, annually,
yearly, year

**Churned** → `true`: true, yes, y, 1, t · `false`: false, no, n, 0, f

> **Contract authoring rule, learned the expensive way.** The `churned` keys must
> be **quoted** in `contract.yaml` (`"true":` / `"false":`). Unquoted, YAML parses
> them as actual booleans, the lookup against a lower-cased *string* never
> matches, and every account falls through to unknown churn. Nothing errors — the
> churn table simply reports **0.00% for every plan**, which is how it was caught.
> Any mapping key that could be read as a YAML scalar type — `true`, `false`,
> `yes`, `no`, `null`, `on`, `off`, or a bare number — is quoted for this reason.

**A value not in the mapping is never guessed.** It is quarantined with a named
reason. This is the rule that protects the revenue number: a status spelling the
contract has not seen must never be bucketed into `paid` by a default branch or a
fuzzy match, because the failure mode is silent revenue inflation that nothing
downstream can detect. A new spelling is cheap to fix — the client's finance lead
adds one word to `contract.yaml` and re-runs. A wrongly-recognised invoice is not.

## 7. De-duplication

**Key:** `invoice_id` (`contract.yaml: deduplication.key`).
**Scale:** 124 `invoice_id` values appear more than once — about 2.4% of 5,125
rows (both figures from `contract.yaml` comments).

Two distinct cases hide behind one symptom:

1. **Full duplicates** — byte-identical rows, the same invoice exported twice.
   Collapsing them is lossless and uncontroversial.
2. **Near-duplicates** — the same `invoice_id` with a **different status or
   amount**, because the billing tool and the CRM genuinely disagree about that
   invoice. Here one record must be chosen, and choosing is a business decision.

**Rule** (`deduplication.strategy: status_precedence_then_latest`):

1. **Status precedence** — `paid` > `refunded` > `failed` > `void` > `pending`.
2. Then **later `invoice_date`**.
3. Then **larger absolute amount**, purely so the result is deterministic.

**Reasoning for the tie-break, and why it needs ratifying.** Payment status moves
forward through a lifecycle: an invoice goes from `pending` to `paid`, not back.
When two systems disagree, the record showing the later lifecycle stage is
ordinarily the more recently updated one, and the system still saying `pending` is
ordinarily the one that has not caught up. That is a reasonable inference and it is
not a fact about the client's systems. A plausible alternative — "the billing tool
is authoritative, always" — would produce a different revenue number, and the
difference is real money.

So this is recorded as **a business decision currently being made by an engineer**
(Assumption **A2**), with a specific question for the client in
`ASSUMPTIONS.md`. The ordering is declared in `contract.yaml`, so when the client
rules differently, the fix is one list in one YAML file.

## 8. Quarantine policy

Rejected rows are **kept**, in `quarantine_invoices`, each with a **named
reason**. Nothing is ever deleted.

The reasons, from `contract.yaml: quarantine_when`:

| Reason | A row is quarantined when |
|---|---|
| `invoice_id_missing` | `invoice_id` is null or empty — no key, so no grain and no de-duplication |
| `status_unknown` | the status spelling is not in the mapping — must never be guessed into `paid` |
| `plan_unknown` | the plan is not in the mapping — no list price, so no MRR |
| `billing_cycle_unknown` | the billing cycle is not in the mapping |
| `amount_unparseable` | the amount string yields no number — an invoice with no amount is not revenue |
| `invoice_date_unparseable` | no parseable invoice date — cannot be placed in a period |
| `seats_not_positive` | `seats` is null or `<= 0` — cannot price a subscription with no seats |
| `currency_unknown` | no FX rate for the currency — converting at 1.0 would be a silent error |

### 8.1 Why rejected rows are kept rather than dropped

Dropping bad rows makes the output look immaculate while concealing that part of
the client's business was discarded. The cleaned table would be perfectly typed,
every check would pass, and the revenue figure would be quietly missing however
many invoices the parser could not read — with no artefact anywhere saying so. The
client's finance team would eventually find the gap, and at that point every other
number in the deliverable becomes suspect too.

Keeping them, with a reason each, converts an invisible loss into three visible,
useful things:

1. A **count and rate** that the quality gate can threshold (DQ03) and that
   `out/_dq_report.json` publishes, so the size of the gap is a stated number
   rather than an unknown.
2. A **reason breakdown**, which is actionable upstream feedback: "412 rows
   carried a status spelling the contract does not know" tells the client exactly
   what to fix in which source system.
3. The **original text of each rejected row**, so a human can look and adjudicate.

The quarantine table is a deliverable, not a bin.

## 9. Gold tables

One table per stakeholder question (`contract.yaml: gold_tables`). The numbered
questions are verbatim from `data/BUSINESS_CONTEXT.md`.

### 9.1 `gold_revenue_by_year` — Q1
> *"What was our total recognized revenue in USD for **paid** invoices in 2024?"*

Net recognised revenue in USD (§5.7) grouped by `invoice_year`, with gross paid
revenue and refunds shown as separate columns so the netting is auditable. Year
comes from the parsed `invoice_date`, which makes this table the one most exposed
to the date-ambiguity decision in §5.2: an invoice read day-first instead of
month-first can land in the wrong month, and — for dates near a year boundary —
the wrong year.

### 9.2 `gold_mrr_by_region` — Q2
> *"Which **region** has the highest average MRR per account?"*

MRR is a property of a **subscription**, not of an invoice (§5.6), and `region` is
an account attribute carried on every invoice (§3.3). So this table reduces to one
row per `account_id` first — taking the account's **most recent invoice** as its
current subscription state, because seats, plan and discount change over time and
the latest invoice is the best available statement of the subscription as it stands
(Assumption **A10**) — and only then averages `mrr_usd` within `region`. The
account count per region is reported alongside the average, because an average
over four accounts and an average over four hundred do not deserve equal weight on
a slide.

### 9.3 `gold_refunds` — Q3
> *"How much have we given back in **refunds**?"*

Total refunded amount in USD from rows with `status_canonical = 'refunded'`, with
the invoice count, and broken out so the figure can be traced. Reported as a
positive "amount given back" with the sign convention stated on the table, since
the underlying amounts are negative (§5.3) and a minus sign in a report is read as
an error as often as it is read as a refund.

### 9.4 `gold_churn_by_plan` — Q4
> *"What's the **churn rate** among Enterprise accounts vs Starter?"*

Churn is an **account** state, so the rate is accounts, never invoices:

```
churn_rate = distinct churned accounts / distinct accounts, per plan
```

Counting invoices instead would weight each account by how often it was billed,
which is why an account-level reduction comes first. An account's plan is taken
from its most recent invoice (an account can upgrade; Assumption **A10**).
Accounts with an unparseable `churned` value are counted in a separate `unknown`
column rather than being assumed not to have churned, and the denominator used for
the rate is stated on the table.

### 9.5 `gold_top_accounts` — Q5
> *"List the top 5 accounts by revenue, and flag any with **CSAT ≤ 2**."*

Net recognised revenue in USD per `account_id`, ranked descending, with a
`low_csat_flag`. CSAT is an account attribute (§3.3), taken from the account's most
recent invoice. **A blank CSAT is `unknown`, not `0`** — treating it as zero would
be a fabricated bad score, and it would flag the account as unhappy on no
evidence. Unknown is therefore reported as its own state and is **not** flagged as
low (Assumption **A6**). The table is materialised in full rank order; "top 5" is
a `LIMIT` in the query layer, so the same table also answers "top 10".

### 9.6 `gold_exposed_pipeline` — Q6
> *"How many invoices are stuck in **pending/failed** and what's the exposed $?"*

Count and total USD amount for `status_canonical IN ('pending','failed')`, split
by status. `void` is deliberately **excluded**: a void invoice was cancelled on
purpose and nobody is going to collect it, so including it would overstate what is
genuinely recoverable. "Exposed" here means *billed and not yet collected* — it is
not a forecast, and no probability of collection is applied.

## 10. Quality gate

Seven numbered checks (`contract.yaml: quality_gate.checks`). Results are written
to `out/_dq_report.json`. **If any check breaches, the job exits non-zero.**
Nothing in the gate is advisory, and a breach is not a warning printed under a
published number — it stops the publish.

| ID | Measure | Test | Why that threshold |
|---|---|---|---|
| **DQ01** | `raw_row_count` | `>= 1` | An empty export is a delivery failure, not an empty result. Reporting "$0 revenue" from a zero-byte file is the worst possible silent failure. |
| **DQ02** | `clean_row_count` | `>= 1` | Everything quarantined means the contract no longer matches the source — a schema change upstream, not a data problem. |
| **DQ03** | `quarantine_rate` | `<= 0.15` | Up to 15% of a three-system stitched export being unusable is tolerable *and is reported back to the client*. Above that the export itself is suspect and no revenue figure should be published from it. |
| **DQ04** | `duplicate_rate` | `<= 0.10` | 124 duplicate ids in 5,125 rows is ~2.4%. A jump to 10%+ is the signature of an extract window that overlapped, i.e. the same period exported twice — a delivery error that would inflate revenue. |
| **DQ05** | `clean_pk_duplicate_count` | `== 0` | The cleaned grain is one row per `invoice_id`. Non-negotiable: a duplicated key double-counts money. |
| **DQ06** | `revenue_reconciliation_abs_diff` | `<= 0.01` | Recognised revenue summed from the cleaned table must equal the gold revenue table **to the cent**. This is the check that catches a lost filter or a join fan-out — the class of bug that survives code review and is then found by the client's finance team. One cent of tolerance covers decimal rounding, nothing more. |
| **DQ07** | `negative_amount_outside_refunded_count` | `== 0` | A negative amount on a non-refunded invoice is a source defect, not a discount. Left alone it silently reduces revenue. |

### 10.1 The gate must be proven to fail

A gate that has only ever been seen passing is not evidence of anything; it could
be returning `True` unconditionally. So the eval suite exercises each check
against **deliberately broken input** and asserts the specific check fails and the
process exits non-zero. A check that cannot be shown to fail is treated as an
untested check. See §11, acceptance criterion 8.

### 10.2 Natural-language layer

`--ask "<question>"` matches the question text to one of a fixed set of known
intents — the six in §9 and close paraphrases of them — and runs the reviewed,
parameterised SQL belonging to that intent against the gold tables.

- **The generated SQL is printed with the answer.** The finance lead asked to
  *trust* the answers; a number with the query that produced it can be checked by
  someone who knows the business, which is what trust is actually made of.
- **An unmatched question is declined**, explicitly, with the list of questions
  the system does know. It is not answered on a best guess. A confidently stated
  wrong number is worse than "I can't answer that", because the wrong number gets
  forwarded into a board pack and the refusal gets a follow-up question.
- Parameters (a year, a status, a row limit) are bound, never string-formatted
  into the SQL.

## 11. Acceptance criteria

The build is done when all of the following hold.

1. `python -m app` runs end to end from the delivered CSV with no manual steps,
   creating `raw`, `clean`, `quarantine` and all six `gold_*` tables.
2. `clean_invoices` has exactly one row per `invoice_id`, and
   `raw + 0 = clean + quarantine` — every delivered row is accounted for in
   exactly one of the two outputs, with no row silently vanishing.
3. Every quarantined row carries one of the named reasons in §8. No row is
   quarantined as `other`, and no row is deleted.
4. All six gold tables exist and each answers its mapped question in §9.
5. The quality gate runs on every invocation, prints a per-check pass/fail line,
   writes `out/_dq_report.json`, and **exits non-zero** on any breach.
6. `python -m app --ask "<one of the six questions>"` returns the answer **and**
   the SQL, for all six.
7. `python -m app --ask "<a question outside the known intents>"` declines
   clearly. It does not return a number.
8. `python evals/run_evals.py` passes, and includes at least one case per gate
   check that **forces that check to fail** (§10.1), plus the six stakeholder
   questions as expected-value cases.
9. No business rule, threshold, FX rate, status spelling or enum is hardcoded in
   Python. Each comes from `contract.yaml`, and a drift test asserts that.
10. Setup is `pip install duckdb pyyaml`. No server, no API key, no network call
    on the default path.

## 12. Relationship to `contract.yaml`

### 12.1 Division of labour

`contract.yaml` is the machine-readable twin of this document: the same rules,
without the reasoning, in the form the code reads at run time. Thresholds, FX
rates, status spellings, plan prices, date formats, the de-duplication precedence
order and the quarantine tests all live there. This document explains *why* each
value is what it is — which is what makes the contract safe to change.

Commercially that split is the point. the client's finance lead can add a status
spelling, correct an FX rate or loosen a quality threshold by editing one YAML
file — no developer, no code change, no release.

### 12.2 The rule that keeps it honest

**A business rule discovered mid-build is added to `contract.yaml`, not patched
into Python.** The moment one special case is handled in code "just for now", the
contract stops describing the pipeline and the finance lead's ability to change
behaviour silently disappears. This applies to the open date discrepancy in §5.2
and the suspected regex escaping defect in §5.5: both are fixed in the contract.

`evals/test_contract_drift.py` fails the build if the code and the contract ever
disagree.
