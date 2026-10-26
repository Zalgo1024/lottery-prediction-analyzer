# -*- coding: utf-8 -*-
"""写出 v1.9 预注册：把「≥四等 门继续搞（两条都做）」的自由度先钉死。

★ 时序纪律：本脚本只写**层 1**（解析/枚举量）。层 2（真实开奖读数）一行都不碰。
  两条轨道都设计成「在层 1 内即可判决」，因此本版**不产生新的确认性声明**。
★ 哈希自校验：改任何字段都必须重算 `哈希`，否则 `ev.preregistration.load()` 直接抛。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from ev import preregistration as PR                        # noqa: E402
from ev import tier_target as TT                            # noqa: E402

TARGET_4 = {"kind": "grade", "grade": "四等"}
TARGET_10 = {"kind": "payout", "min_payout": 10.0}

P = {
    "双色球": TT.single_ticket_probability(
        "双色球", TT.resolve_conditions("双色球", TARGET_4, None)["conditions"], None),
    "大乐透": TT.single_ticket_probability(
        "大乐透", TT.resolve_conditions("大乐透", TARGET_4, None)["conditions"], None),
}
P10 = {
    "双色球": TT.single_ticket_probability(
        "双色球", TT.resolve_conditions("双色球", TARGET_10, None)["conditions"], None),
}

PERIODS_PER_MONTH = 13
AVAIL_PERIODS = 1408


def kp_cross(p: float) -> int:
    """K·p 跨过 1 所需的最小 K（上取整）。"""
    import math
    return int(math.ceil(1.0 / p))


def month_upper(kp: float) -> float:
    """月内至少一次的上限：1-(1-min(1,K·p))^13。

    ★ 不是 1-(1-p)^(13K)：那等于把 13 次独立开奖当成一次可联合选号的开奖，
      会把 13 期的独立性白送掉，从而把上限吹到 ~1。
    """
    q = min(1.0, float(kp))
    return 1.0 - (1.0 - q) ** PERIODS_PER_MONTH


def month_random(p: float, k: int) -> float:
    return 1.0 - (1.0 - p) ** (k * PERIODS_PER_MONTH)


def n_star(p1: float, p2: float, z_a: float = 1.959964,
           z_b: float = 0.841621) -> float:
    """两比例检验所需样本（期数）。p1=基线，p2=对照。"""
    import math
    d = abs(p2 - p1)
    if d <= 0:
        return float("inf")
    pbar = (p1 + p2) / 2.0
    return ((z_a * math.sqrt(2 * pbar * (1 - pbar))
             + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2) / (d ** 2)


def main() -> int:
    doc = PR.load()
    if str(doc.get("版本")) == "1.9" and "四等门_二次评估" in doc:
        print(f"已是 v1.9（哈希 {doc['哈希'][:12]}…），不重复写")
        return 0
    if str(doc.get("版本")) != "1.8":
        raise SystemExit(f"前置版本不是 1.8：{doc.get('版本')}")

    const = doc["常量"]
    z_a = float(const["Z_alpha"])
    z_b = float(const["Z_power"])

    cells = []
    for lot in ("双色球", "大乐透"):
        p = P[lot]
        need = kp_cross(p)
        cells.append({
            "彩种": lot, "目标": "≥四等", "单注p": p,
            "K跨1所需": need,
            "K跨1成本元每期": need * float(const["单注成本元"]),
            "K跨1月度定额元": need * float(const["单注成本元"]) * PERIODS_PER_MONTH,
        })

    monthly = []
    for lot in ("双色球", "大乐透"):
        p = P[lot]
        for k, tag in ((int(round(200 if lot == "双色球" else 38)), "生产旧口径"),
                       (int(round(500 if lot == "双色球" else 400)), "生产现行口径"),
                       (kp_cross(p), "K·p=1")):
            kp = min(1.0, k * p)
            rnd = month_random(p, k)
            up = month_upper(kp)
            monthly.append({
                "彩种": lot, "K": k, "口径": tag,
                "单期K·p": k * p, "单期上限": kp,
                "单期随机基线": 1.0 - (1.0 - p) ** k,
                "月内随机基线": rnd,
                "月内理论上限": up,
                "月内可争取空间": up - rnd,
                "n星_月内_随机对上限": n_star(rnd, up, z_a, z_b),
                "可用月数": AVAIL_PERIODS / PERIODS_PER_MONTH,
            })

    blk = {
        "_立项": (
            "v1.9（2026-10-26）：用户指令「≥四等 门继续搞」，并选择「两条都做」。"
            "v1.8 已用算术判「未能识别」（双色球 K=200 可争取空间 2.99e-6）。"
            "本版把两条可能翻案的路都钉死口径后走完，给出定量判决，不再留模糊。"),
        "轨道1_K扩展": {
            "问题": "「K·p 跨过 1」是不是 ≥四等 门的真杠杆",
            "K网格": [200, 500, 1000, 1500, 2000, 2251],
            "_K网格说明": "2251 = 双色球 ≥四等 K·p=1 的最小整数 K；200 是 v1.8 已实测口径。",
            "重叠上限": 3,
            "_重叠上限来源": "生产口径 POProd（不是 ev.selection 的双色球 4），与 v1.8 主跑一致，不许逐 K 微调",
            "候选池口径": "max(8000, 40×K)",
            "_候选池理由": (
                "探针 1 用固定池 8000 跑到 K=2251 时只出 938 注 —— 那不是覆盖极限，"
                "是池耗竭。池必须按 K 放大，否则会把自己的约束当成数据的约束。"),
            "MC采样": 20000,
            "_MC说明": (
                "主跑用 100000；本轨道只判「出票可行性」与「效率单调性」，不做 p 值判决，"
                "故允许降采样。降采样只会**低估**优化器能力 ⇒ 结论方向保守。"),
            "判决口径": (
                "出票率 < 1 ⇒ 该 K 判「不可达」（不是「无空间」）；"
                "效率 = 精确P ÷ min(1,K·p)，只用于看趋势，不做阈值判决。"),
            "跨1所需": cells,
        },
        "轨道2_月度合并": {
            "问题": "把「单期≥四等」改成「一个自然月内至少一次≥四等」，是否改变可判性",
            "期数每月": PERIODS_PER_MONTH,
            "_期数来源": "乐透型每周 3 期 ⇒ 13 期/月",
            "月内上限公式": "1-(1-min(1,K·p))^期数",
            "_为什么不是 13K 注": (
                "13 期开奖相互独立，跨期不能联合选号去覆盖同一次开奖。"
                "把 13K 张票当成一次开奖的覆盖问题会把上限吹到 ~1，"
                "那是把独立性白送掉，属于口径错误。"),
            "n*口径": f"两比例检验，双侧 α=0.05、power=0.80（Z_alpha={z_a}、Z_power={z_b}）",
            "可用期数": AVAIL_PERIODS,
            "可用月数": AVAIL_PERIODS / PERIODS_PER_MONTH,
            "判决口径": "n*(月) > 可用月数 ⇒ 判「不可判」，不跑实测（跑了也只是噪声）",
            "格": monthly,
        },
        "层1披露": (
            "本版两条轨道全部在层 1（解析/枚举）内结论，因此**不产生新的确认性声明**。"
            "层 1 有否决权无确认力：精确枚举的解析误差比实测频率小约 9 个数量级，"
            "足以否掉一个不可能为真的 Δ，但不能替实测背书。"),
        "不参与声明": (
            "两条轨道的结论都不构成「可以投入」的理由。长期 EV 仍为负："
            "彩票资金按游戏规则分为奖金、发行费与公益金，只有奖金部分构成返奖来源。"),
    }
    doc["四等门_二次评估"] = blk
    doc["版本"] = "1.9"
    doc.setdefault("变更日志", []).append({
        "版本": "1.9",
        "日": "2026-10-26",
        "说明": ("新增 `四等门_二次评估`：轨道1 K 扩展（查 K·p 跨 1 是否是真杠杆、"
                 "并区分池耗竭与覆盖极限）与轨道2 月度合并口径（查换口径能否让门可判）。"
                 "两条轨道都在层 1 内判决，不新增确认性声明。"
                 "★ 登记在跑数之前：本块里的层 1 数字（K 跨 1 所需注数、月内上限、n*）"
                 "全部由解析公式在跑数前算出。"),
    })
    doc["哈希"] = PR.compute_hash(doc)

    p = Path(PR.STORE_PATH)
    fd, tmp_name = tempfile.mkstemp(suffix=".json", dir=str(p.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    # ★ Windows 上 os.replace 会因目标被别的进程（调度器/worker）打开而 WinError 32。
    #   重试若干次；全程原子替换，不存在「写了一半」的中间态。
    last = None
    for _ in range(30):
        try:
            os.replace(tmp, p)
            last = None
            break
        except PermissionError as e:                          # noqa: PERF203
            last = e
            import time as _t
            _t.sleep(0.5)
    if last is not None:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SystemExit(f"替换 config/preregistration.json 失败（被占用）：{last}")
    print(f"已写 v1.9：{p}  哈希 {doc['哈希'][:12]}…  顶层字段 {len(doc)}")
    print(json.dumps({"单注p": P, "跨1所需": cells}, ensure_ascii=False, indent=1))
    print(json.dumps(monthly, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
