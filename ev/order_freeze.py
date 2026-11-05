# -*- coding: utf-8 -*-
"""ev/order_freeze.py —— 前缀口径的「排序冻结」记录（离线研究，不接生产）。

为什么需要
----------
「实际买前 n 注」这个口径**完全依赖票序**。若顺序是生成完再按一个未校准的
置信度随手排的，前 3 注就等于随机抽 3 注，任何回放都对不上真实行为。
因此每期必须在开奖结算前把**完整排序**连同版本 / 策略哈希 / 随机种子 /
批次时间一起冻结，结算时**复制票序**、不重跑排序函数（重跑会因代码版本
与种子漂移，前缀结果会产生严重选择偏差）。

三条硬纪律
----------
1. `prefix_of()` 是**纯切片**，不调用任何选号 / 排序函数。改票序不可能
   通过哈希校验（票序哈希 + 策略哈希双锁）。
2. 同一「期号 + 版本 + 策略哈希」已有文件且内容不同 ⇒ **拒绝覆盖**。
   同内容重跑是幂等的，不会留下两份互相矛盾的证据。
3. 快照里的 `批次时间` 必须由调用方传入；模块内**不读系统时间**。
   离线回放的时间点带 `时间点性质` 显式标注，不许被当成生产出号证据。

★ 本模块是离线研究通道：生产出号 / 结算 / 看板不 import 它，
  不改下注量、不改 `ticket_size.json`、不改预算闸门。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ORDER_FREEZE_VERSION = "2026-11-05-A"
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "logs" / "order_freeze"

_REQUIRED = (
    "版本", "彩种", "期号", "批次时间", "时间点性质", "策略版本", "策略哈希",
    "随机种子", "K", "规则版本", "代码版本", "票序", "票序哈希", "注数",
)


class OrderFreezeError(RuntimeError):
    """票序冻结 / 装载失败：一律抛，不回退、不重排、不静默补默认值。"""


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(obj: Any) -> str:
    """规范化 JSON 的 sha256（跨进程、跨字典顺序稳定）。"""
    return hashlib.sha256(_canonical(obj)).hexdigest()


def strategy_hash(*, lottery: str, issue: Any, strategy_version: str,
                  seed: int, K: int, rule_version: Any,
                  code_version: str, target: Any = None,
                  experiment: Any = None) -> str:
    """所有会影响排序的输入一起进哈希；换任一项都必须换文件名。

    ★ `experiment`（块编号 / 池种子 / 池规模 / 重叠上限等）只在非 None 时入哈希：
      早期快照没有这个字段，默认路径的哈希必须与历史**完全一致**，
      否则旧证据会被判校验失败。新回放必须显式传，避免不同块撞同一个键。
    """
    payload: Dict[str, Any] = {
        "彩种": str(lottery), "期号": str(issue),
        "策略版本": str(strategy_version), "随机种子": int(seed),
        "K": int(K), "规则版本": str(rule_version),
        "代码版本": str(code_version), "目标": target,
    }
    if experiment is not None:
        payload["实验参数"] = experiment
    return digest(payload)


def build_snapshot(*, lottery: str, issue: Any,
                   tickets: Sequence[Dict[str, Any]],
                   strategy_version: str, seed: int, K: int,
                   rule_version: Any, code_version: str,
                   batch_time: str, target: Any = None,
                   time_nature: str = "回放模拟（非生产批次时间）",
                   extra: Optional[Dict[str, Any]] = None,
                   experiment: Any = None) -> Dict[str, Any]:
    """冻结一份完整票序。★ 不读系统时间，`batch_time` 由调用方给。"""
    tks = copy.deepcopy(list(tickets))
    K = int(K)
    if len(tks) != K:
        raise OrderFreezeError(f"票序注数 {len(tks)} != K={K}（前缀口径要求恰好 K 注）")
    if not str(batch_time or "").strip():
        raise OrderFreezeError(
            "缺批次时间：只有它能自证票序在开奖前已冻结；模块不会替你填系统时间")
    if not str(time_nature or "").strip():
        raise OrderFreezeError("缺时间点性质：离线回放时间不许被当成生产出号证据")
    snap: Dict[str, Any] = {
        "版本": ORDER_FREEZE_VERSION,
        "彩种": str(lottery),
        "期号": str(issue),
        "批次时间": str(batch_time),
        "时间点性质": str(time_nature),
        "策略版本": str(strategy_version),
        "随机种子": int(seed),
        "K": K,
        "规则版本": str(rule_version),
        "代码版本": str(code_version),
        "目标": target,
        "注数": len(tks),
        "票序": tks,
    }
    snap["策略哈希"] = strategy_hash(
        lottery=lottery, issue=issue, strategy_version=strategy_version,
        seed=seed, K=K, rule_version=rule_version,
        code_version=code_version, target=target, experiment=experiment)
    snap["票序哈希"] = digest(tks)
    if experiment is not None:
        # 进哈希的字段必须同时留在快照里，否则 verify 无法复算（缺席≠通过）。
        snap["实验参数"] = copy.deepcopy(experiment)
    if extra:
        snap["备注"] = dict(extra)
    return snap


def verify_snapshot(snap: Dict[str, Any],
                    *, expected_version: Optional[str] = None) -> Dict[str, Any]:
    """校验完整性；任何不符都抛，绝不「修一修继续用」。"""
    if not isinstance(snap, dict):
        raise OrderFreezeError("快照顶层必须是对象")
    missing = [k for k in _REQUIRED if k not in snap]
    if missing:
        raise OrderFreezeError(f"快照缺字段：{missing}")
    want = ORDER_FREEZE_VERSION if expected_version is None else str(expected_version)
    if snap["版本"] != want:
        raise OrderFreezeError(
            f"冻结版本 {snap['版本']!r} != 期望 {want!r}；历史须按当时版本回放，"
            "禁止用新版重排旧票序")
    if not str(snap["批次时间"]).strip() or not str(snap["时间点性质"]).strip():
        raise OrderFreezeError("批次时间 / 时间点性质为空")
    tks = snap["票序"]
    if not isinstance(tks, list):
        raise OrderFreezeError("票序必须是列表")
    if len(tks) != int(snap["注数"]) or int(snap["注数"]) != int(snap["K"]):
        raise OrderFreezeError(
            f"注数不自洽：票序 {len(tks)} / 注数 {snap['注数']} / K {snap['K']}")
    if digest(tks) != snap["票序哈希"]:
        raise OrderFreezeError("★ 票序被改动（票序哈希不符）：排序已不再冻结")
    got = strategy_hash(
        lottery=snap["彩种"], issue=snap["期号"],
        strategy_version=snap["策略版本"], seed=snap["随机种子"],
        K=snap["K"], rule_version=snap["规则版本"],
        code_version=snap["代码版本"], target=snap.get("目标"),
        experiment=snap.get("实验参数"))   # 旧快照缺字段 ⇒ None ⇒ 哈希与历史一致
    if got != snap["策略哈希"]:
        raise OrderFreezeError("策略哈希不符：版本 / 种子 / 目标被事后改动")
    return {"ok": True, "版本": snap["版本"], "注数": len(tks),
            "策略哈希": snap["策略哈希"]}


def snapshot_name(snap: Dict[str, Any]) -> str:
    """文件名带期号 + 版本 + 策略哈希 ⇒ 不同排序不会互相覆盖。"""
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(snap["期号"]))
    return f"{safe}_{snap['版本']}_{str(snap['策略哈希'])[:12]}.json"


def save_snapshot(snap: Dict[str, Any], *, directory: Optional[Path] = None,
                  path: Optional[Path] = None) -> Path:
    """原子落盘。同键已存在且内容不同 ⇒ 拒绝覆盖（排名不许事后改）。"""
    verify_snapshot(snap)
    p = (Path(path) if path is not None
         else Path(directory or SNAPSHOT_DIR) / snapshot_name(snap))
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(snap).decode("utf-8")
    if p.exists():
        if p.read_text(encoding="utf-8") == payload:
            return p                      # 幂等重跑
        raise OrderFreezeError(
            f"同一期号 + 版本 + 策略哈希已有不同票序，拒绝覆盖：{p}")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_snapshot(path: Path, *, expected_version: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path)
    snap = json.loads(p.read_text(encoding="utf-8"))
    verify_snapshot(snap, expected_version=expected_version)
    return snap


def prefix_of(snap: Dict[str, Any], n: int,
              *, expected_version: Optional[str] = None) -> List[Dict[str, Any]]:
    """★ 取冻结票序的前 n 注：**纯切片 + 深拷贝**，绝不重跑排序。"""
    verify_snapshot(snap, expected_version=expected_version)
    n = int(n)
    if n < 1 or n > int(snap["K"]):
        raise OrderFreezeError(f"前缀 n={n} 越界（K={snap['K']}）")
    return copy.deepcopy(list(snap["票序"][:n]))
