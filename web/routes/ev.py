"""
期望时机 Web 路由

- GET /ev                          期望时机页面
- GET /api/ev/<lottery>            期望序列数据（最近50期）
- GET /api/ev/advice/<lottery>     当前期购买建议
"""

from flask import Blueprint, jsonify, render_template, request

from web.utils import api_error_handler
from web.validators import validate_lottery
from data.loader import load_lottery
from pipeline.step6_ev import compute_ev_series, current_period_advice

bp = Blueprint("ev", __name__)


@bp.route("/ev")
def page():
    return render_template("ev.html")


@bp.route("/api/ev/<lottery>")
@api_error_handler
@validate_lottery
def api_ev(lottery):
    """期望序列（默认最近 50 期；可用 ?limit= 或 ?days= 控制窗口）"""
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    days = request.args.get("days")
    if days:
        try:
            days = int(days)
        except ValueError:
            days = None
    data = load_lottery(lottery)
    series = compute_ev_series(lottery, data, limit=limit, days=days)
    # 附加：训练诚实化指标 + 待开奖推荐清单（供曲线下方展示）
    from web.utils import get_training_honesty, build_pending_with_weights
    series["training_honesty"] = get_training_honesty(lottery)
    series["pending_recommended"] = build_pending_with_weights(lottery)
    return jsonify(series)


@bp.route("/api/ev/advice/<lottery>")
@api_error_handler
@validate_lottery
def api_ev_advice(lottery):
    """当前期购买建议"""
    data = load_lottery(lottery)
    return jsonify(current_period_advice(lottery, data))
