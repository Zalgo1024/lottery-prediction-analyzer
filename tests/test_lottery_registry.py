# -*- coding: utf-8 -*-
"""彩种注册契约：全仓「彩种清单」多份拷贝的一致性守卫。

★ 为什么需要这个文件（2026-11-02，接入七乐彩/快乐8 时实踩）
    项目里散落着 20+ 份手写彩种清单：白名单、月度期数、研究轨范围、推送范围、
    预算上限…… 漏改一处**不会报错**，只会让该彩种在那个环节静静消失：

      · `ev/payout._LOTTERY` 漏了快乐8 ⇒ `payout_eff` 直接返回 error；
      · `ev/payout._resolve_issue` 不认识七乐彩/快乐8 ⇒ 期号返回 None ⇒
        `base_payout` 一路降级到**名义值**，表上照样打印、数字照样"合理"
        （七乐彩一等奖显示 127 万而不是当期实测 132 万），比报错难查得多；
      · `data/ticket_size.DEFAULT_LIMITS` 漏了快乐8 ⇒ 落到 `_FALLBACK_LIMIT_LOTTO=500`，
        而快乐8 日开 30 期 ⇒ 月度上限 3 万元，且无人察觉。

    这些坑的共同点：**没有一处会失败**。所以判据不能是"跑一遍看有没有异常"，
    而必须是"清单之间互相比对"。本文件就是那个比对。

判据分两类：
  A. **必须覆盖全部彩种**的清单 —— 少一个就是 bug；
  B. **显式不适用**的模块 —— 必须写明原因，且原因与彩种集合互相对齐
     （既不许多列一个已删除的彩种，也不许让新彩种无声无息地掉进去）。
"""
from __future__ import annotations

import pytest

import config
from config import LOTTERY_CONFIG, ticket_price

# 现行 8 个彩种（顺序即 config 里的顺序，加彩种时这里也要改 —— 这是**故意**的：
# 让新增彩种必须显式过一遍本文件的每一条判据，而不是靠"自动出现在集合里"）。
EXPECTED = ("双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩",
            "七乐彩", "快乐8")


# ---------------------------------------------------------------- 0. 注册表本身
def test_all_expected_lotteries_registered():
    assert tuple(LOTTERY_CONFIG) == EXPECTED


def test_every_lottery_declares_price_and_zones():
    for name, cfg in LOTTERY_CONFIG.items():
        assert cfg.get("name_en"), f"{name} 缺 name_en（market.db 的彩种代码）"
        assert cfg.get("zones"), f"{name} 缺 zones"
        assert ticket_price(name) > 0, f"{name} 票价非正"
        assert cfg.get("draw_days") is not None, f"{name} 缺 draw_days"


def test_name_en_unique():
    """name_en 是 market.db / CSV 的键，重复会让两个彩种抢同一份数据。"""
    codes = [cfg["name_en"] for cfg in LOTTERY_CONFIG.values()]
    assert len(codes) == len(set(codes)), codes


# ---------------------------------------------------------------- A. 必须全覆盖
def test_audit_lots_cover_all_lotteries():
    """数据体检的彩种清单：少一个 ⇒ 该彩种从不被体检。"""
    from data.audit.checks import LOTS
    assert set(LOTS) == set(LOTTERY_CONFIG), set(LOTTERY_CONFIG) - set(LOTS)


def test_payout_lottery_set_covers_all():
    """赔付层的彩种白名单：少一个 ⇒ payout_eff 返回 error，EV 链整条断掉。"""
    from ev.payout import _LOTTERY
    assert set(_LOTTERY) == set(LOTTERY_CONFIG), set(LOTTERY_CONFIG) - set(_LOTTERY)


def test_push_all_lotteries_cover_all():
    """全彩种简报：少一个 ⇒ 该彩种在推送里静默消失。"""
    from data.push_notify import ALL_LOTTERIES
    assert set(ALL_LOTTERIES) == set(LOTTERY_CONFIG), set(LOTTERY_CONFIG) - set(ALL_LOTTERIES)
    assert len(ALL_LOTTERIES) == len(set(ALL_LOTTERIES)), "推送清单有重复项"


def test_web_all_lists_cover_all():
    """看板下拉的彩种清单。"""
    from web.routes.audit import _ALL as audit_all
    from web.routes.ledger import _ALL as ledger_all
    for mod, lst in (("web.routes.audit", audit_all), ("web.routes.ledger", ledger_all)):
        assert set(lst) == set(LOTTERY_CONFIG), f"{mod} 缺 {set(LOTTERY_CONFIG) - set(lst)}"


def test_ps1_schedule_covers_all_lotteries_with_correct_weekdays():
    """Windows 计划任务脚本必须覆盖**全部**彩种，且星期换算与 `draw_days` 一致。

    ★ 2026-11-03 实情：`scripts/auto_scheduled.ps1` 的彩种清单是**手写**的 6 个，
      七乐彩、快乐8 从来没写进去 —— 于是计划任务每天跑十几次，却一次都不跑这两个
      彩种，而日志里一切正常（没有任何一行说"我跳过了谁"）。这类「静默缺席」
      靠人眼发现不了，只能靠"清单必须等于注册表"这条断言。
    ★ 为什么还要校验星期：`draw_days` 用 **Python weekday（周一=0）**，而脚本用
      .NET `[DayOfWeek]`（**周日=0**）⇒ 换算 `(weekday + 1) % 7`。两套编码混用，
      抄错了照样"能跑"，只是那天跑错彩种 —— 而且只在特定星期几才露出来，最难查。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "auto_scheduled.ps1").read_text(encoding="utf-8-sig")

    m = re.search(r"\$lots\s*=\s*@\(([^)]*)\)", src)
    assert m, "auto_scheduled.ps1 里找不到每日彩种数组 `$lots = @(...)`"
    daily = set(re.findall(r"'([^']+)'", m.group(1)))

    all_days = set(range(7))
    expect_daily = {lot for lot, cfg in LOTTERY_CONFIG.items()
                    if set(cfg.get("draw_days") or []) == all_days}
    assert daily == expect_daily, (
        f"每日开奖彩种不一致：脚本 {sorted(daily)} vs 注册表 {sorted(expect_daily)}")

    for lot, cfg in LOTTERY_CONFIG.items():
        if lot in expect_daily:
            continue
        pat = re.compile(
            r"\$dow\s+-in\s+@\(([^)]*)\)\s*\)?\s*\{\s*\$lots\s*\+=\s*'"
            + re.escape(lot) + r"'")
        hit = pat.search(src)
        assert hit, f"{lot} 不在计划任务清单里（该彩种从不被计划任务运行）"
        got = sorted(int(x) for x in hit.group(1).split(","))
        want = sorted((int(d) + 1) % 7 for d in (cfg.get("draw_days") or []))
        assert got == want, (
            f"{lot} 星期映射错：脚本 {got} vs draw_days {cfg.get('draw_days')}→{want}"
            "（.NET 周日=0，Python weekday 周一=0，换算 (d+1)%7）")


def test_ticket_size_limits_cover_all():
    """★ 注数上限必须**在代码里**覆盖全部彩种。

    只靠 `config/ticket_size.json` 不够：该文件是运行时状态，可能缺失/被重置，
    而 `_resolve_limits()` 的兜底是 `_FALLBACK_LIMIT_LOTTO=500`。
    对日开 30 期的快乐8 来说，500 意味着月度上限 3 万元 —— 这不是"保守兜底"，
    是把预算炸掉。所以 DEFAULT_LIMITS 必须有显式值。
    """
    from data.ticket_size import DEFAULT_LIMITS
    missing = set(LOTTERY_CONFIG) - set(DEFAULT_LIMITS)
    assert not missing, f"DEFAULT_LIMITS 缺 {missing}（会落到 500 的乐透型兜底）"


def test_monthly_periods_cover_all():
    """月度期数：缺一个 ⇒ 预算包络按 13 期估，而快乐8 实际 30 期（低估一半以上）。"""
    from ev.budget_envelope import PERIODS_PER_MONTH
    missing = set(LOTTERY_CONFIG) - set(PERIODS_PER_MONTH)
    assert not missing, f"PERIODS_PER_MONTH 缺 {missing}"
    # 日开彩种必须是 30 期口径
    for daily in ("排列3", "排列5", "福彩3D", "快乐8"):
        assert PERIODS_PER_MONTH[daily] == 30, f"{daily} 是日开，应为 30 期/月"


def test_budget_envelope_covers_all():
    """预算包络按彩种生成；缺一个 ⇒ 该彩种没有月度封顶。"""
    from ev.budget_envelope import default_envelopes
    envs = default_envelopes()
    missing = set(LOTTERY_CONFIG) - set(envs)
    assert not missing, f"default_envelopes 缺 {missing}"


# ---------------------------------------------------------------- B. 显式不适用
def test_research_rail_excludes_new_lotteries_with_reason():
    """研究轨（A/B 轨）不含七乐彩/快乐8，但**必须写明原因**。

    只报"不在适用清单内"会把"不适用"和"名字打错/还没接"混为一谈，
    于是有人会去改装载代码硬塞进来。
    """
    from ev.mechanism.common import ALL_LOTTERIES, NOT_APPLICABLE
    assert "七乐彩" not in ALL_LOTTERIES
    assert "快乐8" not in ALL_LOTTERIES
    for name in ("七乐彩", "快乐8"):
        assert name in NOT_APPLICABLE, f"{name} 被排除却没说原因"
        assert len(NOT_APPLICABLE[name]) >= 20, f"{name} 的原因太短，等于没写"


def test_not_applicable_keys_are_registered_lotteries():
    """原因表里不许出现「已删除/拼错」的彩种 —— 那会让排除看起来像借口。"""
    from ev.mechanism.common import NOT_APPLICABLE
    extra = set(NOT_APPLICABLE) - set(LOTTERY_CONFIG)
    assert not extra, f"NOT_APPLICABLE 里有未注册的彩种：{extra}"


# ---------------------------------------------------------------- C. 每彩种算得出数
@pytest.mark.parametrize("name", EXPECTED)
def test_schema_and_rules_resolvable(name):
    """每个彩种都要能建 schema、取规则、算概率 —— 且概率和 ≤ 1。"""
    from data.schema import get_schema
    from ev.payout import grade_probabilities
    from ev.profit import rules as R

    s = get_schema(name)
    assert s.zones, name
    r = R.rules_for(name)
    assert r.name == name
    probs = grade_probabilities(name)
    assert probs, name
    assert 0 < sum(probs.values()) <= 1.0 + 1e-12, (name, sum(probs.values()))


@pytest.mark.parametrize("name", ["七乐彩", "快乐8"])
def test_ev_is_negative_for_new_lotteries(name):
    """诚实底线：新彩种的净 EV 必须为负（不许出现"结构性优势"）。"""
    from ev.payout import payout_eff

    n = 7 if name == "七乐彩" else 10
    ticket = {"号码": {"基本号": list(range(1, n + 1))}}
    if name == "七乐彩":
        ticket["号码"]["特别号"] = []
    r = payout_eff(name, ticket)
    assert "error" not in r, r.get("error")
    assert r["净EV"] < 0, (name, r["净EV"])
