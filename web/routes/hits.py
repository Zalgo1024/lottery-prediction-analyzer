"""
命中历史页面路由

- GET /hits                  命中历史页面
- GET /api/hits              查询命中记录（支持 lottery / limit / min_prize / type /
                             red_hits / blue_hits / total_hits / min_amount 过滤）

返回里除 `records` 外还带三个计数，供页面渲染「xx 注 / xxxx（总数）注」：

- `total`     命中本次筛选条件的**总注数**（分页前，即用户真正筛出了多少注）
- `universe`  全部命中注数（**不加任何筛选**，全彩种）——分母「总数」
- `returned`  本次实际返回的条数（受 limit 限制）
"""

from flask import Blueprint, jsonify, render_template, request
from web.utils import api_error_handler, feedback_record_view
from web.validators import validate_lottery
from config import LOTTERY_CONFIG

bp = Blueprint("hits", __name__)


@bp.route("/hits")
def page():
    return render_template("hits.html")


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
      red_hits:   最小红球命中数（仅对双色球/大乐透生效；数字型恒为真）
      blue_hits:  最小蓝球命中数（仅对双色球/大乐透生效；数字型恒为真）
      total_hits: 最小总命中数（对所有彩种生效，红蓝型=红+蓝）
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

    from data.feedback import load_feedback_history
    from data.prize_table import prize_payout
    from data.schema import is_redblue
    from ev.batch import batch_fields, group_batches

    lottery = request.args.get("lottery", "全部")
    limit = request.args.get("limit", 100, type=int)
    min_prize = request.args.get("min_prize", "全部")
    type_filter = request.args.get("type", "全部")
    red_hits_min = request.args.get("red_hits", "", type=str)
    blue_hits_min = request.args.get("blue_hits", "", type=str)
    total_hits_min = request.args.get("total_hits", "", type=str)
    min_amount = request.args.get("min_amount", "", type=str)
    sort_by = request.args.get("sort_by", "time")
    sort_order = request.args.get("sort_order", "desc")

    scope = set(LOTTERY_CONFIG.keys()) if lottery == "全部" else {lottery}

    # 等级排序权重 —— 三套命名体系并存，缺一会被 .get(x,99) 当成"未中"排序：
    #   乐透型  双色球: 一等~六等
    #   乐透型  大乐透: 一等~九等（2023九级）
    #   七星彩          : 一等奖~六等奖
    #   数字型(3D/排列3/排列5): 直选 / 组选3 / 组选6 / 未中
    prize_rank = {
        "一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6,
        "七等": 7, "八等": 8, "九等": 9,
        "一等奖": 1, "二等奖": 2, "三等奖": 3, "四等奖": 4, "五等奖": 5, "六等奖": 6,
        "直选": 1, "组选3": 2, "组选6": 3,
        "未中": 99,
    }
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

            # 「这一注能中多少钱」：固定奖级直接给金额；浮动奖级（双/大/七星彩一、二等）
            # 优先读当期实际单注奖金，读不到则标注浮动+名义上限。
            payout = prize_payout(name, prize, h.get("期号"))
            # 奖金筛选：金额未知（浮动且无当期数据）不参与筛选，只单独计数，
            # 否则会被静默当成「不满足」而低估筛选结果。
            if amount_min is not None:
                amt = payout.get("金额") if isinstance(payout, dict) else None
                if amt is None:
                    amount_unknown += 1
                    continue
                if float(amt) + 1e-9 < amount_min:
                    continue

            matched += 1
            records.append({
                "lottery": name,
                "期号": h.get("期号"),
                "预测日期": h.get("预测日期", ""),
                "开奖日期": h.get("开奖日期", ""),
                "评估时间": h.get("评估时间", ""),
                "来源": h.get("来源", "predict"),
                "策略": h.get("策略", ""),
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
        "lottery": lottery,
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
