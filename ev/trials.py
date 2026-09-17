"""
ev/trials.py —— 前瞻配对对照实验（NetPayout WP3 后半段）

## 实验设计

    A 组 = 模型选号（ev/selection.select_tickets，按一等奖实得 E[min(cap,池/(1+X))] 降序）
    B 组 = 阴性对照（ev/selection.control_tickets，**同一候选池**随机取、同低重叠约束）

    两组来自同一个候选池、同样的形态与重叠约束，
    **唯一差异 = 有没有按实得排序**。

## 两个终点，性质完全不同（⚠️ 必须区分，否则会把实验读错）

  【主终点｜可完成】中奖率等价性（安慰剂检验）
      理论预期 A/B 中奖率**完全相等**——因为 ev/payout.grade_probabilities 只返回
      组合精确概率（与号码无关），排序在数学上不可能改变中奖概率。
      所以这个终点的作用是**证伪"模型作弊/实现 bug"**：
        - A 显著高于 B → 说明某处破坏了"概率与号码无关"的契约（严重 bug，必须查）
        - 无差异        → 证明选号确实不改变中奖概率（正面结论，可直接对外）
      功效：六等奖概率约 1/15，100 注/期 → 每期约 6.7 注中奖，
            约 5 个月可检出 20% 差异，约 1.6 年可检出 10% 差异。
      → 这是本实验**唯一能在人类时间尺度内得出结论**的终点。

  【次终点｜不可完成】实得奖金差异
      需要真实中一/二等奖事件才会产生差异（固定奖不分薄，冷门优势只体现在浮动奖）。
      双色球每期 n 注、年 156 期 → 中一次头奖的期望等待约
      17,721,088 / (156 × n) 年（n=100 → **约 1136 年**；二等奖约 76 年）。
      → **本终点永远无法给出显著结论**，其数据只作长期存档与异常告警。
      真正的实得提升证据来自历史反事实回测（ev/popularity.counterfactual_backtest，3292 期）。

## 数据落盘
    training/trials/<彩种>.jsonl（append-only，一行一期；training/ 已 gitignore）
    结算时按「期号」回填，不新增行。

## 写保护
    遵循项目三道闸：pytest 进程内默认拒写（WORKBUDDY_ALLOW_PROD_FB_WRITE=1 放行）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config import TRIAL_ARMS

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TRIAL_DIR = BASE_DIR / "training" / "trials"

# 参与实验的彩种：双色球=实验组（WP1 GO）；大乐透=阴性对照（WP1 NO-GO）
# 数字型不参与（固定赔率无奖金侧差异；七星彩待 WP2 接入后再议）
TRIAL_LOTTERIES = ("双色球", "大乐透")

# 每年开奖期数（用于样本量/等待时间估算；每周3期×52≈156，数字型每日≈358）
_DRAWS_PER_YEAR = {"双色球": 156, "大乐透": 156, "七星彩": 156,
                   "排列5": 358, "福彩3D": 358, "排列3": 358}

_DEFAULT_ARMS = TRIAL_ARMS   # 每组注数（config.TRIAL_ARMS，与主流水线出号数解耦）
_DEFAULT_POOL = 20000    # 候选池大小（与 select_tickets 默认一致）


# ---------------- 存储 ----------------
def _path(lottery: str) -> Path:
    return TRIAL_DIR / f"{lottery}.jsonl"


def _writable() -> bool:
    """三道闸之一：pytest 内默认拒写生产目录。"""
    try:
        from data.feedback import _is_pytest
        if _is_pytest() and os.environ.get("WORKBUDDY_ALLOW_PROD_FB_WRITE") != "1":
            return False
    except Exception:
        pass
    return True


def _read_rows(lottery: str) -> List[dict]:
    p = _path(lottery)
    if not p.exists():
        return []
    rows = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception as e:
        logger.warning(f"[trials] 读取 {p} 失败: {e}")
    return rows


def _write_rows(lottery: str, rows: List[dict]) -> None:
    """原子写（tmp + fsync + replace），与项目其它 JSON 落盘口径一致。"""
    p = _path(lottery)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------- 建组 ----------------
def build_arms(lottery: str, n: int = _DEFAULT_ARMS, pool_size: int = _DEFAULT_POOL,
               seed: Optional[int] = None, issue: Optional[str] = None) -> Dict[str, Any]:
    """
    生成 A/B 两组号码（同一候选池）。返回 {"期号","A":[...],"B":[...],"机制"}。
    B 组不做任何打分，只施加同样的低重叠约束 → 严格对照。
    """
    from ev.selection import _rand_tickets, control_tickets, select_tickets

    rng = np.random.default_rng(seed)
    cands = _rand_tickets(lottery, pool_size, rng)

    res = select_tickets(lottery, n=n, seed=seed, issue=issue, candidates=cands)
    if res.get("拒绝"):
        return {"拒绝": True, "原因": res.get("原因"), "彩种": lottery}

    a_tickets = [row["号码"] for row in res.get("选中", [])]
    b_tickets = control_tickets(lottery, cands, n=n, seed=(seed or 0) + 1)
    return {
        "彩种": lottery,
        "期号": res.get("期号"),
        "A": a_tickets,
        "B": b_tickets,
        "机制": res.get("机制", ""),
        "候选池": len(cands),
    }


def record_trial(lottery: str, n: int = _DEFAULT_ARMS, pool_size: int = _DEFAULT_POOL,
                 seed: Optional[int] = None, issue: Optional[str] = None) -> Dict[str, Any]:
    """
    为**下一期**记录一组 A/B 对照。同一目标期号已存在则跳过（幂等）。

    ⚠️ 期号口径（易错）：
      - 记录的「期号」= **下注目标期**（下一期，尚未开奖）
      - 打分的「打分基准期」= 最近已开奖期（奖池/票量只有开奖后才公开）
      两者必须分开存，否则结算时会拿"打分用的那一期"的开奖号去对号码，
      造成"用当期奖池选号、又用当期开奖号结算"的虚假对照。
    """
    if lottery not in TRIAL_LOTTERIES:
        return {"ok": False, "原因": f"{lottery} 不参与对照实验（无奖金侧差异或尚未接入）"}
    if not _writable():
        return {"ok": False, "原因": "pytest 进程内禁止写生产 trials 目录"}

    if issue is None:
        try:
            from data.feedback import _next_issue
            issue = _next_issue(lottery)
        except Exception as e:
            return {"ok": False, "原因": f"无法确定下一期期号: {e}"}
    if issue is None:
        return {"ok": False, "原因": "无法确定下一期期号"}

    arms = build_arms(lottery, n=n, pool_size=pool_size, seed=seed, issue=None)
    if arms.get("拒绝"):
        return {"ok": False, "原因": arms.get("原因")}
    iss = str(issue)

    rows = _read_rows(lottery)
    if any(str(r.get("期号")) == iss for r in rows):
        return {"ok": True, "跳过": True, "期号": iss, "原因": "本期已记录"}

    rows.append({
        "期号": iss,
        "打分基准期": arms.get("期号"),
        "记录时间": datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "每组注数": len(arms["A"]),
        "候选池": arms["候选池"],
        "机制": arms["机制"],
        "A": arms["A"],
        "B": arms["B"],
        "结算": None,
    })
    _write_rows(lottery, rows)
    return {"ok": True, "跳过": False, "期号": iss, "A注数": len(arms["A"]), "B注数": len(arms["B"])}


# ---------------- 结算 ----------------
def _grade_of(lottery: str, ticket: Dict[str, Any], actual_zones: Dict[str, List[int]]) -> str:
    """按官方规则判奖级（复用 data.feedback 的分类函数，避免口径漂移）。"""
    from data.feedback import _classify_dlt_prize, _classify_prize

    red_hits = len(set(ticket.get("红球", [])) & set(actual_zones.get("红球", [])))
    blue_hits = len(set(ticket.get("蓝球", [])) & set(actual_zones.get("蓝球", [])))
    if lottery == "大乐透":
        return _classify_dlt_prize(red_hits, blue_hits)
    return _classify_prize(red_hits, blue_hits)


def _payout_of(lottery: str, ticket: Dict[str, Any], grade: str, issue: str,
               cache: Dict[str, Dict[str, Any]]) -> Optional[float]:
    """
    当期实得单注（含当期实际奖金 / 分薄 / 封顶），口径与 payout_eff 一致。
    同一注的 payout_eff 结果按号码缓存（同彩种同期各奖级只需算一次）。
    """
    from ev.payout import payout_eff

    if grade == "未中":
        return 0.0
    key = json.dumps(ticket, ensure_ascii=False, sort_keys=True)
    r = cache.get(key)
    if r is None:
        r = payout_eff(lottery, ticket, issue=issue)
        cache[key] = r
    if "error" in r:
        return None
    row = next((x for x in r.get("奖级明细", []) if x["奖级"] == grade), None)
    if row is None:
        return None
    v = row.get("实得单注")
    return 0.0 if v is None else float(v)


def settle_trials(lottery: str) -> Dict[str, Any]:
    """
    对所有未结算且已开奖的期次回填实得奖金。
    返回 {"彩种","新结算期数","已结算期数","未开奖期数"}
    """
    if not _writable():
        return {"彩种": lottery, "error": "pytest 进程内禁止写生产 trials 目录"}

    rows = _read_rows(lottery)
    if not rows:
        return {"彩种": lottery, "新结算期数": 0, "已结算期数": 0, "未开奖期数": 0}

    from data.loader import load_lottery
    from data.schema import get_schema, normalize_zone_names

    data = load_lottery(lottery)
    rec_map = {str(r.期号): r for r in data.records}
    schema = get_schema(lottery)

    cache: Dict[str, Dict[str, Any]] = {}
    newly, pending = 0, 0
    for row in rows:
        if row.get("结算"):
            continue
        rec = rec_map.get(str(row["期号"]))
        if rec is None:
            pending += 1
            continue
        az = normalize_zone_names(rec.zone_numbers, schema)
        iss = str(row["期号"])
        side = {}
        for arm in ("A", "B"):
            total, hits, detail = 0.0, 0, {}
            for t in row.get(arm, []):
                g = _grade_of(lottery, t, az)
                if g != "未中":
                    hits += 1
                    detail[g] = detail.get(g, 0) + 1
                    v = _payout_of(lottery, t, g, iss, cache)
                    total += 0.0 if v is None else v
            side[arm] = {"中奖注数": hits, "奖级分布": detail, "实得合计": round(total, 2)}
        row["结算"] = {
            "结算时间": datetime.now().isoformat(timespec="seconds"),
            "开奖": {k: v for k, v in az.items()},
            "A": side["A"],
            "B": side["B"],
            "Δ": round(side["A"]["实得合计"] - side["B"]["实得合计"], 2),
        }
        newly += 1

    if newly:
        _write_rows(lottery, rows)
    return {"彩种": lottery, "新结算期数": newly,
            "已结算期数": sum(1 for r in rows if r.get("结算")), "未开奖期数": pending}


# ---------------- 统计 ----------------
def _sign_martingale(signs: List[int], c: float = 0.5) -> Tuple[float, List[float]]:
    """
    配对符号的序贯 e-value（任意停时有效）。
    H0: P(sign=+1)=0.5 → W_t = Π(1 + c·s_t)，E[W_t|H0]=1，
    Ville 不等式 → P(∃t: W_t ≥ 1/α) ≤ α。取 c=0.5（保守）。
    """
    W, path = 1.0, []
    for s in signs:
        W *= (1.0 + c * s)
        path.append(W)
    return W, path


def power_plan(lottery: str, n_per_issue: int = _DEFAULT_ARMS,
               effects: Tuple[float, ...] = (0.20, 0.10)) -> Dict[str, Any]:
    """
    主终点（中奖率等价性）的功效规划：检出 X% 相对差异需要多少期 / 多少年。
    两样本率检验（保守，未计配对带来的方差削减）：
        n = (z_{α/2}+z_β)² × [p(1-p) + p'(1-p')] / (p'-p)²
    """
    from ev.payout import grade_probabilities

    probs = grade_probabilities(lottery)
    p = float(sum(v for k, v in probs.items() if k != "未中"))
    if p <= 0:
        return {"彩种": lottery, "error": "无法取得中奖概率"}
    dpy = _DRAWS_PER_YEAR.get(lottery, 156)
    z = 1.959963985 + 0.8416212336      # α=.05 双侧 + power=.8
    out = []
    for eff in effects:
        p2 = min(p * (1 + eff), 0.999)
        need = z ** 2 * (p * (1 - p) + p2 * (1 - p2)) / max((p2 - p) ** 2, 1e-18)
        need = math.ceil(need / max(n_per_issue, 1))     # 期数
        out.append({"相对效应": f"+{eff:.0%}", "需要注数_每组": math.ceil(need * n_per_issue),
                    "需要期数": need, "折合年数": round(need / dpy, 2)})
    return {
        "彩种": lottery,
        "每注中奖概率": round(p, 6),
        "每年期数": dpy,
        "每组注数": n_per_issue,
        "规划": out,
        "说明": "主终点=中奖率等价性（预期差异为 0，用于检出模型作弊/bug）；"
                "实得奖金差异需真实中一/二等奖事件，见 wait_time_years()",
    }


def wait_time_years(lottery: str, n_per_issue: int = _DEFAULT_ARMS) -> Dict[str, Any]:
    """次终点（实得奖金差异）的诚实样本量声明：中一次各奖级的期望等待年数。"""
    from ev.payout import grade_probabilities

    probs = grade_probabilities(lottery)
    dpy = _DRAWS_PER_YEAR.get(lottery, 156)
    per_year = dpy * max(n_per_issue, 1)
    out = {}
    for g in ("一等", "二等"):
        p = probs.get(g)
        if not p:
            continue
        out[g] = {"概率": f"1/{round(1/p):,}", "期望等待年数": round(1.0 / (per_year * p), 1)}
    return {
        "彩种": lottery,
        "每组注数": n_per_issue,
        "每年注数": per_year,
        "期望等待": out,
        "结论": ("实得奖金终点在人类时间尺度内不可完成——"
                 "其证据只能来自历史反事实回测（ev/popularity.counterfactual_backtest）"),
    }


def trial_report(lottery: str, alpha: float = 0.05,
                 boot: int = 2000, seed: int = 42) -> Dict[str, Any]:
    """
    对照实验报告。两个终点分开陈述，绝不混为一谈。
    """
    rows = _read_rows(lottery)
    settled = [r for r in rows if r.get("结算")]
    n_issue = len(settled)
    base: Dict[str, Any] = {
        "彩种": lottery,
        "总记录期数": len(rows),
        "已结算期数": n_issue,
        "未开奖期数": len(rows) - n_issue,
        "每组注数": (settled[0].get("每组注数") if settled else _DEFAULT_ARMS),
    }
    if n_issue < 2:
        base.update({
            "裁决": "样本不足以检验",
            "建议": (f"至少积累 30 期再下结论（当前 {n_issue} 期）。"
                     "可用 `python cli.py trials " + lottery + " --plan` 看功效规划。"),
        })
        base["功效规划"] = power_plan(lottery, base["每组注数"])
        base["次终点样本量"] = wait_time_years(lottery, base["每组注数"])
        return base

    a_hit = np.array([r["结算"]["A"]["中奖注数"] for r in settled], dtype=float)
    b_hit = np.array([r["结算"]["B"]["中奖注数"] for r in settled], dtype=float)
    a_amt = np.array([r["结算"]["A"]["实得合计"] for r in settled], dtype=float)
    b_amt = np.array([r["结算"]["B"]["实得合计"] for r in settled], dtype=float)
    d_hit = a_hit - b_hit
    d_amt = a_amt - b_amt

    from scipy import stats as sps

    # ---- 主终点：中奖率等价性（配对符号检验 + 序贯 e-value） ----
    nz = d_hit[d_hit != 0]
    k, pos = int(len(nz)), int((nz > 0).sum())
    if k > 0:
        p_sign = float(sps.binomtest(pos, k, 0.5, alternative="two-sided").pvalue)
        e_val, e_path = _sign_martingale([1 if x > 0 else -1 for x in nz])
    else:
        p_sign, e_val, e_path = 1.0, 1.0, []

    # 等价性（TOST）：差异是否落在 ±10% 边际内
    tot_a, tot_b = float(a_hit.sum()), float(b_hit.sum())
    margin = 0.10 * max(tot_b, 1.0)
    diff = tot_a - tot_b
    equivalent = bool(abs(diff) <= margin)

    # ---- 次终点：实得奖金（仅存档，明确标注不可下结论） ----
    rng = np.random.default_rng(seed)
    if n_issue > 1 and d_amt.std() > 0:
        idx = rng.integers(0, n_issue, size=(boot, n_issue))
        boots = d_amt[idx].sum(axis=1)
        ci = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]
    else:
        ci = [float(d_amt.sum()), float(d_amt.sum())]

    base.update({
        # 主终点
        "A累计中奖注数": int(tot_a),
        "B累计中奖注数": int(tot_b),
        "中奖注数差": int(diff),
        "有差异期数": k,
        "A占优期数": pos,
        "符号检验p": round(p_sign, 5),
        "序贯e_value": round(float(e_val), 4),
        "e_value峰值": round(float(max(e_path)), 4) if e_path else 1.0,
        "等价边际±10%": round(margin, 1),
        "等价性成立": equivalent,
        # 次终点
        "A累计实得": round(float(a_amt.sum()), 2),
        "B累计实得": round(float(b_amt.sum()), 2),
        "实得差Δ": round(float(d_amt.sum()), 2),
        "Δ95%CI": [round(ci[0], 2), round(ci[1], 2)],
    })

    # ---- 裁决 ----
    if k >= 10 and p_sign < alpha and not equivalent:
        verdict = ("⚠️ 主终点异常：A/B 中奖率出现显著差异。"
                   "选号在数学上不改变中奖概率 → 这大概率是实现 bug，请立即排查 "
                   "（检查 grade_probabilities 是否被号码相关逻辑污染）")
    elif equivalent:
        verdict = ("✅ 主终点成立：A/B 中奖率在 ±10% 边际内等价 —— "
                   "与「选号不改变中奖概率」的理论预期一致（未发现作弊或 bug）")
    else:
        verdict = (f"主终点尚无结论：有差异期数仅 {k} 期（需 ≥10 期且样本足够），继续积累")

    base["裁决"] = verdict
    base["次终点声明"] = ("实得奖金差异不可检验（需真实中一/二等奖事件）；"
                          f"当前 Δ={d_amt.sum():.2f} 元仅为存档，不构成证据")
    base["功效规划"] = power_plan(lottery, base["每组注数"])
    base["次终点样本量"] = wait_time_years(lottery, base["每组注数"])
    return base


def pipeline_step(lottery: str, n: int = _DEFAULT_ARMS) -> Optional[str]:
    """
    自动流水线内的一步：结算已开奖期次 + 为下一期记录 A/B 对照。

    返回 steps 文本（非参与彩种返回 None）。
    ⚠️ 调用方必须包 try/except：本步骤只积累证据、不参与出号，
       任何失败都应降级，绝不阻塞主流水线。
    """
    if lottery not in TRIAL_LOTTERIES:
        return None
    st = settle_trials(lottery)
    rc = record_trial(lottery, n=n)
    if rc.get("ok"):
        tail = f"跳过(期{rc.get('期号')}已有)" if rc.get("跳过") else f"记录期{rc.get('期号')}"
    else:
        tail = f"记录失败({rc.get('原因', '未知')})"
    return f"A/B对照: 结算{st.get('新结算期数', 0)}期, {tail}"


def trial_summary_all() -> Dict[str, Any]:
    return {lot: trial_report(lot) for lot in TRIAL_LOTTERIES}


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(errors="replace")
    for lot in (sys.argv[1:] or list(TRIAL_LOTTERIES)):
        print(json.dumps(trial_report(lot), ensure_ascii=False, indent=1))
