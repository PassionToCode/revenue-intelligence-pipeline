# -*- coding: utf-8 -*-
"""Eval harness: build the warehouse once, then run every case in cases.yaml.

Run it with:

    python evals/run_evals.py

What it does, and why it is shaped this way:

* The pipeline is built ONCE, in memory, and every case queries that one
  warehouse. Rebuilding per case would turn nine cases into nine CSV parses and
  would also hide a bug class worth catching -- state that leaks between
  questions.

* The quality gate runs first, as a PRECONDITION. Asserting a revenue figure
  from an export the gate has rejected would be asserting on numbers nobody is
  allowed to publish, so a gate breach fails the whole harness up front.

* A case marked `expect: fail` is asserted NOT to be answerable. When the system
  declines, that is a harness PASS -- it behaved exactly as documented. When it
  answers anyway, that is a loud harness FAILURE, printed as UNEXPECTED SUCCESS,
  because a documented limitation has silently stopped being true and the
  written docs are now wrong. That asymmetry is the whole point of carrying a
  negative case.

Exit code: 0 only when every case behaved as declared. Non-zero otherwise, so
CI and the Makefile can gate on it.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import yaml  # noqa: E402

from app.contract import Contract  # noqa: E402
from app.pipeline import build  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "cases.yaml"
CSV_PATH = ROOT / "data" / "invoices.csv"

# app/gate.py and app/nl.py are written by a parallel worker. Wait for them
# rather than shipping a second, divergent copy of either.
_WAIT_SECONDS = 60
_POLL_SECONDS = 3


def _import_siblings() -> tuple[Any, Any]:
    """Import run_gate and ask, retrying briefly while they are being written."""
    deadline = time.time() + _WAIT_SECONDS
    last = ""
    while True:
        try:
            from app.gate import run_gate
            from app.nl import ask
            return run_gate, ask
        except Exception as exc:  # noqa: BLE001
            last = "%s: %s" % (type(exc).__name__, exc)
            if time.time() >= deadline:
                print("CANNOT START: app.gate / app.nl unavailable after %ds "
                      "(%s)" % (_WAIT_SECONDS, last))
                raise SystemExit(2)
            print("  waiting for app.gate / app.nl ... (%s)" % last)
            time.sleep(_POLL_SECONDS)


# ---------------------------------------------------------------------------
# assertion evaluation
# ---------------------------------------------------------------------------
_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "float": float, "int": int, "len": len,
    "list": list, "max": max, "min": min, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "range": range, "True": True, "False": False, "None": None,
}


def _scalar_factory(con: duckdb.DuckDBPyConnection):
    def sql(query: str) -> Any:
        row = con.execute(query).fetchone()
        if row is None:
            return None
        value = row[0]
        # DuckDB hands back Decimal for DECIMAL(18,2); compare as float so a
        # case can write `> 0` or `<= 0.01` without caring about the SQL type.
        try:
            return float(value) if value is not None and not isinstance(
                value, (str, bool)) else value
        except (TypeError, ValueError):
            return value
    return sql


def _rows_factory(con: duckdb.DuckDBPyConnection):
    def sql_rows(query: str) -> list[tuple]:
        return [tuple(r) for r in con.execute(query).fetchall()]
    return sql_rows


def _evaluate(expr: str, namespace: dict[str, Any]) -> tuple[bool, str]:
    """Return (held, detail). An exception is a failed assertion, not a crash.

    The expression is flattened to a single line first. YAML's folded scalar
    (`>`) keeps a real newline in front of any line indented further than the
    first, which is exactly how a readable multi-line assertion is written in
    cases.yaml -- and `eval` would then reject it as an IndentationError rather
    than evaluating it. Collapsing whitespace is safe here because the only
    strings inside an expression are single-spaced SQL.
    """
    # Wrapped in parentheses as well as flattened. Flattening alone was enough
    # on Windows but failed in CI on Linux with "IndentationError: unexpected
    # indent" -- git normalises line endings on push, so a YAML folded scalar
    # that collapsed to one line locally kept a newline on the runner.
    # Parenthesising makes any residual newline or indent syntactically
    # harmless, which is the difference between "works on my machine" and works.
    flat = "(" + " ".join(str(expr).split()) + ")"
    try:
        value = eval(flat, {"__builtins__": _SAFE_BUILTINS}, namespace)  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        return False, "raised %s: %s" % (type(exc).__name__, exc)
    return bool(value), "" if value else "evaluated false"


# ---------------------------------------------------------------------------
def _ask_case(ask: Any, con: duckdb.DuckDBPyConnection, c: Contract,
              case: dict[str, Any]) -> dict[str, Any]:
    """Put the question through the NL layer, or straight to SQL for `route: sql`."""
    if str(case.get("route", "nl")).lower() == "sql":
        # A warehouse invariant, asserted directly. Not a declined question --
        # it was never routed through the NL layer at all.
        return {"intent": None, "matched": True, "sql": "", "rows": [],
                "answer": "(warehouse invariant, asserted directly in SQL)",
                "routed": "sql"}
    result = ask(con, c, str(case["question"]))
    result["routed"] = "nl"
    return result


def _namespace(result: dict[str, Any], con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    declined = (result.get("routed") != "sql") and not result.get("matched", False)
    return {
        "intent": result.get("intent"),
        "answer": str(result.get("answer") or "").lower(),
        "rows": list(result.get("rows") or []),
        "sql_text": str(result.get("sql") or ""),
        "declined": declined,
        "sql": _scalar_factory(con),
        "sql_rows": _rows_factory(con),
    }


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    # ASCII only: this console is cp1252 and a box-drawing character or an
    # ellipsis raises UnicodeEncodeError mid-report, which would hide the very
    # failure the harness exists to print.
    return text if len(text) <= width else text[: width - 3] + "..."


# ---------------------------------------------------------------------------
def main() -> int:
    run_gate, ask = _import_siblings()

    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    c = Contract.load()

    print("=" * 108)
    print("EVAL HARNESS   contract %s   %d cases   %s"
          % (c.version, len(cases), CASES_PATH))
    print("=" * 108)

    con = duckdb.connect()
    build(con, str(CSV_PATH), c)

    # -- precondition: the gate -------------------------------------------
    report = run_gate(con, c)
    gate_ok = bool(report["passed"])
    print("PRECONDITION  quality gate: %s   (%d checks, raw %d -> clean %d, "
          "quarantined %d)"
          % ("PASS" if gate_ok else "FAIL",
             len(report["checks"]), report["measures"]["raw_row_count"],
             report["measures"]["clean_row_count"],
             report["measures"]["quarantine_row_count"]))
    if not gate_ok:
        for breach in report["breaches"]:
            print("                - %s" % breach)
        print("              No revenue figure may be asserted from an export "
              "the gate has rejected.")
    print("-" * 108)
    print("%-34s %-44s %-8s %-8s %s"
          % ("CASE", "QUESTION", "EXPECT", "ACTUAL", "RESULT"))
    print("-" * 108)

    ok_count = 0
    failures: list[tuple[str, str]] = []
    unexpected_successes: list[str] = []

    for case in cases:
        cid = str(case["id"])
        expect = str(case.get("expect", "pass")).lower()
        try:
            result = _ask_case(ask, con, c, case)
        except Exception:  # noqa: BLE001
            print("%-34s %-44s %-8s %-8s %s"
                  % (cid, _truncate(case["question"], 44), expect, "error",
                     "FAIL"))
            failures.append((cid, "ask() raised:\n%s"
                             % traceback.format_exc(limit=4)))
            continue

        ns = _namespace(result, con)
        answered = not ns["declined"]

        details: list[str] = []
        all_held = True
        for a in case.get("assert", []) or []:
            held, detail = _evaluate(str(a["expr"]), ns)
            if not held:
                all_held = False
                details.append("%s  [%s]" % (str(a["desc"]), detail))

        actual = "pass" if answered else "fail"

        if expect == "fail":
            # Declining IS the documented behaviour -- that is a harness PASS.
            if answered:
                harness_ok = False
                unexpected_successes.append(cid)
                details.insert(0, "UNEXPECTED SUCCESS: the system answered a "
                                  "question documented as out of scope "
                                  "(intent %r). The documented limitation is "
                                  "no longer true and docs/ASSUMPTIONS must be "
                                  "corrected." % ns["intent"])
            else:
                harness_ok = all_held
        else:
            harness_ok = answered and all_held
            if not answered:
                details.insert(0, "the system declined a question it is "
                                  "expected to answer")

        print("%-34s %-44s %-8s %-8s %s"
              % (cid, _truncate(case["question"], 44), expect, actual,
                 "PASS" if harness_ok else "FAIL"))

        if harness_ok:
            ok_count += 1
            if expect == "fail":
                print("        '- declined as documented: %s"
                      % _truncate(case.get("why", ""), 92))
        else:
            for d in details:
                print("        '- %s" % d)
            failures.append((cid, "; ".join(details)))

    print("-" * 108)
    total = len(cases)
    print("SUMMARY  %d/%d cases behaved as declared   |   gate %s   |   "
          "%d expected-fail case(s)"
          % (ok_count, total, "PASS" if gate_ok else "FAIL",
             sum(1 for x in cases if str(x.get("expect")).lower() == "fail")))

    if unexpected_successes:
        print("!! UNEXPECTED SUCCESS in %s -- a documented limitation stopped "
              "being true. Fix the docs or the case, do not ignore this."
              % ", ".join(unexpected_successes))
    if failures:
        print("FAILED CASES: %s" % ", ".join(cid for cid, _ in failures))

    exit_code = 0 if (ok_count == total and gate_ok and not failures) else 1
    print("EXIT %d  (%s)"
          % (exit_code,
             "every case behaved as declared" if exit_code == 0
             else "at least one case did not behave as declared"))
    print("=" * 108)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
