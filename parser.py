"""
parser.py — Parser de archivos .$2k + clasificación de cargas + combinaciones NSR-10
FASE 0: detección de fuentes de cimentación

Funciones principales:
  parse_s2k                     — Lee archivo .$2k y extrae tablas
  get_joints                    — Coordenadas de nudos
  get_frames                    — Conectividad de frames
  get_section_dims              — Dimensiones de secciones
  get_jloads                    — Cargas en nudos
  get_lpats                     — Patrones de carga
  get_frame_local_axes          — Ángulos de ejes locales
  get_joint_restraints          — Restricciones de nudos (detección genérica)
  section_to_global             — Proyección de sección a ejes globales

  identify_supports             — (compatibilidad) apoyos desde nudos con carga
  identify_supports_from_joint_loads
  identify_supports_from_restraints
  inspect_foundation_sources    — Diagnóstico Fase 0 (A / B / ambas / ninguna)

  classify                      — Clasificación de patrones de carga
  gen_combos                    — Generación de combinaciones ADS + LRFD
  compute_forces                — Evaluación de fuerzas por combinación
"""

import re
import math
from collections import defaultdict, deque


# ================================================================
# HELPERS
# ================================================================
def _to_bool(v):
    """Convierte textos/números típicos de SAP a booleano."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return abs(float(v)) > 1e-12
    s = str(v).strip().upper()
    return s in {"YES", "TRUE", "T", "1", "ON", "FIXED", "ASSIGNED"}


def _find_tables_by_contains(T, substrings):
    """
    Retorna lista de nombres de tablas cuyo nombre contiene
    cualquiera de los fragmentos dados (case-insensitive).
    """
    out = []
    subs = [s.upper() for s in substrings]
    for name in T.keys():
        uname = name.upper()
        if any(s in uname for s in subs):
            out.append(name)
    return out


def _member_dims_for_joint(jid, joints, fc, fs, sd, la, prefer="below"):
    """
    Busca el primer elemento frame conectado al nudo y devuelve
    sección/proyección global, con preferencia por elemento hacia abajo
    o hacia arriba según 'prefer'.
    """
    if jid not in joints:
        return None

    jz = joints[jid]["z"]
    preferred = []
    fallback = []

    for fid, conn in fc.items():
        ji, jj = conn["ji"], conn["jj"]
        if ji != jid and jj != jid:
            continue

        other = jj if ji == jid else ji
        if other not in joints:
            continue

        oz = joints[other]["z"]
        sn = fs.get(fid, "")
        dims = sd.get(sn, {"t3": 0.30, "t2": 0.30})
        angle = la.get(fid, 0.0)
        bx, by = section_to_global(dims["t2"], dims["t3"], angle)

        info = {
            "frame": fid,
            "section": sn,
            "bx": bx,
            "by": by,
            "other_joint": other,
            "other_z": oz,
        }

        if prefer == "below" and oz < jz - 0.01:
            preferred.append(info)
        elif prefer == "above" and oz > jz + 0.01:
            preferred.append(info)
        else:
            fallback.append(info)

    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return None


def _build_support_record(jid, joints, fc, fs, sd, la=None, source="unknown"):
    """
    Construye el registro geométrico de un candidato de cimentación
    para un nudo dado, independientemente de si proviene de cargas
    nodales o de restricciones.
    """
    if la is None:
        la = {}

    if jid not in joints:
        return None

    x = joints[jid]["x"]
    y = joints[jid]["y"]
    z = joints[jid]["z"]

    below = _member_dims_for_joint(jid, joints, fc, fs, sd, la, prefer="below")
    if below:
        return {
            "type": "pedestal",
            "source": source,
            "section": below["section"],
            "bx": below["bx"],
            "by": below["by"],
            "x": x,
            "y": y,
            "z": z,
            "warning": None,
        }

    above = _member_dims_for_joint(jid, joints, fc, fs, sd, la, prefer="above")
    if above:
        return {
            "type": "above_only",
            "source": source,
            "section": above["section"],
            "bx": above["bx"],
            "by": above["by"],
            "x": x,
            "y": y,
            "z": z,
            "warning": f"J{jid}: Sin pedestal explícito. Se propone sección {above['section'] or 'genérica'}.",
        }

    return {
        "type": "orphan",
        "source": source,
        "section": None,
        "bx": 0.30,
        "by": 0.30,
        "x": x,
        "y": y,
        "z": z,
        "warning": f"J{jid}: Nodo huérfano, sin elemento estructural asociado.",
    }


# ================================================================
# PARSER $2K
# ================================================================
def parse_s2k(fp):
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    T = {}
    current_table = None
    current_lines = []

    for line in content.split("\n"):
        line = line.strip().replace("\r", "")
        if line.startswith("TABLE:"):
            if current_table:
                T[current_table] = current_lines
            try:
                current_table = line.split('"')[1]
            except Exception:
                current_table = line.replace("TABLE:", "").strip()
            current_lines = []
        elif line and current_table:
            # Manejo de líneas continuadas con "_"
            if current_lines and current_lines[-1].endswith("_"):
                current_lines[-1] = current_lines[-1][:-1] + " " + line
            else:
                current_lines.append(line)

    if current_table:
        T[current_table] = current_lines

    return T


def pkv(line):
    """
    Parse key=value tolerante.
    """
    result = {}
    for m in re.finditer(r'(\w+)=("[^"]*"|[^\s]+)', line):
        k, v = m.group(1), m.group(2).strip('"')
        try:
            vf = float(v)
            if vf == int(vf) and "." not in m.group(2):
                v = int(vf)
            else:
                v = vf
        except Exception:
            pass
        result[k] = v
    return result


def get_joints(T):
    J = {}
    for l in T.get("JOINT COORDINATES", []):
        d = pkv(l)
        if "Joint" in d:
            J[str(d["Joint"])] = {
                "x": float(d.get("XorR", 0)),
                "y": float(d.get("Y", 0)),
                "z": float(d.get("Z", 0)),
            }
    return J


def get_frames(T):
    C, S = {}, {}

    for l in T.get("CONNECTIVITY - FRAME", []):
        d = pkv(l)
        if "Frame" in d:
            C[str(d["Frame"])] = {
                "ji": str(d.get("JointI", "")),
                "jj": str(d.get("JointJ", "")),
            }

    for l in T.get("FRAME SECTION ASSIGNMENTS", []):
        d = pkv(l)
        if "Frame" in d:
            S[str(d["Frame"])] = str(d.get("AnalSect", ""))

    return C, S

def get_area_connectivity(T):
    """
    Lee conectividad de áreas/shells desde el modelo SAP.

    Retorna:
        {
            area_id: {
                "joints": [j1, j2, j3, j4, ...]
            }
        }
    """
    area_conn = {}

    candidate_tables = _find_tables_by_contains(T, [
        "CONNECTIVITY - AREA",
        "AREA CONNECTIVITY",
    ])

    for tname in candidate_tables:
        for line in T.get(tname, []):
            row = pkv(line)

            area_id = (
                row.get("Area")
                or row.get("AreaObj")
                or row.get("Object")
                or row.get("UniqueName")
                or row.get("Shell")
            )
            if area_id is None:
                continue
            area_id = str(area_id)

            joints = []
            for k in ["Joint1", "Joint2", "Joint3", "Joint4", "Joint5", "Joint6", "Joint7", "Joint8"]:
                if k in row and row[k] not in (None, "", 0):
                    joints.append(str(row[k]))

            # fallback por si SAP trae J1/J2/J3/J4
            if not joints:
                for k in ["J1", "J2", "J3", "J4", "J5", "J6", "J7", "J8"]:
                    if k in row and row[k] not in (None, "", 0):
                        joints.append(str(row[k]))

            if len(joints) >= 3:
                area_conn[area_id] = {"joints": joints}

    return area_conn

def get_area_sections(T):
    """
    Lee asignaciones de sección para áreas/shells.

    Retorna:
        {
            area_id: "SECTION_NAME"
        }
    """
    area_secs = {}

    candidate_tables = _find_tables_by_contains(T, [
        "AREA SECTION ASSIGNMENTS",
        "AREA ASSIGNS - SECTION",
        "SHELL SECTION ASSIGNMENTS",
    ])

    for tname in candidate_tables:
        for line in T.get(tname, []):
            row = pkv(line)

            area_id = (
                row.get("Area")
                or row.get("AreaObj")
                or row.get("Object")
                or row.get("UniqueName")
                or row.get("Shell")
            )
            if area_id is None:
                continue

            sec = (
                row.get("Section")
                or row.get("Prop")
                or row.get("SectionName")
                or row.get("Property")
            )

            area_secs[str(area_id)] = str(sec) if sec is not None else ""

    return area_secs

def build_shell_entities(joints, area_conn, area_secs, area_thicknesses=None):
    """
    Construye entidades shell unificadas a partir de conectividad + joints.

    Retorna lista de shells:
        [
            {
                "shell_id": "A1",
                "section": "WALL20",
                "joint_ids": ["10","11","15","14"],
                "coords": [(x,y,z), ...]
            },
            ...
        ]
    """
    shells = []

    for area_id, rec in area_conn.items():
        joint_ids = [str(j) for j in rec.get("joints", []) if str(j) in joints]
        if len(joint_ids) < 3:
            continue

        coords = []
        for jid in joint_ids:
            p = joints[jid]
            coords.append((float(p["x"]), float(p["y"]), float(p["z"])))

        sec_name = area_secs.get(str(area_id), "")
        thickness = None
        if area_thicknesses:
            thickness = area_thicknesses.get(sec_name)

        shells.append({
            "shell_id": str(area_id),
            "section": sec_name,
            "thickness": thickness,
            "joint_ids": joint_ids,
            "coords": coords,
        })

    return shells

def classify_shell_orientation(shell, tol_horizontal=0.90, tol_vertical=0.10):
    """
    Clasifica orientación geométrica del shell:
      - horizontal
      - vertical
      - inclined
      - degenerate

    Criterio:
      nz ~ 1  => horizontal (plano XY)
      nz ~ 0  => vertical
      intermedio => inclined
    """
    pts = shell.get("coords", [])
    if len(pts) < 3:
        return {"orientation": "degenerate", "normal": (0.0, 0.0, 0.0)}

    p1 = pts[0]
    found = False
    for i in range(1, len(pts) - 1):
        p2 = pts[i]
        p3 = pts[i + 1]

        v1 = (p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2])
        v2 = (p3[0] - p1[0], p3[1] - p1[1], p3[2] - p1[2])

        nx = v1[1] * v2[2] - v1[2] * v2[1]
        ny = v1[2] * v2[0] - v1[0] * v2[2]
        nz = v1[0] * v2[1] - v1[1] * v2[0]

        norm = math.sqrt(nx * nx + ny * ny + nz * nz)
        if norm > 1e-9:
            found = True
            break

    if not found:
        return {"orientation": "degenerate", "normal": (0.0, 0.0, 0.0)}

    nx /= norm
    ny /= norm
    nz /= norm

    abs_nz = abs(nz)

    if abs_nz >= tol_horizontal:
        orientation = "horizontal"
    elif abs_nz <= tol_vertical:
        orientation = "vertical"
    else:
        orientation = "inclined"

    return {
        "orientation": orientation,
        "normal": (round(nx, 6), round(ny, 6), round(nz, 6)),
    }

def filter_wall_candidate_shells(shell_entities):
    """
    Conserva solo shells verticales o inclinados.
    Ignora shells horizontales (losas) y degenerados.
    """
    out = []

    for sh in shell_entities:
        info = classify_shell_orientation(sh)
        sh2 = dict(sh)
        sh2.update(info)

        if info["orientation"] in {"vertical", "inclined"}:
            out.append(sh2)

    return out

def _shell_edges(shell):
    """
    Retorna aristas normalizadas del shell como pares de joints.
    """
    joints = shell.get("joint_ids", [])
    if len(joints) < 2:
        return []

    edges = []
    n = len(joints)
    for i in range(n):
        a = str(joints[i])
        b = str(joints[(i + 1) % n])
        if a == b:
            continue
        edges.append(tuple(sorted((a, b))))
    return edges

def group_connected_shells(shell_entities):
    """
    Agrupa shells conectados por arista compartida.
    Retorna lista de grupos, donde cada grupo es una lista de shell_ids.
    """
    shell_map = {sh["shell_id"]: sh for sh in shell_entities}
    edge_to_shells = defaultdict(list)

    for sh in shell_entities:
        for e in _shell_edges(sh):
            edge_to_shells[e].append(sh["shell_id"])

    adj = defaultdict(set)
    for edge, sids in edge_to_shells.items():
        if len(sids) < 2:
            continue
        for i in range(len(sids)):
            for j in range(i + 1, len(sids)):
                a, b = sids[i], sids[j]
                adj[a].add(b)
                adj[b].add(a)

    visited = set()
    groups = []

    for sid in shell_map:
        if sid in visited:
            continue

        q = deque([sid])
        visited.add(sid)
        group = []

        while q:
            cur = q.popleft()
            group.append(cur)

            for nb in adj[cur]:
                if nb not in visited:
                    visited.add(nb)
                    q.append(nb)

        groups.append(sorted(group))

    return groups

def get_area_section_thicknesses(T):
    """
    Lee espesores numéricos de secciones de áreas/shells.

    Retorna:
        {
            "WALL20": 0.20,
            "MURO15": 0.15,
        }
    """
    out = {}

    candidate_tables = _find_tables_by_contains(T, [
        "AREA SECTION PROPERTIES",
        "SHELL PROPERTY DEFINITIONS",
        "AREA PROPERTIES",
    ])

    for tname in candidate_tables:
        for line in T.get(tname, []):
            row = pkv(line)

            sname = (
                row.get("Section")
                or row.get("SectionName")
                or row.get("Prop")
                or row.get("Property")
                or row.get("Name")
            )
            if sname is None:
                continue

            th = (
                row.get("Thickness")
                or row.get("Thick")
                or row.get("MembraneThickness")
                or row.get("WallThickness")
            )

            if th is None:
                continue

            try:
                out[str(sname)] = float(th)
            except Exception:
                pass

    return out

def build_wall_entities_from_shell_groups(shell_groups, shell_entities, joints, tol_z=1e-3):
    """
    Construye wall_entities a partir de grupos de shells conectados.
    """
    shell_map = {sh["shell_id"]: sh for sh in shell_entities}
    walls = []

    for i, group in enumerate(shell_groups, start=1):
        shells = [shell_map[sid] for sid in group if sid in shell_map]
        if not shells:
            continue

        joint_ids = sorted(set(
            jid
            for sh in shells
            for jid in sh.get("joint_ids", [])
            if jid in joints
        ))
        if len(joint_ids) < 2:
            continue

        xs = [float(joints[j]["x"]) for j in joint_ids]
        ys = [float(joints[j]["y"]) for j in joint_ids]
        zs = [float(joints[j]["z"]) for j in joint_ids]

        z_min = min(zs)
        z_max = max(zs)

        base_joints = [j for j in joint_ids if abs(float(joints[j]["z"]) - z_min) <= tol_z]
        top_joints  = [j for j in joint_ids if abs(float(joints[j]["z"]) - z_max) <= tol_z]

        dx = max(xs) - min(xs)
        dy = max(ys) - min(ys)

        if dx > 5 * dy:
            orientation = "X"
            length_est = dx
        elif dy > 5 * dx:
            orientation = "Y"
            length_est = dy
        elif dx > 1e-6 and dy > 1e-6:
            orientation = "DIAGONAL"
            length_est = math.sqrt(dx * dx + dy * dy)
        else:
            orientation = "COMPLEX"
            length_est = max(dx, dy)

        thicknesses = [
            float(sh["thickness"])
            for sh in shells
            if sh.get("thickness") is not None
        ]

        wall_thickness = None
        if thicknesses:
            # si todos son iguales, usa ese; si no, toma el máximo por seguridad
            wall_thickness = max(thicknesses)

        walls.append({
            "id": f"W{i:03d}",
            "entity_type": "wall",
            "shell_ids": [sh["shell_id"] for sh in shells],
            "joint_ids": joint_ids,
            "base_joints": sorted(base_joints),
            "top_joints": sorted(top_joints),
            "section_names": sorted(set(sh.get("section", "") for sh in shells if sh.get("section", ""))),
            "x_min": round(min(xs), 4),
            "x_max": round(max(xs), 4),
            "y_min": round(min(ys), 4),
            "y_max": round(max(ys), 4),
            "z_min": round(z_min, 4),
            "z_max": round(z_max, 4),
            "height": round(z_max - z_min, 4),
            "length_est": round(length_est, 4),
            "orientation": orientation,
            "thickness": round(wall_thickness, 4) if wall_thickness is not None else None,
        })

    return walls

def filter_wall_entities_by_joint_loads(wall_entities, jloads):
    """
    Marca muros utilizables si alguno de sus base_joints tiene cargas nodales.
    """
    out = {}

    for w in wall_entities:
        base = [str(j) for j in w.get("base_joints", [])]
        active = [j for j in base if str(j) in jloads and jloads.get(str(j))]
        if active:
            w2 = dict(w)
            w2["active_basis"] = "joint_loads"
            w2["active_base_joints"] = active
            out[w["id"]] = w2

    return out

def filter_wall_entities_by_restraints(wall_entities, restraints, require_vertical=True):
    """
    Marca muros utilizables si alguno de sus base_joints está restringido.
    """
    out = {}

    for w in wall_entities:
        active = []
        for j in w.get("base_joints", []):
            r = restraints.get(str(j), {})
            if not r:
                continue
            if require_vertical and not r.get("U3", False):
                continue
            active.append(str(j))

        if active:
            w2 = dict(w)
            w2["active_basis"] = "support_reactions"
            w2["active_base_joints"] = active
            out[w["id"]] = w2

    return out

def get_section_dims(T):
    D = {}
    for l in T.get("FRAME SECTION PROPERTIES 01 - GENERAL", []):
        d = pkv(l)
        if "SectionName" in d and "t3" in d:
            D[str(d["SectionName"])] = {
                "t3": float(d["t3"]),
                "t2": float(d["t2"]),
            }
    return D


def get_jloads(T):
    """
    Cargas en nudos.
    Retorna:
      { joint_id: { load_pattern: {F1,F2,F3,M1,M2,M3} } }
    """
    L = defaultdict(lambda: defaultdict(dict))
    for l in T.get("JOINT LOADS - FORCE", []):
        d = pkv(l)
        if "Joint" in d and "LoadPat" in d:
            L[str(d["Joint"])][str(d["LoadPat"])] = {
                k: float(d.get(k, 0.0)) for k in ["F1", "F2", "F3", "M1", "M2", "M3"]
            }
    return dict(L)


def get_lpats(T):
    P = {}
    for l in T.get("LOAD PATTERN DEFINITIONS", []):
        d = pkv(l)
        if "LoadPat" in d:
            P[str(d["LoadPat"])] = str(d.get("DesignType", ""))
    return P


def get_frame_local_axes(T):
    A = {}
    for l in T.get("FRAME LOCAL AXES ASSIGNMENTS 1 - TYPICAL", []):
        d = pkv(l)
        if "Frame" in d and "Angle" in d:
            A[str(d["Frame"])] = float(d["Angle"])
    return A


def get_joint_restraints(T):
    """
    Detección genérica de restricciones de nudos.
    Busca tablas cuyo nombre contenga 'JOINT RESTRAINT' o 'JOINT SPRING'.

    Retorna:
      {
        joint_id: {
          'U1': bool, 'U2': bool, 'U3': bool,
          'R1': bool, 'R2': bool, 'R3': bool,
          'source_table': str
        }
      }

    Nota:
    - Para Fase 0 solo interesa saber si el nudo está restringido de forma
      utilizable como apoyo/cimentación.
    - Si el archivo usa otro nombre de tabla, habrá que ajustarlo con el modelo real.
    """
    restraints = {}

    candidate_tables = _find_tables_by_contains(
        T,
        ["JOINT RESTRAINT", "JOINT SPRING"]
    )

    for tname in candidate_tables:
        for l in T.get(tname, []):
            d = pkv(l)
            if "Joint" not in d:
                continue

            jid = str(d["Joint"])

            # Diferentes exportaciones pueden usar U1/U2/U3 o UX/UY/UZ, etc.
            u1 = _to_bool(d.get("U1", d.get("UX", False)))
            u2 = _to_bool(d.get("U2", d.get("UY", False)))
            u3 = _to_bool(d.get("U3", d.get("UZ", False)))
            r1 = _to_bool(d.get("R1", d.get("RX", False)))
            r2 = _to_bool(d.get("R2", d.get("RY", False)))
            r3 = _to_bool(d.get("R3", d.get("RZ", False)))

            # En algunas tablas de springs puede haber rigideces no booleanas:
            # si existe valor no nulo, se considera restricción efectiva para Fase 0.
            spring_keys = ["K1", "K2", "K3", "KR1", "KR2", "KR3", "KUX", "KUY", "KUZ"]
            if not any([u1, u2, u3, r1, r2, r3]):
                for sk in spring_keys:
                    if sk in d and _to_bool(d.get(sk)):
                        if sk in ["K1", "KUX"]:
                            u1 = True
                        elif sk in ["K2", "KUY"]:
                            u2 = True
                        elif sk in ["K3", "KUZ"]:
                            u3 = True
                        elif sk == "KR1":
                            r1 = True
                        elif sk == "KR2":
                            r2 = True
                        elif sk == "KR3":
                            r3 = True

            if any([u1, u2, u3, r1, r2, r3]):
                restraints[jid] = {
                    "U1": u1, "U2": u2, "U3": u3,
                    "R1": r1, "R2": r2, "R3": r3,
                    "source_table": tname,
                }

    return restraints


def section_to_global(t2, t3, angle_deg):
    a = math.radians(angle_deg)
    return (
        round(abs(t3 * math.cos(a)) + abs(t2 * math.sin(a)), 4),
        round(abs(t3 * math.sin(a)) + abs(t2 * math.cos(a)), 4),
    )


# ================================================================
# IDENTIFICACIÓN DE APOYOS / CANDIDATOS
# ================================================================
def identify_supports_from_joint_loads(joints, fc, fs, sd, jl, la=None):
    """
    Identifica candidatos de cimentación a partir de nudos con carga.
    Este es el comportamiento original de la app.
    """
    if la is None:
        la = {}

    supports = {}
    for jid in jl:
        rec = _build_support_record(
            jid=jid,
            joints=joints,
            fc=fc,
            fs=fs,
            sd=sd,
            la=la,
            source="joint_loads",
        )
        if rec is not None:
            supports[jid] = rec
    return supports


def identify_supports_from_restraints(joints, fc, fs, sd, restraints, la=None, require_vertical=True):
    """
    Identifica candidatos de cimentación a partir de nudos restringidos.

    Parámetros:
      require_vertical=True:
          si True, solo considera candidatos con U3 restringido
          (más representativo de apoyo/base).
    """
    if la is None:
        la = {}

    supports = {}
    for jid, r in restraints.items():
        if jid not in joints:
            continue

        if require_vertical and not r.get("U3", False):
            # Para Fase 0, si no hay restricción vertical, preferimos no contarlo
            # como cimentación representativa.
            continue

        rec = _build_support_record(
            jid=jid,
            joints=joints,
            fc=fc,
            fs=fs,
            sd=sd,
            la=la,
            source="restraints",
        )
        if rec is None:
            continue

        rec["restraints"] = dict(r)
        supports[jid] = rec

    return supports


def identify_supports(joints, fc, fs, sd, jl, la=None):
    """
    Compatibilidad con el motor actual.
    Mantiene el comportamiento original: detectar apoyos
    únicamente desde nudos con carga.
    """
    return identify_supports_from_joint_loads(joints, fc, fs, sd, jl, la=la)


def inspect_foundation_sources(T, joints=None, fc=None, fs=None, sd=None, la=None):
    """
    Diagnóstico Fase 0 del modelo.

    Retorna:
      {
        'has_joint_loads': bool,
        'has_restraints': bool,
        'joint_candidates': {...},
        'restraint_candidates': {...},
        'basis_mode': 'joint_loads' | 'support_reactions' | 'ask_user' | 'invalid_model',
        'basis_options': [...],
        'status_message': str,
        'joint_load_count': int,
        'restraint_count': int,
      }
    """
    if joints is None:
        joints = get_joints(T)
    if fc is None or fs is None:
        fc, fs = get_frames(T)
    if sd is None:
        sd = get_section_dims(T)
    if la is None:
        la = get_frame_local_axes(T)

    jloads = get_jloads(T)
    restraints = get_joint_restraints(T)

    joint_candidates = identify_supports_from_joint_loads(joints, fc, fs, sd, jloads, la=la)
    restraint_candidates = identify_supports_from_restraints(joints, fc, fs, sd, restraints, la=la)

    # Contar solo candidatos “utilizables”; los orphan no ayudan realmente
    usable_joint = {
        jid: v for jid, v in joint_candidates.items()
        if v.get("type") in {"pedestal", "above_only"}
    }
    usable_rest = {
        jid: v for jid, v in restraint_candidates.items()
        if v.get("type") in {"pedestal", "above_only"}
    }

    has_joint_loads = len(usable_joint) > 0
    has_restraints = len(usable_rest) > 0

    if has_joint_loads and not has_restraints:
        basis_mode = "joint_loads"
        basis_options = ["joint_loads"]
        status_message = "Modelo con cargas nodales utilizables y sin apoyos/restricciones base utilizables."
    elif has_restraints and not has_joint_loads:
        basis_mode = "support_reactions"
        basis_options = ["support_reactions"]
        status_message = "Modelo con apoyos/restricciones base utilizables y sin cargas nodales utilizables."
    elif has_joint_loads and has_restraints:
        basis_mode = "ask_user"
        basis_options = ["joint_loads", "support_reactions"]
        status_message = "Modelo con ambas fuentes posibles: cargas nodales y apoyos/restricciones base."
    else:
        basis_mode = "invalid_model"
        basis_options = []
        status_message = "No se identificaron ni cargas nodales ni apoyos/restricciones base representativos para cimentación."

    return {
        "has_joint_loads": has_joint_loads,
        "has_restraints": has_restraints,
        "joint_candidates": joint_candidates,
        "restraint_candidates": restraint_candidates,
        "usable_joint_candidates": usable_joint,
        "usable_restraint_candidates": usable_rest,
        "basis_mode": basis_mode,
        "basis_options": basis_options,
        "status_message": status_message,
        "joint_load_count": len(usable_joint),
        "restraint_count": len(usable_rest),
        "raw_joint_loads": jloads,
        "raw_restraints": restraints,
    }


# ================================================================
# CLASIFICACIÓN + COMBINACIONES NSR-10
# ================================================================
def classify(lp):
    c = {
        "dead": [],
        "superdead": [],
        "live": [],
        "live_roof": [],
        "live_eq": [],
        "seismic_x": [],
        "seismic_y": [],
        "wind_xp": [],
        "wind_xn": [],
        "wind_yp": [],
        "wind_yn": [],
        "other": [],
    }

    for p in lp:
        u = p.upper()
        if p == "D":
            c["dead"].append(p)
        elif p == "SD":
            c["superdead"].append(p)
        elif p == "L":
            c["live"].append(p)
        elif p == "Lr":
            c["live_roof"].append(p)
        elif p == "Le":
            c["live_eq"].append(p)
        elif u.startswith("SX") and "DERIV" not in u and "UD" not in u:
            c["seismic_x"].append(p)
        elif u.startswith("SY") and "DERIV" not in u and "UD" not in u:
            c["seismic_y"].append(p)
        elif p == "Wx+":
            c["wind_xp"].append(p)
        elif p == "Wx-":
            c["wind_xn"].append(p)
        elif p == "Wy+":
            c["wind_yp"].append(p)
        elif p == "Wy-":
            c["wind_yn"].append(p)
        else:
            c["other"].append(p)

    return c


def gen_combos(cl, R=7.0, ortho=True):
    h = {k: bool(v) for k, v in cl.items()}

    def fac(ks, f):
        keys = ks if isinstance(ks, list) else [ks]
        return {p: f for k in keys for p in cl.get(k, [])}

    def D(f): return {**fac("dead", f), **fac("superdead", f)}
    def L(f): return fac("live", f)
    def Lr(f): return fac("live_roof", f)
    def Le(f): return fac("live_eq", f)
    def Ex(f): return fac("seismic_x", f / R)
    def Ey(f): return fac("seismic_y", f / R)
    def Wxp(f): return fac("wind_xp", f)
    def Wxn(f): return fac("wind_xn", f)
    def Wyp(f): return fac("wind_yp", f)
    def Wyn(f): return fac("wind_yn", f)

    def mg(*ds):
        r = {}
        for d in ds:
            r.update(d)
        return r

    ads, lrfd = [], []

    # ADS
    ads.append({"name": "ADS-01: D", "factors": D(1), "group": "q1"})
    if h["live"]:
        ads.append({"name": "ADS-02: D+L", "factors": mg(D(1), L(1)), "group": "q1"})
    if h["live_roof"]:
        ads.append({"name": "ADS-03: D+Lr", "factors": mg(D(1), Lr(1)), "group": "q2"})
    if h["live_eq"]:
        ads.append({"name": "ADS-04: D+Le", "factors": mg(D(1), Le(1)), "group": "q2"})
    if h["live"] and h["live_roof"]:
        ads.append({"name": "ADS-05: D+0.75L+0.75Lr", "factors": mg(D(1), L(0.75), Lr(0.75)), "group": "q2"})
    if h["live"] and h["live_eq"]:
        ads.append({"name": "ADS-06: D+0.75L+0.75Le", "factors": mg(D(1), L(0.75), Le(0.75)), "group": "q2"})

    for s, sn in [(1, "Ex+"), (-1, "Ex-")]:
        if not h["seismic_x"]:
            continue
        ef = lambda f, s=s: Ex(f * s)
        ads.append({"name": f"ADS-S: D+0.7{sn}", "factors": mg(D(1), ef(0.7)), "group": "q3"})
        if h["live"]:
            ads.append({"name": f"ADS-S: D+0.75L+0.525{sn}", "factors": mg(D(1), L(0.75), ef(0.525)), "group": "q3"})
        ads.append({"name": f"ADS-S: 0.6D+0.7{sn}", "factors": mg(D(0.6), ef(0.7)), "group": "q3"})

    for s, sn in [(1, "Ey+"), (-1, "Ey-")]:
        if not h["seismic_y"]:
            continue
        ef = lambda f, s=s: Ey(f * s)
        ads.append({"name": f"ADS-S: D+0.7{sn}", "factors": mg(D(1), ef(0.7)), "group": "q3"})
        if h["live"]:
            ads.append({"name": f"ADS-S: D+0.75L+0.525{sn}", "factors": mg(D(1), L(0.75), ef(0.525)), "group": "q3"})
        ads.append({"name": f"ADS-S: 0.6D+0.7{sn}", "factors": mg(D(0.6), ef(0.7)), "group": "q3"})

    if ortho and h["seismic_x"] and h["seismic_y"]:
        for sx, sxn in [(1, "Ex+"), (-1, "Ex-")]:
            for sy, syn in [(1, "Ey+"), (-1, "Ey-")]:
                exf = lambda f, s=sx: Ex(f * s)
                eyf = lambda f, s=sy: Ey(f * s)
                ads.append({"name": f"ADS-O: D+0.7(1.0{sxn}+0.3{syn})", "factors": mg(D(1), exf(0.7), eyf(0.21)), "group": "q3"})
                ads.append({"name": f"ADS-O: D+0.7(0.3{sxn}+1.0{syn})", "factors": mg(D(1), exf(0.21), eyf(0.7)), "group": "q3"})
                if h["live"]:
                    ads.append({"name": f"ADS-O: D+0.75L+0.525(1.0{sxn}+0.3{syn})", "factors": mg(D(1), L(0.75), exf(0.525), eyf(0.1575)), "group": "q3"})
                    ads.append({"name": f"ADS-O: D+0.75L+0.525(0.3{sxn}+1.0{syn})", "factors": mg(D(1), L(0.75), exf(0.1575), eyf(0.525)), "group": "q3"})
                ads.append({"name": f"ADS-O: 0.6D+0.7(1.0{sxn}+0.3{syn})", "factors": mg(D(0.6), exf(0.7), eyf(0.21)), "group": "q3"})
                ads.append({"name": f"ADS-O: 0.6D+0.7(0.3{sxn}+1.0{syn})", "factors": mg(D(0.6), exf(0.21), eyf(0.7)), "group": "q3"})

    for wn, wf in [("Wx+", Wxp), ("Wx-", Wxn), ("Wy+", Wyp), ("Wy-", Wyn)]:
        ck = {"Wx+": "wind_xp", "Wx-": "wind_xn", "Wy+": "wind_yp", "Wy-": "wind_yn"}[wn]
        if not h[ck]:
            continue
        ads.append({"name": f"ADS-W: D+{wn}", "factors": mg(D(1), wf(1)), "group": "q2"})
        if h["live"]:
            ads.append({"name": f"ADS-W: D+0.75L+0.75{wn}", "factors": mg(D(1), L(0.75), wf(0.75)), "group": "q2"})
        ads.append({"name": f"ADS-W: 0.6D+{wn}", "factors": mg(D(0.6), wf(1)), "group": "q2"})

    # LRFD
    lrfd.append({"name": "LRFD-01: 1.4D", "factors": D(1.4)})

    if h["live"]:
        lrfd.append({"name": "LRFD-02: 1.2D+1.6L", "factors": mg(D(1.2), L(1.6))})
        if h["live_roof"]:
            lrfd.append({"name": "LRFD-02a: 1.2D+1.6L+0.5Lr", "factors": mg(D(1.2), L(1.6), Lr(0.5))})
        if h["live_eq"]:
            lrfd.append({"name": "LRFD-02b: 1.2D+1.6L+0.5Le", "factors": mg(D(1.2), L(1.6), Le(0.5))})

    if h["live_roof"]:
        b = mg(D(1.2), Lr(1.6))
        if h["live"]:
            b = mg(b, L(1))
        lrfd.append({"name": "LRFD-03a: 1.2D+1.6Lr+1.0L", "factors": b})

    if h["live_eq"]:
        b = mg(D(1.2), Le(1.6))
        if h["live"]:
            b = mg(b, L(1))
        lrfd.append({"name": "LRFD-03b: 1.2D+1.6Le+1.0L", "factors": b})

    for wn, wf in [("Wx+", Wxp), ("Wx-", Wxn), ("Wy+", Wyp), ("Wy-", Wyn)]:
        ck = {"Wx+": "wind_xp", "Wx-": "wind_xn", "Wy+": "wind_yp", "Wy-": "wind_yn"}[wn]
        if not h[ck]:
            continue
        b = mg(D(1.2), wf(1.6))
        if h["live"]:
            b = mg(b, L(1))
        lrfd.append({"name": f"LRFD-04: 1.2D+1.6{wn}+1.0L", "factors": b})

    for s, en in [(1, "Ex+"), (-1, "Ex-")]:
        if not h["seismic_x"]:
            continue
        ef = lambda f, s=s: Ex(f * s)
        b = mg(D(1.2), ef(1))
        if h["live"]:
            b = mg(b, L(1))
        lrfd.append({"name": f"LRFD-05: 1.2D+1.0{en}+1.0L", "factors": b})

    for s, en in [(1, "Ey+"), (-1, "Ey-")]:
        if not h["seismic_y"]:
            continue
        ef = lambda f, s=s: Ey(f * s)
        b = mg(D(1.2), ef(1))
        if h["live"]:
            b = mg(b, L(1))
        lrfd.append({"name": f"LRFD-05: 1.2D+1.0{en}+1.0L", "factors": b})

    if ortho and h["seismic_x"] and h["seismic_y"]:
        for sx, sxn in [(1, "Ex+"), (-1, "Ex-")]:
            for sy, syn in [(1, "Ey+"), (-1, "Ey-")]:
                exf = lambda f, s=sx: Ex(f * s)
                eyf = lambda f, s=sy: Ey(f * s)
                lrfd.append({
                    "name": f"LRFD-O: 1.2D+1.0{sxn}+0.3{syn}+1.0L",
                    "factors": mg(D(1.2), exf(1), eyf(0.3), L(1) if h["live"] else {})
                })
                lrfd.append({
                    "name": f"LRFD-O: 1.2D+0.3{sxn}+1.0{syn}+1.0L",
                    "factors": mg(D(1.2), exf(0.3), eyf(1), L(1) if h["live"] else {})
                })

    for wn, wf in [("Wx+", Wxp), ("Wx-", Wxn), ("Wy+", Wyp), ("Wy-", Wyn)]:
        ck = {"Wx+": "wind_xp", "Wx-": "wind_xn", "Wy+": "wind_yp", "Wy-": "wind_yn"}[wn]
        if not h[ck]:
            continue
        lrfd.append({"name": f"LRFD-06: 0.9D+1.6{wn}", "factors": mg(D(0.9), wf(1.6))})

    for s, en in [(1, "Ex+"), (-1, "Ex-")]:
        if not h["seismic_x"]:
            continue
        ef = lambda f, s=s: Ex(f * s)
        lrfd.append({"name": f"LRFD-07: 0.9D+1.0{en}", "factors": mg(D(0.9), ef(1))})

    for s, en in [(1, "Ey+"), (-1, "Ey-")]:
        if not h["seismic_y"]:
            continue
        ef = lambda f, s=s: Ey(f * s)
        lrfd.append({"name": f"LRFD-07: 0.9D+1.0{en}", "factors": mg(D(0.9), ef(1))})

    return {"ADS": ads, "LRFD": lrfd}


def compute_forces(jloads, combos):
    """
    Evalúa fuerzas combinadas por nudo.
    """
    R = {}
    for c in combos:
        cn = c["name"]
        facs = c["factors"]

        for jid, pats in jloads.items():
            if jid not in R:
                R[jid] = {}

            P = Mx = My = Vx = Vy = 0.0
            for pat, factor in facs.items():
                if pat in pats:
                    ld = pats[pat]
                    P += factor * ld["F3"]
                    Vx += factor * ld["F1"]
                    Vy += factor * ld["F2"]
                    Mx += factor * ld["M1"]
                    My += factor * ld["M2"]

            R[jid][cn] = {
                "P": P,
                "Mx": Mx,
                "My": My,
                "Vx": Vx,
                "Vy": Vy,
                "group": c.get("group", ""),
            }

    return R


# ================================================================
# DETECTOR DE FORMATO + PARSER ETABS .e2k / .$et
# ================================================================

def detect_file_format(fp):
    """
    Detecta si el archivo es formato ETABS (.e2k / .$et) o SAP2000 (.$2k).
    Retorna: 'etabs' | 'sap2000' | 'unknown'
    """
    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
        header = f.read(3000)
    if 'PROGRAM "ETABS"' in header or 'PROGRAM  "ETABS"' in header:
        return 'etabs'
    if 'TABLE:' in header:
        return 'sap2000'
    return 'unknown'


def parse_e2k(fp):
    """
    Parsea archivo ETABS .e2k / .$et y retorna dict T compatible con get_*().

    El formato ETABS 2D usa secciones $ HEADER y coordenadas de planta (X, Y).
    La coordenada Z se deduce del sistema de pisos (STORIES).
    Solo se crean entidades a nivel de base (piso N+0.0).
    """
    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # ── Parsear secciones por encabezados $ ──
    secs = {}
    cur = None
    for line in content.split('\n'):
        ls = line.strip()
        if not ls:
            continue
        if ls.startswith('$'):
            cur = ls[1:].strip()
            secs.setdefault(cur, [])
        elif cur is not None:
            secs[cur].append(ls)

    # ── Unidades: factor de longitud a metros ──
    len_fac = 1.0
    for ln in secs.get('CONTROLS', []):
        m = re.match(r'UNITS\s+"[^"]*"\s+"([^"]+)"', ln, re.I)
        if m:
            if m.group(1).upper() == 'MM':
                len_fac = 1e-3
            break

    # ── Pisos: obtener altura del primer piso sobre base ──
    stories_raw = []
    for ln in secs.get('STORIES - IN SEQUENCE FROM TOP', []):
        m_n = re.search(r'STORY\s+"([^"]+)"', ln)
        if not m_n:
            continue
        sname = m_n.group(1)
        m_e = re.search(r'\bELEV\b\s+([\d.eE+-]+)', ln)
        m_h = re.search(r'\bHEIGHT\b\s+([\d.eE+-]+)', ln)
        elev = float(m_e.group(1)) * len_fac if m_e else None
        height = float(m_h.group(1)) * len_fac if m_h else None
        stories_raw.append({'name': sname, 'elev': elev, 'height': height})

    # Piso base: tiene ELEV ~ 0
    base_story_name = 'N+0.0'
    for s in stories_raw:
        if s['elev'] is not None and abs(s['elev']) < 1e-6:
            base_story_name = s['name']
            break

    # Altura del primer piso sobre base (para joints sintéticos)
    h_top = 3.0
    base_idx = next((i for i, s in enumerate(stories_raw) if s['name'] == base_story_name), -1)
    if base_idx > 0:
        above = stories_raw[base_idx - 1]
        if above['height'] is not None:
            h_top = above['height']
        elif above['elev'] is not None:
            h_top = above['elev']
    elif stories_raw:
        for s in stories_raw:
            if s.get('height') is not None:
                h_top = s['height']
                break

    # ── Coordenadas de planta ──
    plan_pts = {}  # id -> (x, y)
    for ln in secs.get('POINT COORDINATES', []):
        m = re.match(r'POINT\s+"([^"]+)"\s+([\d.eE+-]+)\s+([\d.eE+-]+)', ln)
        if m:
            plan_pts[m.group(1)] = (
                float(m.group(2)) * len_fac,
                float(m.group(3)) * len_fac,
            )

    # ── Nudos con restricción en el piso base ──
    base_joints = set()
    for ln in secs.get('POINT ASSIGNS', []):
        m = re.match(r'POINTASSIGN\s+"([^"]+)"\s+"([^"]+)"\s+RESTRAINT', ln, re.I)
        if m and m.group(2) == base_story_name:
            base_joints.add(m.group(1))

    # ── Conectividad de columnas (LINE … COLUMN) ──
    col_plan_pt = {}  # col_id -> plan_point_id
    for ln in secs.get('LINE CONNECTIVITIES', []):
        m = re.match(r'LINE\s+"([^"]+)"\s+COLUMN\s+"([^"]+)"', ln, re.I)
        if m:
            col_plan_pt[m.group(1)] = m.group(2)

    # ── Sección asignada a cada columna (LINE ASSIGNS) ──
    col_section = {}  # col_id -> section_name (primer piso encima de base)
    for ln in secs.get('LINE ASSIGNS', []):
        m = re.match(r'LINEASSIGN\s+"([^"]+)"\s+"([^"]+)"\s+SECTION\s+"([^"]+)"', ln, re.I)
        if m:
            lid, story, sec = m.group(1), m.group(2), m.group(3)
            if lid not in col_section and story != base_story_name:
                col_section[lid] = sec

    # ── Dimensiones de secciones de frame (FRAME SECTIONS) ──
    fsec_dims = {}  # section_name -> {t3, t2}
    for ln in secs.get('FRAME SECTIONS', []):
        m_n = re.search(r'FRAMESECTION\s+"([^"]+)"', ln, re.I)
        m_d = re.search(r'\bD\b\s+([\d.eE+-]+)', ln)
        m_b = re.search(r'\bB\b\s+([\d.eE+-]+)', ln)
        if m_n and m_d and m_b:
            fsec_dims[m_n.group(1)] = {
                't3': float(m_d.group(1)) * len_fac,
                't2': float(m_b.group(1)) * len_fac,
            }

    # ── Conectividad de áreas (muros) ──
    area_joints = {}  # area_id -> [j1, j2] (plan points únicos)
    for ln in secs.get('AREA CONNECTIVITIES', []):
        m_id = re.match(r'AREA\s+"([^"]+)"', ln, re.I)
        if not m_id:
            continue
        aid = m_id.group(1)
        pts = re.findall(r'"([^"]+)"', ln)
        # pts[0] = area_id, resto = joint IDs
        if len(pts) > 1:
            seen_jids = []
            for jid in pts[1:]:
                if jid not in seen_jids:
                    seen_jids.append(jid)
            area_joints[aid] = seen_jids

    # ── Sección asignada a cada área (AREA ASSIGNS) ──
    area_section = {}  # area_id -> section_name
    for ln in secs.get('AREA ASSIGNS', []):
        m = re.match(r'AREAASSIGN\s+"([^"]+)"\s+"([^"]+)"\s+SECTION\s+"([^"]+)"', ln, re.I)
        if m:
            aid = m.group(1)
            if aid not in area_section:
                area_section[aid] = m.group(3)

    # ── Espesores de propiedades de muro (SHELLPROP) ──
    wall_thickness = {}  # section_name -> thickness (m)
    for sec_key in ('WALL PROPERTIES', 'SLAB PROPERTIES', 'DECK PROPERTIES'):
        for ln in secs.get(sec_key, []):
            m_n = re.search(r'SHELLPROP\s+"([^"]+)"', ln, re.I)
            m_t = re.search(r'WALLTHICKNESS\s+([\d.eE+-]+)', ln, re.I)
            if m_t is None:
                m_t = re.search(r'SLABTHICKNESS\s+([\d.eE+-]+)', ln, re.I)
            if m_n and m_t:
                wall_thickness.setdefault(m_n.group(1), float(m_t.group(1)) * len_fac)

    # ── Patrones de carga (LOAD PATTERNS) ──
    lpats_raw = {}  # name -> type_str
    for ln in secs.get('LOAD PATTERNS', []):
        m_n = re.search(r'LOADPATTERN\s+"([^"]+)"', ln, re.I)
        m_t = re.search(r'\bTYPE\b\s+"([^"]+)"', ln, re.I)
        if m_n:
            lpats_raw[m_n.group(1)] = m_t.group(1) if m_t else ''

    # ══════════════════════════════════════════════
    # Construir T dict compatible con get_*()
    # ══════════════════════════════════════════════
    T = {}

    # JOINT COORDINATES: base (Z=0) + sintéticos arriba (Z=h_top)
    jc_rows = []
    for jid, (x, y) in plan_pts.items():
        jc_rows.append(f'Joint="{jid}" XorR={x} Y={y} Z=0')
        jc_rows.append(f'Joint="{jid}_t" XorR={x} Y={y} Z={h_top}')
    T['JOINT COORDINATES'] = jc_rows

    # FRAME SECTION PROPERTIES
    fsp_rows = [
        f'SectionName="{n}" t3={d["t3"]} t2={d["t2"]}'
        for n, d in fsec_dims.items()
    ]
    T['FRAME SECTION PROPERTIES 01 - GENERAL'] = fsp_rows

    # CONNECTIVITY - FRAME (solo columnas en base)
    fc_rows, fs_rows = [], []
    for col_id, pt_id in col_plan_pt.items():
        if pt_id not in base_joints:
            continue
        sec = col_section.get(col_id, '')
        fc_rows.append(f'Frame="{col_id}" JointI="{pt_id}" JointJ="{pt_id}_t"')
        if sec:
            fs_rows.append(f'Frame="{col_id}" AnalSect="{sec}"')
    T['CONNECTIVITY - FRAME'] = fc_rows
    T['FRAME SECTION ASSIGNMENTS'] = fs_rows

    # JOINT RESTRAINT ASSIGNMENTS (nudos base)
    T['JOINT RESTRAINT ASSIGNMENTS'] = [
        f'Joint="{jid}" U1=Yes U2=Yes U3=Yes R1=Yes R2=Yes R3=Yes'
        for jid in base_joints if jid in plan_pts
    ]

    # CONNECTIVITY - AREA (muros: base + top sintético)
    ac_rows, as_rows = [], []
    for aid, jpts in area_joints.items():
        sec = area_section.get(aid, '')
        if not sec or sec not in wall_thickness:
            continue
        if len(jpts) < 2:
            continue
        j1, j2 = jpts[0], jpts[1]
        j_str = f'Joint1="{j1}" Joint2="{j2}" Joint3="{j2}_t" Joint4="{j1}_t"'
        ac_rows.append(f'Area="{aid}" {j_str}')
        as_rows.append(f'Area="{aid}" Section="{sec}"')
    T['CONNECTIVITY - AREA'] = ac_rows
    T['AREA SECTION ASSIGNMENTS'] = as_rows

    # AREA SECTION PROPERTIES (espesores de muro)
    T['AREA SECTION PROPERTIES'] = [
        f'Section="{n}" Thickness={th}'
        for n, th in wall_thickness.items()
    ]

    # LOAD PATTERN DEFINITIONS
    T['LOAD PATTERN DEFINITIONS'] = [
        f'LoadPat="{name}" DesignType="{etype}"'
        for name, etype in lpats_raw.items()
    ]

    # Metadatos ETABS internos (no usan get_*() functions)
    T['_etabs_meta'] = {
        'base_story': base_story_name,
        'h_top': h_top,
        'len_fac': len_fac,
        'lpats_raw': lpats_raw,
    }

    return T


# ================================================================
# CLASIFICACIÓN DE PATRONES: AUTO-DETECCIÓN + USUARIO
# ================================================================

def auto_classify_patterns(lpats):
    """
    Clasifica automáticamente los patrones de carga a partir de sus tipos.

    lpats: {name: type_str}   — del parser SAP2000 o ETABS
    Retorna {name: category_str}
    category_str en: 'D', 'SD', 'L', 'Lr', 'Le',
                     'Wx+', 'Wx-', 'Wy+', 'Wy-',
                     'Sx', 'Sy', 'Ignorar'
    """
    result = {}
    for name, type_str in lpats.items():
        u_type = type_str.upper().strip()
        u_name = name.upper()

        if u_type in ('DEAD', 'DEAD LOAD'):
            cat = 'D'
        elif u_type in ('SUPER DEAD', 'SUPERDEAD', 'SDL', 'SUPERIMPOSED DEAD'):
            cat = 'SD'
        elif u_type in ('LIVE', 'LIVE LOAD', 'LIVE REDUCIBLE',
                        'LIVE UNREDUCIBLE', 'LIVE STORAGE'):
            cat = 'L'
        elif u_type in ('ROOF LIVE', 'ROOF LIVE LOAD'):
            cat = 'Lr'
        elif u_type in ('LIVE PONDING',):
            cat = 'Le'
        elif u_type in ('SEISMIC (DRIFT)', 'SEISMIC DRIFT', 'WIND (DRIFT)'):
            cat = 'Ignorar'
        elif 'SEISMIC' in u_type or 'QUAKE' in u_type:
            # Detectar dirección desde el nombre del patrón
            # Busca X o Y como 2do carácter (ej: "Ex", "Sx") o como palabra sola
            _pfx2 = u_name[:2] if len(u_name) >= 2 else u_name
            _has_x = (
                _pfx2 in ('EX', 'SX', 'QX')
                or bool(re.search(r'(?<![A-Z])X(?![A-Z])', u_name))
            )
            _has_y = (
                _pfx2 in ('EY', 'SY', 'QY')
                or bool(re.search(r'(?<![A-Z])Y(?![A-Z])', u_name))
            )
            if _has_x and not _has_y:
                cat = 'Sx'
            elif _has_y and not _has_x:
                cat = 'Sy'
            else:
                cat = 'Ignorar'
        elif 'WIND' in u_type:
            _pfx2 = u_name[:2] if len(u_name) >= 2 else u_name
            has_x = _pfx2 in ('WX',) or bool(re.search(r'(?<![A-Z])X(?![A-Z])', u_name))
            has_y = _pfx2 in ('WY',) or bool(re.search(r'(?<![A-Z])Y(?![A-Z])', u_name))
            if '+' in name:
                cat = 'Wx+' if (has_x or not has_y) else 'Wy+'
            elif '-' in name:
                cat = 'Wx-' if (has_x or not has_y) else 'Wy-'
            else:
                cat = 'Wx+' if (has_x or not has_y) else 'Wy+'
        else:
            # Intentar clasificar por nombre exacto (SAP2000 estándar)
            _name_map = {
                'D': 'D', 'SD': 'SD', 'L': 'L', 'LR': 'Lr', 'LR': 'Lr',
                'LE': 'Le', 'WX+': 'Wx+', 'WX-': 'Wx-',
                'WY+': 'Wy+', 'WY-': 'Wy-',
                'SX': 'Sx', 'SY': 'Sy', 'EX': 'Sx', 'EY': 'Sy',
            }
            cat = _name_map.get(u_name, 'Ignorar')

        result[name] = cat
    return result


def classify_from_user(user_assignment):
    """
    Convierte el dict de asignación del usuario a dict cl para gen_combos().

    user_assignment: {pattern_name: category_str}
    category_str: 'D', 'SD', 'L', 'Lr', 'Le',
                  'Wx+', 'Wx-', 'Wy+', 'Wy-', 'Sx', 'Sy', 'Ignorar'
    Retorna cl dict compatible con gen_combos().
    """
    cl = {
        'dead': [], 'superdead': [], 'live': [],
        'live_roof': [], 'live_eq': [],
        'seismic_x': [], 'seismic_y': [],
        'wind_xp': [], 'wind_xn': [],
        'wind_yp': [], 'wind_yn': [],
        'other': [],
    }
    _cat_map = {
        'D': 'dead', 'SD': 'superdead', 'L': 'live',
        'Lr': 'live_roof', 'Le': 'live_eq',
        'Wx+': 'wind_xp', 'Wx-': 'wind_xn',
        'Wy+': 'wind_yp', 'Wy-': 'wind_yn',
        'Sx': 'seismic_x', 'Sy': 'seismic_y',
        'Ignorar': 'other',
    }
    for pat, cat in user_assignment.items():
        key = _cat_map.get(cat, 'other')
        cl[key].append(pat)
    return cl