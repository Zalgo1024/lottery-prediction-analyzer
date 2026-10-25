# -*- coding: utf-8 -*-
"""把 hybrid 臂登记进 config/preregistration.json 的顶层 `臂` 列表（v1.3 → v1.4）。

★ 必须在**看到任何 hybrid 的 Stage 1 结果之前**执行，且只执行一次。
  之后要改，必须再走一次「改内容 → 改哈希 → 改变更日志 → 改测试常量」的完整动作。

为什么改 `臂` 而不是 `格`
------------------------
`格` 列表锁的是「主假设恰好 1 个」= 选哪副票集；hybrid 是同一主格下新增的
**选号方法臂**，与 random/tier_coverage/union_greedy/lowcrowd 并列，不新开自由度。
`ev/arena/runner.py::run_cell` 从 `臂` 数组取 `进主检验` 定 BH 的 m，而实际迭代
列表来自 `scripts/oneoff/_arena_run.py` 的硬编码 ARMS —— 两处都要同步，缺一不可。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(r"E:\707")
sys.path.insert(0, str(ROOT))
from ev import preregistration as PR  # noqa: E402

STORE = PR.STORE_PATH

HYBRID_ARM = {
    "名": "hybrid",
    "进主检验": True,
    "说明": ("联合目标：P(并集) 采样边际 + 冷门通道同一打分加权"
             "（λ=0.5 / gate_q=0.30 / cov_source=union，v1.4 锁定）；"
             "拥挤度代理不可用的彩种降级为「暂不评估」（第一等状态，不是 0 期）"),
}

CHANGELOG = (
    "v1.4（2026-10-25，**跑之前**登记）：新增 `hybrid` 臂 —— "
    "`ev/lowcrowd_select.select_joint`（P5 联合目标：覆盖通道 + 冷门通道同一打分加权），"
    "参数全部锁定：λ=0.5 / gate_q=0.30 / cov_source=union / β口径=冻结β / 池 8000 / cap=3，"
    "**不许逐格微调**（改它们 = 改实验契约）。进主检验 true。"
    "理由：P5 层已实现并已验证（proxy 通道与 select_lowcrowd 逐位一致；λ 在窗内 z 标准化后"
    "是真杠杆，见 docs/P5_联合目标选号_结论.md）；Stage 1 的下一个问题是「覆盖 × 冷门联合加权"
    "能否同时改善两侧」，需要 hybrid 与既有四臂同池同预算同 cap 的配对读数。"
    "★ 本条是**事前登记**：发布时尚未看过任何 hybrid 的 Stage 1 结果；不预注册方向性预期"
    "（P5 开发尺度读数：λ=0.5 较 proxy 覆盖 +0.94pp、平均拥挤分位 0.019→0.022，非帕累托），"
    "Stage 1 读数以实测为准。主假设臂不变（union_greedy）；hybrid 进主检验使主格 BH "
    "检验数 3→4，校正只会更保守。大乐透格 hybrid 预期「暂不评估」"
    "（拥挤度代理 No-Go，原因由 crowd.py 三重探测现取实测数字），这是第一等状态，"
    "不是 0 期、不是失败。"
)


def main() -> int:
    doc = json.loads(STORE.read_text(encoding="utf-8"))
    names = {a.get("名") for a in (doc.get("臂") or [])}
    if "hybrid" in names:
        # 幂等判据用「臂里有没有 hybrid」而不是版本号：版本可能被别处手工改过而
        # hybrid 没进去，那种不一致状态必须炸给人看，不许自动修。
        if doc.get("版本") != "1.4":
            raise SystemExit(
                f"状态不一致：臂里已有 hybrid 但版本是 {doc.get('版本')!r}，请人工核对")
        print("已登记过，跳过（避免重复登记把哈希改两次）")
        return 0
    if doc.get("版本") != "1.3":
        raise SystemExit(
            f"基线版本应为 1.3，实际 {doc.get('版本')!r}；先核对再登记")

    doc["版本"] = "1.4"
    doc["锁定日"] = "2026-10-25"
    doc["臂"].append(HYBRID_ARM)          # 追加在 prod_mix 之后（进主检验 true）
    doc["变更日志"].append(CHANGELOG)     # 沿用 v1.2/v1.3 的字符串条目惯例
    doc.pop("哈希", None)                 # 旧条理：只清校验字段，再重算
    doc["哈希"] = PR.compute_hash(doc)

    STORE.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    back = PR.load()                      # 回读校验：装载不过一例外重要（哈希错会抛）
    hm = [a for a in back["臂"] if a["名"] == "hybrid"]
    assert hm and hm[0]["进主检验"] is True
    n_main = sum(1 for a in back["臂"] if a.get("进主检验"))
    print(f"已写 {STORE}")
    print(f"版本 {back['版本']}  哈希 {back['哈希'][:12]}…")
    print(f"臂 {len(back['臂'])} 个；进主检验 {n_main} 个（BH 的 m = 进主检验数 − 基线臂）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
