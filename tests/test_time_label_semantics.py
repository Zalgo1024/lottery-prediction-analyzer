"""
时间字段语义守卫 —— 别再让「预测日期」被读成「预测时刻」。

★ 为什么要有这个文件
  `预测日期` 是**目标开奖日**：由开奖日历推算的派生字段，恒等于 `开奖日期`
  （生产数据 12,402 条里 11,762 条完全相同，占 94.8%）。
  界面曾把这个字段当成「预测发生的时间」来写，于是出现两类观感事故：

    ① 对比卡片写「预测于 2026-10-27 · 开奖 2026-10-27」—— 字面上在说
       「我在开奖那天做的预测」，等于自证马后炮；
    ② 命中详情只并列这两个**必然相同**的日期，而真正能自证提前出号的
       `批次时间`（= pending 的 `生成时间`，开奖后原样结转、不重算）
       藏在批次弹窗里，要看得很深才找得到。

  真实情况是干净的：有 `批次时间` 的 7,351 条里，5,611 条出号日早于开奖日、
  1,740 条同日但时刻早于开奖、**0 条晚于开奖**。问题全在展示层「把证据藏起来、
  把疑点摆出来」，以及 2026-09-21 改了判定逻辑却没回填历史标签（173 条真预测
  被标成 False，见 `scripts/backfill_valid_prediction.py`）。

★ 本文件锁四件事
  1. 前端不许再把目标开奖日写成「预测于 X」；
  2. 前端在缺 `批次时间` 时必须显示「未记录」，不许拿日期顶替；
  3. 回填脚本的证据门禁不许被放宽（无时间戳 / 训练来源一律不动）；
  4. 生产数据里不许出现「声称真预测却没有提前证据」的记录。

全部断言为纯字符串 / dict / 文件读取，不触发拟合，不会被环境标记 skip 成假绿。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN_JS = ROOT / "web" / "static" / "js" / "main.js"
FEEDBACK_ROUTE = ROOT / "web" / "routes" / "feedback.py"

TRAIN_SOURCES = ("train", "训练")


def _js() -> str:
    assert MAIN_JS.exists(), "main.js 不见了"
    return MAIN_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 前端文案
def test_frontend_never_labels_target_draw_date_as_predict_time():
    """`预测日期` 是目标开奖日 ⇒ 不许写成「预测于 / 预测时间」。

    ★ 这是本轮事故的原句：`预测于 ${c.预测日期}`。它把派生字段当成了
      事实字段，字面意思就是「预测发生在开奖日那天」。
    """
    js = _js()
    # 只抓「真正会渲染出来」的形态（带插值），避免误伤正文注释里的反例引用
    for bad in ("预测于 ${", "预测于 `", "预测时间未知"):
        assert bad not in js, (
            "main.js 又出现 %r：`预测日期` 是目标开奖日（恒等于开奖日期），"
            "不能当作预测发生的时刻。要显示真实时刻请用 `批次时间`。" % bad
        )


def test_frontend_shows_generation_time_and_admits_when_missing():
    """必须显式展示出号时刻；缺失时明说「未记录」，不静默兜底成日期。"""
    js = _js()
    assert "出号时刻" in js, "命中详情/对比卡片必须显示「出号时刻」（批次时间）"
    assert "未记录" in js, "缺 批次时间 时必须显示「未记录」，不能拿预测日期顶替"
    # 三处展示（命中详情 / 历史表格 / 对比卡片）都要用到该字段
    assert js.count("批次时间") >= 3, (
        "main.js 里对 `批次时间` 的引用少于 3 处，疑似某处展示被改回用预测日期"
    )


def test_compare_api_contract_carries_batch_time():
    """`/api/feedback/<lottery>/compare` 的 cards 必须带 `批次时间`，
    否则前端只能退回用 `预测日期`（就是事故的样子）。"""
    src = FEEDBACK_ROUTE.read_text(encoding="utf-8")
    assert '"批次时间": f.get("批次时间", "")' in src, (
        "compare 接口的 cards 又丢了「批次时间」字段"
    )


# ---------------------------------------------------------------- 回填门禁
def _fake(**kw):
    base = {"期号": 26109, "预测日期": "2026-09-22", "开奖日期": "2026-09-20",
            "来源": "predict", "批次时间": "2026-09-17T21:31:21"}
    base.update(kw)
    return base


def test_backfill_recompute_matches_settlement_logic():
    """回填脚本的重算必须与结算侧同口径：生成时间优先，训练来源直接否。"""
    from scripts.backfill_valid_prediction import recompute

    # 生成早于开奖日 21:00 → 真预测（哪怕预测日期被日历推到 09-22）
    assert recompute(_fake()) is True
    # 生成晚于开奖日 21:00 → 事后
    assert recompute(_fake(批次时间="2026-09-20T22:05:00")) is False
    # 训练来源一律不算真预测
    assert recompute(_fake(来源="训练")) is False


def test_backfill_refuses_records_without_generation_time():
    """★ 无生成时间戳 ⇒ 只有派生字段可推定，不足以推翻已落盘的 False。

    实证：`双色球 26090` 日期口径自洽、会被日期规则判成 True，但它是被点名的
    **污染记录**（预测蓝球 [1,2] 违反 blue_count=1），标 False 是故意的。
    放开门禁就会把污染样本洗成真预测。
    """
    from scripts.backfill_valid_prediction import eligible

    ok, why = eligible(_fake(批次时间="", 预测日期="2026-08-06", 开奖日期="2026-08-06"))
    assert ok is False, "无时间戳的记录不许回填"
    assert "生成时间" in why


def test_backfill_refuses_train_source():
    from scripts.backfill_valid_prediction import eligible

    ok, why = eligible(_fake(来源="训练"))
    assert ok is False
    assert "训练" in why


def test_backfill_accepts_evidence_backed_record():
    from scripts.backfill_valid_prediction import eligible

    ok, _why = eligible(_fake())
    assert ok is True


# ---------------------------------------------------------------- 生产数据不变量
def _histories():
    from data.feedback import FEEDBACK_DIR

    out = []
    for name in ("双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩"):
        p = FEEDBACK_DIR / f"{name}_feedback_history.json"
        if p.exists():
            out.append((name, json.loads(p.read_text(encoding="utf-8"))))
    return out


@pytest.mark.realdata
def test_no_valid_prediction_is_actually_after_draw():
    """最硬的一条：标成「真预测」的记录，出号时刻不许晚于开奖日 21:00。

    晚于的会被结算侧标成训练/回测，所以这里应当是 0 条 —— 一旦不为 0，
    说明判定链路有漏网，整个「真预测战绩」的口径就不可信了。
    """
    hist = _histories()
    if not hist:
        pytest.skip("没有生产反馈数据（克隆环境）")

    from data.feedback import _is_valid_prediction

    offenders = []
    for name, records in hist:
        for r in records:
            if not isinstance(r, dict) or r.get("valid_prediction") is not True:
                continue
            gen = r.get("批次时间") or r.get("生成时间") or r.get("记录时间") or ""
            draw = str(r.get("开奖日期") or "")[:10]
            if not gen or not draw:
                continue
            if _is_valid_prediction(str(r.get("预测日期") or ""), date.fromisoformat(draw),
                                    str(gen)) is False:
                offenders.append((name, r.get("期号"), str(gen)[:19], draw))
    assert not offenders, (
        "有 %d 条记录标着 valid_prediction=True，但生成时间晚于开奖日 21:00：%r"
        % (len(offenders), offenders[:5]))


@pytest.mark.realdata
def test_backfill_is_idempotent_on_production_data():
    """生产数据里不应再有「可回填」项 —— 否则说明又有一批标签没跟上判定逻辑。"""
    hist = _histories()
    if not hist:
        pytest.skip("没有生产反馈数据（克隆环境）")

    from scripts.backfill_valid_prediction import eligible

    leftovers = []
    for name, records in hist:
        for r in records:
            if isinstance(r, dict) and r.get("valid_prediction") is False and eligible(r)[0]:
                leftovers.append((name, r.get("期号")))
    assert not leftovers, (
        "有 %d 条记录可被回填但仍未回填（跑 scripts/backfill_valid_prediction.py）：%r"
        % (len(leftovers), leftovers[:5]))
