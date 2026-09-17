from flask import Blueprint, jsonify, render_template, request
from pathlib import Path
from datetime import datetime, timedelta
from config import TRAINING_DIR
from web.utils import api_error_handler

bp = Blueprint("history", __name__)


@bp.route("/history")
def page():
    return render_template("history.html")


@bp.route("/api/history")
@api_error_handler
def api_history():
    days = request.args.get("days", 7, type=int)
    if not TRAINING_DIR.exists():
        return jsonify([])

    all_records = sorted(TRAINING_DIR.iterdir(), reverse=True)
    cutoff = datetime.now() - timedelta(days=days)
    result = []

    for r in all_records:
        if not r.is_dir():
            continue
        # 解析日期
        try:
            parts = r.name.split("_")
            record_date = datetime.strptime(parts[0] + parts[1], "%Y%m%d%H%M%S")
            if record_date < cutoff:
                continue
            date_str = record_date.strftime("%Y-%m-%d %H:%M")
        except (ValueError, IndexError):
            date_str = r.name

        # 计算大小
        total_size = sum(f.stat().st_size for f in r.glob("**/*") if f.is_file())
        size_kb = round(total_size / 1024, 1)

        # 类型
        name = r.name
        if "_predict" in name:
            rtype = "PREDICT"
        elif "anomaly" in name:
            rtype = "ANOMALY"
        else:
            rtype = "TRAIN"

        # 图表数量
        chart_count = len(list(r.glob("charts/*.png")))

        result.append({
            "name": name,
            "type": rtype,
            "date": date_str,
            "size_kb": size_kb,
            "charts": chart_count,
            "has_report": (r / "report.md").exists(),
        })

    return jsonify(result)


@bp.route("/api/history/view/<path:record_name>")
@api_error_handler
def api_history_view(record_name):
    """查看指定记录的报告内容"""
    import json
    record_dir = TRAINING_DIR / record_name
    if not record_dir.exists():
        return jsonify({"error": "not found"})

    # 报告
    report_path = record_dir / "report.md"
    report_content = report_path.read_text(encoding="utf-8") if report_path.exists() else ""

    # 预测 JSON（如果有）
    pred_path = record_dir / "prediction.json"
    pred_data = json.loads(pred_path.read_text(encoding="utf-8")) if pred_path.exists() else {}

    # 图表列表
    charts = sorted([str(p.relative_to(TRAINING_DIR)) for p in record_dir.glob("charts/*.png")])

    return jsonify({
        "name": record_name,
        "report": report_content,
        "prediction": pred_data,
        "charts": charts,
    })
