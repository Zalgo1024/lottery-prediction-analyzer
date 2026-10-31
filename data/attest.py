# -*- coding: utf-8 -*-
"""留证台账 —— 哈希链的 append-only 事件流水。

它要回答的问题
--------------
不是「结果对不对」，而是「**当时到底是什么状态**」。因为有两类事故靠普通日志
查不出来：

  · 配置文件被删 → 系统静默回落到默认值，出号照跑、日志全绿，与「一切正常」
    在外部完全无法区分（2026-10-31 真实发生，生效注数从 50 变成 100/100/5/5/5/100）。
  · 历史记录被就地改写 → 事后看到的是「改后」的数字，改前的形状无处可寻。

所以每行都带上**配置文件的哈希**与**上一条的哈希**：

    本行哈希 = sha256(上一行哈希 + 本行内容)

于是「删除/改写中间某一条」会立刻让后续所有行对不上，`verify()` 能指出断在第几行。
这不是密码学意义上的不可篡改（本地文件总能被整体替换），而是**便宜的完整性证据**：
任何一次非追加的改动都会留下痕迹。

★ 本模块**不提供任何修改接口**。要改只能重新 append 一条新事件，
  这正是我们要的语义：历史是追加出来的，不是编辑出来的。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import BASE_DIR

logger = logging.getLogger(__name__)

ATTEST_DIR = BASE_DIR / "training" / "attest"
_GENESIS = "0" * 64


def attest_path(lottery: str, *, base: Optional[Any] = None) -> Path:
    d = base if base is not None else ATTEST_DIR
    return d / f"{lottery}_attest.jsonl"


def _line_hash(prev: str, body: Dict[str, Any]) -> str:
    blob = prev + json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _tail(path: Path):
    """返回 (序号, 最后一行哈希)。文件不存在或损坏都当作创世。"""
    if not path.exists():
        return 0, _GENESIS
    last = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if last is None:
        return 0, _GENESIS
    try:
        doc = json.loads(last)
        return int(doc.get("序号") or 0), str(doc.get("本行哈希") or _GENESIS)
    except Exception:                                         # noqa: BLE001
        logger.warning(f"留证台账尾行损坏：{path}（以创世重新接续）")
        return 0, _GENESIS


def append(lottery: str, event: Dict[str, Any], *, base: Optional[Any] = None) -> Dict[str, Any]:
    """追加一条留证事件，返回写入的行（含哈希）。

    ★ 只追加。没有 update / delete —— 想改就再 append 一条，让改动本身成为事件。
    """
    from data.version import code_sha

    p = attest_path(lottery, base=base)
    p.parent.mkdir(parents=True, exist_ok=True)

    seq, prev = _tail(p)
    body = dict(event or {})
    body["序号"] = seq + 1
    body["彩种"] = lottery
    body["代码版本"] = body.get("代码版本") or code_sha()
    body["上一行哈希"] = prev
    body["本行哈希"] = _line_hash(prev, {k: v for k, v in body.items() if k != "本行哈希"})

    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(body, ensure_ascii=False, sort_keys=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return body


def verify(lottery: str, *, base: Optional[Any] = None) -> Dict[str, Any]:
    """校验链完整性。断链时给出行号与原因，**不静默返回 ok**。"""
    p = attest_path(lottery, base=base)
    if not p.exists():
        return {"彩种": lottery, "行数": 0, "完整": True, "断点": None,
                "说明": "尚无留证事件"}

    prev = _GENESIS
    n = 0
    with p.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as e:
                return {"彩种": lottery, "行数": n, "完整": False, "断点": i,
                        "原因": f"第 {i} 行不是合法 JSON：{e}"}
            if str(doc.get("上一行哈希")) != prev:
                return {"彩种": lottery, "行数": n, "完整": False, "断点": i,
                        "原因": f"第 {i} 行的上一行哈希与实际前驱不一致（中间被删/被改）"}
            body = {k: v for k, v in doc.items() if k != "本行哈希"}
            expect = _line_hash(prev, body)
            if str(doc.get("本行哈希")) != expect:
                return {"彩种": lottery, "行数": n, "完整": False, "断点": i,
                        "原因": f"第 {i} 行内容与自身哈希不符（该行被改写）"}
            prev = expect
    return {"彩种": lottery, "行数": n, "完整": True, "断点": None, "说明": "链条完整"}


def load(lottery: str, *, base: Optional[Any] = None) -> List[Dict[str, Any]]:
    p = attest_path(lottery, base=base)
    if not p.exists():
        return []
    out = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def snapshot_event(lottery: str, *, issue: Any = None, batch: Any = None,
                   extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一条「出号时刻状态」事件 —— 含**配置文件哈希**。

    ★ `配置哈希` 是这次事故的直接解药：文件被删/被改时哈希就变，
      而「哈希变成了缺失(None)」本身就是一个可检索的事件，不需要靠人发现注数不对。
    """
    from data import ticket_size as TS
    from data.version import code_sha, file_hash, prediction_version

    ev: Dict[str, Any] = {
        "事件": "出号快照",
        "期号": issue,
        "批次号": batch,
        "预测版本": prediction_version(),
        "代码版本": code_sha(),
        "出号时刻": None,
        "数据抓取时间": None,
        "开奖结果来源": None,
        "开奖结果抓取时间": None,
        "配置哈希": file_hash(TS.STORE_PATH),
        "配置存在": bool(TS.STORE_PATH.exists()),
        "配置健康度": TS.health().get("安全"),
    }
    ev.update(extra or {})
    return ev
