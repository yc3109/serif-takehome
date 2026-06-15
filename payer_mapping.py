"""
payer_mapping.py
────────────────
Curated payer name normalization table.

This file is the single source of truth for mapping raw payer name strings
(as they appear in HPT hospital files) to canonical payer identifiers
(as they appear in TiC MRF files).

Maintenance notes:
- Add new variants to PAYER_ALIAS as new hospital HPT files are ingested
- canonical_id should always match the payer slug used in TiC MRFs
- When a variant is ambiguous (e.g. could be two payers), flag it in
  AMBIGUOUS_VARIANTS and exclude from deterministic matching
- This file should be reviewed monthly as new HPT files introduce new variants
"""

# ── CANONICAL PAYER REGISTRY ───────────────────────────────────────────────
# Structure: canonical_id → metadata
# canonical_id matches the slug used in TiC payer MRFs

PAYER_REGISTRY = {
    "unitedhealthcare": {
        "display_name": "UnitedHealthcare",
        "parent_company": "UnitedHealth Group",
        "tic_slug": "unitedhealthcare",
    },
    "aetna": {
        "display_name": "Aetna",
        "parent_company": "CVS Health",
        "tic_slug": "aetna",
    },
    "cigna-corporation": {
        "display_name": "Cigna",
        "parent_company": "Cigna Group",
        "tic_slug": "cigna-corporation",
    },
}

# ── PAYER ALIAS TABLE ──────────────────────────────────────────────────────
# Structure: raw_name_lowercase → canonical_id
# Add new variants here as new HPT files are ingested
# Keep lowercase and stripped — normalization applied before lookup

PAYER_ALIAS = {
    # UnitedHealthcare variants
    "united":                   "unitedhealthcare",
    "uhc":                      "unitedhealthcare",
    "united healthcare":        "unitedhealthcare",
    "unitedhealthcare":         "unitedhealthcare",
    "united health":            "unitedhealthcare",
    "uhc choice plus":          "unitedhealthcare",

    # Aetna variants
    "aetna":                    "aetna",
    "aetna commercial":         "aetna",
    "aetna medicare":           "aetna",
    "aetna ppo":                "aetna",

    # Cigna variants
    "cigna":                    "cigna-corporation",
    "cigna-corporation":        "cigna-corporation",
    "cigna corporation":        "cigna-corporation",
    "cigna healthcare":         "cigna-corporation",
    "cigna localplus":          "cigna-corporation",
}

# ── AMBIGUOUS VARIANTS ─────────────────────────────────────────────────────
# Names that could map to multiple payers — exclude from deterministic matching
# Flag for human review queue

AMBIGUOUS_VARIANTS = {
    "bcbs":         "could be any BCBS affiliate — needs state context",
    "empire":       "could be Empire BCBS or Empire HealthPlus",
    "oxford":       "could be Oxford Health (UHC subsidiary) or independent",
    "multiplan":    "network rental — not a payer, rates not directly comparable",
    "medicare":     "government program — not a commercial payer",
    "medicaid":     "government program — not a commercial payer",
}

# ── NORMALIZATION FUNCTION ─────────────────────────────────────────────────

def normalize_payer(raw_name):
    """
    Map a raw HPT payer name string to its canonical TiC identifier.

    Returns:
        canonical_id (str) if matched
        None if not in big-3 payers
        "ambiguous" if in AMBIGUOUS_VARIANTS

    Usage:
        from payer_mapping import normalize_payer
        canonical = normalize_payer("United Healthcare")  # → "unitedhealthcare"
    """
    if not raw_name or raw_name != raw_name:  # handle None/NaN
        return None

    cleaned = str(raw_name).lower().strip()

    # Check ambiguous first
    if cleaned in AMBIGUOUS_VARIANTS:
        return "ambiguous"

    # Direct lookup
    if cleaned in PAYER_ALIAS:
        return PAYER_ALIAS[cleaned]

    # Substring fallback — less reliable, use with caution
    for alias, canonical in PAYER_ALIAS.items():
        if alias in cleaned:
            return canonical

    return None  # not recognized


def get_payer_display_name(canonical_id):
    """
    Return human-readable display name for a canonical payer id.
    Used for output formatting.
    """
    if canonical_id in PAYER_REGISTRY:
        return PAYER_REGISTRY[canonical_id]["display_name"]
    return canonical_id


# ── QUICK TEST ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_cases = [
        ("United", "unitedhealthcare"),
        ("UHC", "unitedhealthcare"),
        ("aetna", "aetna"),
        ("Aetna", "aetna"),
        ("Cigna", "cigna-corporation"),
        ("BCBS", "ambiguous"),
        ("HealthFirst", None),
        ("Medicare", "ambiguous"),
    ]

    print("Running payer mapping tests...\n")
    all_passed = True
    for raw, expected in test_cases:
        result = normalize_payer(raw)
        status = "✓" if result == expected else "✗"
        if result != expected:
            all_passed = False
        print(f"  {status} normalize_payer('{raw}') → {result} (expected: {expected})")

    print(f"\n{'All tests passed!' if all_passed else 'Some tests failed — review mapping'}")