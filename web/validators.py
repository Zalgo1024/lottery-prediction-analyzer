"""
Web API 请求参数校验层
统一的输入校验装饰器，应用于各路由
"""

from functools import wraps
from flask import request, jsonify
from config import LOTTERY_CONFIG


def validate_lottery(f):
    """装饰器：校验 lottery URL 参数是否为合法彩票类型"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        lottery = kwargs.get("lottery", "")
        if lottery not in LOTTERY_CONFIG:
            return jsonify({"error": f"不支持的彩票类型: {lottery}"}), 400
        return f(*args, **kwargs)
    return wrapper


def validate_threshold(f):
    """装饰器：校验 anomaly threshold 查询参数"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        threshold = request.args.get("threshold", "normal")
        if threshold not in ("strict", "normal", "loose", "all"):
            return jsonify({"error": f"不支持的阈值等级: {threshold}"}), 400
        return f(*args, **kwargs)
    return wrapper


def validate_predict_body(f):
    """装饰器：校验 predict POST 请求体参数"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        from config import PREDICT_DEFAULTS
        data = request.get_json(silent=True) or {}
        # groups 缺省 / null → 交给 resolve_groups 按「出号数量」滑轨解析。
        # ★ 2026-10-21：原校验限死 1..10 且把 null 判成非法——它既挡住了滑轨
        #   解析（null 直接 400），又暗示"手工填 5 注"是正常用法，是 5 注覆盖事故的温床。
        groups = data.get("groups")
        if groups is not None:
            upper = int(PREDICT_DEFAULTS.get("max_groups", 200))
            if isinstance(groups, bool) or not isinstance(groups, int) or not (1 <= groups <= upper):
                return jsonify({"error": f"号码组数需在 1-{upper} 之间（留空则按出号数量滑轨）: {groups}"}), 400
        mode = data.get("mode", "fresh")
        if mode not in ("fresh", "trained", "rule", "high_freq", "missing", "balanced",
                        "lowcrowd"):
            return jsonify({"error": f"不支持的预测模式: {mode}"}), 400
        return f(*args, **kwargs)
    return wrapper
