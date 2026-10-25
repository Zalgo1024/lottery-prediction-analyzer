# -*- coding: utf-8 -*-
"""按 `config/preregistration.json` 的 `格_Stage2` 实跑 Stage 2（倍投对照）。

★ 本脚本是 Stage 2 结论的**唯一产出路径**。在它跑起来之前，项目里不存在任何
  Stage 2 数字，所以不存在「先看结果再补登记」的问题。

问的是什么
----------
已经由 Stage 1 选出的**同一副票集**，在同一笔预算（K 注）下，是摊成 K 张互异票
还是压成更少票 × 倍数，「同预算达标率」差多少（金额侧只作描述 + 一致性检验）。

★ 这里量的是**代价** —— 「买更少去重票会损失多少覆盖率」，
  不是「哪个倍投方案更好」。报告阶段不得越界成策略结论。

用法：PYTHONUTF8=1 E:/Python/python.exe -u scripts/oneoff/_arena_stage2_run.py
输出：output/_arena_stage2.json + 控制台摘要
"""
from __future__ import annotations

import json
import math
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, r"E:\707")

import numpy as np  # noqa: E402

from ev import preregistration as PR                # noqa: E402
from ev.arena import arms as A                       # noqa: E402
from ev.arena import periods as PD                   # noqa: E402
from ev.arena import stage2 as S                     # noqa: E402
from ev.arena import stats as ST                     # noqa: E402
from ev.arena.runner import _records_for  # noqa: E402
from ev.coverage_backtest import build_pool           # noqa: E402

OBJ = ("all_single", "few_double", "incremental_coverage")
RATIOS = (0.05, 0.10, 0.20)
OUT_JSON = r"E:\707\output\_arena_stage2.json"
OUT_MD = r"E:\707\output\_arena_stage2.md"


def _fingerprint(tickets: List[Dict[str, Any]]) -> str:
    """票集指纹：用来证明 Stage 2 用的正是 Stage 1 那副票（逐位可复现校验）。"""
    import hashlib
    blob = json.dumps(
        [{k: sorted(map(str, v)) if isinstance(v, list) else str(v)
          for k, v in sorted(t.items())} for t in tickets],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    doc = PR.load()
    const = PR.constants(doc)
    s2 = PR.stage2(doc)
    lottery = s2["彩种"]
    K = int(s2["K"])
    target = s2["目标"]
    cc = s2["常量"]
    rule_mode = cc.get("规则口径", "current")
    drop = list(cc.get("剔除规则版本") or [])
    arms = list(s2["票集来源"]["臂"])
    seeds = list(s2["票集来源"]["seed"])

    print("=" * 118)
    print(f"预注册 v{doc['版本']} 锁定日 {doc['锁定日']} 哈希 {doc['哈希'][:12]}…")
    print(f"Stage 2：{lottery} K={K} 预算 {K*int(float(cc['单注成本元']))} 元/期 | "
          f"票集来源 {arms} seed={seeds} | 口径 {rule_mode}")
    print(f"方案网格 {len(OBJ)-1} 目标 × {len(RATIOS)} 比值 + all_single；"
          f"monetary_threshold 已显式排除（κ=0 下退化为 all_single）")
    print("=" * 118, flush=True)

    issues = PD.build_periods(lottery, None, exclude_rule_versions=drop or None)
    recs = _records_for(lottery, issues)
    target_issue = recs[-1].期号
    prof = PD.profile(lottery, issues)
    print(f"期窗 {prof['期数']} 期 {prof['期号起']}~{prof['期号止']} | "
          f"规则版本 {prof['规则版本数']} 个", flush=True)

    pool = build_pool(lottery, int(const["候选池规模"]), seed=int(const["候选池seed"]))
    print(f"候选池 {len(pool)}", flush=True)

    all_out: List[Dict[str, Any]] = []
    for arm in arms:
        for seed in seeds:
            t1 = time.time()
            tks, meta = A.select_arm(arm, lottery, pool, K, target, target_issue,
                                     seed=int(seed), cap=int(const["重叠上限"]),
                                     params={"m_fit": int(const["MC拟合采样数"]),
                                             "m_eval": int(const["MC评估采样数"]),
                                             "eval_seed_offset": int(const["评估seed偏移"]),
                                             "n_periods": len(issues)})
            fp = _fingerprint(tks)
            print(f"\n[{arm} seed={seed}] 票 {len(tks)} 张 | 指纹 {fp} | "
                  f"选号 {time.time()-t1:.0f}s", flush=True)
            cell = S.run_stage2_cell(
                lottery, tks, recs, target, rule_mode=rule_mode,
                target_issue=target_issue, objectives=OBJ, n2_ratios=RATIOS,
                cost_per_bet=float(const["单注成本元"]), tax=False,
                arm_name=arm, verbose=True)

            # ---------------- 达标侧配对 + BH
            # family = 该票集下的 6 个倍投方案（跨 arm 不合并：不同票集不是同一族检验）
            objs = cell.pop("对象")
            base = objs.get("all_single")
            hit_pairs: List[Dict[str, Any]] = []
            keys: List[str] = []
            for key, o in objs.items():
                if key == "all_single":
                    continue
                hp = ST.pair(o.hit.astype(float), base.hit.astype(float),
                             issues_a=o.issues, issues_b=base.issues,
                             name_a=key, name_b="all_single", metric="binary",
                             effect_source="实测（每期达标与否，二值）")
                hp = ST.achievable(hp, len(o.issues))
                hp["p"] = ST.z_to_p_two_sided(hp.get("z_实测") or float("nan"))
                hit_pairs.append(hp)
                keys.append(key)
            adj = ST.adjust_multiplicity([h["p"] for h in hit_pairs], method="bh")
            for hp, q in zip(hit_pairs, adj["校正后p"]):
                hp["q_BH"] = q
                d = hp.get("实测Δ")
                n_ok = hp.get("n*_可用期数比")
                lo, hi = hp.get("CI95下"), hp.get("CI95上")
                # sd==0（两序列逐位相同）时 CI 会退化成 None ⇒ 不能当成「CI 含 0」
                ci_excludes_zero = isinstance(lo, float) and isinstance(hi, float) \
                    and math.isfinite(lo) and math.isfinite(hi) and (lo > 0 or hi < 0)
                if d is not None and d > 0:
                    hp["判决"] = "实现缺陷嫌疑（倍投不该让达标率上升，违反 I2）"
                elif ci_excludes_zero and n_ok is not None and n_ok <= 1.0 and q <= 0.05:
                    hp["判决"] = "可判（方向为负：倍投拉低达标率）"
                else:
                    hp["判决"] = "不可判（识别不出该量级的代价）"

            cell["票集指纹"] = fp
            cell["选号meta"] = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
            cell["达标侧配对"] = hit_pairs
            cell["多重校正"] = {k: v for k, v in adj.items()} if adj["检验数"] else adj
            all_out.append(cell)

            inv = cell["不变式"]
            print(f"  不变式：{'通过' if inv['通过'] else '*** 违反 ' + str(inv['违反'])}",
                  flush=True)
            print(f"  {'方案':<30}{'去重':>5}{'实投':>6}{'达标率':>9}{'达标Δ':>9}"
                  f"{'z':>7}{'q_BH':>9}{'n*比':>8}  判决", flush=True)
            print("  " + "-" * 100, flush=True)
            for r in cell["方案"]:
                hp = next((h for h in hit_pairs if h["臂A"] == r["臂"]), {})
                ns = hp.get("n*_可用期数比")
                print(f"  {r['臂']:<30}{r['去重注数']:>5}{r['总注数']:>6}"
                      f"{r['达标率']:>9.4f}{hp.get('实测Δ', float('nan')):>9.4f}"
                      f"{hp.get('z_实测', float('nan')):>7.2f}{hp.get('q_BH', float('nan')):>9.2e}"
                      f"{(f'{ns:.2f}' if isinstance(ns, float) else '—'):>8}  "
                      f"{hp.get('判决', '（基线）')}", flush=True)
            print(f"  金额侧：预估不可判（E[实得] 与分配无关，期望差恒为 0）",
                  flush=True)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"预注册": {"版本": doc["版本"], "哈希": doc["哈希"]},
                   "Stage2规格": {k: v for k, v in s2.items() if not k.startswith("_")},
                   "结果": all_out}, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n已写 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
