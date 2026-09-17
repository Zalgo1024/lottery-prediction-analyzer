"""
反馈闭环 Web 路由（阶段4）

提供反馈数据查看和评估触发的 API：
- GET  /feedback                          反馈页面
- GET  /api/feedback/<lottery>/summary    反馈摘要（策略权重 + 各策略表现）
- POST /api/feedback/<lottery>/evaluate   触发开奖对比评估
- GET  /api/feedback/<lottery>/history    历史反馈记录列表
- GET  /api/feedback/<lottery>/pending    待开奖预测列表
"""

from flask import Blueprint, jsonify, render_template, request
from web.utils import api_error_handler, feedback_record_view
from web.validators import validate_lottery
from data.schema import is_redblue

bp = Blueprint("feedback", __name__)


@bp.route("/feedback")
def page():
    return render_template("feedback.html")


@bp.route("/api/feedback/<lottery>/summary")
@api_error_handler
@validate_lottery
def api_feedback_summary(lottery):
    """反馈摘要：策略权重 + 各策略表现 + pending 数量
    lookback 缺省或传 0 表示全量（不截断历史）"""
    from data.feedback import get_feedback_summary
    raw = request.args.get("lookback", 0, type=int)
    lookback = raw if raw and raw > 0 else None
    summary = get_feedback_summary(lottery, lookback=lookback)
    summary["lookback"] = lookback or 0
    summary["is_redblue"] = is_redblue(lottery)
    return jsonify(summary)


@bp.route("/api/feedback/<lottery>/evaluate", methods=["POST"])
@api_error_handler
@validate_lottery
def api_feedback_evaluate(lottery):
    """触发开奖对比评估（更新反馈历史 + 策略权重）"""
    from data.feedback import evaluate_pending_predictions
    result = evaluate_pending_predictions(lottery)
    return jsonify(result)


@bp.route("/api/feedback/<lottery>/history")
@api_error_handler
@validate_lottery
def api_feedback_history(lottery):
    """历史反馈记录列表（支持分页、筛选、排序）

    参数：
      limit:      查询上限，最多返回条数（默认 0=不限；可选 5/10/20/50/100/200/500）
      min_prize:  最小中奖等级（默认"全部"，可填 一等|二等|三等|四等|五等|六等）
      red_hits:   最小红球命中数（仅对双色球/大乐透生效）
      blue_hits:  最小蓝球命中数（仅对双色球/大乐透生效）
      total_hits: 最小总命中数（对所有彩种生效，红蓝型=红+蓝）
      sort_by:    排序字段（time|prize|red_hits|blue_hits|total_hits；默认 time）
      sort_order: 排序方向（asc|desc；默认 desc）
    """
    from data.feedback import load_feedback_history

    raw_limit = request.args.get("limit", 0, type=int)
    limit = raw_limit if raw_limit and raw_limit > 0 else None

    min_prize = request.args.get("min_prize", "全部")
    red_hits_min = request.args.get("red_hits", "", type=str)
    blue_hits_min = request.args.get("blue_hits", "", type=str)
    total_hits_min = request.args.get("total_hits", "", type=str)
    sort_by = request.args.get("sort_by", "time")
    sort_order = request.args.get("sort_order", "desc")

    # 等级排序权重 —— 三套命名体系并存，缺一会被 .get(x,99) 当成"未中"排序：
    #   乐透型  双色球: 一等~六等        （_classify_prize）
    #   乐透型  大乐透: 一等~九等        （_classify_dlt_prize，2023九级）
    #   七星彩          : 一等奖~六等奖    （_qxc_prize）
    #   数字型(3D/排列3/排列5): 直选 / 组选3 / 组选6 / 未中（_group_winning；排列5 只有直选）
    prize_rank = {
        "一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6,
        "七等": 7, "八等": 8, "九等": 9,
        "一等奖": 1, "二等奖": 2, "三等奖": 3, "四等奖": 4, "五等奖": 5, "六等奖": 6,
        "直选": 1, "组选3": 2, "组选6": 3,
        "未中": 99,
    }
    min_rank = prize_rank.get(min_prize, 0) if min_prize != "全部" else 0

    def _int_param(value, default=None):
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    red_min = _int_param(red_hits_min)
    blue_min = _int_param(blue_hits_min)
    total_min = _int_param(total_hits_min)

    rb = is_redblue(lottery)

    # 全量读取后筛选、排序、截断；当前数据规模可控，保证筛选语义准确
    history = load_feedback_history(lottery, lookback=None)
    history = list(reversed(history))  # 默认最新在前

    filtered = []
    for f in history:
        prize = f.get("中奖等级", "未中")
        if min_rank and prize_rank.get(prize, 99) > min_rank:
            continue

        red_hit = f.get("红球命中", 0) or 0
        blue_hit = f.get("蓝球命中", 0) or 0
        total_hit = f.get("总命中", 0) or 0

        if rb:
            if red_min is not None and red_hit < red_min:
                continue
            if blue_min is not None and blue_hit < blue_min:
                continue
        # 总命中筛选对所有彩种生效；红蓝型若没有总命中字段则自行计算
        effective_total = total_hit if total_hit else (red_hit + blue_hit)
        if total_min is not None and effective_total < total_min:
            continue

        filtered.append(f)

    # 排序
    sort_key = None
    if sort_by == "prize":
        sort_key = lambda x: (prize_rank.get(x.get("中奖等级", "未中"), 99), x.get("评估时间", ""))
    elif sort_by == "red_hits":
        sort_key = lambda x: x.get("红球命中", 0)
    elif sort_by == "blue_hits":
        sort_key = lambda x: x.get("蓝球命中", 0)
    elif sort_by == "total_hits":
        sort_key = lambda x: x.get("总命中", 0) or (x.get("红球命中", 0) + x.get("蓝球命中", 0))
    else:  # time
        sort_key = lambda x: x.get("评估时间", "")

    reverse = sort_order != "asc"
    filtered.sort(key=sort_key, reverse=reverse)

    if limit and limit > 0:
        filtered = filtered[:limit]

    records = [dict(f, view=feedback_record_view(lottery, f)) for f in filtered]
    return jsonify({
        "total": len(records),
        "is_redblue": rb,
        "records": records,
    })


@bp.route("/api/feedback/<lottery>/pending")
@api_error_handler
@validate_lottery
def api_feedback_pending(lottery):
    """待开奖预测列表（附带每组策略权重与推荐分，标出 Top1 推荐使用）"""
    from web.utils import build_pending_with_weights
    return jsonify(build_pending_with_weights(lottery))


@bp.route("/api/feedback/<lottery>/tier-comparison")
@api_error_handler
@validate_lottery
def api_feedback_tier_comparison(lottery):
    """三档命中对比（一般/稳健/高鲁棒；数据来自档位化反馈结算）"""
    from data.feedback import tier_comparison
    lookback = request.args.get("lookback", 0, type=int)
    return jsonify(tier_comparison(lottery, lookback=lookback or None))


@bp.route("/api/feedback/<lottery>/compare")
@api_error_handler
@validate_lottery
def api_feedback_compare(lottery):
    """
    对比可视化数据：已评估预测 vs 实际开奖
    返回：
    - cards: 每条已评估预测一张卡片（预测球/实际球/命中/差集/等级/来源）
    - cumulative: 累计命中率折线（按时间序）
    - strategy_avg: 各策略平均命中条形图数据
    """
    from data.feedback import load_feedback_history
    raw = request.args.get("limit", 0, type=int)
    limit = raw if raw and raw > 0 else None
    history = load_feedback_history(lottery, lookback=limit)

    # 卡片（倒序：最新在前）
    cards = []
    for f in reversed(history):
        cards.append({
            "期号": f.get("期号"),
            "预测日期": f.get("预测日期", ""),
            "开奖日期": f.get("开奖日期", ""),
            "评估时间": f.get("评估时间", ""),
            "来源": f.get("来源", "predict"),
            "策略": f.get("策略", ""),
            "预测红球": f.get("预测红球", []),
            "预测蓝球": f.get("预测蓝球", []),
            "实际红球": f.get("实际红球", []),
            "实际蓝球": f.get("实际蓝球", []),
            "红球命中": f.get("红球命中", 0),
            "蓝球命中": f.get("蓝球命中", 0),
            "总命中": f.get("总命中", 0),
            "红球差集": f.get("红球差集", []),
            "蓝球差集": f.get("蓝球差集", []),
            "中奖等级": f.get("中奖等级", "未中"),
            "valid_prediction": f.get("valid_prediction", True),
            "view": feedback_record_view(lottery, f),
        })

    # 累计命中率 / 策略表现只统计真预测（马后炮训练样本不计入，避免误导）
    valid_history = [f for f in history if f.get("valid_prediction", True)]

    # 累计命中率（按评估时间升序，仅真预测）；分母取各记录 total_choose（红蓝=红+蓝应选数，数字型=总选择）
    ordered = sorted(valid_history, key=lambda x: x.get("评估时间", ""))
    cumulative = []
    run_red = 0.0
    run_blue = 0.0
    run_total = 0.0
    for i, f in enumerate(ordered, 1):
        view = feedback_record_view(lottery, f)
        run_red += sum(z["hit"] for z in view["zones"] if z["cls"] == "red")
        run_blue += sum(z["hit"] for z in view["zones"] if z["cls"] == "blue")
        run_total += view["total_hit"]
        red_den = next((z["choose"] for z in view["zones"] if z["cls"] == "red"), 0)
        blue_den = next((z["choose"] for z in view["zones"] if z["cls"] == "blue"), 0)
        tc = view["total_choose"]
        cumulative.append({
            "n": i,
            "期号": f.get("期号"),
            "红球命中率": round(run_red / (i * red_den), 4) if red_den else None,
            "蓝球命中率": round(run_blue / (i * blue_den), 4) if blue_den else None,
            "累计命中率": round(run_total / (i * tc), 4) if tc else None,
        })

    # 各策略平均命中
    from collections import defaultdict
    import numpy as np
    strat = defaultdict(list)
    for f in valid_history:
        strat[f.get("策略", "未命名")].append(f.get("总命中", 0))
    strategy_avg = [
        {"策略": name, "平均总命中": round(float(np.mean(v)), 3), "样本数": len(v)}
        for name, v in strat.items()
    ]
    strategy_avg.sort(key=lambda x: x["平均总命中"], reverse=True)

    return jsonify({
        "total": len(history),
        "cards": cards,
        "cumulative": cumulative,
        "strategy_avg": strategy_avg,
    })
