"""
诚实看板 Web 路由（P3：开奖质量监控 + EV 看板 + P/L 反馈）

- GET /honest                         诚实看板页面
- GET /api/honest/quality/<lottery>   开奖质量监控（数据质量评分+异常分区）
- GET /api/honest/ev/<lottery>        EV 看板（名义EV表 + 限号/撞号修正 + 冷门TOP）
- GET /api/honest/rollover/<lottery>  双/大 Rollover-EV（参与信号）
- GET /api/honest/pl/<lottery>        P/L 反馈摘要（命中率 | 模拟盈亏 | 权重）
- GET /api/honest/distance/<lottery>  距离画像（观测 vs 随机基线）
- GET /api/honest/payout_profile/<lottery> 实得画像（流行度LRT+反事实回测，NetPayout WP4）
- GET /api/honest/trials/<lottery>          A/B 对照进度 + 人群漂移裁决（NetPayout WP5）
- GET /api/honest/attribution/<lottery>    中奖归因（来源×策略×档位 + 最新批次撞号覆盖）
- GET /api/honest/attribution_global       六彩种归因汇总

定位：只呈现「诚实指标」（可证伪的度量），不输出任何"中奖承诺"。
"""

from flask import Blueprint, jsonify, render_template, request

from web.utils import api_error_handler
from web.validators import validate_lottery

bp = Blueprint("honest", __name__)

# 彩种分组
_DIGITAL = ["排列5", "福彩3D", "排列3", "七星彩"]
_LOTTERY_STYLE = ["双色球", "大乐透"]
_ALL = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


@bp.route("/honest")
def page():
    return render_template("honest.html", all_lotteries=_ALL)


@bp.route("/api/honest/quality/<lottery>")
@api_error_handler
@validate_lottery
def api_quality(lottery):
    """开奖质量监控：数据质量评分 + 异常分区（第③支柱 data_quality_report）"""
    from credibility.data_quality import data_quality_report
    r = data_quality_report(lottery)
    return jsonify(r)


@bp.route("/api/honest/ev/<lottery>")
@api_error_handler
@validate_lottery
def api_ev(lottery):
    """EV 看板：名义EV + 限号/撞号修正 + 冷门TOP（数字型）"""
    from ev.rulebook import nominal_ev
    from ev.engine import net_ev, scan_issues
    from ev.sampler import sample_tickets

    if lottery not in _DIGITAL:
        return jsonify({"error": f"{lottery} 为彩票式（双/大），用 /api/honest/rollover 查看 Rollover-EV"})

    # 名义EV 表（各玩法）
    nom = nominal_ev(lottery)
    ev_rows = nom["ev_per_ticket"]
    if isinstance(ev_rows, dict):
        ev_table = [{"玩法": k, "名义EV": v} for k, v in ev_rows.items()]
    else:
        ev_table = [{"玩法": "单票", "名义EV": ev_rows}]

    # 冷门 TOP5（限号EV 最高）——七星彩=撞号修正后最高；固定赔率=EV恒定（展示冷门拥挤度）
    cold = []
    try:
        r = scan_issues(lottery, top_n=5)
        cold = r.get("TOP冷门(限号EV最高)", [])
    except Exception as e:
        cold = [{"error": str(e)}]

    # 约束采样 3 组示例（反大众）
    sample = []
    try:
        s = sample_tickets(lottery, n=3)
        sample = [{"号码": t["号码"], "拥挤分位": t["拥挤分位"], "和值": t["和值"]} for t in s["号码组"]]
    except Exception as e:
        sample = [{"error": str(e)}]

    mechanism = ("七星彩：浮动头奖撞号分薄 → 冷门号EV>热门号EV"
                 if lottery == "七星彩" else
                 "固定赔率：单注EV恒定（限号=停售+滚动放号），选号不改变单注期望")
    return jsonify({
        "彩种": lottery,
        "名义EV表": ev_table,
        "冷门TOP": cold,
        "约束采样": sample,
        "机制": mechanism,
    })


@bp.route("/api/honest/rollover/<lottery>")
@api_error_handler
@validate_lottery
def api_rollover(lottery):
    """双/大 Rollover-EV（参与信号）"""
    from ev.rollover import rollover_ev, rollover_history
    if lottery not in _LOTTERY_STYLE:
        return jsonify({"error": f"{lottery} 非彩票式，无 Rollover 概念（用 /api/honest/ev）"})
    cur = rollover_ev(lottery)
    hist = rollover_history(lottery, n=10)
    cur["趋势(近10期)"] = hist
    return jsonify(cur)


@bp.route("/api/honest/pl/<lottery>")
@api_error_handler
@validate_lottery
def api_pl(lottery):
    """P/L 反馈摘要：命中率 | 模拟盈亏 | 权重（任务1.3）"""
    from data.feedback import get_feedback_summary
    from data.schema import is_redblue
    s = get_feedback_summary(lottery, lookback=None)
    s["is_redblue"] = is_redblue(lottery)
    # 只保留前端需要的字段
    return jsonify(s)


@bp.route("/api/honest/distance/<lottery>")
@api_error_handler
@validate_lottery
def api_distance(lottery):
    """距离画像：预测「差多远」的分布 vs 随机基线（可证伪的近似命中度量）"""
    from ev.distance_metrics import distance_stats
    return jsonify(distance_stats(lottery))


@bp.route("/api/honest/trials/<lottery>")
@api_error_handler
@validate_lottery
def api_trials(lottery):
    """
    A/B 对照实验 + 漂移监控（NetPayout WP5）：
      - 前瞻配对对照的当前进度（主终点可完成的；次终点仅存档）
      - 人群行为漂移裁决（维护信号，不是"变强"信号）
    """
    from ev.popularity import drift_report
    from ev.trials import trial_report

    return jsonify({
        "彩种": lottery,
        "定位": ("前瞻对照 = 验证「选号不改变中奖概率」（等价性检验，可完成）；"
                 "漂移监控 = 保证结论不过时（维护，不提高收益）"),
        "对照实验": trial_report(lottery),
        "漂移监控": drift_report(lottery),
    })


@bp.route("/api/honest/payout_profile/<lottery>")
@api_error_handler
@validate_lottery
def api_payout_profile(lottery):
    """
    实得画像（NetPayout WP4）：组合流行度检验（LRT+CV）+ 历史反事实回测。
    与「距离画像」对称：距离画像证明"不会更常中"，实得画像回答"中了能拿多少"。
    """
    from ev.popularity import get_popularity_model, counterfactual_backtest

    # 走两级缓存（进程内/磁盘 cache/）：fit_popularity 直调会在每次重启后重跑 12 次 GLM
    params = get_popularity_model(lottery)
    fit = params.get("report", {})
    cf = counterfactual_backtest(lottery) if params.get("go") else {
        "go": False, "note": "流行度模型未达 Go 门槛，反事实回测不具解释力（诚实否决）",
    }
    return jsonify({
        "彩种": lottery,
        "定位": ("实得奖金侧分析：不改变中奖概率，只回答「同样概率下，冷门组合中了能拿更多吗」"),
        "流行度检验": fit,
        "反事实回测": cf,
    })


@bp.route("/api/honest/attribution/<lottery>")
@api_error_handler
@validate_lottery
def api_attribution(lottery):
    """
    中奖归因：按 (来源 × 策略 × 档位) 聚合历史已开奖反馈，附最新 pending 批次的撞号覆盖 KPI。

    溯源事实：预测生成时即写入 pending（预先锁定），开奖后只拿当时存下的号码结算，
    **不会重新生成号码** → 每条中奖记录都能定位到"哪一批、哪个策略、哪个档位"。

    ?lookback=N 限制回看条数（默认 2000，防大数据量卡顿）。
    """
    from ev.attribution import attribution_for_lottery

    lb = request.args.get("lookback", type=int)
    return jsonify(attribution_for_lottery(lottery, **({} if lb is None else {"lookback": lb})))


@bp.route("/api/honest/attribution_global")
@api_error_handler
def api_attribution_global():
    """六彩种归因汇总：全局 (来源,策略,档位) 合并表 + 各彩种总计。"""
    from ev.attribution import attribution_global

    lb = request.args.get("lookback", type=int)
    return jsonify(attribution_global(**({} if lb is None else {"lookback": lb})))
