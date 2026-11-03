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


def _budget_snapshot():
    """预算闸门的只读快照（供「出号数量」卡显示「本月已投 / 额度 / 剩余」）。

    为什么要有：闸门是**已启用**的硬约束，但它此前在界面上**完全没有出口** ——
    用户只能翻 `config/budget_aggregate.json` 才知道额度是多少，而额度用尽时
    表现出来的是「某天起忽然不出号了」，卡片上的滑轨值照样显示得很正常。
    这类「滑轨看起来正常、实际已停发」正是本项目最怕的失真，故把读数摆到卡片上。

    ★ 只读、不参与任何决策；任何异常都返回 {"可用": False, "原因": ...} 并让前端隐藏，
      绝不因为「读不到预算」把出号数量卡整块打挂。
    ★ 金额口径：税前单注奖金无关；这里只是**投入**（注数 × 单价），不是收益。
    """
    try:
        from ev.budget_gate import enabled_limit, load_spend
        lim = enabled_limit()
    except Exception as e:                                    # noqa: BLE001
        # ★ GateConfigError（启用但缺总额）也走这里 —— 它是**必须让人看见**的错，
        #   所以带出原因；但界面上仍按「不可用」处理，不假装额度是 0 或无穷。
        return {"可用": False, "原因": f"{type(e).__name__}: {e}"[:200]}
    if lim is None:
        return {"可用": False, "原因": "预算闸门未启用"}
    try:
        doc = load_spend()
        spent = float(doc.get("已投") or 0.0)
        monthly = float(lim["月度总额"])
        out = {
            "可用": True,
            "月度总额": monthly,
            "单期上限": float(lim["单期上限"]),
            "已投": round(spent, 2),
            "剩余": round(max(0.0, monthly - spent), 2),
            "月份": doc.get("月份"),
            "锁定": bool(lim.get("锁定", True)),
        }
        # ★ 只报「**这一次**读盘读不出来」。不能直接用 `doc.get("★台账损坏")`：
        #   那是**历史标记**，一旦某次读失败被写回文件就会永久留在里面，之后每次
        #   读取都「报损坏」—— 我本轮就被它误导过（台账当时是好的）。
        #   故自己再解析一次文件来判断当下的状态。
        try:
            import json as _json
            from ev.budget_gate import SPEND_PATH as _SP
            if _SP.exists():
                _json.loads(_SP.read_text(encoding="utf-8"))
        except Exception as e:                                # noqa: BLE001
            out["台账本次读取失败"] = f"{type(e).__name__}: {e}"[:200]
        # 额度已用尽 ⇒ 明确说出来（此时出号会被夹到 0 注，不是「正常」）
        out["已用尽"] = out["剩余"] <= 0
        return out
    except Exception as e:                                    # noqa: BLE001
        return {"可用": False, "原因": f"{type(e).__name__}: {e}"[:200]}


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
    # ★ 预算只读快照跟着一起回：卡片上要同时显示「可拖到多少」与「这个月还能花多少」，
    #   否则「上限 500」会被读成「可以随便 500」，而真正先撞到的是额度而不是上限。
    return jsonify({**ts.snapshot(expected_rates=rates, hit_stats=stats),
                    "预算": _budget_snapshot()})


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

    # ★ 上限**先于**注数处理：放宽上限后同一次请求里的新注数才不会被旧上限截断。
    if isinstance(data.get("limits"), dict):
        for lot, val in data["limits"].items():
            if lot not in LOTTERY_CONFIG:
                continue
            try:
                cfg["limits"][lot] = max(ts.MIN_TICKETS, min(ts.HARD_CAP, int(val)))
            except (TypeError, ValueError):
                continue

    # ★ 锁定：true ⇒ 动态策略不再自动改注数（用户调好的值不被后台改回去）
    if "锁定" in data:
        cfg["锁定"] = bool(data["锁定"])

    # ★ 服务端兜底：只接受与「生效模式」一致的那个字段。
    #   滑轨只展示当前模式的值，若把另一个字段一起写进去，动态模式下点保存就会把
    #   动态值覆盖进 fixed（用户意图锚点）⇒ 锚点永久丢失、注数单向下沉。
    #   旧版前端可能仍同时提交两个字段，故在服务端再拦一道。
    allow = "dynamic" if cfg.get("mode") == ts.MODE_DYNAMIC else "fixed"
    limits = cfg.get("limits")
    for key in ("fixed", "dynamic"):
        if key != allow:
            continue
        incoming = data.get(key)
        if isinstance(incoming, dict):
            # ★ 「合法的空提交」必须挡在服务端：前端只收集到 0 个彩种时（曾因 DOM id
            #   撞成 99 而全部取不到），请求体是 `{"mode":"dynamic","dynamic":{}}` ——
            #   语法合法、字段合法、业务上也「合法」（等于什么都不改），于是接口回 ok、
            #   页面重渲染回原值，用户看到的是「保存了但又变回去」。这种静默失败在
            #   日志里没有任何痕迹，只能在这里显式拒绝。
            if not incoming:
                return jsonify({
                    "ok": False,
                    "error": "提交里没有任何彩种的注数（内容为空），已拒绝保存",
                }), 400
            _saved = 0
            for lot, val in incoming.items():
                if lot not in LOTTERY_CONFIG:
                    continue
                try:
                    cfg[key][lot] = ts._clamp(int(val), lot, limits)
                    _saved += 1
                except (TypeError, ValueError):
                    continue
            if not _saved:
                return jsonify({
                    "ok": False,
                    "error": "提交的彩种名都不在注册表里，未保存任何注数",
                }), 400

    saved = ts.save_config(cfg)
    # 固定模式下同步 current = fixed（当前生效值）
    if saved.get("mode") == ts.MODE_FIXED:
        saved["current"] = dict(saved["fixed"])
        saved = ts.save_config(saved)

    rates, stats = _expected_and_stats()
    return jsonify({"ok": True, **ts.snapshot(expected_rates=rates, hit_stats=stats),
                    "预算": _budget_snapshot()})


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
            hit_n=st.get("hit_n"), n_batches=st.get("n_batches"),
            anchor=cfg["fixed"].get(lot))
        # force=True：这里是用户**主动**点「按策略调整」，锁定不该拦住它
        ts.apply_dynamic_result(lot, dec["next"], dec["reason"], force=True)
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
    return jsonify({"ok": True, **ts.snapshot(expected_rates=rates, hit_stats=stats),
                    "预算": _budget_snapshot()})


# ===== 微信推送（出号 + 中奖记录送上门，2026-09-16）=====

def _push_snapshot():
    from data import push_notify as pn
    cfg = pn.sanitize(pn.load_config())
    token = cfg.get("token", "")
    out = dict(cfg)
    out["token"] = (token[:2] + "****" + token[-4:]) if len(token) > 8 else ("已配置" if token else "")
    out["token_set"] = bool(token)
    out["ready"] = pn.effective_ready(cfg)
    out["image_supported"] = cfg.get("provider") == pn.PROVIDER_WECOM
    out["webhook_base"] = pn.WECOM_WEBHOOK_BASE   # 供前端提示（公开域名，非密钥）
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
    for k in ("推号码", "推结算", "图片推送"):
        if k in data:
            cfg[k] = bool(data[k])
    if "每日上限" in data:
        try:
            # 0 = 不限制（2026-09-18 用户确认取消上限；>0 才启用保护）
            cfg["每日上限"] = max(0, min(100000, int(data["每日上限"])))
        except (TypeError, ValueError):
            pass
    if "推送间隔秒" in data:
        try:
            # 企微每机器人 20 条/分钟；间隔越大越稳
            cfg["推送间隔秒"] = max(0.0, min(60.0, float(data["推送间隔秒"])))
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
    """发测试消息验证渠道连通性（计入统计）。

    2026-10-24 起按**分组结构**发：组头 → 开奖图 → 出号图（示例数据），
    这样在看板上点一下就能把渲染器验证到，而不用等到晚上自动化跑。
    ★ 2026-11-03：整段持组锁发送（真实推送是「组头 → 开奖 → 中奖 → 号码」，
      测试只发其中两类样本，不追求与真实组完全一致）。
    """
    from data import push_notify as pn
    cfg = pn.sanitize(pn.load_config())
    if not pn.effective_ready(cfg):
        return jsonify({"ok": False, "error": "请先选择渠道并填写 token，再开启推送"}), 400

    with_img = bool(cfg.get("图片推送")) and cfg.get("provider") == pn.PROVIDER_WECOM
    body = ("**配置成功** 此后自动流水线会把「今晚开奖 / 本期出号 / 开奖结算」"
            "按彩种分组推到这里（每个彩种：一条分隔头 + 若干张图）。\n\n")
    body += ("本次会发 3 条：分隔头（就是这条）→ 开奖图 → 出号图（示例数据）。\n\n"
             if with_img else
             "当前渠道不支持图片，后续推送会是单条 markdown 文本。\n\n")
    body += f"> {pn.FOOTER_CONDITION}"

    from data import push_image as pi
    demo_draw = {"期号": "示例", "日期": pn._today_str(),
                 "红球": [3, 11, 21, 23, 29, 32], "蓝球": [5]}
    demo = [{"号码": {"红球": [3, 11, 21, 23, 29, 32], "蓝球": [5]},
             "策略": "ML策略", "置信度": 0.38},
            {"号码": {"红球": [4, 10, 13, 18, 28, 29], "蓝球": [13]},
             "策略": "区间均衡策略", "置信度": 0.37}]

    # ★ 整段抢一次组锁（短超时）：测试推送也是「一条头 + 若干图」，
    #   不抢就可能插进某个彩种的一组图中间，用户会看到「双色球的图里混着示例图」。
    #   抢不到就降级直接发（这是人手动点的验证，不该被锁卡住）。
    with pn._group_spin_lock("推送测试", timeout=10.0):
        result = pn.send(pn._divider_title("推送测试"), body, cfg)
        pn._mark_pushed(cfg, result.get("ok", False), "测试：" + result.get("detail", ""))
        out = dict(result)
        if with_img:
            shots = []
            try:                            # ② 开奖图
                shots.append(("image_draw", pi.render_draw_image(
                    demo_draw, lottery="双色球", title="双色球 · 开奖图（示例）",
                    subtitle="以上为随机示例，不是真实开奖")))
            except Exception as e:
                out["image_draw"] = {"ok": False, "detail": str(e)[:200]}
            try:                            # ③ 出号图（第 1 页）
                pages = pi.paginate_tickets(demo)
                shots.append(("image", pi.render_tickets_image(
                    demo, title="双色球 · 本期出号（示例）",
                    subtitle="以上为随机示例，不是本期出号",
                    groups=pages[0] if pages else None, page=(1, max(1, len(pages))))))
            except Exception as e:
                out["image"] = {"ok": False, "detail": str(e)[:200]}
            for key, png in shots:
                img = pn.send_image(png, cfg)
                pn._mark_pushed(cfg, img.get("ok", False),
                                f"测试图片：{img.get('detail', '')}")
                out[key] = img
    return jsonify(out)
