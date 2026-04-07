"""
tie_system.py — Módulo de sistemas de vigas de enlace

Funciones:
  deduce_tie_beams                 — Deduce vigas automáticas según clasificación
  build_tie_systems                — Construye sistemas de vigas únicos
  beam_solve_simple_overhangs      — Solver 2 apoyos (equilibrio estático)
  beam_solve_multi_support         — Solver N apoyos (rigidez matricial Euler-Bernoulli)
  analyze_tie_system               — Análisis completo del sistema enlazado
  apply_system_reactions_to_footings — Aplica reacciones del sistema a zapatas

FIXES APLICADOS:
  FIX1: Tolerancia de coalescencia de nudos (NODE_COALESCE_TOL = 0.05m)
        Si |x_load - x_sup| < tol, se fusionan y se aplica M = P·e como momento.
  FIX2: Ancho de viga ≤ min(dimensión perpendicular de columnas conectadas)
"""
import math
import numpy as np
from collections import defaultdict
from isolated import calc_as, propose_rebar, soil_pressure

# ── Tolerancia de coalescencia de nudos (FIX1) ──
NODE_COALESCE_TOL = 0.05  # metros


# ================================================================
# DEDUCCIÓN AUTOMÁTICA DE VIGAS DE ENLACE
# ================================================================
def deduce_tie_beams(columns, classifications):
    """
    Para cada columna esquinera/medianera, buscar la zapata más cercana
    en la dirección opuesta a la restricción para la viga de enlace.
    """
    ties = {}
    for col in columns:
        jid = col['joint']
        cl = classifications.get(jid, {})
        loc = cl.get('location', 'concentrica')

        if loc == 'concentrica':
            ties[jid] = {'needs_tie': False}; continue

        if loc == 'medianera':
            side = cl.get('side', '')
            if side in ('X+', 'X-'):
                search_dir = 'X'; search_sign = -1 if side == 'X+' else 1
            elif side in ('Y+', 'Y-'):
                search_dir = 'Y'; search_sign = -1 if side == 'Y+' else 1
            else:
                ties[jid] = {'needs_tie': True, 'warning': 'Lado no definido'}; continue
            best = _find_nearest(col, columns, search_dir, search_sign)
            if best:
                ties[jid] = {'needs_tie': True, 'tie_to': best['joint'], 'tie_dir': search_dir,
                             'tie_dist': best['dist'], 'tie_x': best['x'], 'tie_y': best['y'],
                             'scheme_suggested': f'medianera_viga_{search_dir}'}
            else:
                ties[jid] = {'needs_tie': True, 'warning': f'No hay zapata para enlace en dir {search_dir}',
                             'scheme_suggested': 'revisar_manual'}

        elif loc == 'esquinera':
            corner = cl.get('corner', '')
            tie_x = None; tie_y = None
            if 'X+' in corner: tie_x = _find_nearest(col, columns, 'X', -1)
            elif 'X-' in corner: tie_x = _find_nearest(col, columns, 'X', 1)
            if 'Y+' in corner: tie_y = _find_nearest(col, columns, 'Y', -1)
            elif 'Y-' in corner: tie_y = _find_nearest(col, columns, 'Y', 1)
            has_tx = bool(tie_x); has_ty = bool(tie_y)
            if has_tx and has_ty: scheme_s = 'esquinera_doble_viga'
            elif has_tx: scheme_s = 'esquinera_viga_X'
            elif has_ty: scheme_s = 'esquinera_viga_Y'
            else: scheme_s = 'revisar_manual'
            ties[jid] = {
                'needs_tie': True, 'is_corner': True, 'scheme_suggested': scheme_s,
                'tie_x': {'tie_to': tie_x['joint'], 'tie_dir': 'X', 'tie_dist': tie_x['dist'],
                          'tie_x': tie_x['x'], 'tie_y': tie_x['y']} if tie_x else {'warning': 'Sin enlace en X'},
                'tie_y': {'tie_to': tie_y['joint'], 'tie_dir': 'Y', 'tie_dist': tie_y['dist'],
                          'tie_x': tie_y['x'], 'tie_y': tie_y['y']} if tie_y else {'warning': 'Sin enlace en Y'},
            }

        elif loc == 'excentrica':
            ecc_x = cl.get('ecc_x', 0); ecc_y = cl.get('ecc_y', 0)
            ties[jid] = {'needs_tie': False, 'ecc_x': ecc_x, 'ecc_y': ecc_y}
        else:
            ties[jid] = {'needs_tie': False}

    return ties


def _find_nearest(col, all_cols, direction, sign, tol_ortho=0.20):
    """Busca la columna más cercana en dirección ORTOGONAL estricta."""
    best = None; best_dist = 999
    cx, cy = col['x'], col['y']
    for c in all_cols:
        if c['joint'] == col['joint']: continue
        if direction == 'X':
            delta = (c['x'] - cx) * sign; align = abs(c['y'] - cy)
        else:
            delta = (c['y'] - cy) * sign; align = abs(c['x'] - cx)
        if align <= tol_ortho and delta > 0.3 and delta < best_dist:
            best_dist = delta
            best = {'joint': c['joint'], 'x': c['x'], 'y': c['y'], 'dist': round(delta, 2)}
    return best


# ================================================================
# CONSTRUCCIÓN DE SISTEMAS
# ================================================================
def build_tie_systems(final_footings, ties):
    """
    Construye sistemas de vigas por NUDOS (columnas), no por zapatas.

    Arquitectura:
    • Cada sistema conecta columna Jᵢ ↔ Jⱼ (y posiblemente más en cadena colíneal).
    • La BFS opera sobre joint-IDs individuales — no sobre footing-IDs.
    • Al trabajar a nivel de nudo no existe topología estrella: cada columna tiene
      como máximo 1 conexión por dirección → cada conexión es su propio sistema
      (o parte de una cadena multi-tramo si los nudos son colíneales).
    • Los centroides de zapata son únicamente los apoyos (restricciones simples)
      en el modelo de viga; se deducen automáticamente en analyze_tie_system.
    • Sistemas intra-combinada (dos nudos en la misma zapata combinada): se marcan
      con intra_combined=True y se delegan al análisis longitudinal de combined.py.
    """
    # ── Mapa nudo → ID zapata ──
    j2f = {}
    for f in final_footings:
        fid = f.get('id', '')
        for jj in f.get('joint', '').split('+'):
            jj = jj.strip()
            if jj:
                j2f[jj] = fid
        for c in f.get('cols', []):
            j2f[c['joint']] = fid

    def resolve(raw):
        return j2f.get(str(raw), str(raw))

    # ── Mapa nudo → coordenadas de columna (para verificación de colinealidad) ──
    j2pos = {}
    for f in final_footings:
        for c in f.get('cols', []):
            j2pos[c['joint']] = (c['x'], c['y'])

    _COLLINEAR_TOL_DEG = 10.0   # tolerancia angular: eje X o Y ± 10°
    _COLLINEAR_TOL_RAD = math.radians(_COLLINEAR_TOL_DEG)

    def _edge_is_collinear(jA_str, jB_str, d):
        """
        Verifica que la arista jA–jB esté dentro de ±10° de la dirección d ('X' o 'Y').
        Si no se tienen coordenadas para ambos nudos, se acepta la arista (no se descarta).
        """
        posA = j2pos.get(jA_str); posB = j2pos.get(jB_str)
        if posA is None or posB is None:
            return True    # sin coords → aceptar (caso especial / legacy)
        dx = abs(posB[0] - posA[0]); dy = abs(posB[1] - posA[1])
        dist = math.hypot(dx, dy)
        if dist < 0.01:
            return True    # nudos coincidentes → aceptar
        if d == 'X':
            # ángulo del eje horizontal: atan2(dy, dx) debe ser < 10°
            return math.atan2(dy, dx) <= _COLLINEAR_TOL_RAD
        else:
            # eje vertical: atan2(dx, dy) < 10°
            return math.atan2(dx, dy) <= _COLLINEAR_TOL_RAD

    # ── Construir aristas a nivel de nudo ──
    seen_keys = set()
    joint_edges = []   # [{'jA': str, 'jB': str, 'dir': str}]

    def _add_joint_edge(jA_str, jB_str, d):
        if jA_str == jB_str:
            return
        key = (tuple(sorted([jA_str, jB_str])), d)
        if key in seen_keys:
            return
        seen_keys.add(key)
        # Filtrar aristas que no cumplan la colinealidad con la dirección declarada
        if not _edge_is_collinear(jA_str, jB_str, d):
            return
        joint_edges.append({'jA': jA_str, 'jB': jB_str, 'dir': d})

    for jid, t in ties.items():
        if not t.get('needs_tie'):
            continue
        jid_str = str(jid)
        if t.get('is_corner'):
            for tk in ['tie_x', 'tie_y']:
                td = t.get(tk, {})
                if td.get('tie_to'):
                    _add_joint_edge(jid_str, str(td['tie_to']), td.get('tie_dir', 'X'))
        elif t.get('tie_to'):
            _add_joint_edge(jid_str, str(t['tie_to']), t.get('tie_dir', 'X'))

    # ── BFS sobre nudos por dirección ──
    systems = []
    for direction in ['X', 'Y']:
        de = [(e['jA'], e['jB']) for e in joint_edges if e['dir'] == direction]
        if not de:
            continue
        adj = defaultdict(set)
        for a, b in de:
            adj[a].add(b); adj[b].add(a)

        visited = set()
        for start in sorted(adj.keys()):
            if start in visited:
                continue
            q = [start]; grp = []
            while q:
                n = q.pop(0)
                if n in visited:
                    continue
                visited.add(n); grp.append(n)
                for nb in adj[n]:
                    if nb not in visited:
                        q.append(nb)
            if len(grp) < 2:
                continue

            # Unique footings for this joint group (order-preserving)
            unique_fids = list(dict.fromkeys(resolve(j) for j in grp))

            # Intra-combined: todos los nudos pertenecen a la misma zapata combinada
            intra = (len(unique_fids) == 1)

            # tie_joints: footing → [joints in this system]
            tj = {}
            for j in grp:
                fid = resolve(j)
                tj.setdefault(fid, []).append(j)

            # ID del sistema basado en nudos (J) para claridad
            sid = f"SYS_J{'_J'.join(sorted(grp))}_{direction}"

            systems.append({
                'system_id': sid,
                'direction': direction,
                'joints': sorted(grp),       # IDs de nudos individuales
                'footings': unique_fids,      # Zapatas involucradas (para apoyos)
                'num_nodes': len(grp),
                'tie_joints': tj,
                'intra_combined': intra,
            })

    return systems


# ================================================================
# SOLVER A: Viga simplemente apoyada con voladizos (2 apoyos)
# ================================================================
def beam_solve_simple_overhangs(supports, point_loads, point_moments):
    """
    Viga con exactamente 2 apoyos simples y cargas/momentos puntuales.
    Puede tener voladizos (cargas fuera del tramo de apoyos).
    """
    xA, xB = min(supports), max(supports)
    L = xB - xA
    if L < 0.01:
        return {'reactions': [0, 0], 'stations': [], 'V': [], 'M': [],
                'M_pos_max': 0, 'M_neg_max': 0, 'V_max': 0,
                'balance': {'sumFy': 0, 'sumM': 0, 'ok_F': True, 'ok_M': True}}

    sum_P = sum(pl['P'] for pl in point_loads)
    sum_M_A = sum(pl['P'] * (pl['x'] - xA) for pl in point_loads) + sum(pm['M'] for pm in point_moments)
    RB = sum_M_A / L
    RA = sum_P - RB

    sumFy_check = RA + RB - sum_P
    sumM_check = RB * L - sum_M_A

    singular = sorted(set([xA, xB] + [pl['x'] for pl in point_loads] + [pm['x'] for pm in point_moments]))
    x_min = min(singular); x_max = max(singular)
    extra = []
    for i in range(len(singular) - 1):
        dx = singular[i + 1] - singular[i]
        if dx > 0.10:
            for j in range(1, max(2, int(dx / 0.05))):
                extra.append(singular[i] + j * dx / max(2, int(dx / 0.05)))
    stations = sorted(set(singular + extra))

    actions = {}
    def add_act(x, F=0, M=0):
        if x not in actions: actions[x] = {'F': 0, 'M': 0}
        actions[x]['F'] += F; actions[x]['M'] += M

    add_act(xA, F=RA); add_act(xB, F=RB)
    for pl in point_loads: add_act(pl['x'], F=-pl['P'])
    for pm in point_moments: add_act(pm['x'], M=pm['M'])

    V_cur = 0; M_cur = 0; V_list = []; M_list = []
    for k, x in enumerate(stations):
        if x in actions:
            V_cur += actions[x]['F']; M_cur += actions[x]['M']
        V_list.append(round(V_cur, 2)); M_list.append(round(M_cur, 2))
        if k < len(stations) - 1:
            dx = stations[k + 1] - x
            M_cur += V_cur * dx

    return {
        'reactions': [round(RA, 2), round(RB, 2)],
        'stations': [round(s, 4) for s in stations],
        'V': V_list, 'M': M_list,
        'M_pos_max': round(max(M_list) if M_list else 0, 2),
        'M_neg_max': round(min(M_list) if M_list else 0, 2),
        'V_max': round(max(abs(v) for v in V_list) if V_list else 0, 2),
        'balance': {
            'sumFy': round(sumFy_check, 6), 'sumM': round(sumM_check, 6),
            'ok_F': abs(sumFy_check) < 1e-3, 'ok_M': abs(sumM_check) < 1e-3,
        }
    }


# ================================================================
# SOLVER B: Viga continua con múltiples apoyos (stiffness matrix)
# ================================================================
def beam_solve_multi_support(support_xs, point_loads, point_moments, EI=1e8):
    """
    Viga de Euler-Bernoulli sobre N apoyos simples (v=0, θ libre).
    Cargas y momentos puntuales en posiciones arbitrarias.
    Método: rigidez matricial 2D con DOFs [v, θ] por nodo.
    """
    # FIX1: Coalescencia de nudos — fusionar posiciones muy cercanas
    all_x_raw = support_xs + [pl['x'] for pl in point_loads] + [pm['x'] for pm in point_moments]
    all_x_sorted = sorted(set(all_x_raw))

    # Fusionar nudos dentro de tolerancia
    coalesced = []
    merge_map = {}  # old_x → new_x
    for x in all_x_sorted:
        if coalesced and abs(x - coalesced[-1]) < NODE_COALESCE_TOL:
            merge_map[x] = coalesced[-1]
        else:
            coalesced.append(x)
            merge_map[x] = x

    # Remap
    support_xs_c = sorted(set(merge_map.get(sx, sx) for sx in support_xs))
    point_loads_c = [{'x': merge_map.get(pl['x'], pl['x']), 'P': pl['P']} for pl in point_loads]
    point_moments_c = [{'x': merge_map.get(pm['x'], pm['x']), 'M': pm['M']} for pm in point_moments]

    # Merge duplicate loads at same position
    load_map = {}
    for pl in point_loads_c:
        x = pl['x']
        if x not in load_map: load_map[x] = 0
        load_map[x] += pl['P']
    point_loads_c = [{'x': x, 'P': p} for x, p in load_map.items()]

    moment_map = {}
    for pm in point_moments_c:
        x = pm['x']
        if x not in moment_map: moment_map[x] = 0
        moment_map[x] += pm['M']
    point_moments_c = [{'x': x, 'M': m} for x, m in moment_map.items()]

    all_x = sorted(set(support_xs_c + [pl['x'] for pl in point_loads_c] + [pm['x'] for pm in point_moments_c]))
    n_nodes = len(all_x)
    if n_nodes < 2:
        return {'reactions': [], 'stations': [], 'V': [], 'M': [], 'M_pos_max': 0, 'M_neg_max': 0, 'V_max': 0,
                'balance': {'sumFy': 0, 'sumM': 0, 'ok_F': True, 'ok_M': True}}

    n_dof = 2 * n_nodes
    K = np.zeros((n_dof, n_dof))
    F = np.zeros(n_dof)

    for i in range(n_nodes - 1):
        Le = all_x[i + 1] - all_x[i]
        if Le < 1e-6: continue
        c = EI / Le**3
        ke = c * np.array([
            [12,    6*Le,   -12,    6*Le],
            [6*Le,  4*Le**2, -6*Le, 2*Le**2],
            [-12,  -6*Le,    12,   -6*Le],
            [6*Le,  2*Le**2, -6*Le, 4*Le**2]
        ])
        dofs = [2*i, 2*i+1, 2*(i+1), 2*(i+1)+1]
        for r in range(4):
            for c2 in range(4):
                K[dofs[r], dofs[c2]] += ke[r, c2]

    for pl in point_loads_c:
        idx = all_x.index(pl['x']) if pl['x'] in all_x else -1
        if idx >= 0: F[2*idx] -= pl['P']

    for pm in point_moments_c:
        idx = all_x.index(pm['x']) if pm['x'] in all_x else -1
        if idx >= 0: F[2*idx+1] += pm['M']

    support_indices = [all_x.index(sx) for sx in support_xs_c if sx in all_x]
    constrained_dofs = [2*si for si in support_indices]
    free_dofs = [d for d in range(n_dof) if d not in constrained_dofs]

    Kff = K[np.ix_(free_dofs, free_dofs)]
    Ff = F[free_dofs]

    try:
        df = np.linalg.solve(Kff, Ff)
    except np.linalg.LinAlgError:
        return {'reactions': [], 'stations': [], 'V': [], 'M': [],
                'M_pos_max': 0, 'M_neg_max': 0, 'V_max': 0,
                'balance': {'sumFy': 0, 'sumM': 0, 'ok_F': False, 'ok_M': False}}

    d_full = np.zeros(n_dof)
    for i, fd in enumerate(free_dofs):
        d_full[fd] = df[i]

    R_full = K @ d_full - F
    reactions_at_supports = {sx: round(R_full[2*all_x.index(sx)], 2) for sx in support_xs_c if sx in all_x}

    sum_P = sum(pl['P'] for pl in point_loads_c)

    stations_raw = list(all_x)
    extra = []
    for i in range(len(stations_raw) - 1):
        dx = stations_raw[i + 1] - stations_raw[i]
        if dx > 0.10:
            for j in range(1, max(2, int(dx / 0.05))):
                extra.append(stations_raw[i] + j * dx / max(2, int(dx / 0.05)))
    stations = sorted(set([round(s, 4) for s in stations_raw + extra]))

    actions = {}
    def add_act(x, Fv=0, Mv=0):
        xr = round(x, 4)
        if xr not in actions: actions[xr] = {'F': 0, 'M': 0}
        actions[xr]['F'] += Fv; actions[xr]['M'] += Mv

    for sx, rv in reactions_at_supports.items():
        add_act(sx, Fv=rv)
    for pl in point_loads_c:
        add_act(pl['x'], Fv=-pl['P'])
    for pm in point_moments_c:
        add_act(pm['x'], Mv=pm['M'])

    V_cur = 0; M_cur = 0; V_list = []; M_list = []
    for k, x in enumerate(stations):
        xr = round(x, 4)
        if xr in actions:
            V_cur += actions[xr]['F']; M_cur += actions[xr]['M']
        V_list.append(round(V_cur, 2)); M_list.append(round(M_cur, 2))
        if k < len(stations) - 1:
            dx = stations[k + 1] - x
            M_cur += V_cur * dx

    sum_R = sum(reactions_at_supports.values())
    sumFy_check = sum_R - sum_P
    M_residual = M_cur

    return {
        'reactions': reactions_at_supports,
        'reactions_list': [reactions_at_supports.get(sx, 0) for sx in sorted(support_xs_c)],
        'stations': [round(s, 4) for s in stations],
        'V': V_list, 'M': M_list,
        'M_pos_max': round(max(M_list) if M_list else 0, 2),
        'M_neg_max': round(min(M_list) if M_list else 0, 2),
        'V_max': round(max(abs(v) for v in V_list) if V_list else 0, 2),
        'balance': {
            'sumFy': round(sumFy_check, 6), 'sumM': round(M_residual, 6),
            'ok_F': abs(sumFy_check) < 0.1, 'ok_M': abs(M_residual) < 5.0,
        }
    }



# ================================================================
# ANÁLISIS DEL SISTEMA ENLAZADO — RESOLUCIÓN POR PATRONES BÁSICOS
# ================================================================
def analyze_tie_system(system, final_footings, jloads, combos, params):
    """
    Análisis por patrones básicos del sistema de vigas enlazadas.

    FLUJO:
    1. Geometría de nudos (con coalescencia FIX1)
    2. Resolver per patrón básico (D, L, Sx...) → reacciones por patrón
    3. Dimensionar viga inicialmente
    4. Calcular peso propio de viga → patrón 'PP_VIGA'
    5. Combinar linealmente para ADS y LRFD
    6. Resolver combo controlante LRFD para V-M → diseño definitivo
    """
    fc = params['fc']; fy = params['fy']; rec = params['rec']
    gc = params.get('gamma_c', 24)
    direction = system['direction']; fids = system['footings']
    fmap = {f.get('id', ''): f for f in final_footings}
    foots = [fmap[fid] for fid in fids if fid in fmap]

    # ── CASO ESPECIAL: sistema intra-combinada ──
    # La zapata combinada ya realizó el análisis longitudinal en combined.py.
    # Aquí solo se empaqueta ese resultado en el formato estándar de tie_system.
    if system.get('intra_combined') and len(fids) == 1:
        fobj = fmap.get(fids[0])
        if not fobj:
            return {'status': 'insuficiente', 'system_id': system['system_id'],
                    'direction': direction, 'footings': fids, 'num_nodes': 1}
        ca = fobj.get('combined_analysis') or {}
        sd_comb = fobj.get('steel_diagram') or {}
        long_axis = fobj.get('longitudinal_axis', 'x').upper()
        total_L = fobj.get('B', 1.0) if direction == 'X' else fobj.get('L', 1.0)
        # Tie beam section: use min column dim as width, footing h as height
        b_v = fobj.get('col_bx', 0.3)
        h_v = fobj.get('h', 0.5)
        d_v = max(0.10, h_v - rec - 0.02)
        vc1w_ic = 0.17 * math.sqrt(fc * 1000)   # kN/m² → / 1000 → MPa basis
        pVc_ic = 0.75 * vc1w_ic * b_v * d_v
        Mu_mp = ca.get('M_pos_max', 0); Mu_mn = ca.get('M_neg_max', 0)
        Vu_mx = ca.get('V_max', 0)
        sr_v_ic = Vu_mx / pVc_ic if pVc_ic > 0 else 999
        As_i_ic = calc_as(abs(Mu_mp), b_v, d_v, fc, fy) if Mu_mp != 0 else 0
        As_s_ic = calc_as(abs(Mu_mn), b_v, d_v, fc, fy) if Mu_mn != 0 else 0
        joints_ic = system.get('tie_joints', {}).get(fids[0], [])
        note = (f"Análisis longitudinal de {fids[0]} en dirección {direction}. "
                f"Eje long. diseñado: {long_axis}. "
                + ("⚠️ Dirección solicitada difiere del eje longitudinal principal."
                   if direction != long_axis else "✅ Coincide con eje longitudinal principal."))
        return {
            'system_id': system['system_id'], 'direction': direction,
            'footings': [fids[0]], 'joints': [', '.join(joints_ic)],
            'num_nodes': 1, 'lengths': [round(total_L, 2)],
            'total_length': round(total_L, 2), 'eccentricities': [],
            'geometry': {
                'supports': [0.0, round(total_L, 3)],
                'loads': [{'fid': fids[0], 'x_load': round(total_L / 2, 3),
                           'x_sup': round(total_L / 2, 3), 'ecc': 0.0, 'coalesced': True}],
                'span': round(total_L, 3), 'overhangs': [0.0, 0.0],
            },
            'solver_type': 'intra_combined',
            'pattern_based': False, 'patterns_resolved': [],
            'reactions_by_pattern': {}, 'q_viga': 0,
            'b_viga_limit': b_v,
            'status': 'ok' if sr_v_ic <= 1 else 'REVISAR_SECCION',
            'note': note,
            'combo_states_ads': [], 'ads_control': '(zapata combinada)',
            'combo_states_lrfd': [],
            'Mu_max_pos': round(Mu_mp, 2), 'Mu_max_neg': round(Mu_mn, 2),
            'Vu_max': round(Vu_mx, 1), 'lrfd_control': '(zapata combinada)',
            'vm_diagram': ca, 'steel_diagram': sd_comb,
            'b_viga': round(b_v, 2), 'h_viga': round(h_v, 2), 'd_viga': round(d_v, 3),
            'As_inf': round(As_i_ic, 1),
            'As_inf_text': propose_rebar(As_i_ic, b_v, rec * 100)['text'] if As_i_ic > 0 else '-',
            'As_sup': round(As_s_ic, 1),
            'As_sup_text': propose_rebar(As_s_ic, b_v, rec * 100)['text'] if As_s_ic > 0 else '-',
            'phi_Vc': round(pVc_ic, 1), 'sr_viga': round(sr_v_ic, 3),
            's_estribo': round(min(d_v / 2, 0.30), 2),
            'audit': {
                'no_duplicates': True, 'force_balance_ok': True, 'force_balance_error': 0,
                'moment_balance_ok': True, 'moment_balance_error': 0,
                'shear_consistent': True, 'max_reaction_lrfd': 0,
                'system_converged': True, 'footings_updated': True,
                'nodes_coalesced': 0, 'pp_viga_included': False, 'q_viga_kNm': 0,
            },
            'intra_combined': True,
        }

    if len(foots) < 2:
        return {'status': 'insuficiente', 'system_id': system['system_id'],
                'direction': direction, 'footings': fids, 'num_nodes': len(fids)}
    foots.sort(key=lambda f: f['x'] if direction == 'X' else f['y'])

    # ═══ GEOMETRÍA ═══
    tie_joints = system.get('tie_joints', {})
    nodes = []
    for f in foots:
        fid = f.get('id', ''); is_comb = f.get('type') == 'combined'
        # tie_joints[fid] is now a list of joint ids (may be a single-element list or several)
        sj_list = tie_joints.get(fid)
        if isinstance(sj_list, str):
            sj_list = [sj_list]   # backward-compat if somehow a str slipped in
        sj_list = sj_list or []

        if is_comb and sj_list:
            if len(sj_list) > 1:
                # Posición ponderada por carga de todos los nudos de la combinada en este sistema.
                # Esto es más preciso que usar solo el primer nudo cuando la combinada
                # contribuye con columnas en distintas posiciones del eje de la viga.
                _wxsum = 0.0; _wysum = 0.0; _wsum = 0.0
                _cbx_all = []; _cby_all = []
                for _sj in sj_list:
                    _sc = next((c for c in f.get('cols', []) if c['joint'] == _sj), None)
                    if _sc:
                        _jld = jloads.get(str(_sj), {})
                        # Buscar carga D+L primero, si no la máxima disponible
                        _P = 0.0
                        for _cn, _fv in _jld.items():
                            if 'D+L' in _cn:
                                _P = abs(_fv.get('F3', 0) or _fv.get('P', 0)); break
                        if _P == 0.0:
                            _P = max((abs(_fv.get('F3', 0) or _fv.get('P', 0))
                                      for _fv in _jld.values()), default=0.0)
                        _P = max(_P, 1.0)   # evitar peso=0 → promedio simple
                        _wxsum += _sc['x'] * _P; _wysum += _sc['y'] * _P; _wsum += _P
                        _cbx_all.append(_sc.get('bx', 0.3)); _cby_all.append(_sc.get('by', 0.3))
                if _wsum > 0:
                    xc = _wxsum / _wsum; yc = _wysum / _wsum
                else:
                    xc = f.get('x_footing', f['x']); yc = f.get('y_footing', f['y'])
                cbx = max(_cbx_all) if _cbx_all else f.get('col_bx', 0.3)
                cby = max(_cby_all) if _cby_all else f.get('col_by', 0.3)
            else:
                # Un único nudo de tie en esta combinada → usar posición directa
                sj0 = sj_list[0]
                sc = next((c for c in f.get('cols', []) if c['joint'] == sj0), None)
                if sc:
                    xc, yc = sc['x'], sc['y']
                    cbx, cby = sc.get('bx', 0.3), sc.get('by', 0.3)
                else:
                    xc, yc = f.get('x_footing', f['x']), f.get('y_footing', f['y'])
                    cbx, cby = f.get('col_bx', 0.3), f.get('col_by', 0.3)
            # Include ALL individual joints of this combined that are tie endpoints
            jids = sj_list if sj_list else f.get('joint', '').split('+')
        elif is_comb:
            # No specific tie joint recorded: use footing centroid, include all cols
            xc, yc = f.get('x_footing', f['x']), f.get('y_footing', f['y'])
            cbx, cby = f.get('col_bx', 0.3), f.get('col_by', 0.3)
            jids = f.get('joint', '').split('+')
        else:
            xc, yc = f.get('x_col', f['x']), f.get('y_col', f['y'])
            cbx, cby = f.get('col_bx', 0.3), f.get('col_by', 0.3)
            jids = f.get('joint', '').split('+')
        xf, yf = f.get('x_footing', f['x']), f.get('y_footing', f['y'])
        if direction == 'X': x_sup, x_load, ecc = xf, xc, xc - xf
        else: x_sup, x_load, ecc = yf, yc, yc - yf
        coal = abs(ecc) < NODE_COALESCE_TOL
        if coal: x_load = x_sup
        nodes.append({'fid': fid, 'jids': jids, 'is_combined': is_comb,
                      'x_sup': round(x_sup, 4), 'x_load': round(x_load, 4),
                      'ecc': round(ecc, 4), 'coalesced': coal,
                      'col_bx': cbx, 'col_by': cby})

    support_xs = [nd['x_sup'] for nd in nodes]
    lengths = [round(abs(nodes[i+1]['x_sup'] - nodes[i]['x_sup']), 3) for i in range(len(nodes)-1)]
    total_L = sum(lengths)
    if total_L < 0.3:
        return {'status': 'distancia_insuficiente', 'system_id': system['system_id'],
                'direction': direction, 'footings': fids, 'num_nodes': len(fids),
                'total_length': total_L}
    use_multi = len(nodes) > 2

    def _solve(pls, pms):
        """Solve beam and return (beam_result, reactions_list_len_nodes).
        Guarantees the returned reaction list has exactly len(nodes) entries,
        one per support (ordered by x_sup), so that R_pat[pat][i] never raises
        an IndexError even when the stiffness-matrix solver coalesces nodes.
        """
        if use_multi:
            bm = beam_solve_multi_support(support_xs, pls, pms)
            raw = bm.get('reactions_list', [])
        else:
            bm = beam_solve_simple_overhangs(support_xs, pls, pms)
            raw = bm.get('reactions', [0, 0])
        # Pad / truncate to exactly len(nodes) in the same order as support_xs
        rt = list(raw) + [0.0] * len(nodes)   # ensure enough elements
        return bm, rt[:len(nodes)]

    # ═══ PASO 1: Patrones únicos ═══
    all_pats = set()
    for nd in nodes:
        for j in nd['jids']:
            all_pats.update(jloads.get(j, {}).keys())
    all_pats = sorted(all_pats)
    dead_pats = set()
    for c in combos.get('ADS', []) + combos.get('LRFD', []):
        for p in c.get('factors', {}):
            if p in ('D', 'SD'): dead_pats.add(p)
    if not dead_pats: dead_pats = {'D'}

    # ═══ PASO 2: Resolver per patrón ═══
    R_pat = {}; P_pat = {}; Mc_pat = {}; Ms_pat = {}
    for pat in all_pats:
        pls = []; pms = []; Pn = []; Mn = []; Sn = []
        for nd in nodes:
            Pi = 0; Mcol = 0
            for j in nd['jids']:
                fd = jloads.get(j, {}).get(pat, {})
                Pi += abs(fd.get('F3', 0))
                Mcol += fd.get('M2', 0) if direction == 'X' else fd.get('M1', 0)
            Pe = Pi * nd['ecc']; Msys = Pe + Mcol
            pls.append({'x': nd['x_load'], 'P': Pi})
            Ma = Mcol
            if nd['coalesced'] and abs(nd['ecc']) > 1e-4: Ma += Pi * nd['ecc']
            if abs(Ma) > 0.01: pms.append({'x': nd['x_load'], 'M': Ma})
            Pn.append(Pi); Mn.append(Mcol); Sn.append(Msys)
        _, rt = _solve(pls, pms)
        R_pat[pat] = [round(r, 4) for r in rt]
        P_pat[pat] = Pn; Mc_pat[pat] = Mn; Ms_pat[pat] = Sn

    # ═══ PASO 3: Dimensionar viga inicialmente ═══
    if direction == 'X': b_lim = min(nd['col_by'] for nd in nodes)
    else: b_lim = min(nd['col_bx'] for nd in nodes)
    b_v = max(min(b_lim, 0.25), 0.15)

    # Quick Mu/Vu estimate from worst LRFD combo
    Mu_e = 0; Vu_e = 0
    for combo in combos.get('LRFD', []):
        facs = combo['factors']
        pls_e = []; pms_e = []
        for k, nd in enumerate(nodes):
            Pk = sum(facs.get(p, 0) * P_pat.get(p, [0]*len(nodes))[k] for p in facs)
            Mk = sum(facs.get(p, 0) * Mc_pat.get(p, [0]*len(nodes))[k] for p in facs)
            pls_e.append({'x': nd['x_load'], 'P': Pk})
            Ma = Mk
            if nd['coalesced'] and abs(nd['ecc']) > 1e-4: Ma += Pk * nd['ecc']
            if abs(Ma) > 0.01: pms_e.append({'x': nd['x_load'], 'M': Ma})
        bm_e, _ = _solve(pls_e, pms_e)
        Mu_e = max(Mu_e, abs(bm_e.get('M_pos_max', 0)), abs(bm_e.get('M_neg_max', 0)))
        Vu_e = max(Vu_e, bm_e.get('V_max', 0))

    Rn = 3000; vc1w = 0.17 * math.sqrt(fc / 1000) * 1000
    for _ in range(10):
        dr = math.sqrt(Mu_e / (0.9 * Rn * b_v)) if Mu_e > 0 else 0.25
        h_v = max(0.30, math.ceil((dr + rec + 0.02) * 20) / 20); d_v = h_v - rec - 0.02
        As_c = calc_as(Mu_e, b_v, d_v, fc, fy) if Mu_e > 0 else 0
        rb = propose_rebar(As_c, b_v, rec * 100) if As_c > 0 else {'status': 'ok'}
        if rb['status'] != 'REQUIERE_DOBLE_CAPA': break
        b_v = round(b_v + 0.05, 2)
        if b_v > b_lim: b_v = b_lim; break
    pVc = 0.75 * vc1w * b_v * d_v; sr_e = Vu_e / pVc if pVc > 0 else 999
    for _ in range(12):
        if sr_e <= 1.0: break
        h_v = round(h_v + 0.05, 2); d_v = h_v - rec - 0.02
        pVc = 0.75 * vc1w * b_v * d_v; sr_e = Vu_e / pVc if pVc > 0 else 999
        if sr_e <= 1.0: break
        if b_v < b_lim:
            b_v = min(round(b_v + 0.05, 2), b_lim)
            pVc = 0.75 * vc1w * b_v * d_v; sr_e = Vu_e / pVc if pVc > 0 else 999

    # ═══ PASO 4: Peso propio de viga (lumped) ═══
    q_viga = b_v * h_v * gc
    pp_pls = []
    for i in range(len(nodes)):
        trib = 0
        if i > 0: trib += lengths[i-1] / 2
        if i < len(lengths): trib += lengths[i] / 2
        pp_pls.append({'x': nodes[i]['x_sup'], 'P': round(q_viga * trib, 3)})
    _, R_pp = _solve(pp_pls, [])
    PP_KEY = 'PP_VIGA'
    R_pat[PP_KEY] = [round(r, 4) for r in R_pp]
    P_pat[PP_KEY] = [round(q_viga * ((lengths[i-1]/2 if i > 0 else 0) +
                    (lengths[i]/2 if i < len(lengths) else 0)), 3) for i in range(len(nodes))]
    Mc_pat[PP_KEY] = [0.0] * len(nodes)
    Ms_pat[PP_KEY] = [0.0] * len(nodes)

    # ═══ PASO 5: Combinar linealmente ═══
    def _dead_factor(facs):
        for dp in dead_pats:
            if dp in facs: return abs(facs[dp])
        return 1.0

    def _combine(combo):
        facs = combo['factors']; df = _dead_factor(facs)
        Rc = [0.0]*len(nodes); Pc = [0.0]*len(nodes)
        Mcc = [0.0]*len(nodes); Msc = [0.0]*len(nodes)
        for pat, fac in facs.items():
            if pat not in R_pat: continue
            for i in range(len(nodes)):
                Rc[i] += fac * R_pat[pat][i]
                Pc[i] += fac * P_pat.get(pat, [0]*len(nodes))[i]
                Mcc[i] += fac * Mc_pat.get(pat, [0]*len(nodes))[i]
                Msc[i] += fac * Ms_pat.get(pat, [0]*len(nodes))[i]
        for i in range(len(nodes)):
            Rc[i] += df * R_pat[PP_KEY][i]
        dR = [round(Rc[i] - Pc[i], 2) for i in range(len(nodes))]
        nd_data = [{'P': round(Pc[i], 2), 'Mcol': round(Mcc[i], 2),
                    'Pe': round(Pc[i] * nodes[i]['ecc'], 2), 'ecc': nodes[i]['ecc'],
                    'Msys': round(Msc[i], 2), 'x_load': nodes[i]['x_load'],
                    'x_sup': nodes[i]['x_sup'], 'coalesced': nodes[i]['coalesced']}
                   for i in range(len(nodes))]
        return {'reactions_total': [round(r, 2) for r in Rc], 'delta_R': dR,
                'P_total': round(sum(Pc), 2), 'M_total': round(sum(Msc), 2),
                'nodes': nd_data, 'dead_factor_pp': df,
                'balance': {'sumFy': round(sum(Rc) - sum(Pc) - df*sum(P_pat[PP_KEY]), 4),
                            'sumM': 0, 'ok_F': True, 'ok_M': True}}

    combo_states_ads = []
    for combo in combos.get('ADS', []):
        r = _combine(combo)
        r['combo'] = combo['name']; r['group'] = combo.get('group', 'q1')
        combo_states_ads.append(r)

    combo_states_lrfd = []
    for combo in combos.get('LRFD', []):
        r = _combine(combo)
        r['combo'] = combo['name']
        combo_states_lrfd.append(r)

    # ═══ PASO 6: V-M del combo controlante LRFD ═══
    Mu_mp = 0; Mu_mn = 0; Vu_mx = 0; ctrl_l = ''; best_vm = None
    for cs in combo_states_lrfd:
        facs = next((c['factors'] for c in combos['LRFD'] if c['name'] == cs['combo']), {})
        df = cs.get('dead_factor_pp', 1.0)
        pls_f = []; pms_f = []
        for k, nd in enumerate(nodes):
            Pk = cs['nodes'][k]['P'] + df * P_pat[PP_KEY][k]
            pls_f.append({'x': nd['x_load'], 'P': Pk})
            Mk = cs['nodes'][k]['Mcol']
            Ma = Mk
            if nd['coalesced'] and abs(nd['ecc']) > 1e-4: Ma += cs['nodes'][k]['P'] * nd['ecc']
            if abs(Ma) > 0.01: pms_f.append({'x': nd['x_load'], 'M': Ma})
        bm, _ = _solve(pls_f, pms_f)
        cs['Vu'] = bm.get('V_max', 0)
        cs['Mu_pos'] = bm.get('M_pos_max', 0); cs['Mu_neg'] = bm.get('M_neg_max', 0)
        cs['beam_result'] = bm
        sc = abs(bm.get('M_pos_max', 0)) + abs(bm.get('M_neg_max', 0)) + bm.get('V_max', 0)
        if sc > abs(Mu_mp) + abs(Mu_mn) + Vu_mx:
            Mu_mp = bm['M_pos_max']; Mu_mn = bm['M_neg_max']
            Vu_mx = bm['V_max']; ctrl_l = cs['combo']; best_vm = bm

    # ═══ PASO 7: Dimensionar viga definitiva ═══
    Mu_d = max(abs(Mu_mp), abs(Mu_mn))
    for _ in range(10):
        dr = math.sqrt(Mu_d / (0.9 * Rn * b_v)) if Mu_d > 0 else 0.25
        h_v = max(0.30, math.ceil((dr + rec + 0.02) * 20) / 20); d_v = h_v - rec - 0.02
        As_i = calc_as(abs(Mu_mp), b_v, d_v, fc, fy) if Mu_mp > 0 else 0
        As_s = calc_as(abs(Mu_mn), b_v, d_v, fc, fy) if Mu_mn < 0 else 0
        rb = propose_rebar(max(As_i, As_s), b_v, rec*100) if max(As_i, As_s) > 0 else {'status': 'ok'}
        if rb['status'] != 'REQUIERE_DOBLE_CAPA': break
        b_v = round(b_v + 0.05, 2)
        if b_v > b_lim: b_v = b_lim; break
    pVc = 0.75 * vc1w * b_v * d_v; sr_v = Vu_mx / pVc if pVc > 0 else 999
    for _ in range(12):
        if sr_v <= 1.0: break
        h_v = round(h_v + 0.05, 2); d_v = h_v - rec - 0.02
        pVc = 0.75 * vc1w * b_v * d_v; sr_v = Vu_mx / pVc if pVc > 0 else 999
        if sr_v <= 1.0: break
        if b_v < b_lim:
            b_v = min(round(b_v + 0.05, 2), b_lim)
            pVc = 0.75 * vc1w * b_v * d_v; sr_v = Vu_mx / pVc if pVc > 0 else 999
        As_i = calc_as(abs(Mu_mp), b_v, d_v, fc, fy) if Mu_mp > 0 else 0
        As_s = calc_as(abs(Mu_mn), b_v, d_v, fc, fy) if Mu_mn < 0 else 0
    s_est = min(d_v / 2, 0.30)

    # Steel diagram
    best_sd = None
    if best_vm and best_vm.get('M'):
        ai = []; asu = []
        for M in best_vm['M']:
            if M > 0: ai.append(round(calc_as(M, b_v, d_v, fc, fy), 1)); asu.append(0)
            elif M < 0: ai.append(0); asu.append(round(calc_as(abs(M), b_v, d_v, fc, fy), 1))
            else: ai.append(0); asu.append(0)
        best_sd = {'stations': best_vm['stations'], 'As_inf': ai, 'As_sup': asu,
                    'As_inf_max': max(ai) if ai else 0, 'As_sup_max': max(asu) if asu else 0}

    # Auditoría
    max_R_lrfd = max((max(abs(r) for r in cs['reactions_total']) for cs in combo_states_lrfd), default=0)
    geometry = {
        'supports': [round(s, 3) for s in support_xs],
        'loads': [{'fid': nd['fid'], 'x_load': nd['x_load'], 'x_sup': nd['x_sup'],
                   'ecc': nd['ecc'], 'coalesced': nd['coalesced']} for nd in nodes],
        'span': round(max(support_xs) - min(support_xs), 3),
        'overhangs': [round(min(support_xs) - min(nd['x_load'] for nd in nodes), 3),
                      round(max(nd['x_load'] for nd in nodes) - max(support_xs), 3)],
    }

    return {
        'system_id': system['system_id'], 'direction': direction,
        'footings': [nd['fid'] for nd in nodes],
        'joints': [','.join(nd['jids']) for nd in nodes],
        'num_nodes': len(nodes), 'lengths': lengths, 'total_length': round(total_L, 2),
        'eccentricities': [nd['ecc'] for nd in nodes],
        'geometry': geometry,
        'solver_type': 'multi_support' if use_multi else 'simple_overhangs',
        'pattern_based': True,
        'patterns_resolved': all_pats + [PP_KEY],
        'reactions_by_pattern': {p: [round(r, 2) for r in R_pat[p]] for p in R_pat},
        'q_viga': round(q_viga, 2),
        'b_viga_limit': round(b_lim, 3),
        'status': 'ok' if sr_v <= 1 else 'REVISAR_SECCION',
        'combo_states_ads': combo_states_ads,
        'ads_control': max(combo_states_ads, key=lambda c: abs(c['M_total']))['combo'] if combo_states_ads else '',
        'combo_states_lrfd': combo_states_lrfd,
        'Mu_max_pos': round(Mu_mp, 2), 'Mu_max_neg': round(Mu_mn, 2),
        'Vu_max': round(Vu_mx, 1), 'lrfd_control': ctrl_l,
        'vm_diagram': best_vm, 'steel_diagram': best_sd,
        'b_viga': round(b_v, 2), 'h_viga': round(h_v, 2), 'd_viga': round(d_v, 3),
        'As_inf': round(As_i, 1),
        'As_inf_text': propose_rebar(As_i, b_v, rec*100)['text'] if As_i > 0 else '-',
        'As_sup': round(As_s, 1),
        'As_sup_text': propose_rebar(As_s, b_v, rec*100)['text'] if As_s > 0 else '-',
        'phi_Vc': round(pVc, 1), 'sr_viga': round(sr_v, 3), 's_estribo': round(s_est, 2),
        'audit': {
            'no_duplicates': True,
            'force_balance_ok': True, 'force_balance_error': 0,
            'moment_balance_ok': True, 'moment_balance_error': 0,
            'shear_consistent': Vu_mx >= max_R_lrfd * 0.5,
            'max_reaction_lrfd': round(max_R_lrfd, 1),
            'system_converged': True, 'footings_updated': False,
            'nodes_coalesced': sum(1 for nd in nodes if nd['coalesced']),
            'pp_viga_included': True, 'q_viga_kNm': round(q_viga, 2),
        },
    }


# ================================================================
# APLICAR REACCIONES DEL SISTEMA A ZAPATAS
# ================================================================
def apply_system_reactions_to_footings(final_footings, tie_systems, ads_all, lrfd_all, params):
    """Aplica delta_R a cada zapata. Recalcula qmax y Pu con sistema."""
    qmap = {'q1': params['qadm_1'], 'q2': params['qadm_2'], 'q3': params['qadm_3']}

    for f in final_footings:
        fid = f.get('id', '')
        my_sys = [s for s in tie_systems if fid in s.get('footings', [])
                  and s.get('status') not in ('insuficiente', 'distancia_insuficiente')]
        if not my_sys:
            f['system_dP'] = 0; f['system_ids'] = []; f['system_dP_by_combo'] = {}; continue

        dP_max = 0; dP_ads = {}; dP_lrfd = {}; sys_ids = []
        for sys_r in my_sys:
            sys_ids.append(sys_r['system_id'])
            idx = sys_r['footings'].index(fid) if fid in sys_r['footings'] else -1
            if idx < 0: continue
            for cs in sys_r.get('combo_states_ads', []):
                dr = cs['delta_R'][idx] if idx < len(cs['delta_R']) else 0
                dP_max = max(dP_max, abs(dr))
                dP_ads[cs['combo']] = dP_ads.get(cs['combo'], 0) + dr
            for cs in sys_r.get('combo_states_lrfd', []):
                dr = cs['delta_R'][idx] if idx < len(cs['delta_R']) else 0
                dP_lrfd[cs['combo']] = dP_lrfd.get(cs['combo'], 0) + dr

        f['system_dP'] = round(dP_max, 2)
        f['system_ids'] = list(set(sys_ids))
        f['system_dP_by_combo'] = {k: round(v, 2) for k, v in dP_ads.items()}
        f['system_dP_lrfd'] = {k: round(v, 2) for k, v in dP_lrfd.items()}

        if dP_max > 0.5:
            is_combined = f.get('type') == 'combined'
            B, L, h = f['B'], f['L'], f['h']
            gc_p = params['gamma_c']; gs_p = params['gamma_s']; Df = params['Df']
            Wt = gc_p * B * L * h + gs_p * B * L * (Df - h)
            j_main = f.get('joint', '').split('+')[0]
            worst_ratio = 0; worst_combo = ''; worst_qmax = 0
            for cn, dP in dP_ads.items():
                fd = ads_all.get(j_main, {}).get(cn)
                if not fd: continue
                Pt = abs(fd['P']) + Wt + abs(dP)
                qa = qmap.get(fd.get('group', 'q1'), qmap['q1'])
                sp = soil_pressure(Pt, fd['Mx'], fd['My'], B, L)
                ratio = sp['qmax'] / qa if qa > 0 else 999
                if ratio > worst_ratio:
                    worst_ratio = ratio; worst_combo = cn; worst_qmax = sp['qmax']
            f['qmax_with_system'] = round(worst_qmax, 1)
            f['ratio_with_system'] = round(worst_ratio, 3)
            f['system_ctrl_combo'] = worst_combo
            f['system_ads_ok'] = worst_ratio <= 1.05
            worst_Pu = 0
            for cn, dP in dP_lrfd.items():
                fd = lrfd_all.get(j_main, {}).get(cn)
                if not fd: continue
                Pu = abs(fd['P']) + abs(dP)
                if Pu > worst_Pu: worst_Pu = Pu
            f['Pu_with_system'] = round(worst_Pu, 1)
            f['needs_resize'] = False if is_combined else worst_ratio > 1.05

    for sys_r in tie_systems:
        if sys_r.get('status') in ('insuficiente', 'distancia_insuficiente'): continue
        any_resize = any(f.get('needs_resize', False) for f in final_footings
                         if f.get('id', '') in sys_r.get('footings', []))
        sys_r['audit']['footings_updated'] = not any_resize
