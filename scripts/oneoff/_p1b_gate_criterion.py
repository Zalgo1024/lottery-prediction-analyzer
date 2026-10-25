"""P-1b：闸门「数符号 ≥60%」口径修正诊断。

当前 production 口径（ev/popularity.py:300-301）：
    cv_better = sum(1 for a,b in zip(cv_full,cv_null) if a < b - 1e-9)
    go = (lrt_p<0.01) and cv_better >= ceil(len*0.6)

这是**纯数值符号计数**——只要 full 的平均偏差比 null 低 1e-9 就算「该折更优」，
完全不检验这个差异是否显著。P-1 已指出这会虚高：双色球 5/5 折数值更低，
但只有 ~2/5 折的差异是统计显著的（被少数离群期支配）。

本脚本：
1. 复刻 production 的 5 折 expanding CV，但**保留逐期偏差数组**；
2. 对每折计算逐期改进 d_i = dev_null_i - dev_full_i；
3. 逐折做**单侧显著性检验**（Wilcoxon 符号秩为主，配对 t 为辅）：
   H0: median(d)=0  vs  H1: median(d)>0；
4. 修正后的 cv_better_sig = p<0.05 的折数；
5. 全局池化随机化检验：把所有折的逐期 d_i 合并，置换符号得
   单一 p 值（「full 整体是否真有改进」）；
6. NB2 复核：每折用负二项偏差替代泊松偏差（φ≈8~10 过度离散下更稳健），
   看折数口径是否一致；
7. 输出旧（符号计数）vs 新（显著性）vs NB2 三种口径下双/大各自的 Go 判定。

不动生产代码，只打印结论，供人工核对后决定是否落地。
"""
from __future__ import annotations
import math
import json
import numpy as np
from scipy import stats as sps

from ev.popularity import (
    _load_draw_rows, fit_beta, _fit_glm, _poisson_deviance,
)


def _nb2_deviance(y, mu, theta):
    """负二项（NB2，方差 = mu + mu^2/theta）逐点偏差。theta→∞ 退化为泊松。"""
    y = np.asarray(y, dtype=np.float64)
    mu = np.asarray(mu, dtype=np.float64)
    mu = np.clip(mu, 1e-9, None)
    # 对数似然差（省常数项）：ll_nb = y*log(mu/(mu+alpha)) - (1/alpha)*log(1+alpha*mu)
    # 偏差 = -2 * (ll_full - ll_null) 用同 theta 对齐比较；这里返回「偏差贡献」的逐点形式，
    # 因比较的是 full vs null 在同一测试集上，直接比较两者 mean dev 即可。
    alpha = 1.0 / max(theta, 1e-9)
    def ll(m):
        return y * np.log(m / (m + alpha)) - (1.0 / alpha) * np.log(1.0 + alpha * m)
    return -(ll(mu))  # 越大越差；比较时用 mean，符号与泊松偏差一致


def cv_with_per_period(lottery, n_folds=5, use_nb2=False):
    rows = _load_draw_rows(lottery)
    if rows is None:
        return None
    y = rows["y"]
    fb = fit_beta(rows["reds"], rows["prev"], y, rows["N"])
    if not fb:
        return None
    X_raw, offset = fb["X_raw"], np.log(rows["N"])
    n, k_feat = X_raw.shape
    lrt_p = fb["lrt_p"]

    fold = max(n // (n_folds + 1), 50)
    per = []  # 每折：(d_array, mean_full, mean_null)
    nb2_per = []
    for i in range(1, n_folds + 1):
        cut = fold * i
        if cut + fold > n:
            break
        tr_X_raw, te_X_raw = X_raw[:cut], X_raw[cut:cut + fold]
        tr_y, te_y = y[:cut], y[cut:cut + fold]
        tr_off, te_off = offset[:cut], offset[cut:cut + fold]
        m_, s_ = tr_X_raw.mean(axis=0), tr_X_raw.std(axis=0)
        s_[s_ < 1e-12] = 1.0
        tr_X = np.column_stack([np.ones(len(tr_y)), (tr_X_raw - m_) / s_])
        te_X = np.column_stack([np.ones(len(te_y)), (te_X_raw - m_) / s_])
        try:
            gf = _fit_glm(tr_y, tr_X, tr_off)
            gn = _fit_glm(tr_y, np.ones((len(tr_y), 1)), tr_off)
            mu_f = gf.predict(te_X, offset=te_off)
            mu_n = gn.predict(np.ones((len(te_y), 1)), offset=te_off)
            dev_f = _poisson_deviance(te_y, mu_f)
            dev_n = _poisson_deviance(te_y, mu_n)
            per.append((dev_n - dev_f, float(dev_f.mean()), float(dev_n.mean())))
            if use_nb2:
                # 用训练集估 theta（full），再算两模型在测试集的 NB2 偏差
                try:
                    import statsmodels.api as sm
                    gf2 = sm.GLM(tr_y, tr_X, family=sm.families.NegativeBinomial(),
                                 offset=tr_off).fit()
                    gn2 = sm.GLM(tr_y, np.ones((len(tr_y), 1)),
                                 family=sm.families.NegativeBinomial(),
                                 offset=tr_off).fit()
                    theta = float(np.exp(gf2.params[0]) ** 0)  # 占位，下面用 dispersion
                    # 用 Pearson 残差估 dispersion 近似 theta
                    resid = (tr_y - gf2.mu) / np.sqrt(gf2.mu)
                    phi = float(np.var(resid, ddof=len(tr_X[0])))
                    theta = 1.0 / max(phi - 1.0, 1e-6) if phi > 1.0 else 1e6
                    nbf = _nb2_deviance(te_y, mu_f, theta)
                    nbn = _nb2_deviance(te_y, mu_n, theta)
                    nb2_per.append((float(nbn.mean() - nbf.mean()),))
                except Exception as e:
                    nb2_per.append(None)
        except Exception as e:
            print(f"  [{lottery}] CV 第 {i} 折失败: {e}")

    if not per:
        return None

    # 旧口径：数值符号计数
    old_count = sum(1 for d, mf, mn in per if mf < mn - 1e-9)

    # 新口径：逐折单侧显著性
    wilx_count = 0
    ttest_count = 0
    fold_detail = []
    for idx, (d, mf, mn) in enumerate(per, 1):
        d = np.asarray(d, dtype=np.float64)
        d = d[np.isfinite(d)]
        wilx_p = np.nan
        t_p = np.nan
        try:
            if np.any(d != 0):
                wr = sps.wilcoxon(d, alternative="greater")
                wilx_p = float(wr.pvalue)
                tr = sps.ttest_1samp(d, 0.0, alternative="greater")
                t_p = float(tr.pvalue)
        except Exception:
            pass
        sig_w = (not math.isnan(wilx_p)) and wilx_p < 0.05
        sig_t = (not math.isnan(t_p)) and t_p < 0.05
        if sig_w:
            wilx_count += 1
        if sig_t:
            ttest_count += 1
        fold_detail.append((idx, round(float(d.mean()), 4),
                            round(wilx_p, 4) if not math.isnan(wilx_p) else None,
                            round(t_p, 4) if not math.isnan(t_p) else None,
                            sig_w, sig_t))

    # 全局池化随机化检验
    pooled = np.concatenate([d for d, _, _ in per])
    pooled = pooled[np.isfinite(pooled)]
    obs = float(pooled.mean())
    rng = np.random.default_rng(20261025)
    B = 4000
    pool_pos = pooled[pooled > 0]
    pool_neg = pooled[pooled < 0]
    # 仅用非零点做符号置换（零点不影响均值符号）
    non0 = np.concatenate([pool_pos, -pool_neg])
    perm_means = np.empty(B)
    for b in range(B):
        signs = rng.integers(0, 2, size=len(non0)) * 2 - 1
        perm_means[b] = (non0 * signs).mean()
    global_p = float(np.mean(perm_means >= obs - 1e-12))

    # NB2 折数
    nb2_count = 0
    if nb2_per:
        nb2_count = sum(1 for x in nb2_per if x is not None and x[0] > 0)

    need = math.ceil(len(per) * 0.6)
    old_go = (lrt_p < 0.01) and (old_count >= need)
    new_go = (lrt_p < 0.01) and (wilx_count >= need)
    nb2_go = (lrt_p < 0.01) and (nb2_count >= need)

    return {
        "lottery": lottery, "n": n, "lrt_p": lrt_p, "k_feat": k_feat,
        "n_folds_used": len(per), "need": need,
        "old": {"cv_better": old_count, "go": old_go},
        "wilcoxon": {"cv_better_sig": wilx_count, "go": new_go},
        "ttest": {"cv_better_sig": ttest_count},
        "nb2": {"cv_better": nb2_count, "go": nb2_go},
        "global_perm_p": global_p, "global_obs_mean": round(obs, 5),
        "fold_detail": fold_detail,
    }


def main():
    for lot in ("双色球", "大乐透"):
        print("=" * 70)
        print(f"彩种：{lot}")
        r = cv_with_per_period(lot, use_nb2=True)
        if r is None:
            print("  无可用数据 / 拟合失败")
            continue
        print(f"  n={r['n']}  LRT p={r['lrt_p']:.3g}  k={r['k_feat']}  折数={r['n_folds_used']}  需≥{r['need']} 折")
        print(f"  旧(符号计数):  cv_better={r['old']['cv_better']}  → Go={r['old']['go']}")
        print(f"  新(Wilcoxon) :  cv_better_sig={r['wilcoxon']['cv_better_sig']}  → Go={r['wilcoxon']['go']}")
        print(f"  新(ttest)    :  cv_better_sig={r['ttest']['cv_better_sig']}")
        print(f"  NB2 复核     :  cv_better={r['nb2']['cv_better']}  → Go={r['nb2']['go']}")
        print(f"  全局池化随机化 p={r['global_perm_p']:.4g}  (观测逐期改进均值={r['global_obs_mean']})")
        print("  逐折明细 [折, 改进均值, Wilcoxon_p, ttest_p, sigW, sigT]:")
        for fd in r["fold_detail"]:
            print("    ", fd)
        # 写 json 供报告引用
        with open(f"logs/_p1b_{lot}.json", "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
