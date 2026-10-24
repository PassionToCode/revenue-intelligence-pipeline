# -*- coding: utf-8 -*-
"""Natural-language layer: INTENT MATCHING FIRST, an LLM only as fallback.

Why intents run first, and why that ordering is the whole design
----------------------------------------------------------------
The brief asks for answers a finance lead can trust. For the six questions
the client actually asked (data/BUSINESS_CONTEXT.md) there is reviewed SQL
against gold tables the quality gate has already reconciled to the cent. That
answer is **deterministic, auditable and free**: the same question returns the
same number every time, the SQL was read by a human, and no network call or
token spend is involved. Nothing a language model can do improves on that, and
a model in that path would only add a way for a correct answer to drift.

So a model is reached **only for questions the spec did not anticipate**. For
those, `app/nl.py` falls back to text-to-SQL: the model is shown the `clean`
table's schema and asked for a single DuckDB SELECT, the SQL is checked against
guardrails, executed, and returned **alongside its SQL and rows** so the reader
can see exactly what was computed. A generated query is a draft for a human to
check, never an authority.

With no API key present nothing changes: the six intents still answer, and an
unmatched question declines cleanly, naming what it can answer. That is what
lets the eval harness and CI run with no key and no network.

Declining is a feature. A confidently wrong revenue figure is far more damaging
than no figure, because it gets pasted into a board pack and nobody can tell it
was wrong. Finance work has an audit standard, not a best-effort standard.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import duckdb

from .contract import Contract

ROOT = Path(__file__).resolve().parent.parent

# Scoring: a question must hit at least one STRONG trigger for an intent to be
# eligible. Strong triggers are the distinctive phrases of that question; boost
# words only break ties between eligible intents. This is what makes an
# off-topic question score zero everywhere and get declined rather than
# captured by whichever intent happens to share a common word like "revenue".
_STRONG_WEIGHT = 2
_BOOST_WEIGHT = 1


def _money(value: Any) -> str:
    return "${:,.2f}".format(float(value or 0))


def _plain(value: Any) -> Any:
    """DuckDB DECIMAL/date values -> JSON-safe Python."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def _year_in(question: str) -> int | None:
    m = re.search(r"\b(19\d{2}|20\d{2})\b", question)
    return int(m.group(1)) if m else None


def _top_n_in(question: str, default: int = 5) -> int:
    """Honour a number in the question, but only where it reads as a count.

    `top 10`, `10 accounts`, `best 3` -- not the `2` in "CSAT <= 2" and not a
    year, which would silently turn "top accounts in 2024" into a top-2024 list.
    """
    for pattern in (r"\btop\s+(\d{1,3})\b",
                    r"\b(?:best|first|largest|biggest)\s+(\d{1,3})\b",
                    r"\b(\d{1,3})\s+(?:top\s+)?accounts?\b"):
        m = re.search(pattern, question)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 500:
                return n
    return default


# ---------------------------------------------------------------------------
# the six intents
# ---------------------------------------------------------------------------
def _sql_revenue(c: Contract, question: str) -> str:
    year = _year_in(question)
    if year is not None:
        return (
            "SELECT CAST(year AS VARCHAR) AS period,\n"
            "       recognised_revenue_usd,\n"
            "       paid_invoices\n"
            "FROM gold_revenue_by_year\n"
            "WHERE year = %d" % year)
    return (
        "SELECT 'all years' AS period,\n"
        "       round(sum(recognised_revenue_usd), 2) AS recognised_revenue_usd,\n"
        "       sum(paid_invoices) AS paid_invoices\n"
        "FROM gold_revenue_by_year")


def _answer_revenue(c: Contract, question: str, rows: list[dict]) -> str:
    year = _year_in(question)
    statuses = "/".join(c.recognised_status)
    if not rows or rows[0].get("recognised_revenue_usd") is None:
        return ("No recognised revenue found for %s. The cleaned ledger has no "
                "`%s` invoices in that period." % (year or "any year", statuses))
    r = rows[0]
    return ("Total recognised revenue (%s invoices only) for %s is %s, across "
            "%s invoices. Refunds are NOT netted into this figure -- ask about "
            "refunds for that."
            % (statuses, r["period"], _money(r["recognised_revenue_usd"]),
               "{:,}".format(int(r["paid_invoices"]))))


def _sql_mrr_region(c: Contract, question: str) -> str:
    return (
        "SELECT region, accounts, avg_mrr_usd, total_mrr_usd\n"
        "FROM gold_mrr_by_region\n"
        "ORDER BY avg_mrr_usd DESC")


def _answer_mrr_region(c: Contract, question: str, rows: list[dict]) -> str:
    if not rows:
        return "No region has a usable MRR figure in the cleaned ledger."
    top = rows[0]
    rest = ", ".join("%s %s" % (r["region"], _money(r["avg_mrr_usd"]))
                     for r in rows[1:])
    return ("%s has the highest average MRR per account at %s across %d "
            "accounts (total %s). Others: %s. MRR is each account's latest "
            "invoice, priced from the contract and independent of billing "
            "cycle, so annual lump sums are not double-counted."
            % (top["region"], _money(top["avg_mrr_usd"]), int(top["accounts"]),
               _money(top["total_mrr_usd"]), rest or "none"))


def _sql_refunds(c: Contract, question: str) -> str:
    return "SELECT refunded_usd, refund_invoices\nFROM gold_refunds"


def _answer_refunds(c: Contract, question: str, rows: list[dict]) -> str:
    if not rows or rows[0].get("refunded_usd") is None:
        return "No refunded invoices in the cleaned ledger."
    r = rows[0]
    total = float(r["refunded_usd"])
    return ("%s was given back across %d refunded invoices. Refund rows carry "
            "a negative amount (stored as %s); they are reported separately "
            "and are not inside the recognised-revenue total."
            % (_money(abs(total)), int(r["refund_invoices"]), _money(total)))


def _sql_churn(c: Contract, question: str) -> str:
    return (
        "SELECT plan, accounts, churned_accounts, churn_rate_pct\n"
        "FROM gold_churn_by_plan\n"
        "ORDER BY churn_rate_pct DESC")


def _answer_churn(c: Contract, question: str, rows: list[dict]) -> str:
    if not rows:
        return "No accounts with a usable plan in the cleaned ledger."
    parts = ["%s %.2f%% (%d of %d accounts churned)"
             % (r["plan"], float(r["churn_rate_pct"]),
                int(r["churned_accounts"]), int(r["accounts"]))
             for r in rows]
    return ("Churn rate by plan: %s. Churn is measured per ACCOUNT, not per "
            "invoice -- an account billed twelve times would otherwise count "
            "twelve times." % "; ".join(parts))


def _sql_top_accounts(c: Contract, question: str) -> str:
    return (
        "SELECT account_id, account_name, revenue_usd, csat_score, "
        "low_csat_flag\n"
        "FROM gold_top_accounts\n"
        "ORDER BY revenue_usd DESC\n"
        "LIMIT %d" % _top_n_in(question))


def _answer_top_accounts(c: Contract, question: str, rows: list[dict]) -> str:
    if not rows:
        return "No accounts with recognised revenue in the cleaned ledger."
    lines = []
    for i, r in enumerate(rows, 1):
        flag = "  <-- CSAT %s, AT RISK" % r["csat_score"] if r["low_csat_flag"] else ""
        lines.append("  %d. %-28s %14s  CSAT %s%s"
                     % (i, str(r["account_name"])[:28],
                        _money(r["revenue_usd"]),
                        r["csat_score"] if r["csat_score"] is not None else "n/a",
                        flag))
    flagged = [r for r in rows if r["low_csat_flag"]]
    tail = ("%d of these %d are flagged CSAT <= 2 -- high revenue, unhappy "
            "customer, which is the combination worth a call."
            % (len(flagged), len(rows))) if flagged else \
           "None of these are flagged CSAT <= 2."
    return ("Top %d accounts by recognised revenue:\n%s\n%s"
            % (len(rows), "\n".join(lines), tail))


def _sql_exposed(c: Contract, question: str) -> str:
    return (
        "SELECT status, invoices, exposed_usd\n"
        "FROM gold_exposed_pipeline\n"
        "ORDER BY exposed_usd DESC")


def _answer_exposed(c: Contract, question: str, rows: list[dict]) -> str:
    if not rows:
        return "No pending or failed invoices in the cleaned ledger."
    invoices = sum(int(r["invoices"]) for r in rows)
    exposed = sum(float(r["exposed_usd"] or 0) for r in rows)
    breakdown = ", ".join("%s %d invoices / %s"
                          % (r["status"], int(r["invoices"]),
                             _money(r["exposed_usd"]))
                          for r in rows)
    return ("%d invoices are stuck, with %s of exposed value (%s). Exposed "
            "means billed but not recognised -- it is cash the client has not "
            "collected, and it is excluded from recognised revenue."
            % (invoices, _money(exposed), breakdown))


INTENTS: list[dict[str, Any]] = [
    {
        "name": "total_recognised_revenue",
        "question": "Total recognised revenue in USD for paid invoices "
                    "(optionally for one year)",
        "strong": ["recognised revenue", "recognized revenue", "total revenue",
                   "revenue in usd", "paid invoice", "recognised", "recognized",
                   "revenue for paid", "how much revenue", "total recognised",
                   "total recognized"],
        "boost": ["revenue", "paid", "usd", "total", "sum", "earned", "booked"],
        "sql": _sql_revenue,
        "answer": _answer_revenue,
    },
    {
        "name": "highest_avg_mrr_region",
        "question": "Which region has the highest average MRR per account",
        "strong": ["mrr", "monthly recurring", "which region", "region",
                   "per account", "recurring revenue"],
        "boost": ["highest", "average", "avg", "account", "best", "top"],
        "sql": _sql_mrr_region,
        "answer": _answer_mrr_region,
    },
    {
        "name": "refunds_total",
        "question": "How much has been given back in refunds",
        "strong": ["refund", "refunds", "refunded", "given back", "give back",
                   "charged back", "charge back", "money back", "clawback"],
        "boost": ["how much", "total", "back", "credit"],
        "sql": _sql_refunds,
        "answer": _answer_refunds,
    },
    {
        "name": "churn_by_plan",
        "question": "Churn rate by plan (Enterprise vs Starter)",
        "strong": ["churn", "churned", "churn rate", "attrition", "retention",
                   "cancelled accounts", "lost customers"],
        "boost": ["plan", "enterprise", "starter", "pro", "rate", "vs"],
        "sql": _sql_churn,
        "answer": _answer_churn,
    },
    {
        "name": "top_accounts_by_revenue",
        "question": "Top N accounts by revenue, flagging CSAT <= 2 "
                    "(default N=5)",
        "strong": ["top account", "top 5", "top 10", "top accounts", "csat",
                   "biggest account", "largest account", "top customer",
                   "best account", "accounts by revenue", "satisfaction score"],
        "boost": ["top", "account", "revenue", "flag", "list", "biggest",
                  "largest", "rank"],
        "sql": _sql_top_accounts,
        "answer": _answer_top_accounts,
    },
    {
        "name": "exposed_pipeline",
        "question": "How many invoices are stuck in pending/failed and the "
                    "exposed dollar value",
        "strong": ["pending", "failed", "stuck", "exposed", "exposure",
                   "at risk", "unpaid", "uncollected", "outstanding",
                   "not collected"],
        "boost": ["invoice", "dollar", "how many", "value", "amount", "$"],
        "sql": _sql_exposed,
        "answer": _answer_exposed,
    },
]


def capabilities() -> list[str]:
    return [i["question"] for i in INTENTS]


def _decline_message(extra: str | None = None) -> str:
    lines = ["I can't answer that, and I'm not going to guess -- a wrong "
             "revenue figure is worse than no figure."]
    if extra:
        lines.append(extra)
    lines.append("I answer these six questions about the the client invoice "
                 "ledger deterministically, from reviewed SQL:")
    for i, q in enumerate(capabilities(), 1):
        lines.append("  %d. %s" % (i, q))
    if not extra:
        lines.append("For anything else, set OPENROUTER_API_KEY (or "
                     "OPENAI_API_KEY / ANTHROPIC_API_KEY) to enable the "
                     "text-to-SQL fallback.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM fallback -- providers
# ---------------------------------------------------------------------------
# Auto-detection order. OpenRouter first because that is what this deployment
# uses; each entry is (provider, env var, default model).
PROVIDERS: list[dict[str, str]] = [
    {"name": "openrouter", "env": "OPENROUTER_API_KEY",
     "model": "openai/gpt-4o-mini",
     "url": "https://openrouter.ai/api/v1/chat/completions"},
    {"name": "openai", "env": "OPENAI_API_KEY",
     "model": "gpt-4o-mini",
     "url": "https://api.openai.com/v1/chat/completions"},
    {"name": "anthropic", "env": "ANTHROPIC_API_KEY",
     "model": "claude-3-5-haiku-latest",
     "url": "https://api.anthropic.com/v1/messages"},
]

LLM_TIMEOUT_SECONDS = 45


def load_env_file(path: Path | None = None) -> None:
    """Fold a .env in the repo root into os.environ, without a dependency.

    Only keys absent from the environment are set, so a real environment
    variable always wins. Values are never logged or echoed anywhere -- see
    `_redact` and note that no function in this module returns a key.
    """
    p = path or (ROOT / ".env")
    try:
        if not p.is_file():
            return
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.lower().startswith("export "):
                    key = key[7:].strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        return  # an unreadable .env is not a reason to fail the run


def detect_provider(preferred: str | None = None) -> dict[str, str] | None:
    """Pick a provider from whichever key is in the ENVIRONMENT only."""
    candidates = PROVIDERS
    if preferred:
        candidates = [p for p in PROVIDERS if p["name"] == preferred.lower()]
        if not candidates:
            return None
    for p in candidates:
        if os.environ.get(p["env"], "").strip():
            return p
    return None


def _redact(text: str) -> str:
    """Belt and braces: strip anything key-shaped out of an error string."""
    out = text
    for p in PROVIDERS:
        key = os.environ.get(p["env"], "").strip()
        if key:
            out = out.replace(key, "<redacted>")
    return re.sub(r"\b(sk-[A-Za-z0-9_\-]{8,})", "<redacted>", out)


# ---------------------------------------------------------------------------
# LLM fallback -- SQL guardrails
# ---------------------------------------------------------------------------
_FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "create",
              "attach", "detach", "copy", "install", "load", "export",
              "pragma", "set", "call", "replace", "truncate", "grant",
              "vacuum", "checkpoint", "merge", "upsert")


def check_sql(sql: str) -> tuple[bool, str]:
    """A generated query is untrusted input. One read-only SELECT, or nothing.

    Reject rather than sanitise: a query that needed rewriting to be safe is a
    query whose intent we no longer understand, so it cannot be trusted to
    answer a finance question either.
    """
    text = (sql or "").strip()
    if not text:
        return False, "the model returned no SQL"

    # Strip a markdown fence if the model wrapped the query in one.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()

    body = text.rstrip(";").strip()
    if ";" in body:
        return False, "multiple statements separated by ';' are not allowed"

    # Comments can hide a second statement; refuse them outright.
    if "--" in body or "/*" in body:
        return False, "SQL comments are not allowed in a generated query"

    lowered = body.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, "only a single SELECT (optionally with a leading WITH) is allowed"
    if lowered.startswith("with") and not re.search(r"\bselect\b", lowered):
        return False, "a WITH clause with no SELECT is not a query"

    words = set(re.findall(r"[a-z_]+", lowered))
    hit = sorted(words.intersection(_FORBIDDEN))
    if hit:
        return False, "forbidden keyword(s) in generated SQL: %s" % ", ".join(hit)
    return True, body


def clean_schema(con: duckdb.DuckDBPyConnection,
                 table: str = "clean") -> list[tuple[str, str]]:
    """Column names and types, read from DuckDB rather than hardcoded."""
    rows = con.execute("DESCRIBE %s" % table).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _prompt(con: duckdb.DuckDBPyConnection, c: Contract,
            question: str) -> tuple[str, str]:
    cols = "\n".join("  %s %s" % (n, t) for n, t in clean_schema(con))
    recognised = ", ".join("'%s'" % s for s in c.recognised_status)
    netting = ", ".join("'%s'" % s for s in c.netting_status)
    system = (
        "You translate a business question into ONE DuckDB SELECT statement.\n"
        "Rules:\n"
        "- Output ONLY the SQL. No prose, no markdown fence, no explanation.\n"
        "- Exactly one statement. No semicolons, no comments, no DDL or DML.\n"
        "- Query only the table `clean` (one row per invoice_id, already "
        "de-duplicated and normalised).\n"
        "- Recognised revenue means status_canonical IN (%s) summed over "
        "amount_usd, which is already converted to USD.\n"
        "- Refunds are status_canonical IN (%s) and carry NEGATIVE amounts.\n"
        "- mrr_usd is already computed per invoice and excludes billing cycle.\n"
        "- Aggregate. Never return raw invoice rows.\n"
        "- Add a LIMIT of at most 50 to any non-aggregated result."
        % (recognised, netting))
    user = ("Table `clean` columns:\n%s\n\nQuestion: %s\n\nSQL:"
            % (cols, question))
    return system, user


def _http_json(url: str, payload: dict[str, Any],
               headers: dict[str, str]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def generate_sql(con: duckdb.DuckDBPyConnection, c: Contract, question: str,
                 provider: dict[str, str], model: str | None = None) -> str:
    """One call to the chosen provider, standard library only."""
    system, user = _prompt(con, c, question)
    model = model or provider["model"]
    key = os.environ[provider["env"]].strip()

    if provider["name"] == "anthropic":
        body = _http_json(
            provider["url"],
            {"model": model, "max_tokens": 600, "temperature": 0,
             "system": system,
             "messages": [{"role": "user", "content": user}]},
            {"x-api-key": key, "anthropic-version": "2023-06-01"})
        parts = body.get("content") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))

    headers = {"Authorization": "Bearer %s" % key}
    if provider["name"] == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/invoices_revenue-intelligence"
        headers["X-Title"] = "the client Revenue Intelligence"
    body = _http_json(
        provider["url"],
        {"model": model, "temperature": 0,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]},
        headers)
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("provider returned no choices: %s"
                           % json.dumps(body.get("error", body))[:300])
    return str(choices[0].get("message", {}).get("content", ""))


def _run_readonly(con: duckdb.DuckDBPyConnection,
                  sql: str) -> tuple[list[str], list[tuple]]:
    """Execute inside a transaction that is always rolled back.

    The keyword guard in check_sql is the first line of defence; this is the
    second. Even if something slipped through, nothing it wrote would survive.
    """
    cur = con.cursor()
    cur.execute("BEGIN TRANSACTION")
    try:
        res = cur.execute(sql)
        cols = [d[0] for d in res.description]
        rows = res.fetchall()
        return cols, rows
    finally:
        try:
            cur.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass


def ask_llm(con: duckdb.DuckDBPyConnection, c: Contract, question: str,
            provider: str | None = None,
            model: str | None = None) -> dict[str, Any]:
    """Text-to-SQL fallback for a question the six intents do not cover."""
    p = detect_provider(provider)
    if p is None:
        hint = ("No API key found for %s, so the text-to-SQL fallback is off."
                % (provider or "openrouter/openai/anthropic"))
        return {"question": question, "intent": None, "matched": False,
                "route": "declined", "score": 0, "sql": None, "rows": [],
                "answer": _decline_message(hint)}

    try:
        raw = generate_sql(con, c, question, p, model)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _redact(exc.read().decode("utf-8", "replace"))[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"question": question, "intent": None, "matched": False,
                "route": "llm_error", "score": 0, "sql": None, "rows": [],
                "answer": _decline_message(
                    "The %s call failed (HTTP %s). %s"
                    % (p["name"], exc.code, detail))}
    except Exception as exc:  # noqa: BLE001
        return {"question": question, "intent": None, "matched": False,
                "route": "llm_error", "score": 0, "sql": None, "rows": [],
                "answer": _decline_message(
                    "The %s call failed: %s"
                    % (p["name"], _redact(str(exc))[:300]))}

    ok, result = check_sql(raw)
    if not ok:
        return {"question": question, "intent": None, "matched": False,
                "route": "llm_rejected", "score": 0, "sql": raw.strip() or None,
                "rows": [],
                "answer": _decline_message(
                    "The model produced SQL I refused to run: %s. I do not "
                    "rewrite a generated query to make it safe -- a query that "
                    "needed fixing is one whose intent is no longer clear."
                    % result)}

    sql = result
    try:
        cols, raw_rows = _run_readonly(con, sql)
    except Exception as exc:  # noqa: BLE001
        return {"question": question, "intent": None, "matched": False,
                "route": "llm_sql_error", "score": 0, "sql": sql, "rows": [],
                "answer": ("The generated SQL did not run: %s\nThe query is "
                           "shown above so you can see what was attempted."
                           % str(exc)[:300])}

    rows = [dict(zip(cols, (_plain(v) for v in r))) for r in raw_rows]
    if not rows:
        summary = "The generated query ran and returned no rows."
    elif len(rows) == 1 and len(cols) == 1:
        summary = "%s = %s" % (cols[0], rows[0][cols[0]])
    else:
        summary = "%d row(s), columns: %s" % (len(rows), ", ".join(cols))
    return {
        "question": question, "intent": "llm_text_to_sql", "matched": True,
        "route": "llm", "score": 0, "sql": sql, "rows": rows[:50],
        "provider": p["name"], "model": model or p["model"],
        "answer": ("%s was not one of the six reviewed questions, so this is a "
                   "MODEL-GENERATED query (%s / %s), not reviewed SQL. Check "
                   "the query before quoting the number.\n%s"
                   % ("That question", p["name"], model or p["model"], summary)),
    }


def match(question: str) -> tuple[dict[str, Any] | None, int]:
    """Return the best-scoring eligible intent, or (None, 0) to decline."""
    q = " %s " % re.sub(r"\s+", " ", (question or "").lower())
    best, best_score = None, 0
    for intent in INTENTS:
        strong = sum(1 for t in intent["strong"] if t in q)
        if not strong:
            continue  # not eligible -- boost words alone never match an intent
        score = strong * _STRONG_WEIGHT + \
            sum(1 for t in intent["boost"] if t in q) * _BOOST_WEIGHT
        if score > best_score:
            best, best_score = intent, score
    return best, best_score


def ask(con: duckdb.DuckDBPyConnection, c: Contract, question: str,
        use_llm: bool = True, provider: str | None = None,
        model: str | None = None) -> dict[str, Any]:
    """Answer one stakeholder question, or decline.

    Intent matching runs FIRST and short-circuits: a match means reviewed SQL,
    no model call and no cost. Only an unmatched question reaches the LLM
    text-to-SQL fallback, and only if a key is present -- otherwise it declines.

    Returns {intent, sql, answer, rows, matched} (plus `route`).
    """
    intent, score = match(question)
    if intent is None:
        if use_llm:
            return ask_llm(con, c, question, provider, model)
        return {
            "question": question,
            "intent": None,
            "matched": False,
            "route": "declined",
            "score": 0,
            "sql": None,
            "rows": [],
            "answer": _decline_message(),
        }

    sql: str = intent["sql"](c, question)
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, (_plain(v) for v in row))) for row in cur.fetchall()]
    answer_fn: Callable[..., str] = intent["answer"]
    return {
        "question": question,
        "intent": intent["name"],
        "matched": True,
        "route": "intent",
        "score": score,
        "sql": sql,
        "rows": rows,
        "answer": answer_fn(c, question, rows),
    }


def print_answer(result: dict[str, Any]) -> None:
    route = result.get("route", "intent")
    print("Q: %s" % result["question"])
    if result["matched"] and route == "intent":
        print("   route:  intent match -> reviewed SQL (deterministic, no model)")
        print("   intent: %s" % result["intent"])
    elif result["matched"]:
        print("   route:  LLM text-to-SQL fallback (%s / %s) - UNREVIEWED"
              % (result.get("provider", "?"), result.get("model", "?")))
    else:
        print("   route:  DECLINED (%s)" % route)
    if result.get("sql"):
        print("   SQL:")
        for line in str(result["sql"]).splitlines():
            print("     %s" % line)
    print("   A: %s" % str(result["answer"]).replace("\n", "\n      "))
