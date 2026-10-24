# -*- coding: utf-8 -*-
"""Single command entry point.

    python -m app                      build, gate, write out/_dq_report.json
    python -m app --ask "<question>"   build quietly, then answer with the SQL
    python -m app --demo               build, then answer all six questions
    python -m app --csv <path>         override the input file
    python -m app --ask "..." --provider openrouter --model openai/gpt-4o-mini

The six known questions are answered by intent matching against reviewed SQL --
no key, no network, no cost. Anything else falls back to LLM text-to-SQL if a
key is present in the environment (OPENROUTER_API_KEY, then OPENAI_API_KEY,
then ANTHROPIC_API_KEY), and declines cleanly if not. `--no-llm` forces the
deterministic-only behaviour, which is what CI and the eval harness use.

Exit codes, because this is meant to run in CI as well as on a laptop:

    0   the data passed the gate -- outputs are publishable
    1   the data FAILED the gate -- the program worked, the data did not
    2   the program broke -- a bug, a missing file, a bad contract

Separating 1 from 2 is the point. A build system has to be able to tell "your
export is bad today" from "the pipeline is broken", because those go to
different people.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import duckdb

from .contract import Contract
from .gate import print_report, run_gate, write_report
from .nl import PROVIDERS, ask, detect_provider, load_env_file, print_answer
from .pipeline import build

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "cloudnova_invoices.csv"

# The six questions from data/BUSINESS_CONTEXT.md, verbatim in intent.
DEMO_QUESTIONS = [
    "What was our total recognized revenue in USD for paid invoices in 2024?",
    "Which region has the highest average MRR per account?",
    "How much have we given back in refunds?",
    "What's the churn rate among Enterprise accounts vs Starter?",
    "List the top 5 accounts by revenue, and flag any with CSAT <= 2.",
    "How many invoices are stuck in pending/failed and what's the exposed $?",
]

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_BROKEN = 2


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m app",
        description="CloudNova revenue intelligence: build, gate, ask.")
    p.add_argument("--csv", default=None,
                   help="input CSV (default data/cloudnova_invoices.csv)")
    p.add_argument("--ask", default=None, metavar="QUESTION",
                   help="answer one stakeholder question, showing the SQL")
    p.add_argument("--demo", action="store_true",
                   help="run the pipeline then answer all six questions")
    p.add_argument("--contract", default=None,
                   help="override specs/contract.yaml")
    p.add_argument("--provider", default=None,
                   choices=[x["name"] for x in PROVIDERS],
                   help="force an LLM provider for the text-to-SQL fallback "
                        "(default: whichever key is set, OpenRouter first)")
    p.add_argument("--model", default=None,
                   help="override the provider's default model")
    p.add_argument("--no-llm", action="store_true",
                   help="never call a model; unmatched questions decline")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        csv_path = Path(args.csv) if args.csv else DEFAULT_CSV
        if not csv_path.is_file():
            print("ERROR: input CSV not found: %s" % csv_path, file=sys.stderr)
            return EXIT_BROKEN

        c = Contract.load(Path(args.contract) if args.contract else None)
        con = duckdb.connect()
        build(con, str(csv_path), c)

        report = run_gate(con, c)
        path = write_report(report, c)

        quiet = bool(args.ask) or bool(args.demo)
        if not quiet:
            print_report(report)
            print("  report: %s" % path)
            return EXIT_OK if report["passed"] else EXIT_GATE_FAILED

        # --ask / --demo: the gate still runs, because an answer drawn from data
        # that failed the gate must say so rather than arrive looking clean.
        if report["passed"]:
            print("gate: PASSED (contract %s, %d checks) - report %s"
                  % (report["contract_version"], len(report["checks"]), path))
        else:
            print("gate: FAILED - %d breach(es). Figures below are NOT "
                  "publishable. See %s" % (len(report["breaches"]), path))
            for b in report["breaches"]:
                print("  - %s" % b)
        print("")

        use_llm = not args.no_llm
        if use_llm:
            # Folds a .env into the environment if one exists. Keys are read
            # from the environment only and are never printed.
            load_env_file()
            p = detect_provider(args.provider)
            print("llm fallback: %s (unmatched questions only)"
                  % ("%s / %s" % (p["name"], args.model or p["model"])
                     if p else "OFF - no API key in the environment"))
        else:
            print("llm fallback: OFF (--no-llm)")
        print("")

        questions = DEMO_QUESTIONS if args.demo else [args.ask]
        for q in questions:
            print_answer(ask(con, c, q, use_llm=use_llm,
                             provider=args.provider, model=args.model))
            print("")

        return EXIT_OK if report["passed"] else EXIT_GATE_FAILED

    except Exception:  # noqa: BLE001 -- the top-level boundary, on purpose
        traceback.print_exc()
        print("ERROR: the pipeline itself failed (exit 2), which is a code or "
              "configuration fault, not a data-quality failure.",
              file=sys.stderr)
        return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
