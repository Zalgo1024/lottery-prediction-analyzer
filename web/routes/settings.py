"""路由：出号数量配置（固定 / 动态滑轨）

用户可在看板「出号数量」卡里自由调整每次出号注数：
  - GET  /api/settings/ticket-size          → 各彩种当前值/上限/默认 + 动态建议
  - POST /api/settings/ticket-size          → 保存 mode / fixed / dynamic
  - POST /api/settings/ticket-size/apply    → 动态模式：立刻按策略算一次并应用
  - GET  /api/settings/ticket-size/reason   → 最近一次动态调整理由
"""

from flask import Blueprint, jsonify, request

from web.utils import api_error_handler

bp = Blueprint("settings", __name__)


def _expected_and_stats():
    """构造 snapshot 需要的 {彩种: 理论中奖率} 与 {彩种: 近期命中统计}。"""
    from config import LOTTERY_CONFIG
    from ev.payout import grade_probabilities
    from data.feedback import _recent_hit_stats, _last_compress_kpi

    rates, stats = {}, {}
    for lot in LOTTERY_CONFIG:
        try:
            probs = grade_probabilities(lot) or {}
            rates[lot] = max(0.0, min(1.0, sum(float(v) for v in probs.values())))
        except Exception:
            rates[lot] = None
        try:
            st = _recent_hit_stats(lot)
            st["last_kpi"] = _last_compress_kpi(lot)
            stats[lot] = st
        except Exception:
            stats[lot] = {}
    return rates, stats


@bp.route("/api/settings/ticket-size")
@api_error_handler
def api_ticket_size_get():
    from data import ticket_size as ts
    rates, stats = _expected_and_stats()
    return jsonify(ts.snapshot(expected_rates=rates, hit_stats=stats))


@bp.route("/api/settings/ticket-size", methods=["POST"])
@api_error_handler
def api_ticket_size_set():
    from data import ticket_size as ts
    from config import LOTTERY_CONFIG

    data = request.get_json() or {}
    cfg = ts.sanitize(ts.load_config())

    if "mode" in data:
        m = str(data["mode"]).lower()
        if m not in ts.VALID_MODES:
            return jsonify({"ok": False, "error": f"非法模式: {m}"}), 400
        cfg["mode"] = m

    for key in ("fixed", "dynamic"):
        incoming = data.get(key)
        if isinstance(incoming, dict):
            for lot, val in incoming.items():
                if lot not in LOTTERY_CONFIG:
                    continue
                try:
                    cfg[key][lot] = ts._clamp(int(val), lot)
                except (TypeError, ValueError):
                    continue

    saved = ts.save_config(cfg)
    # 固定模式下同步 current = fixed（当前生效值）
    if saved.get("mode") == ts.MODE_FIXED:
        saved["current"] = dict(saved["fixed"])
        saved = ts.save_config(saved)

    rates, stats = _expected_and_stats()
    return jsonify({"ok": True, **ts.snapshot(expected_rates=rates, hit_stats=stats)})


@bp.route("/api/settings/ticket-size/apply", methods=["POST"])
@api_error_handler
def api_ticket_size_apply():
    """动态模式：立刻按策略评估一次并写回（不等下一期开奖）。"""
    from data import ticket_size as ts
    from config import LOTTERY_CONFIG
    from data.feedback import (_recent_hit_stats, _last_compress_kpi,
                               _expected_win_rate)

    cfg = ts.sanitize(ts.load_config())
    if cfg.get("mode") != ts.MODE_DYNAMIC:
        return jsonify({"ok": False, "error": "当前不是动态模式"}), 400

    results = {}
    for lot in LOTTERY_CONFIG:
        cur = cfg["dynamic"].get(lot) or ts.default_count(lot)
        st = _recent_hit_stats(lot)
        dec = ts.decide_next_count(
            lot, current=cur, last_kpi=_last_compress_kpi(lot),
            hit_rate=st.get("hit_rate"), expected_rate=_expected_win_rate(lot),
            hit_n=st.get("hit_n"))
        ts.apply_dynamic_result(lot, dec["next"], dec["reason"])
        results[lot] = {"上一期注数": int(cur), **dec}

    rates, stats = _expected_and_stats()
    return jsonify({"ok": True, "调整": results, **ts.snapshot(expected_rates=rates, hit_stats=stats)})


@bp.route("/api/settings/ticket-size/reset", methods=["POST"])
@api_error_handler
def api_ticket_size_reset():
    """恢复默认（删除配置文件，回落 config.py 历史口径）。"""
    from data import ticket_size as ts
    try:
        if ts.STORE_PATH.exists():
            import os
            os.replace(ts.STORE_PATH, str(ts.STORE_PATH) + ".bak")
    except Exception:
        pass
    rates, stats = _expected_and_stats()
    return jsonify({"ok": True, **ts.snapshot(expected_rates=rates, hit_stats=stats)})


# ===== 微信推送（出号 + 中奖记录送上门，2026-09-16）=====

def _push_snapshot():
    from data import push_notify as pn
    cfg = pn.sanitize(pn.load_config())
    token = cfg.get("token", "")
    out = dict(cfg)
    out["token"] = (token[:2] + "****" + token[-4:]) if len(token) > 8 else ("已配置" if token else "")
    out["token_set"] = bool(token)
    out["ready"] = pn.effective_ready(cfg)
    return out


@bp.route("/api/settings/push-notify")
@api_error_handler
def api_push_get():
    return jsonify(_push_snapshot())


@bp.route("/api/settings/push-notify", methods=["POST"])
@api_error_handler
def api_push_set():
    from data import push_notify as pn
    data = request.get_json() or {}
    cfg = pn.sanitize(pn.load_config())
    if "provider" in data:
        p = str(data["provider"]).lower()
        if p not in pn.VALID_PROVIDERS:
            return jsonify({"ok": False, "error": f"未知渠道: {p}"}), 400
        cfg["provider"] = p
    if "token" in data:
        token = str(data["token"]).strip()
        # 掩码回传（含 ****）不覆盖真实 token
        if token and "****" not in token:
            cfg["token"] = token
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    for k in ("推号码", "推结算"):
        if k in data:
            cfg[k] = bool(data[k])
    if "每日上限" in data:
        try:
            cfg["每日上限"] = max(1, min(50, int(data["每日上限"])))
        except (TypeError, ValueError):
            pass
    if "看板地址" in data:
        cfg["看板地址"] = str(data["看板地址"] or "").strip()[:200]
    saved = pn.save_config(cfg)
    out = dict(saved)
    out["ok"] = True
    out["ready"] = pn.effective_ready(saved)
    return jsonify(out)


@bp.route("/api/settings/push-notify/test", methods=["POST"])
@api_error_handler
def api_push_test():
    """发一条测试消息验证渠道连通性（不占每日额度语义，但计入统计）。"""
    from data import push_notify as pn
    cfg = pn.sanitize(pn.load_config())
    if not pn.effective_ready(cfg):
        return jsonify({"ok": False, "error": "请先选择渠道并填写 token，再开启推送"}), 400
    result = pn.send("彩票助手·测试消息",
                     "配置成功！此后自动流水线跑完会把**出号**、**出号数量**与**中奖记录**推到这里。\n\n"
                     "> 仅供研究记录；单注中奖概率恒定，整体期望为负。", cfg)
    pn._mark_pushed(cfg, result.get("ok", False), "测试：" + result.get("detail", ""))
    return jsonify(result)
