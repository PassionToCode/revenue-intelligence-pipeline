# CLAUDE.md — CloudNova Revenue Intelligence

How the AI assistant is steered on **this** build. These are working rules for
anyone — human or model — writing code in this repository.

The project turns a dirty, three-system invoice export into trustworthy answers to
six finance questions. "Trustworthy" is the whole product. Every rule below exists
to protect that one property, because a revenue figure that is wrong but confident
is worse than no figure at all.

---

## 1. Spec and contract first, before any transform

No transform is written until the rule it implements is written down.

- `specs/SPEC.md` is the prose spec — the rule plus the reasoning.
- `specs/contract.yaml` is the machine-readable twin — what the code reads at run
  time.

If a rule is needed and neither file states it, **stop and add it to the contract
first**, then implement. The sequence is not ceremony: writing the rule down is
what exposes that it is a judgement call needing an assumption entry, and that is
the step that gets skipped when code comes first.

A rule discovered mid-build goes **into the contract**. It never becomes a special
case in a Python file "just for now".

## 2. Business logic lives in the contract, never hardcoded in Python

No price, FX rate, threshold, enum, status spelling, date format, or
de-duplication precedence order appears as a literal in a `.py` file. Each is read
from `specs/contract.yaml`.

The test: **could CloudNova's finance lead change this without a developer?** If
the answer should be yes and the value is in Python, that is a defect.

A hardcoded `0.15` or a `if status == 'paid'` inside a transform is the specific
thing this rule forbids. `evals/test_contract_drift.py` exists to fail the build
when code and contract disagree.

When editing the contract, **quote any mapping key that YAML could read as a
scalar type** — `true`, `false`, `yes`, `no`, `null`, `on`, `off`, or a bare
number. Unquoted `true:` is parsed as a boolean, never matches a string lookup,
and produces a silently empty result rather than an error. This has already cost
this build one wrong table.

## 3. Every rejected row keeps a named reason, and nothing is ever deleted

A row that cannot be cleaned goes to the quarantine table with one of the reasons
declared in `contract.yaml: quarantine_when`. Never dropped, never filtered away,
never bucketed as `other`.

Deleting bad rows makes the output look immaculate while concealing that part of
the client's business was discarded — a perfectly typed table, every check green,
and some unknown number of invoices simply missing, with no artefact anywhere
saying so.

So: **no silent `WHERE` clause that removes rows from the population.** If rows
leave the clean table, they arrive somewhere else, counted, with a reason.

Every delivered row must be accounted for in exactly one of clean or quarantine,
and the counts must add up. That property is checkable, so it is checked.

## 4. Never guess a value the contract does not know

No default branch that maps an unrecognised status to `paid`. No fuzzy matching on
plan names. No converting an unknown currency at 1.0. No treating a blank as zero.

An unrecognised value is **quarantined with a reason** or **nulled as unknown**,
per the contract — never coerced into a value that happens to make the pipeline
complete. The failure mode of guessing is silent revenue inflation that nothing
downstream can detect, and it is indistinguishable from success in the output.

`NULL` means unknown. It never means zero, false, or empty.

## 5. The quality gate must be proven to FAIL, not only to pass

A gate that has only ever been observed passing is not evidence of anything — it
could be returning `True` unconditionally and nobody would know.

For every check `DQ01`–`DQ07`, the eval suite must contain a case that **feeds
deliberately broken input and asserts that specific check fails and the process
exits non-zero.** A check without a failing test is treated as an untested check.

The gate is not advisory. It does not print a warning above a published number. It
stops the run.

Likewise, thresholds come from `contract.yaml: quality_gate`. A threshold is never
widened in order to make a run go green. If a threshold is breached, either the
data is genuinely bad — which is information, and belongs in the report — or the
threshold was wrong, in which case it is changed deliberately, in the contract,
with its rationale updated.

## 6. Money is decimal, and reconciliation is to the cent

Amounts are `DECIMAL`, never float. Binary floating point cannot represent money
exactly, and a cent of drift fails the reconciliation check for a reason that has
nothing to do with the bug the check exists to find.

`DQ06` requires recognised revenue computed from the clean table to equal the gold
table to the cent. That check is the one that catches a lost filter or a join
fan-out — the class of bug that survives review and is then found by the client's
finance team. Do not relax it.

## 7. The question layer declines rather than guesses

`--ask` matches a question to a known intent and runs reviewed, parameterised SQL.

- An unmatched question is **declined**, with the list of questions the system does
  know. It is never answered on a best guess.
- The SQL is **always printed with the answer.** Trust comes from a number
  somebody can check, not from fluent phrasing.
- Parameters are bound. SQL is never assembled by string formatting around
  user input.

A confidently stated wrong number gets forwarded into a board pack. A refusal gets
a follow-up question. Prefer the refusal.

## 8. Secrets only via `.env.example`

The default path needs **no API key and no network call**. Where an optional
provider key is required for the non-default text-to-SQL path, it is declared as a
commented-out placeholder in `.env.example`.

- Never create or populate a real `.env`.
- Never write a key, token or secret into source, config, tests, or documentation.
- Never print a secret to stdout or into a report file.

## 9. Plain language in the README, because a finance lead reads it

The reader of `README.md` is CloudNova's finance lead, not an engineer. They
should understand what this does and why in about three minutes.

- Expand a term the first time it appears. Say what a number *buys* or *means*,
  not just what it is.
- Give the business consequence, not only the mechanism. "Counting the annual lump
  sum as monthly revenue would make an annual customer look ten times bigger than
  an identical monthly one" lands; "MRR excludes billing cycle" does not.
- No stack traces, no identifiers, no table internals in the README. Those belong
  in `specs/SPEC.md`.
- Spend the extra sentence. A compressed line that has to be asked about twice
  cost more than the longer one would have.

## 10. State limitations rather than hide them

Known gaps, ambiguous decisions and failure modes are written down plainly, in
`README.md` and in `specs/ASSUMPTIONS.md`. An honest limitation is worth more than
an unstated one, and a reader who finds a hidden gap for themselves has reason to
doubt everything else in the repository.

Specifically:

- **Label every number with where it came from.** From the brief, from the
  contract, measured on a run, or derived by arithmetic — say which, in the same
  sentence. Never let a derived figure sit in a table beside a sourced one without
  marking the difference.
- **Never invent a measured result.** If a figure comes from running the pipeline
  and the pipeline has not been run, write `<filled after first run>`. A plausible
  placeholder number is a fabrication.
- **Name the judgement calls as judgement calls.** Where a decision could
  reasonably have gone the other way — the de-duplication tie-break, the date
  ambiguity, stored amount versus derived price — say so, say what changes if it is
  wrong, and record the question to put to the client. That is what
  `specs/ASSUMPTIONS.md` is for.
- "Likely", "probably" and "roughly" in front of a figure the reader cannot check
  read as confidence while carrying none. Either produce the number with its
  provenance, or say plainly that it is unknown and what would settle it.
