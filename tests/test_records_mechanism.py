# -*- coding: utf-8 -*-
"""机制缝隙在策略层「可见」—— 战绩榜状态卡 + 前瞻对照实验 + 命中详情机制行。

背景（2026-10-13）：用户要求在「策略战绩 / 命中历史」里能看到机制缝隙跑得怎么样。
诊断结论是它此前只是乘在底层策略上的修饰层，且 γ 全为 0 ⇒ 不可能有独立战绩。
- 形态 1（只读状态卡）：展示裁决 / R / ū / γ / 是否激活，**不伪造战绩行**。
- 形态 2（前瞻对照实验）：实测 γ 乘数 `exp(-γ·ss)` 与 EV 排序键「一等奖实得」严格
  共线（Spearman ρ=−1）⇒ 对选号恒等于空操作，故改为**复用 `ev/trials.py` 已跑的
  「排序 vs 随机」阴性对照**，在战绩榜单独区块呈现 —— 那是机制相关策略唯一能产生
  独立战绩的地方。

锁住的行为：
  ① 缓存缺失 ⇒ `未测量`，**不**用 0 / 空表粉饰成「无缝隙」；
  ② γ≠0 但 `显著=False` ⇒ 走红线判「未激活」（2026-10-04 端到端实测踩过的坑：
     快照写 γ=-0.0185 且显著=False，只判 γ≠0 会误报「已激活」）；
  ③ 机制名**不进** strategies 聚合（状态卡无独立样本；对照实验走独立台账）；
  ④ 新文案不得出现肯定式本体论硬断言（与 test_narrative_unbinding 同一把尺子）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.app import app
from ev.mechanism import snapshot as SN

from tests.test_narrative_unbinding import _is_hard_assertion


# 假快照（与 tests/test_mechanism_feedback.py 同款，只取状态卡要用的字段）
def _fake_snap(**kw):
    base = {
        "彩种": "双色球",
        "期数": 3504,
        "裁决": "none",
        "判定轨": ["A", "B"],
        "应有判定轨": ["A", "B"],
        "R": 0.993989,
        "ū": 0.5012,
        "γ": -0.0156,
        "γ显著": False,
        "γ_可倾斜": 0.0,
        "模型Go": True,
        "测量时间": "2026-10-06T23:48:00",
        "来源": "只读缓存（不触发新测量；要最新请跑 python cli.py gap）",
    }
    base.update(kw)
    return base


@pytest.fixture
def client(monkeypatch):
    app.config["TESTING"] = True
    return app.test_client()


# ------------------------------------------------------------
# ① 缓存缺失：照实写「未测量」
# ------------------------------------------------------------
def test_status_card_missing_cache_is_honest(client, monkeypatch):
    monkeypatch.setattr(SN, "mechanism_snapshot", lambda lot: None)
    d = client.get("/api/records/双色球/mechanism").get_json()
    assert d["未测量"] is True
    assert d["状态"] == "未测量"
    assert "尚无测量缓存" in d["提示"]
    assert "≠" in d["诚实说明"], "未测量时必须写明「不等于无缝隙/已证明公平」"
    # 不得用 0 / 空表顶替
    assert "快照" not in d and "机制倾斜" not in d
    assert d.get("R") is None and d.get("γ") is None


def test_status_card_missing_cache_lists_measure_command(client, monkeypatch):
    monkeypatch.setattr(SN, "mechanism_snapshot", lambda lot: None)
    d = client.get("/api/records/双色球/mechanism").get_json()
    assert "python cli.py gap" in d["提示"], "必须告诉读者去哪儿刷新"


def test_status_card_rejects_unknown_lottery(client):
    r = client.get("/api/records/不存在的彩种/mechanism")
    assert r.status_code == 400


# ------------------------------------------------------------
# ② 红线：γ≠0 但 CI 含 0 ⇒ 未激活
# ------------------------------------------------------------
def test_status_card_redline_gamma_nonzero_but_not_significant(client, monkeypatch):
    """★ 双色球实测值：γ=-0.0185 且显著=False ⇒ 必须判未激活、γ_可倾斜=0。"""
    monkeypatch.setattr(SN, "mechanism_snapshot",
                        lambda lot: _fake_snap(γ=-0.0185, γ显著=False, γ_可倾斜=0.0))
    d = client.get("/api/records/双色球/mechanism").get_json()
    assert d["未测量"] is False
    assert d["状态"] == "未激活"
    assert d["机制倾斜"]["倾斜"] is False
    assert d["快照"]["γ_可倾斜"] == 0.0
    # 说明必须带功效从句（未检出 ≠ 不存在）
    assert "未检出" in d["说明"] or "不等于「不存在」" in d["说明"]
    # 且不得把「未激活」说成「无缝隙」（否定式引用除外）
    assert "无缝隙" not in d["说明"] or "≠" in d["说明"]


def test_status_card_active_only_when_significant(client, monkeypatch):
    monkeypatch.setattr(SN, "mechanism_snapshot",
                        lambda lot: _fake_snap(γ=0.05, γ显著=True, γ_可倾斜=0.05))
    d = client.get("/api/records/双色球/mechanism").get_json()
    assert d["状态"] == "已激活"
    assert d["机制倾斜"]["倾斜"] is True
    assert d["机制倾斜"]["方向"] == "偏向冷门"


def test_status_card_no_track_lottery_shows_structural_reason(client, monkeypatch):
    """排列3/福彩3D 无判定轨 ⇒ 判定轨为空，前端要能看出「结构上不可判定」。"""
    monkeypatch.setattr(SN, "mechanism_snapshot",
                        lambda lot: _fake_snap(彩种=lot, 裁决="no-track",
                                               判定轨=[], 应有判定轨=[],
                                               R=None, ū=None, γ=None,
                                                γ显著=False, γ_可倾斜=0.0))
    d = client.get("/api/records/排列3/mechanism").get_json()
    assert d["未测量"] is False
    assert d["快照"]["判定轨"] == []
    assert d["状态"] == "未激活"


# ------------------------------------------------------------
# ③ 状态卡 / 对照实验都不是战绩行
# ------------------------------------------------------------
def test_no_mechanism_name_enters_strategy_aggregation():
    """`strategies` 聚合里不得出现任何机制相关策略名。

    状态卡（未测量/未激活/已激活）只是展示：γ=0 时它与基础策略逐位相同，没有
    独立战绩样本。对照实验（A/B：冷门偏好 vs 同池随机）走 `ev/trials.py` 的
    **独立台账**（`training/trials/*.jsonl`，战绩榜单独一个区块展示），也不混进
    feedback 聚合 —— 所以生产策略顺序里一个机制名都不该有。
    """
    from data.records import STRATEGY_ORDER, get_feedback_records
    assert not [n for n in STRATEGY_ORDER if "机制" in n], "机制名不得混进生产策略顺序"
    for name in get_feedback_records("双色球")["strategies"]:
        assert name not in ("未测量", "未激活", "已激活"), f"状态卡混进了战绩聚合：{name}"
        assert "机制" not in name, f"机制名混进了战绩聚合：{name}"


def test_records_page_mentions_status_card_not_strategy_row(client):
    """页面要写明表下方是状态卡而非战绩行，否则读者会把「未激活」读成一条战绩。"""
    html = client.get("/records").get_data(as_text=True)
    assert "状态卡而非战绩行" in html


# ------------------------------------------------------------
# ④ 命中详情接口带机制字段（旧记录没有 ⇒ None，前端隐藏该行）
# ------------------------------------------------------------
def _hit_rec(issue=26100, when="2026-09-01T10:00:00", grade="六等", **kw):
    rec = {
        "期号": issue,
        "开奖日期": "2026-09-01",
        "预测日期": "2026-08-31",
        "评估时间": when,
        "来源": "predict",
        "策略": "高频策略",
        "预测红球": [1, 2, 3, 4, 5, 6],
        "预测蓝球": [7],
        "实际红球": [1, 2, 3, 4, 5, 6],
        "实际蓝球": [7],
        "红球命中": 6,
        "蓝球命中": 1,
        "总命中": 7,
        "中奖等级": grade,
        "valid_prediction": True,
    }
    rec.update(kw)
    return rec


@pytest.fixture
def hits_client(monkeypatch):
    import data.feedback as fb
    import data.prize_table as pt
    import web.routes.hits as hits_route

    hist = {
        "双色球": [
            _hit_rec(26100, "2026-09-01T10:00:00", "六等",
                     机制γ=-0.0185,
                     机制倾斜={"彩种": "双色球", "γ": 0.0, "倾斜": False,
                               "可用": False, "原因": "CI 含 0", "说明": "未激活"},
                     缝隙快照={"测量时间": "2026-10-06T23:48:00"}),
            # 旧记录：没有任何机制字段
            _hit_rec(26099, "2026-08-25T10:00:00", "五等"),
        ],
    }
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: list(hist.get(name, [])))
    monkeypatch.setattr(pt, "prize_payout",
                        lambda lot, grade, issue=None: {"金额": 5.0, "浮动": False,
                                                        "来源": "固定奖级", "文本": "¥5", "备注": ""})
    monkeypatch.setattr(hits_route, "feedback_record_view",
                        lambda name, h: {"zones": [], "total_hit": 0, "total_choose": 0})
    app.config["TESTING"] = True
    return app.test_client()


def test_hits_records_expose_mechanism_fields(hits_client):
    d = hits_client.get("/api/hits?lottery=双色球&limit=0").get_json()
    with_mech = [r for r in d["records"] if r.get("机制倾斜")]
    without_mech = [r for r in d["records"] if not r.get("机制倾斜")]
    assert len(with_mech) == 1 and len(without_mech) == 1, "两条记录的机制字段应一新一旧"
    r = with_mech[0]
    assert r["机制γ"] == -0.0185
    assert r["机制倾斜"]["倾斜"] is False
    assert r["机制测量时间"] == "2026-10-06T23:48:00"
    # 旧记录一律 None（前端据此隐藏该行，不粉饰）
    assert without_mech[0]["机制γ"] is None
    assert without_mech[0]["机制测量时间"] is None


# ------------------------------------------------------------
# ⑤ 机制缝隙·前瞻对照实验（只读 ev/trials 台账）
# ------------------------------------------------------------
def test_trials_card_participating_lottery(client, monkeypatch):
    from ev import trials as T
    monkeypatch.setattr(T, "trial_report", lambda lot, **kw: {
        "彩种": lot, "总记录期数": 13, "已结算期数": 12, "未开奖期数": 1,
        "每组注数": 100, "A累计中奖注数": 87, "B累计中奖注数": 98,
        "A累计实得": 470.0, "B累计实得": 565.0,
        "裁决": "主终点尚无结论", "等价性成立": False, "符号检验p": 0.146,
        "功效规划": [{"相对效应": "+20%", "需要期数": 20, "折合年数": 0.13}],
    })
    d = client.get("/api/records/双色球/trials").get_json()
    assert d["参与"] is True
    assert len(d["臂"]) == 2
    a, b = d["臂"]
    assert a["中奖注数"] == 87 and b["中奖注数"] == 98
    assert a["中奖率"] == pytest.approx(87 / (12 * 100))
    assert d["裁决"] == "主终点尚无结论"
    assert d["数据源"].startswith("training/trials/")


def test_trials_card_rejects_fixed_odds_lottery(client):
    d = client.get("/api/records/排列3/trials").get_json()
    assert d["参与"] is False
    assert "不参与前瞻对照实验" in d["说明"]


def test_trials_card_empty_ledger_is_honest(client, monkeypatch):
    from ev import trials as T
    monkeypatch.setattr(T, "trial_report", lambda lot, **kw: {
        "彩种": lot, "总记录期数": 0, "已结算期数": 0, "未开奖期数": 0,
        "每组注数": 100, "裁决": "样本不足以检验", "功效规划": [],
    })
    d = client.get("/api/records/双色球/trials").get_json()
    assert d["参与"] is True
    assert d["已结算期数"] == 0
    assert all(x["中奖注数"] is None for x in d["臂"]), "空台账不得编造 0 中奖"


def test_records_page_mentions_trials_section(client):
    html = client.get("/records").get_data(as_text=True)
    assert "前瞻对照实验" in html


# ------------------------------------------------------------
# ⑥ 文案纪律：新代码不得冒出肯定式本体论硬断言
# ------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_NEW_FILES = (
    _ROOT / "web" / "routes" / "records.py",
    _ROOT / "ev" / "mechanism" / "snapshot.py",
    _ROOT / "web" / "static" / "js" / "main.js",
    _ROOT / "web" / "templates" / "records.html",
)


@pytest.mark.parametrize("path", _NEW_FILES, ids=lambda p: p.name)
def test_new_strings_have_no_hard_assertions(path):
    """与 test_narrative_unbinding 同一把尺子：只扫本次改动的文件。"""
    hits = []
    for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _is_hard_assertion(ln):
            hits.append(f"{path.name}:{i}: {ln.strip()}")
    assert not hits, "出现肯定式本体论硬断言：\n  " + "\n  ".join(hits)


def test_card_helper_marks_inactive_as_not_proven_fair():
    """状态卡 JS 必须把「未激活」与「无缝隙/已证明公平」区分开。"""
    js = (_ROOT / "web" / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert "无独立战绩样本" in js, "γ=0 的诚实标注不能丢"
    assert "与基础策略逐位相同" in js, "逐位相同的诚实边界不能丢"
    # 「无缝隙」只允许出现在**否定式/纪律声明**里（与 canonical 扫描器同一豁免语义）
    bad = [ln.strip() for ln in js.splitlines()
           if "无缝隙" in ln and not any(e in ln for e in ("≠", "不许", "不得", "不是"))]
    assert not bad, "肯定式提到「无缝隙」：\n  " + "\n  ".join(bad)
