"""
Serif Health Take-Home: HPT × TiC Rate Matching
Author: Yibin Chen
06/08/2026
------------------------------------------------
Objective: Unify hospital price transparency (HPT) and payer Transparency in Coverage (TiC)
datasets into a single schema, identify matching records, score confidence, and surface
rate agreement / discrepancy with domain-grounded explanations.
-----------------------------------------------------
Design summary (see README.md for full reasoning):

  1. Load both extracts.
  2. Resolve hospital identity (HPT license_number -> EIN) so both
     datasets share a join key.
  3. Normalize payer names (HPT) to TiC's canonical payer slugs.
  4. Normalize billing codes to a common string format.
  5. Derive billing_class for HPT (TiC already has it).
  6. Categorize EVERY HPT row up front: rows that can never be compared
     to TiC (non-big-3 payer, LOCAL codes, gov't plans, missing rate)
     are routed straight to the final output as `hpt_only` with their
     exclusion reason as match_category. They never enter matching.
  7. BLOCK_KEY = (ein, payer_canonical, code_clean, code_type,
     billing_class). This is the join key for matching -- including
     billing_class means institutional and professional rates for the
     same code are never cross-compared.
  8. Deduplicate both sides within BLOCK_KEY + rate, collapsing rows
     that differ only in plan_name / network_name / methodology / etc.
     into "associated metadata" lists.
  9. Full outer join on BLOCK_KEY (group level, not rate level) splits
     everything into: both / hpt_only / tic_only.
 10. Within each "both" group, greedily pair HPT rates to TiC rates by
     price proximity (1:1, best score first). Paired rows -> "matched"
     with a confidence band from the rate delta. Unpaired leftovers
     (granularity mismatches -- e.g. one HPT rate vs. five TiC plan
     variants) -> "additional_rate_variant".
 11. Concatenate everything into one unified schema and write to CSV.

Sanity checks (printed at each stage) verify no rows are silently
dropped or duplicated during dedup / matching.
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")
from payer_mapping import normalize_payer

pd.set_option("display.width", 160)


# ============================================================================
# STEP 0 -- LOAD
# ============================================================================
hpt = pd.read_csv("hpt_extract_20250213.csv")
tic = pd.read_csv("tic_extract_20250213.csv")

HPT_RAW_COUNT = len(hpt)
TIC_RAW_COUNT = len(tic)

print(f"[Step 0] Loaded HPT: {HPT_RAW_COUNT} rows | TiC: {TIC_RAW_COUNT} rows")


# ============================================================================
# STEP 1 -- ENTITY RESOLUTION: hospital identifier (license_number -> EIN)
# ============================================================================
# TiC uses EIN (federal tax ID). HPT uses a state license number. Mapped via
# curated lookup (Montefiore's license IS already an EIN; Mount Sinai and
# NYU Langone resolved via public record).
LICENSE_TO_EIN = {
    "13-1740114": "131740114",   # Montefiore
    "330024":     "131624096",   # Mount Sinai
    "7002053H":   "133971298",   # NYU Langone
}

hpt["ein"] = hpt["license_number"].map(LICENSE_TO_EIN)
tic["ein"] = tic["ein"].astype(str).str.strip()

n_unmapped = hpt["ein"].isna().sum()
print(f"[Step 1] HPT rows with unmapped EIN: {n_unmapped} (expect 0)")
assert n_unmapped == 0, "Unmapped hospital EIN -- update LICENSE_TO_EIN"


# ============================================================================
# STEP 2 -- PAYER NAME NORMALIZATION (HPT only; TiC payer slugs are canonical)
# ============================================================================
BIG3 = ["aetna", "unitedhealthcare", "cigna-corporation"]

_normalized = hpt["payer_name"].apply(normalize_payer)

# Keep the canonical id only when it's an actual big-3 match. For everything
# else (None / "ambiguous"), fall back to the original payer_name string so
# that excluded rows still carry useful, human-readable payer info instead
# of collapsing to None/"ambiguous".
hpt["payer_canonical"] = np.where(_normalized.isin(BIG3), _normalized, hpt["payer_name"])

tic = tic.rename(columns={"payer": "payer_canonical"})

print("[Step 2] HPT payer_canonical -- big-3 vs. other (raw name preserved):")
print(_normalized.isin(BIG3).value_counts().rename({True: "big-3", False: "other (raw payer_name kept)"}))


# ============================================================================
# STEP 3 -- BILLING CODE NORMALIZATION
# ============================================================================
# HPT: strip "MS-DRG " prefix (some rows already have it stripped, some don't)
# TiC: cast numeric code to string
hpt["code_clean"] = hpt["raw_code"].str.replace("MS-DRG ", "", regex=False).str.strip()
tic["code_clean"] = tic["code"].astype(str).str.strip()

print("[Step 3] HPT code_clean values:", sorted(hpt["code_clean"].unique()))
print("[Step 3] TiC code_clean values:", sorted(tic["code_clean"].unique()))


# ============================================================================
# STEP 4 -- BILLING CLASS (HPT only; TiC already has billing_class)
# ============================================================================
# Rule 1: MS-DRG codes are always institutional (DRGs are facility case rates)
# Rule 2: description starting with "PR " indicates a professional component
# Rule 3: default to institutional
def infer_billing_class(row):
    if row["code_type"] == "MS-DRG":
        return "institutional"
    if str(row["description"]).startswith("PR "):
        return "professional"
    return "institutional"

hpt["billing_class"] = hpt.apply(infer_billing_class, axis=1)

print("[Step 4] HPT billing_class distribution:")
print(hpt["billing_class"].value_counts())
print("[Step 4] TiC billing_class distribution:")
print(tic["billing_class"].value_counts())


# ============================================================================
# STEP 5 -- MATCH ELIGIBILITY: categorize every HPT row up front
# ============================================================================
# Ineligible rows are NOT dropped -- they go straight to the final output
# as data_source="hpt_only" with their exclusion reason as match_category,
# and never enter the matching pipeline below.
MEDICARE_MEDICAID_PLANS = [
    "Medicare", "Medicare Advantage",
    "Medicare Advantage, Community Plan Medicare Advantage",
]

def categorize_eligibility(row):
    if row["payer_canonical"] not in BIG3:
        return "excluded_non_big3_payer"
    if row["code_type"] == "LOCAL":
        return "excluded_local_code_inconsistent"
    if row["plan_name"] in MEDICARE_MEDICAID_PLANS:
        return "excluded_non_commercial_plan"
    if pd.isna(row["standard_charge_negotiated_dollar"]):
        return "excluded_missing_negotiated_rate"
    return "eligible"

hpt["match_eligibility"] = hpt.apply(categorize_eligibility, axis=1)

print("[Step 5] HPT match_eligibility distribution:")
print(hpt["match_eligibility"].value_counts())

excluded_mask = hpt["match_eligibility"] != "eligible"
hpt_excluded = hpt[excluded_mask].copy()
hpt_eligible = hpt[~excluded_mask].copy()

print(f"[Step 5] Excluded: {len(hpt_excluded)} | Eligible: {len(hpt_eligible)} "
      f"| Sum: {len(hpt_excluded) + len(hpt_eligible)} (expect {HPT_RAW_COUNT})")
assert len(hpt_excluded) + len(hpt_eligible) == HPT_RAW_COUNT


# ============================================================================
# STEP 6 -- DEDUPLICATE WITHIN BLOCK_KEY + RATE
# ============================================================================
# BLOCK_KEY is the join/blocking key used for ALL matching below.
# Including billing_class here is the key design decision: institutional
# and professional rates for the same code are structurally different
# (facility vs. professional component) and must never be cross-compared.
BLOCK_COLS = ["ein", "payer_canonical", "code_clean", "code_type", "billing_class"]

hpt_dedup = hpt_eligible.groupby(BLOCK_COLS + ["standard_charge_negotiated_dollar"], as_index=False).agg(
    hospital_name=("hospital_name", "first"),
    hpt_plan_names=("plan_name", lambda x: sorted(set(x.dropna()))),
    hpt_descriptions=("description", lambda x: sorted(set(x.dropna()))),
    hpt_settings=("setting", lambda x: sorted(set(x.dropna()))),
    hpt_methodologies=("standard_charge_methodology", lambda x: sorted(set(x.dropna()))),
    hpt_gross_charges=("standard_charge_gross", lambda x: sorted(set(x.dropna()))),
    n_hpt_source_rows=("plan_name", "size"),
)
hpt_dedup = hpt_dedup.rename(columns={"standard_charge_negotiated_dollar": "hpt_rate"})

tic_dedup = tic.groupby(BLOCK_COLS + ["rate"], as_index=False).agg(
    tic_network_names=("network_name", lambda x: sorted(set(x.dropna()))),
    tic_npi_lists=("taxonomy_filtered_npi_list", lambda x: sorted(set(x.dropna().astype(str)))),
    tic_modifiers=("modifier_list", lambda x: sorted(set(x.dropna().astype(str)))),
    tic_pos=("place_of_service_list", lambda x: sorted(set(x.dropna().astype(str)))),
    tic_negotiation_types=("negotiation_type", lambda x: sorted(set(x.dropna()))),
    tic_arrangements=("arrangement", lambda x: sorted(set(x.dropna()))),
    cms_baseline_rate=("cms_baseline_rate", "first"),
    cms_baseline_schedule=("cms_baseline_schedule", "first"),
    n_tic_source_rows=("rate", "size"),
)
tic_dedup = tic_dedup.rename(columns={"rate": "tic_rate"})

print(f"[Step 6] HPT eligible rows {len(hpt_eligible)} -> {len(hpt_dedup)} unique "
      f"(block_key, rate) records")
print(f"[Step 6] TiC raw rows {TIC_RAW_COUNT} -> {len(tic_dedup)} unique "
      f"(block_key, rate) records")

# Sanity: no source row lost or duplicated during dedup
n_hpt_check = hpt_dedup["n_hpt_source_rows"].sum()
n_tic_check = tic_dedup["n_tic_source_rows"].sum()
print(f"[Step 6] Sanity -- sum(n_hpt_source_rows) = {n_hpt_check} "
      f"(expect {len(hpt_eligible)})")
print(f"[Step 6] Sanity -- sum(n_tic_source_rows) = {n_tic_check} "
      f"(expect {TIC_RAW_COUNT})")
assert n_hpt_check == len(hpt_eligible)
assert n_tic_check == TIC_RAW_COUNT

# Hospital name lookup for TiC-side rows (TiC has no hospital_name column)
EIN_TO_HOSPITAL = hpt.groupby("ein")["hospital_name"].first().to_dict()

# ============================================================================
# STEP 6b -- EXPLORATORY VISUALIZATION: rate distributions by code/hospital
# ============================================================================
# Pools institutional + professional together (this is a "do these two
# files even cover the same ballpark" view, not a matching diagnostic).
# Uses the deduped, eligible HPT/TiC tables from Step 6.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SHORT_HOSPITAL_NAMES = {
    "131740114": "Montefiore",
    "131624096": "Mount Sinai",
    "133971298": "NYU Langone",
}

def plot_rate_distributions(hpt_dedup, tic_dedup, output_path="rate_distribution.png"):
    codes = sorted(set(hpt_dedup["code_clean"]) | set(tic_dedup["code_clean"]))
    eins = sorted(SHORT_HOSPITAL_NAMES)

    fig, axes = plt.subplots(1, len(codes), figsize=(6 * len(codes), 5))
    if len(codes) == 1:
        axes = [axes]

    for ax, code in zip(axes, codes):
        data, labels = [], []
        for ein in eins:
            hosp = SHORT_HOSPITAL_NAMES[ein]
            hpt_rates = hpt_dedup.loc[
                (hpt_dedup["code_clean"] == code) & (hpt_dedup["ein"] == ein), "hpt_rate"
            ].dropna().tolist()
            tic_rates = tic_dedup.loc[
                (tic_dedup["code_clean"] == code) & (tic_dedup["ein"] == ein), "tic_rate"
            ].dropna().tolist()
            data.append(hpt_rates or [float("nan")])
            labels.append(f"{hosp}\nHPT")
            data.append(tic_rates or [float("nan")])
            labels.append(f"{hosp}\nTiC")

        ax.boxplot(data, tick_labels=labels)
        ax.set_title(f"Code {code} -- HPT vs TiC (deduped, eligible)")
        ax.set_ylabel("Rate ($)")
        ax.tick_params(axis="x", rotation=45)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"[Step 6b] Saved rate distribution chart to {output_path}")


plot_rate_distributions(hpt_dedup, tic_dedup, "rate_distribution.png")

# ============================================================================
# STEP 7 -- GROUP-LEVEL OUTER JOIN ON BLOCK_KEY
# ============================================================================
# Determine which BLOCK_KEY combinations exist on each side. This is a
# *group-level* join (ignores rate) -- it tells us whether the
# ein/payer/code/code_type/billing_class combination exists at all on
# the other side, separate from whether any individual rate matches.
hpt_dedup["_block"] = list(zip(*[hpt_dedup[c] for c in BLOCK_COLS]))
tic_dedup["_block"] = list(zip(*[tic_dedup[c] for c in BLOCK_COLS]))

hpt_groups = set(hpt_dedup["_block"].unique())
tic_groups = set(tic_dedup["_block"].unique())

both_groups = hpt_groups & tic_groups
hpt_only_groups = hpt_groups - tic_groups
tic_only_groups = tic_groups - hpt_groups

print(f"[Step 7] BLOCK_KEY groups -- HPT: {len(hpt_groups)} | TiC: {len(tic_groups)}")
print(f"[Step 7] Groups in both: {len(both_groups)} | "
      f"HPT-only groups: {len(hpt_only_groups)} | "
      f"TiC-only groups: {len(tic_only_groups)}")

# Sanity: every group belongs to exactly one of the three buckets
assert hpt_groups == both_groups | hpt_only_groups
assert tic_groups == both_groups | tic_only_groups


# ============================================================================
# STEP 8 -- TERMINAL ROWS: groups that exist on only one side
# ============================================================================
# These never enter price matching -- the ein/payer/code/billing_class
# combination simply isn't present on the other side at all (e.g. the
# classic "DRG rate filed by the hospital but absent from the payer's
# TiC file" case).
hpt_only_rows = hpt_dedup[hpt_dedup["_block"].isin(hpt_only_groups)].copy()
hpt_only_rows["match_category"] = "no_tic_for_key"
hpt_only_rows["data_source"] = "hpt_only"

tic_only_rows = tic_dedup[tic_dedup["_block"].isin(tic_only_groups)].copy()
tic_only_rows["match_category"] = "no_hpt_for_key"
tic_only_rows["data_source"] = "tic_only"
tic_only_rows["hospital_name"] = tic_only_rows["ein"].map(EIN_TO_HOSPITAL)

print(f"[Step 8] HPT rate records with no TiC counterpart group: {len(hpt_only_rows)}")
print(f"[Step 8] TiC rate records with no HPT counterpart group: {len(tic_only_rows)}")


# ============================================================================
# STEP 9 -- PRICE MATCHING WITHIN "BOTH" GROUPS
# ============================================================================
# For each BLOCK_KEY group present on both sides: greedily pair HPT rates
# to TiC rates by price proximity (highest-scoring pair claims first,
# each rate used at most once). Paired rows -> "matched" with a confidence
# band from the rate delta. Any leftover rates (granularity mismatch,
# e.g. 1 HPT rate vs. 5 TiC plan-specific rates) -> "additional_rate_variant".

def price_score(h, t):
    return 1 - abs(h - t) / max(h, t)

def confidence_band(abs_delta_pct):
    if abs_delta_pct < 0.5:
        return "exact"
    if abs_delta_pct < 5:
        return "high"
    if abs_delta_pct < 20:
        return "medium"
    return "low"

matched_rows = []
additional_rows = []

hpt_both = hpt_dedup[hpt_dedup["_block"].isin(both_groups)]
tic_both = tic_dedup[tic_dedup["_block"].isin(both_groups)]

for block_key, hpt_grp in hpt_both.groupby("_block"):
    tic_grp = tic_both[tic_both["_block"] == block_key]

    h_records = hpt_grp.to_dict("records")
    t_records = tic_grp.to_dict("records")

    # All candidate pairs, scored by price proximity, best first
    pairs = []
    for hi, h in enumerate(h_records):
        for ti, t in enumerate(t_records):
            pairs.append((price_score(h["hpt_rate"], t["tic_rate"]), hi, ti))
    pairs.sort(key=lambda x: x[0], reverse=True)

    used_h, used_t = set(), set()
    for score, hi, ti in pairs:
        if hi in used_h or ti in used_t:
            continue
        used_h.add(hi)
        used_t.add(ti)

        h, t = h_records[hi], t_records[ti]
        delta_pct = round((t["tic_rate"] - h["hpt_rate"]) / h["hpt_rate"] * 100, 2)
        abs_delta = abs(delta_pct)

        row = {**h, **t}
        row["rate_delta_pct"] = delta_pct
        row["abs_delta_pct"] = abs_delta
        row["match_confidence"] = confidence_band(abs_delta)
        row["match_category"] = "matched"
        row["data_source"] = "both"
        matched_rows.append(row)

    # Leftover HPT rates: key matched, but no TiC rate left to pair with
    for hi, h in enumerate(h_records):
        if hi not in used_h:
            row = dict(h)
            row["match_category"] = "additional_rate_variant"
            row["data_source"] = "hpt_only"
            additional_rows.append(row)

    # Leftover TiC rates: key matched, but no HPT rate left to pair with
    for ti, t in enumerate(t_records):
        if ti not in used_t:
            row = dict(t)
            row["hospital_name"] = EIN_TO_HOSPITAL.get(row["ein"])
            row["match_category"] = "additional_rate_variant"
            row["data_source"] = "tic_only"
            additional_rows.append(row)

matched_df = pd.DataFrame(matched_rows)
additional_df = pd.DataFrame(additional_rows)

n_additional_hpt = (additional_df["data_source"] == "hpt_only").sum() if len(additional_df) else 0
n_additional_tic = (additional_df["data_source"] == "tic_only").sum() if len(additional_df) else 0

print(f"[Step 9] 'Both' groups processed: {len(both_groups)}")
print(f"[Step 9] Matched pairs: {len(matched_df)}")
print(f"[Step 9]   confidence band breakdown:")
print(matched_df["match_confidence"].value_counts())
print(f"[Step 9] Additional rate variants -- HPT side: {n_additional_hpt} | "
      f"TiC side: {n_additional_tic}")

# Sanity -- every HPT/TiC dedup row in a "both" group ends up either
# matched or as an additional_rate_variant, with no loss/duplication
print(f"[Step 9] Sanity -- HPT 'both' rows: {len(hpt_both)} = "
      f"matched ({len(matched_df)}) + additional_hpt ({n_additional_hpt}) "
      f"= {len(matched_df) + n_additional_hpt}")
print(f"[Step 9] Sanity -- TiC 'both' rows: {len(tic_both)} = "
      f"matched ({len(matched_df)}) + additional_tic ({n_additional_tic}) "
      f"= {len(matched_df) + n_additional_tic}")
assert len(hpt_both) == len(matched_df) + n_additional_hpt
assert len(tic_both) == len(matched_df) + n_additional_tic


# ============================================================================
# STEP 10 -- ASSEMBLE EXCLUDED-ROW OUTPUT (from Step 5)
# ============================================================================
hpt_excluded_out = hpt_excluded.copy()
hpt_excluded_out["hpt_rate"] = hpt_excluded_out["standard_charge_negotiated_dollar"]
hpt_excluded_out["hpt_plan_names"] = hpt_excluded_out["plan_name"].apply(lambda x: [x] if pd.notna(x) else [])
hpt_excluded_out["hpt_descriptions"] = hpt_excluded_out["description"].apply(lambda x: [x] if pd.notna(x) else [])
hpt_excluded_out["hpt_settings"] = hpt_excluded_out["setting"].apply(lambda x: [x] if pd.notna(x) else [])
hpt_excluded_out["hpt_methodologies"] = hpt_excluded_out["standard_charge_methodology"].apply(lambda x: [x] if pd.notna(x) else [])
hpt_excluded_out["hpt_gross_charges"] = hpt_excluded_out["standard_charge_gross"].apply(lambda x: [x] if pd.notna(x) else [])
hpt_excluded_out["n_hpt_source_rows"] = 1
hpt_excluded_out["match_category"] = hpt_excluded_out["match_eligibility"]
hpt_excluded_out["data_source"] = "hpt_only"

print(f"[Step 10] Excluded HPT rows carried to output as-is: {len(hpt_excluded_out)}")


# ============================================================================
# STEP 11 -- FINAL ASSEMBLY INTO UNIFIED SCHEMA
# ============================================================================
FINAL_COLS = [
    "data_source", "match_category", "match_confidence",
    "ein", "hospital_name", "payer_canonical", "code_clean", "code_type", "billing_class",
    "hpt_rate", "tic_rate", "rate_delta_pct", "abs_delta_pct",
    "n_hpt_source_rows", "n_tic_source_rows",
    "hpt_plan_names", "hpt_descriptions", "hpt_settings", "hpt_methodologies", "hpt_gross_charges",
    "tic_network_names", "tic_npi_lists", "tic_modifiers", "tic_pos",
    "tic_negotiation_types", "tic_arrangements",
    "cms_baseline_rate", "cms_baseline_schedule",
]

pieces = [hpt_excluded_out, hpt_only_rows, tic_only_rows, matched_df, additional_df]
pieces = [p.reindex(columns=FINAL_COLS) for p in pieces if len(p) > 0]

final = pd.concat(pieces, ignore_index=True)

print(f"[Step 11] Final unified dataset: {len(final)} rows, {len(final.columns)} columns")
print("[Step 11] match_category distribution:")
print(final["match_category"].value_counts())
print("[Step 11] data_source distribution:")
print(final["data_source"].value_counts())

# ----------------------------------------------------------------------
# END-TO-END SANITY CHECK -- no HPT or TiC source row is lost or duplicated
# ----------------------------------------------------------------------
hpt_accounted = (
    hpt_excluded_out["n_hpt_source_rows"].sum()
    + hpt_only_rows["n_hpt_source_rows"].sum()
    + matched_df["n_hpt_source_rows"].sum()
    + (additional_df.loc[additional_df["data_source"] == "hpt_only", "n_hpt_source_rows"].sum()
       if len(additional_df) else 0)
)
tic_accounted = (
    tic_only_rows["n_tic_source_rows"].sum()
    + matched_df["n_tic_source_rows"].sum()
    + (additional_df.loc[additional_df["data_source"] == "tic_only", "n_tic_source_rows"].sum()
       if len(additional_df) else 0)
)

print(f"\n[FINAL SANITY] HPT source rows accounted: {hpt_accounted} (expect {HPT_RAW_COUNT})")
print(f"[FINAL SANITY] TiC source rows accounted: {tic_accounted} (expect {TIC_RAW_COUNT})")
assert hpt_accounted == HPT_RAW_COUNT
assert tic_accounted == TIC_RAW_COUNT

# No duplicate (block_key, rate) records within the DEDUPED portion of the
# output (Step 6 dedup -> matched / no_tic_for_key / no_hpt_for_key /
# additional_rate_variant). The excluded_* rows (Step 5) are intentionally
# passed through one-per-source-row and are excluded from this check.
deduped_mask = ~final["match_category"].str.startswith("excluded_")

hpt_side = final[deduped_mask & final["hpt_rate"].notna()]
hpt_dupe_check = hpt_side.duplicated(subset=BLOCK_COLS + ["hpt_rate"]).sum()

tic_side = final[deduped_mask & final["tic_rate"].notna()]
tic_dupe_check = tic_side.duplicated(subset=BLOCK_COLS + ["tic_rate"]).sum()

print(f"[FINAL SANITY] Duplicate (block_key, hpt_rate) rows in deduped output: {hpt_dupe_check} (expect 0)")
print(f"[FINAL SANITY] Duplicate (block_key, tic_rate) rows in deduped output: {tic_dupe_check} (expect 0)")
assert hpt_dupe_check == 0
assert tic_dupe_check == 0

print("\nAll sanity checks passed.")


# ============================================================================
# STEP 12 -- WRITE OUTPUT
# ============================================================================
OUTPUT_PATH = "unified_rate_dataset.csv"
final.to_csv(OUTPUT_PATH, index=False)
print(f"\n[Step 12] Wrote {len(final)} rows to {OUTPUT_PATH}")
