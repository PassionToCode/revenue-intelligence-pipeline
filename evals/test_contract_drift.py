# -*- coding: utf-8 -*-
"""Contract-vs-code drift tests. Plain pytest + PyYAML.

    python -m pytest evals -q

A "contract-driven build" is only a real claim if something BREAKS when the
code and specs/contract.yaml drift apart. These tests are that something.

Each test fails on a specific, nameable kind of drift:

    canonical values   the pipeline invents a category the contract never
                       declared, or silently buckets an unknown one
    quarantine reasons the contract promises a named rejection reason the code
                       cannot actually produce
    gold tables        the contract advertises a table to a stakeholder that
                       the build does not create
    gate checks        a check id exists in the contract that run_gate does not
                       measure, so a quality rule is quietly unenforced
    hardcoded values   an FX rate, a list price or a threshold has been pasted
                       into app/*.py, which means editing the contract no
                       longer changes the behaviour it claims to control

The last one is the test that keeps the contract honest. Everything else can
drift back into agreement; a hardcoded 1.08 cannot, because the next person to
change the EUR rate in the contract will believe they changed the output.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contract import Contract  # noqa: E402
from app.gate import run_gate  # noqa: E402
from app.pipeline import build  # noqa: E402

CONTRACT_PATH = ROOT / "specs" / "contract.yaml"
CSV_PATH = ROOT / "data" / "cloudnova_invoices.csv"
APP_DIR = ROOT / "app"


# ---------------------------------------------------------------------------
# fixtures -- the warehouse is built ONCE for the whole module
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def contract() -> Contract:
    return Contract.load(CONTRACT_PATH)


@pytest.fixture(scope="module")
def raw_contract() -> dict:
    """The contract as plain YAML, so a test cannot be fooled by an accessor.

    Reading through Contract for both sides of a comparison would let a bug in
    Contract hide the drift it is supposed to expose.
    """
    with open(CONTRACT_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def con(contract: Contract):
    connection = duckdb.connect()
    build(connection, str(CSV_PATH), contract)
    yield connection
    connection.close()


def _distinct(con, table: str, column: str) -> set:
    rows = con.execute(
        "SELECT DISTINCT %s FROM %s WHERE %s IS NOT NULL"
        % (column, table, column)).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# 1. canonical values
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "column"),
    [("status", "status_canonical"),
     ("plan", "plan_canonical"),
     ("billing_cycle", "billing_cycle_canonical")],
)
@pytest.mark.parametrize("table", ["typed", "clean"])
def test_normalisation_produces_only_contract_canonical_values(
        con, raw_contract, table, field, column):
    """No category may exist in the data that the contract never declared.

    This is the test that catches a value being guessed into a bucket. The
    contract's whole promise is that an unrecognised status is QUARANTINED with
    a named reason, never nudged into `paid`.
    """
    declared = {str(k) for k in raw_contract["normalise"][field].keys()}
    produced = {str(v) for v in _distinct(con, table, column)}
    unexpected = produced - declared
    assert not unexpected, (
        "%s.%s produced %s, which the contract's normalise.%s does not declare "
        "(declared: %s)" % (table, column, sorted(unexpected), field,
                            sorted(declared)))


def test_clean_statuses_are_partitioned_by_the_revenue_rules(contract, con):
    """Every canonical status must be classified as recognised, netting or
    excluded. A status in the contract that belongs to none of the three is a
    revenue rule nobody wrote, and it would silently vanish from every total."""
    classified = set(contract.recognised_status) | set(contract.netting_status) \
        | set(contract._raw["revenue"]["excluded_status"])
    present = {str(v) for v in _distinct(con, "clean", "status_canonical")}
    assert present <= classified, (
        "statuses present in `clean` but not classified by the contract's "
        "revenue rules: %s" % sorted(present - classified))


# ---------------------------------------------------------------------------
# 2. quarantine reasons
# ---------------------------------------------------------------------------
def test_every_quarantine_reason_is_implemented(contract):
    """Every reason the contract promises must be producible by the code.

    Asserted against the SOURCE of app/pipeline.py rather than against the
    quarantine table, deliberately: a clean export exercises only some reasons,
    so checking the data would let an unimplemented reason pass whenever the
    source happens to be tidy -- exactly when nobody is looking.
    """
    source = (APP_DIR / "pipeline.py").read_text(encoding="utf-8")
    missing = [r for r in contract.quarantine_reasons
               if "'%s'" % r not in source and '"%s"' % r not in source]
    assert not missing, (
        "contract promises quarantine reason(s) app/pipeline.py cannot produce: "
        "%s" % missing)


def test_no_quarantine_reason_exists_outside_the_contract(contract, con):
    """The reverse direction: the code must not invent a reason either."""
    rows = con.execute(
        "SELECT DISTINCT reject_reason FROM quarantine "
        "WHERE reject_reason IS NOT NULL").fetchall()
    produced = {r[0] for r in rows}
    undeclared = produced - set(contract.quarantine_reasons)
    assert not undeclared, (
        "quarantine produced reason(s) the contract does not declare: %s"
        % sorted(undeclared))


def test_no_row_is_rejected_without_a_reason(con):
    """A rejection with a NULL reason is an anonymous deletion with extra steps."""
    orphans = con.execute(
        "SELECT count(*) FROM quarantine WHERE reject_reason IS NULL").fetchone()[0]
    assert orphans == 0, "%d quarantined row(s) carry no named reason" % orphans


# ---------------------------------------------------------------------------
# 3. gold tables
# ---------------------------------------------------------------------------
def test_every_contract_gold_table_exists_after_a_build(contract, con):
    """The contract advertises one table per stakeholder question. If one is
    missing, a question the client was told is answered is not answered."""
    existing = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    declared = [t["name"] for t in contract.gold_tables]
    missing = [name for name in declared if name not in existing]
    assert not missing, "contract declares gold table(s) the build never creates: %s" % missing


def test_every_gold_table_has_a_stakeholder_question(contract):
    """A gold table with no question attached is an output nobody asked for."""
    unlabelled = [t["name"] for t in contract.gold_tables
                  if not str(t.get("question", "")).strip()]
    assert not unlabelled, (
        "gold table(s) declared with no stakeholder question: %s" % unlabelled)


def test_no_gold_table_is_built_that_the_contract_never_declared(contract, con):
    """A gold_* table outside the contract is an undocumented deliverable."""
    built = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name LIKE 'gold_%'").fetchall()}
    declared = {t["name"] for t in contract.gold_tables}
    assert not (built - declared), (
        "built gold table(s) the contract does not declare: %s"
        % sorted(built - declared))


# ---------------------------------------------------------------------------
# 4. gate checks
# ---------------------------------------------------------------------------
def test_every_contract_check_is_implemented_by_the_gate(contract, con):
    """A check id in the contract that run_gate does not report is a quality
    rule the client believes is enforced and which is not running."""
    report = run_gate(con, contract)
    reported = {str(r["id"]) for r in report["checks"]}
    declared = {str(ch["id"]) for ch in contract.checks}
    assert declared == reported, (
        "gate/contract check drift -- declared but not reported: %s; reported "
        "but not declared: %s"
        % (sorted(declared - reported), sorted(reported - declared)))


def test_no_gate_check_measures_nothing(contract, con):
    """run_gate marks a check it cannot measure with an `error`. That is drift,
    not a pass, and it must fail the build rather than appear as a blank row."""
    report = run_gate(con, contract)
    broken = [(r["id"], r.get("error"), r["measure"]) for r in report["checks"]
              if r.get("error") or r.get("measured") is None]
    assert not broken, "check(s) the gate cannot measure: %s" % broken


def test_every_gate_check_uses_a_declared_comparison_and_threshold(contract, con):
    """Each reported check must carry back the contract's own threshold, so a
    threshold edited in the contract provably reaches the gate."""
    report = run_gate(con, contract)
    by_id = {str(r["id"]): r for r in report["checks"]}
    for ch in contract.checks:
        cid = str(ch["id"])
        assert by_id[cid]["comparison"] == str(ch["comparison"]), (
            "%s comparison drift: contract %r, gate %r"
            % (cid, ch["comparison"], by_id[cid]["comparison"]))
        assert abs(float(by_id[cid]["threshold"]) - float(ch["threshold"])) < 1e-9, (
            "%s threshold drift: contract %r, gate %r"
            % (cid, ch["threshold"], by_id[cid]["threshold"]))


def test_every_gate_check_has_a_rationale(contract):
    """A threshold with no stated reason cannot be argued with by the client,
    so it will be ignored the first time it is inconvenient."""
    silent = [ch["id"] for ch in contract.checks
              if not str(ch.get("rationale", "")).strip()]
    assert not silent, "check(s) with no rationale: %s" % silent


# ---------------------------------------------------------------------------
# 5. THE ONE THAT KEEPS THE CONTRACT HONEST -- no hardcoded business values
# ---------------------------------------------------------------------------
# Values that must live in specs/contract.yaml and nowhere else. The FX rates
# and list prices come from data/BUSINESS_CONTEXT.md; 0.15 is the DQ03
# quarantine-rate tolerance.
FORBIDDEN_LITERALS = ["1.08", "1.27", "0.15", "49", "99", "299"]

# Narrow, documented exemptions. Each is an exact snippet that happens to
# contain a forbidden digit string but is provably not a business value. The
# list is asserted to be fully USED, so an exemption cannot quietly outlive the
# code it excuses.
ALLOWED_SNIPPETS = {
    "ELSE 99 END":
        "SQL CASE sentinel ranking an unknown status last in the dedup "
        "window function. A sort key, not a price.",
}

# app/contract.py is the one module allowed to hold these values, because its
# entire job is reading them out of the contract.
EXEMPT_FILES = {"contract.py"}


def _app_sources() -> list[Path]:
    return sorted(p for p in APP_DIR.glob("*.py") if p.name not in EXEMPT_FILES)


def test_app_sources_were_actually_found():
    """Guard against the whole hardcoding test silently passing on zero files."""
    names = [p.name for p in _app_sources()]
    assert len(names) >= 3, "expected to scan several app modules, found %s" % names


@pytest.mark.parametrize("literal", FORBIDDEN_LITERALS)
def test_no_business_value_is_hardcoded_outside_the_contract(literal):
    """Fail if an FX rate, list price or threshold appears in app/*.py.

    Without this test "nothing business-related is hardcoded" is a comment, not
    a property. The failure mode it prevents is the expensive one: the finance
    lead edits the EUR rate in the contract, the build runs green, and every
    figure is still computed at the old rate.

    Matching is on standalone numeric tokens, so `DECIMAL(18,2)`, `round(x, 2)`
    and `100.0` are not false positives.
    """
    pattern = re.compile(r"(?<![\w.])%s(?![\w.])" % re.escape(literal))
    offences: list[str] = []
    for path in _app_sources():
        text = path.read_text(encoding="utf-8")
        for snippet in ALLOWED_SNIPPETS:
            text = text.replace(snippet, " ")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offences.append("%s:%d: %s" % (path.name, lineno, line.strip()))
    assert not offences, (
        "business value %r is hardcoded in app/ -- it must come from "
        "specs/contract.yaml only:\n  %s" % (literal, "\n  ".join(offences)))


@pytest.mark.parametrize("literal", FORBIDDEN_LITERALS)
def test_every_forbidden_value_really_is_in_the_contract(literal):
    """The other half of the claim: these values must exist in exactly one
    place, and this asserts that place is the contract. Without it, the test
    above would also pass if someone deleted the FX table altogether."""
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    assert re.search(r"(?<![\w.])%s(?![\w.])" % re.escape(literal), text), (
        "%r is forbidden in app/ but is not present in specs/contract.yaml -- "
        "the value now lives nowhere and the rule is vacuous" % literal)


def test_every_hardcoding_exemption_is_still_in_use():
    """An exemption that no longer matches any source line is a hole left open."""
    joined = "\n".join(p.read_text(encoding="utf-8") for p in _app_sources())
    stale = [s for s in ALLOWED_SNIPPETS if s not in joined]
    assert not stale, (
        "hardcoding exemption(s) no longer present in app/ and must be removed "
        "so the rule is not weakened for nothing: %s" % stale)


def test_fx_rates_and_prices_in_the_contract_match_the_business_context(raw_contract):
    """The contract is the code's source of truth; data/BUSINESS_CONTEXT.md is
    the client's. If those two drift, the build is correct against the wrong
    brief, which is the hardest kind of wrong to notice."""
    brief = (ROOT / "data" / "BUSINESS_CONTEXT.md").read_text(encoding="utf-8")
    for currency, rate in raw_contract["fx_to_usd"].items():
        if currency == "USD":
            continue
        assert str(rate) in brief, (
            "contract FX %s=%s is not the rate stated in "
            "data/BUSINESS_CONTEXT.md" % (currency, rate))
    prices = raw_contract["pricing"]["list_price_per_seat_per_month_usd"]
    for plan, price in prices.items():
        assert "$%d" % int(price) in brief or "`$%d`" % int(price) in brief, (
            "contract list price %s=%s is not the price stated in "
            "data/BUSINESS_CONTEXT.md" % (plan, price))


# ---------------------------------------------------------------------------
# 6. parsing and dedup rules reach the code
# ---------------------------------------------------------------------------
def test_every_contract_date_format_is_attempted(contract):
    """A date format listed in the contract but never tried means rows are
    quarantined as unparseable while the contract says they are supported."""
    source = (APP_DIR / "pipeline.py").read_text(encoding="utf-8")
    assert "c.date_formats" in source, (
        "app/pipeline.py does not read date_formats from the contract")
    for fmt in contract.date_formats:
        assert fmt in CONTRACT_PATH.read_text(encoding="utf-8"), fmt


def test_dedup_precedence_covers_every_canonical_status(contract, raw_contract):
    """A status missing from status_precedence falls to the sentinel rank, so
    which of two conflicting rows wins becomes arbitrary -- and the losing row's
    revenue silently disappears."""
    declared = {str(k) for k in raw_contract["normalise"]["status"].keys()}
    precedence = set(contract.status_precedence)
    assert declared == precedence, (
        "dedup status_precedence does not cover every canonical status -- "
        "missing: %s; unknown: %s"
        % (sorted(declared - precedence), sorted(precedence - declared)))


def test_cleaned_grain_is_one_row_per_invoice_id(con):
    """The contract's stated grain, asserted rather than assumed."""
    rows, distinct = con.execute(
        "SELECT count(*), count(DISTINCT invoice_id) FROM clean").fetchone()
    assert rows == distinct, (
        "clean holds %d rows for %d distinct invoice_ids -- the contract's "
        "grain is one row per invoice_id" % (rows, distinct))


# ---------------------------------------------------------------------------
# 7. the gate must be proven to FAIL, not only to pass
# ---------------------------------------------------------------------------
# A gate only ever observed passing is not evidence of anything -- it could be
# returning True unconditionally and nobody would know. So every check DQ01-DQ07
# gets a case that breaks exactly the property it guards and asserts that THAT
# check reports a breach.
#
# The breakage is applied to the built warehouse rather than to seven separate
# broken CSV files. Same effect on the measure, and it isolates one property per
# case: a hand-written broken CSV trips several checks at once, so a test built
# on one could pass while the check it names is dead.
#
# Collateral breaches are expected and ignored -- each case asserts only that
# its own check failed and that the overall gate stopped the run.
_BREAKAGE = {
    # measure                               SQL that breaks it
    "DQ01": ["DELETE FROM raw"],
    "DQ02": ["DELETE FROM clean"],
    # quarantine_rate = quarantined / raw. Shrinking raw to 10 rows takes the
    # 2 genuine rejects to 20%, past the contract's 15% tolerance.
    "DQ03": ["DELETE FROM raw WHERE _source_row > 10"],
    # duplicate_rate = (raw - quarantined - distinct clean ids) / raw.
    "DQ04": ["DELETE FROM clean WHERE _source_row > 1000"],
    # one extra copy of a cleaned row breaks the one-row-per-invoice_id grain.
    "DQ05": ["INSERT INTO clean SELECT * FROM clean LIMIT 1"],
    # a cent of drift between clean and gold -- the lost-filter / join-fan-out
    # signature DQ06 exists to catch.
    "DQ06": ["UPDATE gold_revenue_by_year "
             "SET recognised_revenue_usd = recognised_revenue_usd + 1"],
    # a negative amount on an invoice that is not a refund.
    "DQ07": ["UPDATE clean SET amount_usd = -1 WHERE invoice_id = "
             "(SELECT min(invoice_id) FROM clean "
             "WHERE status_canonical = 'paid')"],
}


@pytest.fixture()
def broken_con(contract: Contract):
    """A throwaway warehouse, rebuilt per test so breakage cannot leak."""
    connection = duckdb.connect()
    build(connection, str(CSV_PATH), contract)
    yield connection
    connection.close()


def test_the_gate_passes_on_the_real_export(con, contract):
    """The control case. Without it, a gate that failed on EVERYTHING would
    satisfy all seven failure tests below and look thoroughly verified."""
    report = run_gate(con, contract)
    assert report["passed"], (
        "gate breached on the unmodified export: %s" % report["breaches"])


@pytest.mark.parametrize("check_id", sorted(_BREAKAGE))
def test_each_gate_check_actually_fails_when_breached(
        broken_con, contract, check_id):
    """Break the property one check guards; assert that check reports a breach."""
    for statement in _BREAKAGE[check_id]:
        broken_con.execute(statement)

    report = run_gate(broken_con, contract)
    by_id = {str(r["id"]): r for r in report["checks"]}
    assert check_id in by_id, "%s is not reported by the gate at all" % check_id

    assert not by_id[check_id]["passed"], (
        "%s (%s %s %s) still PASSED after its property was deliberately "
        "broken; measured %r. An untestable check is an unenforced check."
        % (check_id, by_id[check_id]["measure"], by_id[check_id]["comparison"],
           by_id[check_id]["threshold"], by_id[check_id]["measured"]))

    assert not report["passed"], (
        "%s breached but the gate as a whole reported PASS -- the run would "
        "publish a figure the contract forbids" % check_id)
    assert any(check_id in b for b in report["breaches"]), (
        "%s breached but is not named in report['breaches'], so the operator "
        "is never told which rule stopped the run" % check_id)


def test_every_contract_check_has_a_failure_case(contract):
    """Adding a check to the contract without a failure case here would quietly
    land an unverified quality rule. This test makes that impossible."""
    declared = {str(ch["id"]) for ch in contract.checks}
    assert declared == set(_BREAKAGE), (
        "check(s) with no proof they can fail: %s; failure case(s) for a check "
        "that no longer exists: %s"
        % (sorted(declared - set(_BREAKAGE)), sorted(set(_BREAKAGE) - declared)))
