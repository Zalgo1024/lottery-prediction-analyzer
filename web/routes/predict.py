from flask import Blueprint, jsonify, render_template, request
from web.utils import api_error_handler, start_predict, get_task
from web.validators import validate_predict_body

bp = Blueprint("predict", __name__)


@bp.route("/predict")
def page():
    return render_template("predict.html")


@bp.route("/api/predict", methods=["POST"])
@api_error_handler
@validate_predict_body
def api_predict():
    """异步启动预测任务，返回 task_id"""
    data = request.get_json(silent=True) or {}
    lottery = data.get("lottery", "双色球")
    # groups 留空 → 由 resolve_groups 按「出号数量配置(固定/动态)」解析
    groups = data.get("groups")
    mode = data.get("mode", "fresh")

    task_id = start_predict(lottery, {
        "groups": groups,
        "mode": mode,
        # ★ 验证/试算：{"record_pending": false} 只算不落库（默认 true=生产行为）。
        # ★ 研究性入口（lowcrowd）例外：默认不落账 —— 手工用小组数跑会按
        #   (期号, 一般, 研究) 替换掉调度器已写的 200 注研究批，打断台账序列。
        "record_pending": data.get("record_pending", mode != "lowcrowd"),
    })
    return jsonify({"task_id": task_id})


@bp.route("/api/predict/status/<task_id>")
@api_error_handler
def api_predict_status(task_id):
    """查询预测任务状态"""
    task = get_task(task_id)
    if task is None:
        return jsonify({"status": "not_found"})
    return jsonify({
        "status": task["status"],
        "progress": task["progress"],
        "message": task["message"],
        "result": task["result"],
        "error": task["error"],
    })


@bp.route("/api/predict/regenerate", methods=["POST"])
@api_error_handler
def api_predict_regenerate():
    """重新出一批号 —— **不删除**已有号码，新批与旧批并存。

    语义（2026-11-02 用户需求）：
      · 旧的号码全部保留在 pending 里，开奖后照常结算入历史
        （「旧号那边也有中的也要拉出来」）；
      · 新批带「重出批号」= N，与旧批在 pending 去重键上分开，互不覆盖；
      · 当期展示 / 推送 / 组数核对取**最新一批**（`latest_production_pending`
        按记录时间取最大），所以「优先采用新号」是天然成立的，不需要额外开关。

    ★ 不传 groups：一律走 `resolve_groups`，即用户刚在「出号数量」里保存的值。
      否则会出现「滑轨改成 30、重出还是 5 注」这种自相矛盾。
    ★ 不传 mode：与生产流水线一致用 fresh；研究用的 lowcrowd 是另一条台账，
      不能借这个按钮混进生产批次。

    ★★ 2026-11-07 事故修复：可接受「出号数量」= 卡片上用户当前看到的值，
      **先落盘**再走 `resolve_groups`。此前按钮只发彩种名，用户把滑块拖到
      10000 但没点「保存」就直接重出 ⇒ 后端仍按磁盘里的 1000 出号，成功后
      前端重绘 ⇒ 界面又变回 1000，看起来像「系统把我的注数改回去了」。
      ★ 落盘只写当前模式那一个字段（动态写 dynamic、固定写 fixed），不动
        另一个字段的意图锚点；值相同不写盘；超限按 limits 夹取并回传实际值。
      ★ 不带该参数时行为与修复前完全一致（纯读、不写盘、groups 仍为 None）。
    """
    from config import LOTTERY_CONFIG
    from data.feedback import (_next_issue, latest_production_pending,
                               load_pending, next_renumber)

    data = request.get_json(silent=True) or {}
    lottery = str(data.get("lottery") or "").strip()
    if lottery not in LOTTERY_CONFIG:
        return jsonify({"ok": False, "error": f"未知彩种：{lottery!r}"}), 400

    saved_n = None
    if data.get("出号数量") is not None:
        from data import ticket_size as ts
        try:
            want = int(data["出号数量"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "出号数量必须是整数"}), 400
        cfg = ts.sanitize(ts.load_config())
        field = ts.MODE_DYNAMIC if cfg.get("mode") == ts.MODE_DYNAMIC else ts.MODE_FIXED
        want = ts._clamp(want, lottery, cfg.get("limits"))
        if cfg[field].get(lottery) != want:
            cfg[field][lottery] = want
            ts.save_config(cfg)
        saved_n = want

    issue = _next_issue(lottery)
    prev = latest_production_pending(load_pending(lottery), target_issue=issue)
    rn = next_renumber(lottery, issue)

    task_id = start_predict(lottery, {
        "groups": None,                 # → resolve_groups（用滑轨当前生效值）
        "mode": "fresh",
        "record_pending": True,
        "renumber": rn,
    })
    return jsonify({
        "ok": True,
        "task_id": task_id,
        "彩种": lottery,
        "目标期号": issue,
        "重出批号": rn,
        "上一批注数": len((prev or {}).get("预测号码") or []),
        "已保存出号数量": saved_n,
        "出号数量来源": ("卡片当前值（已落盘）" if saved_n is not None
                        else "配置生效值（本次未提交卡片值）"),
        "说明": ("旧批保留（开奖后照常结算）；当前展示与推送优先用新批。"
                 if prev else "本期此前没有生产批，本次即首出。"),
    })
