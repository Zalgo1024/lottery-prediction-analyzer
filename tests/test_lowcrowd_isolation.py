# -*- coding: utf-8 -*-
"""tests/test_lowcrowd_isolation.py —— 低拥挤策略的**生产隔离**契约。

首版定位：独立研究性入口，**默认不参与**生产出号轮换、权重学习、动态注数安全阀。
它一旦漏进任何一条，都会静默改写既有行为：

  · 权重学习：策略数 3 → 4，三个生产策略的权重被稀释成 1/6（不是 1/3）；
  · 命中率安全阀 / 压缩 KPI：用研究臂的记录去推动下一期注数；
  · 去重键：新增元数据若进 `_fb_key`，同批会重复入账。

这里把这些"漏进去"的路径逐条钉死。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import data.feedback as fb

ROOT = Path(__file__).resolve().parents[1]


def _fb(strategy, batch=None, win=False, red_hit=1, blue_hit=1, compress=None):
    d = {
        "期号": 26100, "策略": strategy, "valid_prediction": True,
        "预测红球": [1, 2, 3, 4, 5, 6], "预测蓝球": [7],
        "红球命中": red_hit, "蓝球命中": blue_hit,
        "中奖等级": "六等" if win else "未中",
        "档位": "一般",
    }
    if batch:
        d["批次号"] = batch
    if compress:
        d["压缩"] = compress
    return d


# ---------------------------------------------------------------- ① 权重学习不稀释

def test_non_learning_strategy_set_contents():
    assert "低拥挤策略" in fb.NON_LEARNING_STRATEGIES
    for prod in ("高频策略", "遗漏值策略", "区间均衡策略", "ML策略"):
        assert prod not in fb.NON_LEARNING_STRATEGIES, f"{prod} 不应被排除出权重学习"


def test_weights_are_not_diluted_by_lowcrowd(monkeypatch):
    """★ 三个生产策略必须仍是 1/3 —— 漏掉过滤会变成 1/4（策略数 3→4）。"""
    history = []
    for name in ("高频策略", "遗漏值策略", "区间均衡策略", "低拥挤策略"):
        history += [_fb(name, red_hit=1, blue_hit=1) for _ in range(12)]
    monkeypatch.setattr(fb, "load_feedback_history", lambda n, lookback=None: history)
    saved = {}
    monkeypatch.setattr(fb, "save_strategy_weights",
                        lambda n, w: saved.update(w) or None)

    w = fb.update_strategy_weights("双色球", respect_gate=False)
    assert "低拥挤策略" not in w, "研究性入口不得出现在权重表里"
    assert set(w) == {"高频策略", "遗漏值策略", "区间均衡策略"}
    for v in w.values():
        assert abs(v - 1 / 3) < 1e-6, f"权重被稀释了：{w}"


def test_lowcrowd_does_not_carry_ml_names():
    """策略名不得含「ML」/「统计训练」——否则被 normalize_strategy_name 并进 ML策略。"""
    for token in ("ML", "统计训练"):
        assert token not in "低拥挤策略"
    assert fb.normalize_strategy_name("低拥挤策略") == "低拥挤策略"


# ---------------------------------------------------------------- ② 安全阀 / 压缩 KPI

def test_recent_hit_stats_excludes_and_recounts(monkeypatch):
    """过滤后必须**重新计数**：用过滤前的 len 会稀释命中率（8 条里 4 中 vs 4 条里 1 中）。"""
    history = [_fb("高频策略", win=(i == 0)) for i in range(4)] + \
              [_fb("低拥挤策略", win=True) for _ in range(4)]
    monkeypatch.setattr(fb, "load_feedback_history", lambda n, lookback=None: history)
    s = fb._recent_hit_stats("双色球")
    assert s["hit_n"] == 4, f"过滤后应只剩 4 条，实得 {s['hit_n']}"
    assert s["hit_rate"] == 0.25


def test_recent_hit_stats_all_lowcrowd_is_no_signal(monkeypatch):
    """只剩研究性入口 ⇒ 无信号（hit_rate=None），不许拿它去推动注数。"""
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda n, lookback=None: [_fb("低拥挤策略", win=True) for _ in range(5)])
    assert fb._recent_hit_stats("双色球") == {"hit_n": 0, "hit_rate": None}


def test_last_compress_kpi_ignores_lowcrowd_batch(monkeypatch):
    """只有研究性入口带压缩 KPI ⇒ 返回 None（不许改写下一期注数）。"""
    monkeypatch.setattr(fb, "load_feedback_history", lambda n, lookback=None: [
        _fb("低拥挤策略", batch="B1", compress={"启用": True, "压缩前注数": 400, "压缩后注数": 200}),
    ])
    assert fb._last_compress_kpi("双色球") is None


def test_last_compress_kpi_still_works_for_production(monkeypatch):
    """生产批次的压缩 KPI 照旧能取到 —— 过滤只针对研究性入口。"""
    monkeypatch.setattr(fb, "load_feedback_history", lambda n, lookback=None: [
        _fb("高频策略", batch="B9", compress={"启用": True, "压缩前注数": 400, "压缩后注数": 200}),
        _fb("低拥挤策略", batch="B1", compress={"启用": True, "压缩前注数": 10, "压缩后注数": 5}),
    ])
    k = fb._last_compress_kpi("双色球")
    assert k and k["压缩前注数"] == 400


# ---------------------------------------------------------------- ③ 元数据不得进去重键

def test_carry_lowcrowd_fields_copies_metadata():
    pred = {"低拥挤": {"λ": 0.5, "门控": {"生效分位": 0.3}}}
    out = {}
    fb._carry_lowcrowd_fields(pred, out)
    assert out["低拥挤"] == pred["低拥挤"]
    # pending 上没有 ⇒ 不写空壳
    out2 = {}
    fb._carry_lowcrowd_fields({}, out2)
    assert out2 == {}


def test_lowcrowd_keys_do_not_touch_dedup_key():
    """`_fb_key` = (档位, RB/NUM, 期号, 策略, 预测号码)。新增字段绝不能混进去，
    否则同批会被判成不同记录而重复入账。"""
    dedup_parts = {"档位", "期号", "策略", "预测号码", "预测红球", "预测蓝球", "红球命中"}
    assert set(fb.LOWCROWD_KEYS).isdisjoint(dedup_parts)
    assert "低拥挤" in fb.LOWCROWD_KEYS


# ---------------------------------------------------------------- ④ 战绩榜可见但不参选

def test_strategy_order_includes_lowcrowd_at_the_end():
    from data.records import STRATEGY_ORDER
    assert "低拥挤策略" in STRATEGY_ORDER
    # 生产三策略在前，研究性入口在后（只影响显示位置）
    assert STRATEGY_ORDER.index("低拥挤策略") > STRATEGY_ORDER.index("区间均衡策略")


def test_unsupported_lottery_refuses_instead_of_silent_fallback():
    """大乐透等不支持的彩种：必须**显式拒绝**并给原因，不许静默降级成启发式出号。"""
    from prediction.engine import predict

    r = predict("大乐透", groups=5, mode="lowcrowd", record_pending=False)
    assert r.get("拒绝") is True, f"应显式拒绝，实得：{r}"
    assert r["号码组数"] == 0 and r["预测号码"] == []
    assert "原因" in r and r["原因"]
    assert r.get("低拥挤", {}).get("循环性声明"), "拒绝时也要带上循环性声明"


def test_validators_and_cli_accept_lowcrowd_mode():
    """Web 校验与 CLI 都必须认这个 mode —— 少一处就成了『入口写了但调不动』。"""
    import cli
    src = open(cli.__file__, encoding="utf-8").read()
    assert '"lowcrowd"' in src
    v = open("web/validators.py", encoding="utf-8").read()
    assert '"lowcrowd"' in v


def test_lowcrowd_seed_is_stable_across_processes():
    """★ 内置 hash() 对 str 受 PYTHONHASHSEED 加盐 ⇒ 重启后同一期出另一套号。

    后果不只是「不可复现」：反馈去重键含预测号码 ⇒ 同一期会被记成两批 pending、
    两批都参与评估 ⇒ 战绩翻倍。故 seed 必须来自确定性摘要（crc32）。
    """
    code = ("from prediction.engine import _stable_seed;"
            "print(_stable_seed('双色球', '26121'))")
    outs = []
    for hs in ("0", "1"):
        env = dict(os.environ, PYTHONHASHSEED=hs, PYTHONUTF8="1")
        env["PYTHONPATH"] = str(ROOT)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env, cwd=str(ROOT), timeout=180)
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout.strip())
    assert outs[0] and outs[0] == outs[1], f"seed 随 PYTHONHASHSEED 变化：{outs}"
