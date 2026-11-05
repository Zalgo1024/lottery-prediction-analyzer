# -*- coding: utf-8 -*-
"""tests/test_ticket_size_collect_contract.py —— 「注数改了保存又变回 5」的回归闸。

事故（2026-11-02，用户报障）：
    「出号数量」卡里把七乐彩 / 快乐8 改成别的注数，点「保存」之后页面自己变回 5。
    服务端日志一切正常 —— 它确实收到了一个**语法合法、业务合法**的请求体：
        {"mode": "dynamic", "dynamic": {}}
    因为前端收集函数用的是「彩种在 JS 数组里的下标」拼 DOM id：
        for (const [lot, i] of wbLotteries().entries()) { const num = $('ts-num-' + i); }
    `Array.prototype.entries()` 给的是 `[下标, 值]`，于是 `lot`=下标、`i`=彩种名，
    拼出 `ts-num-双色球` —— 这个元素不存在 ⇒ `collect` 永远是空对象。

★ 本文件是**静态契约测试**，只用正则/字符串检查源码，不执行 JS、不解析 DOM。
  它挡不住「另一种全新写法同样取不到元素」，只能保证**这一种已确证的错法**不会回来。
  运行时证据另做：用真实 Chrome + CDP 打开看板、改 137/42、点保存，再读
  `config/ticket_size.json` 确认落盘（当时实测通过）。两者缺一不可 ——
  静态测试说「代码长得对」，真实浏览器说「它真的工作」。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN_JS = ROOT / "web" / "static" / "js" / "main.js"


@pytest.fixture(scope="module")
def js() -> str:
    assert MAIN_JS.exists(), f"找不到前端主文件：{MAIN_JS}"
    return MAIN_JS.read_text(encoding="utf-8")


def _code_lines(js: str) -> str:
    """去掉整行注释后的源码。

    ★ 为什么需要：事故的**注释**里就写着 `ts-num-${tsIdx(lottery)}`（讲这个坑怎么来的），
      不剔注释的话，「禁止出现该模式」的检查会被自己的说明文字绊倒 —— 一条假失败。
    只剔「整行是注释」的行，不做字符串级解析：`//` 出现在字符串里（URL）时
    误剔会漏检，误留只会在注释行里 —— 选误留，宁可多看一眼也不漏掉真代码。
    """
    keep = []
    for line in js.splitlines():
        s = line.lstrip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        keep.append(line)
    return "\n".join(keep)


def test_no_index_based_dom_ids_for_ticket_size(js: str):
    """注数输入框不许再用 `ts-num-<下标>` / `ts-range-<下标>` 这类拼接 id。

    这批 id 一旦拼错（未登记彩种回落 99、解构写反）就是静默空提交，
    界面上表现为「保存了但值回退」，两端都不报错 —— 最难查的那一类。
    """
    bad = re.findall(r"""['"`]ts-(?:num|range|cur)-\$?\{?[^'"`\n]*""", _code_lines(js))
    assert not bad, (
        "注数输入框又用回了 id 拼接（会退化成静默空提交）：" + repr(bad[:5]))


def test_collect_uses_data_attributes(js: str):
    """收集必须走 `data-role="num"` + `data-lottery` 属性，不依赖任何下标。"""
    m = re.search(r"function\s+tsCollectValues\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "找不到 tsCollectValues —— 收集逻辑被改名/删除了？本测试需要跟着改。"
    body = m.group(1)
    assert 'data-role="num"' in body, "收集没有按 data-role 定位取数框"
    assert "data-lottery" in body, "收集没有按 data-lottery 定位彩种"
    assert "entries()" not in body, (
        "收集逻辑里又出现了 entries() —— 上次就是把 [下标, 值] 解构成 [彩种, 下标] 出的锅")


def test_render_emits_data_lottery_on_both_inputs(js: str):
    """渲染出来的滑轨与数字框都必须带 `data-lottery`，否则收集侧取不到。"""
    for role in ("range", "num"):
        pat = re.compile(r'data-role="%s"\s+data-lottery=' % role)
        assert pat.search(js), f'注数渲染缺少 data-role="{role}" + data-lottery 组合'


def test_save_refuses_empty_collection(js: str):
    """收集到的彩种数与页面渲染数不一致时必须**拒发请求**。

    这是事故的直接放大器：空 payload 让后端「合法地什么都没改」，
    若没有这道闸，用户只能看到值回退而没有任何错误提示。
    """
    m = re.search(r"function\s+tsSave\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "找不到 tsSave"
    body = m.group(1)
    assert "expect" in body and "got" in body, "保存逻辑缺少「应收集数 vs 实收集数」比对"
    assert re.search(r"got\s*!==\s*expect|expect\s*!==\s*got", body), \
        "保存逻辑没有在不一致时拦下请求"


def test_save_writes_only_current_mode_field(js: str):
    """只写当前模式那一个字段：动态模式写 dynamic，不能顺手覆盖 fixed 意图锚点。"""
    m = re.search(r"function\s+tsSave\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    body = m.group(1)
    assert "mode === 'dynamic' ? 'dynamic' : 'fixed'" in body, \
        "保存逻辑没有按当前模式二选一，两个字段一起提交会把 fixed 锚点覆盖掉"


def test_regenerate_button_is_wired(js: str):
    """「重新出号」按钮：卡片里在，handler 在，且打到 /api/predict/regenerate。"""
    assert 'data-regenerate=' in js, "注数卡片里没有「重新出号」按钮"
    assert re.search(r"function\s+tsRegenerate\s*\(", js), "缺少 tsRegenerate 处理函数"
    assert "/api/predict/regenerate" in js, "前端没有调用重出接口"


def test_regenerate_handler_polls_and_reports_kept_old_batch(js: str):
    """点完按钮必须能看到结果：轮询状态、显示新批注数、并明说旧批被保留。

    2026-11-05 起这份流程在 `_tsRegenerateOne`（单彩种按钮与「统一重新出号」
    共用）；`tsRegenerate` 只负责确认框、禁用按钮与展示结果。
    """
    m = re.search(r"function\s+_tsRegenerateOne\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "找不到 _tsRegenerateOne —— 重出流程不再有单一真源？"
    body = m.group(1)
    assert "/api/predict/status/" in body, "重出后没有轮询任务状态（用户看不到结果）"
    assert "上一批注数" in body, "没有读旧批注数 ⇒ 用户无法确认旧号还在"
    assert "重出批号" in body, "没有显示是第几批"
    assert "/api/predict/regenerate" in body, "核心流程没有打到重出接口"
    single = re.search(r"function\s+tsRegenerate\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert single, "找不到 tsRegenerate"
    assert "_tsRegenerateOne(" in single.group(1), "单彩种按钮没走共用核心"
    assert "旧批" in single.group(1), "单彩种按钮没有提示旧批被保留"


def test_regenerate_route_registered():
    """后端路由必须真的注册上（拒绝分支/成功分支都要能走到）。"""
    from web.app import app

    rules = {(r.rule, tuple(sorted(r.methods & {"GET", "POST"}))) for r in app.url_map.iter_rules()}
    assert ("/api/predict/regenerate", ("POST",)) in rules, \
        "重新出号接口没有注册到 Flask 应用上"


def test_regenerate_route_rejects_unknown_lottery_at_runtime():
    """未知彩种要 400，不能悄悄按默认彩种出一批号（静默兜底在这里是灾难）。"""
    from web.app import app

    r = app.test_client().post("/api/predict/regenerate", json={"lottery": "不存在"})
    assert r.status_code == 400
    body = r.get_json()
    assert body.get("ok") is False and "未知彩种" in (body.get("error") or "")


# ---------------------------------------------------------------- 运行时往返
@pytest.mark.parametrize("mode", ["fixed", "dynamic"])
def test_saving_new_lottery_count_round_trips(tmp_path, monkeypatch, mode):
    """真跑一遍保存接口：七乐彩/快乐8 写成非默认值后，回读必须是新值，不是 5。

    ★ 这是**后端往返**证据，用 Flask test client 打在真 app 上，但把配置落盘路径
      `ticket_size.STORE_PATH` 指到 tmp —— 绝不能写生产配置：滑轨一旦与
      pending 组数不等，启动自愈会按新注数**真出一批号**（真金白银）。
    ★ 两种模式都要测：服务端只接受「当前生效模式」那一个字段，只测默认的 fixed
      会漏掉 dynamic 分支 —— 而用户报障时卡片正是停在**动态**。
    ★ 它证明不了「点按钮那一刻前端提交了什么」；那部分由上面的静态契约 +
      真实 Chrome 实测共同覆盖。三层各自只看一段，不能互相顶替。
    """
    from data import ticket_size as ts
    from web.app import app

    monkeypatch.setattr(ts, "STORE_PATH", tmp_path / "ticket_size.json")
    cfg = ts.sanitize({})
    cfg["mode"] = mode
    ts.save_config(cfg)

    c = app.test_client()
    assert c.get("/api/settings/ticket-size").get_json()["mode"] == mode

    targets = {"七乐彩": 137, "快乐8": 42}
    r = c.post("/api/settings/ticket-size", json={"mode": mode, mode: targets})
    assert r.status_code == 200, r.get_json()
    assert r.get_json().get("ok") is True

    # ① 回读响应里的「当前生效」就是新值
    got = {it["lottery"]: it for it in r.get_json()["lotteries"]}
    for lot, want in targets.items():
        assert got[lot]["当前生效"] == want, (
            f"[{mode}] {lot} 保存后当前生效 = {got[lot]['当前生效']}，期望 {want}（又变回去了？）")

    # ② 另起一次 GET（重新读盘）也还是新值 —— 挡住「只在响应里改、没落盘」
    fresh = {it["lottery"]: it for it in
             c.get("/api/settings/ticket-size").get_json()["lotteries"]}
    for lot, want in targets.items():
        assert fresh[lot]["当前生效"] == want, \
            f"[{mode}] {lot} 重新读盘后回退到 {fresh[lot]['当前生效']}"

    # ③ 原样落盘到文件（第三层：绕过接口直接看盘）
    import json as _json
    on_disk = _json.loads((tmp_path / "ticket_size.json").read_text(encoding="utf-8"))
    for lot, want in targets.items():
        assert on_disk[mode][lot] == want, f"[{mode}] 盘上 {lot}={on_disk[mode][lot]}，期望 {want}"


def test_empty_count_payload_is_rejected_by_server(tmp_path, monkeypatch):
    """空字典必须 400 —— 这是当年「保存了但什么都没改」的服务端那道闸。"""
    from data import ticket_size as ts
    from web.app import app

    monkeypatch.setattr(ts, "STORE_PATH", tmp_path / "ticket_size.json")
    ts.save_config(ts.sanitize({}))

    c = app.test_client()
    mode = c.get("/api/settings/ticket-size").get_json()["mode"]
    key = "dynamic" if mode == "dynamic" else "fixed"
    r = c.post("/api/settings/ticket-size", json={"mode": mode, key: {}})
    assert r.status_code == 400
    assert r.get_json().get("ok") is False


# ---------------------------------------------------------------- 统一重新出号（2026-11-05）
DASHBOARD = ROOT / "web" / "templates" / "dashboard.html"


def test_regenerate_all_button_is_next_to_reset(js: str):
    """「恢复默认」旁边必须有统一重新出号按钮，且 handler 与防重点击的 id 都在。"""
    html = DASHBOARD.read_text(encoding="utf-8")
    assert re.search(r'onclick="tsReset\(\)".{0,500}?onclick="tsRegenerateAll\(\)"', html, re.S), \
        "「恢复默认」旁边没有统一的重新出号按钮"
    assert 'id="ts-regen-all-btn"' in html, "批量按钮缺 id（无法禁用以防重复点击）"
    assert re.search(r"function\s+tsRegenerateAll\s*\(", js), "缺少 tsRegenerateAll 处理函数"


def test_regenerate_all_button_is_blue():
    """★ 2026-11-05 用户要求：「统一重新出号」按钮换成蓝色。

    页面里 `btn-primary` 是青绿 #4ECDC4，只有 `btn-info`(--info #45B7D1) 是蓝色；
    同时锁住 CSS 变量，避免日后改 :root 让「蓝色按钮」悄悄变绿/变黄。
    """
    html = DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r'<button\s+class="([^"]*)"\s+id="ts-regen-all-btn"', html)
    assert m, "找不到统一重新出号按钮"
    cls = m.group(1).split()
    assert "btn-info" in cls, "统一重新出号按钮不是蓝色（缺 btn-info）"
    assert "btn-warning" not in cls, "统一重新出号按钮仍是黄色 warning"
    css = (ROOT / "web" / "static" / "css" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"--info:\s*#45B7D1", css), "蓝色变量被改，按钮不再是约定的蓝色"
    assert re.search(r"\.btn-info\s*\{\s*background:\s*var\(--info\)", css), "btn-info 不再取蓝色变量"


def test_regenerate_all_targets_come_from_rendered_cards(js: str):
    """批量清单必须从渲染出来的卡片反推，不许再手写一份（半份）彩种数组。

    2026-11-05 的看板事故：某处手写 `lots: ['福彩3D']`，后端七乐彩/快乐8 明明
    在跑，卡片却静默消失。这里连「只写一个彩种」也要挡住。
    """
    m = re.search(r"function\s+_tsRegenTargets\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "找不到 _tsRegenTargets"
    body = m.group(1)
    assert "#ts-rows [data-regenerate]" in body, "批量清单没有从卡片 DOM 反推"
    assert "getAttribute('data-regenerate')" in body, "没有按属性取彩种名"
    from config import LOTTERY_CONFIG
    for lot in LOTTERY_CONFIG:
        assert lot not in body, f"_tsRegenTargets 里手写了彩种「{lot}」"


def test_regenerate_all_is_serial_shares_core_and_never_aborts(js: str):
    """★ 串行 + 共用核心 + 单彩种失败不中断，三条一起锁。

    `ev/budget_gate.record_spend` 是「读盘 → 改 → 原子替换」，没有跨线程互斥；
    8 个后台任务并发记账会互相覆盖 ⇒ 少记钱 ⇒ 闸门变松且不报错。
    """
    m = re.search(r"function\s+tsRegenerateAll\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "找不到 tsRegenerateAll"
    body = m.group(1)
    assert "await _tsRegenerateOne(" in body, "批量按钮没走共用核心（两套轮询判据会漂移）"
    assert "Promise.all" not in body, "批量按钮并发启动 ⇒ 预算台账读改写会互相覆盖"
    assert "for (" in body, "批量按钮没有逐个串行循环"
    assert "break" not in body, "某彩种失败就中止 ⇒ 用户不知道后面几个到底跑没跑"
    assert "disabled = true" in body and "finally" in body, "没有防重复点击 / 收尾恢复"
    assert "_tsRegenSummary(results)" in body, "没有走三档汇总"
    # 这个按钮会真的产生新批次、按批记账 ⇒ 确认框必须给出合计成本，
    # 不能只问一句「确定吗」（钉关键词/读数，不钉整段文案）。
    assert "byLot" in body and "当前生效" in body, "确认框没有取各彩种当前生效注数"
    assert "total" in body and "合计约" in body, "确认框没有给合计注数"


def test_regenerate_all_summary_has_three_distinct_outcomes(js: str):
    """汇总必须分「全部成功 / 部分成功 / 全部失败」，不许用 any(ok) 代替。"""
    m = re.search(r"function\s+_tsRegenSummary\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "找不到 _tsRegenSummary"
    body = m.group(1)
    for label in ("全部成功", "部分成功", "全部失败"):
        assert label in body, f"汇总缺少「{label}」"
    assert "good.length" in body and "bad.length" in body, "汇总没有分别数成功/失败"
    assert "未执行" in body and "results.length" in body, \
        "空结果没有显式写「未执行」（0 个成功 ≠ 全部成功）"


def test_regenerate_all_summary_three_outcomes_at_runtime():
    """真跑一遍汇总函数：部分成功不能被读成「成功」，空结果也不能被读成通过。"""
    if not shutil.which("node"):
        pytest.skip("本机无 node，跳过汇总判据真跑")
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const a = src.indexOf('function _tsRegenSummary');
const b = src.indexOf('async function tsRegenerateAll');
const M = new Function(src.slice(a, b) + '; return {_tsRegenSummary};')();
const ok = l => ({lottery: l, ok: true, n: 10, rn: 1, prevN: 5});
const bad = l => ({lottery: l, ok: false, reason: '停发'});
const cases = [
  M._tsRegenSummary([ok('A'), ok('B')]),
  M._tsRegenSummary([ok('A'), bad('B')]),
  M._tsRegenSummary([bad('A'), bad('B')]),
  M._tsRegenSummary([]),
];
console.log(JSON.stringify(cases.map(c => ({head: c.head, ok: c.成功数, bad: c.失败数}))));
"""
    cp = subprocess.run(["node", "-e", script, str(MAIN_JS)],
                        capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, cp.stderr
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert "全部成功" in got[0]["head"] and (got[0]["ok"], got[0]["bad"]) == (2, 0)
    assert "部分成功" in got[1]["head"] and (got[1]["ok"], got[1]["bad"]) == (1, 1), \
        "有一个成功就被当成全部成功（any(ok) 陷阱）"
    assert "全部失败" in got[2]["head"] and (got[2]["ok"], got[2]["bad"]) == (0, 2)
    assert "未执行" in got[3]["head"] and got[3]["ok"] == 0, \
        "空结果被当成通过（缺席 ≠ 通过）"


def test_regenerate_all_targets_match_rendered_cards_at_runtime():
    """真跑清单收集：页面画了几张卡就重出几个彩种，一个不少、顺序一致。"""
    if not shutil.which("node"):
        pytest.skip("本机无 node，跳过清单收集真跑")
    from config import LOTTERY_CONFIG
    names = list(LOTTERY_CONFIG)
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const a = src.indexOf('function _tsRegenTargets');
const b = src.indexOf('function _tsRegenSummary');
const names = JSON.parse(process.argv[2]);
global.document = {querySelectorAll: () => names.map(n => ({getAttribute: () => n}))};
const M = new Function(src.slice(a, b) + '; return {_tsRegenTargets};')();
const got = M._tsRegenTargets();
global.document = {querySelectorAll: () => []};   // 卡片未加载 / 渲染失败
console.log(JSON.stringify({got: got, empty: M._tsRegenTargets()}));
"""
    cp = subprocess.run(["node", "-e", script, str(MAIN_JS), json.dumps(names, ensure_ascii=False)],
                        capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, cp.stderr
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert got["got"] == names, "批量清单与渲染出来的卡片不一致（多了/少了/顺序变了）"
    assert got["empty"] == [], "0 张卡片时必须返回空清单由调用方显式提示，不许静默当成功"

