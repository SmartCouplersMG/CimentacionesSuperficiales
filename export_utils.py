# export_utils.py
# Shared helpers for SAP2000 and ETABS export
# ─────────────────────────────────────────────────────────────
"""
Canonical load-name mapping.
When the user confirms load classification (e.g. "PP" → D,
"Sx (FHE-Deriva)" → Sx, "Sx (RE-Deriva)" → Sx …), the export
renames every pattern/case to its canonical category name
(D, SD, L, Lr, Le, Wx+, Wx-, Wy+, Wy-, Sx, Sy).

Rules:
  • First pattern assigned to category "Sx" → exported as "Sx"
  • Second pattern assigned to "Sx" → exported as "Sx_2"
  • "Ignorar" patterns → excluded from export entirely
  • Patterns/cases that appear in jloads but are NOT in the
    user assignment are kept with their original name and type "Other".
"""

from parser import gen_combos


# ── Helper: apply rename map to a jloads dict ─────────────────
def apply_rename_to_jloads(jloads, rename_map):
    """
    Rename load-case keys in a jloads dict using rename_map.
    rename_map: {original_name: canonical_name | None}
    None → case is dropped (ignored).
    Unknown originals (not in rename_map) are kept as-is.
    Forces for two originals mapping to the same canonical name are summed.
    """
    out = {}
    for jid, per_joint in jloads.items():
        renamed: dict = {}
        for orig, fv in per_joint.items():
            exp = rename_map.get(orig, orig)   # unknown → keep
            if exp is None:                    # ignored
                continue
            if exp in renamed:
                for k in renamed[exp]:
                    renamed[exp][k] = renamed[exp].get(k, 0) + fv.get(k, 0)
            else:
                renamed[exp] = dict(fv)
        if renamed:
            out[str(jid)] = renamed
    return out


# ── Canonical category → design-type string ──────────────────
CAT_TO_DESTYPE = {
    "D":   "Dead",
    "SD":  "Super Dead",
    "L":   "Live",
    "Lr":  "Roof Live",
    "Le":  "Live",
    "Wx+": "Wind",
    "Wx-": "Wind",
    "Wy+": "Wind",
    "Wy-": "Wind",
    "Sx":  "Quake",
    "Sy":  "Quake",
}


def build_export_loads(model_data, params):
    """
    Build canonical load info for export.

    Returns
    -------
    export_lp : dict  {export_name: des_type_str}
        Load patterns/cases to define in the export file.
    rename_map : dict  {original_name: export_name | None}
        None means the pattern is ignored.
    renamed_jloads : dict  {joint_id: {export_name: {F1..M3}}}
    renamed_cl : dict  (same structure as model_data["_cl"])
        but referencing canonical export names.
    combos : dict  {"ADS": [...], "LRFD": [...]}
        Generated from renamed_cl.
    """
    user_assign = model_data.get("_lp_user_assignment") or {}
    jloads      = model_data.get("_jloads") or {}
    cl_orig     = model_data.get("_cl") or {}
    lp_orig     = model_data.get("_lp") or {}

    # ── Build rename map ──────────────────────────────────────
    cat_counter = {}    # cat → how many originals already mapped
    rename_map  = {}    # original → export_name  (None = ignored)
    export_lp   = {}    # export_name → des_type_str  (ordered)

    for orig, cat in user_assign.items():
        if cat == "Ignorar":
            rename_map[orig] = None
            continue
        cat_counter[cat] = cat_counter.get(cat, 0) + 1
        n = cat_counter[cat]
        exp_name = cat if n == 1 else f"{cat}_{n}"
        rename_map[orig] = exp_name
        des_type = CAT_TO_DESTYPE.get(cat, "Other")
        export_lp[exp_name] = des_type

    # ── Any jload case NOT in rename_map → keep as-is ────────
    all_jload_cases: set = set()
    for per_joint in jloads.values():
        all_jload_cases.update(per_joint.keys())

    for case in all_jload_cases:
        if case not in rename_map:
            rename_map[case] = case            # keep original
            if case not in export_lp:
                des_type = lp_orig.get(case, "Other")
                export_lp[case] = des_type

    # ── Fallback: if no user assignment, use lp_orig ─────────
    if not user_assign:
        export_lp = dict(lp_orig)
        rename_map = {k: k for k in lp_orig}

    # ── Rename jloads ─────────────────────────────────────────
    renamed_jloads: dict = {}
    for joint, per_joint in jloads.items():
        renamed: dict = {}
        for orig, forces in per_joint.items():
            exp = rename_map.get(orig)
            if exp is None:          # ignored
                continue
            if exp in renamed:
                # Duplicate export name — sum forces (rare edge case)
                for k in renamed[exp]:
                    renamed[exp][k] = renamed[exp].get(k, 0) + forces.get(k, 0)
            else:
                renamed[exp] = dict(forces)
        if renamed:
            renamed_jloads[joint] = renamed

    # ── Rename cl ─────────────────────────────────────────────
    renamed_cl: dict = {}
    for cat_key, orig_cases in cl_orig.items():
        if isinstance(orig_cases, str):
            orig_cases = [orig_cases]
        new_cases = []
        seen_exp = set()
        for c in orig_cases:
            exp = rename_map.get(c)
            if exp and exp not in seen_exp:
                new_cases.append(exp)
                seen_exp.add(exp)
        renamed_cl[cat_key] = new_cases   # keep key even if empty (gen_combos needs all keys)

    # Ensure every key gen_combos expects is present (avoids KeyError on empty categories)
    for _k in ("dead", "superdead", "live", "live_roof", "live_eq",
               "seismic_x", "seismic_y",
               "wind_xp", "wind_xn", "wind_yp", "wind_yn", "other"):
        renamed_cl.setdefault(_k, [])

    # ── Generate combos ───────────────────────────────────────
    combos = gen_combos(renamed_cl, R=params["R"], ortho=True)

    return export_lp, rename_map, renamed_jloads, renamed_cl, combos
