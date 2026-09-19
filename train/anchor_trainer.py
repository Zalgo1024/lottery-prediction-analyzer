# -*- coding: utf-8 -*-
"""随机锚点走前训练器（用户提出的"马后炮"模式的正式实现）。

模式：随机抽一个历史期 t 作为目标（锚点），**严格只用 t 之前的数据**生成预测，
再与 t 期实际开奖比对。与 `rolling_train` 的区别：锚点随机、覆盖全部历史，
而不是固定评最近 N 期——样本更多元，也不偏向近期。

诚实设计（与全项目口径一致）：
- 每个锚点**同时**评估一个同形态随机预测作基线（同成本、同指标、天然可比）；
- 显著性 = 策略平均总命中 − 随机基线平均总命中 的配对 z 检验（正态近似）；
- 只标注「本次抽样」显著性；最终裁决仍以 credibility 裁判层（FDR）为准；
- 每次试验**全量落账**（JSONL），不挑好的、不丢差的。
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR
from data.loader import load_lottery
from data.schema import is_redblue, schema_from_cfg

# 复用 rolling_trainer 的策略与评估口径（同一实现，避免口径漂移）
from train.rolling_trainer import (  # noqa: E402
    _strategy_high_freq,
    _strategy_missing_value,
    _strategy_balanced,
    _strategy_rule_based,
    _evaluate_hit,
)

logger = logging.getLogger(__name__)

DEFAULT_STRATEGIES = ["高频策略", "遗漏值策略", "区间均衡策略", "规则优选"]
STRATEGY_FUNCS = {
    "高频策略": _strategy_high_freq,
    "遗漏值策略": _strategy_missing_value,
    "区间均衡策略": _strategy_balanced,
}
RANDOM_LABEL = "随机基线"


def _ledger_dir() -> Path:
    return Path(TRAINING_DIR) / "anchor_trials"


def _ledger_path(lottery_name: str) -> Path:
    code = LOTTERY_CONFIG.get(lottery_name, {}).get("name_en", lottery_name)
    return _ledger_dir() / f"{code}.jsonl"


def _random_pred(rng: random.Random, schema) -> dict:
    """与策略同形态的随机预测（每区抽 choose 个、不重复），作为天然基线。"""
    zones = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        zones[zone.name] = sorted(rng.sample(range(lo, hi + 1), zone.choose))
    return {"zones": zones, "策略": RANDOM_LABEL}


def _z_label(z: float) -> str:
    if z >= 2.0:
        return "本次抽样显著超随机(z≥2，未做FDR，勿当真实优势)"
    if z <= -2.0:
        return "本次抽样显著低于随机(z≤2)"
    return "不显著"


def anchor_train(
    lottery_name: str,
    *,
    n_trials: int = 10,
    window_size: int = 50,
    min_history: int = 100,
    strategies: Optional[List[str]] = None,
    seed: Optional[int] = None,
    records: Optional[List] = None,
    write_ledger: bool = True,
) -> dict:
    """随机锚点走前验证：抽 n_trials 个历史期作目标，只用目标期之前的数据预测。

    - records: 可注入**从旧到新**的 DrawRecord 列表（测试用）；None 则加载真实数据
      （data.records 是从新到旧，内部会反转成时间序）。
    - 每个锚点同时评估随机基线，配对比较，样本内 z 显著性如实标注。
    - write_ledger=True 时逐试验追加到 training/anchor_trials/<代码>.jsonl。
    """
    strategies = strategies or DEFAULT_STRATEGIES
    if seed is None:
        seed = int(time.time() * 1000) % (2 ** 31)
    rng = random.Random(seed)

    if records is None:
        chrono = list(reversed(load_lottery(lottery_name).records))
    else:
        chrono = list(records)

    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg, lottery_name)
    is_rb = is_redblue(lottery_name)

    lo_t = max(window_size, min_history)
    pool = list(range(lo_t, len(chrono)))
    if not pool:
        return {"error": f"数据不足：需要至少 {lo_t + 1} 期，当前 {len(chrono)} 期"}

    if len(pool) >= n_trials:
        anchors = sorted(rng.sample(pool, n_trials))
    else:  # 池子小于试验数就放回抽样（如实记录，不静默缩量）
        anchors = [rng.choice(pool) for _ in range(n_trials)]

    names = list(strategies) + [RANDOM_LABEL]
    rows: List[dict] = []
    diffs: Dict[str, List[float]] = {s: [] for s in strategies}

    for t in anchors:
        train_window = chrono[t - window_size: t]      # 严格时间切分：只含 t 之前
        actual = chrono[t]
        base_pred = _random_pred(rng, schema)
        preds = []
        for s in strategies:
            if s == "规则优选":
                # 规则优选内部是拒绝采样（用全局 random），不接收种子 →
                # 临时播种 + 还原，保证同种子可复现，且不污染进程全局随机状态
                _saved = random.getstate()
                try:
                    random.seed(seed * 100003 + t)
                    preds.append(_strategy_rule_based(train_window, cfg, schema, is_rb))
                finally:
                    random.setstate(_saved)
            else:
                preds.append(STRATEGY_FUNCS[s](train_window, cfg, schema))
        preds.append(base_pred)

        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "lottery": lottery_name,
            "mode": "anchor",
            "seed": seed,
            "window": window_size,
            "anchor_idx": t,
            "期号": actual.期号,
            "开奖日期": str(actual.开奖日期) if actual.开奖日期 else "",
            "hits": {},
        }
        for p in preds:
            h = _evaluate_hit(p, actual, schema, is_rb, lottery_name)
            row["hits"][p["策略"]] = {"总命中": h["总命中"], "是否中奖": h["是否中奖"]}
        rows.append(row)
        base_total = row["hits"][RANDOM_LABEL]["总命中"]
        for s in strategies:
            diffs[s].append(row["hits"][s]["总命中"] - base_total)

    # 汇总：配对差值 z 检验（正态近似）
    stats = {}
    for s in strategies:
        d = np.asarray(diffs[s], dtype=float)
        mean = float(d.mean()) if d.size else 0.0
        se = float(d.std(ddof=1) / np.sqrt(d.size)) if d.size > 1 else 0.0
        z = mean / se if se > 0 else 0.0
        stats[s] = {
            "样本数": len(d),
            "平均总命中": round(float(np.mean([r["hits"][s]["总命中"] for r in rows])), 4),
            "基线平均总命中": round(float(np.mean([r["hits"][RANDOM_LABEL]["总命中"] for r in rows])), 4),
            "配对差值": round(mean, 4),
            "配对SE": round(se, 4),
            "z": round(z, 3),
            "显著性": _z_label(z),
        }

    result = {
        "lottery_name": lottery_name,
        "mode": "anchor",
        "seed": seed,
        "n_trials": len(anchors),
        "window_size": window_size,
        "min_history": min_history,
        "is_redblue": is_rb,
        "training_time": datetime.now().isoformat(timespec="seconds"),
        "anchor_issues": [chrono[t].期号 for t in anchors],
        "stats": stats,
        "trials": rows,
        "诚实声明": "随机锚点走前验证；显著性为本次抽样结论，未做FDR，最终以裁判层为准。"
                  "彩票零期望，任何「超随机」都应先视为多重比较假象。",
    }

    if write_ledger:
        try:
            p = _ledger_path(lottery_name)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            result["ledger"] = str(p)
        except Exception as e:  # 落账失败不阻塞训练本身
            logger.warning(f"锚点试验落账失败 {lottery_name}: {e}")
            result["ledger_error"] = str(e)[:150]

    return result


def anchor_summary(lotteries: Optional[List[str]] = None) -> dict:
    """读取全部锚点试验台账，按彩种汇总（供看板/接口展示，全量如实）。"""
    lotteries = lotteries or list(LOTTERY_CONFIG.keys())
    out = {}
    for lot in lotteries:
        p = _ledger_path(lot)
        if not p.exists():
            out[lot] = {"试验数": 0}
            continue
        rows = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # 坏行跳过但计数
        per_strat: Dict[str, list] = {}
        for r in rows:
            for s, h in (r.get("hits") or {}).items():
                per_strat.setdefault(s, []).append(h["总命中"])
        strat_stats = {}
        base = per_strat.pop(RANDOM_LABEL, [])
        base_mean = float(np.mean(base)) if base else 0.0
        for s, vals in per_strat.items():
            strat_stats[s] = {
                "试验数": len(vals),
                "平均总命中": round(float(np.mean(vals)), 4) if vals else 0.0,
            }
        out[lot] = {
            "试验数": len(rows),
            "坏行数": sum(1 for r in rows if not isinstance(r, dict)),
            "最新试验": rows[-1].get("ts") if rows else "",
            "基线平均总命中": round(base_mean, 4),
            "策略": strat_stats,
        }
    return out
