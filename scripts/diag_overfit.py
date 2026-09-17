"""即时落盘诊断：每个彩票跑完立即 append 写文件，避免 OOM 丢全部。"""
import sys, os, traceback
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, r"E:\707")
from data.loader import load_lottery
from data.features import build_features, FEATURE_VERSION_STANDARD
from config import LOTTERY_CONFIG

WS = 50
OUTFILE = r"E:\707\scripts\_diag_out.txt"
open(OUTFILE, "w", encoding="utf-8").close()  # 清空

def emit(lines):
    with open(OUTFILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def run(lot):
    data = load_lottery(lot)
    cfg = LOTTERY_CONFIG[lot]
    X, y = build_features(data.records, WS, lot, FEATURE_VERSION_STANDARD)
    if X.size == 0:
        return [f"{lot}: 数据不足"]
    n = len(X); split = int(n * 0.8)
    Xtr, Xte = X[:split], X[split:]
    ytr, yte = y[:split], y[split:]
    sc = StandardScaler().fit(Xtr)
    Xtr_s, Xte_s = sc.transform(Xtr), sc.transform(Xte)
    zero_base = float((yte == 0).mean())
    lines = []
    lines.append("\n" + "=" * 58)
    lines.append(f"{lot}  样本={n} 标签数={y.shape[1]} 训练/测试={split}/{n-split}")
    lines.append(f"基线(全猜0不出): {zero_base:.4f}  正样本(开出)占比={1-zero_base:.4f}")

    # random_forest
    ptr, pte = [], []
    for i in range(y.shape[1]):
        yt = ytr[:, i]
        if len(set(yt)) < 2:
            ptr.append(np.zeros(len(Xtr))); pte.append(np.zeros(len(Xte))); continue
        m = RandomForestClassifier(n_estimators=100, max_depth=10, random_state=42, class_weight="balanced")
        m.fit(Xtr_s, yt)
        ptr.append(m.predict(Xtr_s)); pte.append(m.predict(Xte_s))
    ptr = np.array(ptr).T; pte = np.array(pte).T
    in_r = float((ptr == ytr).mean()); out_r = float((pte == yte).mean())
    lines.append(f"[RF] in={in_r:.4f} out={out_r:.4f} gap={in_r-out_r:+.4f}  信号={'微弱' if out_r>zero_base+0.01 else '无(=基线)'}")

    # logistic
    ptr, pte = [], []
    from sklearn.linear_model import LogisticRegression
    for i in range(y.shape[1]):
        yt = ytr[:, i]
        if len(set(yt)) < 2:
            ptr.append(np.zeros(len(Xtr))); pte.append(np.zeros(len(Xte))); continue
        m = LogisticRegression(max_iter=1000, solver="lbfgs", class_weight="balanced")
        m.fit(Xtr_s, yt)
        ptr.append(m.predict(Xtr_s)); pte.append(m.predict(Xte_s))
    ptr = np.array(ptr).T; pte = np.array(pte).T
    in_r = float((ptr == ytr).mean()); out_r = float((pte == yte).mean())
    lines.append(f"[LR] in={in_r:.4f} out={out_r:.4f} gap={in_r-out_r:+.4f}  信号={'微弱' if out_r>zero_base+0.01 else '无(=基线)'}")
    return lines

for lot in ["双色球", "大乐透", "七星彩", "排列5", "福彩3D", "排列3"]:
    try:
        emit(run(lot))
    except Exception as e:
        emit([f"\n!!! {lot} 失败: {e}", traceback.format_exc()])
    # 强制回收
    import gc; gc.collect()
