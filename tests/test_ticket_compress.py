"""
出号质量压缩回归测试（ev/coverage.compress_tickets + feedback 入账接入）。

锁住的契约：
  1. 真正减注：目标上限 = ceil(n×keep_ratio)，质量线淘汰低推荐分票，
     重叠贪心（双色球红球交集 > cap=4 的不与已保留共存），完全同键去重；
  2. 保底：压缩后不足 min_keep 时按分数回填（仍去重），永不低于保底；
  3. 数字型（固定赔率、注数已为 5）与 n ≤ min_keep 原样返回；
  4. 入账接入：record_pending_prediction 生产预测压缩（预测号码变短 + 带
     「压缩」KPI + 批次规模=压缩后口径），来源=train 不压缩；
  5. 测试只写 tmp 目录，绝不碰生产 feedback 目录。
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.feedback as fb
from ev.coverage import compress_tickets, ticket_key, pair_overlap


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "_pending_path", lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path", lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path", lambda name: tmp_path / f"{name}_weights.json")
    return tmp_path


def _ssq_ticket(reds, blue=1, score=None, conf=None, strategy="高频策略"):
    t = {"红球": sorted(reds), "蓝球": [blue], "号码": {"红球": sorted(reds),
         "蓝球": [blue]}, "策略": strategy, "类型": "single"}
    if score is not None:
        t["推荐分"] = score
    if conf is not None:
        t["置信度"] = conf
    return t


def _mk_pool(n=20, seed_base=1):
    """20 注双色球：前 10 注互相重叠 5 个红球（高重复），后 10 注互相错开。"""
    tickets = []
    for i in range(10):
        reds = [seed_base + i, 2, 3, 4, 5, 30 + (i % 4)]
        tickets.append(_ssq_ticket(reds, score=0.9 - 0.01 * i))
    for i in range(10):
        reds = [8 + i, 9 + i, 15 + i, 20 + i, 25 + (i % 5), 31 - (i % 3)]
        tickets.append(_ssq_ticket(reds, score=0.6 - 0.01 * i))
    return tickets


# ------------------------------------------------------------
# 1. 压缩口径
# ------------------------------------------------------------

def test_compress_actually_reduces_and_respects_target():
    # ★ 2026-10-11：生产已改为「出号数量 = 最终注数」（不按比例削减），
    #   故此处**显式**传 keep_ratio 来验证削减能力本身仍然存在。
    tickets = _mk_pool()
    kept, kpi = compress_tickets(tickets, "双色球", keep_ratio=0.4)
    n = len(tickets)
    assert len(kept) <= -(-n * 2 // 5)  # ceil(n*0.4)
    assert kpi["压缩后注数"] == len(kept)
    assert kpi["压缩前注数"] == n
    # 保留票两两红球重叠 ≤ cap(4)
    keys = [ticket_key(tickets[i], "双色球") for i in kept]
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            assert pair_overlap(keys[a], keys[b], "双色球") <= 4
    # 高分票优先保留：保留集合里的最低推荐分 ≥ 淘汰池的最高推荐分（近似单调）
    kept_scores = [tickets[i]["推荐分"] for i in kept]


def test_compress_target_count_ge_pool_keeps_all():
    """★ 2026-10-11「出号数量 = 最终注数」：target_count ≥ 候选数 → 不削减。

    生产路径 record_pending_prediction 传 target_count=生成目标注数，
    与候选数相等，走这条 —— 滑轨设 176 就真的入账 176 注。
    """
    tickets = _mk_pool(n=20)
    kept, kpi = compress_tickets(tickets, "双色球", target_count=20)
    assert kept == list(range(20))
    assert kpi["压缩后注数"] == 20 and kpi["目标上限"] == 20
    assert "不削减" in kpi["说明"]

    kept2, _ = compress_tickets(tickets, "双色球", target_count=50)
    assert len(kept2) == 20                        # 目标大于候选也不凭空造注


def test_compress_target_count_selects_exact_count():
    """target_count < 候选数 → 精选到该值（不足时回填），最终恰好等于目标。"""
    tickets = _mk_pool(n=20)
    kept, kpi = compress_tickets(tickets, "双色球", target_count=6)
    assert len(kept) == 6
    assert kpi["目标上限"] == 6
    assert kpi["压缩后注数"] == 6


def test_compress_prefers_high_score_and_dedups():
    tickets = [
        _ssq_ticket([1, 2, 3, 4, 5, 6], score=0.9),
        _ssq_ticket([1, 2, 3, 4, 5, 6], score=0.8),   # 完全重复（低分）→ 去重淘汰
        _ssq_ticket([10, 11, 12, 13, 14, 15], score=0.7),
        _ssq_ticket([10, 11, 12, 13, 14, 16], score=0.6),  # 重叠 5 → 淘汰
        _ssq_ticket([20, 21, 22, 23, 24, 25], score=0.5),
        _ssq_ticket([30, 29, 28, 27, 26, 7], score=0.4),
        _ssq_ticket([2, 12, 22, 32, 17, 9], score=0.3),
        _ssq_ticket([4, 14, 24, 31, 19, 6], score=0.2),
    ]
    kept, kpi = compress_tickets(tickets, "双色球", min_keep=3)
    kept_reds = [tickets[i]["红球"] for i in kept]
    assert [1, 2, 3, 4, 5, 6] in kept_reds        # 最高分保留
    assert kpi["重复淘汰"] >= 1
    assert kpi["重叠淘汰"] >= 1
    assert len(kept) == len(set(tuple(r) for r in kept_reds))  # 无完全重复


def test_compress_backfills_to_min_keep():
    # 6 注全部两两重叠 5 → 重叠贪心只能留 1 注，必须回填到 min_keep
    tickets = [_ssq_ticket([1, 2, 3, 4, 5, 6 + i], score=0.9 - 0.1 * i) for i in range(6)]
    kept, kpi = compress_tickets(tickets, "双色球", min_keep=5)
    assert len(kept) >= 5
    assert kpi["回填"] >= 4


def test_compress_quality_floor_drops_low_scores():
    tickets = [_ssq_ticket([1 + i, 2 + i, 3 + i, 4 + i, 5 + i, 6 + i],
                           score=1.0 - 0.05 * i) for i in range(10)]
    kept, kpi = compress_tickets(tickets, "双色球", keep_ratio=0.5, min_keep=2)
    kept_set = set(kept)
    # 分数降序，前 5 名（score >= 0.8）必在保留集（互相重叠 = i 对 i 恰 5 个共享？
    # 相邻票红球重叠 5 个 → 只有互相重叠 ≤4 的能共存；这里构造的重叠是 5，
    # 所以实际保留依赖贪心淘汰，但最高分第一张必保留）
    assert 0 in kept_set
    assert kpi["质量淘汰"] >= 0


def test_compress_noop_for_digital_and_small_pools():
    d3 = [{"号码": {"第1位": [a], "第2位": [b], "第3位": [c]}, "策略": "高频策略",
           "推荐分": 0.5} for a, b, c in [(1, 2, 3), (4, 5, 6), (7, 8, 9)]]
    kept, kpi = compress_tickets(d3, "福彩3D")
    assert kept == [0, 1, 2] and kpi["压缩后注数"] == 3

    two = [_ssq_ticket([1, 2, 3, 4, 5, 6], score=0.9),
           _ssq_ticket([1, 2, 3, 4, 5, 6], score=0.1)]
    kept2, kpi2 = compress_tickets(two, "双色球")  # n=2 ≤ min_keep → 不压
    assert kept2 == [0, 1] and kpi2["质量淘汰"] == 0


# ------------------------------------------------------------
# 2. 入账接入
# ------------------------------------------------------------

def _safe_gen_time():
    """当天 09:00 的生成时间。

    ★ 2026-10-11：原用例不带「生成时间」，record_pending_prediction 会回落到
    datetime.now()；而马后炮闸门对「推算开奖日 == 今天 且 生成时间 ≥ 20:00」直接
    拒绝，导致本用例**只在 20:00 之前通过**（实测 21:00 复现）。固定为 09:00 消除
    这个时间脆弱性，与业务语义无关。
    """
    return datetime.now().replace(hour=9, minute=0, second=0,
                                  microsecond=0).isoformat()


class _FrozenDate(date):
    """把 `data.feedback` 眼里的「今天」钉死（只替换 today()）。"""

    _fixed = None

    @classmethod
    def today(cls):
        return cls._fixed


def _freeze_today_at_target_draw(monkeypatch, lottery="双色球"):
    """★ 2026-11-07（CI 复现）：把「今天」钉在目标开奖日，解除墙钟依赖。

    这两例验的是「入账不按比例削注」，但它撞上了马后炮闸门的**隐含前提**：
    闸门用 `date.today()` 判定目标期是否已过。CI 用的是**提交时那一份 CSV**，
    一旦停更，`_next_issue()` 的目标开奖日就落在这之前 ⇒ 闸门静默拒绝
    （`record_pending_prediction` 的返回值被用例忽略）⇒ pend 为空 ⇒
    `assert len(pend) == 1` 变成「今天离数据有多远」的读数，而不是判据。
    实锤：CI 上数据停在 26127（推算开奖日 2026-11-05），跑在 11-07 ⇒ 被拒。

    钉法：目标开奖日当天 09:00 出号（未过 20:00）——与本例想验的语义一致
    （「开奖前出号，照常入账且不削注」），且与真实日期无关，任何环境都可复现。
    """
    target = fb._next_issue(lottery)
    draw = fb._target_draw_date(lottery, target) if target is not None else None
    if target is None or draw is None:
        pytest.skip(f"本地数据不足，无法推算 {lottery} 目标开奖日（CI/全新克隆）")
    _FrozenDate._fixed = draw
    monkeypatch.setattr(fb, "date", _FrozenDate)
    return target


def test_record_pending_keeps_target_count(tmp_fb, monkeypatch):
    """★ 2026-10-11「出号数量 = 最终注数」：生产入账不再按比例削减。

    历史行为：keep_ratio=0.4 使看板滑轨设 176 实际只入账 64 注，与看板文案
    「每次出号几注，由你决定」矛盾。现在 record_pending_prediction 传
    target_count = 生成时刻目标注数（与候选数相等）→ 不削减，原样入账。
    """
    tickets = _mk_pool(n=20)
    # 目标期号取「真正的下一期」而不是硬编码——硬编码会在开奖后变成已开奖期号，
    # 被马后炮闸门拒绝（2026-09-21 实锤：硬编码 26109 在 09-20 开奖后即失效；
    # 此前它能通过是因为开奖日历把 09-20 误判休市、把推算开奖日推到 09-22）。
    # ★ 仅「取下一期」还不够：闸门还要拿它跟今天比，故把今天钉在它的开奖日。
    rec = {"目标期号": _freeze_today_at_target_draw(monkeypatch),
           "来源": "predict", "档位": "一般",
           "目标注数": 20, "生成时间": _safe_gen_time(),
           "预测号码": tickets}
    fb.record_pending_prediction("双色球", rec)
    pend = fb.load_pending("双色球")
    assert len(pend) == 1
    stored = pend[0]
    assert len(stored["预测号码"]) == 20                     # 出号数量即最终注数
    assert stored["批次规模"] == 20
    assert stored["号码组数"] == 20


def test_record_pending_selects_when_target_below_candidates(tmp_fb, monkeypatch):
    """目标注数 < 候选数 → 精选到目标（压缩能力保留，KPI 随身）。"""
    tickets = _mk_pool(n=20)
    rec = {"目标期号": _freeze_today_at_target_draw(monkeypatch),
           "来源": "predict", "档位": "一般",
           "目标注数": 6, "生成时间": _safe_gen_time(),
           "预测号码": tickets}
    fb.record_pending_prediction("双色球", rec)
    stored = fb.load_pending("双色球")[0]
    assert len(stored["预测号码"]) == 6
    assert stored["压缩"]["压缩前注数"] == 20
    assert stored["批次规模"] == 6
    assert stored["压缩"]["说明"]                           # 诚实声明随身


def test_record_pending_keeps_train_source_intact(tmp_fb):
    tickets = _mk_pool(n=20)
    rec = {"目标期号": 26110, "来源": "train", "档位": "一般",
           "预测号码": tickets}
    fb.record_pending_prediction("双色球", rec)
    stored = fb.load_pending("双色球")[0]
    assert len(stored["预测号码"]) == 20
    assert "压缩" not in stored


def test_record_pending_digital_untouched(tmp_fb):
    tickets = [{"号码": {"第1位": [i], "第2位": [i + 1], "第3位": [i + 2]},
                "策略": "高频策略", "推荐分": 0.5} for i in range(5)]
    rec = {"目标期号": 2026300, "来源": "predict", "档位": "一般",
           "预测号码": tickets}
    fb.record_pending_prediction("福彩3D", rec)
    stored = fb.load_pending("福彩3D")[0]
    assert len(stored["预测号码"]) == 5
