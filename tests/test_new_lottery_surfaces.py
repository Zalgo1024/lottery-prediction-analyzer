# -*- coding: utf-8 -*-
"""新注册彩种（七乐彩 / 快乐8）在各展示面的回归锁（2026-11-02）。

这一组测的不是数学，而是**「接入漏了一处」不会报错、只会静默退化**这类问题：

  1. `pipeline.step1.get_prize_data` 对未覆盖彩种返回单档占位「精确全中，金额 0」。
     若下游照用，统计页/看板会打出「单注期望 −2.0 元、各奖级期望：精确全中 0 元」——
     把「模型不覆盖」显示成「只有一个奖级且不派奖」。这是把未知当 0。
  2. 页面上 12 个模板曾各自硬编码 6 个彩种名：后端已接入、用户却**选不到**；
     `main.js` 的 `tsIdx()` 还会给未登记彩种返回 99 ⇒ 注数"改了保存不上"。
"""

import json
import re

import pytest

from config import LOTTERY_CONFIG
from data.loader import load_lottery
from data.schema import schema_from_cfg
from pipeline.step2_expected import expected_value, _is_placeholder_table
from pipeline.step1_probability import get_prize_data
from pipeline.step6_ev import compute_ev_series, current_period_advice

NEW = ("七乐彩", "快乐8")


# ---------------------------------------------------------------- 期望口径
@pytest.mark.parametrize("lot", NEW)
def test_step1_table_is_the_placeholder_we_think(lot):
    """前置事实：step1 确实只给了「精确全中」占位档 —— 改了这里下游判据也要跟着改。"""
    levels = get_prize_data(lot).get("levels") or []
    assert _is_placeholder_table(levels), f"{lot} 的 step1 表不再是占位档，本文件的判据需复核"


@pytest.mark.parametrize("lot,min_grades", [("七乐彩", 7), ("快乐8", 7)])
def test_expected_value_uses_real_grade_table(lot, min_grades):
    """期望必须按真实奖级表算，不能拿占位档凑出 −2.0。"""
    ev = expected_value(lot)
    rows = ev["各奖级期望明细"]
    assert len(rows) >= min_grades, f"{lot} 只算出 {len(rows)} 个奖级"
    assert all(r["prize"] != "精确全中" for r in rows)
    # 期望仍为负，但不能是"一分钱都拿不到"的 −2
    assert ev["单注期望收益"] < 0
    assert ev["单注期望收益"] > -2.0 + 1e-6
    # 名义返奖率应落在彩票的合理区间（远高于 0，且低于 1）
    gained = sum((r["expected_value"] or 0.0) for r in rows)
    assert 0.3 < gained < 2.0, f"{lot} 期望奖金 {gained} 不在合理区间"


def test_kl8_expected_value_declares_the_play():
    """快乐8 的奖级随玩法变，不说明玩法等于给了个没有前提的数字。"""
    ev = expected_value("快乐8")
    assert "玩法" in ev and "选十" in ev["玩法"]


def test_unknown_amount_grades_are_excluded_not_zeroed():
    """金额未知的档必须单列，不能当 0（当 0 偏低、按上限虚报偏高）。"""
    ev = expected_value("七乐彩")
    if ev.get("金额未知档"):
        for g in ev["金额未知档"]:
            row = next(r for r in ev["各奖级期望明细"] if r["prize"] == g)
            assert row["amount"] is None
            assert row["expected_value"] is None
        assert "偏低" in ev["结论"]


# ---------------------------------------------------------------- 逐期时机
@pytest.mark.parametrize("lot", NEW)
def test_ev_series_declares_not_applicable_instead_of_fake_minus_two(lot):
    """没有「逐期一等奖奖金」的彩种，必须显式宣告不适用，而不是每天 −2.0。"""
    s = compute_ev_series(lot, load_lottery(lot), limit=10)
    assert s["periods"] == [], "不该给出逐期序列"
    assert s.get("不适用"), "缺少「不适用」说明，页面会被读成没有数据"
    assert s["静态期望"]["单注期望收益"] < 0


@pytest.mark.parametrize("lot", NEW)
def test_advice_uses_payout_eff_not_csv_zeros(lot):
    """当期建议须走 payout_eff：单注期望不能是 −2.0，日期须是最近一期。"""
    data = load_lottery(lot)
    a = current_period_advice(lot, data)
    assert a.get("error") is None
    assert a["单注期望"] is not None and a["单注期望"] > -2.0 + 1e-6
    assert a["是否值得买"] is False
    latest = data.records[0].开奖日期
    assert str(a["开奖日期"]) == str(latest), (
        f"{lot} 建议里的开奖日期取错了期（records 是倒序，[0] 才是最新）")
    assert a.get("口径")


# ---------------------------------------------------------------- 展示面
@pytest.fixture(scope="module")
def client():
    from web.app import app
    return app.test_client()


PAGES = ["/", "/anomaly", "/audit", "/compare", "/ev", "/feedback", "/hits",
         "/ledger", "/pipeline", "/predict", "/records", "/robustness",
         "/rolling", "/stats", "/train"]


def test_every_registered_lottery_reaches_the_page_dropdowns(client):
    """新增彩种必须自动出现在下拉框里 —— 这条防的是「后端接了、页面选不到」。"""
    names = list(LOTTERY_CONFIG)
    missing = {}
    for p in PAGES:
        body = client.get(p).get_data(as_text=True)
        lack = [n for n in names
                if f"<option>{n}</option>" not in body and f'value="{n}"' not in body]
        # 不是每页都有彩种下拉；只要求「有下拉的页面必须齐全」
        opts = len(re.findall(r"<option", body))
        if opts >= len(names) and lack:
            missing[p] = lack
    assert not missing, f"这些页面缺彩种选项：{missing}"


def test_frontend_gets_lottery_list_from_backend(client):
    """JS 侧的彩种表必须来自后端注入，且包含全部已注册彩种。"""
    body = client.get("/").get_data(as_text=True)
    m = re.search(r"window\.WB_LOTTERIES = (\[[^;]*\]);", body)
    assert m, "页面没有注入 window.WB_LOTTERIES"
    arr = json.loads(m.group(1))
    assert arr == list(LOTTERY_CONFIG)


def test_main_js_has_no_parallel_lottery_list():
    """main.js 里除了兜底 fallback，不许再手抄一份彩种清单。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "web" / "static" / "js" / "main.js")
    text = src.read_text(encoding="utf-8")
    hits = [ln for ln in text.splitlines()
            if "'排列5', '福彩3D', '排列3', '七星彩'" in ln]
    assert len(hits) <= 1, f"main.js 又出现了手写彩种清单：{hits}"


# ---------------------------------------------------------------- 审计隔离
def test_audit_quarantine_only_skips_already_isolated_records():
    """已隔离记录上的号码问题不计入待处理；但「valid标记不符」本身必须照报。"""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_audit_records_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "audit_records.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    hist = [{"valid_prediction": False}, {"valid_prediction": True}]
    assert mod._quarantined("历史#0 期1", "预测号码非法", hist) is True
    assert mod._quarantined("历史#1 期1", "预测号码非法", hist) is False
    assert mod._quarantined("历史#0 期1", "valid标记不符", hist) is False
    assert mod._quarantined("pending#0 目标期1", "预测号码非法", hist) is False


# ---------------------------------------------------------------- 审计覆盖
def _load_audit():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "_audit_records_mod2",
        Path(__file__).resolve().parent.parent / "scripts" / "audit_records.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_list_follows_registry_not_a_handwritten_six():
    """审计清单必须跟注册清单同源。

    写死 6 个名字的代价是**静默少查两个彩种**，而报告里只会少两行 ——
    读者把「没列出来」读成「它们也没问题」。全仓这条洞已经踩过三次
    （前端模板 / JS 数组 / 奖级表各一份）。
    """
    mod = _load_audit()
    assert mod.LOTTERIES == list(LOTTERY_CONFIG)
    for lot in NEW:
        assert lot in mod.GRADE_JUDGED, f"{lot} 判级没接入 GRADED_JUDGED，审计会静默跳过奖级"


@pytest.mark.parametrize("lot", NEW)
def test_audit_grade_judge_is_same_source_as_production(lot):
    """审计判级必须调生产同一个函数，不能另写一份。

    造一个真实结构的命中场景，比对「审计算出的等级」与「生产 _settle_pl 判的等级」。
    两边一旦分叉，审计会开始报假「奖级不符」，真正的判级 bug 反倒被这份副本掩盖
    —— 2026-11-02 那 173 条 「valid 标记不符」就是这么来的。
    """
    from data.feedback import _settle_pl
    mod = _load_audit()

    if lot == "七乐彩":
        # 4 中且票面不含特别号 → 七等（固定 5 元）；4 中且含特别号 → 六等（10 元）
        cases = [
            ({"基本号": [1, 2, 3, 4, 11, 12, 13]},
             {"基本号": [1, 2, 3, 4, 5, 6, 7], "特别号": [8]}),
            ({"基本号": [1, 2, 3, 4, 8, 12, 13]},
             {"基本号": [1, 2, 3, 4, 5, 6, 7], "特别号": [8]}),
        ]
        expect = ["七等", "六等"]
    else:
        # 选十：中 0 也有奖（选十中零），不能把 hits=0 直接当「未中」
        actual = {"基本号": list(range(1, 21))}
        cases = [
            ({"基本号": list(range(1, 11))}, actual),      # 10 中 → 选十中十
            ({"基本号": list(range(61, 71))}, actual),     # 0 中 → 选十中零（也有奖）
            ({"基本号": [1] + list(range(61, 70))}, actual),  # 仅 1 中 → 未中
        ]
        expect = ["选十中十", "选十中零", None]

    for (pred, act), want in zip(cases, expect):
        got = mod.group_expected(lot, pred, act, "2026126")
        prod = _settle_pl(lot, {"期号": "2026126",
                                "预测号码": pred, "实际号码": act})[2]
        assert got == prod, f"{lot} 审计判级 {got} ≠ 生产判级 {prod}"
        assert got == want, f"{lot} 判级 {got}，应为 {want}（pred={pred}）"


def test_audit_skips_grade_check_for_unjudged_lottery(monkeypatch):
    """没接入判级的彩种：号码/命中照查，但绝不能因为「判级器返回 None」就报假问题。"""
    mod = _load_audit()
    draws, recs = mod.draw_index("快乐8")
    draw = recs[0]
    actual = getattr(draw, "zone_numbers", {}) or {}
    pred = {"基本号": list(actual.get("基本号", []))[:10]}
    rec = {
        "期号": draw.期号,
        "实际号码": actual,
        "预测号码": pred,
        "分区命中": {"基本号": 10},
        "总命中": 10,
        "中奖等级": "选十中十",
        "valid_prediction": True,
        "来源": "predict",
        "生成时间": "%sT09:00:00" % str(draw.开奖日期)[:10],
    }
    # 判级已接入 → 记录与实际一致，不该有问题
    issues, _ = mod.audit_history("快乐8", draws, [rec])
    assert [i for i in issues if i[1] == "奖级不符"] == []

    # 假装这个彩种还没接入判级 → 即使记录里写了胡编奖级也不许报「奖级不符」
    monkeypatch.setattr(mod, "GRADE_JUDGED", set())
    bad = dict(rec, 中奖等级="胡说八等")
    issues2, _ = mod.audit_history("快乐8", draws, [bad])
    assert [i for i in issues2 if i[1] == "奖级不符"] == [], \
        "判级未接入的彩种不该被报「奖级不符」（判级器返回 None 会让每次中奖都成假问题）"



# ---------------------------------------------------------------- 统计口径
def test_null_prob_uses_draw_count_not_ticket_choose():
    """零假设概率必须是「每期开出几个 / 值域」，不是票面 choose。

    踩过的坑（两处同源）：
      · 七乐彩特别号 choose=0 / draw=1（draw_only）⇒ exp=0 ⇒ 0/0，卡方变 inf，
        假报「实质偏离 q=0.0392」——一个纯算术产物被当成开奖异常。
      · 快乐8 票面选 10 而开奖摇 20 ⇒ p0 只有真实值的 1/2 ⇒ 每个号都像
        「出现翻倍」⇒ e-value 指数发散，实见 e-max=3.6e31 的假告警。
    这条测试盯的是**那个数字**，不是结论文字。
    """
    from credibility.randomness_audit import _base_records
    from data.schema import record_zone_numbers
    from discovery.always_valid import _zone_matrix

    for lot, zone_name, expect_c in (("七乐彩", "特别号", 1), ("快乐8", "基本号", 20)):
        sch = schema_from_cfg(LOTTERY_CONFIG[lot], lot)
        zone = sch.zone_by_name(zone_name)
        assert zone is not None
        assert zone.draw_count == expect_c, \
            f"{lot}/{zone_name} draw_count={zone.draw_count}，应为 {expect_c}"
        # 票面 choose 与开奖取号数不一致 —— 正是这条测试要防的分叉
        if lot == "快乐8":
            assert zone.choose != zone.draw_count, "快乐8 票面 10 / 开奖 20 的形状变了"

        recs = load_lottery(lot).records
        B, prob, issues = _zone_matrix(recs, zone)
        assert B is not None and prob is not None
        want = expect_c / float(zone.size)
        assert abs(float(prob[0]) - want) < 1e-12, \
            f"{lot}/{zone_name} p0={prob[0]}，应为 {want}（{expect_c}/{zone.size}）"
        # 指示矩阵每行非零数也必须等于开奖取号数
        per = B.sum(axis=1)
        assert int(per.min()) == int(per.max()) == expect_c, \
            f"{lot}/{zone_name} 每期取号数不恒为 {expect_c}：{per.min()}~{per.max()}"


@pytest.mark.parametrize("lot", NEW)
def test_randomness_audit_finite_and_not_flagged_on_draw_only(lot):
    """真实数据审计必须给出**有限**统计量，且不能对 draw_only 分区假报偏离。"""
    import math

    from credibility.randomness_audit import audit_lottery

    rep = audit_lottery(lot, B=60)
    for z in rep["分区"]:
        for t in z.get("检验", []):
            assert math.isfinite(t["p值"]), f"{lot}/{z['分区']} p 值非有限：{t}"
            assert math.isfinite(float(t["统计量"].split("=")[-1])), \
                f"{lot}/{z['分区']} 统计量非有限：{t['统计量']}"
    if lot == "七乐彩":
        sp = next(z for z in rep["分区"] if z["分区"] == "特别号")
        for t in sp.get("检验", []):
            assert not t.get("实质显著"), \
                f"特别号被报实质偏离（draw_only 分区，choose=0 会除零）：{t}"



# ---------------------------------------------------------------- 统计口径
def test_null_prob_uses_draw_count_not_ticket_choose():
    """零假设概率必须是「每期开出几个 / 值域」，不是票面 choose。

    踩过的坑（两处同源）：
      · 七乐彩特别号 choose=0 / draw=1（draw_only）⇒ exp=0 ⇒ 0/0，卡方变 inf，
        假报「实质偏离 q=0.0392」——一个纯算术产物被当成开奖异常。
      · 快乐8 票面选 10 而开奖摇 20 ⇒ p0 只有真实值的 1/2 ⇒ 每个号都像
        「出现翻倍」⇒ e-value 指数发散，实见 e-max=3.6e31 的假告警。
    这条测试盯的是**那个数字**，不是结论文字。
    """
    from credibility.randomness_audit import _base_records
    from data.schema import record_zone_numbers
    from discovery.always_valid import _zone_matrix

    for lot, zone_name, expect_c in (("七乐彩", "特别号", 1), ("快乐8", "基本号", 20)):
        sch = schema_from_cfg(LOTTERY_CONFIG[lot], lot)
        zone = sch.zone_by_name(zone_name)
        assert zone is not None
        assert zone.draw_count == expect_c,             f"{lot}/{zone_name} draw_count={zone.draw_count}，应为 {expect_c}"
        # 票面 choose 与开奖取号数不一致 —— 正是这条测试要防的分叉
        if lot == "快乐8":
            assert zone.choose != zone.draw_count, "快乐8 票面 10 / 开奖 20 的形状变了"

        recs = load_lottery(lot).records
        B, prob, issues = _zone_matrix(recs, zone)
        assert B is not None and prob is not None
        want = expect_c / float(zone.size)
        assert abs(float(prob[0]) - want) < 1e-12,             f"{lot}/{zone_name} p0={prob[0]}，应为 {want}（{expect_c}/{zone.size}）"
        # 指示矩阵每行非零数也必须等于开奖取号数
        per = B.sum(axis=1)
        assert int(per.min()) == int(per.max()) == expect_c,             f"{lot}/{zone_name} 每期取号数不恒为 {expect_c}：{per.min()}~{per.max()}"


@pytest.mark.parametrize("lot", NEW)
def test_randomness_audit_finite_and_not_flagged_on_draw_only(lot):
    """真实数据审计必须给出**有限**统计量，且不能对 draw_only 分区假报偏离。"""
    import math

    from credibility.randomness_audit import audit_lottery

    rep = audit_lottery(lot, B=60)
    for z in rep["分区"]:
        for t in z.get("检验", []):
            assert math.isfinite(t["p值"]), f"{lot}/{z['分区']} p 值非有限：{t}"
            assert math.isfinite(float(t["统计量"].split("=")[-1])),                 f"{lot}/{z['分区']} 统计量非有限：{t['统计量']}"
    if lot == "七乐彩":
        sp = next(z for z in rep["分区"] if z["分区"] == "特别号")
        for t in sp.get("检验", []):
            assert not t.get("实质显著"),                 f"特别号被报实质偏离（draw_only 分区，choose=0 会除零）：{t}"



# ---------------------------------------------------------------- 期号口径
def test_resolve_issue_does_not_walk_back_past_published_amounts():
    """「当期」期号必须解到**金额已公布的最新一期**，不能因为头奖无人中而往前退。

    探针曾用「头奖」档：快乐8 选十中十 实测概率 1/891 万，大多数期无人中出 ⇒
    market.db 里该档金额是 0 ⇒ 被当成「金额未公布」⇒ 一路往前退，
    最终拿很旧的期号算「当期建议」，却配最新一期的开奖日期
    （实见：期号 2026290 / 开奖日期 2026-11-01，而 2026291 才是 11-01）。
    """
    from data.loader import load_lottery
    from ev.payout import _resolve_issue

    for lot in NEW:
        recs = load_lottery(lot).records
        latest = str(recs[0].期号)
        assert _resolve_issue(lot) == latest, (
            f"{lot} 当期期号解成了 {_resolve_issue(lot)}，最新一期是 {latest}"
            "（金额已公布就不该再往前退）")


@pytest.mark.parametrize("lot", NEW)
def test_advice_issue_and_date_are_the_same_period(lot):
    """建议卡里的「期号」和「开奖日期」必须指同一期 —— 混用两个真源就是自相矛盾。"""
    from data.loader import load_lottery
    from pipeline.step6_ev import current_period_advice

    adv = current_period_advice(lot, load_lottery(lot))
    assert adv.get("期号") and adv.get("开奖日期"), adv
    rec = next(r for r in load_lottery(lot).records
               if str(r.期号) == str(adv["期号"]))
    assert str(adv["开奖日期"]) == str(rec.开奖日期), (
        f"{lot} 建议期号 {adv['期号']} 配了 {adv['开奖日期']}，"
        f"但该期实际开奖日 {rec.开奖日期}")
