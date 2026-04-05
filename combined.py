"""
combined.py — Módulo de zapatas combinadas

Funciones:
  check_overlaps                  — Detecta solapamientos y agrupa clusters
  analyze_combined_longitudinal   — Análisis V-M como viga sobre suelo (Bowles)
  compute_steel_diagram           — Diagrama de acero requerido por estación
  design_combined_footing         — Diseño completo de zapata combinada (extraído de run_design)
"""
import math
import numpy as np
from collections import defaultdict
from isolated import full_structural_design, calc_as


def check_overlaps(footings, min_gap=0.10):
    """Detecta solapamientos y agrupa en clusters."""
    ovs = []
    for i in range(len(footings)):
        ri = footings[i]['rect']
        rie = [ri[0] - min_gap / 2, ri[1] - min_gap / 2, ri[2] + min_gap / 2, ri[3] + min_gap / 2]
        for j in range(i + 1, len(footings)):
            rj = footings[j]['rect']
            if not (rie[2] <= rj[0] or rj[2] <= rie[0] or rie[3] <= rj[1] or rj[3] <= rie[1]):
                ovs.append((i, j))
    # BFS para agrupar
    adj = defaultdict(list)
    for i, j in ovs: adj[i].append(j); adj[j].append(i)
    visited = set(); groups = []
    for i in range(len(footings)):
        if i in visited: continue
        if i not in adj: groups.append([i]); visited.add(i); continue
        q = [i]; g = []
        while q:
            n = q.pop(0)
            if n in visited: continue
            visited.add(n); g.append(n)
            for nb in adj[n]:
                if nb not in visited: q.append(nb)
        groups.append(g)
    return ovs, groups


def analyze_combined_longitudinal(x_left, x_right, B_trans, columns, P_cols, q_uniform, d):
    """
    Analiza zapata combinada como viga sobre reacción uniforme de suelo.
    columns: lista de dicts con 'x','bx'
    P_cols: cargas puntuales por columna (kN)
    q_uniform: presión (kPa), w = q*B_trans kN/m
    """
    w = q_uniform * B_trans
    stations = sorted(set([x_left, x_right] + [
        v for col in columns for v in [
            col['x'] - col['bx'] / 2, col['x'], col['x'] + col['bx'] / 2,
            col['x'] - col['bx'] / 2 - d, col['x'] + col['bx'] / 2 + d,
        ] if x_left <= v <= x_right
    ]))
    extra = []
    for i in range(len(stations) - 1):
        dx = stations[i + 1] - stations[i]
        if dx > 0.15:
            for j in range(1, max(2, int(dx / 0.10))):
                extra.append(stations[i] + j * dx / max(2, int(dx / 0.10)))
    stations = sorted(set(stations + extra))

    V_list = []; M_list = []
    for xp in stations:
        R_soil = w * (xp - x_left)
        P_left = sum(P_cols[i] for i, col in enumerate(columns) if col['x'] <= xp + 0.001)
        V = R_soil - P_left
        M_soil = w * (xp - x_left)**2 / 2
        M_cols = sum(P_cols[i] * (xp - col['x']) for i, col in enumerate(columns) if col['x'] <= xp + 0.001)
        M = M_soil - M_cols
        V_list.append(round(V, 2)); M_list.append(round(M, 2))

    return {
        'stations': [round(s, 3) for s in stations], 'V': V_list, 'M': M_list,
        'M_pos_max': round(max(M_list) if M_list else 0, 2),
        'M_neg_max': round(min(M_list) if M_list else 0, 2),
        'V_max': round(max(abs(v) for v in V_list) if V_list else 0, 2),
        'w': round(w, 2)
    }


def compute_steel_diagram(long_analysis, b_trans, d, fc, fy):
    """As requerido en cada estación: inf (M+) y sup (M-)."""
    As_inf = []; As_sup = []
    for M in long_analysis.get('M', []):
        if M > 0:
            As_inf.append(round(calc_as(M, b_trans, d, fc, fy), 1)); As_sup.append(0)
        elif M < 0:
            As_inf.append(0); As_sup.append(round(calc_as(abs(M), b_trans, d, fc, fy), 1))
        else:
            As_inf.append(0); As_sup.append(0)
    return {
        'stations': long_analysis.get('stations', []),
        'As_inf': As_inf, 'As_sup': As_sup,
        'As_inf_max': max(As_inf) if As_inf else 0,
        'As_sup_max': max(As_sup) if As_sup else 0
    }


def design_combined_footing(grp_indices, footings, group_cols, ads_all, lrfd_all, params, cidx):
    """
    Diseña una zapata combinada a partir de un grupo de zapatas solapadas.

    Args:
        grp_indices: lista de índices en footings[]
        footings: lista completa de zapatas pre-optimizadas
        group_cols: columnas del grupo
        ads_all: fuerzas ADS por nudo
        lrfd_all: fuerzas LRFD por nudo
        params: parámetros de diseño
        cidx: índice para ID de combinada

    Returns:
        dict con resultado de diseño completo + metadatos
    """
    dmin = params.get('dim_min', 0.60)

    col_forces_all = {}
    source_locations = []
    for g in grp_indices:
        fg = footings[g]
        col_forces_all.update(fg.get('column_forces', {}))
        source_locations.append({
            'joint': fg['joint'],
            'location': fg.get('classification', {}).get('location', 'concentrica'),
            'side': fg.get('classification', {}).get('side', ''),
            'corner': fg.get('classification', {}).get('corner', ''),
            'scheme': fg.get('scheme', 'aislada'),
        })

    # ── Heredar restricciones de borde/esquina ──
    edge_sides = set(); corners = set()
    has_edge = False; has_corner = False; has_eccentric = False
    for sl in source_locations:
        if sl['location'] == 'medianera' and sl['side']:
            edge_sides.add(sl['side']); has_edge = True
        elif sl['location'] == 'esquinera' and sl['corner']:
            corners.add(sl['corner']); has_corner = True
        elif sl['location'] in ('excentrica', 'medianera', 'esquinera'):
            has_eccentric = True

    # ── Envolvente por BORDES REALES de columnas ──
    margin = 0.15
    xmins_c = [c['x'] - c['bx'] / 2 for c in group_cols]
    xmaxs_c = [c['x'] + c['bx'] / 2 for c in group_cols]
    ymins_c = [c['y'] - c['by'] / 2 for c in group_cols]
    ymaxs_c = [c['y'] + c['by'] / 2 for c in group_cols]
    env_xmin = min(xmins_c); env_xmax = max(xmaxs_c)
    env_ymin = min(ymins_c); env_ymax = max(ymaxs_c)

    B_min = env_xmax - env_xmin + 2 * margin
    L_min = env_ymax - env_ymin + 2 * margin

    # ── Centro: ponderado pero RESTRINGIDO por herencia ──
    P_w = []
    for c in group_cols:
        j = c['joint']; fd = ads_all.get(j, {})
        Pdl = 0
        for cn, fv in fd.items():
            if 'D+L' in cn: Pdl = abs(fv['P']); break
        if Pdl == 0: Pdl = max((abs(fv['P']) for fv in fd.values()), default=1)
        P_w.append(Pdl)
    Ptot = sum(P_w)
    if Ptot > 0:
        xR = sum(group_cols[i]['x'] * P_w[i] for i in range(len(group_cols))) / Ptot
        yR = sum(group_cols[i]['y'] * P_w[i] for i in range(len(group_cols))) / Ptot
    else:
        xR = np.mean([c['x'] for c in group_cols])
        yR = np.mean([c['y'] for c in group_cols])

    qa_ref = params['qadm_1']
    A_req = Ptot * 1.10 / qa_ref if qa_ref > 0 else B_min * L_min

    B_comb = max(dmin, B_min, A_req / L_min if L_min > 0 else B_min)
    L_comb = max(dmin, L_min, A_req / B_comb if B_comb > 0 else L_min)
    B_comb = math.ceil(B_comb * 20) / 20; L_comb = math.ceil(L_comb * 20) / 20
    h_comb = max(params['h_min'], 0.15 * max(B_comb, L_comb))
    h_comb = math.ceil(h_comb * 20) / 20

    # ── PIN ESTRICTO a linderos ──
    xR_final = xR; yR_final = yR
    margin_borde = 0.05
    pinned_x = False; pinned_y = False

    for sl in source_locations:
        jid_s = sl['joint']
        col_s = next((c for c in group_cols if c['joint'] == jid_s), None)
        if not col_s: continue
        side_s = sl.get('side', ''); corner_s = sl.get('corner', '')
        if sl['location'] in ('medianera', 'esquinera'):
            if side_s == 'X-' or (corner_s and 'X-' in corner_s):
                pin_x = col_s['x'] - col_s['bx'] / 2 - margin_borde
                xR_final = pin_x + B_comb / 2; pinned_x = True
            elif side_s == 'X+' or (corner_s and 'X+' in corner_s):
                pin_x = col_s['x'] + col_s['bx'] / 2 + margin_borde
                xR_final = pin_x - B_comb / 2; pinned_x = True
            if side_s == 'Y-' or (corner_s and 'Y-' in corner_s):
                pin_y = col_s['y'] - col_s['by'] / 2 - margin_borde
                yR_final = pin_y + L_comb / 2; pinned_y = True
            elif side_s == 'Y+' or (corner_s and 'Y+' in corner_s):
                pin_y = col_s['y'] + col_s['by'] / 2 + margin_borde
                yR_final = pin_y - L_comb / 2; pinned_y = True

    # Contención
    req_xmin = env_xmin - margin; req_xmax = env_xmax + margin
    req_ymin = env_ymin - margin; req_ymax = env_ymax + margin
    zap_xmin = xR_final - B_comb / 2; zap_xmax = xR_final + B_comb / 2
    zap_ymin = yR_final - L_comb / 2; zap_ymax = yR_final + L_comb / 2

    def _repin_x():
        nonlocal xR_final
        for sl in source_locations:
            col_s = next((c for c in group_cols if c['joint'] == sl['joint']), None)
            if not col_s: continue
            ss = sl.get('side', ''); cs = sl.get('corner', '')
            if ss == 'X-' or (cs and 'X-' in cs):
                xR_final = round(col_s['x'] - col_s['bx'] / 2 - margin_borde + B_comb / 2, 3); return
            elif ss == 'X+' or (cs and 'X+' in cs):
                xR_final = round(col_s['x'] + col_s['bx'] / 2 + margin_borde - B_comb / 2, 3); return

    def _repin_y():
        nonlocal yR_final
        for sl in source_locations:
            col_s = next((c for c in group_cols if c['joint'] == sl['joint']), None)
            if not col_s: continue
            ss = sl.get('side', ''); cs = sl.get('corner', '')
            if ss == 'Y-' or (cs and 'Y-' in cs):
                yR_final = round(col_s['y'] - col_s['by'] / 2 - margin_borde + L_comb / 2, 3); return
            elif ss == 'Y+' or (cs and 'Y+' in cs):
                yR_final = round(col_s['y'] + col_s['by'] / 2 + margin_borde - L_comb / 2, 3); return

    if zap_xmin > req_xmin or zap_xmax < req_xmax:
        if pinned_x:
            need_left = max(0, zap_xmin - req_xmin); need_right = max(0, req_xmax - zap_xmax)
            B_comb = math.ceil((B_comb + need_left + need_right) * 20) / 20
            _repin_x()
        else:
            if xR_final - B_comb / 2 > req_xmin: xR_final = req_xmin + B_comb / 2
            if xR_final + B_comb / 2 < req_xmax: xR_final = req_xmax - B_comb / 2

    if zap_ymin > req_ymin or zap_ymax < req_ymax:
        if pinned_y:
            need_bot = max(0, zap_ymin - req_ymin); need_top = max(0, req_ymax - zap_ymax)
            L_comb = math.ceil((L_comb + need_bot + need_top) * 20) / 20
            _repin_y()
        else:
            if yR_final - L_comb / 2 > req_ymin: yR_final = req_ymin + L_comb / 2
            if yR_final + L_comb / 2 < req_ymax: yR_final = req_ymax - L_comb / 2

    xR_final = round(xR_final, 3); yR_final = round(yR_final, 3)

    # ── Transportar fuerzas al centroide ──
    ads_c = {}
    for cn in ads_all.get(group_cols[0]['joint'], {}).keys():
        Pt = 0; Mxt = 0; Myt = 0; Vxt = 0; Vyt = 0; gv = 'q1'
        for c in group_cols:
            j = c['joint']
            fd = ads_all.get(j, {}).get(cn, {'P': 0, 'Mx': 0, 'My': 0, 'Vx': 0, 'Vy': 0, 'group': 'q1'})
            Pt += abs(fd['P'])
            Mxt += fd['Mx'] + abs(fd['P']) * (c['y'] - yR_final)
            Myt += fd['My'] + abs(fd['P']) * (c['x'] - xR_final)
            Vxt += fd['Vx']; Vyt += fd['Vy']; gv = fd.get('group', gv)
        ads_c[cn] = {'P': Pt, 'Mx': Mxt, 'My': Myt, 'Vx': Vxt, 'Vy': Vyt, 'group': gv}

    lrfd_c = {}
    for cn in lrfd_all.get(group_cols[0]['joint'], {}).keys():
        Pt = 0; Mxt = 0; Myt = 0; Vxt = 0; Vyt = 0
        for c in group_cols:
            j = c['joint']
            fd = lrfd_all.get(j, {}).get(cn, {'P': 0, 'Mx': 0, 'My': 0, 'Vx': 0, 'Vy': 0})
            Pt += abs(fd['P'])
            Mxt += fd['Mx'] + abs(fd['P']) * (c['y'] - yR_final)
            Myt += fd['My'] + abs(fd['P']) * (c['x'] - xR_final)
            Vxt += fd['Vx']; Vyt += fd['Vy']
        lrfd_c[cn] = {'P': Pt, 'Mx': Mxt, 'My': Myt, 'Vx': Vxt, 'Vy': Vyt}

    # ── Iterar dimensiones ──
    cbx = max(c['bx'] for c in group_cols); cby = max(c['by'] for c in group_cols)
    for _iter in range(20):
        r = full_structural_design(
            '+'.join(c['joint'] for c in group_cols),
            xR_final, yR_final, B_comb, L_comb, h_comb, cbx, cby, ads_c, lrfd_c, params, col_forces_all)
        if r['st'] not in ('NO_CUMPLE', 'REVISAR_h'): break
        if r['st'] == 'NO_CUMPLE':
            sc = 1.1
            B_comb = math.ceil(B_comb * sc * 20) / 20
            L_comb = math.ceil(L_comb * sc * 20) / 20
            if pinned_x: _repin_x()
            else:
                if xR_final - B_comb / 2 > req_xmin: xR_final = round(req_xmin + B_comb / 2, 3)
                if xR_final + B_comb / 2 < req_xmax: xR_final = round(req_xmax - B_comb / 2, 3)
            if pinned_y: _repin_y()
            else:
                if yR_final - L_comb / 2 > req_ymin: yR_final = round(req_ymin + L_comb / 2, 3)
                if yR_final + L_comb / 2 < req_ymax: yR_final = round(req_ymax - L_comb / 2, 3)
        elif r['st'] == 'REVISAR_h':
            h_comb = math.ceil((h_comb + 0.05) * 20) / 20

    # ── Validación FINAL de contención ──
    zap_xmin = xR_final - B_comb / 2; zap_xmax = xR_final + B_comb / 2
    zap_ymin = yR_final - L_comb / 2; zap_ymax = yR_final + L_comb / 2
    cont_details = []; all_contained = True
    for c in group_cols:
        c_xmin = c['x'] - c['bx'] / 2; c_xmax = c['x'] + c['bx'] / 2
        c_ymin = c['y'] - c['by'] / 2; c_ymax = c['y'] + c['by'] / 2
        ok = (c_xmin >= zap_xmin - 0.01 and c_xmax <= zap_xmax + 0.01 and
              c_ymin >= zap_ymin - 0.01 and c_ymax <= zap_ymax + 0.01)
        if not ok: all_contained = False
        cont_details.append({'joint': c['joint'], 'contained': ok,
                             'col_rect': [round(c_xmin, 3), round(c_ymin, 3), round(c_xmax, 3), round(c_ymax, 3)]})

    if not all_contained:
        for cd in cont_details:
            if not cd['contained']:
                cr = cd['col_rect']
                if cr[0] < zap_xmin: B_comb = math.ceil((B_comb + (zap_xmin - cr[0] + margin)) * 20) / 20
                if cr[2] > zap_xmax: B_comb = math.ceil((B_comb + (cr[2] - zap_xmax + margin)) * 20) / 20
                if cr[1] < zap_ymin: L_comb = math.ceil((L_comb + (zap_ymin - cr[1] + margin)) * 20) / 20
                if cr[3] > zap_ymax: L_comb = math.ceil((L_comb + (cr[3] - zap_ymax + margin)) * 20) / 20
        r = full_structural_design(
            '+'.join(c['joint'] for c in group_cols),
            xR_final, yR_final, B_comb, L_comb, h_comb, cbx, cby, ads_c, lrfd_c, params, col_forces_all)
        all_contained = True

    # ── Esquema heredado ──
    if has_edge and has_corner: inherited_scheme = 'combinada_restringida'
    elif has_corner: inherited_scheme = 'combinada_esquinera'
    elif has_edge: inherited_scheme = 'combinada_medianera'
    else: inherited_scheme = 'combinada'

    r['id'] = f'ZC-{cidx:02d}'; r['type'] = 'combined'
    r['cols'] = group_cols; r['replaces'] = [footings[g]['id'] for g in grp_indices]
    r['scheme'] = inherited_scheme; r['location'] = {'location': inherited_scheme}
    r['x_footing'] = xR_final; r['y_footing'] = yR_final

    # ── Análisis longitudinal V-M (Bowles) ──
    xs_g = [c['x'] for c in group_cols]; ys_g = [c['y'] for c in group_cols]
    range_x = max(xs_g) - min(xs_g) if len(xs_g) > 1 else 0
    range_y = max(ys_g) - min(ys_g) if len(ys_g) > 1 else 0
    long_axis = 'x' if range_x >= range_y else 'y'

    best_long = None; best_Mpos = 0; best_sd = None
    d_comb = h_comb - params['rec'] - 0.016; d_comb = max(d_comb, 0.10)
    for cn in lrfd_c.keys():
        P_cols_vm = []; col_geom_vm = []
        for c in group_cols:
            j = c['joint']; fd = lrfd_all.get(j, {}).get(cn, {'P': 0})
            P_cols_vm.append(abs(fd['P']))
            col_geom_vm.append({'x': c['x'] if long_axis == 'x' else c['y'],
                                'bx': c['bx'] if long_axis == 'x' else c['by']})
        Pu_t = lrfd_c[cn]['P']
        qu_vm = Pu_t / (B_comb * L_comb) if B_comb * L_comb > 0 else 0
        if long_axis == 'x':
            xl = xR_final - B_comb / 2; xr = xR_final + B_comb / 2; bt = L_comb
        else:
            xl = yR_final - L_comb / 2; xr = yR_final + L_comb / 2; bt = B_comb
        la = analyze_combined_longitudinal(xl, xr, bt, col_geom_vm, P_cols_vm, qu_vm, d_comb)
        if abs(la['M_pos_max']) > best_Mpos:
            best_Mpos = abs(la['M_pos_max']); best_long = la
    if best_long:
        bt_vm = L_comb if long_axis == 'x' else B_comb
        best_sd = compute_steel_diagram(best_long, bt_vm, d_comb, params['fc'], params['fy'])

    r['combined_analysis'] = best_long
    r['steel_diagram'] = best_sd
    r['longitudinal_axis'] = long_axis
    r['As_long_top'] = best_sd['As_sup_max'] if best_sd else 0
    r['containment'] = {
        'all_contained': all_contained,
        'B_min_env': round(B_min, 3), 'L_min_env': round(L_min, 3),
        'details': cont_details,
    }
    r['combined_constraints'] = {
        'has_edge_constraint': has_edge, 'edge_sides': list(edge_sides),
        'has_corner_constraint': has_corner, 'corners': list(corners),
        'has_eccentric_columns': has_eccentric, 'source_locations': source_locations,
    }
    return r
