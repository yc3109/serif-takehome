# Serif Health Take-Home — HPT × TiC Rate Matching

## Overview

This project combines a Hospital Price Transparency (HPT) extract from three
NYC hospitals (Montefiore, Mount Sinai, NYU Langone) with a Transparency in
Coverage (TiC) extract from three national payers (Aetna, Cigna,
UnitedHealthcare) into one unified dataset. The goal isn't just to join two
tables on shared keys — it's to figure out which records on each side are
actually describing the same negotiated rate, how confident we are in that,
and what to do with the records that don't have an obvious counterpart.

The short version of the approach: clean and normalize both files using
healthcare billing knowledge, exclude the records that structurally can't be
compared (different payers, non-commercial plans, data errors), deduplicate
heavily-repeated rate rows while keeping the detail that got collapsed, then
run a block-level join followed by a greedy price-proximity match within each
block. Everything that doesn't match — on either side — is kept in the output
and labeled with why.

Two files matter most for review:

- `Exploratory_Analysis.ipynb` — the full walkthrough, with every decision
  explained inline as it was made
- `solution.py` — the same logic as a runnable script, plus `payer_mapping.py`
  for the payer name normalization table

## How to Run

```bash
pip install pandas numpy matplotlib
python solution.py
```

Expects `hpt_extract_20250213.csv` and `tic_extract_20250213.csv` in the same
directory. `payer_mapping.py` must be importable (same folder). Output is
`unified_rate_dataset.csv`.

---

## Cleaning the Data — What Gets Excluded and Why

Before any join happens, every HPT row gets tagged with a `match_eligibility`
category. Four exclusion rules came directly out of looking at the data with
a claims-background lens rather than treating it as generic tabular data:

**Payer isn't one of the big three.** HPT lists 73 distinct payer name
variants; TiC only covers Aetna, Cigna, and UnitedHealthcare. Everything else
(HealthFirst, Oxford, Medicaid managed care plans, etc.) simply has nothing to
compare against and is excluded as `excluded_non_big3_payer`. This accounts
for the bulk of the 2,855-row final output (2,577 rows) — not because
something went wrong, but because HPT's payer universe is just much bigger
than TiC's in this extract.

**LOCAL code type at NYU Langone.** 410 rows are tagged `code_type = LOCAL`
with `raw_code = 43239`. CPT 43239 is an upper GI endoscopy with biopsy. But
the descriptions on these LOCAL rows read things like "HEAD HUM 19MM 50MM
SHLDR UNIVERS STRL LF CUF ARTHROPATHY" — a shoulder implant component. The
code and the description describe two completely different procedures. This
isn't an edge case to work around; it's a data encoding error, and these rows
are excluded entirely (`excluded_local_code_inconsistent`) rather than forced
into a CPT bucket they don't belong in.

**Non-commercial plans.** The TiC extract is explicitly drawn from "national
PPO files" — commercial plans only. HPT, on the other hand, reports
everything including Medicare and Medicare Advantage. 22 rows with plan names
like "Medicare" or "Medicare Advantage, Community Plan Medicare Advantage" are
excluded as `excluded_non_commercial_plan`. This is a scope mismatch, not a
data quality problem — there was never going to be a TiC counterpart for these.

**Missing negotiated rate.** 17 rows have no usable
`standard_charge_negotiated_dollar` value. I checked whether
`gross_charge × negotiated_percentage` could recover these — it can't, either
the gross charge is also missing or the methodology isn't percentage-based.
With only 17 rows and no viable imputation path, these are excluded
(`excluded_missing_negotiated_rate`) rather than guessed at.

## Identifier and Code Normalization

**Hospital identity.** TiC identifies hospitals by EIN; HPT uses
`license_number`, which is a different identifier system entirely. Montefiore's
license (`13-1740114`) happens to strip-format into its EIN (`131740114`)
directly. Mount Sinai and NYU Langone don't — their HPT license numbers are
state license formats, not EINs, so I resolved their actual federal EINs from
public records (`13-1624096` and `13-3971298` respectively) and built a small
lookup table. At national scale this wouldn't be a one-off lookup — it'd be a
maintained crosswalk table (NPI → EIN → hospital name, sourced from NPPES),
with fuzzy name + geography matching as a fallback for records that don't
resolve cleanly.

**Payer names.** TiC uses clean lowercase slugs (`aetna`,
`unitedhealthcare`, `cigna-corporation`). HPT has the same three payers under
dozens of spellings — "Aetna", "aetna", "United", "UHC", "United Healthcare".
A normalization function lives in its own `payer_mapping.py` module with a
small test suite, specifically so it can be extended as new HPT files
introduce new variants without touching the matching logic itself.

**Billing codes.** HPT writes the DRG code as either `872` or `MS-DRG 872`
depending on the hospital; both get stripped down to a plain `872` so the two
files use the same code format.

## Billing Class — Why It Had to Become a Hard Join Key

TiC splits every rate by `billing_class` — institutional (facility) vs.
professional (physician). HPT doesn't have this column at all, so it has to be
inferred: MS-DRG codes are always institutional (they're inpatient episode
bundles), descriptions prefixed with "PR " are professional (a consistent
hospital convention for physician-billed services), and everything else
defaults to institutional.

I initially treated billing_class as a secondary signal rather than a join
key, but the actual distributions argued strongly against that. Among
eligible rows, HPT is 93% institutional (266 vs. 21 professional) while TiC is
80% professional (177 vs. 45 institutional) — almost mirror images of each
other. And the rate magnitudes confirm these aren't interchangeable: for CPT
43239, institutional rates average roughly $6,300-7,400 on both sides, while
professional rates average $424-1,500 — a 5-15x gap, consistent across all
three payers. An HPT institutional rate landing anywhere near a TiC
professional rate for the same code would be coincidence, not a real match.
So `billing_class` joined `ein`, `payer_canonical`, `code_clean`, and
`code_type` as a hard join key — without it, every institutional HPT rate
would get compared against a pile of professional TiC rates it has no
business being compared to.

Two other fields came up as candidates for join keys and were deliberately
**not** used that way:

`plan_name` on the HPT side is extremely fragmented — things like
"aetnaopenaccesschoice(epo)1415" or "unitedhealthcarenavigate1404" with no
obvious mapping to TiC's network names ("open-access-managed-choice",
"choice-plus"). Forcing a plan-name match would throw out legitimate matches
just because the same contract is labeled differently by each side. Instead
`plan_name` is carried through as context — useful for a human reviewing a
match, not for deciding whether one exists.

TiC's `taxonomy_filtered_npi_list` tells you how many providers a given rate
applies to. A rate tied to a broad NPI list looks more like a facility-wide
default (closer to what HPT reports); a rate tied to one or two NPIs looks
like a provider-specific carve-out. This is useful context for interpreting a
match, but on its own it wasn't reliable enough to use as a hard key — it's
carried through in the output instead.

## Deduplication — Collapsing Repetition Without Losing Detail

The first time I ran a full join on the cleaned data, the row count exploded
from a couple hundred rows per file into the thousands. The cause is
structural on both sides: HPT repeats the same negotiated rate once per
`plan_name` (the same dollar figure shows up under "All Payers", "HC Compass",
"Choice Plus", etc. as separate rows), and TiC repeats the same rate once per
NPI/network grouping. Join two files that each have this many-to-one
repetition and every repetition on one side pairs with every repetition on the
other — the output becomes an unreadable pile of duplicate comparisons.

The fix was to deduplicate both files on (join keys + rate) *before* joining,
collapsing repeated rows into one — but rather than throwing away the
`plan_name` or NPI-list variation that got collapsed, those values get
gathered into list columns (`associated_plans`, `associated_npi_lists`, etc.)
attached to the single remaining row. So the join operates on genuinely
distinct rate records, but nothing about *why* a rate appears multiple times
is lost — it's just moved from "many rows" to "one row with a list."

This took HPT from 287 eligible rows down to 67 unique rate records, and TiC
from 222 down to 167. Most of that reduction on the HPT side came from a
handful of rates that were repeated 30+ times across plan variants; on the
TiC side, most rates were already unique and only a few repeated 2-5 times.

## The Matching Approach

**Block-level check first.** Before comparing any actual dollar amounts, I
check which (ein, payer, code, code_type, billing_class) combinations — call
these "blocks" — exist on each side at all. HPT has 28 blocks, TiC has 35, and
21 overlap. The 7 HPT-only and 14 TiC-only blocks (18 HPT rows and 63 TiC rows)
can never produce a match no matter what the rates are, because the
combination itself doesn't exist on the other side. These get labeled
`no_tic_for_key` / `no_hpt_for_key` and kept in the output as-is.

**Greedy price-proximity matching within each block.** For the 21 blocks that
exist on both sides, every HPT rate is compared against every TiC rate in that
block, scored by `1 - |hpt_rate - tic_rate| / max(hpt_rate, tic_rate)`. Pairs
are sorted by score and claimed greedily — best match first, each rate used at
most once. A `confidence_band` is assigned from the resulting delta: `exact`
(<0.5%), `high` (<5%), `medium` (<20%), `low` (>=20%).

Anything left over after the best pairs are claimed — say one HPT rate facing
five TiC rates in the same block — is kept as `additional_rate_variant` rather
than discarded. These aren't unmatched noise; they're the other legitimate
rates that exist for the same hospital/payer/code/billing_class combination,
usually tied to different NPIs or plan tiers.

This produced 42 matched pairs (10 exact, 6 high, 7 medium, 19 low), plus 69
additional rate variants (62 TiC-side, 7 HPT-side).

One deliberate choice: there's no minimum confidence threshold for accepting a
match. Every HPT rate in a matched block gets paired with its closest TiC
counterpart, even if that counterpart is 70%+ away. A "low" confidence label
already communicates "this is the best available comparison, treat it with
caution" — an arbitrary cutoff that instead dumped that rate into an
unmatched bucket wouldn't make the information any more useful, just harder to
find. Given the 2-3 hour scope of this exercise, a single price-proximity
score with transparent banding felt like the right tradeoff between
correctness and the production-style mindset Serif described — get something
working end to end, make every decision visible, and leave clear notes on
what the next iteration would add.

## Walking Through the Brief's Specific Questions

**The UHC / CPT 43239 / $6,438 example.** This is one of the 10 `exact`
matches. Mount Sinai's HPT file lists a $6,438 case-rate under plan "All
Payer" for CPT 43239 with UnitedHealthcare. TiC's `choice-plus` extract for the
same EIN, payer, and code includes a $6,438 institutional rate — confirmed an
exact match (0% delta). The *other* rates that show up alongside it in TiC for
this same block (institutional rates around $1,500-5,200, plus a cluster of
much lower professional rates) are exactly the kind of thing
`additional_rate_variant` is for: they're real negotiated rates for narrower
NPI subsets within the same block, not duplicates and not noise. They can't all
be "the" match for HPT's single facility-level $6,438 line — but they're not
discarded either, since they describe legitimate variation in what UHC pays
different providers for the same code at the same hospital.

**The Montefiore / Aetna / DRG 872 example.** The brief cites $29,259.18 and
says you won't find it in the Aetna TiC extract — and that's consistent with
what shows up here, even though the exact figure in this sample's HPT file is
slightly different ($45,907.79 under the "Commercial" plan, $51,350.81 under
"ASA", $13,434.30 under "Medicare"). None of Montefiore's Aetna DRG 872 rates
have a TiC counterpart — this entire block falls into `no_tic_for_key`. Same
story at NYU Langone, which has *four* different Aetna DRG 872 rates ranging
from about $10K to $93K across different plan tiers, none of which have an
Aetna TiC match either. Aetna's national PPO MRF simply doesn't carry
facility-specific institutional DRG rates for these hospitals in this extract.

**Why DRG behaves differently from CPT.** A CPT code like 43239 prices a single
discrete service — the HPT and TiC numbers are describing the same unit of
work, so when they're close, they're plausibly the same contract. DRG 872 is
an entire inpatient episode bundle (the whole admission — nursing, labs, drugs,
everything except separately billed professional fees), and hospitals and
payers don't necessarily land on the same dollar figure for "the bundle" even
when they're both pricing the same underlying contract. Montefiore's own HPT
file shows three *different* Aetna DRG 872 rates depending on plan tier
(Commercial vs. ASA vs. Medicare) — a 3.8x spread for the same code at the same
hospital with the same payer. If the rate isn't even stable within one file,
it's not surprising that HPT's and TiC's versions of "the DRG 872 rate" don't
line up the way two CPT rates might.

**Plan type — join key or confidence signal?** Confidence signal, not a join
key, and the reasoning is covered above under billing class — `plan_name`'s
fragmentation on the HPT side means a hard match on plan type would eliminate
real matches over a labeling mismatch rather than a substantive one.

**When two records "look like" a match but the rates differ — what's going on,
and how should that affect downstream use?** A few legitimate explanations
showed up directly in this data: different NPI scopes within the same block
(the Mount Sinai/Aetna/43239 example — a "low" confidence $2,513.89 vs.
$1,766.00 match sits alongside an `additional_rate_variant` TiC rate of
$5,845.00 tied to a different, narrower set of NPIs — three different prices
for three different provider groupings under one nominal "rate"); different
plan tiers for the same DRG (Montefiore's Commercial/ASA/Medicare Aetna DRG 872
rates); and update-timing drift, since HPT and TiC files aren't published on
the same schedule (NYU's HPT file and the TiC extract are both January 2025 —
0-day gap — while Montefiore's HPT file is from July 2024, a ~184-day gap from
the TiC extract, leaving more room for routine contract escalators to explain
a moderate delta).

For downstream use, the implication is that a single matched row usually isn't
the whole story. The right framing for, say, an employer looking at
Mount-Sinai/Aetna/CPT-43239 isn't "the rate is $1,766" — it's "Aetna's
institutional rate for this code at this hospital ranges from about $1,766 to
$5,845 depending on which provider bills it, and HPT's reported $2,513.89 falls
within that range." The matched row plus its `additional_rate_variant` siblings
together convey that; either alone would understate the real picture.

## Result Summary

| Category | Rows |
|---|---|
| `excluded_non_big3_payer` | 2,577 |
| `additional_rate_variant` | 69 |
| `no_hpt_for_key` (TiC-only block) | 63 |
| `excluded_local_code_inconsistent` | 52 |
| `matched` | 42 |
| `excluded_non_commercial_plan` | 22 |
| `no_tic_for_key` (HPT-only block) | 18 |
| `excluded_missing_negotiated_rate` | 12 |
| **Total** | **2,855** |

The genuinely comparable population is small relative to the full dataset —
42 matches plus 69 variants plus 81 block-level mismatches, against a backdrop
of 2,577 rows that were never going to have a TiC counterpart because they
belong to payers outside the big three. That imbalance is a feature of this
particular extract (3 hospitals x 73 payers vs. 3 hospitals x 3 payers), not a
weakness in the matching approach.

A quick visual check (boxplots of deduplicated HPT vs. TiC rates by hospital
and code) lines up with the numeric results: CPT 43239 ranges broadly overlap
at Mount Sinai and Montefiore, where the `exact`/`high` matches landed; DRG 872
at Montefiore shows HPT rates ($45K-55K) sitting entirely above TiC's ($15K-40K)
with no overlap at all — visually confirming the `no_tic_for_key` finding for
that block; and Mount Sinai's HPT box for CPT 99283 is empty because its only
big-3-labeled rows for that code were Medicare Advantage plans, excluded
upstream as non-commercial.

## Scaling to the Full National Dataset

The approach here generalizes, but a few things would need to change going
from 3 hospitals / 3 payers to 4,000+ hospitals / 500+ payers and billions of
rows a month.

The hospital-identity crosswalk (license number -> EIN -> hospital) was a
3-row lookup table built by hand here. At scale this needs to be a maintained
reference table sourced from NPPES, refreshed regularly, with NPI as the
primary stable key (EINs can change when hospitals reorganize; NPIs are more
durable) and fuzzy name/geography matching as a fallback for the records that
don't resolve cleanly.

The payer alias table (`payer_mapping.py`) covers 3 payers here; at scale
it's hundreds of payers with constantly shifting name variants across
thousands of HPT files. This needs to be a versioned, reviewable asset — new
unrecognized payer strings get queued for human review rather than silently
falling through to "unmatched."

The block-and-match approach itself is the part that scales most naturally:
blocking on (EIN, payer, code, code_type, billing_class) keeps each
comparison local — you're never comparing a Texas hospital's rates against a
New York payer's MRF for an unrelated code. That makes the whole thing
trivially parallelizable by partitioning on (payer, code) or (state, code)
and running each partition independently on Spark/Databricks. MRFs are
published monthly, so a production pipeline wouldn't re-run the whole match
every cycle — it would diff incoming files against the prior month and only
re-process the (EIN, payer, code) combinations that actually changed.

For release, I'd want a fill-rate monitor per payer/code-type (what fraction
of HPT rows land in `matched` vs. `no_tic_for_key`, tracked over time and by
payer), since a sudden drop signals either a payer's MRF format changed or a
new batch of hospitals introduced a payer-name variant the alias table doesn't
recognize yet — exactly the kind of thing that would otherwise show up
silently as a wave of new `excluded_non_big3_payer` rows.

## What I'd Add With More Time

The current confidence score is price proximity alone. Two other signals were
clearly present in the data and would sharpen confidence without changing the
core architecture: comparing HPT's `standard_charge_methodology` (case rate,
fee schedule, % of billed charges) against TiC's `negotiation_type` — two rates
that are close in dollar terms but built on different rate structures are less
likely to represent the same contract than two that agree on both; and using
the date gap between each hospital's HPT `last_updated_on` and the TiC
extract's `network_year_month` (0 days for NYU, ~105 for Mount Sinai, ~184 for
Montefiore) as a soft penalty, since a larger gap leaves more room for routine
escalator-clause drift to explain an otherwise-moderate delta. Neither would
replace the price score — they'd nudge a "low" match with aligned methodology
and a tight date gap toward "medium," and a "medium" match with mismatched
methodology and a wide date gap toward "low."
