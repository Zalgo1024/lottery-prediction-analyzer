# -*- coding: utf-8 -*-
"""ev/prefix_shadow.py —— 前缀口径的「影子验证接线层」（离线研究，不接生产）。

方案骨架第 ④ 步
----------------
`ev/prefix_curve.py::append_shadow()` 此前只有 append-only 入口、**零调用点**：
既没有人登记，也没有状态判据，更没有「什么时候允许开判」的机械回答。本模块
把这一段接成可执行的接线层，但**不生产新读数**——它回答的是：

    现有证据够不够格进入影子验证？还差什么？

★ 本轮最重要的诚实风险
------------------------
`logs/order_freeze/` 下现有冻结快照的 `时间点性质` 全部是
「回放模拟（非生产批次时间）」，`批次时间` 是占位串
「离线回放（未记录生产批次时间）」。它们是**历史回放的证据**，若直接登记进
影子台账，就等于把探索窗读数事后改名成「开奖前冻结」，凭空造出确认力。
因此登记闸门对时间点性质采用**显式白名单**，且要求批次时间可解析并严格早于
开奖时间；生产出号链路当前并不写冻结快照，所以合格证据为 0 是**预期结果**，
不是失败，更不是「已通过」。

两层时间（缺一不可）
--------------------
· `批次时间`：快照里冻结票序的时刻，必须 < 开奖时间；
· `登记时间`：写进台账的时刻，由调用方传入，必须 <= 开奖时间。
只看前者挡不住「开奖后才把旧快照补登记」；只看后者挡不住「拿回放冒充」。
模块内**不读系统时间**，两个时间都由调用方给，便于回放与单测。

★ 边界：离线研究通道。生产出号 / 结算 / 看板 / 推送不 import 本模块；
不改 `ticket_size.json`、不改预算闸门、不改下注量、不改任何既有读数。
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ev import order_freeze as OF
from ev import prefix_curve as PC

#: ★ 只有显式声明为「开奖前生产冻结」的快照才具备登记资格。
#:   绝不能把「回放模拟（非生产批次时间）」加进来：那会把历史回放当未来确认力。
PRODUCTION_NATURE = "生产出号（开奖前冻结）"
#: 离线回放快照的性质（与 order_freeze 默认值一致，有防漂移测试）；
#: 留作显式对照，禁止加入登记白名单。快照池份数由体检函数现数，不在注释里固定。
REPLAY_NATURE = "回放模拟（非生产批次时间）"

KIND_REGISTER = "登记"
KIND_SETTLE = "结算"
BATCH_EXPLORE = "探索批"
BATCH_EVAL = "独立评估批"
BATCH_KINDS = (BATCH_EXPLORE, BATCH_EVAL)

SHADOW_PATH = PC.SHADOW_PATH
FREEZE_DIR = OF.SNAPSHOT_DIR

COUNTERFACTUAL_NOTE = PC.COUNTERFACTUAL_NOTE
NOT_PURCHASED_NOTE = PC.NOT_PURCHASED_NOTE
TERMINOLOGY_NOTE = PC.TERMINOLOGY_NOTE
NOT_OPEN_NOTE = (
    "「未开判」= 尚未攒够预注册要求的独立评估批期数，或合格登记为 0；"
    "只表示已测维度在该样本量下还没有可判证据，不构成证明。"
    "0 期不是通过，缺席不能当成确认（缺席≠通过）。")
STOP_RULE_NOTE = (
    "停止规则：未来开奖只追加、不回改历史；探索批只作描述、不进 BH；"
    "开判须另取独立评估批，且先攒够 `n*_保守` 期；登记必须先于开奖，"
    "结算必须引用已登记的票序哈希，不许事后补登记或换票。")
NO_VERDICT_NOTE = (
    "本层不做多重校正、不做统计判决、不写「检出」：它只判断证据资格与期数缺口；"
    "任何结论都必须回到 `prefix_curve.verdict_curve()` 的预注册判据上做。")
FREEZE_GATE_NOTE = (
    "开奖前冻结是硬闸门：时间点性质须为生产出号冻结；批次时间须可解析且严格早于"
    "开奖时间；登记时间须不晚于开奖时间；票序须通过快照哈希与策略哈希双锁。"
    "回放快照一律不具登记资格，不许把历史回放冒充未来确认力。")


class PrefixShadowError(RuntimeError):
    """登记 / 结算资格不符：一律抛，不静默跳过、不降级、不补默认值。"""


def _parse_dt(value: Any) -> Optional[datetime]:
    """严格解析 ISO 时间；占位串 / 空值一律返回 None（不许当 0 时刻用）。"""
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _snapshot_reasons(snap: Dict[str, Any]) -> List[str]:
    """★ 闸门判定的**单一真源**：哈希双锁 + 时间点性质 + 批次时间可解析性。

    `gate_snapshot()`（登记时，另加开奖先后）与 `audit_freeze_dir()`（体检时，
    不代猜开奖时间）共用它，不许两处各写一份判定后悄悄漂移。
    """
    reasons: List[str] = []
    try:
        OF.verify_snapshot(snap)
    except OF.OrderFreezeError as e:
        reasons.append(f"冻结快照校验失败：{e}")
    except Exception as e:                                    # noqa: BLE001
        reasons.append(f"冻结快照校验异常：{e}")

    nature = str(snap.get("时间点性质") or "")
    if nature != PRODUCTION_NATURE:
        reasons.append(
            f"时间点性质为「{nature or '缺失'}」，不是「{PRODUCTION_NATURE}」"
            "（回放模拟不具备开奖前冻结确认力）")
    if _parse_dt(snap.get("批次时间")) is None:
        reasons.append("批次时间缺失或不可解析（回放占位串不算冻结时刻）")
    return reasons


def gate_snapshot(snap: Dict[str, Any], *, draw_time: Any) -> Dict[str, Any]:
    """判定一份冻结快照是否**具备开奖前登记资格**（纯函数，不写盘）。

    ★ 返回逐条原因，不静默过滤：分母里每一份都要能被追到「为什么不合格」。
    """
    reasons = list(_snapshot_reasons(snap))
    batch_dt = _parse_dt(snap.get("批次时间"))
    draw_dt = _parse_dt(draw_time)
    if draw_dt is None:
        reasons.append("开奖时间缺失或不可解析：无法判定先后，按不合格处理")
    if batch_dt is not None and draw_dt is not None and batch_dt >= draw_dt:
        reasons.append("批次时间不早于开奖时间，不能自证票序在开奖前已定")

    return {"合格": not reasons, "原因": reasons,
            "彩种": str(snap.get("彩种") or ""),
            "期号": str(snap.get("期号") or ""),
            "时间点性质": str(snap.get("时间点性质") or ""),
            "批次时间": str(snap.get("批次时间") or ""),
            "票序哈希": snap.get("票序哈希"),
            "策略哈希": snap.get("策略哈希")}


def audit_freeze_dir(directory: Optional[Path] = None) -> Dict[str, Any]:
    """体检现有冻结快照池：逐份判定资格，**分母 = 文件总数**。

    不做筛选后缩分母；坏 JSON / 校验失败都单独计数并写原因（缺席≠通过）。
    `draw_time` 不由本函数猜：快照池自身不含开奖时间，故只按「时间点性质 +
    批次时间可解析性」判，开奖先后留到 `register_prefix()` 用调用方给的时间判。
    """
    d = Path(directory) if directory is not None else Path(FREEZE_DIR)
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    rows: List[Dict[str, Any]] = []
    for f in files:
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:                                    # noqa: BLE001
            rows.append({"文件": f.name, "合格": False,
                         "原因": [f"JSON 解析失败：{e}"],
                         "时间点性质": "无法读取", "批次时间": "无法读取",
                         "彩种": "", "期号": ""})
            continue
        # ★ 与登记闸门共用 _snapshot_reasons()：只验性质 + 批次时间两关，
        #   开奖先后留到 register_prefix() 由调用方给的时间判，本体检不代猜。
        reasons = list(_snapshot_reasons(snap))
        nature = str(snap.get("时间点性质") or "")
        rows.append({"文件": f.name, "合格": not reasons, "原因": reasons,
                     "时间点性质": nature,
                     "批次时间": str(snap.get("批次时间") or ""),
                     "彩种": str(snap.get("彩种") or ""),
                     "期号": str(snap.get("期号") or "")})

    dist: Counter = Counter()
    for r in rows:
        for why in r["原因"]:
            if "时间点性质" in why:
                dist["时间点性质非生产冻结"] += 1
            elif "批次时间" in why:
                dist["批次时间不可解析"] += 1
            elif "校验" in why:
                dist["快照哈希校验失败"] += 1
            else:
                dist["无法读取"] += 1
    natures = Counter(r["时间点性质"] for r in rows)
    return {"目录": str(d), "文件总数": len(files),
            "合格数": sum(bool(r["合格"]) for r in rows),
            "不合格数": sum(not r["合格"] for r in rows),
            "原因分布": dict(sorted(dist.items())),
            "时间点性质分布": dict(sorted(natures.items())),
            "逐份": rows,
            "分母口径": "文件总数；坏文件 / 回放快照全部留在分母内并写原因",
            "开奖先后": "待登记时由调用方给开奖时间判定，本体检不代猜"}


def load_shadow(path: Optional[Path] = None) -> Dict[str, Any]:
    """只读台账；坏行单独计数并保留，不许静默丢掉后把行数读小。"""
    p = Path(path) if path is not None else Path(SHADOW_PATH)
    rows: List[Dict[str, Any]] = []
    bad = 0
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
                else:
                    bad += 1
            except json.JSONDecodeError:
                bad += 1
    return {"路径": str(p), "存在": p.is_file(), "行数": len(rows),
            "坏行数": bad, "行": rows}


def register_prefix(snap: Dict[str, Any], *, n: int, draw_time: Any,
                    register_time: Any, batch_kind: str = BATCH_EXPLORE,
                    path: Optional[Path] = None) -> Dict[str, Any]:
    """开奖前登记冻结票序的前 n 注。任何资格不符都抛，不写盘。

    ★ 登记行**不含任何开奖读数**：没有实得、没有开奖号码、没有奖级。
      这样才能自证「先登记、后开奖」，事后补登记会撞同键被拒。
    """
    n = int(n)
    if n not in PC.PREFIX_KS:
        raise PrefixShadowError(f"n={n} 不在预注册前缀档表 {PC.PREFIX_KS} 内")
    if batch_kind not in BATCH_KINDS:
        raise PrefixShadowError(f"未知批次性质：{batch_kind}（只允许 {BATCH_KINDS}）")

    check = gate_snapshot(snap, draw_time=draw_time)
    if not check["合格"]:
        raise PrefixShadowError("不具备开奖前冻结登记资格：" + "；".join(check["原因"]))

    reg_dt = _parse_dt(register_time)
    draw_dt = _parse_dt(draw_time)
    if reg_dt is None:
        raise PrefixShadowError("登记时间缺失或不可解析：模块不代填系统时间")
    if draw_dt is not None and reg_dt > draw_dt:
        raise PrefixShadowError("登记时间晚于开奖时间 ⇒ 事后补登记，拒绝")

    prefix = OF.prefix_of(snap, n)      # ★ 纯切片 + 哈希双锁，绝不重跑排序
    record: Dict[str, Any] = {
        "阶段": KIND_REGISTER,
        "期号": str(snap["期号"]),
        "彩种": str(snap["彩种"]),
        "冻结版本": str(snap["版本"]),
        "策略哈希": str(snap["策略哈希"]),
        "票序哈希": str(snap["票序哈希"]),
        "n": n,
        "前缀票序哈希": OF.digest(prefix),
        "注数": len(prefix),
        "批次时间": str(snap["批次时间"]),
        "时间点性质": str(snap["时间点性质"]),
        "开奖时间": str(draw_time),
        "登记时间": str(register_time),
        "批次性质": batch_kind,
        "口径": "反事实：假设每期买冻结票序的前 n 注，非实测购买记录",
    }
    if not PC.append_shadow(record, path=path):
        raise PrefixShadowError("同一期号/冻结版本/策略哈希/n/阶段已有登记，拒绝回改")
    return record


def settle_prefix(register_row: Dict[str, Any], *, result: Dict[str, Any],
                  draw_time: Any, settle_time: Any,
                  path: Optional[Path] = None) -> Dict[str, Any]:
    """开奖后**追加**结算行；不改登记行，不许事后补登记、不许换票。"""
    loaded = load_shadow(path)
    # ★ 复用 append_shadow 的去重键口径（单一真源）：登记行与结算行靠 `阶段` 共存，
    #   但两者必须落在同一个「期号/版本/策略哈希/n」上，否则就是拼错行的假结算。
    key = PC._shadow_key(dict(register_row, 阶段=KIND_REGISTER))
    registered = [r for r in loaded["行"]
                  if r.get("阶段") == KIND_REGISTER and PC._shadow_key(r) == key]
    if not registered:
        raise PrefixShadowError("台账里没有对应登记行 ⇒ 不许事后补登记")
    reg = registered[0]
    if str(reg.get("票序哈希")) != str(register_row.get("票序哈希")):
        raise PrefixShadowError("结算引用的票序哈希与登记不符 ⇒ 不许换票")

    # ★ 顺序有讲究：先确认「结算的是同一期」，再判时间先后。否则拿一个更晚的
    #   开奖时间去结算旧登记，会先撞上「前视」报错，掩盖真正的「改期」违规。
    if str(reg.get("开奖时间")) != str(draw_time):
        raise PrefixShadowError("结算开奖时间与登记时不一致 ⇒ 不许改期")

    draw_dt = _parse_dt(draw_time)
    set_dt = _parse_dt(settle_time)
    if set_dt is None:
        raise PrefixShadowError("结算时间缺失或不可解析：模块不代填系统时间")
    if draw_dt is not None and set_dt < draw_dt:
        raise PrefixShadowError("结算时间早于开奖时间 ⇒ 前视，拒绝")

    payload = dict(result or {})
    record: Dict[str, Any] = {
        "阶段": KIND_SETTLE,
        "期号": reg["期号"],
        "彩种": reg["彩种"],
        "冻结版本": reg["冻结版本"],
        "策略哈希": reg["策略哈希"],
        "票序哈希": reg["票序哈希"],
        "前缀票序哈希": reg["前缀票序哈希"],
        "n": reg["n"],
        "批次性质": reg["批次性质"],
        "开奖时间": str(draw_time),
        "结算时间": str(settle_time),
        "开奖号码": payload.get("开奖号码"),
        "成本": payload.get("成本"),
        "实得": payload.get("实得"),
        "达标注数": payload.get("达标注数"),
        "金额未知注数": int(payload.get("金额未知注数") or 0),
        "口径": payload.get("口径") or "前 n 注反事实口径；金额未知不进求和并单独计数",
    }
    if not PC.append_shadow(record, path=path):
        raise PrefixShadowError("同一期号/冻结版本/策略哈希/n/阶段已有结算，拒绝回改")
    return record


def opening_gate(verdict: Dict[str, Any]) -> Dict[str, Any]:
    """从**判决层读数**取开判门槛，单一真源，不许本模块另立裸数字阈值。

    主口径 n 下每格一个 `n*_保守`；取有限值中最大者为总门槛，向上取整；
    ∞ 表示该档空间上界为 0、无论攒多少期都不可判，单独列出并披露。
    """
    if not isinstance(verdict, dict) or not verdict.get("格"):
        return {"可用": False, "原因": ["未提供判决层读数（缺席≠通过），无法确定门槛"],
                "门槛期数": None, "有限格数": 0, "不可判格数": 0, "逐格": []}
    main_n = int(verdict.get("主口径n", PC.PRIMARY_N))
    cells = [c for c in verdict["格"] if int(c.get("n", -1)) == main_n]
    finite: List[float] = []
    infinite = 0
    detail: List[Dict[str, Any]] = []
    for c in cells:
        ns = c.get("n*_保守")
        try:
            val = float(ns)
        except (TypeError, ValueError):
            val = float("inf")
        ok = math.isfinite(val) and val > 0
        if ok:
            finite.append(val)
        else:
            infinite += 1
        detail.append({"门槛": c.get("门槛"), "臂": c.get("臂"),
                       "n*保守": None if not math.isfinite(val) else val,
                       "可判": ok})
    if not finite:
        return {"可用": False,
                "原因": ["主口径各格的 `n*_保守` 均为 ∞ 或无可用值 ⇒ 理论不可判"],
                "门槛期数": None, "有限格数": 0, "不可判格数": infinite,
                "逐格": detail, "主口径n": main_n}
    need = int(math.ceil(max(finite)))
    return {"可用": True, "原因": [], "门槛期数": need,
            "有限格数": len(finite), "不可判格数": infinite,
            "逐格": detail, "主口径n": main_n,
            "来源": "prefix_curve.verdict_curve() 的 n*_保守（解析上界与实测 Δ 取大）"}


def shadow_status(*, verdict: Optional[Dict[str, Any]] = None,
                  path: Optional[Path] = None) -> Dict[str, Any]:
    """影子验证当前状态：只判「能不能开判」，**不做 BH、不写检出**。

    ★ 0 登记 / 0 结算 ⇒ 显式「未开判」并列原因，绝不返回「通过」或 0 期判据。
    """
    loaded = load_shadow(path)
    rows = loaded["行"]
    reg_rows = [r for r in rows if r.get("阶段") == KIND_REGISTER]
    set_rows = [r for r in rows if r.get("阶段") == KIND_SETTLE]
    eval_reg = [r for r in reg_rows if r.get("批次性质") == BATCH_EVAL]
    eval_set = [r for r in set_rows if r.get("批次性质") == BATCH_EVAL]

    gate = opening_gate(verdict or {})
    reasons: List[str] = []
    if loaded["坏行数"]:
        reasons.append(f"台账有 {loaded['坏行数']} 行坏记录，未静默丢弃（缺席≠通过）")
    if not reg_rows:
        reasons.append("合格登记 0 期：生产出号链路当前不写开奖前冻结快照，"
                       "现有快照均为回放模拟，不具登记资格")
    if not set_rows:
        reasons.append("开奖后结算 0 期：影子台账尚无未来真实开奖记录")
    if not eval_set:
        reasons.append("独立评估批结算 0 期：探索批只作描述，不能充当确认批")
    if not gate["可用"]:
        reasons.extend(gate["原因"])
    elif len(eval_set) < gate["门槛期数"]:
        reasons.append(f"独立评估批 {len(eval_set)} 期 < 门槛 "
                       f"{gate['门槛期数']:,} 期，功效不足")

    counted = len(eval_set)
    need = gate["门槛期数"]
    return {
        "状态": "未开判" if reasons else "可开判（须回到预注册判据另行判决）",
        "可开判": not reasons,
        "未开判原因": reasons,
        "台账": {"路径": loaded["路径"], "存在": loaded["存在"],
                 "行数": loaded["行数"], "坏行数": loaded["坏行数"]},
        "登记期数": len(reg_rows),
        "结算期数": len(set_rows),
        "独立评估批": {"登记期数": len(eval_reg), "结算期数": counted},
        "门槛": gate,
        "还需期数": (max(0, need - counted) if need is not None else None),
        "接线状态": ("本函数只读台账与冻结快照，不判断生产链路是否接线"
                     "（接线状态由编排层与运行日志负责披露）；"
                     "生产生效注数会漂移，前缀读数须按届时的生产票序冻结数据重跑，"
                     "本离线模块不读取也不固定任何生产注数"),
        "说明": [NOT_OPEN_NOTE, STOP_RULE_NOTE, NO_VERDICT_NOTE,
                 FREEZE_GATE_NOTE, COUNTERFACTUAL_NOTE, NOT_PURCHASED_NOTE,
                 TERMINOLOGY_NOTE],
    }
