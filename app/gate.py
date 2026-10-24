# -*- coding: utf-8 -*-
"""The quality gate: seven checks that decide publish or stop.

Every threshold, comparison and check id is read from specs/contract.yaml via
`Contract`. Nothing here is hardcoded, so the client's finance lead can loosen a
threshold by editing the contract -- no developer, no release.

The gate measures COUNTS AND MEASURES ONLY. No row contents ever reach the
report or the console: a data-quality report that quotes customer rows is a
privacy problem, and the report is meant to travel (CI logs, email, a ticket).

DQ06 is the flagship. Recognised revenue summed from `clean` must equal the
gold revenue table to the cent. That is the check that catches the two bugs
which survive code review -- a filter lost somewhere between clean and gold,
and a join fan-out that multiplies rows -- because both change the total while
leaving every individual row looking correct.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from .contract import Contract

ROOT = Path(__file__).resolve().parent.parent

# Comparison operators referenced by the contract's `comparison` field.
_COMPARE = {
    ">=": lambda measured, threshold: measured >= threshold,
    "<=": lambda measured, threshold: measured <= threshold,
    "==": lambda measured, threshold: measured == threshold,
    ">": lambda measured, threshold: measured > threshold,
    "<": lambda measured, threshold: measured < threshold,
    "!=": lambda measured, threshold: measured != threshold,
}


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    return None if row is None else row[0]


def _num(value: Any) -> float:
    """Decimals and NULLs out of DuckDB, floats into the report."""
    return 0.0 if value is None else float(value)


# ---------------------------------------------------------------------------
# measures
# ---------------------------------------------------------------------------
def measure(con: duckdb.DuckDBPyConnection, c: Contract) -> dict[str, Any]:
    """Compute every measure the contract's checks refer to, by name."""
    raw_rows = int(_num(_scalar(con, "SELECT count(*) FROM raw")))
    clean_rows = int(_num(_scalar(con, "SELECT count(*) FROM clean")))
    quarantined = int(_num(_scalar(con, "SELECT count(*) FROM quarantine")))

    # Rows that passed the quarantine filters but BEFORE de-duplication.
    # raw minus quarantine is that population by construction: pipeline._clean
    # applies exactly the inverse of the quarantine predicate.
    surviving = raw_rows - quarantined
    distinct_ids = int(_num(_scalar(
        con, "SELECT count(DISTINCT invoice_id) FROM clean")))
    duplicates = surviving - distinct_ids

    recognised = ", ".join("'%s'" % s for s in c.recognised_status)
    clean_recognised = _num(_scalar(
        con,
        "SELECT coalesce(sum(amount_usd), 0) FROM clean "
        "WHERE status_canonical IN (%s)" % recognised))
    gold_recognised = _num(_scalar(
        con,
        "SELECT coalesce(sum(recognised_revenue_usd), 0) "
        "FROM gold_revenue_by_year"))

    netting = ", ".join("'%s'" % s for s in c.netting_status)
    negative_outside_refunded = int(_num(_scalar(
        con,
        "SELECT count(*) FROM clean "
        "WHERE amount_usd < 0 AND status_canonical NOT IN (%s)" % netting)))

    return {
        "raw_row_count": raw_rows,
        "clean_row_count": clean_rows,
        "quarantine_row_count": quarantined,
        "quarantine_rate": round(quarantined / raw_rows, 6) if raw_rows else 1.0,
        "surviving_row_count": surviving,
        "distinct_invoice_id_count": distinct_ids,
        "duplicate_row_count": duplicates,
        "duplicate_rate": round(duplicates / raw_rows, 6) if raw_rows else 0.0,
        "clean_pk_duplicate_count": clean_rows - distinct_ids,
        "clean_recognised_revenue_usd": round(clean_recognised, 2),
        "gold_recognised_revenue_usd": round(gold_recognised, 2),
        "revenue_reconciliation_abs_diff": round(
            abs(clean_recognised - gold_recognised), 2),
        "negative_amount_outside_refunded_count": negative_outside_refunded,
    }


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------
def run_gate(con: duckdb.DuckDBPyConnection, c: Contract) -> dict[str, Any]:
    """Run every check declared in the contract. Returns the full report dict."""
    measures = measure(con, c)
    results: list[dict[str, Any]] = []
    breaches: list[str] = []

    for check in c.checks:
        check_id = str(check["id"])
        name = str(check["measure"])
        comparison = str(check["comparison"])
        threshold = float(check["threshold"])

        if name not in measures:
            # A check exists in the contract that the code cannot measure. That
            # is a contract/code drift and must fail loudly, not be skipped.
            results.append({
                "id": check_id, "measure": name, "comparison": comparison,
                "threshold": threshold, "measured": None, "passed": False,
                "rationale": str(check.get("rationale", "")).strip(),
                "error": "no such measure implemented",
            })
            breaches.append("%s %s: measure not implemented" % (check_id, name))
            continue

        measured = measures[name]
        passed = _COMPARE[comparison](float(measured), threshold)
        results.append({
            "id": check_id, "measure": name, "comparison": comparison,
            "threshold": threshold, "measured": measured, "passed": bool(passed),
            "rationale": str(check.get("rationale", "")).strip(),
        })
        if not passed:
            breaches.append(
                "%s %s = %s, required %s %s"
                % (check_id, name, _fmt(measured), comparison, _fmt(threshold)))

    gold_present = {
        t["name"]: int(_num(_scalar(con, "SELECT count(*) FROM %s" % t["name"])))
        for t in c.gold_tables
    }

    return {
        "contract_version": c.version,
        "contract_path": str(c.path),
        "passed": not breaches,
        "measures": measures,
        "checks": results,
        "breaches": breaches,
        "gold_row_counts": gold_present,
    }


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return ("%.6f" % value).rstrip("0").rstrip(".") or "0"
    return str(value)


def print_report(report: dict[str, Any]) -> None:
    m = report["measures"]
    print("DATA-QUALITY GATE   contract %s" % report["contract_version"])
    print("  raw %d -> clean %d, quarantined %d, duplicates %d"
          % (m["raw_row_count"], m["clean_row_count"],
             m["quarantine_row_count"], m["duplicate_row_count"]))
    for r in report["checks"]:
        print("  %-5s %-38s %-2s %9s %11s  %s"
              % (r["id"], r["measure"], r["comparison"],
                 _fmt(r["threshold"]),
                 "n/a" if r["measured"] is None else _fmt(r["measured"]),
                 "PASS" if r["passed"] else "FAIL"))
    print("  recognised revenue: clean %.2f vs gold %.2f (diff %.2f)"
          % (m["clean_recognised_revenue_usd"],
             m["gold_recognised_revenue_usd"],
             m["revenue_reconciliation_abs_diff"]))
    if report["passed"]:
        print("  GATE PASSED - outputs are publishable.")
    else:
        print("  GATE FAILED - outputs withheld. %d breach(es):"
              % len(report["breaches"]))
        for b in report["breaches"]:
            print("    - %s" % b)


def report_path(c: Contract) -> Path:
    """From the contract, so the client can redirect it without a code change."""
    rel = c._raw["quality_gate"].get("report_path", "out/_dq_report.json")
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p)


def write_report(report: dict[str, Any], c: Contract) -> Path:
    path = report_path(c)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path
