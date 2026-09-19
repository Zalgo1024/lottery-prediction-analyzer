"""
反馈数据管理模块（反馈闭环 - 阶段2）

管理三类数据：
1. pending 预测：已生成但未开奖的预测记录
2. 反馈历史：已开奖并对比过命中的历史记录
3. 策略权重：基于反馈历史动态调整的各策略权重

文件结构：
    training/feedback/
    ├── <lottery>_pending.json          # 待开奖的预测
    ├── <lottery>_feedback_history.json # 历史命中记录
    └── <lottery>_strategy_weights.json # 当前策略权重
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR
from data.holiday import is_draw_day
from data.loader import load_lottery
from data.schema import schema_from_cfg, normalize_zone_names

logger = logging.getLogger(__name__)

# 反馈数据目录
FEEDBACK_DIR = TRAINING_DIR / "feedback"
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

# 生产反馈目录常量（测试 monkeypatch `*_path` 后不影响本判定）
_PROD_FEEDBACK_DIR = FEEDBACK_DIR


def _is_pytest() -> bool:
    """是否运行在 pytest 进程内。"""
    return ("pytest" in sys.modules) or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _is_prod_feedback_path(path) -> bool:
    """路径是否落在生产反馈目录内。"""
    try:
        p = Path(path).resolve()
        d = _PROD_FEEDBACK_DIR.resolve()
    except Exception:
        return False
    return p == d or d in p.parents

# 默认策略权重（均分）
DEFAULT_STRATEGIES = ["高频策略", "遗漏值策略", "区间均衡策略"]

# 训练模型（ML / 统计训练）归一化后的统一策略名，参与反馈权重分配
ML_STRATEGY = "ML策略"

# 闸门联动：通过闸门的策略（有 proven edge）可分配的权重质量上限比例，
# 剩余 (1-GATE_WINNER_MASS) 由未通过闸门的策略均分。
# 目的：避免单条幸运命中（固定赔率彩种中一次 +1038 / 未中 -2）把权重顶到 0.85。
GATE_WINNER_MASS = 0.5

# 三档号码选择（prediction/robust_tiers）的档位标签。
# 历史/无档位记录一律视为「一般」档（向后兼容）。
TIER_GENERAL = "一般"
TIER_ROBUST = "稳健"
TIER_HIGH = "高鲁棒"
VALID_TIERS = (TIER_GENERAL, TIER_ROBUST, TIER_HIGH)


def _tier_of(record: dict) -> str:
    """取记录的档位标签；无/非法档位一律归「一般」（旧记录向后兼容）。"""
    t = (record or {}).get("档位")
    return t if t in VALID_TIERS else TIER_GENERAL


def _make_batch_id(lottery_name: str, prediction: dict) -> str:
    """
    生成批次号：一次出号 = 一条 pending = 一个批次，组内所有注共用同一个号。

    形如 `双色球-26106-一般-20260913-214858`（彩种-目标期号-档位-时间戳）。
    带上档位是因为三档管线（一般/稳健/高鲁棒）会各自对同一期出号，
    它们票面可能完全相同，但属于三次独立的出号任务，不该并成一个批次。
    2026-09-14 之前的历史记录没有这个字段，查询侧由 ev/batch.py 按评估时间聚簇回推。
    """
    raw = prediction.get("记录时间") or datetime.now().isoformat()
    try:
        ts = datetime.fromisoformat(str(raw)).strftime("%Y%m%d-%H%M%S")
    except (ValueError, TypeError):
        ts = str(raw)[:19].replace("-", "").replace(":", "").replace(" ", "-").replace("T", "-")
    tier = _tier_of(prediction)
    return f"{lottery_name}-{prediction.get('目标期号')}-{tier}-{ts}"


def normalize_strategy_name(name: str) -> str:
    """把 ML 家族（ML(logistic) / 统计训练(W50) / ML策略 等）统一归并为 'ML策略'。

    历史记录里早期标签是 ML(logistic)、统计训练(W50)，新标签是 ML策略。
    统一归并后，反馈权重、战绩榜、前端展示都只看到一个清晰的「ML策略」。
    """
    if not name:
        return name
    if name == ML_STRATEGY or "ML" in name or "统计训练" in name:
        return ML_STRATEGY
    return name


def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    """合并权重字典里的 ML 家族键到 ML策略（用于展示与统计一致，兼容旧权重文件）。"""
    merged: Dict[str, float] = {}
    for k, v in weights.items():
        nk = normalize_strategy_name(k)
        merged[nk] = merged.get(nk, 0.0) + v
    return merged


# ============================================================
# 文件路径辅助
# ============================================================

def _pending_path(lottery_name: str) -> Path:
    return FEEDBACK_DIR / f"{lottery_name}_pending.json"


def _history_path(lottery_name: str) -> Path:
    return FEEDBACK_DIR / f"{lottery_name}_feedback_history.json"


def _weights_path(lottery_name: str) -> Path:
    return FEEDBACK_DIR / f"{lottery_name}_strategy_weights.json"


# ============================================================
# 文件 IO 容错（并发读写/文件锁重试）
# ============================================================

def _safe_read_json(path: Path, default: Any = None, retries: int = 5) -> Any:
    """带重试的 JSON 读取，缓解多进程并发导致的 Permission denied"""
    last_err = None
    for attempt in range(retries):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(0.03 * (attempt + 1))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            # 文件损坏（被中断写入/乱码）：不阻塞主流水线，按空值处理并告警。
            # 损坏的 pending 会在下一次流水线运行时被正常预测覆盖重建。
            logger.warning(f"反馈文件损坏（按空处理）: {path} -> {e}")
            return default
    logger.warning(f"读取反馈文件失败（已重试 {retries} 次）: {path} -> {last_err}")
    raise last_err


def _safe_write_json(path: Path, data: Any, retries: int = 5):
    """带重试的 JSON 写入，缓解多进程并发导致的 Permission denied

    ⚠️ 测试写保护：pytest 进程内禁止写入**生产**反馈目录（training/feedback/），
    否则 `predict()` 之类被测试直接调用的路径会用测试参数（如 groups=3）覆盖生产
    pending —— 2026-09-11 实锤：`tests/test_robust_tiers.py` 把排列5 26244 期
    pending 覆盖成 3 组。需要真实写入的测试请 monkeypatch `_pending_path` 等到
    tmp 目录（如 test_feedback_tiers 的 tmp_fb 夹具）。
    """
    if (_is_pytest() and _is_prod_feedback_path(path)
            and os.environ.get("WORKBUDDY_ALLOW_PROD_FB_WRITE") != "1"):
        logger.warning(
            "[pytest 写保护] 已阻止写生产反馈文件: %s（如需真实写入请隔离到 tmp 目录，"
            "或显式设置 WORKBUDDY_ALLOW_PROD_FB_WRITE=1）", path,
        )
        return
    last_err = None
    for attempt in range(retries):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            return
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(0.03 * (attempt + 1))
    logger.warning(f"写入反馈文件失败（已重试 {retries} 次）: {path} -> {last_err}")
    raise last_err


# ============================================================
# Pending 预测管理
# ============================================================

def load_pending(lottery_name: str) -> List[dict]:
    """加载待开奖的预测记录"""
    path = _pending_path(lottery_name)
    if not path.exists():
        return []
    return _safe_read_json(path, default=[])


def save_pending(lottery_name: str, pending: List[dict]):
    """保存待开奖的预测记录"""
    path = _pending_path(lottery_name)
    _safe_write_json(path, pending)


def dedupe_pending(lottery_name: str) -> int:
    """
    一次性清理 pending：同一 (目标期号, 档位) 只保留记录时间最新的一条。
    档位化后三档（一般/稳健/高鲁棒）同期待开奖共存，去重键必须是期号+档位。
    返回实际删除的条数。
    """
    pending = load_pending(lottery_name)
    if not pending:
        return 0
    best_by_key = {}
    for r in pending:
        issue = r.get("目标期号")
        key = (issue, _tier_of(r))
        if issue is None:
            # 无目标期号的孤立记录，保留最后一条
            best_by_key.setdefault(key, r)
            continue
        cur = best_by_key.get(key)
        if cur is None or (r.get("记录时间", "") > cur.get("记录时间", "")):
            best_by_key[key] = r
    deduped = list(best_by_key.values())
    removed = len(pending) - len(deduped)
    if removed > 0:
        save_pending(lottery_name, deduped)
        logger.info(f"pending 去重完成: {lottery_name} 删除 {removed} 条，保留 {len(deduped)} 条")
    return removed


def predict_target(lottery_name: str) -> Tuple[Optional[int], Optional[date]]:
    """出号统一口径（引擎/三档管线共用）：返回 (目标期号, 该期推算开奖日)。

    此前「目标期号」用 max+1、「预测日期」用 next_draw_date(从今天起找) 两个
    独立口径，数据滞后时会配出 (26103, 9/8) 这类错对——期号那期其实 9/6 就开了。
    统一为：期号 = 本地最大期号+1；开奖日 = 从本地最近开奖日按 draw_days 推算
    该期对应的那一天（与 _target_draw_date 同源，评估端按同一口径判真伪）。
    """
    issue = _next_issue(lottery_name)
    draw_day = _target_draw_date(lottery_name, issue) if issue is not None else None
    return issue, draw_day


def record_pending_prediction(lottery_name: str, prediction: dict):
    """
    记录一次新预测到 pending 列表（等待开奖后对比）

    prediction 格式：
    {
        "预测日期": "2026-07-02",
        "生成时间": "2026-07-01T00:30:00",
        "来源": "predict",          # 可选：predict(日常预测)/train(训练)
        "预测号码": [
            {"红球": [...], "蓝球": [...], "策略": "高频策略", "置信度": 0.35},
            ...
        ],
        "状态": "pending"
    }
    """
    pending = load_pending(lottery_name)
    prediction["状态"] = "pending"
    prediction["记录时间"] = datetime.now().isoformat()
    # 来源：显式传入优先，否则按是否带训练标记推断，再回退 predict
    if not prediction.get("来源"):
        prediction["来源"] = "train" if prediction.get("训练标记") else "predict"
    # 推断目标期号（稳定主键，评估时优先按期号匹配）
    # 若 prediction 已带目标期号（如测试注入）则保留，避免覆盖
    if not prediction.get("目标期号"):
        prediction["目标期号"] = _next_issue(lottery_name)

    # —— 批次号：一次出号（一条 pending）内的所有注共用一个批次号 ——
    # 开奖后即可回答「这一注是哪一批出号算出来的」。调用方若已显式指定则保留。
    if not prediction.get("批次号"):
        prediction["批次号"] = _make_batch_id(lottery_name, prediction)
    # —— 质量+重叠压缩（2026-09-14）：生产出号入账前裁掉低质量/高重复注 ——
    # 训练样本（来源=train）保持全量，评估口径不受影响；失败不阻断入账。
    # 压缩 KPI 记进 prediction["压缩"]（压缩前注数/淘汰明细/诚实声明）。
    if prediction.get("来源") != "train" and prediction.get("预测号码"):
        try:
            from ev.coverage import compress_tickets
            _tickets = prediction["预测号码"]
            _kept, _kpi = compress_tickets(_tickets, lottery_name)
            if len(_kept) < len(_tickets):
                prediction["预测号码"] = [_tickets[i] for i in _kept]
                prediction["压缩"] = _kpi
                logger.info(f"{lottery_name} 出号压缩: {_tickets.__len__()} → "
                            f"{len(_kept)} 注（质量 {_kpi['质量淘汰']} / 重复 "
                            f"{_kpi['重复淘汰']} / 重叠 {_kpi['重叠淘汰']}）")
        except Exception as e:  # pragma: no cover - 压缩失败不阻断 pending 入账
            logger.warning(f"出号压缩失败 {lottery_name}: {e}")

    # 批次规模：本批出号注数（压缩后的最终登记口径）
    prediction["批次规模"] = len(prediction.get("预测号码") or [])
    # ★ 号码组数同步（2026-09-20 修）：引擎在压缩前写入 len(预测号码)（如 88），
    #   压缩后票面只剩 36 注但字段没跟着更新 → 审计「号码组数不符」每晚误报。
    #   契约：号码组数 == len(预测号码)（见 ev/coverage.py 模块注释）。
    if prediction.get("预测号码") is not None:
        prediction["号码组数"] = len(prediction["预测号码"])

    # —— 去重：同一 (目标期号, 档位) 只保留最新一条预测 ——
    # auto 流水线每次运行都会写入一组新预测，若开奖前不清理，
    # pending 会无限累积同一期号码（双色球曾堆积 67 条同期）。
    # 三档管线（robust_tiers）后，同期待开奖存在 一般/稳健/高鲁棒 三条记录，
    # 去重键必须带上档位，否则三档会互相覆盖。
    # 这里在写入前移除同 (目标期号, 档位) 的旧记录，保证列表清爽。
    target_issue = prediction.get("目标期号")
    tier = _tier_of(prediction)
    if target_issue is not None:
        pending = [r for r in pending
                   if not (r.get("目标期号") == target_issue and _tier_of(r) == tier)]

    # —— 防御：马后炮（目标期实际已开奖，开奖后才生成的号码不入账）——
    # 两道闸（来源='train' 的训练回测刻意预测已开奖期号，放行）：
    # ① 目标期号已存在于本地 CSV → 该期必然已开奖（含本地数据比生成时更新的情形）
    # ② 推算开奖日已过：早于今天，或就是今天但生成时间已过 20:00（开奖均在 21:15+）
    # 2026-09-06 实锤：数据滞后时流水线仍对已开的 26103 期出号，积压 50 条马后炮。
    if prediction.get("来源") != "train":
        target_issue = prediction.get("目标期号")
        try:
            local_max = max((r.期号 for r in load_lottery(lottery_name).records
                             if r.期号), default=None)
        except Exception:
            local_max = None
        if (target_issue is not None and local_max is not None
                and target_issue <= local_max):
            msg = (f"⚠️ 拒绝记录 pending：{lottery_name} 目标期号 {target_issue} "
                   f"已存在于本地数据（最新期号 {local_max}），该期已开奖，"
                   f"此时出号属马后炮。请先更新数据再预测。")
            logger.warning(msg)
            return {
                "状态": "已拒绝(目标期号已开奖)",
                "原因": "目标期号已存在于本地数据，该期已开奖",
                "目标期号": target_issue,
            }
        pred_date = _parse_iso_date(prediction.get("预测日期", ""))
        target_draw = _target_draw_date(lottery_name, prediction["目标期号"])
        if pred_date and target_draw and pred_date > target_draw:
            msg = (f"⚠️ 拒绝记录 pending：{lottery_name} 目标期号 {prediction['目标期号']} "
                   f"推算开奖日 {target_draw} 已早于预测日期 {pred_date}，疑似本地数据过期。"
                   f"请先运行数据更新（fetch）再预测，避免「马后炮」假命中。")
            logger.warning(msg)
            return {
                "状态": "已拒绝(目标期号已开奖)",
                "原因": "本地数据可能过期，请先更新数据再预测",
                "目标期号": prediction["目标期号"],
                "推算开奖日": str(target_draw),
            }
        if target_draw is not None:
            gen_raw = prediction.get("生成时间")
            try:
                gen_dt = (datetime.fromisoformat(str(gen_raw))
                          if gen_raw else datetime.now())
            except (ValueError, TypeError):
                gen_dt = datetime.now()
            if (target_draw < date.today()
                    or (target_draw == date.today() and gen_dt.time() >= datetime.strptime("20:00", "%H:%M").time())):
                msg = (f"⚠️ 拒绝记录 pending：{lottery_name} 目标期号 "
                       f"{prediction['目标期号']} 推算开奖日 {target_draw} 的开奖时间已过"
                       f"（生成时间 {gen_dt.isoformat(timespec='seconds')}），马后炮出号不入账。")
                logger.warning(msg)
                return {
                    "状态": "已拒绝(开奖时间已过)",
                    "原因": "目标期推算开奖时间已过，马后炮出号不入账",
                    "目标期号": prediction["目标期号"],
                    "推算开奖日": str(target_draw),
                    "生成时间": gen_dt.isoformat(timespec="seconds"),
                }

    pending.append(prediction)
    save_pending(lottery_name, pending)
    logger.info(f"已记录 pending 预测: {lottery_name}, 预测日期={prediction.get('预测日期')}, 目标期号={prediction.get('目标期号')}")


# ============================================================
# 反馈历史管理
# ============================================================

def load_feedback_history(lottery_name: str, lookback: Optional[int] = None) -> List[dict]:
    """加载历史命中记录，可选只取最近 lookback 条"""
    path = _history_path(lottery_name)
    if not path.exists():
        return []
    history = _safe_read_json(path, default=[])
    if lookback and len(history) > lookback:
        history = history[-lookback:]
    return history


def save_feedback_history(lottery_name: str, history: List[dict]):
    """保存历史命中记录"""
    path = _history_path(lottery_name)
    _safe_write_json(path, history)


def append_feedback(lottery_name: str, feedback: dict):
    """追加一条反馈记录"""
    history = load_feedback_history(lottery_name)
    history.append(feedback)
    save_feedback_history(lottery_name, history)


# ============================================================
# 策略权重管理
# ============================================================

def load_strategy_weights(lottery_name: str) -> Dict[str, float]:
    """加载当前策略权重，不存在则返回均分默认值"""
    path = _weights_path(lottery_name)
    default_weights = {s: 1.0 / len(DEFAULT_STRATEGIES) for s in DEFAULT_STRATEGIES}
    if not path.exists():
        return default_weights
    return _safe_read_json(path, default=default_weights)


def save_strategy_weights(lottery_name: str, weights: Dict[str, float]):
    """保存策略权重"""
    path = _weights_path(lottery_name)
    _safe_write_json(path, weights)


# ============================================================
# 开奖对比评估
# ============================================================

def _classify_prize(red_hits: int, blue_hits: int) -> str:
    """中奖等级分类（双色球规则）"""
    if red_hits == 6 and blue_hits == 1:
        return "一等"
    elif red_hits == 6:
        return "二等"
    elif red_hits == 5 and blue_hits == 1:
        return "三等"
    elif red_hits == 5 or (red_hits == 4 and blue_hits == 1):
        return "四等"
    elif red_hits == 4 or (red_hits == 3 and blue_hits == 1):
        return "五等"
    elif blue_hits == 1 or (red_hits == 2 and blue_hits == 1):
        return "六等"
    else:
        return "未中"


def _classify_dlt_prize(red_hits: int, blue_hits: int) -> str:
    """
    中奖等级分类（大乐透专用，2023 新规九级）。

    ⚠️ 历史坑：此前红蓝彩种统一走 `_classify_prize`（双色球规则），对大乐透
    造成系统性误判：5+0 判四等(官方三等)、4+2/3+2 判五等(官方四等)、
    3+0/1+2/0+2 官方中奖却被判"未中"(漏判)、0+1/1+1 官方**不中奖**却判"六等"(虚报)。
    条件与 `ev/rollover.py::_dlt_fixed()`（前区5/35、后区2/12）逐条一致：
      一等 5+2（浮动）；二等 5+1（浮动）；
      三等 5+0；四等 4+2；五等 4+1；六等 3+2；七等 4+0；
      八等 3+1、2+2；九等 3+0、2+1、1+2、0+2；其余（2+0/1+1/1+0/0+1/0+0）未中。
    命名延续乐透型体系"X等"（不带"奖"字）。
    """
    if red_hits == 5 and blue_hits == 2:
        return "一等"
    if red_hits == 5 and blue_hits == 1:
        return "二等"
    if red_hits == 5 and blue_hits == 0:
        return "三等"
    if red_hits == 4 and blue_hits == 2:
        return "四等"
    if red_hits == 4 and blue_hits == 1:
        return "五等"
    if red_hits == 3 and blue_hits == 2:
        return "六等"
    if red_hits == 4 and blue_hits == 0:
        return "七等"
    if (red_hits == 3 and blue_hits == 1) or (red_hits == 2 and blue_hits == 2):
        return "八等"
    if (red_hits == 3 and blue_hits == 0) or (red_hits == 2 and blue_hits == 1) \
            or (red_hits == 1 and blue_hits == 2) or (red_hits == 0 and blue_hits == 2):
        return "九等"
    return "未中"


def _hit_detail(lottery_name: str, pred_ps: dict, actual_rec, schema) -> tuple:
    """
    计算一条预测 vs 实际开奖的「逐分区命中」。

    双色球/大乐透（无序乐透型）：分区内按集合交集算命中数。
    数字型（有序）：逐位置精确匹配算命中数（位置+数字都对才算）。

    返回 (zone_hits, total_hits, total_choose, is_redblue)：
      - zone_hits: {分区名: 命中数}
      - total_hits: 所有分区命中数之和
      - total_choose: 所有分区应选数之和（红6+蓝1=7；排列5=5…）
      - is_redblue: 是否为红/蓝彩种（决定走官方奖级还是通用命中率）
    """
    pred_zones = pred_ps.get("号码")
    if pred_zones is None:
        # 旧格式兼容：红球/蓝球 拆成两个分区
        pred_zones = {
            "红球": pred_ps.get("红球", []),
            "蓝球": pred_ps.get("蓝球", []),
        }
    # 旧分区名归一化（数字型历史遗留：万位/千位/… → 第N位）。
    # 不做这一步时 zones.get("第N位") 取到空列表 → 命中被静默算成 0（fail-silent）。
    pred_zones = normalize_zone_names(pred_zones, schema)
    actual_zones = normalize_zone_names(actual_rec.zone_numbers, schema)

    zone_hits = {}
    for z in schema.zones:
        pn = pred_zones.get(z.name, [])
        an = actual_zones.get(z.name, [])
        if z.ordered:
            h = sum(1 for i in range(min(len(pn), len(an))) if pn[i] == an[i])
        else:
            h = len(set(pn) & set(an))
        zone_hits[z.name] = h

    # 兜底告警：分区名对不上（预测里有号码、schema 里却找不到对应区）→ 命中必然为 0，
    # 属于结构性错误，必须响亮地报出来，不能再静默吞掉。
    known = {z.name for z in schema.zones}
    unknown = [k for k in pred_zones if k not in known and pred_zones.get(k)]
    if unknown:
        logger.warning(
            "[_hit_detail] %s 预测分区名无法识别 %s（schema=%s）：命中将被算作 0，请检查分区名迁移",
            lottery_name, sorted(unknown), sorted(known),
        )

    total_hits = sum(zone_hits.values())
    total_choose = sum(z.choose for z in schema.zones)
    is_redblue = bool(schema.red_zone and schema.blue_zone)
    return zone_hits, total_hits, total_choose, is_redblue


def _classify_prize_generic(total_hits: int, total_choose: int) -> str:
    """
    通用奖级（**仅限未接入官方规则的全新彩种**兜底用）：按「命中数 / 应选总数」比例分级。

    数字型每位 choose=1，total_choose 即总位数（排列5=5、福彩3D=3…），
    比例就是「命中位占比」；乐透型则用总命中占比。
    命中率=1.0 → 一等；≥0.8 二等；≥0.6 三等；≥0.4 四等；>0 五等；否则未中。

    ⚠️⚠️ 2026-08-30 起**数字型已全部接入官方规则，禁止再用本函数**：
    福彩3D/排列3 = 直选·组选3·组选6·未中；排列5 = 直选·未中；
    七星彩 = 一~六等奖(唯一按位数分级的数字型)。
    此前误用于数字型，导致历史里出现"命中1位→五等""命中2位→三等"这种
    **数字型官方根本不存在的奖级名**（福彩3D 25条、排列3 24条、排列5 2条被污染）。
    新彩种接入时请优先在 `_group_winning` / `_qxc_prize` 写官方规则，不要用本函数凑。
    """
    if total_choose <= 0:
        return "未中"
    ratio = total_hits / total_choose
    if ratio >= 1.0:
        return "一等"
    elif ratio >= 0.8:
        return "二等"
    elif ratio >= 0.6:
        return "三等"
    elif ratio >= 0.4:
        return "四等"
    elif ratio > 0:
        return "五等"
    else:
        return "未中"


# ============================================================
# 模拟盈亏 P/L 结算（任务1.3：目标函数从"命中率"改"盈亏"）
# ============================================================
def _digits_from_zones(zones) -> list:
    """把 {分区名: [数字...]} 拍平成数字列表（去嵌套、转 int）。"""
    out = []
    if not isinstance(zones, dict):
        return out
    for v in zones.values():
        if isinstance(v, (list, tuple)):
            for x in v:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    pass
    return out


def _pl5_direct_payout(issue):
    """排列5 直选单注奖金：优先读当期实际单注奖金（已含风险控制），无数据则名义 100000。"""
    try:
        from data.fetch_sales import load_sales
        df = load_sales("排列5")
        if df is not None and issue:
            row = df[df["期号"] == str(issue)]
            if not row.empty and "一等奖单注奖金" in row.columns:
                v = row["一等奖单注奖金"].iloc[-1]
                if v is not None and v == v and v > 0:
                    return float(v), "实际赔付(含风险控制)"
    except Exception:
        pass
    return 100000.0, "名义EV"


def _group_winning(lottery: str, pred_digits: list, actual_digits: list, issue=""):
    """
    数字型「最佳奖项」判定（官方中奖规则）。
    返回 (玩法等级, 单注赔付, 结算方式)；未中返回 (None, 0.0, "名义EV")。
    规则：
    - 福彩3D/排列3：直选(全位精确)→1040；同数字集合→组选3(346,有重号)/组选6(173,全异)。
    - 排列5：**只设 1 个奖级**——直选(5位全对且顺序一致)→当期实际单注奖金(含风险控制，名义10万)。
      ⚠️ 没有"组选"奖级：六形态(五不同/二同/三同/两组二同/四同/三同二同)是「直选全组合」
      投注方式(打包买下该形态全部排列)，中奖仍按直选10万兑1注；
      240/120/60/40/20/10 = 排列数(120/60/30/20/10/5)×2元 = **投注成本，不是奖金**。
      故数字同集合但顺序不同 = 未中奖。
    - 七星彩：走 _settle_pl 专用分支（按 X=前6位命中数 / Y=后区是否中 判定），不在此处理。
    """
    from collections import Counter
    if not pred_digits or not actual_digits:
        return None, 0.0, "名义EV"
    n = len(pred_digits)
    exact = (pred_digits == actual_digits)
    same_set = (n == len(actual_digits)) and (sorted(pred_digits) == sorted(actual_digits))

    if lottery in ("福彩3D", "排列3"):
        if exact and n == 3:
            return "直选", 1040.0, "名义EV"
        if same_set and n == 3:
            c = Counter(pred_digits)
            if any(v == 2 for v in c.values()):
                return "组选3", 346.0, "名义EV"
            return "组选6", 173.0, "名义EV"
        return None, 0.0, "名义EV"

    if lottery == "排列5":
        # 仅 1 个奖级：5 位全对且顺序一致。数字同集合但顺序不同 = 未中奖。
        if exact and n == 5:
            payout, mode = _pl5_direct_payout(issue)
            return "直选", payout, mode
        return None, 0.0, "名义EV"

    return None, 0.0, "名义EV"


def _qxc_prize(feedback: dict):
    """
    七星彩(现行版 2020 升级)奖级判定。返回 (等级, 固定赔付)；未中返回 (None, 0.0)。
    一/二等奖为浮动奖 → 固定赔付返回 0.0，由调用方读当期实际单注奖金。

    ⚠️ 官方按「任意位置匹配位数」计奖，**不要求连续**
       （"连续位数 + 奖金1800/300/20"是 2004-2020 旧版，勿混用）。
    设 X = 前6位命中数，Y = 后区(第7位)是否命中，T = X + Y：
        T=7           一等奖(浮动)
        X=6 且 Y=0    二等奖(浮动)
        X=5 且 Y=1    三等奖 3000
        T=5           四等奖 500
        T=4           五等奖 30
        T>=3 或 Y=1   六等奖 5   （含"仅后区中"与"前6位任意1位+后区"）
        其余          未中
    不兼中兼得，按最高奖级。官方中奖注数(分母1500万)：
        1 / 14 / 54 / 1971 / 31590 / 1188270。
    """
    zh = feedback.get("分区命中") or {}

    def _idx(k):
        digits = "".join(ch for ch in str(k) if ch.isdigit())
        return int(digits) if digits else 999

    keys = sorted(zh.keys(), key=_idx)
    if len(keys) < 7:
        return None, 0.0
    X = sum(1 for k in keys[:6] if int(zh.get(k) or 0) > 0)
    Y = 1 if int(zh.get(keys[6]) or 0) > 0 else 0
    T = X + Y
    if T == 7:
        return "一等奖", 0.0
    if X == 6 and Y == 0:
        return "二等奖", 0.0
    if X == 5 and Y == 1:
        return "三等奖", 3000.0
    if T == 5:
        return "四等奖", 500.0
    if T == 4:
        return "五等奖", 30.0
    if T >= 3 or Y == 1:
        return "六等奖", 5.0
    return None, 0.0


def _settle_pl(lottery_name: str, feedback: dict):
    """
    数字型反馈按真实奖级规则结算模拟盈亏（每注成本 2 元）。
    返回 (模拟盈亏, 结算方式, 中奖玩法)：
      - 中奖玩法 为 "直选/组选3/组选6/一等奖~六等奖" 或 None（未中）
      - 双/大 或 未知彩种 → (None, "命中率", None) 回退命中率逻辑
    规则修订（2026-08-29 官方核对）：
      - 排列5 只设 1 个奖级(直选10万)；六形态是「直选全组合」投注方式而非奖级，
        240/120/60/40/20/10 是投注成本，不是奖金。
      - 七星彩按「任意位置匹配位数」计奖(不要求连续)，走 _qxc_prize。
    """
    if "红球命中" in feedback:
        return None, "命中率", None  # 双/大：浮动奖池，无固定赔付表，回退
    pred_digits = _digits_from_zones(feedback.get("预测号码", {}))
    actual_digits = _digits_from_zones(feedback.get("实际号码", {}))
    issue = feedback.get("期号", "")
    level, payout, mode = _group_winning(lottery_name, pred_digits, actual_digits, issue)
    if level is not None:
        return round(payout - 2.0, 2), mode, level

    if lottery_name == "七星彩":
        level, fixed_pay = _qxc_prize(feedback)
        if level:
            if fixed_pay > 0:
                return round(fixed_pay - 2.0, 2), "名义EV", level
            # 一/二等奖浮动：读当期实际单注奖金（优先）；无则名义/0
            try:
                from data.fetch_sales import load_sales
                df = load_sales("七星彩")
                if df is not None:
                    row = df[df["期号"] == str(issue)]
                    if not row.empty:
                        col = f"{level}单注奖金"
                        v = row[col].iloc[-1] if col in row.columns else None
                        if v is not None and v == v and v > 0:
                            return round(float(v) - 2.0, 2), "限号EV", level
            except Exception:
                pass
            if level == "一等奖":
                return round(5000000.0 - 2.0, 2), "名义EV", level
            return -2.0, "名义EV", None  # 二等奖无当期数据 → 视为未中（净亏2元）

    # 数字型未中：净亏 2 元（成本）
    return -2.0, "名义EV", None


def _find_draw_by_date(lottery_name: str, target_date_str: str):
    """根据预测日期找到对应的实际开奖记录"""
    data = load_lottery(lottery_name)
    for rec in data.records:
        if rec.开奖日期 and str(rec.开奖日期) == target_date_str:
            return rec
    return None


def _next_issue(lottery_name: str, from_date: str = None) -> Optional[int]:
    """
    推断下一次开奖的期号（稳定主键，比日期可靠）

    规则：取历史最大期号 + 1；处理年内连续编号（如 2026090 -> 2026091）。
    若末3位达到 999 则进位年份（如 2026999 -> 2027000）。
    """
    try:
        data = load_lottery(lottery_name)
        if not data.records:
            return None
        max_issue = max(r.期号 for r in data.records if r.期号)
        # 取末 3 位编号（双色球/大乐透年内连续编号）
        year = max_issue // 1000
        seq = max_issue % 1000
        if seq >= 999:
            year += 1
            seq = 1
        else:
            seq += 1
        return year * 1000 + seq
    except Exception as e:
        logger.warning(f"推断目标期号失败: {e}")
        return None


def _find_draw_by_issue(lottery_name: str, target_issue: int):
    """按期号（主键）查找实际开奖记录"""
    if target_issue is None:
        return None
    data = load_lottery(lottery_name)
    for rec in data.records:
        if rec.期号 == target_issue:
            return rec
    return None


def _target_draw_date(lottery_name: str, target_issue: int):
    """
    推算目标期号的开奖日（基于本地最近一期开奖日向后推 draw_days）。

    用途：防御「马后炮」——本地数据过期时 _next_issue 算出的期号其实已经开奖。
    通过从本地最近一期开奖日向后逐日找开奖日，推算目标期号对应的开奖日，
    再与预测日期比较即可判定该期是否「预测日期晚于开奖日期」。

    返回 date；信息不足时返回 None（保守放行，避免误杀）。
    """
    try:
        data = load_lottery(lottery_name)
        if not data.records:
            return None
        last_rec = max(data.records, key=lambda r: r.期号 if r.期号 else 0)
        last_date = last_rec.开奖日期
        if not last_date:
            return None
        diff = target_issue - (last_rec.期号 or 0)
        if diff <= 0:
            # 目标期号 <= 本地最近期号，本地已知且已开奖
            return last_date
        # 从 last_date 之后逐日找开奖日，取第 diff 个（与期号一一对应）
        d = last_date
        found = 0
        for _ in range(400):
            d += timedelta(days=1)
            if is_draw_day(lottery_name, d):
                found += 1
                if found == diff:
                    return d
        return None
    except Exception as e:
        logger.warning(f"推算目标期号开奖日失败: {e}")
        return None


def _parse_iso_date(s):
    """解析 YYYY-MM-DD / YYYY/MM/DD 为 date，失败返回 None"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(s)[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _is_valid_prediction(pred_date_str, actual_date):
    """
    判定一条反馈是否为「真预测」而非「马后炮回测」。

    规则：预测日期必须不晚于开奖日期。
    - 预测日期 <= 开奖日期 → 真预测（开奖前生成，valid=True）
    - 预测日期 >  开奖日期 → 开奖后才生成（马后炮），一律归为「训练/回测」，valid=False
    任一方日期缺失则保守视为真预测（避免误杀旧数据）。
    """
    pd = _parse_iso_date(pred_date_str)
    if not pd or not actual_date:
        return True
    return pd <= actual_date


def evaluate_pending_predictions(lottery_name: str) -> dict:
    """
    检查所有 pending 预测，对比已开奖结果，记录命中

    流程：
    1. 遍历所有 pending 预测
    2. 查找对应期号的实际开奖
    3. 对比命中，记录到反馈历史
    4. 标记 pending 为 evaluated
    5. 基于最新反馈更新策略权重

    返回：
    {
        "evaluated_count": 评估的预测数量,
        "new_feedback_count": 新增的反馈记录数,
        "still_pending": 仍未开奖的预测数,
        "updated_weights": 更新后的策略权重
    }
    """
    pending = load_pending(lottery_name)
    if not pending:
        logger.info(f"无 pending 预测需要评估: {lottery_name}")
        return {
            "evaluated_count": 0,
            "new_feedback_count": 0,
            "still_pending": 0,
            "updated_weights": load_strategy_weights(lottery_name),
        }

    history = load_feedback_history(lottery_name)

    # 去重护栏：同(档位,期号,策略,预测号码)只允许一条反馈。
    # 实证：2026-08-11 竞态曾把同一预测重复写入 13 次（26092 期 235 条），
    # 污染命中率统计（257 条实为 77 条唯一预测）。
    # 档位化后不同档位可能生成完全相同的票面（如一般档与稳健档撞号），
    # 去重键必须带档位，否则后结算的档位会被误判重复而丢账。
    def _fb_key(fb):
        if "红球命中" in fb:
            return (_tier_of(fb), "RB", fb.get("期号"), fb.get("策略"),
                    tuple(fb.get("预测红球") or []), tuple(fb.get("预测蓝球") or []))
        return (_tier_of(fb), "NUM", fb.get("期号"), fb.get("策略"),
                json.dumps(fb.get("预测号码"), ensure_ascii=False, sort_keys=True))

    seen_keys = {_fb_key(h) for h in history}

    new_feedback_count = 0
    new_feedbacks = []
    still_pending = 0
    still_pending_list = []

    for pred in pending:
        if pred.get("状态") != "pending":
            still_pending_list.append(pred)
            continue

        # 查找对应开奖：优先按期号主键，日期做 fallback
        target_issue = pred.get("目标期号")
        actual = _find_draw_by_issue(lottery_name, target_issue)
        if actual is None:
            pred_date = pred.get("预测日期", "")
            actual = _find_draw_by_date(lottery_name, pred_date)

        if actual is None:
            # 还没开奖（或期号/日期都查不到），保留 pending
            # 防御：离预测日期已超过 14 天仍查不到，标记为 missed 避免永久堆积
            pred_date = pred.get("预测日期", "")
            try:
                from datetime import date as _date
                if pred_date:
                    pd = _date.fromisoformat(pred_date)
                    if (_date.today() - pd).days > 14:
                        pred["状态"] = "missed"
                        pred["评估时间"] = datetime.now().isoformat()
                        logger.warning(f"预测 {pred_date} 超过14天未匹配到开奖，标记 missed")
                        still_pending_list.append(pred)
                        continue
            except Exception:
                pass
            still_pending += 1
            still_pending_list.append(pred)
            continue

        # 判定真预测/马后炮：预测日期必须不晚于开奖日期
        is_valid = _is_valid_prediction(pred.get("预测日期", ""), actual.开奖日期)
        # 来源=「训练/train」的一律不算真预测（此前仅按日期判定，马后炮训练样本混入 valid 口径）
        if pred.get("来源") in ("train", "训练"):
            is_valid = False

        # 对比命中（分区感知：红/蓝彩种走官方奖级，新彩种走通用命中率奖级）
        schema = schema_from_cfg(LOTTERY_CONFIG[lottery_name], lottery_name)
        pred_sets = pred.get("预测号码", []) or []
        # 批次信息：一次出号 = 一条 pending = 一个批次，组内所有注共用
        # （旧 pending 无批次号时留空，查询侧 ev/batch.py 按评估时间回推）
        _batch_common = {
            "批次号": pred.get("批次号", ""),
            "批次时间": pred.get("生成时间") or pred.get("记录时间") or "",
            "批次规模": len(pred_sets),
        }
        # 出号质量压缩 KPI：批次规模是压缩后的登记口径，反馈里补记压缩前原始注数
        # （/hits 右侧徽章显示"本批 M 注（原 P 注）"用；旧记录无此键自然缺省）
        _compress_kpi = pred.get("压缩") or {}
        if _compress_kpi.get("压缩前注数"):
            _batch_common["压缩前注数"] = _compress_kpi["压缩前注数"]
        for _bseq, ps in enumerate(pred_sets, start=1):
            zone_hits, total_hits, total_choose, is_redblue = _hit_detail(
                lottery_name, ps, actual, schema
            )

            if is_redblue:
                red_hits = zone_hits.get("红球", 0)
                blue_hits = zone_hits.get("蓝球", 0)
                pred_red = ps.get("红球", []) or ps.get("号码", {}).get("红球", [])
                pred_blue = ps.get("蓝球", []) or ps.get("号码", {}).get("蓝球", [])
                # 号码个数必须符合 schema（双色球蓝1红6、大乐透后区2前区5）。
                # 实证：26090 污染记录预测蓝球=[1,2] 违反 blue_count=1 却被放行，产出假"二等"。
                # 非法票根本不是一张合法彩票 → 不判任何奖级（否则战绩查询会显示 1/110 万级的假高奖级）。
                _illegal = False
                try:
                    if len(pred_blue) != schema.blue_zone.choose or len(pred_red) != schema.red_zone.choose:
                        _illegal = True
                except Exception:
                    pass
                if _illegal:
                    is_valid = False
                _grade = ("未中" if _illegal else
                          (_classify_dlt_prize(red_hits, blue_hits) if lottery_name == "大乐透"
                           else _classify_prize(red_hits, blue_hits)))
                feedback = {
                    "期号": actual.期号,
                    "开奖日期": str(actual.开奖日期) if actual.开奖日期 else pred.get("预测日期", ""),
                    "预测日期": pred.get("预测日期", ""),
                    "来源": pred.get("来源", "predict"),
                    "档位": _tier_of(pred),
                    "策略": ps.get("策略", ""),
                    # 出号时的置信度评分：入账留存，供事后 lift 检验（推荐分分档 vs 实际命中）
                    "置信度": ps.get("置信度"),
                    "预测红球": pred_red,
                    "预测蓝球": pred_blue,
                    "实际红球": actual.红球,
                    "实际蓝球": actual.蓝球,
                    "红球命中": red_hits,
                    "蓝球命中": blue_hits,
                    "总命中": red_hits + blue_hits,
                    "红球差集": sorted(set(pred_red) - set(actual.红球)),
                    "蓝球差集": sorted(set(pred_blue) - set(actual.蓝球)),
                    # 按彩种分发：双色球六等级 / 大乐透2023九级
                    # （此前大乐透误用双色球规则：5+0判四等、3+0/1+2/0+2漏判、0+1/1+1虚报六等）
                    "中奖等级": _grade,
                    "valid_prediction": is_valid,
                    "记录类型": "预测" if is_valid else "训练",
                    "评估时间": datetime.now().isoformat(),
                    **_batch_common,
                    "批次内序号": _bseq,
                }
                if _illegal:
                    feedback["备注"] = "预测号码非法（红/蓝球个数不符 schema），不判奖级"
            else:
                feedback = {
                    "期号": actual.期号,
                    "开奖日期": str(actual.开奖日期) if actual.开奖日期 else pred.get("预测日期", ""),
                    "预测日期": pred.get("预测日期", ""),
                    "来源": pred.get("来源", "predict"),
                    "档位": _tier_of(pred),
                    "策略": ps.get("策略", ""),
                    # 出号时的置信度评分：入账留存，供事后 lift 检验（推荐分分档 vs 实际命中）
                    "置信度": ps.get("置信度"),
                    "预测号码": ps.get("号码", {}),
                    "实际号码": getattr(actual, "zone_numbers", {}) or {},
                    "分区命中": zone_hits,
                    "总命中": total_hits,
                    "总选择": total_choose,
                    # ⚠️ 数字型不使用通用比例分级：
                    # 「一等~六等奖」是乐透型(双色球/大乐透)的概念，数字型官方根本没有这套奖级。
                    # 数字型官方命名：3D/排列3 = 直选·组选3·组选6·未中；排列5 = 直选·未中；
                    # 七星彩 = 一~六等奖(它是唯一按位数分级的数字型)。
                    # 此处先置"未中"，紧接着由 _settle_pl 按官方规则覆盖。
                    "中奖等级": "未中",
                    "valid_prediction": is_valid,
                    "记录类型": "预测" if is_valid else "训练",
                    "评估时间": datetime.now().isoformat(),
                    **_batch_common,
                    "批次内序号": _bseq,
                }
                # 任务1.3：模拟盈亏 P/L（数字型按真实奖级结算；双/大回退命中率）
                pl, settle, level = _settle_pl(lottery_name, feedback)
                feedback["模拟盈亏"] = pl
                feedback["结算方式"] = settle
                # 真实中奖玩法（直选/组选3/组选6/一等奖~六等奖）；覆盖通用命中率奖级
                if level is not None:
                    feedback["中奖玩法"] = level
                    feedback["中奖等级"] = level
                else:
                    # 未中奖：清掉通用比例分级（如命中1位被标"五等"）以免误导
                    feedback["中奖等级"] = "未中"
                    feedback.pop("中奖玩法", None)
            _k = _fb_key(feedback)
            if _k in seen_keys:
                # 重复(期号,策略,预测号码)不再入账，仅消费 pending
                logger.info(f"跳过重复反馈: {lottery_name} 期{feedback.get('期号')} 策略{feedback.get('策略')}")
            else:
                seen_keys.add(_k)
                history.append(feedback)
                new_feedbacks.append(feedback)
                new_feedback_count += 1

        # 标记为已评估
        pred["状态"] = "evaluated"
        pred["评估时间"] = datetime.now().isoformat()

    # 保存
    save_pending(lottery_name, still_pending_list)
    save_feedback_history(lottery_name, history)

    # 更新策略权重
    updated_weights = update_strategy_weights(lottery_name)

    # 动态出号数量策略（2026-09-14）：评估入账后按「压缩冗余度 + 命中率安全阀」
    # 自适应下一期注数。仅当处于"动态"模式时生效；固定模式/异常时静默跳过。
    try:
        _dynamic = _apply_dynamic_ticket_size(lottery_name, new_feedbacks)
        if _dynamic:
            result_dynamic = _dynamic
        else:
            result_dynamic = None
    except Exception as e:  # pragma: no cover - 策略失败不影响评估主流程
        logger.warning(f"动态出号数量策略失败 {lottery_name}: {e}")
        result_dynamic = None

    result = {
        "evaluated_count": len(pending) - len(still_pending_list),
        "new_feedback_count": new_feedback_count,
        "new_feedback_records": new_feedbacks,
        "still_pending": len(still_pending_list),
        "updated_weights": updated_weights,
        "动态出号数量": result_dynamic,
    }

    logger.info(
        f"评估完成: {lottery_name}, "
        f"评估 {result['evaluated_count']} 条预测, "
        f"新增 {new_feedback_count} 条反馈, "
        f"仍 pending {result['still_pending']} 条"
    )

    return result


def _last_compress_kpi(lottery_name: str) -> Optional[Dict[str, Any]]:
    """取最近一次已评估批次的质量压缩 KPI（动态策略的冗余度信号源）。

    来源优先级：反馈历史里的「压缩」KPI（若写入）→ 命中记录汇总的淘汰明细。
    找不到返回 None（策略将维持现注数）。
    """
    history = load_feedback_history(lottery_name)
    if not history:
        return None
    # 从最近记录里找带压缩明细的
    for fb in reversed(history[-500:]):
        k = fb.get("压缩")
        if isinstance(k, dict) and k.get("启用"):
            return k
    # 退化：由「压缩前注数」与批次规模推算出淘汰总量
    for fb in reversed(history[-500:]):
        before = fb.get("压缩前注数")
        if before:
            try:
                before = int(before)
            except (TypeError, ValueError):
                continue
            after = int(fb.get("批次规模") or 0)
            if before > 0:
                return {"启用": True, "压缩前注数": before,
                        "压缩后注数": after, "质量淘汰": before - after,
                        "重复淘汰": 0, "重叠淘汰": 0}
    return None


def _recent_hit_stats(lottery_name: str, lookback: int = 500) -> Dict[str, Any]:
    """近期命中统计：(命中注数, 总注数, 命中率)。用于动态策略的偏离安全阀。"""
    history = load_feedback_history(lottery_name)
    recent = history[-lookback:] if history else []
    n = len(recent)
    if n == 0:
        return {"hit_n": 0, "hit_rate": None}
    hits = 0
    for fb in recent:
        lvl = fb.get("中奖等级")
        if lvl and lvl != "未中":
            hits += 1
    return {"hit_n": n, "hit_rate": hits / float(n)}


def _expected_win_rate(lottery_name: str) -> Optional[float]:
    """该彩种"至少中任意奖级"的理论概率（i.i.d. 基线），用于偏离判定。"""
    try:
        from ev.payout import grade_probabilities
        probs = grade_probabilities(lottery_name)
        if not probs:
            return None
        # 任一奖级命中概率（奖级互斥时求和；含重复键则取并集近似）
        p = sum(float(v) for v in probs.values())
        return max(0.0, min(1.0, p))
    except Exception:
        return None


def _apply_dynamic_ticket_size(lottery_name: str,
                               new_feedbacks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """动态模式：评估后自适应下一期出号注数。返回决策 dict 或 None（未启用）。"""
    from data import ticket_size as ts

    cfg = ts.sanitize(ts.load_config())
    if cfg.get("mode") != ts.MODE_DYNAMIC:
        return None

    cur = cfg["dynamic"].get(lottery_name) or ts.default_count(lottery_name)
    stats = _recent_hit_stats(lottery_name)
    kpi = _last_compress_kpi(lottery_name)
    exp = _expected_win_rate(lottery_name)

    decision = ts.decide_next_count(
        lottery_name,
        current=cur,
        last_kpi=kpi,
        hit_rate=stats.get("hit_rate"),
        expected_rate=exp,
        hit_n=stats.get("hit_n"),
    )
    if decision["next"] != int(cur):
        ts.apply_dynamic_result(lottery_name, decision["next"], decision["reason"])
        logger.info(f"动态出号数量 {lottery_name}: {cur} → {decision['next']}（{decision['reason']}）")
    else:
        ts.apply_dynamic_result(lottery_name, decision["next"], decision["reason"])
    return {"上一期注数": int(cur), **decision}


def _gate_survived_strategies(lottery_name: str):
    """读取 P0 闸门（假设登记册）判定，返回「通过闸门的策略名」集合。

    返回 None  = 该彩种闸门从未跑过（无任何 tested 假设）→ 调用方保持原逻辑，不引入回归。
    返回 set   = 闸门已跑过；空 set 表示「无一条假设通过」（= 无 proven edge）。

    判定来源：discovery/hypothesis_registry.py 登记的假设，status == "survived" 者
    其 probe.strategy 即被认为有统计意义上的 edge。
    """
    try:
        from discovery.hypothesis_registry import load_hypotheses
        hyps = load_hypotheses(lottery_name)
    except Exception as e:
        logger.debug(f"闸门判定暂不可用({lottery_name}): {e}")
        return None
    if not hyps:
        return None
    if not any(h.get("status") in ("survived", "rejected") for h in hyps):
        return None  # 从未检验 → 无判定可用
    survived = set()
    for h in hyps:
        if h.get("status") == "survived":
            s = (h.get("probe") or {}).get("strategy")
            if s:
                survived.add(normalize_strategy_name(s))
    return survived


def update_strategy_weights(lottery_name: str, lookback: int = None,
                            respect_gate: bool = True) -> Dict[str, float]:
    """
    基于全部历史反馈，调整各策略权重

    评分公式：score = 红球命中 × 0.6 + 蓝球命中 × 0.4
    权重 = score / sum(all_scores)

    自适应策略列表：不写死三个默认策略，统计所有出现过的策略
    （涵盖 统计训练(Wxxx) / ML(logistic) / 规则优选 等）

    注意：lookback 默认 None（全量），不再截断历史，避免历史记录在
    权重计算中"被丢弃"。如需只参考近期，可显式传 lookback。
    """
    history = load_feedback_history(lottery_name, lookback)

    if not history:
        # 无历史数据，返回均分默认策略
        weights = {s: 1.0 / len(DEFAULT_STRATEGIES) for s in DEFAULT_STRATEGIES}
        save_strategy_weights(lottery_name, weights)
        return weights

    # 按策略分组计算平均得分（仅真预测，马后炮训练样本不参与权重）
    # 归一化：ML 家族（"ML(logistic)" / "统计训练(W50)" / "ML策略" 等）统一归并到 "ML策略"，
    # 让训练模型作为一个整体参与权重分配，而非散落成互不相关的标签。
    # 任务1.3：数字型评分 = 模拟盈亏 P/L（目标函数从命中率改盈亏）；
    #          双/大（红蓝）保留命中率评分（无固定赔付表）。
    strategy_scores = defaultdict(list)
    for f in history:
        if not f.get("valid_prediction", True):
            continue
        # 脏数据防御：缺策略名的记录无法归组，跳过（曾因占位记录缺「策略」直接 KeyError）
        if not f.get("策略"):
            continue
        # 档位隔离：策略权重学习仅由一般档驱动。
        # 稳健/高鲁棒档是不同生成流程（共识集成），混学只会互相稀释权重；
        # 三档的命中对比见 tier_comparison()。
        if _tier_of(f) != TIER_GENERAL:
            continue
        cname = normalize_strategy_name(f["策略"])
        if "红球命中" in f:
            # 双色球/大乐透：红蓝加权得分（回退命中率逻辑）
            score = f["红球命中"] * 0.6 + f["蓝球命中"] * 0.4
        else:
            # 数字型：优先模拟盈亏（P/L）；旧记录无该字段 → 命中率兜底
            pl = f.get("模拟盈亏")
            if pl is not None:
                score = float(pl)
            else:
                total_choose = f.get("总选择") or 1
                score = (f.get("总命中", 0) / total_choose) if total_choose else 0.0
        strategy_scores[cname].append(score)

    # 无有效策略记录时回退均分
    if not strategy_scores:
        weights = {s: 1.0 / len(DEFAULT_STRATEGIES) for s in DEFAULT_STRATEGIES}
        save_strategy_weights(lottery_name, weights)
        return weights

    # 计算各策略权重（任务1.3 新规则）：
    # ① 样本 <10 → 回归均匀（不信任小样本）
    # ② 平均 P/L < 0 → 权重 ×0.5（惩罚持续亏损策略）
    # ③ 归一化后保底 5%（防清零后无法复出）
    n_strats = len(strategy_scores)
    raw_weights = {}
    for name, scores in strategy_scores.items():
        if len(scores) < 10:
            raw_weights[name] = 1.0 / n_strats
        else:
            m = float(np.mean(scores))
            if m < 0:
                m *= 0.5
            raw_weights[name] = max(m, 1e-6)

    # ---- 闸门联动（P0 假设登记册）----
    # 问题：已实现 P/L 在固定赔率彩种上方差极大（中一次 +1038 / 不中 -2），
    # 单条幸运命中（甚至旧 bug 的假阳性命中）就能把某策略权重顶到 0.85（排列3 实踩）。
    # 规则：闸门判定「无 proven edge」的策略，不允许凭已实现 P/L 占据优势权重。
    #   - 无任何策略通过闸门 → 全部均分（无信息状态，运气带不偏权重）
    #   - 有通过者 → 胜者按 P/L 分走 GATE_WINNER_MASS，其余均分剩余质量
    #   - 闸门从未跑过（None）→ 保持原逻辑，避免回归
    if respect_gate:
        gate = _gate_survived_strategies(lottery_name)
        if gate is not None:
            winners = [s for s in raw_weights if s in gate]
            if not winners:
                n_all = len(raw_weights)
                raw_weights = {k: 1.0 / n_all for k in raw_weights}
                logger.info(f"[闸门联动] {lottery_name} 无策略通过闸门 → 权重回归均分(1/{n_all})，"
                            f"已实现P/L不参与区分")
            else:
                win_raw = {w: raw_weights[w] for w in winners}
                wsum = sum(win_raw.values())
                if wsum <= 1e-6:
                    for w in winners:
                        raw_weights[w] = GATE_WINNER_MASS / len(winners)
                else:
                    for w in winners:
                        raw_weights[w] = GATE_WINNER_MASS * win_raw[w] / wsum
                losers = [s for s in raw_weights if s not in gate]
                if losers:
                    for s in losers:
                        raw_weights[s] = (1.0 - GATE_WINNER_MASS) / len(losers)
                logger.info(f"[闸门联动] {lottery_name} 通过闸门的策略={winners}，"
                            f"未通过者({len(losers)}个)均分剩余权重")

    total = sum(raw_weights.values()) + 1e-8
    # ③ 保底：每策略权重 ≥ floor（默认 5%），总和=1，保持 raw 相对比例
    n = len(raw_weights)
    floor = 0.05 if n <= 10 else 0.5 / n
    weights = {k: floor + (1.0 - n * floor) * v / total for k, v in raw_weights.items()}

    save_strategy_weights(lottery_name, weights)
    logger.info(f"策略权重已更新: {lottery_name}, weights={weights}")
    return weights


# ============================================================
# 反馈统计
# ============================================================

def get_feedback_summary(lottery_name: str, lookback: int = None) -> dict:
    """获取反馈历史摘要统计（默认全量，不截断历史）"""
    history = load_feedback_history(lottery_name, lookback)
    pending_count = len(load_pending(lottery_name))

    if not history:
        return {
            "total_records": 0,
            "pending_count": pending_count,
            "strategies": {},
            "weights": normalize_weights(load_strategy_weights(lottery_name)),
        }

    valid_history = [f for f in history if f.get("valid_prediction", True)]
    strategy_stats = {}
    # 自适应：统计历史中所有出现过的策略（仅真预测，不写死默认三策略）
    # ML 家族（ML(logistic)/统计训练(W50) 等）统一归并为 "ML策略"，避免散落成多行
    all_strategies = list(dict.fromkeys(
        normalize_strategy_name(f["策略"]) for f in valid_history if f.get("策略")
    ))
    for name in all_strategies:
        strat_records = [f for f in valid_history if normalize_strategy_name(f["策略"]) == name]
        if not strat_records:
            strategy_stats[name] = {"样本数": 0}
            continue

        # 命中统计兼容红/蓝彩种与新彩种（分区命中）
        if strat_records and "红球命中" in strat_records[0]:
            red_hits = [f["红球命中"] for f in strat_records]
            blue_hits = [f["蓝球命中"] for f in strat_records]
            hit_stats = {
                "平均红球命中": round(float(np.mean(red_hits)), 4),
                "平均蓝球命中": round(float(np.mean(blue_hits)), 4),
            }
        else:
            # 新彩种：平均命中率（总命中/总选择）+ 平均模拟盈亏（P/L，任务1.3）
            ratios = []
            pls = []
            for f in strat_records:
                tc = f.get("总选择") or 1
                ratios.append((f.get("总命中", 0) / tc) if tc else 0.0)
                pl = f.get("模拟盈亏")
                if pl is not None:
                    pls.append(float(pl))
            hit_stats = {
                "平均命中率": round(float(np.mean(ratios)), 4) if ratios else 0.0,
            }
            if pls:
                hit_stats["平均模拟盈亏"] = round(float(np.mean(pls)), 2)
                hit_stats["累计模拟盈亏"] = round(float(np.sum(pls)), 2)
        prize_won = sum(1 for f in strat_records if f["中奖等级"] != "未中")

        # 中奖等级分布
        prize_grades = defaultdict(int)
        for f in strat_records:
            prize_grades[f["中奖等级"]] += 1

        strategy_stats[name] = {
            "样本数": len(strat_records),
            **hit_stats,
            "中奖次数": prize_won,
            "中奖率": round(prize_won / len(strat_records), 4),
            "中奖等级分布": dict(prize_grades),
        }

    return {
        "total_records": len(history),
        "valid_records": len(valid_history),
        "lookback": lookback,
        "strategies": strategy_stats,
        "weights": normalize_weights(load_strategy_weights(lottery_name)),
        "pending_count": len(load_pending(lottery_name)),
    }


def tier_comparison(lottery_name: str, lookback: Optional[int] = None) -> dict:
    """
    三档命中对比（一般 / 稳健 / 高鲁棒）。

    按档位聚合历史反馈（仅真预测），产出各档：
    预测注数 / 中奖次数 / 中奖率 / 平均命中得分（红蓝加权 or 命中率）/ 平均模拟盈亏。

    诚实口径：对比结果描述「各选号流程的历史表现记录」，样本量小、号码随机，
    不能推断某档未来更可能中奖。
    """
    history = load_feedback_history(lottery_name, lookback)
    tiers = {t: {"预测注数": 0, "中奖次数": 0, "scores": [], "pls": []}
             for t in VALID_TIERS}
    issues_by_tier = {t: set() for t in VALID_TIERS}

    for f in history:
        if not f.get("valid_prediction", True):
            continue
        t = _tier_of(f)
        stat = tiers[t]
        stat["预测注数"] += 1
        if f.get("期号") is not None:
            issues_by_tier[t].add(f["期号"])
        if f.get("中奖等级", "未中") != "未中":
            stat["中奖次数"] += 1
        if "红球命中" in f:
            stat["scores"].append(f.get("红球命中", 0) * 0.6 + f.get("蓝球命中", 0) * 0.4)
        else:
            tc = f.get("总选择") or 1
            stat["scores"].append((f.get("总命中", 0) / tc) if tc else 0.0)
        pl = f.get("模拟盈亏")
        if pl is not None:
            stat["pls"].append(float(pl))

    out = {}
    for t in VALID_TIERS:
        stat = tiers[t]
        n = stat["预测注数"]
        entry = {
            "预测注数": n,
            "覆盖期数": len(issues_by_tier[t]),
            "中奖次数": stat["中奖次数"],
            "中奖率": round(stat["中奖次数"] / n, 4) if n else 0.0,
            "平均命中得分": round(float(np.mean(stat["scores"])), 4) if stat["scores"] else None,
            "平均模拟盈亏": round(float(np.mean(stat["pls"])), 2) if stat["pls"] else None,
        }
        out[t] = entry
    return {"lottery_name": lottery_name, "lookback": lookback, "tiers": out,
            "说明": "对比描述各选号流程的历史表现记录，不代表未来中奖概率。"}
