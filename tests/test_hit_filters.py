# -*- coding: utf-8 -*-
"""命中条件（`zones` 参数 + `/api/hits/filters`）契约测试。

锁住的行为：

  1. **通用档上限 = 该彩种的理论最大命中数**（3D 只有 1~3，不许出现「总命中 ≥ 7」）；
  2. **专属档的每个 key 都必须能被后端解析、且分区名在该彩种 schema 内**
     （否则用户点了就 400 —— 选项与后端必须同源）；
  3. **「全部彩种」不给专属组**：同一个「红球 ≥4」在不同彩种含义不同，跨彩种套用
     会让数字型记录按不存在的字段被静默筛掉；
  4. `zones` 新口径与老的 `red_hits/blue_hits/total_hits` **结果必须一致**（保住老链接语义）；
  5. **不认识的分区名 / 格式错 / 非整数 / 负数 ⇒ 400**，绝不静默忽略
     —— 静默忽略会把「筛选失效」伪装成「没有数据」；
  6. **3D/排3/排5 的 83 条历史记录用「百位/千位/万位」作分区键**，必须被归一化到
     「第N位」，否则分区筛选会静默漏掉它们、命中详情还会把号码整行丢掉；
  7. 页面**不许手抄命中选项**（规则改了就悄悄过期），选项必须来自 `/api/hits/filters`；
  8. 条件不适用时必须**明确提示**，不许静默清空。

合成数据，不依赖真实 feedback 文件。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.feedback as fb
import data.prize_table as pt
from data.schema import get_schema
from web.app import app
from web.hit_filters import (
    group_hits,
    known_names,
    options_payload,
    parse_condition,
    validate_condition,
)
import web.routes.hits as hits_route

WEB = Path(__file__).resolve().parent.parent / "web"


def _rec(issue, grade, zones=None, red=None, blue=None, total=None, day="2026-09-02"):
    r = {
        "期号": issue,
        "中奖等级": grade,
        "valid_prediction": True,
        "评估时间": day + "T21:00:00",
        "预测日期": "2026-09-01",
        "开奖日期": day,
        "来源": "predict",
        "策略": "高频策略",
        "总命中": total if total is not None else 0,
    }
    if zones is not None:
        r["分区命中"] = zones
    if red is not None:
        r["红球命中"] = red
    if blue is not None:
        r["蓝球命中"] = blue
    return r


HIST = {
    "双色球": [
        _rec(1, "四等", red=4, blue=1, total=5),
        _rec(2, "六等", red=1, blue=1, total=2),
        _rec(3, "未中", red=1, blue=0, total=1),
    ],
    "福彩3D": [
        # 新键名（现行 schema）
        _rec(4, "直选", zones={"第1位": 1, "第2位": 1, "第3位": 1}, total=3),
        # 老键名（2026 年前后统一前的历史记录）
        _rec(5, "组选6", zones={"百位": 0, "十位": 0, "个位": 1}, total=1),
    ],
    "七星彩": [
        _rec(6, "六等奖",
             zones={"第1位": 0, "第2位": 0, "第3位": 0,
                    "第4位": 0, "第5位": 0, "第6位": 0, "第7位": 1}, total=1),
        _rec(7, "三等奖",
             zones={"第1位": 1, "第2位": 1, "第3位": 1,
                    "第4位": 1, "第5位": 1, "第6位": 0, "第7位": 1}, total=6),
    ],
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: HIST.get(name, []))
    monkeypatch.setattr(pt, "prize_payout",
                        lambda lottery, grade, issue=None: {
                            "金额": 5.0, "浮动": False, "来源": "固定奖金", "文本": "¥5", "备注": ""})
    monkeypatch.setattr(hits_route, "feedback_record_view",
                        lambda name, h: {"zones": [], "total_hit": 0, "total_choose": 0})
    app.config["TESTING"] = True
    return app.test_client()


# ------------------------------------------------------------
# 1. 选项内容（/api/hits/filters）
# ------------------------------------------------------------

def test_generic_upper_bound_is_theoretical_max(client):
    """通用档上限必须等于该彩种理论最大命中数：3D=3、排5=5、双/大/七星=7。"""
    expect = {"全部": 7, "双色球": 7, "大乐透": 7, "福彩3D": 3, "排列3": 3, "排列5": 5, "七星彩": 7}
    for lottery, top in expect.items():
        d = client.get("/api/hits/filters?lottery=" + lottery).get_json()
        assert d["上限"] == top, lottery
        assert len(d["通用"]) == top, lottery
        assert d["通用"][-1]["label"] == "总命中 ≥ %d" % top, lottery
        # 每个通用档都必须能被后端解析（选项与解析器同源）
        for opt in d["通用"]:
            cond = parse_condition(opt["key"])
            assert cond and list(cond) == ["总"], opt


def test_specific_options_are_all_parseable_and_within_schema(client):
    """专属档的 key 必须解析得通、分区名必须在该彩种 schema 内——否则点了就 400。"""
    for lottery in ("双色球", "大乐透", "福彩3D", "排列3", "排列5", "七星彩"):
        d = client.get("/api/hits/filters?lottery=" + lottery).get_json()
        spec = d["专属"]
        assert spec and spec["档位"], lottery
        assert lottery in spec["标题"], lottery
        names = set(known_names(lottery))
        schema_names = {z.name for z in get_schema(lottery).zones}
        for opt in spec["档位"]:
            cond = parse_condition(opt["key"])
            assert cond, (lottery, opt)
            for atom in cond:
                assert atom in names, (lottery, atom, opt)
                if atom not in ("总", "前区", "后区"):
                    assert atom in schema_names, (lottery, atom)
            validate_condition(cond, lottery)      # 不抛异常
            # 每档都要有「含义」说明，否则「更详细」就只是选项变多
            assert opt["note"] and len(opt["note"]) >= 6, (lottery, opt)


def test_all_lottery_has_no_specific_group(client):
    """「全部彩种」不给专属组：同一分区名在不同彩种含义不同，不能跨彩种套用。"""
    d = client.get("/api/hits/filters?lottery=全部").get_json()
    assert d["专属"] is None
    assert d["分区"] == []


def test_digit_lottery_shows_position_name_mapping(client):
    """数字型要给出「第N位 = 百位/十位/个位」对照，否则看「第3位」得自己数。"""
    d = client.get("/api/hits/filters?lottery=福彩3D").get_json()
    assert "第1位=百位" in d["位置对照"] and "第3位=个位" in d["位置对照"]
    d5 = client.get("/api/hits/filters?lottery=排列5").get_json()
    assert "第1位=万位" in d5["位置对照"] and "第5位=个位" in d5["位置对照"]
    # 红蓝型没有这套对照
    assert client.get("/api/hits/filters?lottery=双色球").get_json()["位置对照"] == ""


def test_unknown_lottery_rejected(client):
    assert client.get("/api/hits/filters?lottery=不存在的彩种").status_code == 400


# ------------------------------------------------------------
# 2. zones 筛选
# ------------------------------------------------------------

def test_zones_equals_legacy_params(client):
    """新口径 `zones=红球:1,蓝球:1` 与老参数 `red_hits=1&blue_hits=1` 必须同结果。"""
    a = client.get("/api/hits?lottery=双色球&zones=红球:1,蓝球:1&limit=0").get_json()
    b = client.get("/api/hits?lottery=双色球&red_hits=1&blue_hits=1&limit=0").get_json()
    assert a["total"] == b["total"] == 2       # 期号 1（4+1）与期号 2（1+1）都满足
    assert [r["期号"] for r in a["records"]] == [r["期号"] for r in b["records"]]


def test_zones_specific_combination(client):
    """红球≥4 且蓝球命中 ⇒ 只剩 4+1 那一条。"""
    d = client.get("/api/hits?lottery=双色球&zones=红球:4,蓝球:1&limit=0").get_json()
    assert d["total"] == 1
    assert d["records"][0]["期号"] == 1
    # 分母 universe 不随筛选变
    assert d["universe"] == 6


def test_zones_total_on_all_lotteries(client):
    """「全部彩种」允许 `总:N`，跨彩种同一口径。"""
    d = client.get("/api/hits?lottery=全部&zones=总:3&limit=0").get_json()
    assert d["total"] == 3          # 双色球 5、3D 3、七星彩 6
    assert d["universe"] == 6


def test_zones_group_atoms_for_qxc(client):
    """七星彩用官方 X/Y 记法：`后区:1` = 第 7 位命中；`前区:6` = 前六位全中。"""
    back = client.get("/api/hits?lottery=七星彩&zones=后区:1&limit=0").get_json()
    assert back["total"] == 2
    front = client.get("/api/hits?lottery=七星彩&zones=前区:6&limit=0").get_json()
    assert front["total"] == 0
    both = client.get("/api/hits?lottery=七星彩&zones=前区:5,后区:1&limit=0").get_json()
    assert both["total"] == 1 and both["records"][0]["期号"] == 7


def test_legacy_digit_zone_names_are_matched(client):
    """★ 老记录 `分区命中={'百位':0,'十位':0,'个位':1}` 必须能被 `第3位:1` 筛到。"""
    d = client.get("/api/hits?lottery=福彩3D&zones=第3位:1&limit=0").get_json()
    assert d["total"] == 2, "位名未归一化会把老记录静默筛漏（这里应为 2 条）"
    assert sorted(r["期号"] for r in d["records"]) == [4, 5]


# ------------------------------------------------------------
# 3. 拒绝非法条件（不许静默忽略）
# ------------------------------------------------------------

@pytest.mark.parametrize("query,needle", [
    ("lottery=双色球&zones=瞎写:1", "没有分区"),
    ("lottery=全部&zones=红球:4", "只能用「总命中」"),
    ("lottery=双色球&zones=红球", "格式应为"),
    ("lottery=双色球&zones=红球:x", "不是整数"),
    ("lottery=双色球&zones=红球:-1", "不能为负"),
    ("lottery=双色球&zones=:1", "缺少分区名"),
])
def test_bad_zone_condition_returns_400(client, query, needle):
    r = client.get("/api/hits?" + query)
    assert r.status_code == 400, query
    d = r.get_json()
    assert "error" in d and needle in d["error"], (query, d)
    # 400 时不能返回一份「看起来正常」的记录列表
    assert d["records"] == [] and d["total"] == 0


def test_zone_condition_is_case_and_space_tolerant():
    """空格与重复项要能容忍（用户手改 URL 时常见），但语义取更严格的那个。"""
    assert parse_condition(" 红球:2 , 蓝球:1 ") == {"红球": 2, "蓝球": 1}
    assert parse_condition("红球:2,红球:4") == {"红球": 4}
    assert parse_condition("") == {}
    assert parse_condition(None) == {}


# ------------------------------------------------------------
# 4. 老位名归一化（不止筛选，命中详情也要）
# ------------------------------------------------------------

def test_group_hits_normalizes_legacy_names():
    rec = {"分区命中": {"百位": 0, "十位": 0, "个位": 1}, "总命中": 1}
    g = group_hits(rec, "福彩3D")
    assert g["第3位"] == 1 and g["第1位"] == 0
    assert g["总"] == 1
    assert "百位" not in g


def test_record_view_keeps_legacy_numbers_and_hits():
    """★ 老记录在命中详情里不能丢号码：号码与命中都必须归一化后取到。"""
    from web.utils import feedback_record_view

    rec = {
        "预测号码": {"百位": [1], "十位": [2], "个位": [3]},
        "实际号码": {"百位": [1], "十位": [9], "个位": [3]},
        "分区命中": {"百位": 1, "十位": 0, "个位": 1},
        "总命中": 2,
        "总选择": 3,
    }
    v = feedback_record_view("福彩3D", rec)
    assert v["total_hit"] == 2
    by = {z["name"]: z for z in v["zones"]}
    assert by["第1位"]["pred"] == [1] and by["第1位"]["actual"] == [1]
    assert by["第3位"]["hit"] == 1
    # 逐位命中之和必须等于记录里的总命中（老代码这里会是 0，与总命中自相矛盾）
    assert sum(z["hit"] for z in v["zones"]) == rec["总命中"]


# ------------------------------------------------------------
# 5. 前端接线（不许手抄规则 / 不许静默丢弃）
# ------------------------------------------------------------

def test_page_has_no_handwritten_hit_options(client):
    """页面不许写死命中选项：规则改一处就得改两处 ⇒ 必然过期。"""
    html = client.get("/hits").get_data(as_text=True)
    assert 'id="hits-cond"' in html
    assert 'id="hits-cond-note"' in html
    # 老的三个写死下拉必须已经退场（它们的内容是手抄的规则）
    for gone in ('id="hits-red"', 'id="hits-blue"', 'id="hits-total"'):
        assert gone not in html, gone
    # 不许残留写死的档位文案
    assert "总命中 ≥ 7" not in html, "档位必须由 /api/hits/filters 生成"
    assert "红球命中 ≥ 4" not in html


def test_url_params_only_applied_when_the_option_exists(client):
    """URL 回填不能裸赋值：`el.value = 不存在的值` 会变成空串 ⇒ 后端收到空参数 ⇒ 静默 0 条。"""
    html = client.get("/hits").get_data(as_text=True)
    assert "setSel" in html
    # 老写法（裸赋值）必须已经退场
    for bad in ("$('hits-lottery').value = lottery;", "$('hits-prize').value = prize;",
                "$('hits-type').value = type;"):
        assert bad not in html, bad
    # 回填必须真的比对过选项
    assert "some(o => o.value === val)" in html


def test_frontend_builds_options_from_api(client):
    js = client.get("/static/js/main.js").get_data(as_text=True)
    assert "/api/hits/filters" in js
    assert "optgroup" in js
    # 选项元素必须来自后端字段，而不是在前端拼规则表
    assert "data.通用" in js and "data.专属" in js
    # 命中条件必须真的被发出去
    assert "&zones=" in js
    # ★ 条件被丢弃时必须告诉用户（否则页面显示「全部」而用户以为筛选生效）
    assert "不适用，已清除" in js
    assert "选定单个彩种" in js
