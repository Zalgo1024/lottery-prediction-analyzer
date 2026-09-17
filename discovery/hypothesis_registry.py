"""
discovery/hypothesis_registry.py —— 假设登记册

存储：training/hypotheses/{彩种}_hypotheses.json（list[hypothesis]）

一条假设是可证伪的 edge 声明，结构：
{
  "id": "H20260828-001",
  "name": "……",
  "lottery": "排列3",
  "proposed_by": "auto|human",
  "proposed_at": "……",
  "hypothesis": "可证伪的陈述",
  "probe": {
    "type": "credibility_gate|ev_edge",
    "strategy": "被检验的策略名",
    "target_metric": "目标指标（OOS均分/限号EV…）",
    "gate_criteria": {oos_vs_random, fdr_q_max, practically_min,
                      bayes_p_edge_min, allow_anomaly_overlap}
  },
  "status": "proposed|testing|survived|rejected|dormant",
  "promoted": false,
  "tests": [ {"tested_at","passed","sample_n","metrics","verdict"} ],
  "last_tested": null
}
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from config import TRAINING_DIR

logger = logging.getLogger(__name__)

HYP_DIR = TRAINING_DIR / "hypotheses"
HYP_DIR.mkdir(parents=True, exist_ok=True)

# 状态机
STATUSES = ("proposed", "testing", "survived", "rejected", "dormant")

# 默认闸门标准（四道闸，与裁判层结论一致：彩票零期望是默认先验）
DEFAULT_GATE = {
    "oos_vs_random": True,          # 必须 OOS 击败随机基线军团
    "fdr_q_max": 0.05,              # FDR 校正 q 阈值
    "practically_min": 0.02,        # 相对效应必须达实质量级（≥2%）
    "bayes_p_edge_min": 0.80,       # 贝叶斯后验 P(正edge) 阈值
    "allow_anomaly_overlap": False,  # 与数据异常分区重叠一律驳回（跨支柱揭穿）
}

# 并发写锁（同一彩种登记表）
_LOCK = threading.Lock()

# ---------------- 自动模板 ----------------
# 仅指向"可被裁判层/EV边"检验的声明；不猜号码、不承诺中奖。
# credibility_gate 型的 strategy 必须是裁判层在测的三策略之一。
_INTERNAL_STRATEGIES = ("高频策略", "遗漏值策略", "区间均衡策略")


def _cred_templates(lottery: str, hypotheses: dict) -> list:
    """为裁判层在测的三策略各生成一条『OOS 是否显著优于随机』假设。"""
    return [
        {
            "name": f"{s}OOS显著优于随机",
            "hypothesis": f"{lottery}的{s}在严格样本外滚动回测中显著优于随机基线军团（经FDR校正）",
            "strategy": s,
            "probe_type": "credibility_gate",
            "target_metric": "OOS均分",
        }
        for s in _INTERNAL_STRATEGIES
    ]


def _ev_templates(lottery: str) -> list:
    """数字型彩种：冷门号限号EV 是否持续高于热门号（真实边探测）。

    固定赔率彩种限号EV恒定 → 预期判『无持续边』，这正是正确结论；
    七星彩浮动头奖若 crowd 数据可用，才是真正能出差距的地方。
    """
    if lottery not in ("排列3", "福彩3D", "排列5", "七星彩"):
        return []
    return [
        {
            "name": "冷门号限号EV持续高于热门号",
            "hypothesis": f"{lottery}冷门号（低拥挤分位）限号EV在最近窗口中持续高于热门号，构成可重复选号边",
            "strategy": "冷门号vs热门号",
            "probe_type": "ev_edge",
            "target_metric": "限号EV差距",
        }
    ]


def auto_templates(lottery: str) -> list:
    """该彩种的全部候选模板（人工提假设时可参考；--auto 用）。"""
    hyp_by_strategy = {s: f"{lottery}的{s}在严格样本外滚动回测中显著优于随机基线军团（经FDR校正）"
                       for s in _INTERNAL_STRATEGIES}
    return _cred_templates(lottery, hyp_by_strategy) + _ev_templates(lottery)


# ---------------- 文件 IO ----------------
def _path(lottery: str) -> Path:
    return HYP_DIR / f"{lottery}_hypotheses.json"


def load_hypotheses(lottery: str) -> list:
    p = _path(lottery)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读取假设登记表失败: {p} -> {e}")
        return []


def save_hypotheses(lottery: str, hyps: list):
    p = _path(lottery)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hyps, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _new_id(lottery: str, hyps: list) -> str:
    today = datetime.now().strftime("%Y%m%d")
    seq = 1
    for h in hyps:
        hid = h.get("id", "")
        if hid.startswith(f"H{today}-"):
            try:
                seq = max(seq, int(hid.split("-")[-1]) + 1)
            except ValueError:
                pass
    return f"H{today}-{seq:03d}"


# ---------------- 增查改 ----------------
def add_hypothesis(lottery: str, name: str, hypothesis: str, strategy: str,
                   probe_type: str = "credibility_gate", target_metric: str = None,
                   gate_criteria: dict = None, proposed_by: str = "human") -> dict:
    """登记一条假设。返回完整假设记录。"""
    with _LOCK:
        hyps = load_hypotheses(lottery)
        hyp = {
            "id": _new_id(lottery, hyps),
            "name": name,
            "lottery": lottery,
            "proposed_by": proposed_by,
            "proposed_at": datetime.now().isoformat(timespec="seconds"),
            "hypothesis": hypothesis,
            "probe": {
                "type": probe_type,
                "strategy": strategy,
                "target_metric": target_metric,
                "gate_criteria": gate_criteria or dict(DEFAULT_GATE),
            },
            "status": "proposed",
            "promoted": False,
            "tests": [],
            "last_tested": None,
        }
        hyps.append(hyp)
        save_hypotheses(lottery, hyps)
        logger.info(f"登记假设 {hyp['id']}: {lottery}/{name}")
        return hyp


def add_auto_hypotheses(lottery: str) -> list:
    """从模板批量生成候选（按 name 去重，已存在则跳过）。返回实际新增的假设。"""
    with _LOCK:
        hyps = load_hypotheses(lottery)
        existing = {h.get("name") for h in hyps}
        added = []
        for t in auto_templates(lottery):
            if t["name"] in existing:
                continue
            hyp = {
                "id": _new_id(lottery, hyps),
                "name": t["name"],
                "lottery": lottery,
                "proposed_by": "auto",
                "proposed_at": datetime.now().isoformat(timespec="seconds"),
                "hypothesis": t["hypothesis"],
                "probe": {
                    "type": t["probe_type"],
                    "strategy": t["strategy"],
                    "target_metric": t.get("target_metric"),
                    "gate_criteria": dict(DEFAULT_GATE),
                },
                "status": "proposed",
                "promoted": False,
                "tests": [],
                "last_tested": None,
            }
            hyps.append(hyp)
            added.append(hyp)
        if added:
            save_hypotheses(lottery, hyps)
        return added


def get_hypothesis(lottery: str, hid: str) -> dict:
    for h in load_hypotheses(lottery):
        if h.get("id") == hid:
            return h
    return None


def list_hypotheses(lottery: str) -> list:
    return load_hypotheses(lottery)


def update_hypothesis(lottery: str, hid: str, **fields) -> dict:
    """更新假设字段（status/promoted/tests/last_tested 等）。返回更新后的记录。"""
    with _LOCK:
        hyps = load_hypotheses(lottery)
        for h in hyps:
            if h.get("id") == hid:
                h.update(fields)
                save_hypotheses(lottery, hyps)
                return h
    return None


def record_test(lottery: str, hid: str, passed: bool, metrics: dict,
                verdict: str, reasons: list, sample_n=None) -> dict:
    """记录一次闸门判卷结果并更新状态。"""
    status = "survived" if passed else "rejected"
    test = {
        "tested_at": datetime.now().isoformat(timespec="seconds"),
        "passed": bool(passed),
        "sample_n": sample_n,
        "metrics": metrics or {},
        "reasons": reasons or [],
        "verdict": verdict,
    }
    with _LOCK:
        hyps = load_hypotheses(lottery)
        for h in hyps:
            if h.get("id") == hid:
                h.setdefault("tests", []).append(test)
                h["status"] = status
                h["promoted"] = bool(passed)
                h["last_tested"] = test["tested_at"]
                save_hypotheses(lottery, hyps)
                return h
    return None
