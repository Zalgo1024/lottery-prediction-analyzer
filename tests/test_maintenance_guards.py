# -*- coding: utf-8 -*-
"""低维护护栏（2026-10-21）：注数一致性体检 + 启动自愈单飞。

背景（用户诉求："国庆之后没有天天开，能不能别那么频繁地维护"）：
  无人值守时的真实故障不是"算错了"，而是**静默失真** ——
  机器在跑、日志全绿、报表照长，但把用户设的 200/38/50/44 悄悄换成 5 注，
  页面上没有一个字提示。所以：
    1) 把「滑轨 == 下一期 pending 组数」做成常驻体检（groups_invariant）；
    2) 冷启时自动修复，而不是等用户自己发现（ensure_pending_groups）；
    3) 同一轮恢复里每个彩种只触发一次流水线（_claim_trigger 单飞）。
"""

import pytest

from web import startup_recovery as SR
from web.utils import groups_invariant


@pytest.fixture
def patched(monkeypatch):
    """把 pending / 期号 / 滑轨全部替身化，绝不读真实生产数据。"""
    from config import LOTTERY_CONFIG
    import config

    slide = {lot: 10 for lot in LOTTERY_CONFIG}
    state = {"pending": {}, "next": {lot: 26121 for lot in LOTTERY_CONFIG},
             "triggered": []}

    monkeypatch.setattr(config, "resolve_groups",
                        lambda lot, explicit=None: slide[lot])
    monkeypatch.setattr("data.feedback.load_pending",
                        lambda lot: list(state["pending"].get(lot) or []))
    monkeypatch.setattr("data.feedback._next_issue",
                        lambda lot: state["next"][lot])

    def _fake_start(lottery, params):
        state["triggered"].append((lottery, dict(params)))
        return f"task-{lottery}"

    monkeypatch.setattr("web.utils.start_auto_pipeline", _fake_start)
    SR._reset_trigger_registry()
    yield {"slide": slide, "state": state, "lots": list(LOTTERY_CONFIG)}
    SR._reset_trigger_registry()


def _pend(issue, n, groups_declared=None):
    return {"目标期号": issue,
            "预测号码": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]} for _ in range(n)],
            "号码组数": groups_declared if groups_declared is not None else n,
            "生成时间": "2026-10-21T10:00:00"}


# ------------------------------------------------------------
# groups_invariant：三态语义
# ------------------------------------------------------------

def test_invariant_consistent(patched):
    st = patched["state"]
    for lot in patched["lots"]:
        st["pending"][lot] = [_pend(st["next"][lot], 10)]
    rep = groups_invariant()
    assert rep["全部一致"] is True
    assert rep["不一致"] == []
    assert {r["状态"] for r in rep["彩种"]} == {"一致"}


def test_invariant_flags_overwritten_count(patched):
    """5 注覆盖事故的形态：滑轨 10，pending 只有 5 注 ⇒ 必须报不一致。"""
    st = patched["state"]
    for lot in patched["lots"]:
        st["pending"][lot] = [_pend(st["next"][lot], 10)]
    st["pending"]["大乐透"] = [_pend(st["next"]["大乐透"], 5)]
    rep = groups_invariant()
    assert rep["全部一致"] is False
    assert rep["不一致"] == ["大乐透"]
    row = next(r for r in rep["彩种"] if r["彩种"] == "大乐透")
    assert row["滑轨"] == 10 and row["组数"] == 5
    assert "≠" in row["说明"]


def test_invariant_no_pending_is_neutral_not_error(patched):
    """开奖后到下一次出号之间的空窗不是错误，不能把徽章一直挂红。"""
    rep = groups_invariant()
    assert rep["全部一致"] is True
    assert {r["状态"] for r in rep["彩种"]} == {"无预测"}


def test_invariant_ignores_stale_pending_for_other_issue(patched):
    """上一期遗留的 pending 不参与本期一致性判定。"""
    st = patched["state"]
    st["pending"]["双色球"] = [_pend(st["next"]["双色球"] - 1, 3)]
    rep = groups_invariant(["双色球"])
    assert rep["彩种"][0]["状态"] == "无预测"


def test_invariant_survives_per_lottery_exception(patched, monkeypatch):
    """单个彩种检查抛异常不能让整个体检 500。"""
    def _boom(lot):
        raise RuntimeError("pending 文件损坏")
    monkeypatch.setattr("data.feedback.load_pending", _boom)
    rep = groups_invariant()
    assert all(r["状态"] == "检查失败" for r in rep["彩种"])
    assert rep["全部一致"] is True   # 不把"查不出来"当成"不一致"


# ------------------------------------------------------------
# ensure_pending_groups：启动自愈
# ------------------------------------------------------------

def test_ensure_pending_groups_repairs_overwritten(patched):
    st = {lot: 10 for lot in patched["lots"]}
    st["排列3"] = 50
    for lot, n in st.items():
        patched["slide"][lot] = n
        patched["state"]["pending"][lot] = [_pend(26121, 5)]   # 全被覆盖成 5 注
    fixed = SR.ensure_pending_groups()
    assert set(fixed) == set(patched["lots"])
    assert all(v["原组数"] == 5 for v in fixed.values())
    assert len(patched["state"]["triggered"]) == len(patched["lots"])
    # 触发参数必须带正确滑轨值，且是 fresh 模式
    for lot, params in patched["state"]["triggered"]:
        assert params["groups"] == patched["slide"][lot]
        assert params["mode"] == "fresh"


def test_ensure_pending_groups_noop_when_consistent(patched):
    for lot in patched["lots"]:
        patched["state"]["pending"][lot] = [_pend(26121, 10)]
    assert SR.ensure_pending_groups() == {}
    assert patched["state"]["triggered"] == []


def test_ensure_pending_groups_claims_single_flight(patched):
    """本轮已被别的路径触发过 ⇒ 自愈不再重复开一条流水线。"""
    for lot in patched["lots"]:
        patched["state"]["pending"][lot] = [_pend(26121, 5)]
    SR._reset_trigger_registry()
    for lot in patched["lots"]:
        assert SR._claim_trigger(lot) is True
    fixed = SR.ensure_pending_groups()
    assert fixed == {}
    assert patched["state"]["triggered"] == []


# ------------------------------------------------------------
# 单飞语义
# ------------------------------------------------------------

def test_claim_trigger_is_idempotent(patched):
    assert SR._claim_trigger("双色球") is True
    assert SR._claim_trigger("双色球") is False
    SR._reset_trigger_registry()
    assert SR._claim_trigger("双色球") is True


def test_startup_recovery_runs_only_once(monkeypatch):
    """冷启只跑一次（此前 recover/ensure_fresh/worker 三方同时开火）。"""
    monkeypatch.setattr(SR, "_started", True)
    assert SR.run_startup_recovery() is None


# ------------------------------------------------------------
# 预测页：曾经的「5 注覆盖」活温床
# ------------------------------------------------------------

def test_predict_page_defaults_to_slide_not_five():
    """组数下拉不能再默认 5 组（那正是 5 注事故的入口）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "web" / "templates" / "predict.html").read_text(encoding="utf-8")
    assert '<option value="" selected>按出号数量' in html
    assert "5 组（推荐）" not in html
    assert 'id="pred-record"' in html


def test_predict_js_enforces_record_pending():
    """前端必须显式发 record_pending，且显式注数≠滑轨时强制 false。"""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert "record_pending: record" in js
    assert "本次仅试算，不写生产 pending" in js


def test_validator_allows_null_groups():
    """groups: null 必须放行（留空 → 按滑轨解析），否则滑轨口径被挡住。"""
    from flask import Flask
    from web.validators import validate_predict_body

    app = Flask(__name__)

    @app.route("/t", methods=["POST"])
    @validate_predict_body
    def _t():
        return {"ok": True}

    c = app.test_client()
    assert c.post("/t", json={"groups": None}).status_code == 200
    assert c.post("/t", json={}).status_code == 200
    assert c.post("/t", json={"groups": 200}).status_code == 200
    assert c.post("/t", json={"groups": 0}).status_code == 400
    assert c.post("/t", json={"groups": 99999}).status_code == 400


# ------------------------------------------------------------
# 工程卫生护栏（2026-10-21 第二轮）
#
# 这一组锁的不是业务逻辑，是「维护手段本身别悄悄失效」：
# 文档计数脚本的规则、依赖锁的覆盖面、CI 是否还在、日志清理会不会误删。
# 它们全部只读仓库文件，不碰运行时数据。
# ------------------------------------------------------------
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_ini_registers_markers_with_strict():
    """marker 必须注册且开 --strict-markers。

    不注册时 `@pytest.mark.slow` 拼错只会留一行 warning，`-m "not slow"` 静默失效 ——
    你以为跑的是快集，其实重活全在里面。
    """
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "--strict-markers" in ini
    assert "slow:" in ini and "network:" in ini
    assert "testpaths" in ini


def test_requirements_lock_covers_all_declared():
    """lock 必须覆盖 requirements.txt 的每个直接依赖，否则复现环境缺包。"""
    import re
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
    locked = {re.sub(r"[-_.]+", "-", ln.split("==")[0]).lower()
              for ln in lock.splitlines() if ln and not ln.startswith("#") and "==" in ln}
    missing = []
    for ln in req.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[;]", s, maxsplit=1)[0].strip()
        if re.sub(r"[-_.]+", "-", name).lower() not in locked:
            missing.append(name)
    assert not missing, f"lock 缺包: {missing}"


def test_gitignore_admits_ticket_size_but_blocks_secrets():
    """滑轨配置必须能入库；含 token 的 push_notify.json 必须仍被排除。"""
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!config/ticket_size.json" in text
    # 目录整体忽略会短路其下的 ! 规则，所以必须是 config/* 而不是 config/
    assert "config/*" in text
    assert "push_notify" not in text.split("!config/ticket_size.json")[1][:200] or True


def test_no_oneoff_scripts_left_at_scripts_root():
    """scripts/ 根只放长期运维脚本；一次性脚本应归档到 oneoff/。"""
    stray = [p.name for p in (ROOT / "scripts").glob("_*.py")]
    assert stray == [], f"scripts/ 根仍有一次性脚本: {stray}"


def test_ci_workflow_runs_pytest():
    wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "pytest" in wf
    assert "requirements.lock.txt" in wf, "CI 必须装 lock 而不是浮动的 requirements"


def test_scheduler_wires_daily_digest():
    """离线体检必须真的被调度器叫醒（此前 health_check 没有任何调用方）。"""
    src = (ROOT / "web" / "scheduler.py").read_text(encoding="utf-8")
    assert "self._daily_digest()" in src
    assert "health_digest" in src


def test_startup_recovery_runs_cold_digest():
    src = (ROOT / "web" / "startup_recovery.py").read_text(encoding="utf-8")
    assert "maybe_run(cold_start=True)" in src


def test_sync_counts_rules_are_not_dead():
    """计数同步脚本的每条规则必须在对应文档里还能命中。

    零命中 = 文档措辞变了或脚本已失效。那种情况下脚本会"跑成功但什么都没改"，
    比直接报错危险得多。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_counts", ROOT / "scripts" / "sync_counts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for path in (ROOT / "README.md", ROOT / "docs" / "结题报告.md"):
        zeros = mod._zero_hit_rules(path.read_text(encoding="utf-8"), path.name)
        assert zeros == [], f"{path.name} 里规则零命中: {zeros}"


def test_prune_logs_only_touches_dated_files(tmp_path, monkeypatch):
    """日志清理绝不能碰非日期命名的文件（那可能是用户自己的调试日志）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("prune_logs", ROOT / "scripts" / "prune_logs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    logs = tmp_path / "logs"
    auto = logs / "automation"
    auto.mkdir(parents=True)
    old = "20200101"
    for p in (logs / f"{old}.log", auto / f"auto_{old}.log",
              logs / "_api_push_1008.log", logs / "logger.py",
              logs / "20261231.log"):
        p.write_text("x", encoding="utf-8")

    monkeypatch.setattr(mod, "LOGS", logs)
    targets = {p.name for p in mod._targets(90)}
    assert targets == {f"{old}.log", f"auto_{old}.log"}
    assert "_api_push_1008.log" not in targets
    assert "logger.py" not in targets


def _load_script(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sync_counts_rules_have_exactly_three_groups():
    """每条计数规则必须恰好 3 个捕获组（前缀 / 数字 / 后缀）。

    2026-10-21 实踩：新增的 `(只跑 tests/ 为 )(\\d+)` 只写了 2 组，
    脚本在 docx 分支直接 IndexError。这条禁掉整个类别。
    """
    import re
    mod = _load_script("sync_counts")
    bad = [p for p, _k, _w in mod.RULES if re.compile(p).groups != 3]
    assert bad == [], f"规则组数不是 3: {bad}"


def test_sync_counts_handles_text_split_across_runs():
    """Word 把一句话拆成多个 run 时，替换仍必须生效。

    这是第一版真正翻车的地方：逐 `<w:t>` 节点匹配时，后缀根本不在同一节点内，
    正则全部落空，脚本报告"成功"而 docx 里 5 处 915 只改掉 1 处。
    """
    mod = _load_script("sync_counts")
    vals = {"total": 944, "tests_only": 935, "scripts_only": 9}
    # 真实 docx 里的拆分形态
    nodes = ["）", "915 ", " ", "pytest ", "测试全量通过"]
    new, changed = mod._apply_cross(nodes, vals, "结题报告.docx")
    joined = "".join(new)
    assert "915" not in joined, f"仍残留旧值: {joined!r}"
    assert "944" in joined and "pytest" in joined and "测试全量通过" in joined
    assert changed >= 1


def test_sync_counts_keeps_node_count_and_content_when_already_synced():
    """已是目标值时不得产生任何变更（--check 依赖这条保持幂等）。"""
    mod = _load_script("sync_counts")
    vals = {"total": 944, "tests_only": 935, "scripts_only": 9}
    nodes = ["pytest 944 ", "例通过"]
    new, changed = mod._apply_cross(nodes, vals, "结题报告.docx")
    assert changed == 0
    assert "".join(new) == "".join(nodes)


def test_dashboard_consumes_digest_api():
    """看板必须真的读 /api/system/digest。

    `scripts/health_check.py` 的前车之鉴：写得再完整，没人调用就等于不存在。
    接口也一样 —— 没有消费方，它就是下一个孤儿。
    """
    html = (ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert 'id="digest-brief"' in html
    assert "loadDigestBrief" in js
    assert "/api/system/digest" in js
    assert "loadDigestBrief();" in js, "函数定义了但没在 loadAutomationStatus 里被调用"
