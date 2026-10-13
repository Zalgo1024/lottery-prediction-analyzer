"""
策略战绩榜 Web 路由

提供战绩榜页面和 API：
- GET  /records                           战绩榜页面
- GET  /api/records/<lottery>             战绩数据（实盘 + 回测）
- GET  /api/records/<lottery>/mechanism   机制缝隙状态卡（只读缓存，非战绩行）
- GET  /api/records/<lottery>/trials      机制缝隙·前瞻对照实验（只读 ev/trials 台账）
"""

from flask import Blueprint, jsonify, render_template
from web.utils import api_error_handler
from web.validators import validate_lottery
from data.records import get_records_summary

bp = Blueprint("records", __name__)


@bp.route("/records")
def page():
    return render_template("records.html")


@bp.route("/api/records/<lottery>")
@api_error_handler
@validate_lottery
def api_records(lottery):
    """战绩汇总：实盘反馈 + 滚动回测"""
    return jsonify(get_records_summary(lottery))


@bp.route("/api/records/<lottery>/mechanism")
@api_error_handler
@validate_lottery
def api_records_mechanism(lottery):
    """机制缝隙（四轨测量）**状态卡**：裁决 / R / ū / γ / 是否激活 / 测量时间。

    ⚠️ 它是状态卡，**不是战绩行**：γ=0（不可折算或 CI 含 0）时出号与基础策略
    逐位相同，没有独立战绩样本，故不得进 `strategies` 聚合、不参与任何统计。
    ⚠️ 只读缓存，绝不触发测量（完整测量约 17s/彩种）；缓存缺失 ⇒ `未测量=True`，
    前端照实显示「尚未测量」，不拿 0 或空表粉饰成「无缝隙」。
    """
    from ev.mechanism.snapshot import mechanism_status_card
    return jsonify(mechanism_status_card(lottery))


@bp.route("/api/records/<lottery>/trials")
@api_error_handler
@validate_lottery
def api_records_trials(lottery):
    """机制缝隙·前瞻对照实验：把 `ev/trials.py` 的 A/B 台账转成策略层可读的展示模型。

    A 组 = 按「一等奖实得」排序（冷门偏好）；B 组 = 同候选池随机（阴性对照）。
    两组来自**同一候选池、同一低重叠约束，唯一差异 = 有没有按冷门偏好排序**
    —— 这正是「机制缝隙」假设的可利用面（买得少的组合更值钱）的直接检验。

    ⚠️ 这也解释了为什么「机制倾斜 γ」不需要单独做对照：实测 γ 乘数
    `exp(-γ·ss)` 与 EV 排序键「一等奖实得」严格共线（Spearman ρ=−1），
    对选号恒等于空操作；真正能区分的是「排序 vs 不排序」，本实验测的就是它。

    ⚠️ **只读**：不生成、不入账、不触发测量；数据源 `training/trials/<彩种>.jsonl`。
    """
    from ev import trials as _T

    if lottery not in _T.TRIAL_LOTTERIES:
        return jsonify({
            "彩种": lottery, "参与": False,
            "说明": (f"{lottery} 不参与前瞻对照实验——固定赔率彩种没有奖金侧差异，"
                     f"构不成「排序有没有用」的对照。当前参与彩种："
                     f"{'、'.join(_T.TRIAL_LOTTERIES)}。"),
        })

    rep = _T.trial_report(lottery)
    n_issue = int(rep.get("已结算期数") or 0)
    per = int(rep.get("每组注数") or 0)
    denom = max(n_issue * per, 1)

    def _arm(name, hits, amount):
        return {
            "名": name,
            "中奖注数": hits,
            "中奖率": (float(hits) / denom) if hits is not None else None,
            "实得合计": amount,
        }

    return jsonify({
        "彩种": lottery,
        "参与": True,
        "每组注数": per,
        "总记录期数": rep.get("总记录期数", 0),
        "已结算期数": n_issue,
        "未开奖期数": rep.get("未开奖期数", 0),
        "臂": [
            _arm("A · 按实得排序（冷门偏好）", rep.get("A累计中奖注数"), rep.get("A累计实得")),
            _arm("B · 同池随机（阴性对照）", rep.get("B累计中奖注数"), rep.get("B累计实得")),
        ],
        "裁决": rep.get("裁决", ""),
        "等价性成立": rep.get("等价性成立"),
        "符号检验p": rep.get("符号检验p"),
        "功效": rep.get("功效规划", []),
        "次终点声明": rep.get("次终点声明", ""),
        "数据源": f"training/trials/{lottery}.jsonl（随自动流水线每期追加，开奖后结算）",
    })
