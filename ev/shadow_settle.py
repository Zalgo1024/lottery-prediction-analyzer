# -*- coding: utf-8 -*-
"""ev/shadow_settle.py —— 影子台账的**结算编排**（登记行 + 冻结快照 → 结算行）。

它补上影子验证链路的最后一环：`ev/prefix_shadow.settle_prefix()` 只负责
「把结算行追加进台账并校验不许换票 / 改期 / 前视」，它需要调用方先把
「前 n 注到底中了什么、实得多少」算出来。本模块就做这件事：

    登记行（含 期号 / 冻结版本 / 策略哈希 / n）
      → 定位冻结快照（文件名 = 期号_版本_策略哈希前12.json）
      → `prefix_of()` 取**纯切片**的前 n 注（绝不重跑排序）
      → `coverage_backtest.evaluate_period()` 逐注判级 + 取实得
      → `settle_prefix()` 追加结算行

纪律
----
  · 不读系统时间：`settle_time` 由调用方注入（与 `order_freeze` 同一不变量）；
  · 不换票：快照的 `票序哈希` 必须与登记行一致，否则拒绝；
  · 不判优势：这里只算「前 n 注的实得」，不做任何显著性 / BH / 判决；
  · 失败不抛：返回 `{"状态": "失败", "原因": ...}`，由调用方决定是否告警。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from config import ticket_price
from ev import order_freeze as OF
from ev import prefix_shadow as PS

logger = logging.getLogger("ev.shadow_settle")

SETTLE_VERSION = "2026-11-06-A"

STATUS_OK = "已结算"
STATUS_SKIP = "跳过"
STATUS_FAIL = "失败"


def snapshot_path(register_row: Dict[str, Any], *, snapshot_dir: Optional[Path] = None) -> Path:
    """按登记行还原快照文件名（`期号_版本_策略哈希前12.json`，与 `order_freeze` 同规则）。"""
    issue = re.sub(r"[^0-9A-Za-z_-]", "_", str(register_row.get("期号")))
    ver = str(register_row.get("冻结版本"))
    h = str(register_row.get("策略哈希"))[:12]
    return Path(snapshot_dir or OF.SNAPSHOT_DIR) / f"{issue}_{ver}_{h}.json"


def _draw_numbers(actual_rec: Any) -> Any:
    """从开奖记录里取号码字段；取不到就如实写「未展开」，不编造。"""
    if isinstance(actual_rec, dict):
        for k in ("号码", "开奖号码", "zones"):
            if k in actual_rec:
                return actual_rec[k]
        return {k: v for k, v in actual_rec.items()
                if k not in ("期号", "开奖日期")} or {"说明": "开奖记录无号码字段"}
    for attr in ("号码", "开奖号码", "zones"):
        if hasattr(actual_rec, attr):
            return getattr(actual_rec, attr)
    return {"说明": "开奖记录未暴露号码字段（判级由 evaluate_period 内部完成）"}


def _find_record(lottery: str, issue: Any,
                 records: Optional[Sequence[Any]]) -> Any:
    recs = records
    if recs is None:
        from data.loader import load_lottery
        recs = load_lottery(lottery).records or []
    want = str(issue)
    for r in recs:
        got = r.get("期号") if isinstance(r, dict) else getattr(r, "期号", None)
        if str(got) == want:
            return r
    return None


def settle_from_register(register_row: Dict[str, Any], *, settle_time: Any,
                         snapshot_dir: Optional[Path] = None,
                         shadow_path: Optional[Path] = None,
                         records: Optional[Sequence[Any]] = None,
                         rule_issue: Any = None) -> Dict[str, Any]:
    """按一条**登记行**结算。返回 `{状态, 原因, 结算行, 结果}`，永不抛出。

    `settle_time` 必须由调用方给（本模块不读系统时间）。
    `rule_issue` 透传给 `evaluate_period`：给定时按该期号规则版本判级（反事实口径）。
    """
    base = {"编排版本": SETTLE_VERSION,
            "彩种": register_row.get("彩种"), "期号": register_row.get("期号"),
            "n": register_row.get("n")}
    try:
        if register_row.get("阶段") != PS.KIND_REGISTER:
            return dict(base, 状态=STATUS_FAIL, 原因="传入的不是登记行（阶段必须=登记）")
        lottery = str(register_row.get("彩种") or "")
        issue = register_row.get("期号")
        n = int(register_row.get("n") or 0)
        if not lottery or not issue or n < 1:
            return dict(base, 状态=STATUS_FAIL, 原因="登记行缺 彩种 / 期号 / n")

        path = snapshot_path(register_row, snapshot_dir=snapshot_dir)
        if not path.exists():
            return dict(base, 状态=STATUS_FAIL,
                        原因=f"冻结快照不存在：{path}（无法复核票序，不换票也不猜票）")
        snap = OF.load_snapshot(path)
        if str(snap["票序哈希"]) != str(register_row.get("票序哈希")):
            return dict(base, 状态=STATUS_FAIL,
                        原因="快照票序哈希与登记行不一致 ⇒ 拒绝结算（不许换票）")

        prefix = OF.prefix_of(snap, n)          # 纯切片 + 哈希双锁
        actual = _find_record(lottery, issue, records)
        if actual is None:
            return dict(base, 状态=STATUS_SKIP,
                        原因=f"开奖记录里还没有期号 {issue}（未开奖或数据未入库），"
                              "本次不结算；不写成通过。")

        from ev.coverage_backtest import evaluate_period
        ev = evaluate_period(lottery, prefix, actual, issue, {},
                             rule_issue=rule_issue)
        price = float(ticket_price(lottery))
        result = {
            "开奖号码": _draw_numbers(actual),
            "成本": round(price * n, 2),
            "实得": ev.get("总实得"),
            "达标注数": ev.get("命中注数"),
            "金额未知注数": 0,
            "口径": ("前 n 注反事实口径；判级与金额由 evaluate_period 按当期规则计算，"
                     "金额未知不进求和并单独计数"),
        }
        row = PS.settle_prefix(register_row, result=result,
                               draw_time=register_row.get("开奖时间"),
                               settle_time=settle_time, path=shadow_path)
        return dict(base, 状态=STATUS_OK, 原因="", 结算行=row, 结果=result,
                    结算口径=ev.get("结算口径"))
    except PS.PrefixShadowError as exc:
        if "已有结算" in str(exc):
            return dict(base, 状态=STATUS_SKIP, 原因="同键已有结算，拒绝回改（幂等跳过）")
        logger.warning(f"影子结算被拒：{exc}")
        return dict(base, 状态=STATUS_FAIL, 原因=f"PrefixShadowError: {exc}")
    except Exception as exc:                                   # noqa: BLE001
        logger.warning(f"影子结算失败：{type(exc).__name__}: {exc}")
        return dict(base, 状态=STATUS_FAIL, 原因=f"{type(exc).__name__}: {exc}")
