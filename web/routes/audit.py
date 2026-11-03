"""
测量与证据看板 Web 路由（只读指标，卡片清单见 `web.audit_glossary._CARDS`）

★ 页面在导航里叫「证据看板」（侧栏「测量与证据看板」），本 docstring、导航 note、
  模板 h2 三处用同一句话，避免重演「三者各说一套」。

- GET /audit                         测量与证据看板页面
- GET /api/audit/quality/<lottery>   ① 开奖质量监控（数据质量评分+异常分区）
- GET /api/audit/ev/<lottery>        ② EV 看板（名义EV表 + 限号/撞号修正 + 冷门TOP）
- GET /api/audit/rollover/<lottery>  ② 双/大回落：Rollover-EV（参与信号）
- GET /api/audit/pl/<lottery>        ③ P/L 反馈摘要（命中率 | 模拟盈亏 | 权重）
- GET /api/audit/distance/<lottery>  ④ 距离画像（观测 vs 随机基线）
- GET /api/audit/payout_profile/<lottery> ⑤ 实得画像（流行度LRT+反事实回测，NetPayout WP4）
- GET /api/audit/trials/<lottery>         ⑥ A/B 对照进度 + 人群漂移裁决（NetPayout WP5）
- GET /api/audit/attribution/<lottery>    ⑦ 中奖归因（来源×策略×档位 + 最新批次撞号覆盖）
- GET /api/audit/attribution_global       ⑦ 六彩种归因汇总
- GET /api/audit/gap/<lottery>            ⑧ 机制缝隙测量状态卡（四轨裁决）
- GET /api/audit/gap_global               ⑧ 六彩种缝隙测量汇总
- GET /api/audit/robustness               ⑨ 策略鲁棒性（压力检验：优势在失真下是否存活）
- GET /api/audit/glossary                 本页术语与卡片口径（文案单一真源）

定位：只呈现已测量的结果与它们的适用边界，不输出任何"中奖承诺"。

★ 名词边界：本站真正的「数据体检」是离线脚本 `scripts/health_check.py` 与
  `scripts/scan_data_integrity.py`（A~G 七项完整性扫描），结论落在
  `logs/项目体检_*.md` 与推送里，**不在本页**。本页改名正是为了让出这个词。
"""

from flask import Blueprint, jsonify, render_template, request

from web.utils import api_error_handler
from web.validators import validate_lottery

bp = Blueprint("audit", __name__)

# 彩种分组
_DIGITAL = ["排列5", "福彩3D", "排列3", "七星彩"]
_LOTTERY_STYLE = ["双色球", "大乐透"]
_ALL = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩", "七乐彩", "快乐8"]


@bp.route("/audit")
def page():
    from web.audit_glossary import cards_public
    # ★ 卡片骨架（序号/标题/id）来自术语模块，模板不再自己抄一遍标题清单。
    return render_template("audit.html", all_lotteries=_ALL, audit_cards=cards_public())


@bp.route("/api/audit/quality/<lottery>")
@api_error_handler
@validate_lottery
def api_quality(lottery):
    """开奖质量监控：数据质量评分 + 异常分区（第③支柱 data_quality_report）"""
    from credibility.data_quality import data_quality_report
    r = data_quality_report(lottery)
    return jsonify(r)


@bp.route("/api/audit/ev/<lottery>")
@api_error_handler
@validate_lottery
def api_ev(lottery):
    """EV 看板：名义EV + 限号/撞号修正 + 冷门TOP（数字型）"""
    from ev.rulebook import nominal_ev
    from ev.engine import net_ev, scan_issues
    from ev.sampler import sample_tickets

    if lottery not in _DIGITAL:
        # ★ 这里原先对所有非数字型都回「用 rollover 看」—— 对双/大是对的，
        #   但七乐彩/快乐8 **没有奖池滚存**，指过去是空页面。
        #   它们的口径在 ev.profit（逐奖级、按规则表 + 当期实测单注奖金）。
        if lottery in ("七乐彩", "快乐8"):
            from ev.profit.rules import rules_for

            n_grades = len(rules_for(lottery).grades)
            return jsonify({
                # ★ 「不适用」是给前端看的类型标记：不适用 ≠ 加载失败。
                #   没有这个标记时前端只能落进 alert-warning，一张正常的
                #   「本页不适用」卡片被渲染成红色的故障样式。
                "不适用": True,
                "error": f"{lottery} 的 EV 口径在 ev.profit（按当期实测单注奖金逐奖级算），"
                         f"不走 net_ev；本页的「限号/撞号修正」只对数字型有意义",
                "怎么看": f"python cli.py payout {lottery} --nums "
                          + ",".join(str(i) for i in range(1, 8 if lottery == "七乐彩" else 11))
                          + (" --play 选十" if lottery == "快乐8" else ""),
                # ★ 档数按规则表现算，别写死：七乐彩 7 档、快乐8 选十 7 档（全玩法 39 档）。
                "口径": f"单注 EV 与选号无关；{lottery} 共 {n_grades} 档"
                        + ("（选十玩法；全玩法 39 档）" if lottery == "快乐8" else "")
                        + "，全部按「名义值 + 限赔/封顶」结算",
            })
        return jsonify({"error": f"{lottery} 为彩票式（双/大），用 /api/audit/rollover 查看 Rollover-EV"})

    # 名义EV 表（各玩法）
    nom = nominal_ev(lottery)
    ev_rows = nom["ev_per_ticket"]
    if isinstance(ev_rows, dict):
        ev_table = [{"玩法": k, "名义EV": v} for k, v in ev_rows.items()]
    else:
        ev_table = [{"玩法": "单票", "名义EV": ev_rows}]

    # 冷门 TOP5（限号EV 最高）——七星彩=撞号修正后最高；固定赔率=EV恒定（展示冷门拥挤度）
    cold = []
    try:
        r = scan_issues(lottery, top_n=5)
        cold = r.get("TOP冷门(限号EV最高)", [])
    except Exception as e:
        cold = [{"error": str(e)}]

    # 约束采样 3 组示例（反大众）
    sample = []
    try:
        s = sample_tickets(lottery, n=3)
        sample = [{"号码": t["号码"], "拥挤分位": t["拥挤分位"], "和值": t["和值"]} for t in s["号码组"]]
    except Exception as e:
        sample = [{"error": str(e)}]

    mechanism = ("七星彩：浮动头奖撞号分薄 → 冷门号EV>热门号EV"
                 if lottery == "七星彩" else
                 "固定赔率：单注EV恒定（限号=停售+滚动放号），选号不改变单注期望")
    return jsonify({
        "彩种": lottery,
        "名义EV表": ev_table,
        "冷门TOP": cold,
        "约束采样": sample,
        "机制": mechanism,
    })


@bp.route("/api/audit/rollover/<lottery>")
@api_error_handler
@validate_lottery
def api_rollover(lottery):
    """双/大 Rollover-EV（参与信号）"""
    from ev.rollover import rollover_ev, rollover_history
    if lottery not in _LOTTERY_STYLE:
        # 七乐彩/快乐8 没有奖池滚存 ⇒ 指到 rollover 是空页面，直接给真实入口。
        if lottery in ("七乐彩", "快乐8"):
            return jsonify({
                "error": f"{lottery} 无奖池滚存（Rollover）概念",
                "怎么看": f"python cli.py payout {lottery}",
                "口径": "单注 EV 按期列出（含限赔/封顶后的实测单注奖金）",
            })
        return jsonify({"error": f"{lottery} 非彩票式，无 Rollover 概念（用 /api/audit/ev）"})
    cur = rollover_ev(lottery)
    hist = rollover_history(lottery, n=10)
    cur["趋势(近10期)"] = hist
    return jsonify(cur)


@bp.route("/api/audit/pl/<lottery>")
@api_error_handler
@validate_lottery
def api_pl(lottery):
    """P/L 反馈摘要：命中率 | 模拟盈亏 | 权重（任务1.3）"""
    from data.feedback import get_feedback_summary
    from data.schema import is_redblue
    s = get_feedback_summary(lottery, lookback=None)
    s["is_redblue"] = is_redblue(lottery)
    # 只保留前端需要的字段
    return jsonify(s)


@bp.route("/api/audit/distance/<lottery>")
@api_error_handler
@validate_lottery
def api_distance(lottery):
    """距离画像：预测「差多远」的分布 vs 随机基线（与随机基线对照的近似命中度量）"""
    from ev.distance_metrics import distance_stats
    return jsonify(distance_stats(lottery))


@bp.route("/api/audit/trials/<lottery>")
@api_error_handler
@validate_lottery
def api_trials(lottery):
    """
    A/B 对照实验 + 漂移监控（NetPayout WP5）：
      - 前瞻配对对照的当前进度（主终点可完成的；次终点仅存档）
      - 人群行为漂移裁决（维护信号，不是"变强"信号）
    """
    from ev.popularity import drift_report
    from ev.trials import trial_report

    return jsonify({
        "彩种": lottery,
        "定位": ("前瞻对照 = 验证「选号不改变中奖概率」（等价性检验，可完成）；"
                 "漂移监控 = 保证结论不过时（维护，不提高收益）"),
        "对照实验": trial_report(lottery),
        "漂移监控": drift_report(lottery),
    })


@bp.route("/api/audit/payout_profile/<lottery>")
@api_error_handler
@validate_lottery
def api_payout_profile(lottery):
    """
    实得画像（NetPayout WP4）：组合流行度检验（LRT+CV）+ 历史反事实回测。
    与「距离画像」对称：距离画像证明"不会更常中"，实得画像回答"中了能拿多少"。
    """
    from ev.popularity import get_popularity_model, counterfactual_backtest

    # 走两级缓存（进程内/磁盘 cache/）：fit_popularity 直调会在每次重启后重跑 12 次 GLM
    params = get_popularity_model(lottery)
    fit = params.get("report", {})
    cf = counterfactual_backtest(lottery) if params.get("go") else {
        "go": False, "note": "流行度模型未达 Go 门槛，反事实回测不具解释力（诚实否决）",
    }
    return jsonify({
        "彩种": lottery,
        "定位": ("实得奖金侧分析：不改变中奖概率，只回答「同样概率下，冷门组合中了能拿更多吗」"),
        "流行度检验": fit,
        "反事实回测": cf,
    })


@bp.route("/api/audit/attribution/<lottery>")
@api_error_handler
@validate_lottery
def api_attribution(lottery):
    """
    中奖归因：按 (来源 × 策略 × 档位) 聚合历史已开奖反馈，附最新 pending 批次的撞号覆盖 KPI。

    溯源事实：预测生成时即写入 pending（预先锁定），开奖后只拿当时存下的号码结算，
    **不会重新生成号码** → 每条中奖记录都能定位到"哪一批、哪个策略、哪个档位"。

    ?lookback=N 限制回看条数（默认 2000，防大数据量卡顿）。
    """
    from ev.attribution import attribution_for_lottery

    lb = request.args.get("lookback", type=int)
    return jsonify(attribution_for_lottery(lottery, **({} if lb is None else {"lookback": lb})))


@bp.route("/api/audit/attribution_global")
@api_error_handler
def api_attribution_global():
    """六彩种归因汇总：全局 (来源,策略,档位) 合并表 + 各彩种总计。"""
    from ev.attribution import attribution_global

    lb = request.args.get("lookback", type=int)
    return jsonify(attribution_global(**({} if lb is None else {"lookback": lb})))


# ------------------------------------------------------------------ ⑧ 机制缝隙测量
@bp.route("/api/audit/gap/<lottery>")
@api_error_handler
@validate_lottery
def api_gap(lottery):
    """
    单彩种机制缝隙**状态卡**（四轨测量；与战绩榜/命中历史同一份口径）。

    ⚠️ **只读缓存，不触发测量**：一次完整测量约 17s，看板刷新不该付这个代价。
    缓存缺失 ⇒ `未测量=True` + `状态="未测量"`，前端照实显示「尚未测量」并给出
    运行命令，**不**用 0 或空表粉饰成「无缝隙」。

    状态取值：`未测量` / `未激活`（γ=0，含不可折算与 CI 含 0 两种降级）
    / `已激活`（γ≠0 且过显著门）。它是**状态卡不是战绩行**——γ=0 时出号与
    基础策略逐位相同，没有独立战绩样本。

    ?refresh=1 才强制重算（显式、人工触发，用于「刚拉完新数据」）。
    """
    from ev.mechanism.snapshot import mechanism_status_card

    if request.args.get("refresh", type=int) == 1:
        from ev.gap import measure
        measure(lottery, use_cache=False)

    # 与战绩榜/命中历史共用同一份状态卡口径（单一真源：未测量 wording、红线、从句）
    return jsonify(mechanism_status_card(lottery))


@bp.route("/api/audit/gap_global")
@api_error_handler
def api_gap_global():
    """六彩种机制缝隙快照汇总（只读缓存）。未测量的彩种单列，不与已测量混排。"""
    from ev.mechanism.snapshot import mechanism_snapshot_all

    return jsonify(mechanism_snapshot_all())


@bp.route("/api/audit/robustness")
@api_error_handler
def api_robustness():
    """策略鲁棒性：盈利排序策略在「热度失真 / 拥挤度缺失」下相对随机基线还剩多少优势。

    ★ 只读夜间缓存（`health_digest.refresh_strategy_robustness` 落盘），本接口**不触发**
      那次约 40 秒的测量 —— 页面刷新不该等于重跑一次压力测试。

    ★ 三条诚实纪律（对应 `web/health_digest.strategy_robustness` 的口径）：
      ① 缓存缺失或陈旧 ⇒ 回 `可用=False` + 原因，**绝不补 0**（0% 与「没测」是两件事）；
      ② 「无优势」也是结论而不是故障（该彩种没有可用的分奖杠杆），前端按类型渲染、不进红框；
      ③ 保留率的分母是**基准优势**，基准优势 ≤0 时上游返回 `None` ⇒ 这里照传 `None`，
         不做「除以接近 0」的除法去凑一个看起来很大的百分比。
    """
    from web.health_digest import strategy_robustness

    return jsonify(strategy_robustness())


@bp.route("/api/audit/glossary")
@api_error_handler
def api_glossary():
    """本页术语与卡片口径（文案单一真源）。

    ★ 前端的名词速查抽屉、术语 hover 气泡、每张卡的「在测什么 / 名词 / 口径与诚实边界」
      三段都从这里取。**不许**在模板或 JS 里手抄这些文案——并发分支会各自过期。
    """
    from web.audit_glossary import glossary_payload

    return jsonify(glossary_payload())
