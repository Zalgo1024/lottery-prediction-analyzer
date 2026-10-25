# -*- coding: utf-8 -*-
"""scripts/oneoff/_register_v15.py —— 把「≥四等奖」前瞻终点登记进预注册（v1.4 → v1.5）。

为什么这个登记只能是**前瞻式**的
--------------------------------
本条发布时（2026-10-25），探索性分级复盘（`scripts/oneoff/_tier_review.py`，
产出 `output/_tier_review.json`）**已经看过**该目标在历史窗上的读数：
双色球 3,519 期 Δ=−0.54pp（n*_保守 43,539，不可判）；大乐透 2,939 期
Δ=+0.82pp/+0.75pp（BH 0.081/0.050，边缘）。

同窗口「补登」不会改变任何数字，只会把探索性读数洗成确认性标签 ——
预注册机制存在的唯一目的就是防这个。故本登记：
  · 期窗写死「前瞻（登记后首期起）」；
  · 预期判决写死「不可判」（允许未来数据否证它）；
  · 变更日志自证「已经看过 + 不覆盖历史窗」。
回溯登记会长什么样、为什么不用，见 `docs/回溯登记演示_为何不采用.md`。

★ 与 v1.3/v1.4 的先例一致：改内容 → 改哈希 → 改测试锁 → 回读校验，四步缺一不可。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(r"E:\707")
sys.path.insert(0, str(ROOT))
from ev import preregistration as PR  # noqa: E402

STORE = PR.STORE_PATH

FORWARD_BLOCK = {
    "_说明": ("≥四等奖 的前瞻终点登记。★ 与 `格`（Stage 1 选号格）、`格_Stage2`（倍投规格）并列的第三个块："
              "它管的是「同一副票集在一个**更严**的达标门上、对**未来开奖**的读数」。"
              "确认力**不覆盖**历史窗（2026-10-25 探索性分级复盘已观测过历史窗读数），只覆盖登记之后的开奖。"),
    "_登记性质": "事前登记；预期判决写死在下游，允许未来数据否证它",
    "彩种": ["双色球", "大乐透"],
    "K": 200,
    "预算元每期": 400,
    "_预算说明": ("与 Stage 1 主格同预算（200 注 × 2 元）。选号常量全部取自 `常量`"
                  "（池 8000 / cap 3 / 池seed 20261024 / 基准seed 0 / MC 100k×2 / 评估seed偏移 10000），"
                  "不许逐格微调。"),
    "目标": {"kind": "grade", "grade": "四等"},
    "_目标定义": ("按 `ev/tier_target.resolve_conditions` 解析为 (h, j) 条件集。"
                  "★ 大乐透五个规则版本下该条件集有**三套**"
                  "（2007/09/19：{(4,2),(5,0),(5,1),(5,2)}；2014：+(3,2),(4,1)；"
                  "2026：{(4,1),(4,2),(5,0),(5,1),(5,2)}）⇒ 选号门按 target_issue（最新期）解析，"
                  "评估按 current/periodic 双口径分开报，禁止混用。"),
    "常量": {
        "期窗": "前瞻（登记后首期起，逐期累积）",
        "规则口径": "current + periodic 双报",
        "_规则口径说明": ("current = 用现行规则重算（反事实口径）；periodic = 按当年实际规则结算（历史事实口径）。"
                          "双色球单版本 ⇒ 两口径构造性等价（`tests/test_tier_review.py` 有逐位相同的钉）；"
                          "大乐透不等价，两个都必须报。"),
        "候选池规模": 8000,
        "重叠上限": 3,
        "候选池seed": 20261024,
        "基准seed": 0,
        "单注成本元": 2.0,
    },
    "预期判决": "不可判",
    "_预期依据": ("探索性实测（2026-10-25，历史窗，**不在**本登记确认范围内）："
                  "双色球 n*_保守 = 43,539 ≫ 可用 3,519 期；大乐透 current n* = 6,562 / "
                  "periodic n* = 5,332，判决边缘。预期：前瞻窗内同样判不出差异。"
                  "★ 写这个预期是为了让未来的「检出」必须意外才成立，"
                  "不是为了让「未检出」被读成失败。"),
    "消费方": ("`scripts/oneoff/_tier_review.py`（或其后继）在前瞻窗上手动驱动；"
               "尚无自动调度消费方 —— 这是已知边界，不是遗漏。"),
    "停止规则": [
        "哨兵（单期达标注数）：|实测差| > 4×SE(差) ⇒ 判为实现缺陷，停止并查 bug，不报结论",
        "金额侧：|配对差 z| > 4 ⇒ 判为实现缺陷（E[实得] 的期望差恒为 0），不报效应",
        "集中度守门（v1.2 两级）：单臂单次大奖占总实得比 > 30%，或配对级单期占 Δ 比 > 50% ⇒ 强制不可判",
        "分级累加总额与 `money_series` 总实得逐分相等（断言）；不等 ⇒ 停机查口径",
        "分桶必须按 (规则版本, 奖级名) 双键；出现跨版本同名合并 ⇒ 判实现缺陷",
    ],
}

_POINTER_OLD = "Stage 0b 实测两个彩种、K=10…1600 的每一个点上两臂精确概率差都 < 0.005pp。整支删除。"
_POINTER_NEW = (_POINTER_OLD +
                "（v1.5 起已作为**前瞻终点**重新登记，见 `前瞻终点_四等奖`；"
                "主终点口径不变，本条依据依然成立。）")

CHANGELOG = (
    "v1.5（2026-10-25，**跑之前**登记，确认力仅对**未来开奖**生效）：新增 `前瞻终点_四等奖` 块 —— "
    "把「≥四等奖」登记为前瞻终点（双色球/大乐透，K=200 = 400 元/期同预算，"
    "目标 {kind: grade, grade: 四等}，current/periodic 双口径分开报，"
    "分桶按 (规则版本, 奖级名) 双键）。★ 本条发布时，2026-10-25 的探索性分级复盘**已经看过**"
    "该目标在历史窗上的读数（双色球 3,519 期 Δ=−0.54pp、n*_保守 43,539、不可判；"
    "大乐透 2,939 期 Δ=+0.82pp/+0.75pp、BH 0.081/0.050、边缘），故本登记的确认力**不覆盖**历史窗："
    "历史窗读数永远挂探索性标签，不得引用本条当作它们的确认来源；确认力只对登记之后的开奖生效。"
    "预注册预期判决：**不可判**（依据见块内 `_预期依据`）。★ 该目标不是第一次被考虑："
    "v1.0 的 `指标._主终点选型的依据` 已因「单注 p 太小 ⇒ K·p<1 ⇒ 可改善的超额是 O((K·p)^2)」"
    "把 ≥四等 从主终点整支删除；本条是**前瞻性**重新登记（对未来数据的预测），"
    "不是对该依据的推翻，也不改主终点口径。分桶与口径纪律已固化：`ev/tier_review.py` + "
    "`tests/test_tier_review.py`（18 例，三个关键性质均经故意破坏验证）。"
    "「先看结果再补登记」会长什么样、为什么不用，见 `docs/回溯登记演示_为何不采用.md`。"
)


def main() -> int:
    doc = json.loads(STORE.read_text(encoding="utf-8"))

    # 幂等：已登记过就只做一致性核对，不重复写（重复写会把哈希动两次）
    if doc.get("版本") == "1.5" and "前瞻终点_四等奖" in doc:
        blk = doc["前瞻终点_四等奖"]
        assert blk["彩种"] == FORWARD_BLOCK["彩种"]
        assert blk["K"] == FORWARD_BLOCK["K"]
        assert blk["目标"] == FORWARD_BLOCK["目标"]
        assert blk["预期判决"] == "不可判"
        assert blk["常量"]["期窗"].startswith("前瞻")
        assert "current" in blk["常量"]["规则口径"] and "periodic" in blk["常量"]["规则口径"]
        entry = doc["变更日志"][-1]
        assert entry.startswith("v1.5") and "已经看过" in entry and "不覆盖" in entry
        print("已登记过，跳过（幂等）")
        return 0

    if doc.get("版本") != "1.4":
        raise SystemExit(
            f"基线版本应为 1.4，实际 {doc.get('版本')!r}；先核对再登记")

    doc["版本"] = "1.5"
    doc["锁定日"] = "2026-10-25"
    doc["变更日志"].append(CHANGELOG)
    doc["前瞻终点_四等奖"] = FORWARD_BLOCK
    # v1.0 依据段加指针（不是推翻，是指向新块）
    basis = doc["指标"]["_主终点选型的依据"]
    if _POINTER_NEW not in basis:
        if _POINTER_OLD not in basis:
            raise SystemExit("依据段文本已变，请人工核对后再登记")
        doc["指标"]["_主终点选型的依据"] = basis.replace(_POINTER_OLD, _POINTER_NEW)
    doc.pop("哈希", None)
    doc["哈希"] = PR.compute_hash(doc)

    STORE.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    back = PR.load()                      # 回读校验：哈希错会抛，一例外重要
    blk = back["前瞻终点_四等奖"]
    assert blk["预期判决"] == "不可判"
    assert blk["常量"]["期窗"].startswith("前瞻")
    entry = back["变更日志"][-1]
    assert entry.startswith("v1.5") and "已经看过" in entry and "不覆盖" in entry
    print(f"已写 {STORE}")
    print(f"版本 {back['版本']}  哈希 {back['哈希'][:12]}…")
    print("前瞻终点_四等奖：双彩种 K=200 同预算 | 预期判决 不可判 | 期窗 前瞻")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
