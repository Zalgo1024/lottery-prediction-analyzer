"""
策略战绩榜 Web 路由

提供战绩榜页面和 API：
- GET  /records                           战绩榜页面
- GET  /api/records/<lottery>             战绩数据（实盘 + 回测）
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
