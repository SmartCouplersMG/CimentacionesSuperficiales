"""
engine.py v7 — Orquestador de diseño de cimentaciones superficiales
NSR-10 Título B — Bowles/McCormac

FASE 0:
- Diagnóstico del modelo
- Selección automática de fuente:
    A) cargas en nudos
    B) restricciones/apoyos base
- Si existen ambas, deja listo el caso para preguntar al usuario
- Si no existe ninguna, marca modelo inválido

Módulos:
  parser.py      — Lectura de .$2k, clasificación, combinaciones, inspección de fuentes
  isolated.py    — Zapatas aisladas: presiones, punzonamiento, optimización, diseño
  combined.py    — Zapatas combinadas: solapamiento, análisis longitudinal
  tie_system.py  — Vigas de enlace: solvers, análisis de sistemas

Este archivo expone:
  read_model        — Paso 0: parsear modelo + diagnosticar fuente
  resolve_basis_selection — Resolver manualmente A/B si el parser detectó ambas
  run_design        — Pasos 3-5: optimizar, combinar, diseñar
  deduce_tie_beams  — Paso 2: deducción automática de vigas
  propose_rebar     — Utilidad para la UI
"""

import math
import numpy as np
import pandas as pd

# ── Importar de módulos ──
from parser import (
    parse_s2k,
    get_joints,
    get_frames,
    get_section_dims,
    get_jloads,
    get_lpats,
    get_frame_local_axes,
    identify_supports,
    classify,
    gen_combos,
    compute_forces,
    inspect_foundation_sources,
    get_area_connectivity,
    get_area_sections,
    build_shell_entities,
    filter_wall_candidate_shells,
    group_connected_shells,
    build_wall_entities_from_shell_groups,
    filter_wall_entities_by_joint_loads,
    filter_wall_entities_by_restraints,
    get_area_section_thicknesses,
    detect_file_format,
    parse_e2k,
    auto_classify_patterns,
    classify_from_user,
)

from isolated import (
    optimize_isolated,
    full_structural_design,
    propose_rebar,
    soil_pressure,
    calc_as,
    infer_column_axis,
)

from combined import (
    check_overlaps,
    design_combined_footing,
)

from tie_system import (
    deduce_tie_beams,
    build_tie_systems,
    analyze_tie_system,
    apply_system_reactions_to_footings,
)


# ================================================================
# HELPERS INTERNOS FASE 0
# ================================================================

def _normalize_joint_id(v):
    """
    Normaliza IDs de joints para evitar diferencias tipo:
      1   vs 1.0   vs '1'
    """
    s = str(v).strip()
    try:
        f = float(s)
        if abs(f - round(f)) < 1e-9:
            return str(int(round(f)))
    except Exception:
        pass
    return s

def build_jloads_from_sap_reactions_excel(df, foundation_entities):
    """
    Convierte la tabla SAP 'Joint Reactions' en una estructura tipo jloads:
        jloads[joint][OutputCase] = {
            "F1": ...,
            "F2": ...,
            "F3": ...,
            "M1": ...,
            "M2": ...,
            "M3": ...
        }
    """
    required = ["Joint", "OutputCase", "F1", "F2", "F3", "M1", "M2", "M3"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Faltan columnas requeridas en Excel de reacciones: {missing}")

    valid_joints = {_normalize_joint_id(ent["joint"]) for ent in foundation_entities}
    out = {}

    df2 = df.copy()

    # Normalizar joints y nombres de caso
    df2["Joint"] = df2["Joint"].apply(_normalize_joint_id)
    df2["OutputCase"] = df2["OutputCase"].astype(str).str.strip()

    # Eliminar fila de unidades / textos
    df2 = df2[df2["Joint"].str.upper() != "TEXT"]
    df2 = df2[df2["OutputCase"].str.upper() != "TEXT"]

    # Convertir numéricos
    for c in ["F1", "F2", "F3", "M1", "M2", "M3"]:
        df2[c] = pd.to_numeric(df2[c], errors="coerce")

    # Quitar filas vacías o no numéricas
    df2 = df2.dropna(subset=["F1", "F2", "F3", "M1", "M2", "M3"])

    # Filtrar solo joints válidos
    df2 = df2[df2["Joint"].isin(valid_joints)]

    sign = -1.0

    for jid in sorted(df2["Joint"].unique()):
        sub = df2[df2["Joint"] == jid]
        out[jid] = {}

        for case in sub["OutputCase"].unique():
            rows = sub[sub["OutputCase"] == case]

            row = None
            if "StepType" in rows.columns:
                rows_max = rows[rows["StepType"].astype(str).str.upper() == "MAX"]
                if not rows_max.empty:
                    row = rows_max.iloc[0]
            if row is None:
                row = rows.iloc[0]

            out[jid][case] = {
                "F1": sign * float(row["F1"]),
                "F2": sign * float(row["F2"]),
                "F3": sign * float(row["F3"]),
                "M1": sign * float(row["M1"]),
                "M2": sign * float(row["M2"]),
                "M3": sign * float(row["M3"]),
            }

    return out

def _supports_to_foundation_entities(supports):
    """
    Convierte supports dict -> lista de entidades de cimentación unificadas.

    Cada entidad representa un candidato puntual de cimentación,
    independientemente de si proviene de cargas nodales o restricciones.
    """
    entities = []
    warnings = []

    for jid, sup in sorted(supports.items(), key=lambda x: (x[1]["x"], x[1]["y"])):
        if sup.get("type") == "orphan":
            if sup.get("warning"):
                warnings.append(sup["warning"])
            continue

        if sup.get("warning"):
            warnings.append(sup["warning"])

        ent = {
            "id": f"J{jid}",
            "entity_type": "point",
            "source": sup.get("source", ""),
            "joint": jid,
            "origin_joints": [jid],
            "x": sup["x"],
            "y": sup["y"],
            "z": sup.get("z", 0.0),
            "bx": sup["bx"],
            "by": sup["by"],
            "section": sup.get("section", ""),
            "support_type": sup.get("type", ""),
            "raw_support": dict(sup),
        }

        # Si vino de restricciones, guardar info útil
        if "restraints" in sup:
            ent["restraints"] = dict(sup["restraints"])

        entities.append(ent)

    return entities, warnings

def _foundation_entities_to_columns(entities):
    """
    Convierte foundation_entities -> columns
    para mantener compatibilidad con la UI actual.
    """
    columns = []
    for ent in entities:
        if ent.get("entity_type") != "point":
            continue

        columns.append({
            "joint": ent["joint"],
            "x": ent["x"],
            "y": ent["y"],
            "bx": ent["bx"],
            "by": ent["by"],
            "section": ent.get("section", ""),
            "type": ent.get("support_type", ""),
            "source": ent.get("source", ""),
        })
    return columns

def _supports_to_columns(supports):
    """
    Convierte supports dict -> lista de columnas/candidatos utilizables.
    Excluye 'orphan' del flujo principal, pero su warning puede conservarse aparte.
    """
    columns = []
    warnings = []

    for jid, sup in sorted(supports.items(), key=lambda x: (x[1]["x"], x[1]["y"])):
        if sup.get("type") == "orphan":
            if sup.get("warning"):
                warnings.append(sup["warning"])
            continue

        if sup.get("warning"):
            warnings.append(sup["warning"])

        columns.append({
            "joint": jid,
            "x": sup["x"],
            "y": sup["y"],
            "bx": sup["bx"],
            "by": sup["by"],
            "section": sup.get("section", ""),
            "type": sup.get("type", ""),
            "source": sup.get("source", ""),
        })

    return columns, warnings


def _select_supports_by_basis(diag, basis_mode):
    """
    Escoge el set de supports según la fuente elegida.
    """
    if basis_mode == "joint_loads":
        return diag.get("usable_joint_candidates", {})
    elif basis_mode == "support_reactions":
        return diag.get("usable_restraint_candidates", {})
    return {}

def _select_walls_by_basis(diag, basis_mode):
    if basis_mode == "joint_loads":
        return diag.get("usable_wall_joint_candidates", {})
    elif basis_mode == "support_reactions":
        return diag.get("usable_wall_restraint_candidates", {})
    return {}

def _remove_point_entities_absorbed_by_walls(foundation_entities, active_wall_entities):
    """
    Si un punto de cimentación coincide con un base_joint activo de un muro,
    se elimina de la familia puntual. Predomina el muro sobre la columna.
    """
    if not active_wall_entities:
        return foundation_entities

    wall_base_joints = set()
    for w in active_wall_entities.values():
        for j in w.get("active_base_joints", []) or w.get("base_joints", []):
            wall_base_joints.add(str(j))

    filtered = []
    for ent in foundation_entities:
        jid = str(ent.get("joint", ""))
        if ent.get("entity_type") == "point" and jid in wall_base_joints:
            continue
        filtered.append(ent)

    return filtered

def _build_model_return(
    tables, joints, jloads, lp, cl,
    supports_selected, warnings_selected,
    basis_mode, basis_options, status_message,
    diag, params,
    wall_entities_all=None,
    active_wall_entities=None,
    shell_entities=None,
    candidate_wall_shells=None,
    shell_groups=None,

):
    """
    Construye el dict final que consume la app.
    """
    foundation_entities, warnings_from_supports = _supports_to_foundation_entities(supports_selected)
    foundation_entities = _remove_point_entities_absorbed_by_walls(
        foundation_entities,
        active_wall_entities or {},
    )
    columns = _foundation_entities_to_columns(foundation_entities)
    warnings = list(warnings_selected) + warnings_from_supports

    combos = gen_combos(cl, R=params["R"])

    return {

        # ── Salida principal para la UI ──
        "foundation_entities": foundation_entities,
        "n_foundation_entities": len(foundation_entities),
        "columns": columns,
    
        "warnings": warnings,
        "basis_mode": basis_mode,
        "basis_options": basis_options,
        "status_message": status_message,

        "wall_entities": list((active_wall_entities or {}).values()),
        "n_wall_entities": len(active_wall_entities or {}),
        "_wall_entities_all": wall_entities_all or [],
        "wall_resultants": diag.get("wall_resultants_active", {}),
        "_wall_resultants_joint": diag.get("wall_resultants_joint", {}),
        "wall_foundation_entities": diag.get("wall_foundation_entities_active", []),
        "n_wall_foundation_entities": len(diag.get("wall_foundation_entities_active", [])),
        "_active_wall_entities": active_wall_entities or {},
        "_shell_entities": shell_entities or [],
        "_candidate_wall_shells": candidate_wall_shells or [],
        "_shell_groups": shell_groups or [],
        "columns_design": columns,
        "wall_segment_audit": diag.get("wall_segment_audit", []),

        "design_entities": diag.get("design_entities_active", []),
        "design_jloads": diag.get("design_jloads_active", {}),

        # ── Diagnóstico Fase 0 ──
        "has_joint_loads": diag.get("has_joint_loads", False),
        "has_restraints": diag.get("has_restraints", False),
        "joint_load_count": diag.get("joint_load_count", 0),
        "restraint_count": diag.get("restraint_count", 0),

        # Para que app.py pueda decidir o preguntar luego
        "_diag": diag,

        # ── Info del modelo original ──
        "n_combos": {
            "ADS": len(combos["ADS"]),
            "LRFD": len(combos["LRFD"]),
        },
        "load_patterns": {k: v for k, v in cl.items() if v},

        # ── Estado bruto interno ──
        "_tables": tables,
        "_joints": joints,
        "_jloads": jloads,
        "_lp": lp,
        "_cl": cl,
        "_supports": supports_selected,

        # Set completos por fuente para futura selección manual
        "_supports_joint_loads": diag.get("usable_joint_candidates", {}),
        "_supports_restraints": diag.get("usable_restraint_candidates", {}),

        # ── Formato y metadatos de archivo ──
        "file_format": ("etabs" if isinstance(tables.get("_etabs_meta"), dict) else "sap2000"),
        "_lpats_raw": (tables.get("_etabs_meta", {}).get("lpats_raw", lp) if isinstance(tables.get("_etabs_meta"), dict) else lp),
        "_etabs_base_story": (tables.get("_etabs_meta", {}).get("base_story", "N+0.0") if isinstance(tables.get("_etabs_meta"), dict) else "N+0.0"),
    }

def _infer_wall_axis_segment(wall_entity, joints):
    """
    Construye el segmento eje del muro a partir de sus active_base_joints.
    """
    base_joints = wall_entity.get("active_base_joints", []) or wall_entity.get("base_joints", [])
    pts = []
    for jid in base_joints:
        jid_s = str(jid)
        if jid_s in joints:
            pts.append((
                float(joints[jid_s]["x"]),
                float(joints[jid_s]["y"])
            ))

    if not pts:
        return {
            "x0": 0.0, "y0": 0.0, "x1": 0.0, "y1": 0.0,
            "x_center": 0.0, "y_center": 0.0,
            "length": 0.0,
        }

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x_center = sum(xs) / len(xs)
    y_center = sum(ys) / len(ys)

    orientation = wall_entity.get("orientation", "COMPLEX")

    if orientation == "X":
        x0 = min(xs); x1 = max(xs)
        y0 = y1 = y_center
    elif orientation == "Y":
        y0 = min(ys); y1 = max(ys)
        x0 = x1 = x_center
    else:
        # fallback simple usando extremos de la nube de puntos
        # por ahora no resolver diagonal fino
        dx = max(xs) - min(xs)
        dy = max(ys) - min(ys)
        if dx >= dy:
            x0 = min(xs); x1 = max(xs)
            y0 = y1 = y_center
        else:
            y0 = min(ys); y1 = max(ys)
            x0 = x1 = x_center

    length = math.sqrt((x1 - x0)**2 + (y1 - y0)**2)

    return {
        "x0": round(x0, 4),
        "y0": round(y0, 4),
        "x1": round(x1, 4),
        "y1": round(y1, 4),
        "x_center": round(x_center, 4),
        "y_center": round(y_center, 4),
        "length": round(length, 4),
    }


def build_wall_foundation_entities(wall_entities, wall_resultants, joints, default_support_width=0.60):
    """
    Construye entidades lineales de diseño a partir de muros activos + resultantes.
    """
    out = []

    for w in wall_entities:
        wid = w.get("id", "")
        wr = wall_resultants.get(wid, {})

        axis = _infer_wall_axis_segment(w, joints)

        length = axis["length"] if axis["length"] > 1e-6 else float(w.get("length_est", 0.0) or 0.0)
        thickness = w.get("thickness", None)

        if thickness is not None:
            support_width_initial = max(default_support_width, 2.0 * float(thickness))
        else:
            support_width_initial = default_support_width

        out.append({
            "id": f"WF_{wid}",
            "entity_type": "wall_foundation",
            "wall_id": wid,
            "orientation": w.get("orientation", ""),
            "length": round(length, 4),
            "height": round(float(w.get("height", 0.0) or 0.0), 4),
            "thickness": thickness,
            "x0": axis["x0"],
            "y0": axis["y0"],
            "x1": axis["x1"],
            "y1": axis["y1"],
            "x_center": axis["x_center"],
            "y_center": axis["y_center"],
            "support_width_initial": round(support_width_initial, 4),
            "active_base_joints": list(w.get("active_base_joints", [])),
            "resultants_by_case": wr.get("resultants_by_case", {}),
        })

    return out

def build_wall_as_column_entities(wall_entities, wall_resultants, joints):
    """
    Convierte cada muro activo en una entidad equivalente tipo columna,
    para reutilizar el módulo actual de zapatas aisladas.
    """
    out = []

    for w in wall_entities:
        wid = w.get("id", "")
        wr = wall_resultants.get(wid, {})
        orientation = w.get("orientation", "")

        # Centro geométrico base
        base_joints = w.get("active_base_joints", []) or w.get("base_joints", [])
        xs = [float(joints[str(j)]["x"]) for j in base_joints if str(j) in joints]
        ys = [float(joints[str(j)]["y"]) for j in base_joints if str(j) in joints]

        if xs and ys:
            x_center = sum(xs) / len(xs)
            y_center = sum(ys) / len(ys)
        else:
            x_center = 0.0
            y_center = 0.0

        length = float(w.get("length_est", 0.0) or 0.0)
        thickness = float(w.get("thickness", 0.20) or 0.20)

        if orientation == "X":
            bx = length
            by = thickness
        elif orientation == "Y":
            bx = thickness
            by = length
        else:
            # fallback con caja geométrica del muro
            bx = max(float(w.get("x_max", 0.0)) - float(w.get("x_min", 0.0)), thickness)
            by = max(float(w.get("y_max", 0.0)) - float(w.get("y_min", 0.0)), thickness)

        out.append({
            "id": f"WC_{wid}",
            "entity_type": "wall_as_column",
            "joint": f"WJ_{wid}",
            "x": round(x_center, 4),
            "y": round(y_center, 4),
            "z": round(float(w.get("z_min", 0.0) or 0.0), 4),
            "bx": round(bx, 4),
            "by": round(by, 4),
            "section": "WALL_EQUIV",
            "type": "wall_equivalent",
            "source": w.get("active_basis", ""),
            "wall_id": wid,
            "orientation": orientation,
            "thickness": thickness,
            "length": length,
            "resultants_by_case": wr.get("resultants_by_case", {}),
        })

    return out

def build_wall_as_column_jloads(wall_as_column_entities):
    """
    Construye una estructura tipo jloads compatible con el motor actual,
    usando las resultantes consolidadas del muro equivalente.
    """
    out = {}

    for ent in wall_as_column_entities:
        jid = str(ent["joint"])
        out[jid] = {}

        for case_name, r in ent.get("resultants_by_case", {}).items():
            P = float(r.get("P", 0.0) or 0.0)
            Vx = float(r.get("Vx", 0.0) or 0.0)
            Vy = float(r.get("Vy", 0.0) or 0.0)
            Mx = float(r.get("Mx", 0.0) or 0.0)
            My = float(r.get("My", 0.0) or 0.0)
            Mz = float(r.get("Mz", 0.0) or 0.0)

            out[jid][case_name] = {
                "F1": Vx,
                "F2": Vy,
                "F3": -P,
                "M1": Mx,
                "M2": My,
                "M3": Mz,
            }

    return out

# ================================================================
# PASO 0: LEER MODELO + DIAGNÓSTICO DE FUENTE
# ================================================================
import math
from collections import defaultdict

def _unit(vx, vy, eps=1e-12):
    n = math.hypot(vx, vy)
    if n < eps:
        return 0.0, 0.0
    return vx/n, vy/n

def _angle(vx, vy):
    return math.atan2(vy, vx)

def _angle_diff(a, b):
    d = abs(a - b)
    d = min(d, 2*math.pi - d)
    return d

def _is_colinear(theta1, theta2, tol_rad):
    # colineales si ~0 o ~pi
    d = _angle_diff(theta1, theta2)
    return d < tol_rad or abs(d - math.pi) < tol_rad

def build_base_edges_from_shell_group(shell_group, joints, tol_z=1e-3):
    # base joints por z mínimo
    zs = [joints[str(j)]["z"] for j in shell_group["joint_ids"] if str(j) in joints]
    if not zs:
        return [], []

    zmin = min(zs)
    base = [str(j) for j in shell_group["joint_ids"]
            if str(j) in joints and abs(joints[str(j)]["z"] - zmin) <= tol_z]

    # aristas: usa conectividades del shell (asume que las tienes)
    # Si no, arma edges por pares consecutivos de cada shell
    edges = []
    seen = set()
    for sh in shell_group["shell_ids"]:
        conn = shell_group["area_connectivity"].get(sh, [])
        # cierra polígono
        for i in range(len(conn)):
            j1 = str(conn[i])
            j2 = str(conn[(i+1) % len(conn)])
            if j1 not in base or j2 not in base:
                continue
            key = tuple(sorted((j1, j2)))
            if key in seen:
                continue
            seen.add(key)
            x1, y1 = float(joints[j1]["x"]), float(joints[j1]["y"])
            x2, y2 = float(joints[j2]["x"]), float(joints[j2]["y"])
            dx, dy = x2-x1, y2-y1
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            ux, uy = dx/L, dy/L
            th = _angle(ux, uy)
            edges.append({
                "j1": j1, "j2": j2,
                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "length": L,
                "ux": ux, "uy": uy,
                "theta": th
            })
    return base, edges

def group_colinear_edges(edges, tol_angle_deg=5.0):
    tol = math.radians(tol_angle_deg)
    groups = []
    used = set()

    # adjacency por nodos
    adj = defaultdict(list)
    for i, e in enumerate(edges):
        adj[e["j1"]].append(i)
        adj[e["j2"]].append(i)

    for i, e in enumerate(edges):
        if i in used:
            continue
        stack = [i]
        group = set([i])
        used.add(i)

        while stack:
            k = stack.pop()
            ek = edges[k]
            for j in (ek["j1"], ek["j2"]):
                for nei in adj[j]:
                    if nei in used:
                        continue
                    if _is_colinear(edges[nei]["theta"], ek["theta"], tol):
                        used.add(nei)
                        group.add(nei)
                        stack.append(nei)

        groups.append([edges[idx] for idx in group])

    return groups

def build_segments_from_edge_groups(edge_groups, joints, thickness):
    segments = []
    for gi, g in enumerate(edge_groups):
        # recolectar joints del grupo
        js = set()
        for e in g:
            js.add(e["j1"]); js.add(e["j2"])

        # extremos por proyección en la dirección promedio
        ux = sum(e["ux"] for e in g) / len(g)
        uy = sum(e["uy"] for e in g) / len(g)
        ux, uy = _unit(ux, uy)

        # proyectar puntos
        pts = [(j, float(joints[j]["x"]), float(joints[j]["y"])) for j in js]
        projs = [(j, x*ux + y*uy) for j, x, y in pts]
        jmin = min(projs, key=lambda t: t[1])[0]
        jmax = max(projs, key=lambda t: t[1])[0]

        x0, y0 = float(joints[jmin]["x"]), float(joints[jmin]["y"])
        x1, y1 = float(joints[jmax]["x"]), float(joints[jmax]["y"])
        L = math.hypot(x1-x0, y1-y0)

        segments.append({
            "id": f"WS_{gi+1}",
            "base_joints": list(js),
            "edges": g,
            "x0": x0, "y0": y0,
            "x1": x1, "y1": y1,
            "ux": ux, "uy": uy,
            "length": L,
            "thickness": thickness,
        })
    return segments

def map_joint_to_segments(segments):
    m = defaultdict(list)
    for si, s in enumerate(segments):
        for j in s["base_joints"]:
            m[j].append(si)
    return m

def _solve_2d_decomposition(Fx, Fy, e1, e2, eps=1e-9):
    # resuelve F = a*e1 + b*e2
    (u1x, u1y) = e1
    (u2x, u2y) = e2
    det = u1x*u2y - u1y*u2x
    if abs(det) < eps:
        # casi colineales → asigna todo al más alineado
        dot1 = Fx*u1x + Fy*u1y
        dot2 = Fx*u2x + Fy*u2y
        if abs(dot1) >= abs(dot2):
            return dot1, 0.0
        else:
            return 0.0, dot2
    a = ( Fx*u2y - Fy*u2x) / det
    b = (-Fx*u1y + Fy*u1x) / det
    return a, b

def _tributary_length_at_joint(segment, joint_id):
    L = 0.0
    for e in segment["edges"]:
        if e["j1"] == joint_id or e["j2"] == joint_id:
            L += e["length"] * 0.5
    return L

def distribute_joint_to_segments(joint_id, joint_action, seg_ids, segments, joints):
    """
    Reparte una acción nodal global entre los tramos que concurren en el nudo.

    Convención:
      F1, F2 = fuerzas globales en planta
      F3     = fuerza vertical
      M1, M2 = momentos globales alrededor de X e Y
      M3     = momento torsional/alrededor de Z

    MVP actual:
      - Si hay 1 tramo: todo va a ese tramo.
      - Si hay 2 tramos:
          * F1,F2 se descomponen exactamente en la base de ambos ejes longitudinales.
          * F3 se reparte por longitud tributaria.
          * M1,M2 se proyectan al eje local de cada tramo y luego se reparten con pesos tributarios.
          * M3 se reparte por longitud tributaria.

    Nota:
      Esta rutina conserva equilibrio global para F1,F2 exactamente,
      y para F3/M3 por construcción tributaria.
    """
    Fx = float(joint_action.get("F1", 0.0) or 0.0)
    Fy = float(joint_action.get("F2", 0.0) or 0.0)
    Fz = float(joint_action.get("F3", 0.0) or 0.0)
    Mx = float(joint_action.get("M1", 0.0) or 0.0)
    My = float(joint_action.get("M2", 0.0) or 0.0)
    Mz = float(joint_action.get("M3", 0.0) or 0.0)

    out = {}

    # ── Caso simple: un solo tramo ──
    if len(seg_ids) == 1:
        si = seg_ids[0]
        out[si] = {
            "F1": Fx, "F2": Fy, "F3": Fz,
            "M1": Mx, "M2": My, "M3": Mz
        }
        return out

    # ── MVP: dos tramos ──
    s1 = segments[seg_ids[0]]
    s2 = segments[seg_ids[1]]

    e1 = (s1["ux"], s1["uy"])
    e2 = (s2["ux"], s2["uy"])

    # 1) Fuerzas horizontales exactas por descomposición vectorial
    a, b = _solve_2d_decomposition(Fx, Fy, e1, e2)

    Fx_1 = a * e1[0]
    Fy_1 = a * e1[1]
    Fx_2 = b * e2[0]
    Fy_2 = b * e2[1]

    # 2) Pesos tributarios para vertical y momentos no direccionales
    L1 = _tributary_length_at_joint(s1, joint_id)
    L2 = _tributary_length_at_joint(s2, joint_id)
    denom = max(L1 + L2, 1e-9)
    w1 = L1 / denom
    w2 = L2 / denom

    Fz_1 = Fz * w1
    Fz_2 = Fz * w2

    # 3) Momentos Mx, My: proyectar cada uno al eje longitudinal del tramo
    # y luego reconstruir en global con peso tributario
    def project_global_moment_to_segment(Mxg, Myg, seg, weight):
        ux, uy = seg["ux"], seg["uy"]
        tx, ty = -uy, ux   # eje transversal en planta

        # Componentes locales del momento global
        M_long = Mxg * ux + Myg * uy
        M_trans = Mxg * tx + Myg * ty

        # Aplicar peso tributario para no duplicar
        M_long *= weight
        M_trans *= weight

        # Volver a global
        Mx_back = M_long * ux + M_trans * tx
        My_back = M_long * uy + M_trans * ty
        return Mx_back, My_back

    Mx_1, My_1 = project_global_moment_to_segment(Mx, My, s1, w1)
    Mx_2, My_2 = project_global_moment_to_segment(Mx, My, s2, w2)

    # 4) Torsión / Mz por peso tributario
    Mz_1 = Mz * w1
    Mz_2 = Mz * w2

    out[seg_ids[0]] = {
        "F1": Fx_1, "F2": Fy_1, "F3": Fz_1,
        "M1": Mx_1, "M2": My_1, "M3": Mz_1
    }
    out[seg_ids[1]] = {
        "F1": Fx_2, "F2": Fy_2, "F3": Fz_2,
        "M1": Mx_2, "M2": My_2, "M3": Mz_2
    }

    return out

def build_segment_resultants(segments, joint_to_segments, jloads, joints):
    # estructura: {segment_id: {case: {...}}}
    res = {i: {} for i in range(len(segments))}

    for jid, cases in jloads.items():
        seg_ids = joint_to_segments.get(str(jid), [])
        if not seg_ids:
            continue

        for case, vec in cases.items():
            dist = distribute_joint_to_segments(str(jid), vec, seg_ids, segments, joints)
            for si, contrib in dist.items():
                r = res[si].setdefault(case, {"F1":0,"F2":0,"F3":0,"M1":0,"M2":0,"M3":0})
                for k in r:
                    r[k] += float(contrib.get(k, 0.0) or 0.0)

    # redondeo opcional
    for si in res:
        for case in res[si]:
            for k in res[si][case]:
                res[si][case][k] = round(res[si][case][k], 6)
    return res

def check_family_equilibrium(joint_ids, jloads, segments, seg_resultants, tol_abs=1e-3, tol_rel=1e-2):
    """
    Verifica cierre de equilibrio entre:
      - acciones nodales originales del grupo
      - suma de acciones asignadas a los segmentos

    tol_abs: tolerancia absoluta
    tol_rel: tolerancia relativa
    """
    orig = {"F1": 0.0, "F2": 0.0, "F3": 0.0, "M1": 0.0, "M2": 0.0, "M3": 0.0}
    for jid in joint_ids:
        cases = jloads.get(str(jid), {})
        for _, v in cases.items():
            for k in orig:
                orig[k] += float(v.get(k, 0.0) or 0.0)

    segs = {"F1": 0.0, "F2": 0.0, "F3": 0.0, "M1": 0.0, "M2": 0.0, "M3": 0.0}
    for si in seg_resultants:
        for _, v in seg_resultants[si].items():
            for k in segs:
                segs[k] += float(v.get(k, 0.0) or 0.0)

    err = {k: orig[k] - segs[k] for k in orig}

    def is_ok(k):
        base = max(abs(orig[k]), 1.0)
        return abs(err[k]) <= max(tol_abs, tol_rel * base)

    ok = all(is_ok(k) for k in err)

    return ok, orig, segs, err

def wall_entity_is_composite(wall_entity, angle_tol_deg=5.0):
    """
    Determina si un wall_entity parece compuesto (L, T, U, C, etc.)
    a partir de la dispersión angular de sus edges, si existen.
    """
    edges = wall_entity.get("edges", [])
    if not edges or len(edges) <= 1:
        return False

    tol = math.radians(angle_tol_deg)
    thetas = [e["theta"] for e in edges if "theta" in e]
    if len(thetas) <= 1:
        return False

    ref = thetas[0]
    for th in thetas[1:]:
        if not _is_colinear(ref, th, tol):
            return True

    return False

def build_shell_group_from_wall_entity(wall_entity, shell_entities):
    """
    Reconstruye un shell_group mínimo a partir de un wall_entity ya detectado.
    """
    shell_map = {sh["shell_id"]: sh for sh in shell_entities}
    shells = [shell_map[sid] for sid in wall_entity.get("shell_ids", []) if sid in shell_map]

    joint_ids = sorted(set(
        jid
        for sh in shells
        for jid in sh.get("joint_ids", [])
    ))

    area_connectivity = {
        sh["shell_id"]: list(sh.get("joint_ids", []))
        for sh in shells
    }

    return {
        "id": wall_entity.get("id", ""),
        "shell_ids": list(wall_entity.get("shell_ids", [])),
        "joint_ids": joint_ids,
        "area_connectivity": area_connectivity,
        "thickness": wall_entity.get("thickness", None),
    }

def segment_wall_entities_if_needed(wall_entities, shell_entities, joints, jloads, angle_tol_deg=5.0):
    """
    Segmenta únicamente muros compuestos.
    Los muros rectos se dejan intactos.

    Retorna:
      segmented_walls: lista final de wall_entities (rectos o segmentados)
      audit: lista de auditoría de segmentación
    """
    segmented_walls = []
    audit = []

    for w in wall_entities:
        shell_group = build_shell_group_from_wall_entity(w, shell_entities)

        base_joints, edges = build_base_edges_from_shell_group(shell_group, joints)
        if not edges:
            segmented_walls.append(dict(w))
            audit.append({
                "wall_id": w.get("id", ""),
                "segmented": False,
                "reason": "sin_edges_base",
                "n_segments": 1,
            })
            continue

        # Guardar edges temporalmente para diagnóstico
        w_tmp = dict(w)
        w_tmp["edges"] = edges

        if not wall_entity_is_composite(w_tmp, angle_tol_deg=angle_tol_deg):
            segmented_walls.append(dict(w))
            audit.append({
                "wall_id": w.get("id", ""),
                "segmented": False,
                "reason": "recto",
                "n_segments": 1,
            })
            continue

        thickness = w.get("thickness", shell_group.get("thickness", None))
        if thickness is None:
            thickness = 0.20

        edge_groups = group_colinear_edges(edges, tol_angle_deg=angle_tol_deg)
        segments = build_segments_from_edge_groups(edge_groups, joints, thickness)
        joint_to_segments = map_joint_to_segments(segments)
        seg_resultants = build_segment_resultants(segments, joint_to_segments, jloads, joints)
        ok, orig, segs, err = check_family_equilibrium(base_joints, jloads, segments, seg_resultants)

        # Convertir cada segmento a un wall_entity compatible
        for i, seg in enumerate(segments, start=1):
            wid = f"{w.get('id', 'W')}_S{i}"
            length_est = seg["length"]

            orientation = "DIAGONAL"
            if abs(seg["ux"]) > 0.98:
                orientation = "X"
            elif abs(seg["uy"]) > 0.98:
                orientation = "Y"

            # resultantes del segmento -> estructura compatible con build_wall_resultants
            segment_cases = seg_resultants.get(i - 1, {})

            segmented_walls.append({
                "id": wid,
                "entity_type": "wall",
                "parent_wall_id": w.get("id", ""),
                "shell_ids": list(w.get("shell_ids", [])),
                "joint_ids": list(seg.get("base_joints", [])),
                "base_joints": list(seg.get("base_joints", [])),
                "top_joints": [],
                "section_names": list(w.get("section_names", [])),
                "x_min": round(min(seg["x0"], seg["x1"]), 4),
                "x_max": round(max(seg["x0"], seg["x1"]), 4),
                "y_min": round(min(seg["y0"], seg["y1"]), 4),
                "y_max": round(max(seg["y0"], seg["y1"]), 4),
                "z_min": w.get("z_min", 0.0),
                "z_max": w.get("z_max", 0.0),
                "height": w.get("height", 0.0),
                "length_est": round(length_est, 4),
                "orientation": orientation,
                "thickness": thickness,
                "active_basis": w.get("active_basis", ""),
                "active_base_joints": list(seg.get("base_joints", [])),
                "segment_resultants_by_case": segment_cases,
                "segment_axis": {
                    "x0": round(seg["x0"], 4),
                    "y0": round(seg["y0"], 4),
                    "x1": round(seg["x1"], 4),
                    "y1": round(seg["y1"], 4),
                    "ux": round(seg["ux"], 6),
                    "uy": round(seg["uy"], 6),
                },
            })

        audit.append({
            "wall_id": w.get("id", ""),
            "segmented": True,
            "reason": "compuesto",
            "n_segments": len(segments),
            "equilibrium_ok": ok,
            "equilibrium_error": err,
        })

    return segmented_walls, audit

def _normalize_joint_id(v):
    """
    Normaliza IDs de joints para evitar diferencias tipo:
      1   vs 1.0   vs '1'
    """
    s = str(v).strip()
    try:
        f = float(s)
        if abs(f - round(f)) < 1e-9:
            return str(int(round(f)))
    except Exception:
        pass
    return s

def normalize_reactions_df(df):
    """
    Normaliza un DataFrame de reacciones para compatibilidad SAP2000/ETABS.
    Renombra columnas de ETABS al formato esperado por build_jloads_from_sap_reactions_excel().
    """
    rename_map = {
        'Label': 'Joint',
        'Output Case': 'OutputCase',
        'Step Type': 'StepType',
        'FX': 'F1', 'FY': 'F2', 'FZ': 'F3',
        'MX': 'M1', 'MY': 'M2', 'MZ': 'M3',
    }
    return df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})


def read_model(filepath, params):
    """
    Parsea el modelo y diagnostica la fuente de cimentación.

    Salidas posibles:
      - basis_mode = 'joint_loads'
      - basis_mode = 'support_reactions'
      - basis_mode = 'ask_user'
      - basis_mode = 'invalid_model'

    En Fase 0:
      - Si basis_mode == 'joint_loads', columns queda lista desde cargas nodales.
      - Si basis_mode == 'support_reactions', columns queda lista desde restricciones.
      - Si basis_mode == 'ask_user', por defecto se devuelve columns desde joint_loads
        si existen, solo para no romper la UI actual, pero la app idealmente debe
        pedir al usuario la fuente antes de continuar.
      - Si basis_mode == 'invalid_model', columns queda vacía.
    """
    file_format = detect_file_format(filepath)
    if file_format == 'etabs':
        tables = parse_e2k(filepath)
    else:
        tables = parse_s2k(filepath)
    joints = get_joints(tables)
    fc_t, fs_t = get_frames(tables)
    sdims = get_section_dims(tables)
    jloads = get_jloads(tables)
    lp = get_lpats(tables)
    if file_format == 'etabs':
        # Para ETABS usamos auto_classify_patterns (detecta por DesignType, no por nombre de patrón)
        cl = classify_from_user(auto_classify_patterns(lp))
    else:
        cl = classify(lp)
    la = get_frame_local_axes(tables)
    area_conn = get_area_connectivity(tables)
    area_secs = get_area_sections(tables)
    area_thicknesses = get_area_section_thicknesses(tables)

    shell_entities = build_shell_entities(
        joints,
        area_conn,
        area_secs,
        area_thicknesses
    )

    candidate_wall_shells = filter_wall_candidate_shells(shell_entities)
    shell_groups = group_connected_shells(candidate_wall_shells)

    candidate_wall_shells = filter_wall_candidate_shells(shell_entities)
    shell_groups = group_connected_shells(candidate_wall_shells)
    wall_entities = build_wall_entities_from_shell_groups(
        shell_groups,
        candidate_wall_shells,
        joints,
    )

    # ── Diagnóstico nuevo Fase 0 ──
    diag = inspect_foundation_sources(
        tables,
        joints=joints,
        fc=fc_t,
        fs=fs_t,
        sd=sdims,
        la=la,
    )

    usable_wall_joint = filter_wall_entities_by_joint_loads(
        wall_entities,
        diag.get("raw_joint_loads", {}),
    )
    usable_wall_rest = filter_wall_entities_by_restraints(
        wall_entities,
        diag.get("raw_restraints", {}),
        require_vertical=True,
    )

    diag["wall_entities_all"] = wall_entities
    diag["usable_wall_joint_candidates"] = usable_wall_joint
    diag["usable_wall_restraint_candidates"] = usable_wall_rest
    diag["wall_joint_count"] = len(usable_wall_joint)
    diag["wall_restraint_count"] = len(usable_wall_rest)

    # ── Segmentación controlada de muros compuestos (solo joint_loads por ahora) ──
    usable_wall_joint_segmented_list, wall_segment_audit = segment_wall_entities_if_needed(
        list(usable_wall_joint.values()),
        shell_entities,
        joints,
        jloads,
    )

    usable_wall_joint = {w["id"]: w for w in usable_wall_joint_segmented_list}
    diag["usable_wall_joint_candidates"] = usable_wall_joint
    diag["wall_joint_count"] = len(usable_wall_joint)
    diag["wall_segment_audit"] = wall_segment_audit

    wall_resultants_joint = build_wall_resultants(
        list(usable_wall_joint.values()),
        jloads,
        joints,
    )

    diag["wall_resultants_joint"] = wall_resultants_joint 

    wall_foundation_entities_joint = build_wall_foundation_entities(
        list(usable_wall_joint.values()),
        wall_resultants_joint,
        joints,
        default_support_width=params.get("dim_min", 0.60),
    )
    diag["wall_foundation_entities_joint"] = wall_foundation_entities_joint

    wall_as_column_entities_joint = build_wall_as_column_entities(
        list(usable_wall_joint.values()),
        wall_resultants_joint,
        joints,
    )
    diag["wall_as_column_entities_joint"] = wall_as_column_entities_joint

    wall_as_column_jloads_joint = build_wall_as_column_jloads(
        wall_as_column_entities_joint
    )
    diag["wall_as_column_jloads_joint"] = wall_as_column_jloads_joint

    # columnas reales actuales según joint loads
    joint_supports = _select_supports_by_basis(diag, "joint_loads")
    joint_foundation_entities, _ = _supports_to_foundation_entities(joint_supports)
    joint_foundation_entities = _remove_point_entities_absorbed_by_walls(
        joint_foundation_entities,
        usable_wall_joint,
    )
    joint_columns = _foundation_entities_to_columns(joint_foundation_entities)

    design_entities_joint = merge_design_entities(
        joint_columns,
        wall_as_column_entities_joint,
    )
    design_jloads_joint = merge_design_jloads(
        joint_columns,
        wall_as_column_entities_joint,
        jloads,
        wall_as_column_jloads_joint,
    )

    diag["design_entities_joint"] = design_entities_joint
    diag["design_jloads_joint"] = design_jloads_joint

    basis_mode = diag.get("basis_mode", "invalid_model")
    basis_options = diag.get("basis_options", [])
    status_message = diag.get("status_message", "")

    # ── Selección automática de supports ──
    # Nota:
    # En ask_user, para no romper el flujo actual, dejamos una base provisional.
    # Recomiendo que app.py luego llame resolve_basis_selection() según la elección.
    warnings_selected = []

    if basis_mode == "joint_loads":
        supports_selected = _select_supports_by_basis(diag, "joint_loads")
        active_wall_entities = _select_walls_by_basis(diag, "joint_loads")
        active_wall_resultants = diag.get("wall_resultants_joint", {})
        active_wall_foundation_entities = diag.get("wall_foundation_entities_joint", [])
        active_design_entities = diag.get("design_entities_joint", [])
        active_design_jloads = diag.get("design_jloads_joint", {})

    elif basis_mode == "support_reactions":
        supports_selected = _select_supports_by_basis(diag, "support_reactions")
        active_wall_entities = _select_walls_by_basis(diag, "support_reactions")
        active_wall_resultants = {}
        active_wall_foundation_entities = []
        active_design_entities = []
        active_design_jloads = {}
        warnings_selected.append(
            "Modelo detectado con apoyos/restricciones base. "
            "En fases posteriores se requerirá Excel de reacciones para el diseño."
        )

    elif basis_mode == "ask_user":
        supports_selected = _select_supports_by_basis(diag, "joint_loads")
        active_wall_entities = _select_walls_by_basis(diag, "joint_loads")
        active_wall_resultants = diag.get("wall_resultants_joint", {})
        active_wall_foundation_entities = diag.get("wall_foundation_entities_joint", [])
        active_design_entities = diag.get("design_entities_joint", [])
        active_design_jloads = diag.get("design_jloads_joint", {})
        warnings_selected.append(
            "Se detectaron dos fuentes posibles de cimentación: "
            "cargas nodales y apoyos/restricciones base. "
            "La app debería solicitar al usuario cuál desea usar."
        )

    else:
        supports_selected = {}
        active_wall_entities = {}
        active_wall_resultants = {}
        active_wall_foundation_entities = []
        active_design_entities = []
        active_design_jloads = {}
        warnings_selected.append(
            "No se identificaron fuentes representativas para cimentación. "
            "El proceso no debería continuar."
        )

    diag["wall_resultants_active"] = active_wall_resultants
    diag["wall_foundation_entities_active"] = active_wall_foundation_entities
    diag["design_entities_active"] = active_design_entities
    diag["design_jloads_active"] = active_design_jloads

    return _build_model_return(
        tables=tables,
        joints=joints,
        jloads=jloads,
        lp=lp,
        cl=cl,
        supports_selected=supports_selected,
        warnings_selected=warnings_selected,
        basis_mode=basis_mode,
        basis_options=basis_options,
        status_message=status_message,
        diag=diag,
        params=params,
        wall_entities_all=wall_entities,
        active_wall_entities=active_wall_entities,
        shell_entities=shell_entities,
        candidate_wall_shells=candidate_wall_shells,
        shell_groups=shell_groups,
    )


def resolve_basis_selection(model_data, basis_mode, params):
    """
    Resuelve manualmente la fuente A/B cuando el usuario la elige en la UI.
    Re-construye model_data con columns provenientes de la fuente escogida
    y también con los muros activos de esa misma base.

    basis_mode esperado:
      - 'joint_loads'
      - 'support_reactions'
    """
    if basis_mode not in ("joint_loads", "support_reactions"):
        raise ValueError("basis_mode debe ser 'joint_loads' o 'support_reactions'.")

    diag = model_data.get("_diag", {})

    supports_selected = _select_supports_by_basis(diag, basis_mode)
    active_wall_entities = _select_walls_by_basis(diag, basis_mode)

    if basis_mode == "joint_loads":
        diag["wall_resultants_active"] = diag.get("wall_resultants_joint", {})
        diag["wall_foundation_entities_active"] = diag.get("wall_foundation_entities_joint", [])
        diag["design_entities_active"] = diag.get("design_entities_joint", [])
        diag["design_jloads_active"] = diag.get("design_jloads_joint", {})
    else:
        diag["wall_resultants_active"] = {}
        diag["wall_foundation_entities_active"] = []
        diag["design_entities_active"] = []
        diag["design_jloads_active"] = {}

    warnings_selected = []

    if basis_mode == "support_reactions":
        warnings_selected.append(
            "Se seleccionó la base por restricciones/apoyos. "
            "En fases posteriores se requerirá Excel de reacciones para el diseño."
        )

    return _build_model_return(
        tables=model_data["_tables"],
        joints=model_data["_joints"],
        jloads=model_data["_jloads"],
        lp=model_data["_lp"],
        cl=model_data["_cl"],
        supports_selected=supports_selected,
        warnings_selected=warnings_selected,
        basis_mode=basis_mode,
        basis_options=[basis_mode],
        status_message=(
            "Fuente seleccionada manualmente por el usuario: "
            + ("cargas nodales." if basis_mode == "joint_loads" else "apoyos/restricciones base.")
        ),
        diag=diag,
        params=params,
        wall_entities_all=diag.get("wall_entities_all", []),
        active_wall_entities=active_wall_entities,
        shell_entities=model_data.get("_shell_entities", []),
        candidate_wall_shells=model_data.get("_candidate_wall_shells", []),
        shell_groups=model_data.get("_shell_groups", []),
    )

def extract_wall_joint_loads(wall_entity, jloads):
    """
    Extrae las cargas nodales de los active_base_joints del muro.
    """
    out = {}
    for jid in wall_entity.get("active_base_joints", []) or wall_entity.get("base_joints", []):
        jid_s = str(jid)
        if jid_s in jloads and jloads[jid_s]:
            out[jid_s] = jloads[jid_s]
    return out


def consolidate_wall_resultants_from_jloads(wall_entity, jloads, joints):
    """
    Consolida fuerzas y momentos del muro por caso/patrón
    a partir de los nudos base activos.

    Convención:
      P  = -F3   (compresión positiva)
      Vx =  F1
      Vy =  F2
      Mx =  M1
      My =  M2
      Mz =  M3
    """
    wall_jloads = extract_wall_joint_loads(wall_entity, jloads)
    result = {}

    active_joints = wall_entity.get("active_base_joints", []) or wall_entity.get("base_joints", [])

    for jid in active_joints:
        jid_s = str(jid)
        if jid_s not in wall_jloads:
            continue

        x = float(joints[jid_s]["x"])
        y = float(joints[jid_s]["y"])

        for case_name, vec in wall_jloads[jid_s].items():
            if case_name not in result:
                result[case_name] = {
                    "P": 0.0,
                    "Vx": 0.0,
                    "Vy": 0.0,
                    "Mx": 0.0,
                    "My": 0.0,
                    "Mz": 0.0,
                    "_sumPx": 0.0,
                    "_sumPy": 0.0,
                    "_sumP": 0.0,
                    "n_active_joints": 0,
                }

            F1 = float(vec.get("F1", 0.0) or 0.0)
            F2 = float(vec.get("F2", 0.0) or 0.0)
            F3 = float(vec.get("F3", 0.0) or 0.0)
            M1 = float(vec.get("M1", 0.0) or 0.0)
            M2 = float(vec.get("M2", 0.0) or 0.0)
            M3 = float(vec.get("M3", 0.0) or 0.0)

            P_i = -F3

            result[case_name]["P"] += P_i
            result[case_name]["Vx"] += F1
            result[case_name]["Vy"] += F2
            result[case_name]["Mx"] += M1
            result[case_name]["My"] += M2
            result[case_name]["Mz"] += M3
            result[case_name]["_sumPx"] += P_i * x
            result[case_name]["_sumPy"] += P_i * y
            result[case_name]["_sumP"] += P_i
            result[case_name]["n_active_joints"] += 1

    # cerrar xR, yR
    x_geom = None
    y_geom = None
    if active_joints:
        xs = [float(joints[str(j)]["x"]) for j in active_joints if str(j) in joints]
        ys = [float(joints[str(j)]["y"]) for j in active_joints if str(j) in joints]
        if xs and ys:
            x_geom = sum(xs) / len(xs)
            y_geom = sum(ys) / len(ys)

    for case_name, r in result.items():
        if abs(r["_sumP"]) > 1e-9:
            xR = r["_sumPx"] / r["_sumP"]
            yR = r["_sumPy"] / r["_sumP"]
        else:
            xR = x_geom if x_geom is not None else 0.0
            yR = y_geom if y_geom is not None else 0.0

        r["xR"] = round(xR, 4)
        r["yR"] = round(yR, 4)

        # limpiar auxiliares
        del r["_sumPx"]
        del r["_sumPy"]
        del r["_sumP"]

        # redondeo final
        for k in ["P", "Vx", "Vy", "Mx", "My", "Mz"]:
            r[k] = round(r[k], 3)

    return result

def _convert_segment_cases_to_wall_resultants(wall_entity, segment_cases, joints):
    """
    Convierte casos segmentados crudos:
        F1,F2,F3,M1,M2,M3
    a la estructura estándar que espera la UI:
        P,Vx,Vy,Mx,My,Mz,xR,yR,n_active_joints
    """
    out = {}

    # centro geométrico del tramo
    if "segment_axis" in wall_entity:
        ax = wall_entity["segment_axis"]
        xR = 0.5 * (float(ax.get("x0", 0.0)) + float(ax.get("x1", 0.0)))
        yR = 0.5 * (float(ax.get("y0", 0.0)) + float(ax.get("y1", 0.0)))
    else:
        active_joints = wall_entity.get("active_base_joints", []) or wall_entity.get("base_joints", [])
        xs = [float(joints[str(j)]["x"]) for j in active_joints if str(j) in joints]
        ys = [float(joints[str(j)]["y"]) for j in active_joints if str(j) in joints]
        if xs and ys:
            xR = sum(xs) / len(xs)
            yR = sum(ys) / len(ys)
        else:
            xR = 0.0
            yR = 0.0

    n_active = len(wall_entity.get("active_base_joints", []) or wall_entity.get("base_joints", []))

    for case_name, raw in segment_cases.items():
        F1 = float(raw.get("F1", 0.0) or 0.0)
        F2 = float(raw.get("F2", 0.0) or 0.0)
        F3 = float(raw.get("F3", 0.0) or 0.0)
        M1 = float(raw.get("M1", 0.0) or 0.0)
        M2 = float(raw.get("M2", 0.0) or 0.0)
        M3 = float(raw.get("M3", 0.0) or 0.0)

        out[case_name] = {
            "P": round(-F3, 3),   # compresión positiva
            "Vx": round(F1, 3),
            "Vy": round(F2, 3),
            "Mx": round(M1, 3),
            "My": round(M2, 3),
            "Mz": round(M3, 3),
            "xR": round(xR, 4),
            "yR": round(yR, 4),
            "n_active_joints": n_active,
        }

    return out

def build_wall_resultants(wall_entities, jloads, joints):
    """
    Construye resultantes por caso para todos los muros activos.
    Devuelve siempre la estructura estándar:
        P, Vx, Vy, Mx, My, Mz, xR, yR
    """
    out = {}

    for w in wall_entities:
        if "segment_resultants_by_case" in w:
            resultants = _convert_segment_cases_to_wall_resultants(
                w,
                w["segment_resultants_by_case"],
                joints,
            )
        else:
            resultants = consolidate_wall_resultants_from_jloads(w, jloads, joints)

        out[w["id"]] = {
            "id": w.get("id", ""),
            "orientation": w.get("orientation", ""),
            "length_est": w.get("length_est", 0.0),
            "height": w.get("height", 0.0),
            "thickness": w.get("thickness", None),
            "active_base_joints": list(w.get("active_base_joints", [])),
            "resultants_by_case": resultants,
        }

    return out

def merge_design_entities(columns, wall_as_column_entities):
    """
    Une columnas reales + muros equivalentes en una sola lista
    para clasificación, vigas, diseño y exportación.
    """
    out = []

    for c in columns:
        c2 = dict(c)
        c2["entity_type"] = c2.get("entity_type", "point")
        c2["design_family"] = "column"
        out.append(c2)

    for w in wall_as_column_entities:
        w2 = {
            "id": w.get("id", ""),
            "entity_type": "wall_as_column",
            "design_family": "wall",
            "joint": w.get("joint", ""),
            "x": w.get("x", 0.0),
            "y": w.get("y", 0.0),
            "bx": w.get("bx", 0.0),
            "by": w.get("by", 0.0),
            "section": w.get("section", "WALL_EQUIV"),
            "type": w.get("type", "wall_equivalent"),
            "source": w.get("source", ""),
            "wall_id": w.get("wall_id", ""),
            "orientation": w.get("orientation", ""),
            "thickness": w.get("thickness", 0.0),
            "length": w.get("length", 0.0),
        }
        out.append(w2)

    return out

def merge_design_jloads(columns, wall_as_column_entities, base_jloads, wall_as_column_jloads):
    """
    Une cargas de columnas reales + muros equivalentes en una sola
    estructura tipo jloads.
    """
    out = {}

    # columnas reales
    real_joints = {str(c["joint"]) for c in columns}
    for jid, cases in base_jloads.items():
        if str(jid) in real_joints:
            out[str(jid)] = cases

    # muros equivalentes
    for jid, cases in wall_as_column_jloads.items():
        out[str(jid)] = cases

    return out

def build_wall_resultants_from_reactions_df(wall_entities, reactions_df, joints):
    """
    Construye resultantes por muro usando el Excel de reacciones de SAP.
    Reutiliza build_jloads_from_sap_reactions_excel convirtiendo primero
    los joints activos de muros a una estructura tipo jloads.
    """
    # Construir lista mínima de entidades puntuales ficticias a partir de los
    # active_base_joints de todos los muros
    foundation_entities = []
    used = set()

    for w in wall_entities:
        for jid in w.get("active_base_joints", []) or w.get("base_joints", []):
            jid_s = str(jid)
            if jid_s in used:
                continue
            if jid_s not in joints:
                continue

            foundation_entities.append({
                "joint": jid_s,
                "entity_type": "point",
            })
            used.add(jid_s)

    jloads_from_reactions = build_jloads_from_sap_reactions_excel(
        reactions_df,
        foundation_entities
    )

    return build_wall_resultants(wall_entities, jloads_from_reactions, joints)

# ================================================================
# PASOS 3-5: ORQUESTADOR COMPLETO
# ================================================================
def _combo_has_ex(combo_name: str) -> bool:
    u = combo_name.upper()
    return "EX+" in u or "EX-" in u


def _combo_has_ey(combo_name: str) -> bool:
    u = combo_name.upper()
    return "EY+" in u or "EY-" in u


def _signed_max(original_value, user_value):
    """
    Devuelve el valor de mayor magnitud absoluta.
    Conserva el signo del valor que controla.
    Si user_value <= 0, ignora override.
    """
    uv = abs(float(user_value or 0.0))
    ov = float(original_value or 0.0)

    if uv <= 0:
        return ov, "combo"

    if abs(ov) >= uv:
        return ov, "combo"

    sign = 1.0 if ov >= 0 else -1.0
    if abs(ov) < 1e-12:
        sign = 1.0
    return sign * uv, "user"

def build_design_jloads_with_seismic_overrides(jloads, classifications, R):
    """
    Construye una copia de las cargas nodales para DISEÑO y EXPORTACIÓN,
    ajustando los patrones sísmicos base con Mp/Vu del usuario.

    Regla:
      - El usuario ingresa valores objetivo a nivel Ex/Ey ya reducidos.
      - Para llevarlos al patrón base Sx/Sy, se multiplican por R.
      - Luego compute_forces() los dividirá por R al formar Ex/Ey.

    Convención:
      - SX: dirección sísmica X
          * cortante principal = F1
          * momento principal = M2
      - SY: dirección sísmica Y
          * cortante principal = F2
          * momento principal = M1

    Comparación:
      - Cortante: sqrt(F1^2 + F2^2) vs Vu_user * R
      - Momento: sqrt(M1^2 + M2^2) vs M_user * R

    Si controla el usuario:
      - se alinea la acción con el eje principal
      - se conserva el signo del componente principal original
      - el componente ortogonal se hace cero
    """
    out = {}

    for jid, pats in jloads.items():
        ci = classifications.get(jid, {})

        # Valores objetivo del usuario a nivel Ex/Ey
        mpx = float(ci.get("mpx", 0.0) or 0.0)   # Para SY -> M1
        mpy = float(ci.get("mpy", 0.0) or 0.0)   # Para SX -> M2
        vux = float(ci.get("vux", 0.0) or 0.0)   # Para SX -> F1
        vuy = float(ci.get("vuy", 0.0) or 0.0)   # Para SY -> F2

        # Llevar a patrón base
        mpx_base = mpx * R
        mpy_base = mpy * R
        vux_base = vux * R
        vuy_base = vuy * R

        out[jid] = {}

        for pat, ld in pats.items():
            new_ld = dict(ld)
            up = str(pat).upper()

            f1 = float(new_ld.get("F1", 0.0))
            f2 = float(new_ld.get("F2", 0.0))
            m1 = float(new_ld.get("M1", 0.0))
            m2 = float(new_ld.get("M2", 0.0))

            v_res = math.sqrt(f1**2 + f2**2)
            m_res = math.sqrt(m1**2 + m2**2)

            # ── Sismo X: eje principal X => F1 y M2 ──
            if up.startswith("SX") and "DERIV" not in up and "UD" not in up:
                # Cortante
                if vux_base > 0 and v_res <= abs(vux_base):
                    sign_f1 = -1.0 if f1 < 0 else 1.0
                    if abs(f1) < 1e-12:
                        sign_f1 = 1.0
                    new_ld["F1"] = sign_f1 * abs(vux_base)
                    new_ld["F2"] = 0.0

                # Momento
                if mpy_base > 0 and m_res <= abs(mpy_base):
                    sign_m2 = -1.0 if m2 < 0 else 1.0
                    if abs(m2) < 1e-12:
                        sign_m2 = 1.0
                    new_ld["M2"] = sign_m2 * abs(mpy_base)
                    new_ld["M1"] = 0.0

            # ── Sismo Y: eje principal Y => F2 y M1 ──
            elif up.startswith("SY") and "DERIV" not in up and "UD" not in up:
                # Cortante
                if vuy_base > 0 and v_res <= abs(vuy_base):
                    sign_f2 = -1.0 if f2 < 0 else 1.0
                    if abs(f2) < 1e-12:
                        sign_f2 = 1.0
                    new_ld["F2"] = sign_f2 * abs(vuy_base)
                    new_ld["F1"] = 0.0

                # Momento
                if mpx_base > 0 and m_res <= abs(mpx_base):
                    sign_m1 = -1.0 if m1 < 0 else 1.0
                    if abs(m1) < 1e-12:
                        sign_m1 = 1.0
                    new_ld["M1"] = sign_m1 * abs(mpx_base)
                    new_ld["M2"] = 0.0

            out[jid][pat] = new_ld

    return out

def run_design(model_data, classifications, params, user_ties=None, user_dims=None):
    """
    Ejecuta PASOS 3-5 con las clasificaciones del usuario.

    Args:
        model_data: resultado de read_model()
        classifications: dict { joint_id: { location, side, corner, ecc_x, ecc_y } }
        params: parámetros de diseño
        user_ties: dict opcional — reemplaza deducción automática de vigas
        user_dims: dict opcional — { footing_id: {'B': float, 'L': float, 'h': float} }
                   Si se provee, salta optimización y usa dimensiones del usuario (modo verificación).

    Nota Fase 0:
        Este run_design sigue operando con fuerzas provenientes de _jloads.
        Si basis_mode == 'support_reactions', todavía no está diseñado para trabajar
        con Excel de reacciones. Esa parte vendrá en una fase posterior.
    """
    R = params["R"]
    cl = model_data["_cl"]
    combos = gen_combos(cl, R=R, ortho=True)

    # ── Entidades unificadas de diseño ──
    entities = model_data.get("design_entities", model_data.get("columns", []))

    # ── Cargas base unificadas ──
    entity_joints = {str(e["joint"]) for e in entities}

    if (
        model_data.get("basis_mode") == "support_reactions"
        and model_data.get("reactions_df") is not None
    ):
        # Base completa desde Excel para entidades puntuales reales
        jloads_full = build_jloads_from_sap_reactions_excel(
            model_data["reactions_df"],
            model_data.get("foundation_entities", [])
        )

        # Si existen muros equivalentes dentro de design_jloads, sobreescribirlos encima
        design_jloads_existing = model_data.get("design_jloads", {}) or {}
        jloads_raw = {str(j): cases for j, cases in jloads_full.items()}

        for jid, cases in design_jloads_existing.items():
            if str(jid) in entity_joints and str(jid) not in jloads_raw:
                jloads_raw[str(jid)] = cases

        for jid, cases in design_jloads_existing.items():
            if str(jid) in entity_joints and jid not in {str(c["joint"]) for c in model_data.get("columns", [])}:
                jloads_raw[str(jid)] = cases

    elif model_data.get("design_jloads"):
        jloads_raw = model_data["design_jloads"]

    else:
        jloads_raw = model_data["_jloads"]

    # ── Aplicar rename canónico (D, SD, Sx, Sy …) si fue configurado ─────────
    _rename_map = model_data.get("_rename_map", {})
    if _rename_map:
        from export_utils import apply_rename_to_jloads as _apply_rn
        jloads_raw = _apply_rn(jloads_raw, _rename_map)

    jloads_design = build_design_jloads_with_seismic_overrides(
        jloads=jloads_raw,
        classifications=classifications,
        R=R
    )

    ads_all = compute_forces(jloads_design, combos["ADS"])
    lrfd_all = compute_forces(jloads_design, combos["LRFD"])

    dmin = params.get("dim_min", 0.60)
    columns = entities
    verify_mode = user_dims is not None and len(user_dims) > 0

    export_jloads = jloads_design


    # ── PASO 2: Vigas de enlace ──
    if user_ties is not None:
        ties = user_ties
    else:
        ties = deduce_tie_beams(columns, classifications)

    # ── PASO 3: Dimensionar cada zapata ──
    footings = []
    idx = 1
    margin_borde = 0.05

    for col in columns:
        jid = col["joint"]
        cl_info = classifications.get(jid, {"location": "concentrica"})
        ads_f = ads_all.get(jid, {})
        lrfd_f = lrfd_all.get(jid, {})
        fid = f"Z-{idx:02d}"

        # Verificación: usar dimensiones del usuario si están disponibles
        if verify_mode and fid in user_dims:
            ud = user_dims[fid]
            B = ud["B"]
            L = ud["L"]
            h = ud["h"]
            cx, cy, bx, by = col["x"], col["y"], col["bx"], col["by"]
            loc = cl_info.get("location", "concentrica")
            side = cl_info.get("side", "")
            corner = cl_info.get("corner", "")

            # Calcular posición de zapata según clasificación + dimensiones del usuario
            zx, zy = cx, cy
            if loc == "medianera":
                if side == "X+":
                    zx = cx + bx / 2 + margin_borde - B / 2
                elif side == "X-":
                    zx = cx - bx / 2 - margin_borde + B / 2
                elif side == "Y+":
                    zy = cy + by / 2 + margin_borde - L / 2
                elif side == "Y-":
                    zy = cy - by / 2 - margin_borde + L / 2
            elif loc == "esquinera":
                if "X+" in corner:
                    zx = cx + bx / 2 + margin_borde - B / 2
                elif "X-" in corner:
                    zx = cx - bx / 2 - margin_borde + B / 2
                if "Y+" in corner:
                    zy = cy + by / 2 + margin_borde - L / 2
                elif "Y-" in corner:
                    zy = cy - by / 2 - margin_borde + L / 2

            zx = round(zx, 3)
            zy = round(zy, 3)

            opt = {
                "B": B,
                "L": L,
                "h": h,
                "x_footing": zx,
                "y_footing": zy,
                "x_col": cx,
                "y_col": cy,
                "vol": round(B * L * h, 3),
                "e_geo_x": round(abs(cx - zx), 4),
                "e_geo_y": round(abs(cy - zy), 4),
                "e_geo": round(math.sqrt((cx - zx) ** 2 + (cy - zy) ** 2), 4),
            }
        else:
            opt = optimize_isolated(col, ads_f, lrfd_f, params, cl_info)

        cf = {
            pat: {k: round(v, 3) for k, v in ld.items()}
            for pat, ld in jloads_design.get(jid, {}).items()
        }

        # x / y = centro REAL de la zapata (desplazado para medianera/esquinera).
        # x_col / y_col = posición de la columna en el modelo.
        # rect se construye desde el centro de la zapata para que check_overlaps sea correcto.
        _zx = opt["x_footing"]
        _zy = opt["y_footing"]
        f = {
            "id": f"Z-{idx:02d}",
            "type": "isolated",
            "joint": jid,
            "x": _zx,
            "y": _zy,
            "x_col": opt["x_col"],
            "y_col": opt["y_col"],
            "x_footing": _zx,
            "y_footing": _zy,
            "e_geo_x": opt.get("e_geo_x", 0),
            "e_geo_y": opt.get("e_geo_y", 0),
            "B": opt["B"],
            "L": opt["L"],
            "h": opt["h"],
            "A": round(opt["B"] * opt["L"], 2),
            "vol": opt["vol"],
            "rect": [
                _zx - opt["B"] / 2,
                _zy - opt["L"] / 2,
                _zx + opt["B"] / 2,
                _zy + opt["L"] / 2,
            ],
            "cols": [col],
            "col_bx": col["bx"],
            "col_by": col["by"],
            "classification": cl_info,
            "ties": ties.get(jid, {}),
            "column_forces": {jid: cf},

            # ── marcar si proviene de muro equivalente ──
            "design_family": col.get("design_family", "column"),
            "is_wall_equivalent": col.get("design_family", "column") == "wall",
            "source_joint": col.get("joint", ""),
        }
        footings.append(f)
        idx += 1

    # ── PASO 4: Detectar solapamientos → combinadas ──
    # ── Las zapatas provenientes de muros NO entran a combinadas ──
    footings_nonwall = [f for f in footings if not f.get("is_wall_equivalent", False)]
    footings_wall = [f for f in footings if f.get("is_wall_equivalent", False)]

    # min_gap=0.02: sólo agrupa zapatas que se solapan realmente (< 1cm borde a borde).
    # Antes era 0.10, lo que agrupaba zapatas a < 5cm de distancia (falsos positivos
    # para medianera/esquinera desplazadas respecto a concéntricas vecinas).
    ovs, groups_nonwall = check_overlaps(footings_nonwall, min_gap=0.02)

    # Los muros siempre se tratan como aisladas independientes
    groups_wall = [[i] for i in range(len(footings_wall))]

    # Reconstruir grupos sobre la lista completa "all_footings"
    all_footings = footings_nonwall + footings_wall

    groups = []

    # Grupos no-wall
    for grp in groups_nonwall:
        groups.append(grp)

    # Grupos wall como aisladas individuales, con índice corrido
    offset = len(footings_nonwall)
    for grp in groups_wall:
        groups.append([offset + grp[0]])

    footings = all_footings

    final = []
    cidx = 1
    for grp in groups:
        if len(grp) == 1:
            # ── Zapata aislada → diseño estructural completo ──
            f = footings[grp[0]]
            # Pasar siempre el CENTRO DE LA ZAPATA (no el de la columna)
            _fx = f.get("x_footing", f["x"])
            _fy = f.get("y_footing", f["y"])
            r = full_structural_design(
                f["joint"], _fx, _fy, f["B"], f["L"], f["h"],
                f["col_bx"], f["col_by"],
                ads_all.get(f["joint"], {}), lrfd_all.get(f["joint"], {}),
                params, f["column_forces"]
            )
            r["id"] = f["id"]
            r["type"] = "isolated"
            r["cols"] = f["cols"]
            r["classification"] = f["classification"]
            r["ties"] = f["ties"]
            r["vol"] = f["vol"]
            r["location"] = f["classification"]
            r["scheme"] = f.get("ties", {}).get("scheme_suggested", "aislada")
            r["x_col"] = f.get("x_col", f["x"])
            r["y_col"] = f.get("y_col", f["y"])
            r["x_footing"] = _fx
            r["y_footing"] = _fy
            r["e_geo_x"] = f.get("e_geo_x", round(abs(f.get("x_col", f["x"]) - _fx), 4))
            r["e_geo_y"] = f.get("e_geo_y", round(abs(f.get("y_col", f["y"]) - _fy), 4))
            # rect: bounding box desde centro real de zapata (para plots y export)
            r["rect"] = [_fx - f["B"]/2, _fy - f["L"]/2, _fx + f["B"]/2, _fy + f["L"]/2]
            final.append(r)
        else:
            # ── Zapata combinada → módulo combined ──
            group_cols = []
            for g in grp:
                for c in footings[g].get("cols", []):
                    group_cols.append(c)

            r = design_combined_footing(
                grp, footings, group_cols, ads_all, lrfd_all, params, cidx
            )
            final.append(r)
            cidx += 1

    # ── Análisis de sistemas enlazados con convergencia ──
    tie_systems_raw = build_tie_systems(final, ties)
    max_convergence_iter = 4
    convergence_tol = 0.05  # m

    for conv_iter in range(max_convergence_iter):
        tie_systems = []
        for sys in tie_systems_raw:
            result = analyze_tie_system(sys, final, jloads_design, combos, params)
            tie_systems.append(result)

        apply_system_reactions_to_footings(final, tie_systems, ads_all, lrfd_all, params)

        # Check if any footing needs resize
        needs_resize = [
            f for f in final
            if f.get("needs_resize", False) and f.get("type") != "combined"
        ]
        if not needs_resize:
            break  # Converged

        # Re-optimize footings that need resize
        any_changed = False
        for f in needs_resize:
            jid = f["joint"]
            col = next((c for c in columns if c["joint"] == jid), None)
            if not col:
                continue

            cl_info = classifications.get(jid, {"location": "concentrica"})

            # Add system dP to ADS forces for re-optimization
            ads_f_aug = {}
            for cn, fv in ads_all.get(jid, {}).items():
                dP = f.get("system_dP_by_combo", {}).get(cn, 0)
                ads_f_aug[cn] = {**fv, "P": fv["P"] - abs(dP)}

            lrfd_f_aug = {}
            for cn, fv in lrfd_all.get(jid, {}).items():
                dP = f.get("system_dP_lrfd", {}).get(cn, 0)
                lrfd_f_aug[cn] = {**fv, "P": fv["P"] - abs(dP)}

            opt = optimize_isolated(col, ads_f_aug, lrfd_f_aug, params, cl_info)

            # Check convergence
            dB = abs(opt["B"] - f["B"])
            dL = abs(opt["L"] - f["L"])
            dh = abs(opt["h"] - f["h"])

            if dB > convergence_tol or dL > convergence_tol or dh > convergence_tol:
                any_changed = True

                cf = f.get("column_forces", {})
                r = full_structural_design(
                    jid,
                    opt["x_footing"], opt["y_footing"], opt["B"], opt["L"], opt["h"],
                    col["bx"], col["by"], ads_f_aug, lrfd_all.get(jid, {}), params, cf
                )

                # Preserve metadata
                r["id"] = f["id"]
                r["type"] = "isolated"
                r["cols"] = f["cols"]
                r["classification"] = f["classification"]
                r["ties"] = f["ties"]
                r["vol"] = opt["vol"]
                r["location"] = f["classification"]
                r["scheme"] = f.get("scheme", "aislada")
                r["x_col"] = opt.get("x_col", f.get("x_col"))
                r["y_col"] = opt.get("y_col", f.get("y_col"))
                r["x_footing"] = opt["x_footing"]
                r["y_footing"] = opt["y_footing"]
                r["e_geo_x"] = opt.get("e_geo_x", 0)
                r["e_geo_y"] = opt.get("e_geo_y", 0)
                r["rect"] = [
                    opt["x_footing"] - opt["B"] / 2,
                    opt["y_footing"] - opt["L"] / 2,
                    opt["x_footing"] + opt["B"] / 2,
                    opt["y_footing"] + opt["L"] / 2,
                ]
                r["resized_iter"] = conv_iter + 1

                idx_f = next(i for i, ff in enumerate(final) if ff["id"] == f["id"])
                final[idx_f] = r

        if not any_changed:
            break  # Dimensions converged

    convergence_info = {
        "iterations": conv_iter + 1,
        "converged": conv_iter < max_convergence_iter - 1,
        "resized_footings": [f["id"] for f in final if f.get("resized_iter", 0) > 0],
    }

    ta = sum(f["A"] for f in final)
    tv = sum(f["A"] * f["h"] for f in final)

    return {
        "final_footings": final,
        "overlaps": ovs,
        "total_area": round(ta, 1),
        "total_volume": round(tv, 2),
        "n_combos_ads": len(combos["ADS"]),
        "n_combos_lrfd": len(combos["LRFD"]),
        "ties": ties,
        "tie_systems": tie_systems,
        "verify_mode": verify_mode,
        "convergence": convergence_info,
        "export_jloads": export_jloads,
    }