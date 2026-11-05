# -*- coding: utf-8 -*-
"""ev/draw_evidence.py —— 方案 B「外部开奖证据」的只读证据层（append-only）。

方案 B 想解决什么
-----------------
方案 A 的开奖时刻是**声明值**（`config/draw_schedule.json` 里写死的 21:15 / 21:25 / 21:30），
一旦官方调整时刻而未同步，先后关系会**系统性**判错。方案 B 用独立采集任务
把「外部源上该期结果首次可被观测到的时刻」记下来，作为实测旁证。

★ 必须说清的边界（这里极易搞反，搞反就会误判前视）
--------------------------------------------------
官方渠道**不公布秒级摇奖时刻**。两类时间的方向完全不同：

    声明值（直播开始 21:15 …）  ≤  实际摇奖  ≤  首次可观测时刻

  · **声明值是下界**：直播开始后才摇奖 ⇒ `实际开奖 ≥ 声明值`。
    要判「批次时间早于开奖」，必须用**下界**：`批次时间 < 声明值 ⇒ 批次时间 < 实际开奖` ✓
  · **首次可观测时刻是上界**：`实际开奖 ≤ 首次可观测`。
    它**不能**用来判「早于」—— 批次时间早于它，仍可能晚于实际开奖 ✗

所以方案 B 的正确用法是**交叉校验下界**，不是取代它：

  · 判据仍用声明值（下界）；
  · 若某证据的 `首次可观测时刻 < 声明值` ⇒ 实际开奖 ≤ 它 < 声明值 ⇒
    **声明值偏高、不再是下界** ⇒ 该期判不合格并告警（保守，不静默采用）；
  · 否则证据只作旁证，用来确认「声明值与实测不矛盾」。

纪律
----
  · 只追加，不回改：同 (版本, 彩种, 期号, 来源) 已有记录就拒写（幂等）；
  · 不读系统时间：`抓取时间` / `首次可观测时刻` 全由采集器注入；
  · 不 fail-open：坏行计数并披露，缺字段的记录一律判不合格；
  · 不写开奖读数进登记行：证据文件与影子台账物理隔离，登记行仍只含时间与哈希。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EVIDENCE_VERSION = "2026-11-06-A"

BASE_DIR = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = BASE_DIR / "logs" / "draw_evidence.jsonl"

REQUIRED = ("版本", "彩种", "期号", "开奖日期", "来源", "抓取时间",
            "首次可观测时刻", "开奖号码")

BOUND_NOTE = (
    "声明开奖时刻（直播开始）是开奖事件的**下界**：直播开始后才摇奖。"
    "「首次可观测时刻」是**上界**（实际摇奖不晚于它），只用于交叉校验，不能单独当判据。"
    "两者都不能证明任何策略优势。"
)
DECLARED_LATE_NOTE = (
    "证据显示该期结果在「声明开奖时刻」之前就已被观测到 ⇒ 实际开奖早于声明值，"
    "声明值不再构成下界（`config/draw_schedule.json` 该彩种时刻可能偏晚）。"
    "本期按保守处理：不具备「开奖前冻结」的判定资格，请核对官方公告后更新时刻表。"
)


class DrawEvidenceError(ValueError):
    """证据记录不合格。一律显式抛，绝不静默丢弃或补默认值。"""


def _dt(value: Any, field: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError) as exc:
        raise DrawEvidenceError(f"{field} 不可解析：{value!r}") from exc


def _issues_of(rec: Dict[str, Any]) -> List[str]:
    """校验单条记录，返回问题列表（空=合格）。"""
    out: List[str] = []
    for k in REQUIRED:
        if rec.get(k) in (None, "", []):
            out.append(f"缺字段 {k}")
    if rec.get("版本") not in (None, EVIDENCE_VERSION):
        out.append(f"版本不符：{rec.get('版本')!r} != {EVIDENCE_VERSION!r}")
    for field in ("抓取时间", "首次可观测时刻"):
        if rec.get(field) in (None, ""):
            continue
        try:
            _dt(rec.get(field), field)
        except DrawEvidenceError as exc:
            out.append(str(exc))
    # 时区必须统一：混用会让后续比较抛 TypeError 而不是判不合格
    kinds = set()
    for field in ("抓取时间", "首次可观测时刻"):
        if rec.get(field) in (None, ""):
            continue
        try:
            kinds.add(_dt(rec.get(field), field).tzinfo is not None)
        except DrawEvidenceError:
            pass
    if len(kinds) > 1:
        out.append("抓取时间与首次可观测时刻的时区口径混用（须同为 naive 或同为 aware）")
    nums = rec.get("开奖号码")
    if nums is not None and not isinstance(nums, (dict, list)):
        out.append("开奖号码必须是对象或列表")
    return out


def validate_record(rec: Any) -> Dict[str, Any]:
    """校验单条记录；不合格 ⇒ 抛（不返回「凑合用」的版本）。"""
    if not isinstance(rec, dict):
        raise DrawEvidenceError(f"证据记录必须是对象，实际 {type(rec).__name__}")
    issues = _issues_of(rec)
    if issues:
        raise DrawEvidenceError("证据记录不合格：" + "；".join(issues))
    return rec


def evidence_key(rec: Dict[str, Any]) -> tuple:
    """幂等键：(版本, 彩种, 期号, 来源)。同键重复写一律拒绝（不回改）。"""
    return (str(rec.get("版本") or ""), str(rec.get("彩种") or ""),
            str(rec.get("期号") or ""), str(rec.get("来源") or ""))


def append_evidence(rec: Dict[str, Any], *, path: Optional[Path] = None) -> bool:
    """追加一条证据。返回 True=写入，False=同键已存在（幂等跳过）。"""
    validate_record(rec)
    p = Path(path) if path is not None else EVIDENCE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    key = evidence_key(rec)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(old, dict) and evidence_key(old) == key:
                return False
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def load_evidence(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """读全部证据；坏行计数并披露（不静默丢弃）。"""
    p = Path(path) if path is not None else EVIDENCE_PATH
    out: Dict[str, Any] = {"路径": str(p), "存在": p.exists(),
                           "行": [], "行数": 0, "坏行数": 0}
    if not p.exists():
        return out
    rows: List[Dict[str, Any]] = []
    bad = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(rec, dict) or _issues_of(rec):
            bad += 1
            continue
        rows.append(rec)
    out.update({"行": rows, "行数": len(rows), "坏行数": bad})
    return out


def for_issue(lottery: str, issue: Any, *, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """取某彩种某期的全部合格证据（可能来自多个源）。"""
    doc = load_evidence(path=path)
    lot, iss = str(lottery), str(issue)
    return [r for r in doc["行"]
            if str(r.get("彩种")) == lot and str(r.get("期号")) == iss]


def derive_draw_lower_bound(declared_time: Any, records: Sequence[Dict[str, Any]], *,
                            declared_source: str = "config/draw_schedule.json"
                            ) -> Dict[str, Any]:
    """求开奖事件的**下界**（判「批次时间早于开奖」只能用下界），并用证据交叉校验。

    下界 = 声明时刻（直播开始；实际摇奖在其后）。
    证据的 `首次可观测时刻` 是**上界**，只用来否定下界，不能单独当判据：

      · 某证据的上界 < 声明值 ⇒ 实际开奖 ≤ 上界 < 声明值 ⇒ **声明值偏高、不再构成下界**
        ⇒ `可用=False` 并给原因（本期按保守处理：不具登记资格）；
      · 否则 `可用=True`，下界 = 声明值。

    返回 `{下界, 可用, 矛盾, 明细, 披露, 原因}`。无下界时 `可用=False`（不 fail-open）。
    """
    detail: List[Dict[str, Any]] = []
    declared_dt: Optional[datetime] = None
    if declared_time not in (None, ""):
        try:
            declared_dt = _dt(declared_time, "声明开奖时刻")
            detail.append({"时间": str(declared_time), "类别": "声明值（下界）",
                           "来源": declared_source})
        except DrawEvidenceError as exc:
            detail.append({"时间": str(declared_time), "类别": "声明值（下界）",
                           "来源": declared_source, "错误": str(exc)})
    else:
        detail.append({"时间": None, "类别": "声明值（下界）",
                       "来源": declared_source, "错误": "缺失"})

    measured: List[datetime] = []
    for rec in records:
        raw = rec.get("首次可观测时刻")
        try:
            dt = _dt(raw, "首次可观测时刻")
        except DrawEvidenceError as exc:
            detail.append({"时间": raw, "类别": "实测首次可观测（上界）",
                           "来源": rec.get("来源"), "错误": str(exc)})
            continue
        measured.append(dt)
        detail.append({"时间": str(raw), "类别": "实测首次可观测（上界）",
                       "来源": rec.get("来源")})

    if declared_dt is None:
        return {"下界": None, "可用": False, "矛盾": False, "明细": detail,
                "披露": [BOUND_NOTE],
                "原因": ["声明开奖时刻缺失或不可解析 ⇒ 没有下界可用，不 fail-open。"]}

    if measured:
        kinds = {declared_dt.tzinfo is not None} | {m.tzinfo is not None for m in measured}
        if len(kinds) > 1:
            return {"下界": None, "可用": False, "矛盾": False, "明细": detail,
                    "披露": [BOUND_NOTE],
                    "原因": ["声明值与证据的时区口径混用（须同为 naive 或同为 aware）"
                              "⇒ 拒绝给出下界。"]}
        earliest = min(measured)
        if earliest < declared_dt:
            return {"下界": None, "可用": False, "矛盾": True, "明细": detail,
                    "披露": [BOUND_NOTE, DECLARED_LATE_NOTE],
                    "原因": [f"证据的首次可观测时刻 {earliest.isoformat()} 早于声明值 "
                             f"{declared_dt.isoformat()} ⇒ 声明值不再是下界。"]}

    return {"下界": str(declared_time), "可用": True, "矛盾": False, "明细": detail,
            "披露": [BOUND_NOTE], "原因": []}
