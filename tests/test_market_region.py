"""中彩网详情接口补头奖分省：解析与回填的边界。

★ 为什么值得单独测：
  1. 大乐透的原文是「辽宁(基本1注)、湖北(基本3注 追加1注)」这种括号格式，
     现有的「省名N注」正则整段解析不出来 —— 不加括号分支，大乐透分省永远 0 行。
  2. 「追加」是同一注的加价投注，不是额外的注。若把 基本+追加 相加，
     分省合计会比奖级表的一等奖注数多出来（26124：7 vs 6），凭空造假。
  3. prize_region 的汇总视图（v_region_prize / region_prize_board）是**跨源求和**，
     同一期若同时写 cwl 与 zhcw 两套行，注数会被算两遍 —— 必须只在缺口期写。

全部用临时库 + 打桩，绝不碰真实的 market/market.db，也绝不发真网络请求。
"""

import sqlite3

import pytest

import market.build as B
import market.sources as S


# ---------------------------------------------------------------- 解析

@pytest.mark.parametrize("text,expect", [
    # 大乐透括号格式：取「基本」注数，追加不并入
    ("辽宁(基本1注)、湖北(基本3注 追加1注)、湖南(基本1注)、重庆(基本1注)。",
     [("辽宁", 1), ("湖北", 3), ("湖南", 1), ("重庆", 1)]),
    ("内蒙古(基本5注 追加2注)、北京(基本1注)。",
     [("内蒙古", 5), ("北京", 1)]),
    # 括号里只有「追加」也别丢掉这一省（回退到首个 N 注）
    ("湖北(追加1注)、湖南(基本2注)。",
     [("湖北", 1), ("湖南", 2)]),
    # 福彩 cwl / 中彩网双色球格式：保持原样
    ("内蒙古7注，上海1注，安徽1注，共9注。",
     [("内蒙古", 7), ("上海", 1), ("安徽", 1)]),
    ("江苏1注，山东2注，广东1注，广西2注，四川1注，西藏1注，共8注。",
     [("江苏", 1), ("山东", 2), ("广东", 1), ("广西", 2), ("四川", 1), ("西藏", 1)]),
    ("广东1注。", [("广东", 1)]),
    # 同省重复出现要合并
    ("广东3注，广东1注，共4注。", [("广东", 4)]),
    ("", []),
    (None, []),
])
def test_parse_region_text_handles_both_formats(text, expect):
    assert S.parse_region_text(text) == expect


def test_paren_format_does_not_inflate_with_append():
    """★回归锁：26124 官方一等奖基本=6 注。分省合计必须也是 6，不能把追加算成额外的注。"""
    raw = "辽宁(基本1注)、湖北(基本3注 追加1注)、湖南(基本1注)、重庆(基本1注)。"
    rows = S.parse_region_text(raw)
    assert sum(n for _, n in rows) == 6, "追加被当成额外注 → 分省合计与奖级表对不上"


def test_parse_total_only_from_plain_format():
    # 括号格式没有「共N注」结尾
    assert S.parse_total_from_text("辽宁(基本1注)、湖北(基本3注 追加1注)。") is None
    assert S.parse_total_from_text("河南1注，陕西1注，共2注。") == 2


@pytest.mark.parametrize("text,total", [
    # ★老期原文有排版空格：「甘 肃」「浙 江」「追加 1注」—— 不压掉空白就整省漏计
    ("内蒙(基本1注)、辽宁(基本1注)、山东(基本2注)、河南(基本2注)、甘 肃(基本1注)", 7),
    ("山西(基本1注)、内蒙(基本1注)、辽宁(基本2注)、江苏(基本2注)、浙 江(基本1注)、"
     "山东(基本2注 追加1注)、广东(基本1注)、云南(基本1注 追加1注)", 11),
    ("浙江(基本1注 追加1注)、山东(基本2注 追加1注)、广西(基本1注 追加 1注)、"
     "四川(基本1注 追加1注)、贵州(基本1注)、陕西(基本1注)。", 7),
])
def test_parse_region_text_tolerates_internal_spaces_and_alias(text, total):
    rows = S.parse_region_text(text)
    assert sum(n for _, n in rows) == total, rows
    # 「内蒙」要归一成「内蒙古」，不能留成「内蒙」
    assert not any(r == "内蒙" for r, _ in rows)


@pytest.mark.parametrize("text,total", [
    # 「其中复式投注为：…」是对总数的子集细分，不能并入合计
    ("天津1注,河北1注,黑龙江6注,共37注。其中复式投注为：天津1注,黑龙江1注。", 8),
    # 「其中复式或胆拖投注为：…」同义（旧代码只认「复式投注」，漏了这一种）
    ("河南1注,湖北1注,广东1注,贵州1注,共6注。其中复式或胆拖投注为：河南1注,贵州1注。", 4),
    # 「其中一等奖特别奖为：…」是派奖期的子集
    ("上海3注,浙江1注,共4注。其中一等奖特别奖为：上海2注。", 4),
    # 深圳与广东并排：深圳必须单独计进合计，不能丢
    ("广东2注,深圳1注,共3注。", 3),
    ("山西1注,辽宁1注,河南1注,深圳1注,共8注。", 4),
])
def test_parse_region_text_ignores_subset_section(text, total):
    """★★「其中…」之后全是前面总数的**子集细分**（复式/胆拖/特别奖），累加就重复计数。

    实测 2026-11-01：双色球 176 期、七乐彩 21 期分省合计因此虚高
    （2016132 分省 51 vs 官方一等奖 37）。
    """
    rows = S.parse_region_text(text)
    assert sum(n for _, n in rows) == total, rows
    assert "深圳" in S.REGIONS, "深圳缺失会让这部分注数整条丢失"


def test_parse_counts_shenzhen_separately_not_into_guangdong():
    """深圳是计划单列市，福彩原文与广东并排列出 ⇒ 不能并进广东，也不能丢。"""
    rows = dict(S.parse_region_text("广东2注,深圳1注,共3注。"))
    assert rows == {"广东": 2, "深圳": 1}, rows


def test_reparse_fixes_undercount_from_stored_raw(tmp_path):
    """解析器改进后，用已存原文离线修正 —— 不必再抓网络。"""
    con = _db(tmp_path / "m.db")
    # 该期之前被旧解析器漏掉「甘肃」（带空格），只剩 5 注
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('dlt','18141','sporttery',2018,141,"
                "'内蒙(基本1注)、辽宁(基本1注)、山东(基本2注)、河南(基本2注)、甘 肃(基本1注)')")
    for r, n in [("山东", 2), ("河南", 2), ("辽宁", 1)]:
        con.execute("INSERT INTO prize_region VALUES('dlt','18141','zhcw',?,?)", (r, n))
    con.commit()
    assert con.execute("SELECT SUM(winners) FROM prize_region").fetchone()[0] == 5

    st = B.reparse_zhcw_region(con, codes=["dlt"], verbose=False)

    assert st["dlt"]["changed"] == 1
    assert con.execute("SELECT SUM(winners) FROM prize_region").fetchone()[0] == 7
    assert con.execute("SELECT COUNT(*) FROM prize_region WHERE region='甘肃'").fetchone()[0] == 1


def test_reparse_ignores_issues_without_zhcw_rows(tmp_path):
    """只有 cwl 分省行的期不能被重解析成 zhcw 行（否则双计）。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('ssq','2026124','cwl',2026,124,'江苏1注，共1注。')")
    con.execute("INSERT INTO prize_region VALUES('ssq','2026124','cwl','江苏',1)")
    con.commit()
    st = B.reparse_zhcw_region(con, codes=["ssq"], verbose=False)
    assert st["ssq"]["issues"] == 0
    assert con.execute("SELECT COUNT(*) FROM prize_region WHERE source='zhcw'").fetchone()[0] == 0


# ---------------------------------------------------------------- 详情接口解析

class _FakeResp:
    def __init__(self, payload: str):
        self._b = payload.encode("utf-8")

    def read(self):
        return self._b


# _get(..., as_json=False) 在真实实现里直接返回 bytes（内部已 r.read()），
# 所以打桩要返回 bytes，而不是 response 对象。
def _fake_get_bytes(payload: str):
    return lambda *a, **k: payload.encode("utf-8")


def test_fetch_zhcw_region_parses_jsonp(monkeypatch):
    payload = ('callback({"resCode":"000000","firstPrizeAddress":'
               '"辽宁(基本1注)、湖北(基本3注 追加1注)。"})')
    monkeypatch.setattr(S, "_get", _fake_get_bytes(payload))
    raw, rows = S.fetch_zhcw_region("dlt", "26124")
    assert raw and "辽宁" in raw
    assert rows == [("辽宁", 1), ("湖北", 3)]


def test_fetch_zhcw_region_empty_address_returns_none(monkeypatch):
    # ★空串/None = 该期无人中头奖或数字型无此概念 → 不伪造行
    for addr in ('""', "null"):
        payload = 'callback({"resCode":"000000","firstPrizeAddress":%s})' % addr
        monkeypatch.setattr(S, "_get", _fake_get_bytes(payload))
        assert S.fetch_zhcw_region("pl3", "26290") == (None, [])


def test_fetch_zhcw_region_bad_rescode_returns_none(monkeypatch):
    payload = 'callback({"resCode":"999999","firstPrizeAddress":"广东1注。"})'
    monkeypatch.setattr(S, "_get", _fake_get_bytes(payload))
    assert S.fetch_zhcw_region("dlt", "1") == (None, [])


def test_fetch_zhcw_region_unknown_code_returns_none():
    # 未登记 lotteryId 的彩种直接返回空，不猜
    assert S.fetch_zhcw_region("nope", "1") == (None, [])


# ---------------------------------------------------------------- 回填

def _db(path):
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE draw(
            lottery TEXT, issue TEXT, source TEXT, issue_year INT, issue_seq INT,
            draw_date TEXT, front TEXT, back TEXT, seq_front TEXT, seq_back TEXT,
            sales REAL, pool REAL, first_winners INT, first_region_raw TEXT,
            fetched_at TEXT, PRIMARY KEY(lottery, issue, source));
        CREATE TABLE prize_region(
            lottery TEXT, issue TEXT, source TEXT, region TEXT, winners INT NOT NULL,
            PRIMARY KEY(lottery, issue, source, region));
        CREATE TABLE sync_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, started_at TEXT,
            finished_at TEXT, rows INT, status TEXT, note TEXT);
        CREATE TABLE prize(
            lottery TEXT, issue TEXT, source TEXT, level INT, level_name TEXT,
            winners INT, amount INT, amount_kind TEXT);
        CREATE TABLE region_probe(
            lottery TEXT, issue TEXT, outcome TEXT, checked_at TEXT,
            PRIMARY KEY(lottery, issue));
    """)
    con.commit()
    return con


def test_build_zhcw_region_skips_issues_that_already_have_rows(tmp_path, monkeypatch):
    """★防双计：该期已有 cwl 分省行 → 不再写 zhcw 行（否则汇总视图会把注数算两遍）。"""
    con = _db(tmp_path / "m.db")
    # 26123 同时有 sporttery 行（无分省）与一条 cwl 分省行
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('dlt','26123','sporttery',2026,123,NULL)")
    con.execute("INSERT INTO prize_region VALUES('dlt','26123','cwl','湖北',3)")
    con.commit()

    calls = []

    def fake(code, issue):
        calls.append((code, issue))
        return "湖北(基本3注)。", [("湖北", 3)]

    monkeypatch.setattr(S, "fetch_zhcw_region", fake)
    st = B.build_zhcw_region(con, codes=["dlt"], only_missing=True, verbose=False, sleep=0)

    assert calls == [], "已有分省行的期不该再抓：%s" % calls
    assert st["dlt"]["checked"] == 0
    n = con.execute("SELECT COUNT(*) FROM prize_region WHERE source='zhcw'").fetchone()[0]
    assert n == 0


def test_build_zhcw_region_updates_all_source_rows_by_year_seq(tmp_path, monkeypatch):
    """同一期有 csv500 旧写法与 sporttery 两种期号 → 两行的 first_region_raw 都要补上。"""
    con = _db(tmp_path / "m.db")
    # 同一期（2026 年第 124 期）在 csv500 里写成 '26124'，在 sporttery 里也是 '26124'；
    # 再造一个旧写法 '024124' 模拟位数差异，验证按 (year,seq) 命中。
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('dlt','26124','sporttery',2026,124,NULL)")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('dlt','26124','csv500',2026,124,NULL)")
    con.commit()

    monkeypatch.setattr(S, "fetch_zhcw_region",
                        lambda code, issue: ("辽宁(基本1注)、湖北(基本3注)。",
                                             [("辽宁", 1), ("湖北", 3)]))
    st = B.build_zhcw_region(con, codes=["dlt"], only_missing=True, verbose=False, sleep=0)

    assert st["dlt"]["filled_issues"] == 1
    assert st["dlt"]["region_rows"] == 2
    assert st["dlt"]["draw_updated"] == 2, "两行同源期号都要补 first_region_raw"
    raws = [r[0] for r in con.execute(
        "SELECT first_region_raw FROM draw WHERE lottery='dlt' AND issue='26124'")]
    assert all(r and "辽宁" in r for r in raws), raws
    assert con.execute("SELECT COUNT(*) FROM prize_region WHERE source='zhcw'").fetchone()[0] == 2


def test_build_zhcw_region_records_zero_winner_as_raw_not_region(tmp_path, monkeypatch):
    """「共0注。」= 查了但无人中：写 first_region_raw 留痕，但不写 prize_region 行。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('qlc','2026121','zhcw',2026,121,NULL)")
    con.commit()

    monkeypatch.setattr(S, "fetch_zhcw_region", lambda code, issue: ("共0注。", []))
    st = B.build_zhcw_region(con, codes=["qlc"], only_missing=True, verbose=False, sleep=0)

    assert st["qlc"]["empty"] == 1
    assert st["qlc"]["region_rows"] == 0
    raw = con.execute("SELECT first_region_raw FROM draw WHERE lottery='qlc'").fetchone()[0]
    assert raw == "共0注。"
    assert con.execute("SELECT COUNT(*) FROM prize_region").fetchone()[0] == 0


def test_build_zhcw_region_does_not_overwrite_existing_raw(tmp_path, monkeypatch):
    """cwl 已有原文时，即便 only_missing=False 也不许覆盖。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('dlt','26124','sporttery',2026,124,'原有原文')")
    con.commit()

    monkeypatch.setattr(S, "fetch_zhcw_region", lambda code, issue: ("新原文", [("广东", 1)]))
    B.build_zhcw_region(con, codes=["dlt"], only_missing=False, verbose=False, sleep=0)

    raw = con.execute("SELECT first_region_raw FROM draw WHERE lottery='dlt'").fetchone()[0]
    assert raw == "原有原文", "非空原文被覆盖了"


def test_build_zhcw_region_tolerates_missing_tables(tmp_path):
    """空库（连 prize_region 都没有）不能抛，返回零统计即可 —— 供 run_update 里兜底调用。"""
    con = sqlite3.connect(str(tmp_path / "empty.db"))
    con.execute("CREATE TABLE draw(lottery TEXT, issue TEXT, source TEXT)")
    con.commit()
    st = B.build_zhcw_region(con, codes=["dlt"], verbose=False, sleep=0)
    assert st["dlt"]["checked"] == 0


def test_reference_first_winners_takes_max_across_sources(tmp_path):
    """★派奖行是**子集**：同一致期有「一等奖基本派奖=4」与「一等奖=7」时必须取 7。

    取 max 而非「优先某源」，是为了让判据只会**多抓**、不会误判成无人中。
    """
    con = _db(tmp_path / "m.db")
    con.executemany(
        "INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
        " VALUES('dlt',?, 'sporttery',1,?,?)",
        [("21039", "一等奖基本派奖", 4), ("21039", "一等奖", 7),
         ("21040", "一等奖", 0), ("21041", "一等奖", None)])
    con.commit()

    ref = B._reference_first_winners(con, "dlt")
    assert ref["21039"] == 7, "派奖子集把总数压低了：%s" % ref
    assert ref["21040"] == 0
    assert "21041" not in ref, "None 不该当成 0 记进来"


def test_reference_first_winners_tolerates_missing_table(tmp_path):
    """没有任何 prize 表时返回空 dict（不抛），调用方据此「全部照抓」。"""
    con = sqlite3.connect(str(tmp_path / "e.db"))
    con.execute("CREATE TABLE draw(lottery TEXT, issue TEXT, source TEXT)")
    con.commit()
    assert B._reference_first_winners(con, "dlt") == {}


def test_build_zhcw_region_skips_issue_without_any_first_winner(tmp_path, monkeypatch):
    """★参考一等奖注数=0 的期不发请求：本就没有分省数据，抓了也是空。

    这类期永远填不进 prize_region，`only_missing` 每轮都会再抓一遍 ⇒ 永久重抓。
    """
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                " VALUES('kl8','2026290','cwl',2026,290)")
    con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                " VALUES('kl8','2026290','cwl',1,'选十中十',0)")
    con.commit()

    calls = []
    monkeypatch.setattr(S, "fetch_zhcw_region",
                        lambda code, issue: calls.append(issue) or (None, []))
    st = B.build_zhcw_region(con, codes=["kl8"], only_missing=True,
                             verbose=False, sleep=0)

    assert calls == [], "无人中的期不该发请求：%s" % calls
    assert st["kl8"]["checked"] == 0
    assert st["kl8"]["skipped_no_winner"] == 1


def test_build_zhcw_region_keeps_issue_with_unknown_reference(tmp_path, monkeypatch):
    """★有奖项行缺失（参考值未知）时必须照抓 —— 「不知道」不等于「无人中」。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                " VALUES('dlt','26124','sporttery',2026,124)")
    con.commit()   # prize 表为空

    calls = []
    monkeypatch.setattr(S, "fetch_zhcw_region",
                        lambda code, issue: calls.append(issue) or ("广东3注", [("广东", 3)]))
    st = B.build_zhcw_region(con, codes=["dlt"], only_missing=True,
                             verbose=False, sleep=0)

    assert calls == ["26124"]
    assert st["dlt"]["skipped_no_winner"] == 0
    assert st["dlt"]["filled_issues"] == 1


def test_build_zhcw_region_force_ignores_no_winner_filter(tmp_path, monkeypatch):
    """force（skip_no_winner=False）用于复核参考值失真：即使参考=0 也要抓。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                " VALUES('dlt','26124','sporttery',2026,124)")
    con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                " VALUES('dlt','26124','sporttery',1,'一等奖',0)")
    con.commit()

    calls = []
    monkeypatch.setattr(S, "fetch_zhcw_region",
                        lambda code, issue: calls.append(issue) or ("广东1注", [("广东", 1)]))
    st = B.build_zhcw_region(con, codes=["dlt"], only_missing=False, skip_no_winner=False,
                             verbose=False, sleep=0)

    assert calls == ["26124"]
    assert st["dlt"]["filled_issues"] == 1


# ---------------------------------------------------------------- 离线重建（全部源）

def test_reparse_region_all_fixes_subset_double_count(tmp_path):
    """★福彩老数据的两个坑：复式段被重复累加、深圳整条丢失。离线重建要一次修好。

    重建必须**保留原来的 source 标签**（cwl 的仍记 cwl），否则会与 zhcw 行混源。
    """
    con = _db(tmp_path / "m.db")
    raw = "广东2注,深圳1注,共3注。其中复式投注为：广东1注。"
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('ssq','2016132','cwl',2016,132,?)", (raw,))
    # 旧解析的错误结果：广东 3（把复式段加进来）+ 深圳 1 丢了
    con.executemany("INSERT INTO prize_region VALUES('ssq','2016132','cwl',?,?)",
                    [("广东", 3)])
    con.commit()

    st = B.reparse_region_all(con, codes=["ssq"], verbose=False)

    assert st["ssq"]["changed"] == 1
    rows = dict(con.execute(
        "SELECT region, winners FROM prize_region WHERE lottery='ssq' AND issue='2016132'"))
    assert rows == {"广东": 2, "深圳": 1}, rows
    assert sum(rows.values()) == 3
    srcs = {r[0] for r in con.execute(
        "SELECT DISTINCT source FROM prize_region WHERE lottery='ssq'")}
    assert srcs == {"cwl"}, "重建把源标签改了：%s" % srcs


def test_reparse_region_all_skips_ambiguous_multi_source(tmp_path):
    """同一期有两个源的分省行 ⇒ 口径不明，跳过并点名，不猜。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('ssq','2016132','cwl',2016,132,'广东2注,共2注。')")
    con.execute("INSERT INTO prize_region VALUES('ssq','2016132','cwl','广东',2)")
    con.execute("INSERT INTO prize_region VALUES('ssq','2016132','zhcw','广东',2)")
    con.commit()

    st = B.reparse_region_all(con, codes=["ssq"], verbose=False)
    assert st["ssq"]["ambiguous"] == 1
    assert st["ssq"]["changed"] == 0
    n = con.execute("SELECT COUNT(*) FROM prize_region WHERE lottery='ssq'").fetchone()[0]
    assert n == 2, "跳过时不该动任何行"


def test_reparse_region_all_dry_run_does_not_write(tmp_path):
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('ssq','2016132','cwl',2016,132,'广东2注,深圳1注,共3注。')")
    con.execute("INSERT INTO prize_region VALUES('ssq','2016132','cwl','广东',3)")
    con.commit()

    st = B.reparse_region_all(con, codes=["ssq"], verbose=False, dry_run=True)
    assert st["ssq"]["changed"] == 1, "dry-run 也要报出会改多少期"
    rows = con.execute("SELECT region, winners FROM prize_region WHERE lottery='ssq'").fetchall()
    assert rows == [("广东", 3)], "dry-run 写库了：%s" % rows


def test_reparse_region_all_tolerates_missing_tables(tmp_path):
    con = sqlite3.connect(str(tmp_path / "e.db"))
    con.execute("CREATE TABLE draw(lottery TEXT, issue TEXT, source TEXT)")
    con.commit()
    st = B.reparse_region_all(con, codes=["ssq"], verbose=False)
    assert st["ssq"]["issues"] == 0


# ---------------------------------------------------------------- 自检

def test_check_zhcw_region_classifies_ok_low_high_noref(tmp_path):
    """四类判决：相符 / 偏少 / 偏多 / 无参考，并且分省合计与参考合计分别累计。"""
    con = _db(tmp_path / "m.db")
    cases = [
        ("dlt", "26101", 3, 3),    # ok
        ("dlt", "26102", 2, 5),    # low
        ("dlt", "26103", 6, 2),    # high（重复计数/追加当额外注）
    ]
    for code, iss, region_sum, first in cases:
        con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                    " VALUES(?,?,'sporttery',1,'一等奖',?)", (code, iss, first))
        con.execute("INSERT INTO prize_region VALUES(?,?,'zhcw','广东',?)",
                    (code, iss, region_sum))
    # 无参考：有分省行但没有奖项行
    con.execute("INSERT INTO prize_region VALUES('dlt','26104','zhcw','江苏',1)")
    con.commit()

    st = B.check_zhcw_region(con, codes=["dlt"], verbose=False)["dlt"]
    assert (st["ok"], st["low"], st["high"], st["noref"]) == (1, 1, 1, 1), st
    assert st["sum_region"] == 3 + 2 + 6
    assert st["sum_ref"] == 3 + 5 + 2

    # ★参考值取各源 max：派奖子集不能把「相符」误报成「偏多」
    con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                " VALUES('dlt','26101','sporttery',1,'一等奖基本派奖',1)")
    con.commit()
    st = B.check_zhcw_region(con, codes=["dlt"], verbose=False)["dlt"]
    assert st["ok"] == 1, "派奖子集把「相符」压成了「偏多」：%s" % st


def test_check_zhcw_region_flags_multi_source_issue(tmp_path):
    """同一期两套地区行 ⇒ 汇总会双计，自检必须点名。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                " VALUES('ssq','2026124','cwl',1,'一等奖',2)")
    con.execute("INSERT INTO prize_region VALUES('ssq','2026124','cwl','广东',1)")
    con.execute("INSERT INTO prize_region VALUES('ssq','2026124','zhcw','广东',1)")
    con.commit()

    st = B.check_zhcw_region(con, codes=["ssq"], verbose=False)["ssq"]
    assert st["multi_source"] == 1
    # ★两源各 1 注相加恰好等于参考 2 ⇒ 判成「相符」，**这是假相符**
    #   ——「相符」不能只看数字，必须同时要求 multi_source==0。
    assert st["ok"] == 1 and st["high"] == 0



# ---------------------------------------------------------------- 限流/失败不得伪装成「无数据」

def test_fetch_zhcw_region_raises_on_network_error(monkeypatch):
    """★★请求失败必须抛异常，不能返回 (None, [])。

    否则调用方无法区分「抓失败」和「源头没有」—— 2026-11-01 实踩：中彩网在约 1600 次
    请求后开始 RemoteDisconnected，旧代码静默返回空，统计里全算成「无人中」。
    """
    def boom(*a, **kw):
        raise RuntimeError("请求失败：RemoteDisconnected")

    monkeypatch.setattr(S, "_get", boom)
    with pytest.raises(RuntimeError):
        S.fetch_zhcw_region("dlt", "26124")


def test_build_zhcw_region_aborts_on_consecutive_failures(tmp_path, monkeypatch):
    """连续失败达阈值就中止，且不写任何假数据（已写入的都是真数据，可续跑）。"""
    con = _db(tmp_path / "m.db")
    for seq in range(1, 6):
        con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                    " VALUES('dlt','261%02d','sporttery',2026,%d)" % (seq, seq))
    con.commit()

    def boom(code, issue):
        raise RuntimeError("RemoteDisconnected")

    monkeypatch.setattr(S, "fetch_zhcw_region", boom)
    st = B.build_zhcw_region(con, codes=["dlt"], only_missing=True, verbose=False,
                             sleep=0, max_consecutive_failures=3)

    s = st["dlt"]
    assert s["aborted"] is True
    assert s["checked"] == 3, "到阈值就该停，不该继续往下试：%s" % s
    assert s["failed"] == 3
    assert s["empty"] == 0, "失败被记成「无人中」了"
    assert s["filled_issues"] == 0
    assert con.execute("SELECT COUNT(*) FROM prize_region").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM draw WHERE first_region_raw IS NOT NULL"
        " AND first_region_raw<>''").fetchone()[0] == 0
    assert "限流" in s["abort_reason"]


def test_build_zhcw_region_abort_stops_remaining_codes(tmp_path, monkeypatch):
    """已被限流时不再去试后面的彩种（避免把封禁拖长）。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                " VALUES('dlt','26124','sporttery',2026,124)")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                " VALUES('ssq','2026124','zhcw',2026,124)")
    con.commit()

    monkeypatch.setattr(S, "fetch_zhcw_region",
                        lambda code, issue: (_ for _ in ()).throw(RuntimeError("blocked")))
    st = B.build_zhcw_region(con, codes=["dlt", "ssq"], only_missing=True, verbose=False,
                             sleep=0, max_consecutive_failures=1)

    assert "dlt" in st and "ssq" not in st, st


def test_parse_handles_truncated_tail_paren():
    """★原文被截断时最后一个省缺右括号：「浙江(基本2注 追加2注」。

    严格正则匹配不到该省 ⇒ 整条丢失（实测 dlt 16098 少 2 注）。
    只在**串尾**兜底，不做「右括号可选」，否则会吞掉下一省的文本。
    """
    rows = S.parse_region_text("内蒙(基本1注)、吉林(基本1注)、浙江(基本2注 追加2注")
    assert rows == [("内蒙古", 1), ("吉林", 1), ("浙江", 2)], rows
    assert sum(n for _, n in rows) == 4
    # 正常文本不能被兜底逻辑重复计入
    ok = S.parse_region_text("辽宁(基本1注)、湖北(基本3注 追加1注)。")
    assert ok == [("辽宁", 1), ("湖北", 3)], ok


def test_parse_non_region_buckets_identifies_jifen():
    """「积分换彩」= 积分兑换票中奖，没有省份：不进 prize_region，但校验要认。"""
    assert S.parse_non_region_buckets("上海1注,积分换彩1注,共2注。") == [("积分换彩", 1)]
    assert S.parse_non_region_buckets("广东1注,共1注。") == []


def test_check_region_explains_non_region_bucket_as_shortfall(tmp_path):
    """分省 1 + 积分换彩 1 == 参考 2 ⇒ 记「非地域已解释」，不报偏少。

    否则这条永久噪声会让自检天天报警（和 health_check 的纪律一致）。
    """
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('qlc','2020033','cwl',2020,33,'上海1注,积分换彩1注,共2注。')")
    con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                " VALUES('qlc','2020033','cwl',1,'一等奖',2)")
    con.execute("INSERT INTO prize_region VALUES('qlc','2020033','cwl','上海',1)")
    con.commit()

    st = B.check_zhcw_region(con, codes=["qlc"], verbose=False)["qlc"]
    assert st["ok_explained"] == 1
    assert st["low"] == 0 and st["high"] == 0


@pytest.mark.parametrize("text,total", [
    # ★2014 年前后的老期用**全角括号**，且「基本1 注」中间还有空格。
    #   只认半角 ⇒ 整期解析成空 ⇒ 写不进 prize_region ⇒ 每轮都被当缺口重抓（实测 38 期）。
    ("上海（基本1 注）", 1),
    ("辽宁（基本2 注）、安徽（基本1 注 追加1 注）、福建（基本1 注）", 4),
    ("浙江（基本1 注）、山东（基本1 注）、河南（基本2 注 追加1 注）、"
     "湖南（基本1 注）、云南（基本1 注）", 6),
    # 混用全角半角也不能丢
    ("广东（基本1 注）、四川(基本1注 追加1注)", 2),
])
def test_parse_fullwidth_paren_format(text, total):
    rows = S.parse_region_text(text)
    assert sum(n for _, n in rows) == total, rows
    assert rows, "全角括号整期解析成空"


def test_reparse_region_all_fills_issues_without_any_rows(tmp_path):
    """★有原文但一条行都没有的期要能补建（否则永久重抓）。

    源标签按彩种定：福彩三彩种记 cwl（原文来自福彩官网），大乐透记 zhcw（中彩网接口）。
    """
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('dlt','14056','sporttery',2014,56,'上海（基本1 注）')")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('ssq','2016125','cwl',2016,125,'辽宁1注,浙江2注,共3注。')")
    con.commit()

    st = B.reparse_region_all(con, codes=["dlt", "ssq"], verbose=False)

    assert st["dlt"]["filled_new"] == 1
    assert st["dlt"]["new_rows"] == 1
    assert con.execute(
        "SELECT region, winners FROM prize_region WHERE lottery='dlt'").fetchall() == [("上海", 1)]
    srcs = dict(con.execute(
        "SELECT lottery, source FROM prize_region GROUP BY lottery").fetchall())
    assert srcs == {"dlt": "zhcw", "ssq": "cwl"}, srcs
    # ssq 的 3 注要完整出来，不能只出一条
    assert con.execute(
        "SELECT COUNT(*) FROM prize_region WHERE lottery='ssq'").fetchone()[0] == 2


def test_reparse_region_all_does_not_refill_when_rows_exist(tmp_path):
    """已有行的期走「重建」，不能再被 fill_missing 重复插一遍（否则注数翻倍）。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('dlt','14056','sporttery',2014,56,'上海（基本1 注）')")
    con.execute("INSERT INTO prize_region VALUES('dlt','14056','zhcw','上海',1)")
    con.commit()

    st = B.reparse_region_all(con, codes=["dlt"], verbose=False)
    assert st["dlt"]["filled_new"] == 0
    assert st["dlt"]["unchanged"] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM prize_region WHERE lottery='dlt'").fetchone()[0] == 1


# ---------------------------------------------------------------- 「查过、源头没有」≠「缺口」

def test_build_zhcw_region_records_probe_and_skips_next_time(tmp_path, monkeypatch):
    """★★源头没有分省的期必须记成「已探过」，否则永久重抓。

    实测：大乐透 14048~14055 接口返回空，而 14056 有「上海（基本1 注）」。
    这类期写不出行，`only_missing` 每轮都会把它们当缺口再抓一遍。
    """
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                " VALUES('dlt','14055','sporttery',2014,55)")
    con.commit()

    calls = []
    monkeypatch.setattr(S, "fetch_zhcw_region",
                        lambda code, issue: calls.append(issue) or (None, []))
    st1 = B.build_zhcw_region(con, codes=["dlt"], verbose=False, sleep=0)
    assert st1["dlt"]["empty"] == 1
    assert con.execute(
        "SELECT outcome FROM region_probe WHERE lottery='dlt'").fetchone()[0] == "empty"

    # 第二次：不该再发请求
    st2 = B.build_zhcw_region(con, codes=["dlt"], verbose=False, sleep=0)
    assert calls == ["14055"], "已探过的期被重抓了：%s" % calls
    assert st2["dlt"]["checked"] == 0
    assert st2["dlt"]["skipped_checked"] == 1


def test_build_zhcw_region_force_refetches_probed_issue(tmp_path, monkeypatch):
    """源头后来补上数据时，用 skip_checked=False（force）能重新抓。"""
    con = _db(tmp_path / "m.db")
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                " VALUES('dlt','14055','sporttery',2014,55)")
    con.execute("INSERT INTO region_probe VALUES('dlt','14055','empty','2026-11-01')")
    con.commit()

    calls = []
    monkeypatch.setattr(S, "fetch_zhcw_region",
                        lambda code, issue: calls.append(issue) or ("上海（基本1 注）", [("上海", 1)]))
    st = B.build_zhcw_region(con, codes=["dlt"], verbose=False, sleep=0,
                             skip_checked=False)
    assert calls == ["14055"]
    assert st["dlt"]["filled_issues"] == 1
    assert con.execute(
        "SELECT outcome FROM region_probe WHERE lottery='dlt'").fetchone()[0] == "filled"


def test_pending_region_issues_applies_all_filters(tmp_path):
    """四道过滤集中在一个函数里：已有行 / 已探过 / 无人中 / 期号写法。"""
    con = _db(tmp_path / "m.db")
    rows = [("26101", 2026, 101), ("26102", 2026, 102), ("26103", 2026, 103),
            ("26104", 2026, 104)]
    for iss, y, s in rows:
        con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq)"
                    " VALUES('dlt',?, 'sporttery',?,?)", (iss, y, s))
    con.execute("INSERT INTO prize_region VALUES('dlt','26101','zhcw','广东',1)")   # 已有行
    con.execute("INSERT INTO region_probe VALUES('dlt','26102','empty','x')")        # 已探过
    con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                " VALUES('dlt','26103','sporttery',1,'一等奖',0)")                   # 无人中
    con.commit()

    ph = B.pending_region_issues(con, "dlt")
    assert [c[0] for c in ph[0]] == ["26104"], ph
    assert ph[1] == 1 and ph[2] == 1


def test_check_region_reports_source_conflict_not_overflow(tmp_path):
    """★分省合计 == 原文自述「共N注」但与奖级表不一致 ⇒ 记「源内矛盾」，不能算「偏多」。

    实测 ssq 2008065：原文「北京1注,…共11注」合计 11，奖级表一等奖只有 4。
    解析是忠实的，矛盾在源头自己；当成解析错去改，只会越改越假。
    """
    con = _db(tmp_path / "m.db")
    raw = "北京1注,河北2注,辽宁1注,浙江2注,湖南3注,广东1注,四川1注,共11注。"
    con.execute("INSERT INTO draw(lottery,issue,source,issue_year,issue_seq,first_region_raw)"
                " VALUES('ssq','2008065','zhcw',2008,65,?)", (raw,))
    con.execute("INSERT INTO prize(lottery,issue,source,level,level_name,winners)"
                " VALUES('ssq','2008065','zhcw',1,'一等奖',4)")
    for region, n in (("北京", 1), ("河北", 2), ("辽宁", 1), ("浙江", 2),
                      ("湖南", 3), ("广东", 1), ("四川", 1)):
        con.execute("INSERT INTO prize_region VALUES('ssq','2008065','zhcw',?,?)", (region, n))
    con.commit()

    st = B.check_zhcw_region(con, codes=["ssq"], verbose=False)["ssq"]
    assert st["src_conflict"] == 1
    assert st["high"] == 0 and st["low"] == 0, st
    assert st["conflict_samples"][0][0] == "2008065"
