# -*- coding: utf-8 -*-
"""ev/shadow_bridge.py —— 影子验证的**四包外唯一编排层**（白名单文件）。

为什么单独一个模块
------------------
生产四包（`prediction/` `web/` `pipeline/` `data/`）**不得 import 任何研究侧 ev 模块**，
这是三条静态护栏（`tests/test_prefix_curve.py` / `test_prefix_shadow.py` /
`test_prefix_real.py`）守的线。但影子验证要在生产出号后写一份「开奖前冻结」的证据，
所以必须有一个**被显式放行**的接入点。本模块就是那个唯一的接入点：

  · 生产侧只允许 `cli.py` / `web/utils.py` 在出号返回后调用 `freeze_after_predict()`；
  · 四包内出现 `ev.shadow_bridge`（或任何）import ⇒ 护栏红；
  · 白名单若被扩到四包 ⇒ `test_shadow_bridge_is_only_whitelisted_entry` 红。

五条不变量（每条都有对应牙齿测试）
----------------------------------
1. **不读系统时间**：`batch_time` / `register_time` / `target_date` 全由调用方注入。
   `build_snapshot` 与 `order_freeze` 本来就拒收空时间、不代填 now，本模块沿用。
2. **不读系统随机**：`seed` 由调用方注入；缺省按 `crc32(彩种|期号)` 确定性派生。
3. **不写固定注数**：`K` 必须来自调用方（`clamp()` / engine 的生效注数），
   本模块不决定买多少，也不允许把 K 写死。
4. **不 fail-open**：开关文件缺失 / `启用=false` ⇒ 返回「不适用」并逐条写原因，
   **绝不记为通过**（缺席≠通过）。
5. **失败不阻断出号**：任何异常都收敛成「失败」返回，让证据层永远不影响真钱链路。

诚实披露（进快照备注，不是口头声明）
------------------------------------
- 主路径 `engine.predict()` 的随机性来自 `random.seed(datetime.now().microsecond)`
  （`prediction/engine.py:1920`），因此**票序不能靠 seed 复现**。本模块记录的
  `随机种子` 只是标识用的确定性派生值；真正的锁定是 `票序哈希`。
- 开奖时刻来自 `config/draw_schedule.json`（方案 A 声明值），它是**开奖直播开始
  时刻**，被当作开奖事件时间的上界使用；核验状态原样带进快照，不拔高。
- 本模块不产生任何统计判决；达门槛前影子验证一律「未开判」。
"""
from __future__ import annotations

import json
import logging
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ev import draw_schedule as DS
from ev import order_freeze as OF
from ev import prefix_shadow as PS

logger = logging.getLogger("ev.shadow_bridge")

BRIDGE_VERSION = "2026-11-06-A"

# 主口径前缀档（与 ev/prefix_curve.py::PREFIX_KS 同源校验，不手抄数值集合）
MAIN_N = 3

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "prefix_shadow.json"
SCHEDULE_PATH = BASE_DIR / "config" / "draw_schedule.json"

DEFAULTS: Dict[str, Any] = {
    "启用": False,
    "时刻表来源": "config/draw_schedule.json",
    "外部源": None,
    "模式": "过渡A",
}

STATUS_OK = "已登记"
STATUS_NA = "不适用"
STATUS_FAIL = "失败"


def load_switch(path: Optional[Path] = None) -> Dict[str, Any]:
    """读影子开关。缺文件 ⇒ 返回 `启用=false` 的默认值（不 fail-open）。"""
    p = Path(path) if path is not None else CONFIG_PATH
    if not p.exists():
        return dict(DEFAULTS, _缺失=True, _路径=str(p))
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # 配置读坏了也必须「更保守」：按关闭处理并写原因，绝不静默打开。
        return dict(DEFAULTS, _损坏=f"{type(exc).__name__}: {exc}", _路径=str(p))
    if not isinstance(doc, dict):
        return dict(DEFAULTS, _损坏=f"顶层非对象：{type(doc).__name__}", _路径=str(p))
    out = dict(DEFAULTS)
    out.update({k: v for k, v in doc.items() if not str(k).startswith("_")})
    return out


def is_enabled(path: Optional[Path] = None) -> Dict[str, Any]:
    """开关是否真的开启（显式 true 才算）。返回 `{启用, 原因, 配置}`。"""
    cfg = load_switch(path)
    if cfg.get("_缺失"):
        return {"启用": False,
                "原因": f"开关文件不存在：{cfg['_路径']}（按关闭处理，不 fail-open）",
                "配置": cfg}
    if cfg.get("_损坏"):
        return {"启用": False,
                "原因": f"开关文件不可用：{cfg['_损坏']}（按关闭处理）",
                "配置": cfg}
    if cfg.get("启用") is not True:
        return {"启用": False, "原因": "开关 启用 != true", "配置": cfg}
    return {"启用": True, "原因": "", "配置": cfg}


def _resolve(base: Path, rel: Any) -> Path:
    p = Path(str(rel or ""))
    return p if p.is_absolute() else (base / p)


def derive_seed(lottery: str, issue: Any) -> int:
    """确定性派生 seed（与 `prediction.engine._stable_seed` 同构：crc32）。

    ★ 它只作**标识**：主路径票序的随机性来自 `datetime.now().microsecond`，
      seed 不能复现票序。要复现必须用快照里的 `票序哈希` 校验。
    """
    return zlib.crc32(f"{lottery}|{issue}".encode("utf-8")) & 0xFFFFFFFF


def freeze_after_predict(
        *, lottery: str, issue: Any, tickets: Sequence[Dict[str, Any]], K: int,
        target_date: Any, batch_time: Any, register_time: Any,
        strategy_version: str = "未记录",
        rule_version: Optional[Any] = None,
        code_version: str = "未记录",
        seed: Optional[int] = None,
        target: Any = None,
        renumber: Any = 0,
        n: int = MAIN_N,
        schedule_path: Optional[Path] = None,
        switch_path: Optional[Path] = None,
        snapshot_dir: Optional[Path] = None,
        shadow_path: Optional[Path] = None,
        switch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """出号后立即冻结票序并登记前 n 注。**永不抛出**，失败只返回「失败」。

    参数全部由调用方注入：本函数不读系统时间、不读系统随机、不决定注数。
    返回 `{"状态": 已登记|不适用|失败, "原因": str, ...}`。
    """
    base = {"彩种": str(lottery), "期号": str(issue), "编排版本": BRIDGE_VERSION}
    try:
        sw = switch if switch is not None else is_enabled(switch_path)
        if not sw.get("启用"):
            return dict(base, 状态=STATUS_NA,
                        原因=str(sw.get("原因") or "影子开关未开启"),
                        披露=["关闭期间不产生任何快照与台账行；不记为通过。"])

        # ---- 1. 开奖时刻（方案 A：声明值，treated as 开奖事件上界）----
        # 优先级：显式 schedule_path > 开关里的「时刻表来源」> 默认路径。
        if schedule_path is not None:
            sched_file = Path(schedule_path)
        else:
            sched_file = _resolve(
                BASE_DIR,
                (sw.get("配置") or {}).get("时刻表来源") or SCHEDULE_PATH)
        sched_doc = DS.load_schedule(sched_file)
        sched = DS.validate_schedule(sched_doc)
        if not sched["可用"]:
            return dict(base, 状态=STATUS_NA,
                        原因="时刻表未覆盖全部彩种：" + json.dumps(
                            sched["未参与"], ensure_ascii=False))
        draw_time = DS.build_draw_time(lottery, target_date, sched_doc)

        # ---- 2. 时区口径 + 先后关系预检（早失败，给明确原因）----
        DS.check_time_basis(batch_time, register_time, draw_time)
        batch_dt = datetime.fromisoformat(str(batch_time))
        draw_dt = datetime.fromisoformat(str(draw_time))
        if not batch_dt < draw_dt:
            return dict(base, 状态=STATUS_FAIL,
                        原因=f"批次时间 {batch_time} 不早于开奖时间 {draw_time}"
                             "（不具备开奖前冻结资格）")

        # ---- 3. 冻结（不重排：调用方给的顺序就是冻结顺序）----
        tks = list(tickets)
        if len(tks) != int(K):
            return dict(base, 状态=STATUS_FAIL,
                        原因=f"票序 {len(tks)} 注 != K={K}（前缀口径要求恰好 K 注）")
        rv = rule_version
        if rv is None:
            from ev.profit.rules import rules_for
            rv = rules_for(lottery, issue).rule_version
        # ★ 重出批号进哈希：同一期点「重新出号」是**又买了一套票**，票序不同；
        #   不进哈希会让第 2 批撞第 1 批的文件名，被 save_snapshot 的
        #   「同键不同内容拒绝覆盖」挡下 —— 等于重出批永远拿不到证据。
        try:
            _rn = int(renumber or 0)
        except (TypeError, ValueError):
            _rn = 0
        snap = OF.build_snapshot(
            lottery=lottery, issue=issue, tickets=tks,
            strategy_version=str(strategy_version),
            seed=int(derive_seed(lottery, issue) if seed is None else seed),
            K=int(K), rule_version=rv, code_version=str(code_version),
            batch_time=str(batch_time), target=target,
            time_nature=PS.PRODUCTION_NATURE,
            experiment={"重出批号": _rn},
            extra={
                "编排版本": BRIDGE_VERSION,
                "开奖时刻口径": "开奖直播开始时刻（声明值），作为开奖事件时间上界",
                "时刻表核验状态": sched["核验状态"],
                "时刻表来源": sched["来源"],
                "种子性质": "标识用（crc32(彩种|期号)）；主路径随机性来自系统微秒，"
                            "seed 不能复现票序，复现以票序哈希为准",
                "未记录字段": [k for k, v in
                               (("策略版本", strategy_version),
                                ("代码版本", code_version)) if v == "未记录"],
            })
        path = OF.save_snapshot(snap, directory=snapshot_dir)

        # ---- 4. 登记前 n 注（登记行不含任何开奖读数）----
        reg = PS.register_prefix(snap, n=int(n), draw_time=draw_time,
                                 register_time=str(register_time),
                                 path=shadow_path)
        return dict(base, 状态=STATUS_OK, 原因="", 开奖时间=draw_time,
                    快照路径=str(path), n=int(n),
                    票序哈希=snap["票序哈希"], 策略哈希=snap["策略哈希"],
                    登记时间=str(register_time), 批次时间=str(batch_time),
                    披露=[
                        "开奖时刻是声明值（直播开始时刻）而非实测；它只提供可比基准，"
                        "不构成对开奖时刻的证明。",
                        "本登记不构成任何策略优势证据；达门槛前影子验证一律「未开判」。",
                    ])
    except PS.PrefixShadowError as exc:
        if "已有登记" in str(exc):
            return dict(base, 状态=STATUS_OK, 原因="同键已存在登记，拒绝回改（幂等跳过）")
        logger.warning(f"影子登记被拒（不阻断出号）：{exc}")
        return dict(base, 状态=STATUS_FAIL, 原因=f"PrefixShadowError: {exc}")
    except Exception as exc:                                   # noqa: BLE001
        # ★ 证据层永不影响真钱链路：任何异常都只告警 + 返回失败。
        logger.warning(f"影子冻结失败（不阻断出号）：{type(exc).__name__}: {exc}")
        return dict(base, 状态=STATUS_FAIL, 原因=f"{type(exc).__name__}: {exc}")
