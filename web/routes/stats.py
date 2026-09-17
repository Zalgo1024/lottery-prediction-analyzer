from flask import Blueprint, jsonify, render_template, request
from config import LOTTERY_CONFIG
from data.loader import load_lottery
from pipeline.statistics import summary_stats, frequency_analysis, missing_value_analysis, hot_cold_ranking
from pipeline.step1_probability import get_prize_data
from pipeline.step2_expected import expected_value
from web.utils import api_error_handler
from web.validators import validate_lottery

bp = Blueprint("stats", __name__)


@bp.route("/stats")
def page():
    return render_template("stats.html")


@bp.route("/api/stats/<lottery>")
@api_error_handler
@validate_lottery
def api_stats(lottery):
    data = load_lottery(lottery)
    s = summary_stats(data)
    # 奖级概率（通用入口：双色球/大乐透走原逻辑，数字型走分区通用计算）
    probs = get_prize_data(lottery)
    ev = expected_value(lottery)
    return jsonify({
        "summary": s,
        "prize_levels": probs["levels"],
        "total_win_prob": probs["total_win_probability"],
        "expected_value": ev,
    })
