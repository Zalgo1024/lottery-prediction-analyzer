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

import re
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
    """点完按钮必须能看到结果：轮询状态、显示新批注数、并明说旧批被保留。"""
    m = re.search(r"function\s+tsRegenerate\s*\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "找不到 tsRegenerate"
    body = m.group(1)
    assert "/api/predict/status/" in body, "重出后没有轮询任务状态（用户看不到结果）"
    assert "上一批注数" in body, "没有提示旧批注数 ⇒ 用户无法确认旧号还在"
    assert "重出批号" in body, "没有显示是第几批"


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

