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
        data = request.get_json(silent=True) or {}
        groups = data.get("groups", 5)
        if not isinstance(groups, int) or not (1 <= groups <= 10):
            return jsonify({"error": f"号码组数需在1-10之间: {groups}"}), 400
        mode = data.get("mode", "fresh")
        if mode not in ("fresh", "trained", "rule", "high_freq", "missing", "balanced"):
            return jsonify({"error": f"不支持的预测模式: {mode}"}), 400
        return f(*args, **kwargs)
    return wrapper
