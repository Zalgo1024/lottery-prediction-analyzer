import sys, subprocess
sys.path.insert(0, 'E:/707')
import config
from data.loader import load_lottery
import data.features as newfeat

# 提取 git HEAD 上的原始 features.py（重构前）并直接执行，用于权威对比
src = subprocess.check_output(['git', 'show', 'HEAD:data/features.py'], cwd='E:/707').decode('utf-8')
ns = {}
exec(compile(src, 'old_features', 'exec'), ns)

for lot in ['双色球', '大乐透']:
    cfg = config.LOTTERY_CONFIG[lot]
    data = load_lottery(lot)
    win = data.records[:50]
    newv1 = newfeat.build_feature_vector_v1(win, cfg)
    oldv1 = ns['build_feature_vector_v1'](win, cfg)
    newv2 = newfeat.build_feature_vector_v2(win, cfg)
    oldv2 = ns['build_feature_vector_v2'](win, cfg)
    d1 = sum(1 for a, b in zip(newv1, oldv1) if abs(a - b) > 1e-12)
    d2 = sum(1 for a, b in zip(newv2, oldv2) if abs(a - b) > 1e-12)
    print(f'{lot}: v1 len={len(newv1)}/{len(oldv1)} diffs={d1} | v2 len={len(newv2)}/{len(oldv2)} diffs={d2}')

    # 直接对比原始 _current_omission_chrono 的蓝球输出 与 新版
    cfg = config.LOTTERY_CONFIG[lot]
    r_min, r_max = cfg['red_range']; b_min, b_max = cfg['blue_range']
    ws = sorted(win, key=lambda r: r.期号 if r.期号 else 0)
    w = max(len(ws), 1)
    half = max(w // 2, 1)
    fh = ws[:half]; sh = ws[half:]
    # 原始
    o_ro_f, o_bo_f = ns['_current_omission_chrono'](fh, r_min, r_max, b_min, b_max)
    o_ro_s, o_bo_s = ns['_current_omission_chrono'](sh, r_min, r_max, b_min, b_max)
    # 新版
    from data.features import schema_from_cfg, _current_omission_chrono as new_co
    schema = schema_from_cfg(cfg)
    n_co_f = new_co(fh, schema)
    n_co_s = new_co(sh, schema)
    for n in [1, 2, 3]:
        print(f'  n={n}: 原始 first={o_bo_f[n]} second={o_bo_s[n]} | 新版 first={n_co_f["蓝球"][n]} second={n_co_s["蓝球"][n]}')

    # 直接打印 blue n=1 的 rich 块（新旧各4个值）及原始组件
    print('  新版 blue n=1 rich块 newv2[235:239] =', [round(x,4) for x in newv2[235:239]])
    print('  原始 blue n=1 rich块 oldv2[235:239] =', [round(x,4) for x in oldv2[235:239]])
    # 原始在 235..298 中找 0.06 出现位置
    old_06 = [i for i,v in enumerate(oldv2) if abs(v-0.06)<1e-9]
    new_06 = [i for i,v in enumerate(newv2) if abs(v-0.06)<1e-9]
    print('  原始 0.06 出现位置:', old_06[:10])
    print('  新版 0.06 出现位置:', new_06[:10])


    if d2:
        print(f'  v2 前几个差异位置(索引/新值/旧值):')
        cnt = 0
        for i, (a, b) in enumerate(zip(newv2, oldv2)):
            if abs(a - b) > 1e-12:
                print(f'    [{i}] new={a} old={b}')
                cnt += 1
                if cnt >= 8:
                    break

