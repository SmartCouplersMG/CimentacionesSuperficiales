# export_s2k.py
# ------------------------------------------------------------
# Exportador SAP2000 .$2k para el modelo de cimentación
# v5:
#   - pedestales como frames
#   - zapatas shell malladas
#   - un BODY constraint por pedestal
#   - resortes por ÁREA (compatibilidad SAP) en X, Y, Z
#   - Kx = Ky = alpha_xy * Kz
#   - material único de concreto según f'c del sidebar
#   - load patterns, load cases, combos ASD/LRFD, envelopes
#   - vigas de enlace con dimensiones diseñadas por la app
# ------------------------------------------------------------
import math
import uuid
from collections import defaultdict

from parser import gen_combos
from export_utils import build_export_loads


def _guid():
    return str(uuid.uuid4())


def _fmt(v):
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


def _line(**kwargs):
    parts = []
    for k, v in kwargs.items():
        parts.append(f"{k}={_fmt(v)}")
    return "   ".join(parts)


def _copy_table_if_exists(out, tables_src, tname):
    if tname in tables_src and tables_src[tname]:
        out[tname].extend(tables_src[tname])


def build_axis_lines(foot_min, foot_max, features, min_lines=5):
    pts = [foot_min, foot_max]
    for f in features:
        if foot_min - 1e-9 <= f <= foot_max + 1e-9:
            pts.append(float(f))
    pts = sorted(set(round(float(p), 6) for p in pts))
    while len(pts) < min_lines:
        spans = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
        idx = max(range(len(spans)), key=lambda k: spans[k])
        mid = round((pts[idx] + pts[idx + 1]) / 2.0, 6)
        if mid in pts:
            break
        pts = sorted(pts + [mid])
    return pts


def generate_compatible_mesh(footing, pedestal_rects, min_lines=5):
    xf = footing["x_footing"]
    yf = footing["y_footing"]
    B = footing["B"]
    L = footing["L"]
    x_min, x_max = xf - B / 2.0, xf + B / 2.0
    y_min, y_max = yf - L / 2.0, yf + L / 2.0

    x_features, y_features = [], []
    for (xr0, yr0, xr1, yr1) in pedestal_rects:
        x_features += [xr0, (xr0 + xr1) / 2.0, xr1]
        y_features += [yr0, (yr0 + yr1) / 2.0, yr1]

    xs = build_axis_lines(x_min, x_max, x_features, min_lines=min_lines)
    ys = build_axis_lines(y_min, y_max, y_features, min_lines=min_lines)
    return xs, ys


def _rect_section_general(section_name, material, t3, t2, color="Yellow"):
    area = t2 * t3
    I33 = t2 * t3**3 / 12.0
    I22 = t3 * t2**3 / 12.0
    J = min(t2, t3) * max(t2, t3)**3 / 3.0 * 0.2
    AS2 = max(0.833 * area, 1e-6)
    AS3 = max(0.833 * area, 1e-6)
    return _line(
        SectionName=f'"{section_name}"', Material=f'"{material}"', Shape="Rectangular",
        t3=t3, t2=t2, Area=area, TorsConst=J, I33=I33, I22=I22, I23=0,
        AS2=AS2, AS3=AS3,
        S33Top=I33 / (t3 / 2.0) if t3 > 0 else 0,
        S33Bot=I33 / (t3 / 2.0) if t3 > 0 else 0,
        S22Left=I22 / (t2 / 2.0) if t2 > 0 else 0,
        S22Right=I22 / (t2 / 2.0) if t2 > 0 else 0,
        Z33=1.5 * I33 / (t3 / 2.0) if t3 > 0 else 0,
        Z22=1.5 * I22 / (t2 / 2.0) if t2 > 0 else 0,
        R33=math.sqrt(I33 / area) if area > 0 else 0,
        R22=math.sqrt(I22 / area) if area > 0 else 0,
        CGOffset3=0, CGOffset2=0, EccV2=0, EccV3=0, Cw=0,
        Color=color, FromFile="No",
        AMod=1, A2Mod=1, A3Mod=1, JMod=1, I2Mod=1, I3Mod=1, MMod=1, WMod=1,
        GUID=f'"{_guid()}"'
    )


def _rect_beam_rebar_row(section_name):
    return _line(
        SectionName=f'"{section_name}"',
        RebarMatL="A706Gr60", RebarMatC="A706Gr60",
        TopCover=0.04, BotCover=0.04,
        TopLeftArea=0, TopRghtArea=0, BotLeftArea=0, BotRghtArea=0
    )


def _rect_column_rebar_row(section_name, cover=0.04):
    return _line(
        SectionName=f'"{section_name}"',
        RebarMatL="A706Gr60", RebarMatC="A706Gr60",
        ReinfConfig="Rectangular", LatReinf="Ties",
        Cover=cover, NumBars3Dir=3, NumBars2Dir=3,
        BarSizeL="#5", BarSizeC="#3",
        SpacingC=0.10, NumCBars2=3, NumCBars3=3, ReinfType="Check"
    )


def _area_shell_row(section_name, material, thickness, color="DarkCyan"):
    return _line(
        Section=f'"{section_name}"', Material=f'"{material}"', MatAngle=0,
        AreaType="Shell", Type="Shell-Thick", DrillDOF="Yes",
        Thickness=thickness, BendThick=thickness, Color=color,
        F11Mod=1, F22Mod=1, F12Mod=1, M11Mod=1, M22Mod=1, M12Mod=1,
        V13Mod=1, V23Mod=1, MMod=1, WMod=1, GUID=f'"{_guid()}"'
    )


def _build_concrete_material_name(fc_mpa: float) -> str:
    return f"CONC_{int(round(fc_mpa))}MPa"

def _project_info_rows(project_info=None, model_name="CIMENTACION_EXPORTADA"):
    project_info = project_info or {}

    defaults = {
        "Company Name": "SmartCouplers MG SAS",
        "Client Name": "No Client",
        "Project Name": "Sin Nombre",
        "Project Number": "001",
        "Model Name": model_name,
        "Model Description": "Modelo creado a partir de una aplicacion web creada con IA",
        "Revision Number": "001",
        "Frame Type": "Sistema de Cimentaciones Superficiales",
        "Engineer": "Definir Nombre de Diseñador",
        "Checker": "Definir Nombre de Revisor",
        "Supervisor": "Definir Nombre de Supervisor",
        "Issue Code": "Version 01",
        "Design Code": "Version 01",
    }

    defaults.update(project_info)

    rows = []
    for item, data in defaults.items():
        rows.append(_line(Item=f'"{item}"', Data=f'"{data}"'))
    return rows

def export_foundation_s2k(model_data, results, params, export_cfg=None):
    export_cfg = export_cfg or {}
    tables_src = model_data["_tables"]

    # ── Canonical load rename (D, SD, Sx, Sy … instead of original names) ──
    export_lp, _rename_map, jloads_src, _renamed_cl, _combos_prebuilt = \
        build_export_loads(model_data, params)

    final_footings = results["final_footings"]
    tie_systems = results.get("tie_systems", [])
    ties = results.get("ties", {})

    units = export_cfg.get("units", "KN, m, C")
    model_name = export_cfg.get("model_name", "CIMENTACION_EXPORTADA")
    project_info = export_cfg.get("project_info", {})
    pedestal_h = float(export_cfg.get("pedestal_h", 0.50))
    z_top = float(export_cfg.get("z_top", 0.0))
    footing_z = z_top - pedestal_h
    k_subgrade = float(export_cfg.get("k_subgrade", 12000.0))
    alpha_xy = float(export_cfg.get("alpha_xy", 0.35))
    fc_mpa = float(export_cfg.get("fc_mpa", 21.0))
    concrete_material = export_cfg.get("concrete_material_name", _build_concrete_material_name(fc_mpa))
    fallback_tie_b = float(export_cfg.get("tie_b", 0.30))
    fallback_tie_h = float(export_cfg.get("tie_h", 0.50))
    shell_sec_prefix = export_cfg.get("shell_section_prefix", "ZAP_")

    out = defaultdict(list)

    # --------------------------------------------------------
    # PROGRAM CONTROL
    # --------------------------------------------------------
    pc_written = False

    if tables_src.get("PROGRAM CONTROL"):
        pc = tables_src["PROGRAM CONTROL"][0]
        if "CurrUnits=" in pc:
            parts = []
            for token in pc.split("   "):
                if token.startswith("CurrUnits="):
                    parts.append(f'CurrUnits="{units}"')
                else:
                    parts.append(token)
            pc = "   ".join(parts)
        out["PROGRAM CONTROL"].append(pc)
        pc_written = True

    if not pc_written:
        out["PROGRAM CONTROL"].append(
            _line(
                ProgramName="SAP2000",
                Version="26.1.0",
                CurrUnits=f'"{units}"',
                SteelCode="AISC 360-10",
                ConcCode="ACI 318-08/IBC2009",
                AlumCode="AA 2015",
                ColdCode="AISI-16",
                ConcSCode="Eurocode 2-2004",
                RegenHinge="Yes"
            )
        )
        # --------------------------------------------------------
        # PROJECT INFORMATION
        # --------------------------------------------------------
        out["PROJECT INFORMATION"].extend(
            _project_info_rows(project_info=project_info, model_name=model_name)
        )

    # Copy all non-concrete material tables
    for tname in [
        "MATERIAL PROPERTIES 03A - STEEL DATA",
        "MATERIAL PROPERTIES 03D - COLD FORMED DATA",
        "MATERIAL PROPERTIES 03E - REBAR DATA",
        "MATERIAL PROPERTIES 03F - TENDON DATA",
        "MATERIAL PROPERTIES 03G - OTHER DATA",
        "MATERIAL PROPERTIES 03J - COUPLED NONLINEAR VON MISES DATA",
        "MATERIAL PROPERTIES 04 - USER STRESS-STRAIN CURVES",
        "MATERIAL PROPERTIES 06 - DAMPING PARAMETERS",
        "REBAR SIZES",
    ]:
        _copy_table_if_exists(out, tables_src, tname)

    for row in tables_src.get("MATERIAL PROPERTIES 01 - GENERAL", []):
        if 'Type="Concrete"' not in row:
            out["MATERIAL PROPERTIES 01 - GENERAL"].append(row)

    # Create one concrete material from fc
    e_mpa = 4700 * math.sqrt(fc_mpa)
    out["MATERIAL PROPERTIES 01 - GENERAL"].append(
        _line(
            Material=f'"{concrete_material}"', Type="Concrete", SymType="Isotropic",
            TempDepend="No", Color="Cyan", Notes=f'"fpc={fc_mpa} MPa"', GUID=f'"{_guid()}"'
        )
    )
    out["MATERIAL PROPERTIES 02 - BASIC MECHANICAL PROPERTIES"].append(
        _line(Material=f'"{concrete_material}"', E1=e_mpa, U12=0.2, A1=9.9e-6, UnitMass=24.0, UnitWeight=24.0)
    )
    out["MATERIAL PROPERTIES 03B - CONCRETE DATA"].append(
        _line(
            Material=f'"{concrete_material}"', Fc=fc_mpa * 10.1972, LtWtConc="No",
            FcsFactor=1, SSType="Parametric-Simple", SSHysType="Takeda",
            StrainAtFc=0.002219, StrainUltimate=0.005, FinalSlope=0
        )
    )

    # Load patterns and cases  (use canonical export names)
    load_pat_rows = []
    load_case_rows = []
    static_assignments = []
    for pat, des_type in export_lp.items():
        load_pat_rows.append(
            _line(LoadPat=f'"{pat}"', DesignType=f'"{des_type}"', SelfWtMult=0,
                  AutoLoad="None")
        )
    out["LOAD PATTERN DEFINITIONS"].extend(load_pat_rows)
    for pat, des_type in export_lp.items():
        load_case_rows.append(
            _line(
                Case=f'"{pat}"', Type="LinStatic", InitialCond="Zero",
                DesTypeOpt='"Prog Det"', DesignType=f'"{des_type}"',
                DesActOpt='"Prog Det"', DesignAct='"Short-Term Composite"',
                AutoType="None", RunCase="Yes", GUID=f'"{_guid()}"'
            )
        )
        static_assignments.append(_line(Case=f'"{pat}"', LoadType='"Load pattern"', LoadName=f'"{pat}"', LoadSF=1))
    out["LOAD CASE DEFINITIONS"].extend(load_case_rows)
    out["CASE - STATIC 1 - LOAD ASSIGNMENTS"].extend(static_assignments)

    # Combos (use pre-built combos with canonical names)
    combos = _combos_prebuilt
    ads_combo_names = []
    lrfd_combo_names = []

    def add_combo_block(combo_name, factors, combo_type="Linear Add"):
        first = True
        for case_name, sf in factors.items():
            if case_name not in export_lp:   # skip cases not defined
                continue
            row = {"ComboName": f'"{combo_name}"'}
            if first:
                row.update({
                    "ComboType": f'"{combo_type}"', "AutoDesign": "No",
                    "CaseName": f'"{case_name}"', "ScaleFactor": sf,
                    "SteelDesign": "None", "ConcDesign": "None", "AlumDesign": "None", "ColdDesign": "None",
                    "GUID": f'"{_guid()}"'
                })
                first = False
            else:
                row.update({"CaseName": f'"{case_name}"', "ScaleFactor": sf})
            out["COMBINATION DEFINITIONS"].append(_line(**row))

    for c in combos["ADS"]:
        ads_combo_names.append(c["name"])
        add_combo_block(c["name"], c["factors"])
    for c in combos["LRFD"]:
        lrfd_combo_names.append(c["name"])
        add_combo_block(c["name"], c["factors"])

    def add_envelope(env_name, members):
        first = True
        for m in members:
            row = {"ComboName": f'"{env_name}"'}
            if first:
                row.update({
                    "ComboType": '"Envelope"', "AutoDesign": "No", "CaseName": f'"{m}"',
                    "ScaleFactor": 1, "SteelDesign": "None", "ConcDesign": "None",
                    "AlumDesign": "None", "ColdDesign": "None", "GUID": f'"{_guid()}"'
                })
                first = False
            else:
                row.update({"CaseName": f'"{m}"', "ScaleFactor": 1})
            out["COMBINATION DEFINITIONS"].append(_line(**row))

    if ads_combo_names:
        add_envelope("ENV_ASD", ads_combo_names)
    if lrfd_combo_names:
        add_envelope("ENV_LRFD", lrfd_combo_names)

    # Sections
    shell_sections_used = {}
    for f in final_footings:
        sec = f"{shell_sec_prefix}{int(round(f['h'] * 100))}cm"
        shell_sections_used[sec] = f["h"]
    for sec, thk in shell_sections_used.items():
        out["AREA SECTION PROPERTIES"].append(_area_shell_row(sec, concrete_material, thk))

    beam_section_by_system = {}
    for sys in tie_systems:
        if sys.get("status") in ("insuficiente", "distancia_insuficiente"):
            continue
        b_v = float(sys.get("b_viga", fallback_tie_b))
        h_v = float(sys.get("h_viga", fallback_tie_h))
        sec_name = f'VG_{sys["system_id"]}_{int(round(b_v*100))}x{int(round(h_v*100))}'
        beam_section_by_system[sys["system_id"]] = sec_name
        out["FRAME SECTION PROPERTIES 01 - GENERAL"].append(
            _rect_section_general(sec_name, concrete_material, h_v, b_v, color="Yellow")
        )
        out["FRAME SECTION PROPERTIES 03 - CONCRETE BEAM"].append(_rect_beam_rebar_row(sec_name))

    pedestal_sections_added = set()
    for f in final_footings:
        for c in f.get("cols", []):
            sec = c.get("section") or f"PED_{int(round(c['bx'] * 100))}x{int(round(c['by'] * 100))}"
            if sec in pedestal_sections_added:
                continue
            pedestal_sections_added.add(sec)
            out["FRAME SECTION PROPERTIES 01 - GENERAL"].append(
                _rect_section_general(sec, concrete_material, c["by"], c["bx"], color="Red")
            )
            out["FRAME SECTION PROPERTIES 02 - CONCRETE COLUMN"].append(_rect_column_rebar_row(sec))

    joint_map = {}
    joints_out = []
    joint_constraints = []
    frames_out = []
    frame_assign = []
    areas_out = []
    area_assign = []
    area_springs = []
    joint_load_rows = []

    body_defs_added = set()
    frame_counter = 1
    area_counter = 1
    joint_counter = 1

    def get_joint_id(x, y, z):
        nonlocal joint_counter
        key = (round(x, 6), round(y, 6), round(z, 6))
        if key in joint_map:
            return joint_map[key]
        jid = str(joint_counter)
        joint_counter += 1
        joint_map[key] = jid
        joints_out.append(_line(Joint=jid, CoordSys="GLOBAL", CoordType="Cartesian", XorR=x, Y=y, Z=z))
        return jid

    # support tie section lookup
    joint_to_footing = {}
    for f in final_footings:
        for c in f.get("cols", []):
            joint_to_footing[str(c["joint"])] = f["id"]

    pair_to_system = {}
    for sys in tie_systems:
        if sys.get("status") in ("insuficiente", "distancia_insuficiente"):
            continue
        fids = sys.get("footings", [])
        _dir = sys.get("direction", "X")
        _sid = sys["system_id"]
        # Usar TODOS los pares de zapatas del sistema (grafo completo), no solo consecutivos.
        # El BFS retorna footings en orden de visita, que puede no ser el orden espacial,
        # por lo que pares consecutivos no cubren todas las conexiones directas posibles.
        for _i in range(len(fids)):
            for _j in range(_i + 1, len(fids)):
                pair_to_system[(fids[_i], fids[_j], _dir)] = _sid
                pair_to_system[(fids[_j], fids[_i], _dir)] = _sid

    tie_done = set()

    for f in final_footings:
        cols = f.get("cols", [])
        pedestal_rects = [
            (c["x"] - c["bx"] / 2.0, c["y"] - c["by"] / 2.0, c["x"] + c["bx"] / 2.0, c["y"] + c["by"] / 2.0)
            for c in cols
        ]
        xs, ys = generate_compatible_mesh(f, pedestal_rects, min_lines=5)

        shell_joint_ids = {}
        for j, y in enumerate(ys):
            for i, x in enumerate(xs):
                shell_joint_ids[(i, j)] = get_joint_id(x, y, footing_z)

        # BODY per pedestal
        ped_name_by_joint = {}
        for c in cols:
            ped_name = f"BODY_PED_{str(c['joint'])}"
            ped_name_by_joint[str(c["joint"])] = ped_name

            if ped_name not in body_defs_added:
                out["CONSTRAINT DEFINITIONS - BODY"].append(
                    _line(Name=ped_name, CoordSys="GLOBAL", UX="Yes", UY="Yes", UZ="Yes", RX="Yes", RY="Yes", RZ="Yes")
                )
                body_defs_added.add(ped_name)
            xr0 = c["x"] - c["bx"] / 2.0
            yr0 = c["y"] - c["by"] / 2.0
            xr1 = c["x"] + c["bx"] / 2.0
            yr1 = c["y"] + c["by"] / 2.0
            for j, y in enumerate(ys):
                for i, x in enumerate(xs):
                    if xr0 - 1e-9 <= x <= xr1 + 1e-9 and yr0 - 1e-9 <= y <= yr1 + 1e-9:
                        joint_constraints.append(_line(Joint=shell_joint_ids[(i, j)], Constraint=ped_name))

        # Shell areas + area springs
        sec_name = f"{shell_sec_prefix}{int(round(f['h'] * 100))}cm"
        for j in range(len(ys) - 1):
            for i in range(len(xs) - 1):
                a_id = f"A{area_counter}"
                area_counter += 1
                n1 = shell_joint_ids[(i, j)]
                n2 = shell_joint_ids[(i + 1, j)]
                n3 = shell_joint_ids[(i + 1, j + 1)]
                n4 = shell_joint_ids[(i, j + 1)]
                areas_out.append(_line(Area=a_id, NumJoints=4, Joint1=n1, Joint2=n2, Joint3=n3, Joint4=n4))
                area_assign.append(_line(Area=a_id, Section=f'"{sec_name}"', MatProp="Default"))

                dx = xs[i + 1] - xs[i]
                dy = ys[j + 1] - ys[j]
                area_panel = dx * dy

                kz = k_subgrade
                kx = alpha_xy * kz
                ky = alpha_xy * kz

                # Resorte vertical
                area_springs.append(
                    _line(
                        Area=a_id,
                        Type="Simple",
                        Stiffness=kz,
                        SimpleType='"Compression Only"',
                        Face="Bottom",
                        Dir1Type='"Normal To Face"',
                        NormalDir="Inward"
                    )
                )

                # Resorte horizontal X
                area_springs.append(
                    _line(
                        Area=a_id,
                        Type="Simple",
                        Stiffness=kx,
                        SimpleType='"Tension and Compression"',
                        Face="Bottom",
                        Dir1Type='"User Vector"',
                        CoordSys="GLOBAL",
                        VecX=1,
                        VecY=0,
                        VecZ=0
                    )
                )

                # Resorte horizontal Y
                area_springs.append(
                    _line(
                        Area=a_id,
                        Type="Simple",
                        Stiffness=ky,
                        SimpleType='"Tension and Compression"',
                        Face="Bottom",
                        Dir1Type='"User Vector"',
                        CoordSys="GLOBAL",
                        VecX=0,
                        VecY=1,
                        VecZ=0
                    )
                )

        # Pedestals + loads
        for c in cols:
            x_col = c["x"]
            y_col = c["y"]
            sec = c.get("section") or f"PED_{int(round(c['bx'] * 100))}x{int(round(c['by'] * 100))}"
            j_top = get_joint_id(x_col, y_col, z_top)
            j_bot = get_joint_id(x_col, y_col, footing_z)
            ped_name = ped_name_by_joint.get(str(c["joint"]))
            if ped_name:
                joint_constraints.append(_line(Joint=j_top, Constraint=ped_name))
                joint_constraints.append(_line(Joint=j_bot, Constraint=ped_name))
            fr = f"F{frame_counter}"
            frame_counter += 1
            frames_out.append(_line(Frame=fr, JointI=j_top, JointJ=j_bot))
            frame_assign.append(_line(Frame=fr, AutoSelect="N.A.", AnalSect=f'"{sec}"', MatProp="Default"))

            orig_joint = str(c["joint"])
            # jloads_src is already renamed to canonical names by build_export_loads
            for pat, fv in jloads_src.get(orig_joint, {}).items():
                if pat not in export_lp:   # safety: skip anything not defined
                    continue
                joint_load_rows.append(
                    _line(
                        Joint=j_top, LoadPat=f'"{pat}"', CoordSys="GLOBAL",
                        F1=fv.get("F1", 0), F2=fv.get("F2", 0), F3=fv.get("F3", 0),
                        M1=fv.get("M1", 0), M2=fv.get("M2", 0), M3=fv.get("M3", 0)
                    )
                )

        # Tie beams with designed sections
        own_joints = [str(c["joint"]) for c in cols]
        for oj in own_joints:
            t = ties.get(str(oj), {})
            if not t.get("needs_tie"):
                continue

            def add_tie(target_joint_raw, direction):
                nonlocal frame_counter
                target_joint_raw = str(target_joint_raw)
                pair = tuple(sorted([str(oj), target_joint_raw])) + (direction,)
                if pair in tie_done:
                    return
                tie_done.add(pair)

                c_from = next((cc for cc in cols if str(cc["joint"]) == str(oj)), None)
                c_to = None
                for ff in final_footings:
                    for cc in ff.get("cols", []):
                        if str(cc["joint"]) == target_joint_raw:
                            c_to = cc
                            break
                    if c_to:
                        break
                if not c_from or not c_to:
                    return

                fid_from = joint_to_footing.get(str(oj))
                fid_to = joint_to_footing.get(target_joint_raw)
                sys_id = pair_to_system.get((fid_from, fid_to, direction))
                sec_name_tie = beam_section_by_system.get(sys_id)
                if not sec_name_tie:
                    return

                j1 = get_joint_id(c_from["x"], c_from["y"], footing_z)
                j2 = get_joint_id(c_to["x"], c_to["y"], footing_z)
                fr = f"F{frame_counter}"
                frame_counter += 1
                frames_out.append(_line(Frame=fr, JointI=j1, JointJ=j2))
                frame_assign.append(_line(Frame=fr, AutoSelect="N.A.", AnalSect=f'"{sec_name_tie}"', MatProp="Default"))

            if t.get("is_corner"):
                tx = t.get("tie_x", {})
                ty = t.get("tie_y", {})
                if tx.get("tie_to"):
                    add_tie(tx["tie_to"], "X")
                if ty.get("tie_to"):
                    add_tie(ty["tie_to"], "Y")
            elif t.get("tie_to"):
                add_tie(t["tie_to"], t.get("tie_dir", "X"))

    out["JOINT COORDINATES"].extend(joints_out)
    out["CONNECTIVITY - FRAME"].extend(frames_out)
    out["FRAME SECTION ASSIGNMENTS"].extend(frame_assign)
    out["CONNECTIVITY - AREA"].extend(areas_out)
    out["AREA SECTION ASSIGNMENTS"].extend(area_assign)
    out["AREA SPRING ASSIGNMENTS"].extend(area_springs)
    out["JOINT CONSTRAINT ASSIGNMENTS"].extend(joint_constraints)
    out["JOINT LOADS - FORCE"].extend(joint_load_rows)

    ordered_names = [
        "PROGRAM CONTROL",
        "PROJECT INFORMATION",
        "MATERIAL PROPERTIES 01 - GENERAL",
        "MATERIAL PROPERTIES 02 - BASIC MECHANICAL PROPERTIES",
        "MATERIAL PROPERTIES 03A - STEEL DATA",
        "MATERIAL PROPERTIES 03B - CONCRETE DATA",
        "MATERIAL PROPERTIES 03D - COLD FORMED DATA",
        "MATERIAL PROPERTIES 03E - REBAR DATA",
        "MATERIAL PROPERTIES 03F - TENDON DATA",
        "MATERIAL PROPERTIES 03G - OTHER DATA",
        "MATERIAL PROPERTIES 03J - COUPLED NONLINEAR VON MISES DATA",
        "MATERIAL PROPERTIES 04 - USER STRESS-STRAIN CURVES",
        "MATERIAL PROPERTIES 06 - DAMPING PARAMETERS",
        "REBAR SIZES",
        "LOAD PATTERN DEFINITIONS",
        "LOAD CASE DEFINITIONS",
        "CASE - STATIC 1 - LOAD ASSIGNMENTS",
        "COMBINATION DEFINITIONS",
        "CONSTRAINT DEFINITIONS - BODY",
        "FRAME SECTION PROPERTIES 01 - GENERAL",
        "FRAME SECTION PROPERTIES 02 - CONCRETE COLUMN",
        "FRAME SECTION PROPERTIES 03 - CONCRETE BEAM",
        "AREA SECTION PROPERTIES",
        "JOINT COORDINATES",
        "CONNECTIVITY - FRAME",
        "FRAME SECTION ASSIGNMENTS",
        "CONNECTIVITY - AREA",
        "AREA SECTION ASSIGNMENTS",
        "AREA SPRING ASSIGNMENTS",
        "JOINT CONSTRAINT ASSIGNMENTS",
        "JOINT LOADS - FORCE",
    ]

    lines = [f"$ File generated by Foundation Exporter | Model={model_name}", ""]
    for tname in ordered_names:
        rows = out.get(tname, [])
        if not rows:
            continue
        lines.append(f'TABLE:  "{tname}"')
        lines.extend(rows)
        lines.append("")

    for tname, rows in out.items():
        if tname in ordered_names or not rows:
            continue
        lines.append(f'TABLE:  "{tname}"')
        lines.extend(rows)
        lines.append("")

    text_out = "\n".join(lines)

    if 'TABLE:  "PROGRAM CONTROL"' not in text_out:
        raise ValueError("No se generó la tabla PROGRAM CONTROL en el .$2k")

    return text_out
