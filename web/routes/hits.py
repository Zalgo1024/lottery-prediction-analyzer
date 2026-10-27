"""
命中历史页面路由

- GET /hits                  命中历史页面
- GET /api/hits              查询命中记录（支持 lottery / limit / min_prize / type /
                             red_hits / blue_hits / total_hits / min_amount / strategy 过滤）

返回里除 `records` 外还带三个计数，供页面渲染「xx 注 / xxxx（总数）注」：

- `total`     命中本次筛选条件的**总注数**（分页前，即用户真正筛出了多少注）
- `universe`  全部命中注数（**不加任何筛选**，全彩种）——分母「总数」
- `returned`  本次实际返回的条数（受 limit 限制）

另带**累计中奖金额**，成对给出两个口径（页面必须点名口径，不许只报一个数）：

- `amount_sum` / `amount_sum_unknown`   当前筛选口径：可确定金额的注求和 + 金额未知的注数
- `amount_all` / `amount_all_unknown`   全部命中口径（不筛选）：同上

★ 口径 = 各注「单注奖金」之和（税前，不是实得；浮动奖取当期实际单注奖金）。
★ 浮动奖级当期金额读不到时**不按 0 计入**（会让合计系统性偏低且看不出来），
  而是单独计数由前端披露。
"""

from flask import Blueprint, jsonify, render_template, request
from web.utils import api_error_handler, feedback_record_view
from web.validators import validate_lottery
from config import LOTTERY_CONFIG

bp = Blueprint("hits", __name__)


@bp.route("/hits")
def page():
    return render_template("hits.html")


@bp.route("/api/hits/filters")
@api_error_handler
def api_hits_filters():
    """命中条件下拉的全部内容（术语与规则单一真源在 `web/hit_filters.py`）。

    参数：lottery = 全部 | 双色球 | 大乐透 | 排列5 | 福彩3D | 排列3 | 七星彩
    返回：
      通用      总命中 ≥ N（上界按该彩种的理论最大命中数，3D 只有 1~3）
      专属      该彩种规则下的分区命中结构；「全部」时为 null（跨彩种不可比）
      分区      该彩种的分区名与每区应选个数
      位置对照  数字型的「第N位 = 百位/十位/个位」对照串（仅数字型非空）
      口径说明  两个组各自的含义
    """
    from web.hit_filters import options_payload

    lottery = request.args.get("lottery", "全部", type=str)
    if lottery not in ("全部",) and lottery not in LOTTERY_CONFIG:
        return jsonify({"error": "未知彩种：%s" % lottery}), 400
    return jsonify(options_payload(lottery))


@bp.route("/api/hits")
@api_error_handler
def api_hits():
    """
    返回命中记录列表。
    参数：
      lottery:    全部 | 双色球 | 大乐透 | 排列5 | 福彩3D | 排列3 | 七星彩（默认"全部"）
      limit:      最多返回条数（默认 100，0=不限；可选 5/10/20/50/100/200/500/0）
      min_prize:  最小中奖等级（默认"全部"，可填 一等|二等|三等|四等|五等|六等）
      type:       全部 | 预测 | 训练
      strategy:   策略名（默认"全部" ⇒ 逐位保持既有行为）。
                  按 normalize_strategy_name 归一时比对（ML(logistic)/统计训练(Wxx) → ML策略），
                  便于「低拥挤策略」这类研究性独立入口单独筛出来看。
      red_hits:   最小红球命中数（仅对双色球/大乐透生效；数字型恒为真）
      blue_hits:  最小蓝球命中数（仅对双色球/大乐透生效；数字型恒为真）
      total_hits: 最小总命中数（对所有彩种生效，红蓝型=红+蓝）
      zones:      命中条件（新口径，推荐；**与上面三个字段并存，可叠加**）。
                  格式 `名称:最小命中`，多项用逗号连接，名称可取：
                    · 单分区名：红球 / 蓝球 / 第1位 … 第7位
                    · 逻辑组名：总（全部分区合计）/ 前区（七星彩第1~6位）/ 后区（七星彩第7位）
                  例：`红球:4,蓝球:1`、`第7位:1`、`前区:3`、`总:5`
                  ★ 分区名不认识或格式不合法 ⇒ 返回 400，绝不静默忽略
                    （「全部」彩种只允许 `总`，否则数字型记录会被按红球条件静默筛掉）。
      min_amount: 单注奖金下限（元，默认不限）。按「这一注能中多少钱」筛选；
                  浮动奖级若读不到当期实际奖金（金额未知）则不参与金额筛选，
                  其条数在响应 `amount_unknown` 里单独给出，避免静默漏计。
      date_from:  时间下界（按结算/评估时间），支持 YYYY / YYYY-MM / YYYY-MM-DD，
                  短粒度自动取该月/该年第一天；非法值忽略（不筛选）。
      date_to:    时间上界，同上格式，短粒度自动取该月/该年最后一天。
      sort_by:    排序字段（time|prize|red_hits|blue_hits|total_hits|amount；默认 time）
      sort_order: 排序方向（asc|desc；默认 desc）
    """
    from datetime import datetime
    import calendar

    from data.feedback import load_feedback_history, normalize_strategy_name
    from data.prize_table import prize_payout
    from data.schema import is_redblue
    from ev.batch import batch_fields, group_batches

    lottery = request.args.get("lottery", "全部")
    limit = request.args.get("limit", 100, type=int)
    min_prize = request.args.get("min_prize", "全部")
    type_filter = request.args.get("type", "全部")
    strategy = request.args.get("strategy", "全部")
    red_hits_min = request.args.get("red_hits", "", type=str)
    blue_hits_min = request.args.get("blue_hits", "", type=str)
    total_hits_min = request.args.get("total_hits", "", type=str)
    min_amount = request.args.get("min_amount", "", type=str)
    sort_by = request.args.get("sort_by", "time")
    sort_order = request.args.get("sort_order", "desc")

    scope = set(LOTTERY_CONFIG.keys()) if lottery == "全部" else {lottery}

    # 等级排序权重 —— 三套命名体系并存，缺一会被 .get(x,99) 当成"未中"排序：
    #   乐透型  双色球: 一等~六等
    #   乐透型  大乐透: 一等~九等（历史上 6/7/8/9 级四种，由规则表生成）
    #   七星彩          : 一等奖~六等奖
    #   数字型(3D/排列3/排列5): 直选 / 组选3 / 组选6 / 未中
    # 「X等」部分从 ev.profit.rules.grade_rank_names 取（单一真源，不手写副本）。
    from ev.profit.rules import grade_rank_names

    prize_rank = {g: i + 1 for i, g in enumerate(grade_rank_names())}
    prize_rank.update({
        "一等奖": 1, "二等奖": 2, "三等奖": 3, "四等奖": 4, "五等奖": 5, "六等奖": 6,
        "直选": 1, "组选3": 2, "组选6": 3,
        "未中": 99,
    })
    min_rank = prize_rank.get(min_prize, 0) if min_prize != "全部" else 0

    def _int_param(value, default=None):
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    red_min = _int_param(red_hits_min)
    blue_min = _int_param(blue_hits_min)
    total_min = _int_param(total_hits_min)

    # 命中条件（新口径，替代 red_hits/blue_hits/total_hits 三件套）：
    #   zones=红球:4,蓝球:1  ⇒ 红球命中 ≥4 且 蓝球命中 ≥1
    #   zones=第7位:1 / 前区:3 / 总:4
    # 解析或分区名不认识时**直接 400**，不许静默忽略——否则筛选悄悄失效、页面显示
    # 「筛出 0 注」而用户以为真的没有，是本项目最忌讳的 fail-silent。
    from web.hit_filters import match_condition, group_hits, parse_condition, validate_condition

    try:
        zone_cond = parse_condition(request.args.get("zones", "", type=str))
        # 「全部」彩种只允许「总」：同一个「红球 ≥4」在数字型上根本不存在，
        # 跨彩种套用会让数字型记录被静默筛掉。
        validate_condition(zone_cond, "" if lottery == "全部" else lottery)
    except ValueError as exc:
        return jsonify({
            "error": str(exc), "records": [], "total": 0, "universe": 0,
            "returned": 0, "amount_sum": 0, "amount_all": 0,
        }), 400

    def _float_param(value, default=None):
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    amount_min = _float_param(min_amount)
    if amount_min is not None and amount_min <= 0:
        amount_min = None   # 「奖金 ≥ 0 元」= 没有任何约束，等价于不筛选

    def _date_bound(value, which):
        """'2026-09' → 月初/月末；'2026-09-05' → 当日；'2026' → 年初/年末。非法返回 ''。"""
        s = (value or "").strip()
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                dt = datetime.strptime(s, fmt)
            except ValueError:
                continue
            if fmt == "%Y-%m-%d":
                return s
            if fmt == "%Y-%m":
                last = calendar.monthrange(dt.year, dt.month)[1]
                day = last if which == "end" else 1
                return f"{dt.year:04d}-{dt.month:02d}-{day:02d}"
            return f"{dt.year:04d}-12-31" if which == "end" else f"{dt.year:04d}-01-01"
        return ""

    date_from = _date_bound(request.args.get("date_from", "", type=str), "start")
    date_to = _date_bound(request.args.get("date_to", "", type=str), "end")

    universe = 0         # 全部命中注数（不加任何筛选，全彩种）——「总数」分母
    matched = 0          # 通过全部筛选条件的注数（分页前）
    amount_unknown = 0   # 浮动奖级当期金额未知 → 未参与金额筛选的注数（诚实计数）
    # 「累计中奖金额」两个口径同时给：
    #   amount_all*  全部命中注（= universe 口径，不随筛选变）
    #   amount_sum*  通过全部筛选的注（= total 口径，跟着筛选走）
    # ★ 金额未知（浮动奖级读不到当期实际单注奖金）的注**不得按 0 计入**，
    #   否则合计会系统性偏低且看不出来 ⇒ 单独计数，由前端显式披露。
    amount_all = 0.0
    amount_all_unknown = 0
    amount_sum = 0.0
    amount_sum_unknown = 0
    records = []
    # 总是遍历全部彩种：即使只看某一彩种，也要能给出「全部命中总数」这个稳定分母
    for name in LOTTERY_CONFIG:
        in_scope = name in scope
        rb = is_redblue(name)
        try:
            history = load_feedback_history(name, lookback=None)
        except Exception:
            continue
        # 批次归组：一次出号（一条 pending）= 一个批次。
        # 用 id(record) 建索引避免按值匹配（同一批次的号码可能完全相同）。
        rec_group = {}
        for _g in group_batches(history, name):
            for _r in _g.records:
                rec_group[id(_r)] = _g
        for h in history:
            prize = h.get("中奖等级", "未中")
            if prize == "未中":
                continue
            universe += 1                      # 先记总数，再判是否在筛选范围内

            # 单注奖金在这里算一次，「全部」与「当前筛选」两个合计共用同一份。
            # prize_payout 内部有按彩种的 TTL 缓存，重复调用不读盘。
            payout = prize_payout(name, prize, h.get("期号"))
            _amt = payout.get("金额") if isinstance(payout, dict) else None
            if _amt is None:
                amount_all_unknown += 1        # 浮动奖级无当期数据：单独计数，不当 0
            else:
                try:
                    amount_all += float(_amt)
                except (TypeError, ValueError):
                    amount_all_unknown += 1

            if not in_scope:
                continue
            # 时间筛选：按结算（评估）时间的日期部分，ISO 字符串可直接比较
            if date_from or date_to:
                rec_day = str(h.get("评估时间") or "")[:10]
                if not rec_day:
                    continue
                if date_from and rec_day < date_from:
                    continue
                if date_to and rec_day > date_to:
                    continue
            # 策略筛选：默认「全部」⇒ 与加这个参数之前逐位一致。
            # 用归一后的名字比对，ML(logistic)/统计训练(Wxx) 才会并进 ML策略。
            if strategy != "全部" and normalize_strategy_name(h.get("策略", "")) != strategy:
                continue
            grp = rec_group.get(id(h))
            if min_rank and prize_rank.get(prize, 99) > min_rank:
                continue
            valid = h.get("valid_prediction", True)
            # 类型筛选：预测=真预测(开奖前生成)；训练=马后炮(开奖后回测)
            if type_filter == "预测" and not valid:
                continue
            if type_filter == "训练" and valid:
                continue

            red_hit = h.get("红球命中", 0)
            blue_hit = h.get("蓝球命中", 0)
            total_hit = h.get("总命中", 0)

            # 命中数筛选
            if rb:
                if red_min is not None and red_hit < red_min:
                    continue
                if blue_min is not None and blue_hit < blue_min:
                    continue
            # 总命中筛选对所有彩种生效；红蓝型若没有总命中字段则自行计算
            effective_total = total_hit if total_hit else (red_hit + blue_hit)
            if total_min is not None and effective_total < total_min:
                continue
            # 命中条件（分区/逻辑组）：用归一化后的逐分区命中比较，
            # 3D/排3/排5 的「百位/十位/个位」老键名由 group_hits 内部归一化，不会被筛漏。
            if zone_cond and not match_condition(zone_cond, group_hits(h, name)):
                continue

            # 奖金筛选：金额未知（浮动且无当期数据）不参与筛选，只单独计数，
            # 否则会被静默当成「不满足」而低估筛选结果。
            if amount_min is not None:
                if _amt is None:
                    amount_unknown += 1
                    continue
                if float(_amt) + 1e-9 < amount_min:
                    continue

            matched += 1
            if _amt is None:
                amount_sum_unknown += 1        # 入选但金额未知：不能让合计悄悄少一笔
            else:
                try:
                    amount_sum += float(_amt)
                except (TypeError, ValueError):
                    amount_sum_unknown += 1
            records.append({
                "lottery": name,
                "期号": h.get("期号"),
                "预测日期": h.get("预测日期", ""),
                "开奖日期": h.get("开奖日期", ""),
                "评估时间": h.get("评估时间", ""),
                "来源": h.get("来源", "predict"),
                "策略": h.get("策略", ""),
                # P5 机制字段：出号当时的机制状态（γ / 倾斜），
                # 供命中详情展示「这一注出号时机制缝隙处于什么状态」。
                # 旧记录没有这些键 ⇒ 全部为 None，前端据此隐藏该行，不粉饰。
                "机制γ": h.get("机制γ"),
                "机制倾斜": h.get("机制倾斜"),
                "机制测量时间": (h["缝隙快照"].get("测量时间")
                                if isinstance(h.get("缝隙快照"), dict) else None),
                # 低拥挤策略元数据：出号当时的 λ / 门控分位 / 覆盖键 / 循环性声明。
                # 旧记录没有这个键 ⇒ 前端隐藏该行，不粉饰。
                "低拥挤": h.get("低拥挤"),
                "预测红球": h.get("预测红球", []),
                "预测蓝球": h.get("预测蓝球", []),
                "实际红球": h.get("实际红球", []),
                "实际蓝球": h.get("实际蓝球", []),
                "红球命中": red_hit,
                "蓝球命中": blue_hit,
                "总命中": total_hit,
                "中奖等级": prize,
                "奖金": payout,
                "valid_prediction": valid,
                "记录类型": "预测" if valid else "训练",
                "view": feedback_record_view(name, h),
                "is_redblue": rb,
                **batch_fields(h, grp),
            })

    # 排序
    sort_key = None
    if sort_by == "prize":
        sort_key = lambda x: (prize_rank.get(x.get("中奖等级", "未中"), 99), x.get("评估时间", ""))
    elif sort_by == "red_hits":
        sort_key = lambda x: x.get("红球命中", 0)
    elif sort_by == "blue_hits":
        sort_key = lambda x: x.get("蓝球命中", 0)
    elif sort_by == "total_hits":
        sort_key = lambda x: x.get("总命中", 0) or (x.get("红球命中", 0) + x.get("蓝球命中", 0))
    elif sort_by == "amount":
        sort_key = lambda x: _amount_of(x.get("奖金"))
    else:  # time
        sort_key = lambda x: x.get("评估时间", "")

    reverse = sort_order != "asc"
    records.sort(key=sort_key, reverse=reverse)
    if limit and limit > 0:
        records = records[:limit]
    returned = len(records)

    return jsonify({
        "total": matched,          # 命中本次筛选的总注数（分页前）→ 分子
        "universe": universe,      # 全部命中注数（不加筛选）→ 分母「总数」
        "returned": returned,      # 实际返回条数（已在 limit 截断之后）
        "limit": limit,
        "amount_unknown": amount_unknown,
        # 累计中奖金额 = 各注「单注奖金」之和（税前、非实得；浮动奖取当期实际单注奖金）。
        # 两套口径成对给出，前端必须点名口径后再显示，不能只报一个数让人以为通用。
        "amount_sum": round(amount_sum, 2),              # 当前筛选：可确定金额的注合计
        "amount_sum_unknown": amount_sum_unknown,        # 当前筛选：金额未知的注数（未计入）
        "amount_all": round(amount_all, 2),              # 全部命中（不筛选）合计
        "amount_all_unknown": amount_all_unknown,        # 全部命中：金额未知的注数（未计入）
        "lottery": lottery,
        "strategy": strategy,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "date_from": date_from,
        "date_to": date_to,
        "records": records,
    })


@bp.route("/api/hits/batch-detail")
@api_error_handler
def api_batch_detail():
    """
    批次详情：某一「第 X 批」的出号时间 + 该批出的**全部号码**（按出号顺序，含未中奖的注）。

    参数：
      lottery:  彩种名（必填）
      batch_id: 批次号（/api/hits 记录里的 `批次号` 字段，必填）

    一个批次 = 一次出号任务 = N 条反馈记录（含未中奖的），所以整批号码
    不需要额外落盘，按批次归组还原即可。出号时间：新数据精确到秒；
    历史数据只有预测日期（日级），`出号时间精确=false` 时前端需注明。
    """
    from data.feedback import load_feedback_history
    from ev.batch import batch_detail, group_batches

    lottery = request.args.get("lottery", "")
    batch_id = request.args.get("batch_id", "")
    if lottery not in LOTTERY_CONFIG:
        return jsonify({"error": f"未知彩种：{lottery}"}), 400
    if not batch_id:
        return jsonify({"error": "缺少 batch_id 参数"}), 400
    try:
        history = load_feedback_history(lottery, lookback=None)
    except Exception as e:
        return jsonify({"error": f"读取反馈记录失败：{e}"}), 500
    for g in group_batches(history, lottery):
        if g.batch_id == batch_id:
            return jsonify(batch_detail(g, lottery))
    return jsonify({"error": f"找不到批次 {batch_id}（记录可能尚未写入或已清理）"}), 404


def _amount_of(payout) -> float:
    """排序用：取单注奖金数值；金额未知（浮动且无当期数据）记为 -1（排到最后）。"""
    if not isinstance(payout, dict):
        return -1.0
    amt = payout.get("金额")
    try:
        return float(amt) if amt is not None else -1.0
    except (TypeError, ValueError):
        return -1.0
