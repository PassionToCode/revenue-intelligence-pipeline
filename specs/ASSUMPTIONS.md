# ASSUMPTIONS

Every judgement call the pipeline makes that the data could not settle on its own.

The format is deliberate. For each assumption: **what was assumed**, **why**,
**what changes if it is wrong**, and **the question to put to CloudNova**. The
last column is the useful one — an assumption without a question attached is just
a guess with better formatting.

These are decisions an engineer made because the work could not proceed without
them. They are not CloudNova's decisions yet. Each one is declared in
`specs/contract.yaml`, so ratifying or overturning any of them is a one-line edit
to a YAML file and a re-run — not a code change.

---

## A1 — Ambiguous dates are read month-first

**Assumed.** Where a date could be read either way (`05/01/2024`, `05-01-2024`),
the month-first reading is the declared house rule: 1 May 2024, not 5 January
2024. `specs/contract.yaml: date_formats` holds the ordered list of formats; the
first one that parses a value wins.

**Why.** CloudNova is a US SaaS company and US-origin billing exports
conventionally write the month first. The rows in the delivered file that are
*not* ambiguous — those with a day component above 12 — are consistent with a
month-first reading. `data/BUSINESS_CONTEXT.md` names these dates as genuinely
ambiguous and asks for a documented rule rather than supplying one.

**What changes if it is wrong.** Affected invoices land in the wrong month, and
for dates near a year boundary, the **wrong year** — which moves money between
reporting periods in `gold_revenue_by_year` (Q1). Nothing downstream can detect
it: both readings produce a valid date that passes every type check and every
quality-gate check. This is the one error in the pipeline that is genuinely
undetectable from inside the data.

**Open discrepancy that must be settled, not just noted.** The ordered format list
implements month-first for the **slash** form but not for the **dash** form: it
contains no `%m-%d-%Y`, so `05-01-2024` falls through to `%d-%m-%Y` and is read
**day-first**. The declared assumption and the declared format list disagree for
precisely the values the briefing calls ambiguous. Fix is in `contract.yaml` —
either insert `%m-%d-%Y` ahead of `%d-%m-%Y`, or amend the comment to state that
dash-separated dates are day-first by design.

**Ask CloudNova.** Which source system emits the dash-separated dates, and what
locale convention does that system use? If any single ambiguous invoice can be
confirmed against the billing tool, that settles the whole class — and if the
source systems differ from one another, the rule has to become per-source rather
than global.

---

## A2 — For a duplicated invoice id, the record further along the payment lifecycle wins

**Assumed.** Where the same `invoice_id` appears with conflicting values, the
surviving record is chosen by status precedence `paid` > `refunded` > `failed` >
`void` > `pending`, then the later `invoice_date`, then the larger absolute amount
as a deterministic final tie-break. 124 invoice ids are affected — about 2.4% of
5,125 rows (`contract.yaml`).

**Why.** Payment status moves forward: an invoice goes from `pending` to `paid`,
not back. When the billing tool and the CRM disagree, the record showing the later
lifecycle stage is ordinarily the more recently updated one, and the system still
saying `pending` is ordinarily the one that has not caught up.

**What changes if it is wrong.** Revenue moves, and by a real amount. If the
stale system is in fact the one claiming `paid`, the pipeline recognises revenue
CloudNova never collected. The opposite rule — "the billing tool is always
authoritative" — is equally defensible on the evidence available here and would
produce a different Q1 revenue figure.

**This is a business decision currently being made by an engineer.** It is the
single assumption in this file most in need of ratification, because the output is
money in a revenue report.

**Ask CloudNova.** Which of the three source systems is the system of record for
payment status? Is there an updated-at or last-modified timestamp on either side
that we are not seeing in the export — because that would replace this inference
with a fact. And does finance already have a documented rule for this conflict?

---

## A3 — The stored invoice amount is the source of truth for revenue; the pricing rules are used only for MRR

**Assumed.** `amount` as delivered (parsed, FX-converted) is revenue. The pricing
rules are used to compute MRR and to compute an *expected* amount that is reported
as a diagnostic only. Where the two disagree, the mismatch is reported and the
stored amount stands.

**Why.** An invoice records what was actually billed, and for a `paid` row, what
the customer actually paid. The pricing rules are a model of what should have been
billed; they know nothing about proration, mid-term seat changes, negotiated
one-offs or credits. Recomputing revenue from the model would replace CloudNova's
books with an engineer's reconstruction of them.

**The disagreement is real and observable.** Row 1 of the delivered file is a
`STARTER`, `yearly`, 100-seat, `EUR` invoice stored at `52920.00`. The pricing
rules imply $49 × 100 seats × 10 months = **$49,000**. The stored amount converts
to EUR 52,920 × 1.08 ≈ **$57,154**. A gap of roughly $8,000 on one invoice, and
neither number is obviously the error.

**What changes if it is wrong.** If the stored amounts are the corrupted side,
every revenue figure in the deliverable inherits that corruption, and the pricing
rules were the correct answer all along. The pipeline would need to switch to
derived amounts — which is a one-line change given that `amount_expected_usd` is
already computed and carried on every row.

**Ask CloudNova.** For the sample mismatches we report, which figure does finance
consider correct? Does `amount` include tax, credits, or prorated adjustments that
the list-price formula is not meant to reproduce? Is `discount_pct` already
reflected in the stored amount, or applied on top of it?

---

## A4 — MRR ignores the billing cycle, so the annual lump sum is not double-counted

**Assumed.** `mrr_usd = list_price_usd[plan] × seats × (1 − discount_pct/100)`,
with **no** billing-cycle multiplier (`contract.yaml: mrr.includes_billing_cycle:
false`). An annual subscription and a monthly subscription with the same plan,
seats and discount have the same MRR.

**Why.** MRR is a monthly run-rate. An annual invoice is ten months of that
run-rate collected in a single payment (annual bills 10 months, not 12 — two
months free as the annual incentive). `data/BUSINESS_CONTEXT.md` states it
plainly: *"Annual invoices still map to the same underlying MRR — don't
double-count the annual lump sum."*

**What changes if it is wrong.** If the annual amount were carried into MRR, an
annual customer's monthly run-rate would read roughly ten times a monthly
customer's for an identical subscription, and `gold_mrr_by_region` (Q2) would
stop measuring regional run-rate and start measuring which region happens to buy
annually. The answer to "which region has the highest average MRR" would flip on
a billing preference.

**Ask CloudNova.** Confirm this matches how finance already reports MRR. And for
an account that churned part-way through an annual term, should its MRR be
included at full value, pro-rated, or dropped — the current rule includes it at
full value.

---

## A5 — Refunds net against revenue rather than being excluded from it

**Assumed.** `refunded` rows carry negative amounts and are **subtracted** from
recognised revenue. They are kept in the cleaned table and excluded from the
`paid` population (`contract.yaml: revenue.netting_status`).

**Why.** CloudNova gave the money back. Revenue that was returned is not revenue.
`data/BUSINESS_CONTEXT.md` is explicit: refunded rows *"carry a negative amount
and should net against revenue."*

**What changes if it is wrong.** If finance reports gross revenue with refunds as
a separate contra line (a common and perfectly legitimate presentation), the Q1
headline figure would be **higher** than what this pipeline reports, by the full
refund total. The two presentations are not comparable, so a mismatch here would
make the deliverable look wrong against CloudNova's own books even though the
arithmetic is sound. To make this visible rather than buried, the gold revenue
table reports gross paid revenue, total refunds and the net figure as three
separate columns.

**Ask CloudNova.** Does the headline revenue number finance publishes net refunds
inside the period, or in the period the refund was issued? A refund in March
against a January invoice lands in March under this pipeline.

---

## A6 — A blank CSAT score means unknown, not zero, and is not flagged as low

**Assumed.** A blank `csat_score` is `NULL`. It is reported as a distinct
`unknown` state and is **not** caught by the `CSAT <= 2` flag in
`gold_top_accounts` (Q5). It is excluded from any CSAT average rather than
contributing a zero.

**Why.** Zero is not on the scale — CSAT is 1–5 (`data/BUSINESS_CONTEXT.md`).
Treating a blank as zero manufactures the worst possible score from an absence of
information. The practical consequence is concrete: it would put an account on a
list titled "flag any with CSAT ≤ 2" on no evidence, in front of whoever owns that
relationship. "We don't have a score for this account" is both true and more
useful.

**What changes if it is wrong.** If a blank genuinely encodes "surveyed and
unhappy" in one of the source systems, the Q5 flag under-reports unhappy accounts.
The count of unknowns is reported on the table precisely so that the size of the
blind spot is visible rather than guessed at.

**Ask CloudNova.** Does a blank CSAT mean "never surveyed", "declined to answer",
or "the field was lost in the stitch between systems"? Only the third makes it
recoverable, and from a different source.

---

## A7 — An amount with no decimal part is whole currency units

**Assumed.** `"31432"` is **31,432.00**, not 314.32.

**Why.** Every other amount in the file is written with explicit cents
(`"$4,900.00"`, `"1,200.00 EUR"`), so the value without them is most readily
explained by a spreadsheet dropping trailing zeros rather than by cents-as-integer
storage. A cents-as-integer convention would also put these invoices two orders of
magnitude below everything else on the same plan and seat count, which is not what
the file shows.

**What changes if it is wrong.** The affected invoices are overstated by 100×.
Because invoice amounts flow straight into recognised revenue (A3), a single
mis-read large invoice can visibly distort the Q1 figure and the Q5 top-accounts
ranking. The expected-amount diagnostic (A3) is the available check: an invoice
that is 100× its expected price will show up there.

**Ask CloudNova.** Which source system emits amounts without cents, and does it
store money as an integer number of cents?

---

## A8 — The `currency` column wins over a currency code written inside the amount string

**Assumed.** For `"1,200.00 EUR"` the trailing code is stripped as formatting
noise, and the row's `currency` column determines the FX rate applied
(`contract.yaml: amount.strip_trailing_currency_codes`).

**Why.** `currency` is a typed, dedicated field documented as the invoice's
currency; the text inside the amount string is display formatting from whichever
system wrote that row. One field has to be authoritative and it should be the
declared one.

**What changes if it is wrong.** Any row where the two disagree is converted at
the wrong rate — up to a 27% error on a GBP amount labelled as USD, in either
direction. Revenue totals are affected directly, and the error is invisible in the
output because the result is still a plausible dollar figure.

**Ask CloudNova.** Should we fail the run when the embedded code and the
`currency` column disagree, rather than silently preferring one? Our
recommendation is to treat that disagreement as a quarantine reason, because a
row that contradicts itself about its own currency is not a row we should be
converting at all.

---

## A9 — An unrecognised question is declined rather than answered

**Assumed.** The `--ask` layer matches a question to a fixed set of known intents
and runs reviewed, parameterised SQL. A question outside that set gets an explicit
decline and the list of questions the system does know. It does not get a number.

**Why.** The ask was to *"actually trust the answers"*. A confidently stated wrong
number is worse than a refusal, because the number gets forwarded into a board
pack and the refusal gets a follow-up question.

**What changes if it is wrong.** If the finance lead's real need is open-ended
exploration rather than these six questions, this layer is too narrow and a
general text-to-SQL path is needed — with its own guardrails, a visible query, and
an accuracy figure measured on a held-out question set before anyone is invited to
rely on it. `.env.example` reserves the key for that path; it is off by default.

**Ask CloudNova.** Beyond these six, what do you actually ask in a normal month?
If the answer is "a dozen variations on these six", the fixed-intent design is
correct and the intent list just needs extending. If it is genuinely open-ended,
that is a different build and should be scoped as one.

---

## A10 — An account's current state is taken from its most recent invoice

**Assumed.** Where a gold table reports per account rather than per invoice —
`gold_mrr_by_region` (Q2), `gold_churn_by_plan` (Q4), `gold_top_accounts` (Q5) —
the account's plan, seats, discount, region, `churned` flag and CSAT are taken
from its **latest invoice by `invoice_date`**.

**Why.** These are account attributes carried on every one of that account's
invoice rows, and they change over time: accounts upgrade plans, add seats,
renegotiate discounts, and churn. The most recent invoice is the best available
statement of the subscription as it stands today. Averaging an attribute across
invoice rows instead would weight each account by how often it was billed — so a
monthly Starter customer would count twelve times as heavily as an annual
Enterprise one in a regional MRR average.

**What changes if it is wrong.** Q2 and Q4 both shift. An account that downgraded
is counted entirely at its new plan rather than its old one, so churn-by-plan
attributes that churn to the plan the account ended on. For a business with
material plan migration that is a meaningful difference in the Enterprise-versus-
Starter comparison the stakeholder asked for.

**This interacts with A1 and A2.** "Most recent" depends on `invoice_date`, so a
mis-parsed ambiguous date (A1) can pick the wrong invoice as representative, and
the de-duplication tie-break (A2) also uses `invoice_date`. The three assumptions
are not independent, and that is worth knowing before overturning any one of them
in isolation.

**Ask CloudNova.** Is there a subscription or entitlement table — separate from
the invoice ledger — that states each account's *current* plan and seat count?
That table would replace this assumption with a fact and would improve Q2 and Q4
more than any other single input.
