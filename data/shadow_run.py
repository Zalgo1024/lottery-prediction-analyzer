# -*- coding: utf-8 -*-
"""影子三臂的**每期执行与开奖后结算**（`data/shadow.py` 只负责记账与纪律）。

要回答的问题：**同样的注数、同样的钱，覆盖选号比不选号多拿回了多少？**

三臂（定义见 `data.shadow`）：

| 臂 | 号码来源 | 有信息？ |
|---|---|---|
| 当前模型 | 生产 pending 的实际号码 | 是（用了历史统计） |
| 等注数随机 | 同一候选池 + `control_tickets`（cap 约束） | 否 |
| 固定规则 | **同一候选池的自然顺序** + 同样 cap 约束，不打乱不排序 | 否 |

★ 为什么要有两个「无信息」臂：随机臂与固定规则臂的唯一差别是「打不打乱池顺序」。
  若这两臂差异显著，说明差异来自随机化本身，与选号质量无关 —— 这是把
  「模型打败的是随机」与「模型打败的是顺序」分开的判据。两臂都不许用任何
  历史统计，否则它们就不再是零假设基线。

★ 四条硬纪律（每条对应一个已知的自欺方式）：

1. **候选池必须远大于 K**。实测 m=2000 时双色球 K=500 在 cap=3 下只挑出 435 注
   （重叠约束凑不满）；池不够大时随机臂会静默给出**更少的注数**，
   三臂就不同预算了。故 `POOL_SIZE=8000`（与研究口径 `候选池规模` 一致），
   且生成后**必须校验 len==K**，不足则记 `结算失败`，绝不悄悄少买。
2. **同一期内三臂共用同一个池**（池 seed 由 (彩种, 期号) 派生）。
   若各臂各建一个池，比较就退化成「两批随机样本」，测不出任何东西。
3. **不拿已开奖的期补录**。补录等于事后生成，无法自证提前出号 ⇒
   影子只由调度器在开奖前写；`settle()` 只结算**已存在的记录行**。
4. **停发期也要留行**（`注数=0`）。只保留出过号的期，等于把不利样本删掉；
   分母纪律同样适用于影子（`开奖期数 / 参与期数` 三数并列）。

★ 本模块不改变任何出号决策，也不占用预算（`记账=False`，见 `tests/test_shadow_no_budget.py`）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence

from ev.coverage_backtest import build_pool

from data import shadow as SH

logger = logging.getLogger(__name__)

#: 候选池规模。与研究口径 `config/preregistration.json::候选池规模` 一致。
#: ★ 不要按「省时间」调小：m 太小时 cap 约束凑不满 K，三臂注数就不一致了。
POOL_SIZE = 8000

#: 判级口径说明（写进每一行结算，供汇总时披露）。
GRADE_NOTE = (
    "逐票判级（复用 ev.profit.settle._grade）；固定奖按**当期规则表单注固定金额**"
    "（不含派奖加奖），浮动奖（一等/二等）的金额记入「金额未知」并从求和中排除。"
    "三臂口径完全一致，可直接横向比。"
)


def _seed_of(lottery: str, issue: Any, salt: str) -> int:
    """确定性 seed：同期同臂可复现，跨期不同。

    ★ 必须确定性：重跑同一期（自愈/重试）要得到同一批号码，否则影子台账里
      同一期会出现两套号码，事后无法判断当时到底买了什么。
    """
    key = f"{lottery}|{issue}|{salt}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


def _fixed_pick(lottery: str, pool: Sequence[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    """固定规则臂：**按池的自然顺序**取前 k 张，施加与研究口径相同的重叠上限。

    不 shuffle、不排序、不打分 —— 「简单固定规则」的全部内容。
    """
    from ev.selection import _OVERLAP_CAP, _overlap

    cap = _OVERLAP_CAP.get(lottery, 3)
    picked: List[Dict[str, Any]] = []
    for t in pool:
        if len(picked) >= k:
            break
        if all(_overlap(t, u, lottery) <= cap for u in picked):
            picked.append(t)
    return picked


def build_arm_tickets(lottery: str, arm: str, k: int, *,
                      model_tickets: Optional[Sequence[Any]] = None,
                      issue: Any = None,
                      pool: Optional[Sequence[Dict[str, Any]]] = None
                      ) -> List[Any]:
    """生成某一臂的号码。`k<=0` 返回空（停发期）。"""
    if arm not in SH.ARMS:
        raise ValueError(f"未知影子臂：{arm}（只允许 {SH.ARMS}）")
    if int(k) <= 0:
        return []
    if arm == SH.ARM_MODEL:
        return list(model_tickets or [])[:int(k)]

    if pool is None:
        pool = build_pool(lottery, POOL_SIZE, seed=_seed_of(lottery, issue, "pool"))

    if arm == SH.ARM_RANDOM:
        from ev.selection import control_tickets
        return control_tickets(lottery, list(pool), int(k),
                               seed=_seed_of(lottery, issue, "random"))
    return _fixed_pick(lottery, pool, int(k))


def record_batch(lottery: str, issue: Any, model_tickets: Sequence[Any], *,
                 note: Optional[str] = None,
                 base: Optional[Any] = None) -> Dict[str, Any]:
    """为一期记下三臂的号码（幂等：该期已有记录则跳过）。

    `model_tickets` 是生产 pending 的实际号码 —— 三臂的注数 K 由它决定，
    这样「同注数」不是靠另外指定，而是天然成立。

    ★ 整个「查重 → 生成 → 写三臂」都在同一把锁内：三条触发路径可能几乎同时到达，
      锁外的查重会让两个进程都通过检查、各写一遍。
    """
    with SH.shadow_lock(lottery, base=base):
        return _record_locked(lottery, issue, model_tickets, note=note, base=base)


def _record_locked(lottery: str, issue: Any, model_tickets: Sequence[Any], *,
                   note: Optional[str] = None,
                   base: Optional[Any] = None) -> Dict[str, Any]:
    if str(issue) in {str(r.get("期号")) for r in SH.load(lottery, base=base)}:
        return {"状态": "跳过", "期号": str(issue), "原因": "该期影子已记录"}

    k = len(list(model_tickets or []))
    pool = None
    if k > 0:
        pool = build_pool(lottery, POOL_SIZE, seed=_seed_of(lottery, issue, "pool"))

    detail: Dict[str, Any] = {}
    for arm in SH.ARMS:
        try:
            tks = build_arm_tickets(lottery, arm, k, model_tickets=model_tickets,
                                    issue=issue, pool=pool)
        except Exception as e:                                # noqa: BLE001
            logger.warning(f"影子生成失败 {lottery} {arm} 期{issue}: {e}")
            SH.record(lottery, issue, arm, tickets=[],
                      result={"结算失败": True, "口径": f"生成失败：{e}"},
                      note=note, base=base)
            detail[arm] = {"注数": 0, "告警": f"生成失败：{e}"}
            continue

        if len(tks) != k:
            # ★ 绝不静默少买：池太小 / cap 太紧时随机臂会凑不满 K。
            #   若照记，三臂就变成不同注数，净收益率不可比，而表面上一切正常。
            msg = f"注数不足 {len(tks)}/{k}（池 {POOL_SIZE}，重叠上限约束太紧？）"
            logger.warning(f"影子 {lottery} {arm} 期{issue}：{msg}")
            SH.record(lottery, issue, arm, tickets=tks,
                      result={"结算失败": True, "口径": msg}, note=note, base=base)
            detail[arm] = {"注数": len(tks), "告警": msg}
            continue

        SH.record(lottery, issue, arm, tickets=tks, note=note, base=base)
        detail[arm] = {"注数": len(tks)}

    return {"状态": "已记录", "期号": str(issue), "注数": k, "臂": detail}


def settle(lottery: str, *, issues: Optional[Sequence[Any]] = None,
           base: Optional[Any] = None) -> Dict[str, Any]:
    """结算已开奖的影子期（只结算**已有记录行**，不补录）。

    返回 `{结算行数, 结算期数, 仍未开奖, 无号码行}`，便于调度日志直接读。
    """
    from data.loader import load_lottery
    from data.schema import get_schema, normalize_zone_names
    from ev.profit.rules import rules_for
    from ev.profit.settle import _grade

    schema = get_schema(lottery)
    try:
        recs = {str(getattr(r, "期号", "")): r for r in load_lottery(lottery).records}
    except Exception as e:                                    # noqa: BLE001
        return {"彩种": lottery, "错误": f"开奖数据不可读：{e}", "结算行数": 0}

    def _fixed_amount(issue: str) -> Dict[str, Optional[float]]:
        """该期固定奖的「奖级 → 单注金额」表；浮动奖映射为 None。

        ★ 为什么必须自己查规则表、而不是相信 `_grade` 返回的金额：
          `_grade` 对浮动奖走 `base_payout`，那是「假设只有你一个人中」的估算。
          冒烟时 50 注一等奖算出 **2.5 亿元** —— 数字合法、结论荒谬，
          而且它会直接把净收益率抬成正无穷，三臂对照当场作废。
          ⇒ 浮动奖一律记「金额未知」，不计入求和（与账本同一纪律）。
        """
        try:
            rules = rules_for(lottery, issue)
        except Exception:                                     # noqa: BLE001
            return {}
        out: Dict[str, Optional[float]] = {}
        for g, r in rules.grades.items():
            out[str(g)] = float(r.fixed) if (r.kind == "fixed" and r.fixed is not None) else None
        return out

    settled_rows = 0
    settled_issues = set()
    not_drawn: List[str] = []
    no_tickets = 0

    # ★ 读 todo 与逐行写入都要在同一把锁内：两个触发路径同时结算同一期时，
    #   锁外读会让双方都看到「未结算」，于是同一期被结算两遍（金额不重复计，
    #   但 `结算行数` 会翻倍，日志与实际不符）。
    with SH.shadow_lock(lottery, base=base):
        todo = [r for r in SH.load(lottery, base=base) if not r.get("结算")]
        if issues is not None:
            want = {str(i) for i in issues}
            todo = [r for r in todo if str(r.get("期号")) in want]

        for row in todo:
            iss = str(row.get("期号"))
            arm = row.get("臂")
            rec = recs.get(iss)
            if rec is None or not getattr(rec, "zone_numbers", None):
                if iss not in not_drawn:
                    not_drawn.append(iss)
                continue

            az = normalize_zone_names(rec.zone_numbers, schema)
            tks = SH._unpack(row.get("号码压缩"))
            if not tks:
                # 停发期（0 注）也要结算成 0，否则它永远挂在「待结算」里，
                # 而分母里又不能少它 —— 直接写一行 0 结果。
                no_tickets += 1

            hits, gross, failed, unknown = 0, 0.0, False, 0
            fixed_tbl = _fixed_amount(iss)
            for t in tks:
                try:
                    grade, _pay = _grade(lottery, t, az, schema, iss)
                except Exception as e:                        # noqa: BLE001
                    failed = True
                    logger.warning(f"影子判级失败 {lottery} {arm} 期{iss}: {e}")
                    continue
                if not grade:
                    continue
                hits += 1
                # 奖级名两套写法（记录写「六等奖」、规则表键是「六等」）⇒ 去尾「奖」再查。
                key = str(grade).strip()
                if len(key) > 1 and key.endswith("奖"):
                    key = key[:-1]
                amt = fixed_tbl.get(key)
                if amt is None:
                    unknown += 1      # 浮动奖 / 表里没有该级 ⇒ 金额未知，不参与求和
                    continue
                gross += float(amt)

            try:
                SH.settle_row(lottery, iss, arm, base=base, result={
                    "中奖注数": hits,
                    "税前奖金": round(gross, 2),
                    "结算失败": failed,
                    "金额未知注数": unknown,
                    "开奖号码": az,
                    "口径": GRADE_NOTE,
                })
                settled_rows += 1
                settled_issues.add(iss)
            except Exception as e:                            # noqa: BLE001
                logger.error(f"影子结算写入失败 {lottery} {arm} 期{iss}: {e}")

    return {
        "彩种": lottery,
        "结算行数": settled_rows,
        "结算期数": len(settled_issues),
        "仍未开奖": not_drawn,
        "无号码行": no_tickets,
    }


def settle_all(*, base: Optional[Any] = None) -> Dict[str, Any]:
    """六彩种一轮结算（调度器调用）。单彩种失败不影响其余。"""
    from config import LOTTERY_CONFIG

    out: Dict[str, Any] = {}
    for lot in LOTTERY_CONFIG:
        try:
            out[lot] = settle(lot, base=base)
        except Exception as e:                                # noqa: BLE001
            logger.warning(f"影子结算失败 {lot}: {e}")
            out[lot] = {"错误": str(e)}
    return out
