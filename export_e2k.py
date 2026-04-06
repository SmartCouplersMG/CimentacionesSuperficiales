# export_e2k.py
# ------------------------------------------------------------
# Exportador ETABS .e2k para el modelo de cimentación
# v2:
#   - $ CONTROLS section with UNITS (required by ETABS parser)
#   - canonical load names (D, SD, Sx, Sy, L, Le, Lr, Wx+/-, Wy+/-)
#     via export_utils.build_export_loads()
#   - zapatas shell malladas con resortes de área (AREASPRING)
#   - pedestales como COLUMN frames
#   - rigidez bajo columna: modificadores ×1000 en paneles solapados
#   - pedestal joint en N+0.0: DIAPH "DISCONNECTED"
#   - resortes verticales "Compression Only" (NONLINEAROPT3)
#   - vigas de enlace como BEAM en N+0.0
# ------------------------------------------------------------
import math
import re
import uuid

from export_s2k import (
    build_axis_lines,
    generate_compatible_mesh,
    _build_concrete_material_name,
)
from export_utils import build_export_loads, CAT_TO_DESTYPE


def _guid():
    return str(uuid.uuid4()).upper()


def _f(v):
    """Format a number for .e2k output."""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if abs(v) < 1e-12:
            v = 0.0
        txt = f"{v:.6f}".rstrip("0").rstrip(".")
        return txt if txt else "0"
    return str(v)


def _panel_intersects_col(xs, ys, i, j, cols):
    """True if panel (i,j)→(i+1,j+1) overlaps any column footprint."""
    px0, px1 = xs[i], xs[i + 1]
    py0, py1 = ys[j], ys[j + 1]
    for c in cols:
        cx0 = c["x"] - c["bx"] / 2 - 1e-9
        cx1 = c["x"] + c["bx"] / 2 + 1e-9
        cy0 = c["y"] - c["by"] / 2 - 1e-9
        cy1 = c["y"] + c["by"] / 2 + 1e-9
        if px0 < cx1 and px1 > cx0 and py0 < cy1 and py1 > cy0:
            return True
    return False


def _joint_near_col(x, y, cols):
    """True if (x,y) is within any column footprint."""
    for c in cols:
        cx0 = c["x"] - c["bx"] / 2 - 1e-9
        cx1 = c["x"] + c["bx"] / 2 + 1e-9
        cy0 = c["y"] - c["by"] / 2 - 1e-9
        cy1 = c["y"] + c["by"] / 2 + 1e-9
        if cx0 <= x <= cx1 and cy0 <= y <= cy1:
            return True
    return False


# ─────────────────────────────────────────────────────────────
def export_foundation_e2k(model_data, results, params, export_cfg=None):
    export_cfg = export_cfg or {}

    final_footings = results["final_footings"]
    tie_systems    = results.get("tie_systems", [])
    ties           = results.get("ties", {})

    model_name   = export_cfg.get("model_name", "CIMENTACION_ETABS")
    ped_h        = float(export_cfg.get("pedestal_h", 0.50))
    k_subgrade   = float(export_cfg.get("k_subgrade", 12000.0))
    alpha_xy     = float(export_cfg.get("alpha_xy", 0.35))
    fc_mpa       = float(export_cfg.get("fc_mpa", 21.0))
    fallback_b   = float(export_cfg.get("tie_b", 0.30))
    fallback_h   = float(export_cfg.get("tie_h", 0.50))
    shell_prefix = export_cfg.get("shell_section_prefix", "ZAP_")

    concrete_mat = _build_concrete_material_name(fc_mpa)
    E_mpa        = 4700.0 * math.sqrt(fc_mpa)

    ped_story  = f"N+{ped_h:.2f}"
    base_story = "N+0.0"
    stiff_mod  = 1000          # modifier for panels under column footprint
    kv         = k_subgrade
    kh         = alpha_xy * k_subgrade

    # ── Canonical loads (renamed) ────────────────────────────
    export_lp, rename_map, jloads, renamed_cl, combos = build_export_loads(
        model_data, params)

    # ── Collect footing shell sections ───────────────────────
    shell_secs = {}     # name → thickness
    for f in final_footings:
        sec = f"{shell_prefix}{int(round(f['h'] * 100))}cm"
        shell_secs[sec] = f["h"]

    # ── Pedestal sections ────────────────────────────────────
    ped_secs = {}       # (bx, by) → sec_name
    for f in final_footings:
        for c in f.get("cols", []):
            key = (round(c["bx"], 4), round(c["by"], 4))
            if key not in ped_secs:
                ped_secs[key] = (
                    c.get("section")
                    or f"PED_{int(round(c['bx']*100))}x{int(round(c['by']*100))}"
                )

    # ── Beam sections (tie beams) ────────────────────────────
    beam_sec_by_sysid = {}  # sys_id → (sec_name, b, h)
    for sys in tie_systems:
        if sys.get("status") in ("insuficiente", "distancia_insuficiente"):
            continue
        b_v = float(sys.get("b_viga", fallback_b))
        h_v = float(sys.get("h_viga", fallback_h))
        sn  = f"VG_{sys['system_id']}_{int(round(b_v*100))}x{int(round(h_v*100))}"
        beam_sec_by_sysid[sys["system_id"]] = (sn, b_v, h_v)

    # ── Support lookups ──────────────────────────────────────
    joint_to_footing = {}
    for f in final_footings:
        for c in f.get("cols", []):
            joint_to_footing[str(c["joint"])] = f["id"]

    pair_to_system = {}
    for sys in tie_systems:
        if sys.get("status") in ("insuficiente", "distancia_insuficiente"):
            continue
        fids = sys.get("footings", [])
        for i in range(len(fids) - 1):
            pair_to_system[(fids[i], fids[i + 1], sys.get("direction", "X"))] = sys["system_id"]
            pair_to_system[(fids[i + 1], fids[i], sys.get("direction", "X"))] = sys["system_id"]

    # ════════════════════════════════════════════════════════
    # Geometry generation
    # ════════════════════════════════════════════════════════
    point_map = {}          # (x, y) → pid
    point_defs = []         # POINT coordinate lines
    pid_counter = [1]

    def get_pid(x, y):
        key = (round(x, 6), round(y, 6))
        if key in point_map:
            return point_map[key]
        pid = str(pid_counter[0]); pid_counter[0] += 1
        point_map[key] = pid
        point_defs.append(f'  POINT  "{pid}"  {_f(x)}  {_f(y)}')
        return pid

    line_defs    = []   # LINE CONNECTIVITIES
    area_defs    = []   # AREA CONNECTIVITIES
    point_assign = []   # POINT ASSIGNS (raw, will be deduped)
    line_assign  = []   # LINE ASSIGNS
    area_assign  = []   # AREA ASSIGNS
    point_loads  = []   # POINT LOADS

    line_counter = [1]
    area_counter = [1]
    tie_done     = set()

    for f in final_footings:
        cols = f.get("cols", [])
        pedestal_rects = [
            (c["x"] - c["bx"]/2, c["y"] - c["by"]/2,
             c["x"] + c["bx"]/2, c["y"] + c["by"]/2)
            for c in cols
        ]
        xs, ys = generate_compatible_mesh(f, pedestal_rects, min_lines=5)

        # ── Footing mesh nodes ──────────────────────────────
        mesh_pids = {}  # (i, j) → pid
        for j, y in enumerate(ys):
            for i, x in enumerate(xs):
                pid = get_pid(x, y)
                mesh_pids[(i, j)] = pid
                if _joint_near_col(x, y, cols):
                    point_assign.append(
                        f'  POINTASSIGN  "{pid}"  "{base_story}"  DIAPH "DISCONNECTED"')
                else:
                    point_assign.append(
                        f'  POINTASSIGN  "{pid}"  "{base_story}"  USERJOINT  "Yes"')

        # ── Footing area elements ───────────────────────────
        sec_name = f"{shell_prefix}{int(round(f['h']*100))}cm"
        for j in range(len(ys) - 1):
            for i in range(len(xs) - 1):
                aid = f"FA{area_counter[0]}"; area_counter[0] += 1
                n1 = mesh_pids[(i,   j)]
                n2 = mesh_pids[(i+1, j)]
                n3 = mesh_pids[(i+1, j+1)]
                n4 = mesh_pids[(i,   j+1)]
                area_defs.append(
                    f'  AREA  "{aid}"  FLOOR  4  "{n1}"  "{n2}"  "{n3}"  "{n4}"  0  0  0  0')

                under_col = _panel_intersects_col(xs, ys, i, j, cols)
                if under_col:
                    mod_str = (
                        f'PROPMODF11 {stiff_mod} PROPMODF22 {stiff_mod} '
                        f'PROPMODF12 {stiff_mod} PROPMODM11 {stiff_mod} '
                        f'PROPMODM22 {stiff_mod} PROPMODM12 {stiff_mod} '
                        f'PROPMODV13 {stiff_mod} PROPMODV23 {stiff_mod} '
                    )
                else:
                    mod_str = ""
                area_assign.append(
                    f'  AREAASSIGN  "{aid}"  "{base_story}"  SECTION "{sec_name}"  '
                    f'{mod_str}SPRINGPROP "ASpr1"  '
                    f'OBJMESHTYPE "DEFAULT"  ADDRESTRAINT "No"  '
                    f'CARDINALPOINT "TOP"  TRANSFORMSTIFFNESSFOROFFSETS "No"')

        # ── Pedestals ───────────────────────────────────────
        for c in cols:
            xc, yc = c["x"], c["y"]
            pid_col = get_pid(xc, yc)
            # Pedestal base joint → disconnected from diaphragm
            point_assign.append(
                f'  POINTASSIGN  "{pid_col}"  "{base_story}"  DIAPH "DISCONNECTED"')

            sec_key = (round(c["bx"], 4), round(c["by"], 4))
            ped_sec = ped_secs[sec_key]

            lid = f"LC{line_counter[0]}"; line_counter[0] += 1
            line_defs.append(
                f'  LINE  "{lid}"  COLUMN  "{pid_col}"  "{pid_col}"  1')
            line_assign.append(
                f'  LINEASSIGN  "{lid}"  "{ped_story}"  SECTION "{ped_sec}"  '
                f'MINNUMSTA 3  AUTOMESH "YES"  MESHATINTERSECTIONS "YES"')

            # Loads at pedestal top (ped_story) — use canonical (renamed) names
            orig_jid = str(c["joint"])
            for exp_name, fv in jloads.get(orig_jid, {}).items():
                f1 = fv.get("F1", 0); f2 = fv.get("F2", 0); f3 = fv.get("F3", 0)
                m1 = fv.get("M1", 0); m2 = fv.get("M2", 0); m3 = fv.get("M3", 0)
                point_loads.append(
                    f'  POINTLOAD  "{pid_col}"  "{ped_story}"  "{exp_name}"  '
                    f'UX  {_f(f1)}  UY  {_f(f2)}  UZ  {_f(f3)}  '
                    f'RX  {_f(m1)}  RY  {_f(m2)}  RZ  {_f(m3)}  CSys  "Global"')

        # ── Tie beams ────────────────────────────────────────
        own_joints = [str(c["joint"]) for c in cols]
        for oj in own_joints:
            t = ties.get(str(oj), {})
            if not t.get("needs_tie"):
                continue

            def add_tie(target_raw, direction):
                target_raw = str(target_raw)
                pair = tuple(sorted([str(oj), target_raw])) + (direction,)
                if pair in tie_done:
                    return
                tie_done.add(pair)
                c_from = next((cc for cc in cols if str(cc["joint"]) == str(oj)), None)
                c_to   = None
                for ff in final_footings:
                    for cc in ff.get("cols", []):
                        if str(cc["joint"]) == target_raw:
                            c_to = cc; break
                    if c_to:
                        break
                if not c_from or not c_to:
                    return
                fid_from = joint_to_footing.get(str(oj))
                fid_to   = joint_to_footing.get(target_raw)
                sys_id   = pair_to_system.get((fid_from, fid_to, direction))
                info     = beam_sec_by_sysid.get(sys_id)
                if not info:
                    return
                beam_sec, _, _ = info
                p1 = get_pid(c_from["x"], c_from["y"])
                p2 = get_pid(c_to["x"],   c_to["y"])
                lid = f"LB{line_counter[0]}"; line_counter[0] += 1
                line_defs.append(
                    f'  LINE  "{lid}"  BEAM  "{p1}"  "{p2}"  0')
                line_assign.append(
                    f'  LINEASSIGN  "{lid}"  "{base_story}"  SECTION "{beam_sec}"  '
                    f'MINNUMSTA 3  AUTOMESH "YES"  MESHATINTERSECTIONS "YES"')

            if t.get("is_corner"):
                tx = t.get("tie_x", {}); ty = t.get("tie_y", {})
                if tx.get("tie_to"):
                    add_tie(tx["tie_to"], "X")
                if ty.get("tie_to"):
                    add_tie(ty["tie_to"], "Y")
            elif t.get("tie_to"):
                add_tie(t["tie_to"], t.get("tie_dir", "X"))

    # ════════════════════════════════════════════════════════
    # Assemble output
    # ════════════════════════════════════════════════════════
    out = []

    # ── File header ─────────────────────────────────────────
    out.append(f"$ File generated by Foundation Exporter - ETABS | Model={model_name}")
    out.append("")

    # ── PROGRAM INFORMATION ──────────────────────────────────
    # Only PROGRAM keyword here (no UNITS — those go in CONTROLS)
    out.append("$ PROGRAM INFORMATION")
    out.append('  PROGRAM  "ETABS"  VERSION "21.0.0"')
    out.append("")

    # ── CONTROLS ─────────────────────────────────────────────
    # ETABS parser reads UNITS, MERGETOL, etc. from this section
    out.append("$ CONTROLS")
    out.append('  UNITS  "KN"  "M"  "C"')
    out.append(f'  TITLE1  "{model_name}"')
    out.append('  PREFERENCE  MERGETOL 0.001')
    out.append("")

    # ── STORIES ──────────────────────────────────────────────
    # Base N+0.0 is implicit in ETABS; only list stories above it
    out.append("$ STORIES - IN SEQUENCE FROM TOP")
    out.append(
        f'  STORY  "{ped_story}"  HEIGHT  {_f(ped_h)}  MASTERSTORY "Yes"')
    out.append("")

    # ── MATERIAL PROPERTIES ──────────────────────────────────
    out.append("$ MATERIAL PROPERTIES")
    out.append(
        f'  MATERIAL  "{concrete_mat}"  TYPE "CONCRETE"  '
        f'WEIGHTPERVOLUME  24  MODULUS  {_f(E_mpa)}  POISSON  0.2  '
        f'THERMALCOEFF  9.9E-06')
    out.append("")

    # ── SHELL PROPERTIES ─────────────────────────────────────
    out.append("$ SHELL PROPERTIES")
    for sec, thk in shell_secs.items():
        out.append(
            f'  SHELLPROP  "{sec}"  PROPTYPE "Slab"  MATERIAL "{concrete_mat}"  '
            f'MODELINGTYPE "ShellThin"  SLABTYPE "Slab"  SLABTHICKNESS {_f(thk)}')
    out.append("")

    # ── FRAME PROPERTIES ─────────────────────────────────────
    out.append("$ FRAME PROPERTIES")
    for (bx, by), sec in ped_secs.items():
        out.append(
            f'  FRAMESECTION  "{sec}"  MATERIAL "{concrete_mat}"  '
            f'SHAPE "Concrete Rectangular"  D {_f(by)}  B {_f(bx)}  '
            f'AUTORIGIDZONEAREA "Yes"')
    written_beam_secs = set()
    for _, (sec, b_v, h_v) in beam_sec_by_sysid.items():
        if sec not in written_beam_secs:
            out.append(
                f'  FRAMESECTION  "{sec}"  MATERIAL "{concrete_mat}"  '
                f'SHAPE "Concrete Rectangular"  D {_f(h_v)}  B {_f(b_v)}  '
                f'AUTORIGIDZONEAREA "Yes"')
            written_beam_secs.add(sec)
    out.append("")

    # ── AREA SPRING PROPERTIES ───────────────────────────────
    out.append("$ AREA SPRING PROPERTIES")
    out.append(
        f'  AREASPRING  "ASpr1"  U1  {_f(kh)}  U2  {_f(kh)}  U3  {_f(kv)}  '
        f'NONLINEAROPT3  "Compression Only"')
    out.append("")

    # ── LOAD PATTERNS ────────────────────────────────────────
    out.append("$ LOAD PATTERNS")
    for exp_name, des_type in export_lp.items():
        out.append(
            f'  LOADPATTERN  "{exp_name}"  TYPE "{des_type}"  SELFWEIGHT  0')
    out.append("")

    # ── LOAD CASES ───────────────────────────────────────────
    out.append("$ LOAD CASES")
    for exp_name in export_lp:
        out.append(
            f'  LOADCASE  "{exp_name}"  TYPE  "Linear Static"  INITCOND  "PRESET"')
        out.append(
            f'  LOADCASE  "{exp_name}"  LOADPAT  "{exp_name}"  SF  1')
    out.append("")

    # ── COMBINATIONS ─────────────────────────────────────────
    out.append("$ COMBINATIONS")
    for c in combos["ADS"] + combos["LRFD"]:
        out.append(f'  COMBCASE  "{c["name"]}"  TYPE  "Linear Add"')
        for case_name, sf in c["factors"].items():
            # Only include the case if it's actually defined in export_lp
            if case_name in export_lp:
                out.append(
                    f'  COMBCASE  "{c["name"]}"  CASE  "{case_name}"  SF  {_f(sf)}')
    out.append("")

    # ── POINT COORDINATES ────────────────────────────────────
    out.append("$ POINT COORDINATES")
    out.extend(point_defs)
    out.append("")

    # ── LINE CONNECTIVITIES ───────────────────────────────────
    out.append("$ LINE CONNECTIVITIES")
    out.extend(line_defs)
    out.append("")

    # ── AREA CONNECTIVITIES ───────────────────────────────────
    out.append("$ AREA CONNECTIVITIES")
    out.extend(area_defs)
    out.append("")

    # ── POINT LOADS ──────────────────────────────────────────
    if point_loads:
        out.append("$ POINT LOADS")
        out.extend(point_loads)
        out.append("")

    # ── POINT ASSIGNS ─────────────────────────────────────────
    # Deduplicate: keep first occurrence per (pid, story)
    seen_pa: set = set()
    deduped_pa = []
    for line in point_assign:
        m = re.findall(r'"([^"]*)"', line)
        key = (m[0], m[1]) if len(m) >= 2 else line
        if key not in seen_pa:
            seen_pa.add(key)
            deduped_pa.append(line)

    out.append("$ POINT ASSIGNS")
    out.extend(deduped_pa)
    out.append("")

    # ── LINE ASSIGNS ──────────────────────────────────────────
    out.append("$ LINE ASSIGNS")
    out.extend(line_assign)
    out.append("")

    # ── AREA ASSIGNS ──────────────────────────────────────────
    out.append("$ AREA ASSIGNS")
    out.extend(area_assign)
    out.append("")

    return "\n".join(out)
