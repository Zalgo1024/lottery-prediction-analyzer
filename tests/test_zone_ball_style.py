# -*- coding: utf-8 -*-
"""分区球面样式的「单一真源」回归锁（2026-11-05）。

背景：七乐彩是 30 选 7 的**乐透型**（无放回选号），却一直被当成「第N位」那种
按位数字型，落到 `ZONE_PALETTE[0]` 的纯红平球；双色球/大乐透走的是
`.red-ball` / `.blue-ball` 渐变亮面球。用户要求「七乐彩改成双色球同款」，
于是 基本号 = red、特别号 = blue。

三条必须同时守住，少一条就会静默错回去（不报错、只是看不出差别）：
  1. 前端 `zoneClsFor()` 与后端 `web.utils._zone_cls()` 口径逐项一致 ——
     只改一边就是「卡片亮面球、命中页平涂球」。
  2. 所有号码渲染调用点都要传彩种 —— 曾有 5 处手写分区配色，改一处漏四处。
  3. 票面**绝不**为了配色补出特别号 —— `choose=0/draw=1`（draw_only，票面不含），
     只在开奖/命中对照里出现。凭空补一个蓝球 = 把开奖号混进预测（前视）。
     `data/push_image` 早已按「基本号 idx0 红 + 特别号 idx1 蓝」出图，口径同源。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN_JS = ROOT / "web" / "static" / "js" / "main.js"

# 前端真源里钉死的七乐彩映射（与 web.utils._zone_cls 必须逐项一致）
EXPECTED_QLC = {"基本号": "red", "特别号": "blue"}


def _js_text() -> str:
    return MAIN_JS.read_text(encoding="utf-8")


def _frontend_map() -> dict:
    """从 main.js 源码里解析 LOTTERY_ZONE_CLS（不执行 JS，纯文本对账）。"""
    text = _js_text()
    m = re.search(r"const\s+LOTTERY_ZONE_CLS\s*=\s*\{(.*?)\n\};", text, re.S)
    assert m, "main.js 里找不到 LOTTERY_ZONE_CLS —— 分区配色真源被删/改名了？"
    out = {}
    for lot, body in re.findall(r"'([^']+)'\s*:\s*\{([^}]*)\}", m.group(1)):
        out[lot] = dict(re.findall(r"'([^']+)'\s*:\s*'(red|blue)'", body))
    return out


# ---------------------------------------------------------------- ① 真源本身
def test_frontend_zone_style_source_pins_qlc():
    """七乐彩必须显式登记「基本号红 / 特别号蓝」，不能靠调色板顺序凑出来。"""
    assert _frontend_map().get("七乐彩") == EXPECTED_QLC


@pytest.mark.parametrize("zone,cls", sorted(EXPECTED_QLC.items()))
def test_backend_cls_matches_frontend_and_requires_lottery(zone, cls):
    """后端同口径；且**不传彩种必须退回旧口径**（防止有人把映射做成全局改色）。"""
    from web.utils import _zone_cls

    assert _zone_cls(zone, "七乐彩") == cls
    assert _frontend_map()["七乐彩"][zone] == cls
    assert _zone_cls(zone) is None, f"{zone} 没传彩种却变了色 ⇒ 会误伤快乐8/数字型"


@pytest.mark.parametrize("lot,zone,expected", [
    ("双色球", "红球", "red"),
    ("双色球", "蓝球", "blue"),
    ("大乐透", "蓝球", "blue"),
    ("排列3", "第1位", None),
    ("快乐8", "基本号", None),     # 本次只改七乐彩，快乐8 保持原调色板
    ("七乐彩", "红球", "red"),      # 彩种定制表里查不到 ⇒ 回落通用规则（与前端一致）
    (None, "基本号", None),
])
def test_backend_cls_fallback_is_stable(lot, zone, expected):
    from web.utils import _zone_cls

    assert _zone_cls(zone, lot) == expected


# ---------------------------------------------------------------- ② 调用点
def test_all_renderers_use_the_single_source():
    """号码渲染不许再手写 `cls: null` / 红蓝三元表达式。

    ★ 例外：冷热号 Top5 卡片的 `ballCls`（约 L1984）画的是热号/冷号徽章，
      不是分区号码，统一红球是刻意的 —— 判据只针对 `cls:` 与 `name === '红球'`。
    """
    text = _js_text()
    assert "cls: null" not in text, "又出现手写空配色（数字型调色板会吞掉七乐彩）"
    assert "name === '红球' ? 'red'" not in text, "又出现手写红蓝分区配色"
    hits = re.findall(r"cls:\s*zoneClsFor\(", text)
    assert len(hits) >= 5, f"走真源的渲染路径不足：{len(hits)} 处"


def test_every_number_render_call_site_passes_lottery():
    """每个 renderPredNumbers / renderDrawNumbers 调用点都必须带彩种。

    漏一处 = 那个页面上的七乐彩静默退回纯红平球，而且**不报错**；
    这类「改了主卡片、别的页面没改」正是本轮要堵的洞。
    """
    lines = _js_text().splitlines()
    pred = [ln for ln in lines if "renderPredNumbers(" in ln
            and "function renderPredNumbers" not in ln]
    draw = [ln for ln in lines if "renderDrawNumbers(" in ln
            and "function renderDrawNumbers" not in ln]
    assert len(pred) == 5, f"预测号码渲染调用点数变了：{len(pred)}"
    assert len(draw) == 3, f"开奖号码渲染调用点数变了：{len(draw)}"
    for ln in pred:
        assert "lottery" in ln, f"预测渲染没传彩种：{ln.strip()}"
        assert "lottery: title" not in ln, \
            f"把占位标题「最近一次运行」当彩种传了，配色会静默退回旧口径：{ln.strip()}"
    for ln in draw:
        assert re.search(r"renderDrawNumbers\(dn,\s*\w+\)", ln), \
            f"开奖渲染没传彩种：{ln.strip()}"
        assert ", title)" not in ln, \
            f"把占位标题当彩种传了：{ln.strip()}"


# ---------------------------------------------------------------- ③ 不造号
def test_draw_only_special_number_is_not_on_ticket():
    """特别号票面不含（choose=0/draw=1）—— 配色改动绝不许把它补进票面。"""
    from data.schema import get_schema

    zones = {z.name: z for z in get_schema("七乐彩").zones}
    assert zones["特别号"].choose == 0
    assert zones["特别号"].draw == 1
    assert zones["特别号"].draw_count == 1
    assert zones["基本号"].choose == 7


def test_push_image_zone_colors_are_the_same_red_blue_pair():
    """企业微信推送图早就是「基本号红 + 特别号蓝」，网页改色后两边口径要一致。"""
    from data.push_image import C_BLUE, C_RED, _zone_color, _zone_order

    zones = {"基本号": [1, 2, 3, 4, 5, 6, 7], "特别号": [8]}
    order = _zone_order(zones)
    assert order == ["基本号", "特别号"]
    assert _zone_color(order[0], 0) == C_RED
    assert _zone_color(order[1], 1) == C_BLUE


def test_real_frontend_render_is_ssq_style_and_does_not_invent_a_blue_ball():
    """用真实 main.js 渲染一次：票面 7 个亮面红球、零蓝球；开奖 7 红 + 1 蓝。

    这不是源码文本守卫，是真跑 —— 能抓住「映射加了但 renderZoneBalls 没吃到」。
    CI 无 node 时跳过（历史 40 项 CI 失败全是环境，别再伪装成 bug）。
    """
    if not shutil.which("node"):
        pytest.skip("本机无 node，跳过真实前端渲染对账")
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const a = src.indexOf('function toBallArray');
const b = src.indexOf('// ===== 数据状态面板');
const M = new Function(src.slice(a, b) + '; return {renderPredNumbers, renderDrawNumbers};')();
const num = {'基本号': [1, 3, 4, 15, 17, 19, 20]};
const ticket = M.renderPredNumbers(num, {size: 18, showLabels: false, lottery: '七乐彩'});
const draw = M.renderDrawNumbers({'号码': Object.assign({}, num, {'特别号': [8]})}, '七乐彩');
const kl8 = M.renderPredNumbers({'基本号': [1, 2, 3]}, {size: 18, showLabels: false, lottery: '快乐8'});
const count = (s, c) => (s.match(new RegExp('class="' + c + '"', 'g')) || []).length;
console.log(JSON.stringify({
  ticket_red: count(ticket, 'red-ball'), ticket_blue: count(ticket, 'blue-ball'),
  ticket_flat: ticket.includes('background:#E24B4A'),
  draw_red: count(draw, 'red-ball'), draw_blue: count(draw, 'blue-ball'),
  kl8_flat: kl8.includes('background:#E24B4A'),
}));
"""
    cp = subprocess.run(["node", "-e", script, str(MAIN_JS)],
                        capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, cp.stderr
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert got["ticket_red"] == 7, got
    assert got["ticket_blue"] == 0, f"票面凭空多出蓝球（前视）：{got}"
    assert got["ticket_flat"] is False, f"票面还是纯红平涂：{got}"
    assert got["draw_red"] == 7 and got["draw_blue"] == 1, got
    assert got["kl8_flat"] is True, "快乐8 被顺手改了色（超出本次要求）"
