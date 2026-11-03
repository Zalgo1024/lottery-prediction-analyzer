"""
盈利账本 Web 路由（只读）

★ 定位：把"每期花了多少、回来多少、净亏多少"摊开，**不预测号码**。
  页面回答的三件事，按优先级：
    ① 净收益率（净盈亏 ÷ 成本）—— 决策指标
    ② 最大回撤 —— 决策指标
    ③ 分母三数（开奖 / 参与 / 未参与）—— 防止"只数中奖期"
  「中奖金额超 10 元的期数」保留，但**标注为次级指标**：它随注数机械上升，
  不能跨注数档比较；花 800 中 20 元仍是亏损。

- GET /ledger                              页面
- GET /api/ledger/summary                  六彩种 + 合计（`?lottery=`、`?periods=`）
- GET /api/ledger/periods/<lottery>        逐期明细（`?limit=`）
- GET /api/ledger/glossary                 术语与卡片口径（文案单一真源）
- GET /api/ledger/shadow                   同预算三臂对照摘要（影子模式，不占预算）

★ 只读纪律：本模块**只 load 已落盘的账本**，不在请求里重建。
  重建要从逐注历史 + 开奖数据算全量，属于批处理，走 `scripts/rebuild_ledger.py`。
  账本文件不存在时返回空态（`状态: nodata`）并说明该跑哪条命令，
  **绝不把"没有数据"显示成"0 元"**（¥0.00 是合法数字，会骗人）。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from web.utils import api_error_handler
from web.validators import validate_lottery

bp = Blueprint("ledger", __name__)

_ALL = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩", "七乐彩", "快乐8"]


def _empty_state(lottery: str) -> dict:
    """没有账本时的显式空态（金额一律 None，不给 0）。"""
    return {
        "状态": "nodata",
        "彩种": lottery,
        "开奖期数": None, "参与期数": None, "未参与期数": None,
        "成本": None, "税前奖金": None, "税": None, "实得": None,
        "净盈亏": None, "净收益率": None, "最大回撤": None,
        "中奖注数": None, "中奖期数": None, "中奖期占比": None,
        "中奖注数_ge10": None, "中奖期数_ge10": None, "中奖期占比_ge10": None,
        "说明": "账本尚未生成。运行 `python scripts/rebuild_ledger.py --all` 后刷新。",
    }


def _summary_of(lottery: str, periods: int | None = None) -> dict:
    from data import ledger as L

    rows = L.load(lottery)
    if not rows:
        return _empty_state(lottery)
    if periods:
        rows = rows[-int(periods):]
    s = L.summary_of(rows, lottery=lottery)
    s["状态"] = "ok"
    return s


def _total(items: list) -> dict:
    """跨彩种合计。★ 任一彩种缺数据 ⇒ 该项不参与求和并单独计数（不按 0 计）。"""
    ok = [s for s in items if s.get("状态") == "ok"]
    missing = [s["彩种"] for s in items if s.get("状态") != "ok"]

    def _sum(key):
        return round(sum(float(s.get(key) or 0.0) for s in ok), 2) if ok else None

    cost = _sum("成本")
    net = _sum("净盈亏")
    part = sum(int(s.get("参与期数") or 0) for s in ok) if ok else None
    hitp = sum(int(s.get("中奖期数") or 0) for s in ok) if ok else None
    hitp10 = sum(int(s.get("中奖期数_ge10") or 0) for s in ok) if ok else None
    return {
        "状态": "ok" if ok else "nodata",
        "彩种": "合计",
        "参与彩种数": len(ok),
        "缺数据彩种": missing,
        "开奖期数": sum(int(s.get("开奖期数") or 0) for s in ok) if ok else None,
        "参与期数": part,
        "未参与期数": sum(int(s.get("未参与期数") or 0) for s in ok) if ok else None,
        "成本": cost,
        "税前奖金": _sum("税前奖金"),
        "税": _sum("税"),
        "实得": _sum("实得"),
        "净盈亏": net,
        "净收益率": (net / cost) if (cost and net is not None) else None,
        "最大回撤": max((float(s.get("最大回撤") or 0.0) for s in ok), default=None) if ok else None,
        "中奖注数": sum(int(s.get("中奖注数") or 0) for s in ok) if ok else None,
        "中奖期数": hitp,
        # ★ 占比的分母是**参与期数**，跨彩种合计时也只能用参与期数之和
        "中奖期占比": (hitp / part) if part else None,
        "中奖注数_ge10": sum(int(s.get("中奖注数_ge10") or 0) for s in ok) if ok else None,
        "中奖期数_ge10": hitp10,
        "中奖期占比_ge10": (hitp10 / part) if part else None,
        "金额未知注数": sum(int(s.get("金额未知注数") or 0) for s in ok) if ok else None,
    }


@bp.route("/ledger")
def page():
    return render_template("ledger.html", all_lotteries=_ALL)


@bp.route("/api/ledger/summary")
@api_error_handler
def api_summary():
    lottery = (request.args.get("lottery") or "全部").strip()
    period_arg = request.args.get("periods")
    periods = None
    if period_arg and str(period_arg).isdigit() and int(period_arg) > 0:
        periods = int(period_arg)

    targets = _ALL if lottery in ("全部", "", "all") else [lottery]
    items = [_summary_of(x, periods) for x in targets]
    total = _total(items) if len(items) > 1 else None

    from ev.mechanism.narrative import EV_NEGATIVE_CAVEAT

    return jsonify({
        "彩种": lottery,
        "窗口": periods,
        "分彩种": items,
        "合计": total,
        "主指标": ["净收益率", "最大回撤"],
        "次级指标": ["中奖注数", "中奖期数", "中奖期数_ge10"],
        "诚实边界": EV_NEGATIVE_CAVEAT,
        "口径": "成本 = 实际注数 × 单价；奖金按当期规则版本回填；税为全额 20%（超一万起征）；"
                "金额未知的注排除在求和之外并单独计数。",
    })


@bp.route("/api/ledger/periods/<lottery>")
@api_error_handler
@validate_lottery
def api_periods(lottery):
    from data import ledger as L

    limit = request.args.get("limit") or "50"
    try:
        n = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        n = 50

    rows = L.load(lottery)
    if not rows:
        return jsonify({
            "状态": "nodata", "彩种": lottery, "期数": [],
            "说明": "账本尚未生成。运行 `python scripts/rebuild_ledger.py --all` 后刷新。",
        })

    tail = rows[-n:]
    keep = ("期号", "开奖日期", "参与", "参与原因", "批次号", "出号时刻",
            "请求注数", "实际注数", "单价", "成本", "税前奖金", "税", "实得",
            "净盈亏", "累计净盈亏", "回撤", "最大回撤",
            "中奖注数", "中奖期", "金额未知注数")
    out = [{k: r.get(k) for k in keep} for r in tail]
    return jsonify({"状态": "ok", "彩种": lottery, "期数": out,
                    "总数": len(rows), "返回": len(out)})


@bp.route("/api/ledger/shadow")
@api_error_handler
def api_shadow():
    """同预算三臂对照摘要 + **可判性判决**。影子模式 `记账=False`，不占预算。

    ★ 读数与判决一起给、也一起不给：只给读数时，8~10 期的三行净收益率几乎一定长得
      不一样，人眼会挑最小的那个当「模型变强了」。判决层负责回答「够不够算」。
    """
    from data import shadow as S

    out = {}
    for lot in _ALL:
        try:
            summ = S.summary(lot)
        except Exception as e:                     # 单彩种失败不影响其余
            out[lot] = {"错误": str(e)}
            continue
        try:
            from data import shadow_verdict as V
            summ["判决"] = V.compare(lot)
        except Exception as e:                     # 判决失败不能让读数一起消失
            summ["判决"] = {"可用": False, "原因": f"判决计算失败：{e}"}
        out[lot] = summ
    return jsonify({"分彩种": out, "诚实边界": S.HONEST_NOTE})


@bp.route("/api/ledger/glossary")
@api_error_handler
def api_glossary():
    from web.ledger_glossary import payload
    return jsonify(payload())
