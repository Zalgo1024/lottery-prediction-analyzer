import sys
sys.path.insert(0, 'E:/707')
import config
from data.loader import load_lottery
from data.features import build_feature_vector_v2
from data.schema import DrawRecord


def old_window_rb_freq(window, cfg):
    r_min, r_max = cfg['red_range']; b_min, b_max = cfg['blue_range']
    rf = {n: 0 for n in range(r_min, r_max + 1)}; bf = {n: 0 for n in range(b_min, b_max + 1)}
    for rec in window:
        for n in rec.红球: rf[n] = rf.get(n, 0) + 1
        for n in rec.蓝球: bf[n] = bf.get(n, 0) + 1
    return rf, bf


def old_current_omission(window, r_min, r_max, b_min, b_max):
    rl = {n: None for n in range(r_min, r_max + 1)}; bl = {n: None for n in range(b_min, b_max + 1)}
    for idx, rec in enumerate(window):
        for n in rec.红球: rl[n] = idx
        for n in rec.蓝球: bl[n] = idx
    w = max(len(window), 1)
    ro = {n: (w - 1 - rl[n]) if rl[n] is not None else w for n in range(r_min, r_max + 1)}
    bo = {n: (w - 1 - bl[n]) if bl[n] is not None else w for n in range(b_min, b_max + 1)}
    return ro, bo


def old_cooc(window, cfg):
    r_min, r_max = cfg['red_range']; b_min, b_max = cfg['blue_range']
    rf, bf = old_window_rb_freq(window, cfg)
    rt = [n for n, _ in sorted(rf.items(), key=lambda x: x[1], reverse=True)[:5]]
    bt = [n for n, _ in sorted(bf.items(), key=lambda x: x[1], reverse=True)[:5]]
    w = max(len(window), 1)
    rc = {n: 0.0 for n in range(r_min, r_max + 1)}
    for n in range(r_min, r_max + 1):
        c = 0
        for rec in window:
            if n in rec.红球:
                co = sum(1 for t in rt if t != n and t in rec.红球); c += co / max(len(rt) - (1 if n in rt else 0), 1)
        rc[n] = c / w
    bc = {n: 0.0 for n in range(b_min, b_max + 1)}
    for n in range(b_min, b_max + 1):
        c = 0
        for rec in window:
            if n in rec.蓝球:
                co = sum(1 for t in bt if t != n and t in rec.蓝球); c += co / max(len(bt) - (1 if n in bt else 0), 1)
        bc[n] = c / w
    return rc, bc


def old_v2(window, cfg):
    ws_ = sorted(window, key=lambda r: r.期号 if r.期号 else 0)
    r_min, r_max = cfg['red_range']; b_min, b_max = cfg['blue_range']
    rm = (r_min + r_max) // 2; rt = (r_max - r_min + 1) // 3; z1 = r_min + rt; z2 = r_min + 2 * rt
    w = max(len(ws_), 1)
    rf, bf = old_window_rb_freq(ws_, cfg)
    ro, bo = old_current_omission(ws_, r_min, r_max, b_min, b_max)
    rfreqs = {}
    for K in [10, 30]:
        sub = ws_[-K:] if len(ws_) >= K else ws_
        a, b = old_window_rb_freq(sub, cfg); rfreqs[K] = (a, b, max(len(sub), 1))
    half = max(w // 2, 1)
    fh = ws_[:half]; sh = ws_[half:]
    ro_f, _ = old_current_omission(fh, r_min, r_max, b_min, b_max)
    ro_s, _ = old_current_omission(sh, r_min, r_max, b_min, b_max)
    bo_f, _ = old_current_omission(fh, r_min, r_max, b_min, b_max)
    bo_s, _ = old_current_omission(sh, r_min, r_max, b_min, b_max)
    rco, bco = old_cooc(ws_, cfg)
    feat = []
    for n in range(r_min, r_max + 1): feat.append(rf[n] / w)
    for n in range(b_min, b_max + 1): feat.append(bf[n] / w)
    for n in range(r_min, r_max + 1): feat.append(ro[n] / w)
    for n in range(b_min, b_max + 1): feat.append(bo[n] / w)
    odd = 0; tot = 0; sm = 0; z1c = z2c = z3c = 0
    for rec in ws_:
        for n in rec.红球:
            tot += 1
            if n % 2 == 1: odd += 1
            if n <= rm: sm += 1
            if n <= z1: z1c += 1
            elif n <= z2: z2c += 1
            else: z3c += 1
    feat.append(odd / max(tot, 1)); feat.append(sm / max(tot, 1)); feat.append(z1c / max(tot, 1)); feat.append(z2c / max(tot, 1)); feat.append(z3c / max(tot, 1))
    for n in range(r_min, r_max + 1):
        for K in [10, 30]:
            a, _, d = rfreqs[K]; feat.append(a[n] / d)
        sl = (ro_s[n] - ro_f[n]) / w; feat.append(sl); feat.append(rco[n])
    for n in range(b_min, b_max + 1):
        for K in [10, 30]:
            _, b, d = rfreqs[K]; feat.append(b[n] / d)
        sl = (bo_s[n] - bo_f[n]) / w; feat.append(sl); feat.append(bco[n])
    return feat


lot = '双色球'
cfg = config.LOTTERY_CONFIG[lot]
data = load_lottery(lot)
win = data.records[:50]
nv = build_feature_vector_v2(win, cfg)
ov = old_v2(win, cfg)
print('len', len(nv), len(ov))
for i, (a, b) in enumerate(zip(nv, ov)):
    if abs(a - b) > 1e-12:
        print('first diff idx', i, 'new', a, 'old', b)
        print('  new ctx', nv[max(0, i - 4):i + 4])
        print('  old ctx', ov[max(0, i - 4):i + 4])
        break

# 额外：直接对比 blue n=3 的 omission 计算
from data.features import schema_from_cfg, _current_omission_chrono
cfg = config.LOTTERY_CONFIG['双色球']
schema = schema_from_cfg(cfg)
bz = schema.blue_zone
ws_ = sorted(win, key=lambda r: r.期号 if r.期号 else 0)
w = max(len(ws_), 1)
half = max(w // 2, 1)
fh = ws_[:half]; sh = ws_[half:]
new_sec = _current_omission_chrono(sh, schema)['蓝球']
new_fir = _current_omission_chrono(fh, schema)['蓝球']
print('blue n=3 new slope num =', new_sec[3] - new_fir[3], 'w=', w)
old_ro_f, old_bo_f = old_current_omission(fh, 1, 33, 1, 16)
old_ro_s, old_bo_s = old_current_omission(sh, 1, 33, 1, 16)
print('blue n=3 old slope num =', old_bo_s[3] - old_bo_f[3])
print('new_sec[3]=', new_sec[3], 'new_fir[3]=', new_fir[3])
print('old_bo_s[3]=', old_bo_s[3], 'old_bo_f[3]=', old_bo_f[3])
print('--- full blue omission (second) new vs old ---')
for n in range(1, 17):
    if new_sec[n] != old_bo_s[n]:
        print(f'  n={n}: new_sec={new_sec[n]} old_bo_s={old_bo_s[n]}  (first: new={new_fir[n]} old={old_bo_f[n]})')
print('--- blue FIRST omission new vs old ---')
for n in range(1, 17):
    if new_fir[n] != old_bo_f[n]:
        print(f'  n={n}: new_fir={new_fir[n]} old_bo_f={old_bo_f[n]}')
print('--- blue slope arrays (new vs old) ---')
new_slope = [(new_sec[n] - new_fir[n]) / w for n in range(1, 17)]
old_slope = [(old_bo_s[n] - old_bo_f[n]) / w for n in range(1, 17)]
for n in range(1, 17):
    if abs(new_slope[n-1] - old_slope[n-1]) > 1e-12:
        print(f'  n={n}: new_slope={new_slope[n-1]} old_slope={old_slope[n-1]}')
# 在 nv 中找 new_slope 各值的位置
print('--- 在 nv 中定位 blue slope 值 ---')
for n in range(1, 17):
    val = new_slope[n-1]
    idxs = [i for i, v in enumerate(nv) if abs(v - val) < 1e-12]
    print(f'  n={n} slope={val} idxs={idxs[:3]}')



