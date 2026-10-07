"""
discovery/hypothesis_registry.py —— 假设登记册

存储：training/hypotheses/{彩种}_hypotheses.json（list[hypothesis]）

一条假设是待检验的 edge 声明，结构：
{
  "id": "H20260828-001",
  "name": "……",
  "lottery": "排列3",
  "proposed_by": "auto|human",
  "proposed_at": "……",
  "hypothesis": "待检验的陈述",
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


# ---------------------------------------------------------------- 机制缝隙类
# 对应 ev/mechanism/ 四轨测量（python cli.py gap）。
# ★ 与「选号 edge」类假设是**两个不同的问题**：edge 类问「有没有策略能跑赢随机」，
#   缝隙类问「开奖结果本身是否偏离等可能」。后者即使检出，也不自动给出可利用的选号优势
#   （还取决于你能不能在别人之前买到那注）；所以它单独成类，不混进 _cred_templates。
#
# ★ `adjudicating` 标明该轨**是否参与机制裁决**。这是硬纪律，不是装饰：
#   C 轨（位级/分区结构偏差）已被证实对分段断裂敏感（大乐透 2007–2013 和值 97.2
#   vs 2014 起 88.5 是数据分段问题，非机制），D 轨（派奖期）样本量仅 21 期、功效极低
#   ⇒ 两者一律**不参与裁决**，只作诊断。注册它们是为了让「测过什么」可追溯，
#   不是为了把它们抬进判定。
_GAP_ADJUDICATING = {"A": True, "A′": True, "B": True, "C": False, "D": False}


def _gap_templates(lottery: str) -> list:
    """机制缝隙类假设模板（四轨测量的假设面）。

    按彩种可判定性给模板，不给齐——某轨在该彩种上不可判定却硬注册一条，
    等于承诺一个测不了的检验（排列3/福彩3D 的销售额覆盖全部玩法，A′ 轨量到的是
    投注份额而非开奖倾向，这就是它们的反证）。
    """
    out = []

    def _t(track, name, hyp, strategy, metric):
        out.append({
            "name": name,
            "hypothesis": hyp,
            "strategy": strategy,
            "probe_type": "mechanism_gap",
            "target_metric": metric,
            "track": track,
            "adjudicating": bool(_GAP_ADJUDICATING.get(track, False)),
        })

    if lottery in ("双色球", "大乐透"):
        # A 轨：免票量口径 R = (Σy1/Σy2)(p2/p1)，免疫追加/复式/多倍/记账差异
        _t("A", "高奖级注数比显著小于1",
           f"{lottery}一等奖实际中奖注数相对理论注数的比值 R 显著小于 1（A 轨，"
           f"免票量口径，长块 bootstrap 标准误）：即高奖级的开出密度低于等可能开奖的预期",
           "A轨注数比", "R")
        # B 轨：真实开出号的冷门度分位均值（分位用中点秩，避免并列正偏）
        _t("B", "开出号冷门度分位均值显著低于0.5",
           f"{lottery}真实开出组合在流行度模型下的分位均值 ū 显著低于 0.5（B 轨，"
           f"经验离散度 + 长块 bootstrap）：即开出的号系统性比随机组合更冷门",
           "B轨开出号分位", "ū")
    elif lottery in ("排列5", "七星彩"):
        # A′ 轨：数字型注数比。七星彩必须整段切现行段（≥2020-10-11），否则是假信号
        _t("A′", "高奖级注数比显著偏离1",
           f"{lottery}各奖级实际中奖注数相对理论注数的比值 R 显著偏离 1（A′ 轨，"
           f"直读销售奖级数据）：即中奖密度与等可能开奖的预期不符",
           "A′轨注数比", "R")

    # C 轨：位级/分区结构偏差 —— 诊断性，不参与裁决
    if lottery in ("排列3", "福彩3D", "排列5", "七星彩"):
        _t("C", "位级分区分布偏离均匀",
           f"{lottery}各位/分区的开出分布偏离均匀（C 轨，经 BH 多重比较校正）："
           f"★ 诊断性假设，不参与机制裁决——已证实该轨对规则变更导致的分段断裂敏感",
           "C轨结构偏差", "BH校正p")
    # D 轨：派奖期 —— 诊断性，样本量小、功效极低
    if lottery == "七星彩":
        _t("D", "派奖期冷门偏置强于平常期",
           f"{lottery}派奖期的冷门偏置显著强于平常期（D 轨）："
           f"★ 诊断性假设，不参与机制裁决——派奖期仅 21 期，可检出效应下限约 0.37，功效极低",
           "D轨派奖期", "ΔR")
    return out


def auto_templates(lottery: str) -> list:
    """该彩种的全部候选模板（人工提假设时可参考；--auto 用）。"""
    hyp_by_strategy = {s: f"{lottery}的{s}在严格样本外滚动回测中显著优于随机基线军团（经FDR校正）"
                       for s in _INTERNAL_STRATEGIES}
    return (_cred_templates(lottery, hyp_by_strategy)
            + _ev_templates(lottery)
            + _gap_templates(lottery))


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
                    # 机制缝隙类专用：标明所属轨 + 该轨是否参与裁决（C/D 恒 False）
                    **({"track": t["track"], "adjudicating": t["adjudicating"]}
                       if "track" in t else {}),
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
