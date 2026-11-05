"""pytest 全局夹具：把「会落盘到 config/」的模块存储路径重定向到临时目录。

背景（2026-09-18 实踩，务必保留本夹具）：
  `data/push_notify.py` 的 `push_health_summary` / `push_recovery_summary` /
  `push_pipeline_result` 在推送后会走 `_mark_pushed()` → `save_config()`，
  落盘到 `config/push_notify.json`（真身配置：渠道 + webhook token）。
  新写的「自检推送」用例当时只传了 cfg、**没有重定向 STORE_PATH**，
  结果测试把真身配置覆盖成了测试值 —— 用户真实的企业微信 webhook token 被写成了 "k"。

本夹具对**所有**用例 autouse 生效，作为最后一道保险：任何测试都不再可能
写到真实的 `config/`。各测试内部的 `_TmpStore` 仍可继续用（会保存/恢复本夹具
给出的临时路径，互不冲突）。
"""

import pytest

_TARGETS = (
    ("data.push_notify", "STORE_PATH", "push_notify.json"),
    # 整组推送的跨进程锁（2026-11-03）。不隔离的话，用例会在真实 config/ 下
    # 建出 `.push_group.lock`，还可能被别的进程正持着 ⇒ 测试随机卡到超时。
    ("data.push_notify", "GROUP_LOCK_PATH", ".push_group"),
    # 跨进程节流状态文件（2026-10-24 新增）。不加这一条，用例会把真实
    # config/.push_pace.json 写成"刚刚发过"，让**下一次真实推送白等 3.2 秒**；
    # 反过来，真实文件里的旧时间戳也会让用例莫名其妙变慢。
    ("data.push_notify", "PACE_PATH", ".push_pace.json"),
    ("data.ticket_size", "STORE_PATH", "ticket_size.json"),
    ("web.training_loop", "STORE_PATH", "training_loop.json"),
    # ev/mechanism/gap_cache.py 的磁盘缓存目录（测量结果 JSON）。
    # 不加这一条，用例一旦走 measure_gap(use_cache=True) 就会把真实 cache/mechanism 写脏。
    ("ev.mechanism.gap_cache", "CACHE_DIR", "mechanism"),
    # 离线体检日报的「今天跑过没」状态（web/health_digest.py）。
    # 不加这一条，用例调用 maybe_run() 会把真实 config/digest_state.json 写成今天已跑，
    # 导致当晚真实日报被静默跳过。
    ("web.health_digest", "STATE_PATH", "digest_state.json"),
    ("web.health_digest", "DIGEST_DIR", "automation"),
    # 娱乐预算账本（v1.8 新增，「本月已投」跨期状态）。不加这一条，用例会把真实
    # config/budget_ledger.json 写成「本月已投 400 元」，让当晚真实包络闸门误判额度耗尽。
    ("ev.budget_envelope", "LEDGER_PATH", "budget_ledger.json"),
    # 手动定额表（v2 动态包络新增）。这是**唯一能上调额度**的通道，比账本更危险：
    # 用例一旦写脏 config/budget_manual.json，真实判定会拿一个测试额度当上限，
    # 而且 `default_envelope()` 每次都读它 ⇒ 影响面覆盖所有彩种。
    ("ev.budget_envelope", "MANUAL_PATH", "budget_manual.json"),
    # 覆盖选择的**本机**调参文件（v1.9）。默认值写在代码里，本文件只做覆盖。
    # 不隔离的话，本机上「关掉双色球覆盖」的调参会把出号路径用例整批判红/判绿，
    # 而 CI 上看不出原因（仓库里没有这个文件）。
    ("prediction.coverage_production", "CONFIG_PATH", "coverage_production.json"),
    # 生产预算闸门（2026-10-30 新增，任务 2）。两条路径都要隔离：
    #   · LIMIT_PATH 是**唯一能改额度**的人工通道 —— 用例写脏它就等于给真实出号
    #     装了一个测试额度（或关掉闸门）；
    #   · SPEND_PATH 是「本月已投」台账 —— 写脏会让当晚真实出号误以为额度耗尽而停发。
    ("ev.budget_gate", "LIMIT_PATH", "budget_aggregate.json"),
    ("ev.budget_gate", "SPEND_PATH", "budget_spend.json"),
    # 逐期账本（2026-10-31 新增任务 1）。不隔离的话，用例 rebuild 会把真实
    # training/ledger/*.jsonl 覆盖成「只有测试彩种、只有若干期」的残本，
    # 而那正是复盘时唯一的成本/净盈亏证据。
    ("data.ledger", "LEDGER_DIR", "ledger"),
    # 留证台账（2026-10-31）。这是**事故取证**的唯一第一手材料，被测试覆盖掉
    # 就等于把「当时到底是什么状态」的证据抹了。
    ("data.attest", "ATTEST_DIR", "attest"),
    # 影子对照台账（2026-10-31）。写脏了会让三臂汇总混入测试期的假读数，
    # 而「同预算对照」正是用来判「要不要加注」的依据 —— 污染它的代价最大。
    ("data.shadow", "SHADOW_DIR", "shadow"),
    # 前缀口径研究（2026-11-05，离线通道）。冻结快照是「排序在开奖前已定」的
    # 唯一证据，影子台账只追加不回改；用例写脏真实 logs/ 会污染这两份留痕。
    ("ev.order_freeze", "SNAPSHOT_DIR", "order_freeze"),
    ("ev.prefix_curve", "SNAPSHOT_DIR", "order_freeze"),
    ("ev.prefix_curve", "SHADOW_PATH", "prefix_shadow.jsonl"),
    # 影子验证接线层（2026-11-05，离线通道）。它在 import 时从上面两个模块
    # 复制了路径常量，若不单独重定向，autouse 夹具改的是源模块的属性，
    # 本模块仍读真实 logs/ ⇒ 用例会污染影子台账 / 冻结快照池。
    ("ev.prefix_shadow", "SHADOW_PATH", "prefix_shadow.jsonl"),
    ("ev.prefix_shadow", "FREEZE_DIR", "order_freeze"),
)


# ★ 2026-10-31：`STORE_PATH` 被指到 tmp 后文件**并不存在**，而新增的 `health()`
#   哨兵会把「文件缺失」判成不安全 ⇒ 所有 `predict(record_pending=True)` 一律停发。
#   这是**生产语义**（配置缺失就该停发），但测试里它只会变成噪声。所以给测试环境
#   种一份健康配置；值刻意取 `default_count()`（数字型 5 / 乐透型 100），
#   与「文件缺失时 sanitize 兜底」的生效值完全一致，避免改变任何既有断言的期望值。
#   要验「缺失即不安全」的用例请显式传 `path=`（见 tests/test_ticket_size_sentinel.py）。
_TEST_TICKET_DYNAMIC = {
    "双色球": 100, "大乐透": 100, "七星彩": 100, "七乐彩": 100, "快乐8": 100,
    "排列5": 5, "福彩3D": 5, "排列3": 5,
}


def _seed_ticket_size_store():
    import json
    try:
        from data import ticket_size as ts
    except Exception:
        return
    p = ts.STORE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "mode": "dynamic",
            "fixed": dict(_TEST_TICKET_DYNAMIC),
            "dynamic": dict(_TEST_TICKET_DYNAMIC),
            "current": dict(_TEST_TICKET_DYNAMIC),
            # ★ 上限必须与 `data/ticket_size.py::DEFAULT_LIMITS` 同口径：
            #   2026-11-02 起统一（500），2026-11-04 用户要求提到 **1000**。
            #   这里曾是「逐彩种 500/400/100/200/200/300/500/100」的镜像，
            #   于是代码改了上限、测试还在用旧刻度 —— 断言读到的是这份种子而不是新默认，
            #   表现为「明明改了却还有测试说 400」（本轮实踩）。
            "limits": {"双色球": 1000, "大乐透": 1000, "排列5": 1000,
                       "福彩3D": 1000, "排列3": 1000, "七星彩": 1000,
                       "七乐彩": 1000, "快乐8": 1000},
            "锁定": True,
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _isolate_config_stores(tmp_path):
    saved = []
    for mod_name, attr, filename in _TARGETS:
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:            # 模块不可导入时跳过，不因夹具让测试报错
            continue
        if not hasattr(mod, attr):
            continue
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, tmp_path / filename)

    _seed_ticket_size_store()

    yield

    for mod, attr, old in saved:
        setattr(mod, attr, old)


# ---------------------------------------------------------------- 环境依赖守卫
# 立项（2026-10-26）：CI 自有历史 17/17 全红，40 个失败（36 FAILED + 4 ERROR）
# 逐条查过 traceback，根因只有三类，**没有一条是代码缺陷**：
#   ① market.db 不入库（旁路补偿库，删即回滚）—— 14 例 + 4 例 robustness 夹具；
#   ② training/hypotheses 登记册是发现流水线的生成物，克隆里没有 —— 2 例；
#   ③ statsmodels 是不在 requirements 的可选科学栈（GLM 拟合），CI 没装 —— 19 例；
#   ④ Windows 中文字体只有本机有，Linux 上 matplotlib 缺字形 —— 2 例。
# 处理：三个标记 + 本钩子。本机（全环境）照跑；CI / 全新克隆 / 无数据环境
# **自动跳过**，且理由逐项点名缺什么——不做笼统的"环境不支持"。
# ★ 纪律：新用例按同样根因归类打标；不得把"懒得造数据"的用例标成 realdata
#   （那是用跳过掩盖可构造的测试）。分类台账见 docs/CI环境依赖隔离_结论.md。

import importlib.util
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent


def _market_db_exists() -> bool:
    """与 data/prize_amounts._db_path 同源（repo 相对），不认硬编码绝对路径。"""
    return (_REPO_ROOT / "market" / "market.db").exists()


def _statsmodels_installed() -> bool:
    return importlib.util.find_spec("statsmodels") is not None


def _hypothesis_registry_populated() -> bool:
    """至少一个彩种的登记册 JSON 在（import 时 mkdir 会造出空目录，不能只看目录）。"""
    d = _REPO_ROOT / "training" / "hypotheses"
    return d.is_dir() and any(d.glob("*_hypotheses.json"))


HAS_MARKET_DB = _market_db_exists()
HAS_STATSMODELS = _statsmodels_installed()
HAS_REGISTRY = _hypothesis_registry_populated()

_CJK_FONT: Optional[bool] = None


def _cjk_font_available() -> bool:
    """matplotlib 能否解析到中文字体（与 data/push_image._FONTS 前三项同源）。"""
    global _CJK_FONT
    if _CJK_FONT is None:
        try:
            from matplotlib import font_manager
            have = {f.name for f in font_manager.fontManager.ttflist}
            _CJK_FONT = bool(have & {"Microsoft YaHei", "SimHei", "SimSun"})
        except Exception:
            _CJK_FONT = False
    return _CJK_FONT


def _missing_env() -> dict:
    """逐标记给出缺失组件列表（空列表 = 环境满足，照跑）。"""
    return {
        "realdata": ([] if HAS_MARKET_DB else ["market/market.db"])
                    + ([] if HAS_REGISTRY else ["training/hypotheses 登记册（发现流水线生成物）"]),
        "statsmodels": [] if HAS_STATSMODELS else ["statsmodels"],
        "winfont": ([] if _cjk_font_available()
                    else ["中文字体（Microsoft YaHei / SimHei / SimSun）"]),
    }


def pytest_collection_modifyitems(config, items):
    """环境不满足的标记用例自动跳过；未标记的用例永不受影响。"""
    checks = _missing_env()
    for item in items:
        for mark, gone in checks.items():
            if mark in item.keywords and gone:
                item.add_marker(pytest.mark.skip(
                    reason=f"{mark} 环境不满足：缺少 {'、'.join(gone)}"
                           f"（CI/全新克隆/无数据环境自动跳过，本机全量照跑）"))
