# -*- coding: utf-8 -*-
"""ev/preregistration.py —— 实验预注册的**只读**装载与校验。

为什么要有这个模块
------------------
方案 §6：实验的所有自由度（期窗、K、主终点、效应量口径、判据、停止规则）
必须在**看到结果之前**固定下来，否则「未检出差异」会在事后被读成「策略无效」。
预注册文件进 Git（`config/preregistration.json`），改它要留变更日志。

★ 本模块的设计取向是「**一律抛异常，绝不回退默认值**」。
  对比 `data/ticket_size.py::sanitize` —— 那个函数遇到坏值会静默回落默认值，
  这在**运行时配置**里是对的（服务不能崩），但在**实验契约**里是灾难：
  一个静默改掉的 K 或期窗会让整份对照表失效且无人察觉。

三层锁
------
1. **哈希自校验**：文件内容（去掉 `哈希` 字段后规范化）的 sha256；
   改任何字段都必须同步改 `哈希`，否则装载直接失败；
2. **测试硬编码常量**：`tests/test_preregistration.py` 把主格的关键常量
   写成字面量，改配置会红；
3. **报告回显**：`ev/arena/report.py` 把装载到的内容原样打进报告首部。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_BASE_DIR = Path(__file__).resolve().parent.parent
STORE_PATH = _BASE_DIR / "config" / "preregistration.json"

_HASH_FIELD = "哈希"
_SELF_EXEMPT = {_HASH_FIELD}


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def compute_hash(doc: Dict[str, Any]) -> str:
    """去掉 `哈希` 字段后的规范化 sha256。"""
    body = {k: v for k, v in doc.items() if k not in _SELF_EXEMPT}
    return hashlib.sha256(_canonical(body)).hexdigest()


class PreregistrationError(RuntimeError):
    """预注册文件缺失 / 结构不符 / 哈希不匹配。**一律抛，不回退。**"""


def load(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """装载并校验预注册。任何不符都抛 `PreregistrationError`。"""
    p = Path(path) if path is not None else STORE_PATH
    if not p.exists():
        raise PreregistrationError(
            f"预注册文件不存在：{p}。★ 实验契约不允许回退默认值 —— "
            f"请先写出 config/preregistration.json 并同步它的 {_HASH_FIELD}。")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        raise PreregistrationError(f"预注册文件解析失败：{p}：{e}") from e
    if not isinstance(doc, dict):
        raise PreregistrationError(f"预注册文件顶层必须是对象：{p}")
    for key in ("版本", "锁定日", _HASH_FIELD, "常量", "指标", "基线臂", "格", "停止规则"):
        if key not in doc:
            raise PreregistrationError(f"预注册缺少必需字段 {key!r}（文件 {p}）")
    want = str(doc[_HASH_FIELD])
    got = compute_hash(doc)
    if want != got:
        raise PreregistrationError(
            f"预注册哈希不匹配：文件记 {want[:12]}…，实算 {got[:12]}…。"
            f"★ 改了内容就必须同步改 {_HASH_FIELD}（并在 `变更日志` 里写明理由），"
            f"否则无法区分「有意的修订」与「被误改」。")
    return doc


def constants(doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return dict((doc or load())["常量"])


def cells(doc: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Stage 1 的格（选哪副票集那个自由度）。"""
    return list((doc or load())["格"])


def stage2_cells(doc: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Stage 2 的倍投规格（同一副票集怎么摊钱那个自由度）。

    ★ Stage 2 不是「多个格」而是「一个规格」，因为它的全部方案共用同一副票集、
    同一个预算 —— 可比性来自「同票集同预算」，而不是来自格子之间的独立性。

    “未登记” 必须显式抛：Stage 2 的 runner（`ev/arena/stage2.py`）在没有登记的
    情况下也能跑出数字，所以这里的缺失不会被自动发现 —— 缺了就必须炸。
    """
    d = doc or load()
    if "格_Stage2" not in d:
        raise PreregistrationError(
            "预注册没有 `格_Stage2`（Stage 2 倍投尚未登记）。"
            "★ Stage 2 的结果必须在登记之后产生：先看结果再补登记，"
            "等于把「报价单」写在账单之后。请先运行 scripts/oneoff/_register_stage2.py。")
    return [d["格_Stage2"]]


def stage2(doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return stage2_cells(doc)[0]


def primary_cell(doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """主格：唯一一个 `"主假设": true` 的格；没有或有多个都抛异常。"""
    hits = [c for c in cells(doc) if bool(c.get("主假设"))]
    if len(hits) != 1:
        raise PreregistrationError(
            f"主假设必须**恰好**有 1 个格，实际 {len(hits)} 个。"
            f"★ 多个主假设 = 事后挑显著结果的入口。")
    return hits[0]


def assert_run_matches(run_meta: Dict[str, Any], cell: Dict[str, Any]) -> None:
    """把一次实跑的常量与预注册逐项对齐，不符即抛。

    · 以 `_` 开头的键是**注释**（例 `_规则口径说明`），不参与比对；
    · 空列表 / None / 缺字段 视为等价（「不剔除任何规则版本」的三种写法）。
    """
    bad: List[str] = []
    for k, want in (cell.get("常量") or {}).items():
        if k.startswith("_"):
            continue
        got = run_meta.get(k)
        empty_want = want is None or (isinstance(want, (list, tuple, dict)) and len(want) == 0)
        empty_got = got is None or (isinstance(got, (list, tuple, dict)) and len(got) == 0)
        if empty_want and empty_got:
            continue
        if got is None:
            bad.append(f"缺字段 {k}")
        elif str(got) != str(want):
            bad.append(f"{k}: 预注册 {want} != 实跑 {got}")
    if bad:
        raise PreregistrationError("实跑常量与预注册不符：" + "；".join(bad))
