# -*- coding: utf-8 -*-
"""审计阈值 —— 预先定义、锁定、可校验。

为什么阈值必须「先定后锁」
--------------------------
开奖之后再调阈值，等于给自己发一张「总能解释过去」的许可证：结果不合预期就把
判据放宽，于是永远不会有异常，也永远不会学到东西。这与多重比较里「看到 p 值
再选显著性水平」是同一个错误。

所以：
  · 阈值写入 `config/audit_thresholds.json`，带 `锁定: true`；
  · 锁定时 `save()` **抛错**（要改必须显式 `unlock=True`，且改动本身留痕）；
  · 每次检查记录阈值内容哈希 ⇒ 「阈值什么时候变的」可查。

★ 阈值本身**不影响出号**，它只决定「要不要把某条记为待查」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

THRESHOLD_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "audit_thresholds.json"

DEFAULTS: Dict[str, Any] = {
    "版本": "audit-v1",
    "设定日期": "2026-10-31",
    "锁定": True,
    "说明": "预设的异常判据。改阈值必须在开奖前，且改动本身应被记录。",
    "阈值": {
        # 开奖结果写入时刻 − 开奖日，超过这个天数记为「延迟」
        "延迟天数上限": 3,
        # 「批次时间晚于开奖日」= 自证马后炮，任何一条都记为待查（不设容差）
        "允许前视条数": 0,
        # 一期之内「金额未知」注数占比超过该值记为待查
        "金额未知占比上限": 0.5,
        # 相邻期之间「缺失期号」连续段长度超过该值记为待查
        "缺失期段上限": 1,
        # 配置健康度不安全（文件缺失/未锁/模式被兜底）时是否记为待查
        "配置不安全记待查": True,
    },
}


class ThresholdLocked(RuntimeError):
    """阈值已锁定却尝试写入。"""


def load(*, path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path is not None else THRESHOLD_PATH
    if not p.exists():
        return dict(DEFAULTS)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("根节点不是对象")
        merged = dict(DEFAULTS)
        merged.update(doc)
        merged["阈值"] = {**DEFAULTS["阈值"], **(doc.get("阈值") or {})}
        return merged
    except Exception as e:                                    # noqa: BLE001
        logger.error(f"审计阈值文件损坏，回退默认值（注意：默认值 ≠ 上次生效值）：{e}")
        return dict(DEFAULTS)


def content_hash(doc: Optional[Dict[str, Any]] = None, *, path: Optional[Path] = None) -> str:
    d = doc if doc is not None else load(path=path)
    blob = json.dumps(d.get("阈值") or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def save(doc: Dict[str, Any], *, path: Optional[Path] = None, unlock: bool = False) -> Dict[str, Any]:
    """写阈值。**锁定时抛错** —— 开奖后改阈值是自欺。"""
    p = Path(path) if path is not None else THRESHOLD_PATH
    cur = load(path=p)
    if cur.get("锁定") and not unlock:
        raise ThresholdLocked(
            "审计阈值已锁定，拒绝写入。若确需修改，必须在开奖前显式 unlock=True。"
        )
    out = dict(doc)
    out.setdefault("版本", cur.get("版本"))
    out.setdefault("设定日期", time.strftime("%Y-%m-%d"))
    out.setdefault("锁定", True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
