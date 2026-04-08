"""
isolated.py v3 — Módulo de diseño de zapatas aisladas

FASE 3 — Nuevas funciones:
  infer_column_axis       — Posición del eje de columna según clasificación
  overturning_check       — FS volcamiento en X e Y
  sliding_check           — FS deslizamiento en X e Y

Funciones existentes:
  soil_pressure             — Modelo de presiones Bowles (contacto total/parcial)
  punching_with_moment      — Punzonamiento ACI 318 con momento no balanceado
  factored_pressure_at_face — Presión factorizada en cara de columna
  calc_as                   — Acero requerido por flexión
  propose_rebar             — Propuesta de refuerzo (barras y separación)
  optimize_isolated         — Optimización por barrido B×L×h (menor volumen)
  full_structural_design    — Auditoría completa ADS + LRFD con dimensiones fijas
"""
import math


# ================================================================
# INFERENCIA AUTOMÁTICA DE POSICIÓN DE COLUMNA
# ================================================================
def infer_column_axis(x_z, y_z, B, L, bx, by, classification):
    loc = classification.get('location', 'concentrica')
    side = classification.get('side', '')
    corner = classification.get('corner', '')
    x_col = x_z; y_col = y_z
    if loc == 'medianera':
        if side == 'X+': x_col = x_z + (B / 2 - bx / 2)
        elif side == 'X-': x_col = x_z - (B / 2 - bx / 2)
        elif side == 'Y+': y_col = y_z + (L / 2 - by / 2)
        elif side == 'Y-': y_col = y_z - (L / 2 - by / 2)
    elif loc == 'esquinera':
        if 'X+' in corner: x_col = x_z + (B / 2 - bx / 2)
        elif 'X-' in corner: x_col = x_z - (B / 2 - bx / 2)
        if 'Y+' in corner: y_col = y_z + (L / 2 - by / 2)
        elif 'Y-' in corner: y_col = y_z - (L / 2 - by / 2)
    return {'x_col': round(x_col, 4), 'y_col': round(y_col, 4),
            'ex_geo': round(x_col - x_z, 4), 'ey_geo': round(y_col - y_z, 4),
            'abs_ex': round(abs(x_col - x_z), 4), 'abs_ey': round(abs(y_col - y_z), 4)}


# ================================================================
# MODELO DE PRESIONES
# ================================================================
TOL_Q = 0.5; TOL_KERN = 1e-4

def soil_pressure(P, Mx, My, B, L):
    if P <= 0:
        return {'q1': 0, 'q2': 0, 'q3': 0, 'q4': 0, 'qmax': 0, 'qmin': 0,
                'ex': 0, 'ey': 0, 'kern_x': True, 'kern_y': True,
                'contact': 'tension', 'B_eff': B, 'L_eff': L}
    A = B * L; ex = abs(My) / P; ey = abs(Mx) / P
    kern_x = ex <= B / 6 + TOL_KERN; kern_y = ey <= L / 6 + TOL_KERN
    qb = P / A; qmx = 6 * abs(My) / (L * B**2) if B > 0 else 0; qmy = 6 * abs(Mx) / (B * L**2) if L > 0 else 0
    sx = 1 if My >= 0 else -1; sy = 1 if Mx >= 0 else -1
    q1 = qb + sx * qmx + sy * qmy; q2 = qb + sx * qmx - sy * qmy
    q3 = qb - sx * qmx + sy * qmy; q4 = qb - sx * qmx - sy * qmy
    qmin_n = min(q1, q2, q3, q4)
    if kern_x and kern_y and qmin_n >= -TOL_Q:
        return {'q1': round(q1, 1), 'q2': round(q2, 1), 'q3': round(q3, 1), 'q4': round(q4, 1),
                'qmax': round(max(q1, q2, q3, q4), 1), 'qmin': round(qmin_n, 1),
                'ex': round(ex, 4), 'ey': round(ey, 4), 'kern_x': True, 'kern_y': True,
                'contact': 'full', 'B_eff': round(B, 4), 'L_eff': round(L, 4)}
    B_eff = max(B - 2 * ex, 0.01); L_eff = max(L - 2 * ey, 0.01)
    if (not kern_x) and kern_y:
        a_x = min(3 * (B / 2 - ex), B); a_x = max(a_x, 0.01); qmax_x = 2 * P / (a_x * L)
        if sx > 0: q1 = qmax_x; q2 = qmax_x; q3 = 0; q4 = 0
        else: q1 = 0; q2 = 0; q3 = qmax_x; q4 = qmax_x
    elif kern_x and (not kern_y):
        a_y = min(3 * (L / 2 - ey), L); a_y = max(a_y, 0.01); qmax_y = 2 * P / (B * a_y)
        if sy > 0: q1 = qmax_y; q3 = qmax_y; q2 = 0; q4 = 0
        else: q1 = 0; q3 = 0; q2 = qmax_y; q4 = qmax_y
    else:
        qmb = P / (B_eff * L_eff); q1 = q2 = q3 = q4 = 0
        if sx > 0 and sy > 0: q1 = qmb * 2
        elif sx > 0: q2 = qmb * 2
        elif sy > 0: q3 = qmb * 2
        else: q4 = qmb * 2
    qmax = max(q1, q2, q3, q4); qmin = min(q1, q2, q3, q4)
    return {'q1': round(q1, 1), 'q2': round(q2, 1), 'q3': round(q3, 1), 'q4': round(q4, 1),
            'qmax': round(qmax, 1), 'qmin': round(qmin, 1), 'ex': round(ex, 4), 'ey': round(ey, 4),
            'kern_x': kern_x, 'kern_y': kern_y, 'contact': 'partial',
            'B_eff': round(B_eff, 4), 'L_eff': round(L_eff, 4)}


def factored_pressure_at_face(P, Mx, My, B, L, cbx, cby, d, direction):
    if P <= 0: return {'Mu': 0, 'Vu': 0, 'b_resist': B if direction == 'x' else L}
    A = B * L; qb = P / A
    if direction == 'x':
        cant = (B - cbx) / 2; qmx = 6 * abs(My) / (L * B**2) if B > 0 else 0
        qe = qb + qmx; qf = qb + qmx * (cbx / 2) / (B / 2) if B > 0 else qb
        Mu = L * cant**2 / 6 * (2 * qe + qf) if cant > 0 else 0
        dv = cant - d; Vu = L * dv * (qf + (qe - qf) * d / cant + qe) / 2 if dv > 0 and cant > 0 else 0
        return {'Mu': round(Mu, 2), 'Vu': round(Vu, 1), 'b_resist': L}
    else:
        cant = (L - cby) / 2; qmy = 6 * abs(Mx) / (B * L**2) if L > 0 else 0
        qe = qb + qmy; qf = qb + qmy * (cby / 2) / (L / 2) if L > 0 else qb
        Mu = B * cant**2 / 6 * (2 * qe + qf) if cant > 0 else 0
        dv = cant - d; Vu = B * dv * (qf + (qe - qf) * d / cant + qe) / 2 if dv > 0 and cant > 0 else 0
        return {'Mu': round(Mu, 2), 'Vu': round(Vu, 1), 'b_resist': B}


def punching_with_moment(Pu, Mux, Muy, B, L, cbx, cby, d, fc):
    phi_v = 0.75; A = B * L; b1 = cbx + d; b2 = cby + d; bo = 2 * (b1 + b2); Ap = b1 * b2
    if A <= 0 or bo <= 0 or d <= 0:
        return {'vu_max': 9999, 'phi_vc': 0, 'ratio': 9999, 'vu_direct': 0, 'vu_mx': 0, 'vu_my': 0}
    Vu = Pu * (1 - Ap / A); vu_d = Vu / (bo * d)
    bc = max(cbx, cby) / min(cbx, cby) if min(cbx, cby) > 0 else 1; f = math.sqrt(fc / 1000)
    vc = min(.33 * f, .17 * (1 + 2 / bc) * f, .083 * (40 * d / bo + 2) * f) * 1000; pvc = phi_v * vc
    gvx = 1 - 1 / (1 + (2 / 3) * math.sqrt(b1 / b2)) if b2 > 0 else 0
    gvy = 1 - 1 / (1 + (2 / 3) * math.sqrt(b2 / b1)) if b1 > 0 else 0
    Jx = d * b1**3 / 6 + b1 * d**3 / 6 + 2 * d * b2 * (b1 / 2)**2
    Jy = d * b2**3 / 6 + b2 * d**3 / 6 + 2 * d * b1 * (b2 / 2)**2
    vmx = gvx * abs(Muy) * (b1 / 2) / Jx if Jx > 0 else 0; vmy = gvy * abs(Mux) * (b2 / 2) / Jy if Jy > 0 else 0
    vm = vu_d + vmx + vmy; ratio = vm / pvc if pvc > 0 else 9999
    return {'Vu': round(Vu, 1), 'vu_direct': round(vu_d, 1), 'vu_mx': round(vmx, 1), 'vu_my': round(vmy, 1),
            'vu_max': round(vm, 1), 'phi_vc': round(pvc, 1), 'ratio': round(ratio, 3)}


# ================================================================
# FS VOLCAMIENTO Y DESLIZAMIENTO
# ================================================================
def overturning_check(P, Mx, My, B, L):
    if P <= 0: return {'fs_volc_x': 0, 'fs_volc_y': 0, 'M_estab_x': 0, 'M_estab_y': 0, 'M_volc_x': 0, 'M_volc_y': 0}
    M_estab_x = P * B / 2; M_volc_x = abs(My)
    fs_volc_x = M_estab_x / M_volc_x if M_volc_x > 0.01 else 999
    M_estab_y = P * L / 2; M_volc_y = abs(Mx)
    fs_volc_y = M_estab_y / M_volc_y if M_volc_y > 0.01 else 999
    return {'fs_volc_x': round(fs_volc_x, 2), 'fs_volc_y': round(fs_volc_y, 2),
            'M_estab_x': round(M_estab_x, 2), 'M_estab_y': round(M_estab_y, 2),
            'M_volc_x': round(M_volc_x, 2), 'M_volc_y': round(M_volc_y, 2)}

def sliding_check(P, Vx, Vy, mu):
    if P <= 0: return {'fs_desl_x': 0, 'fs_desl_y': 0, 'F_resist': 0, 'Vx': 0, 'Vy': 0}
    F_resist = mu * P

    Vres = math.sqrt(Vx**2 + Vy**2)

    fsx = F_resist / abs(Vx) if abs(Vx) > 1e-6 else float('inf')
    fsy = F_resist / abs(Vy) if abs(Vy) > 1e-6 else float('inf')
    fs_res = F_resist / Vres if Vres > 1e-6 else float('inf')

    return {
        'fs_desl_x': round(fsx, 3),
        'fs_desl_y': round(fsy, 3),
        'fs_desl_res': round(fs_res, 3),
        'Vx': round(Vx, 2),
        'Vy': round(Vy, 2),
        'Vres': round(Vres, 2)
    }


# ================================================================
# REFUERZO
# ================================================================
REBAR_SIZES = ['#4', '#5', '#6']
REBAR_DB = {}
for _n in REBAR_SIZES:
    _num = int(_n[1:]); _d = _num * 2.54 / 8.0
    REBAR_DB[_n] = {'d_cm': round(_d, 3), 'Ab': round(math.pi / 4 * _d**2, 3)}
SPACINGS = [30, 25, 20, 17.5, 15, 12.5, 10]

def propose_rebar(As_cm2, width_m, rec_cm=7.5):
    if As_cm2 <= 0: return {'text': '-', 'bar': '-', 'n_bars': 0, 'spacing_cm': 0, 'As_prov': 0, 'As_req': 0, 'status': 'ok'}
    w = round(width_m * 100, 1); u = round(w - 2 * rec_cm, 1)
    if u < 5: u = w
    cands = []
    for bn in REBAR_SIZES:
        Ab = REBAR_DB[bn]['Ab']
        for sp in SPACINGS:
            n = math.floor(u / sp) + 1; n = max(n, 2); ap = n * Ab
            if ap >= As_cm2 * 0.98:
                cands.append({'bar': bn, 'Ab': Ab, 'n_bars': n, 'spacing_cm': sp,
                              'As_prov': round(ap, 2), 'As_req': round(As_cm2, 2), 'ratio': round(ap / As_cm2, 2)})
    if cands:
        bo = {b: i for i, b in enumerate(REBAR_SIZES)}
        cands.sort(key=lambda c: (-c['spacing_cm'], c['ratio'], bo[c['bar']]))
        best = cands[0]; best['text'] = f'{best["n_bars"]}{best["bar"]} @{best["spacing_cm"]}cm'; best['status'] = 'ok'; return best
    Ab6 = REBAR_DB['#6']['Ab']; n = max(math.ceil(As_cm2 / Ab6), 2); sp = round(u / (n - 1), 1) if n > 1 else 10
    return {'text': f'{n}#6 @{sp:.1f}cm (DOBLE CAPA?)', 'bar': '#6', 'Ab': Ab6, 'n_bars': n,
            'spacing_cm': round(sp, 1), 'As_prov': round(n * Ab6, 2), 'As_req': round(As_cm2, 2),
            'ratio': round(n * Ab6 / As_cm2, 2), 'status': 'REQUIERE_DOBLE_CAPA'}

def calc_as(Mu, b, d, fc, fy, phi_f=0.9):
    if Mu <= 0 or d <= 0: return 0.0
    Rn = Mu * 1000 / (phi_f * b * d**2); disc = 1 - 2 * Rn / (.85 * fc * 1000)
    rho = (.85 * fc * 1000 / (fy * 1000)) * (1 - math.sqrt(disc)) if disc >= 0 else .025
    return max(rho, max(.0018, 1400 / fy)) * b * d * 10000


# ================================================================
# OPTIMIZACIÓN AISLADA (menor volumen)
# ================================================================
def optimize_isolated(col, ads_f, lrfd_f, params, classification, tied_dirs=None):
    jid = col['joint']; cx = col['x']; cy = col['y']; bx = col['bx']; by = col['by']
    fc = params['fc']; fy = params['fy']; rec = params['rec']
    gc = params['gamma_c']; gs = params['gamma_s']; Df = params['Df']
    hmin = params['h_min']; dmin = params.get('dim_min', 0.60)
    qmap = {'q1': params['qadm_1'], 'q2': params['qadm_2'], 'q3': params['qadm_3']}
    margin_borde = 0.05
    # Zero moments in directions restrained by tie beams so the optimizer
    # doesn't penalize eccentricity in the tied direction.
    if tied_dirs:
        def _ztm(fd):
            out = {}
            for cn, fv in fd.items():
                nf = dict(fv)
                if 'X' in tied_dirs: nf['My'] = 0.0   # X-beam restrains My (eccentricity in X)
                if 'Y' in tied_dirs: nf['Mx'] = 0.0   # Y-beam restrains Mx (eccentricity in Y)
                out[cn] = nf
            return out
        ads_f = _ztm(ads_f)
        lrfd_f = _ztm(lrfd_f)

    loc = classification.get('location', 'concentrica')
    side = classification.get('side', ''); corner = classification.get('corner', '')
    ecc_x = classification.get('ecc_x', 0); ecc_y = classification.get('ecc_y', 0); ecc_dir = classification.get('ecc_dir', 'ambas')
    P_max = max((abs(f['P']) for f in ads_f.values()), default=0); qa_ref = qmap['q1']
    A_est = max(P_max * 1.1 / qa_ref, dmin**2) if qa_ref > 0 else 1.0; dim_est = math.sqrt(A_est)
    B_lo = max(dmin, bx + 0.10); B_hi = max(dim_est * 2, B_lo + 1.5)
    L_lo = max(dmin, by + 0.10); L_hi = max(dim_est * 2, L_lo + 1.5); step = 0.05
    B_vals = [round(B_lo + i * step, 2) for i in range(int((B_hi - B_lo) / step) + 1)]
    L_vals = [round(L_lo + i * step, 2) for i in range(int((L_hi - L_lo) / step) + 1)]
    h_vals = [round(hmin + i * 0.05, 2) for i in range(12)]
    if loc == 'medianera' and side in ('X+', 'X-'): B_vals = sorted(B_vals)
    elif loc == 'medianera' and side in ('Y+', 'Y-'): L_vals = sorted(L_vals)
    elif loc == 'esquinera': B_vals = sorted(B_vals); L_vals = sorted(L_vals)
    candidates = []
    for B in B_vals:
        for L in L_vals:
            zx, zy = cx, cy
            if loc == 'medianera':
                if side == 'X+': zx = cx + bx / 2 + margin_borde - B / 2
                elif side == 'X-': zx = cx - bx / 2 - margin_borde + B / 2
                elif side == 'Y+': zy = cy + by / 2 + margin_borde - L / 2
                elif side == 'Y-': zy = cy - by / 2 - margin_borde + L / 2
            elif loc == 'esquinera':
                if 'X+' in corner: zx = cx + bx / 2 + margin_borde - B / 2
                elif 'X-' in corner: zx = cx - bx / 2 - margin_borde + B / 2
                if 'Y+' in corner: zy = cy + by / 2 + margin_borde - L / 2
                elif 'Y-' in corner: zy = cy - by / 2 - margin_borde + L / 2
            elif loc == 'excentrica':
                if ecc_dir == 'ambas' and ecc_x > 0 and ecc_y > 0: zx = cx - ecc_x + B / 2; zy = cy - ecc_y + L / 2
                elif ecc_dir == 'solo_X' and ecc_x > 0: zx = cx - ecc_x + B / 2
                elif ecc_dir == 'solo_Y' and ecc_y > 0: zy = cy - ecc_y + L / 2
                elif ecc_x > 0: zx = cx - ecc_x + B / 2
                elif ecc_y > 0: zy = cy - ecc_y + L / 2
            zx = round(zx, 3); zy = round(zy, 3)
            for h in h_vals:
                A = B * L; Wt = gc * A * h + gs * A * (Df - h); d = h - rec - 0.006; d = max(d, 0.10)
                ok = True
                for cn, f in ads_f.items():
                    Pt = abs(f['P']) + Wt; qa = qmap.get(f.get('group', 'q1'), qa_ref)
                    sp = soil_pressure(Pt, f['Mx'], f['My'], B, L)
                    if sp['qmax'] > qa * 1.05: ok = False; break
                if not ok: continue
                for cn, f in lrfd_f.items():
                    Pu = abs(f['P'])
                    p = punching_with_moment(Pu, f['Mx'], f['My'], B, L, bx, by, d, fc)
                    if p['ratio'] > 1: ok = False; break
                    sxf = factored_pressure_at_face(Pu, f['Mx'], f['My'], B, L, bx, by, d, 'x')
                    syf = factored_pressure_at_face(Pu, f['Mx'], f['My'], B, L, bx, by, d, 'y')
                    vc = 0.17 * math.sqrt(fc / 1000) * 1000; pv = 0.75
                    px = pv * vc * sxf['b_resist'] * d; py = pv * vc * syf['b_resist'] * d
                    if (px > 0 and sxf['Vu'] / px > 1) or (py > 0 and syf['Vu'] / py > 1): ok = False; break
                if not ok: continue
                vol = B * L * h; egx = abs(cx - zx); egy = abs(cy - zy); eg = math.sqrt(egx**2 + egy**2)
                candidates.append({'B': B, 'L': L, 'h': h, 'x_footing': zx, 'y_footing': zy,
                                   'x_col': cx, 'y_col': cy, 'vol': round(vol, 3),
                                   'e_geo_x': round(egx, 4), 'e_geo_y': round(egy, 4), 'e_geo': round(eg, 4)})
                break
    if candidates:
        if loc == 'medianera' and side in ('X+', 'X-'): candidates.sort(key=lambda c: (c['e_geo_x'], c['vol']))
        elif loc == 'medianera' and side in ('Y+', 'Y-'): candidates.sort(key=lambda c: (c['e_geo_y'], c['vol']))
        elif loc == 'esquinera': candidates.sort(key=lambda c: (c['e_geo'], c['vol']))
        else: candidates.sort(key=lambda c: c['vol'])
        best = candidates[0]
    else:
        best = {'B': round(dim_est * 1.3, 2), 'L': round(dim_est * 1.3, 2), 'h': round(hmin + 0.10, 2),
                'x_footing': round(cx, 3), 'y_footing': round(cy, 3), 'x_col': cx, 'y_col': cy,
                'vol': 999, 'e_geo_x': 0, 'e_geo_y': 0, 'e_geo': 0}
    return best


# ================================================================
# DISEÑO ESTRUCTURAL COMPLETO (dimensiones fijas)
# ================================================================
def full_structural_design(jid, x, y, B, L, h, col_bx, col_by, ads_f, lrfd_f, params,
                            column_forces=None, tied_dirs=None,
                            col_shear_bx=None, col_shear_by=None):
    """
    Auditoría ADS + LRFD completa. Incluye FS volcamiento y deslizamiento.

    col_shear_bx / col_shear_by (opcionales):
        Ancho efectivo de columna para el cálculo del VOLADIZO en cortante 1-vía.
        Para zapatas aisladas = col_bx / col_by (valor por defecto).
        Para zapatas COMBINADAS = tramo entre caras exteriores de todas las columnas en
        cada dirección.  Esto evita que 'cant = (B - cbx)/2' use solo el tamaño de
        una columna cuando la zapata abarca varias, produciendo voladizos irrealistas.
        col_bx / col_by siguen siendo usados para el perímetro de punzonamiento.
    """
    fc = params['fc']; fy = params['fy']; rec = params['rec']
    gc = params['gamma_c']; gs = params['gamma_s']; Df = params['Df']
    mu = params.get('mu', 0.40)
    fs_volc_min_p = params.get('fs_volc_min', {'q1': 2.0, 'q2': 1.5, 'q3': 1.5})
    fs_desl_min_p = params.get('fs_desl_min', {'q1': 1.5, 'q2': 1.2, 'q3': 1.2})
    qmap = {'q1': params['qadm_1'], 'q2': params['qadm_2'], 'q3': params['qadm_3']}
    A = B * L; Wf = gc * A * h; Ws = gs * A * (Df - h); Wt = Wf + Ws; d = h - rec - 0.006; d = max(d, 0.10)

    # Zero moments in directions restrained by tie beams.
    # Tie in X → beam along X restrains My (eccentricity in X) → My = 0.
    # Tie in Y → beam along Y restrains Mx (eccentricity in Y) → Mx = 0.
    # Zeroing Mx/My naturally propagates: soil_pressure → ex/ey = 0,
    # overturning_check → fs_volc_x or fs_volc_y = 999 (not critical).
    if tied_dirs:
        def _ztm_full(fd):
            out = {}
            for cn, fv in fd.items():
                nf = dict(fv)
                if 'X' in tied_dirs: nf['My'] = 0.0
                if 'Y' in tied_dirs: nf['Mx'] = 0.0
                out[cn] = nf
            return out
        ads_f = _ztm_full(ads_f)
        lrfd_f = _ztm_full(lrfd_f)

    # ADS + FS volcamiento + FS deslizamiento
    ads_audit = []; max_q = 0; min_q = 9999; ctrl_ads = ''; any_partial = False
    min_fs_volc = 999; min_fs_desl = 999; any_volc_fail = False; any_desl_fail = False
    for cn, f in ads_f.items():
        Pt = abs(f['P']) + Wt; qa = qmap.get(f.get('group', 'q1'), qmap['q1']); grp = f.get('group', 'q1')
        sp = soil_pressure(Pt, f['Mx'], f['My'], B, L)
        ratio = sp['qmax'] / qa if qa > 0 else 999
        if sp['contact'] == 'partial':
            any_partial = True; qe = Pt / (sp['B_eff'] * sp['L_eff']) if sp['B_eff'] * sp['L_eff'] > 0 else 9999; ratio = qe / qa
        if sp['qmax'] > max_q: max_q = sp['qmax']; ctrl_ads = cn
        if sp['qmin'] < min_q: min_q = sp['qmin']
        # FS volcamiento
        ot = overturning_check(Pt, f['Mx'], f['My'], B, L)
        fs_vmin = min(ot['fs_volc_x'], ot['fs_volc_y']); min_fs_volc = min(min_fs_volc, fs_vmin)
        req_v = fs_volc_min_p.get(grp, 1.5) if isinstance(fs_volc_min_p, dict) else fs_volc_min_p
        volc_ok = fs_vmin >= req_v
        if not volc_ok: any_volc_fail = True
        # FS deslizamiento
        sl = sliding_check(Pt, f.get('Vx', 0), f.get('Vy', 0), mu)
        fs_dmin = min(sl['fs_desl_x'], sl['fs_desl_y']); min_fs_desl = min(min_fs_desl, fs_dmin)
        req_d = fs_desl_min_p.get(grp, 1.2) if isinstance(fs_desl_min_p, dict) else fs_desl_min_p
        desl_ok = fs_dmin >= req_d
        if not desl_ok: any_desl_fail = True
        ads_audit.append({
            'combo': cn, 'group': grp, 'qadm': round(qa, 1),
            'P': round(abs(f['P']), 2), 'P_total': round(Pt, 2),
            'Mx': round(f['Mx'], 2), 'My': round(f['My'], 2),
            'Vx': round(f.get('Vx', 0), 2), 'Vy': round(f.get('Vy', 0), 2),
            'ex': sp['ex'], 'ey': sp['ey'], 'kern_x': sp['kern_x'], 'kern_y': sp['kern_y'], 'contact': sp['contact'],
            'q1': sp['q1'], 'q2': sp['q2'], 'q3': sp['q3'], 'q4': sp['q4'],
            'qmax': sp['qmax'], 'qmin': sp['qmin'], 'ratio': round(ratio, 3),
            'fs_volc_x': ot['fs_volc_x'], 'fs_volc_y': ot['fs_volc_y'], 'fs_volc_min': round(fs_vmin, 2), 'fs_volc_req': req_v, 'volc_ok': volc_ok,
            'fs_desl_x': sl['fs_desl_x'], 'fs_desl_y': sl['fs_desl_y'], 'fs_desl_min': round(fs_dmin, 2), 'fs_desl_req': req_d, 'desl_ok': desl_ok,
        })

    # LRFD
    lrfd_audit = []; Pu_max = 0; ctrl_lrfd = ''; pr_max = 0; sr_max = 0
    for cn, f in lrfd_f.items():
        Pu = abs(f['P']); Mxu = f['Mx']; Myu = f['My']; sp_u = soil_pressure(Pu, Mxu, Myu, B, L)
        punch = punching_with_moment(Pu, Mxu, Myu, B, L, col_bx, col_by, d, fc); pr = punch['ratio']
        # Para cortante 1-vía usar col_shear_bx/by si se proveyeron (zapatas combinadas):
        # el cantilever real = (B - span_entre_caras)/2, no (B - col_bx)/2.
        _csbx = col_shear_bx if col_shear_bx is not None else col_bx
        _csby = col_shear_by if col_shear_by is not None else col_by
        sx = factored_pressure_at_face(Pu, Mxu, Myu, B, L, _csbx, _csby, d, 'x')
        sy = factored_pressure_at_face(Pu, Mxu, Myu, B, L, _csbx, _csby, d, 'y')
        vc1w = 0.17 * math.sqrt(fc / 1000) * 1000; pv = 0.75
        pVnx = pv * vc1w * sx['b_resist'] * d; pVny = pv * vc1w * sy['b_resist'] * d
        srx = sx['Vu'] / pVnx if pVnx > 0 else 999; sry = sy['Vu'] / pVny if pVny > 0 else 999; sr = max(srx, sry)
        As_x = calc_as(sx['Mu'], B, d, fc, fy); As_y = calc_as(sy['Mu'], L, d, fc, fy)
        if Pu > Pu_max: Pu_max = Pu; ctrl_lrfd = cn
        pr_max = max(pr_max, pr); sr_max = max(sr_max, sr)
        # ex/ey (LRFD): excentricidades de la carga última respecto al centroide
        _ex_u = round(abs(Myu) / Pu, 4) if Pu > 0 else 0
        _ey_u = round(abs(Mxu) / Pu, 4) if Pu > 0 else 0
        lrfd_audit.append({
            'combo': cn, 'Pu': round(Pu, 2), 'Mux': round(Mxu, 2), 'Muy': round(Myu, 2),
            'ex': _ex_u, 'ey': _ey_u,
            'Vux': round(f['Vx'], 2), 'Vuy': round(f['Vy'], 2),
            'q1': sp_u['q1'], 'q2': sp_u['q2'], 'q3': sp_u['q3'], 'q4': sp_u['q4'], 'contact': sp_u['contact'],
            # Punzonamiento (ACI 318 §22.6) — tensiones en kPa, fuerza Vu_punch en kN
            'Vu_punch': punch['Vu'],
            'vu_direct': punch['vu_direct'], 'vu_mx': punch['vu_mx'], 'vu_my': punch['vu_my'],
            'vu_max': punch['vu_max'], 'phi_vc': punch['phi_vc'], 'punch_ratio': round(pr, 3),
            # Cortante 1-vía (viga amplia) en kN
            'Vu_x': sx['Vu'], 'phi_Vn_x': round(pVnx, 1), 'sr_x': round(srx, 3),
            'Vu_y': sy['Vu'], 'phi_Vn_y': round(pVny, 1), 'sr_y': round(sry, 3), 'shear_ratio': round(sr, 3),
            'Mu_x': round(sx['Mu'], 2), 'Mu_y': round(sy['Mu'], 2), 'As_x': round(As_x, 1), 'As_y': round(As_y, 1)})

    max_Asx = max((a['As_x'] for a in lrfd_audit), default=0); max_Asy = max((a['As_y'] for a in lrfd_audit), default=0)
    ok_ads = all(a['ratio'] <= 1.05 for a in ads_audit); ok_lrfd = pr_max <= 1 and sr_max <= 1
    if ok_ads and ok_lrfd and not any_partial: status = 'PRELIMINAR_OK'
    elif ok_ads and ok_lrfd: status = 'REVISION_EXCENTRICIDAD'
    elif ok_ads: status = 'REVISAR_h'
    else: status = 'NO_CUMPLE'

    return {
        'joint': jid, 'x': round(x, 3), 'y': round(y, 3), 'B': round(B, 2), 'L': round(L, 2), 'h': round(h, 2),
        'A': round(A, 2), 'Wt': round(Wt, 1), 'd': round(d, 4),
        'qmax': round(max_q, 1), 'qmin': round(min_q, 1), 'ctrl_ads': ctrl_ads, 'ctrl_lrfd': ctrl_lrfd,
        'Pu': round(Pu_max, 1), 'pr': round(pr_max, 3), 'sr': round(sr_max, 3),
        'Asx': round(max_Asx, 1), 'Asy': round(max_Asy, 1), 'st': status,
        'col_bx': col_bx, 'col_by': col_by, 'any_partial_contact': any_partial,
        'rect': [x - B / 2, y - L / 2, x + B / 2, y + L / 2],
        'ads_audit': ads_audit, 'lrfd_audit': lrfd_audit, 'column_forces': column_forces,
        'fs_volc_min': round(min_fs_volc, 2), 'fs_desl_min': round(min_fs_desl, 2),
        'any_volc_fail': any_volc_fail, 'any_desl_fail': any_desl_fail,
    }
