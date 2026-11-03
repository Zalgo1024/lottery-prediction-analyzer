"""market 数据源抓取与归一化。

只做三件事：请求、解析、产出归一化行。不碰数据库，不碰现有 CSV。

四类源：
  cwl       福彩官方   —— 含 ★头奖分省原文（content）、奖级注数、销售额、奖池
  sporttery 体彩官方   —— 全历史、★出球顺序（近期）、奖级注数
  zhcw      中彩网     —— 双色球 2003 起全量、★出球顺序（全历史）
  mof       财政部     —— ★31 省月销量（xlsx）
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# ------------------------------------------------------------------ 彩种字典

LOTTERIES = {
    "ssq": dict(name="双色球", family="cwl", width=7, cwl="ssq", zhcw=1,
                zones=[dict(name="红球", min=1, max=33, count=6),
                       dict(name="蓝球", min=1, max=16, count=1)]),
    "sd":  dict(name="福彩3D", family="cwl", width=7, cwl="3d", zhcw=2,
                zones=[dict(name="百位", min=0, max=9, count=1),
                       dict(name="十位", min=0, max=9, count=1),
                       dict(name="个位", min=0, max=9, count=1)]),
    "qlc": dict(name="七乐彩", family="cwl", width=7, cwl="qlc", zhcw=3,
                zones=[dict(name="基本号", min=1, max=30, count=7),
                       dict(name="特别号", min=1, max=30, count=1)]),
    "kl8": dict(name="快乐8", family="cwl", width=7, cwl="kl8", zhcw=None,
                zones=[dict(name="基本号", min=1, max=80, count=20)]),
    "dlt": dict(name="大乐透", family="sporttery", width=5, cwl=None, zhcw=None, game="85",
                zones=[dict(name="前区", min=1, max=35, count=5),
                       dict(name="后区", min=1, max=12, count=2)]),
    "pl3": dict(name="排列3", family="sporttery", width=5, cwl=None, zhcw=None, game="35",
                zones=[dict(name="百位", min=0, max=9, count=1),
                       dict(name="十位", min=0, max=9, count=1),
                       dict(name="个位", min=0, max=9, count=1)]),
    "pl5": dict(name="排列5", family="sporttery", width=5, cwl=None, zhcw=None, game="350133",
                zones=[dict(name="万位", min=0, max=9, count=1),
                       dict(name="千位", min=0, max=9, count=1),
                       dict(name="百位", min=0, max=9, count=1),
                       dict(name="十位", min=0, max=9, count=1),
                       dict(name="个位", min=0, max=9, count=1)]),
    "qxc": dict(name="七星彩", family="sporttery", width=5, cwl=None, zhcw=None, game="04",
                zones=[dict(name="第1位", min=0, max=9, count=1),
                       dict(name="第2位", min=0, max=9, count=1),
                       dict(name="第3位", min=0, max=9, count=1),
                       dict(name="第4位", min=0, max=9, count=1),
                       dict(name="第5位", min=0, max=9, count=1),
                       dict(name="第6位", min=0, max=9, count=1),
                       dict(name="第7位", min=0, max=14, count=1)]),
}


# ------------------------------------------------------------------ 省级行政区

REGIONS = [
    "内蒙古", "黑龙江", "北京", "天津", "河北", "山西", "辽宁", "吉林", "上海",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
    "甘肃", "青海", "宁夏", "新疆", "台湾", "香港", "澳门",
    # ★计划单列市：福彩原文里「深圳」与「广东」**并排列出**（广东 2 注 + 深圳 1 注 = 3 注），
    #   即广东口径**不含**深圳。若不认深圳，这些注会整条丢失（实测双色球 356 期、
    #   七乐彩 15 期、快乐8 12 期原文含深圳，是「分省合计<官方一等奖」的主因）。
    #   ★注意：财政部《分省销量》的「广东」**含**深圳 ⇒ 做「头奖/销量」比值时
    #   深圳没有分母，界面必须显示「—」，不能当 0。
    "深圳",
]

# 别名归一：源里偶见「内蒙」「广西壮族」等写法（★必须在 _REGION_ALT 之前定义，
#   因为「内蒙」这类短名要能被正则直接匹配到，否则「内蒙(基本1注)」整省丢掉）
_ALIAS = {
    "内蒙": "内蒙古",
    "广西壮族": "广西", "宁夏回族": "宁夏", "新疆维吾尔": "新疆",
    "西藏自治区": "西藏",
}

# 长名优先，避免「内蒙古」被「内蒙」截断这类问题；同时把别名也纳入可匹配集合
_REGION_ALT = "|".join(sorted(set(REGIONS) | set(_ALIAS), key=len, reverse=True))

# 「内蒙古7注」「上海 1 注」「共9注」(共 不在省名表里，天然排除)
_REGION_RE = re.compile(r"(%s)\s*(\d+)\s*注" % _REGION_ALT)

# ★大乐透/中彩网详情格式：「辽宁(基本1注)、湖北(基本3注 追加1注)、湖南(基本1注)」
#   —— 省名后跟括号，括号内「基本N注」+ 可选「追加M注」。
#   ★★「追加」不是另一种票，而是对同一注加 1 元手续费后提高一/二等奖金，
#      所以 湖北(基本3注 追加1注) = 湖北中出 3 注一等奖（其中 1 注是追加票），
#      **不是 4 注**。按 4 计数会让分省合计=7 而奖级表一等奖=6，凭空多出一注。
#   ★不能复用 _REGION_RE：它只认「省名N注」，遇到括号格式整段返回空，
#   会让大乐透分省数据全丢。
#   ★★括号有**全角/半角**两种：2014 年前后是「上海（基本1 注）」（全角括号 + 「1 注」
#      中间还有空格），2018 年后是「辽宁(基本1注)」。只认半角 ⇒ 整期解析成空，
#      而且因为写不进 prize_region，`only_missing` 每轮都会把它当缺口再抓一遍
#      （实测 38 期「抓到了原文但没出行」，永久重抓）。下面三个正则都同时认全角半角。
_PAREN_REGION_RE = re.compile(r"(%s)\s*[\(（]([^)）]*)[\)）]" % _REGION_ALT)
_PAREN_BASIC_RE = re.compile(r"基本\s*(\d+)\s*注")
_PAREN_ANY_RE = re.compile(r"(\d+)\s*注")

# ★原文被**截断**时末尾那个省的右括号会缺（实测 dlt 16098：
#   「内蒙(基本1注)、吉林(基本1注)、浙江(基本2注 追加2注」——浙江少 2 注）。
#   只用「右括号可有可无」会吞掉下一省的文本，所以单独做**串尾**兜底。
_TAIL_PAREN_RE = re.compile(r"(%s)\s*[\(（]([^)）]*)$" % _REGION_ALT)

# 非地域口径的中奖（没有省份可归）：福彩「积分换彩」= 用积分兑换的票中奖。
# ★官方一等奖总数**包含**它们 ⇒ 一致性校验不能把它当「偏少」。
NON_REGION_BUCKETS = ("积分换彩",)
_BUCKET_RE = re.compile(r"(%s)\s*(\d+)\s*注" % "|".join(NON_REGION_BUCKETS))

# 「共9注」「共0注」——头奖总注数
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*注")


def parse_region_text(text: str) -> list[tuple[str, int]]:
    """把「内蒙古7注，上海1注，安徽1注，共9注。」解析成 [('内蒙古',7),('上海',1),('安徽',1)]。

    同时兼容中彩网大乐透的括号格式「辽宁(基本1注)、湖北(基本3注 追加1注)」——
    取括号内「基本N注」为该省头奖注数。★「追加M注」是同一注的加价投注，
    不是额外的注，**不并入**（否则分省合计会多出追加那些注，与奖级表对不上）。

    容错：全角/半角逗号、空格、无「共」结尾、多行。解析不出来就返回空列表——
    绝不猜测，宁可这一期没有地区行。

    ★★必须**先压掉所有空白**：老期原文里有「甘 肃(基本1注)」「浙 江(基本1注)」
      「追加 1注」这种排版空格 —— 不压掉就会整省漏计（实测 2018~2019 有 132 期
      分省合计比官方一等奖少 1~2 注，全是这类空格/「内蒙」缩写导致）。
    ★★「其中…」之后全是**前面总数的子集细分**，必须截掉，否则重复计数：
      「其中复式投注为：…」「其中复式或胆拖投注为：…」「其中一等奖特别奖为：…」（派奖期）。
      实测双色球 176 期、七乐彩 21 期分省合计虚高（2016132 分省 51 vs 官方 37）。
    """
    if not text:
        return []
    s = re.sub(r"[\s\u3000]+", "", str(text))
    # 子集段截断（见上）；「其中」不可能是省名的一部分
    s = s.split("其中", 1)[0]
    out: list[tuple[str, int]] = []

    # 1) 括号格式（大乐透）：省名(...) → 取「基本N注」；无「基本」标签时退回首个「N注」
    for m in _PAREN_REGION_RE.finditer(s):
        region = _ALIAS.get(m.group(1), m.group(1))
        inner = m.group(2)
        mb = _PAREN_BASIC_RE.search(inner)
        if mb:
            n = int(mb.group(1))
        else:
            ma = _PAREN_ANY_RE.search(inner)
            n = int(ma.group(1)) if ma else 0
        if n > 0:
            out.append((region, n))

    # 1b) 原文末尾被截断：最后一个省只有左括号（见 _TAIL_PAREN_RE 注释）
    if "(" in s or "（" in s:
        mt = _TAIL_PAREN_RE.search(s)
        if mt:
            region = _ALIAS.get(mt.group(1), mt.group(1))
            if region not in {r for r, _ in out}:
                inner = mt.group(2)
                mb = _PAREN_BASIC_RE.search(inner) or _PAREN_ANY_RE.search(inner)
                n = int(mb.group(1)) if mb else 0
                if n > 0:
                    out.append((region, n))

    # 2) 无括号格式（福彩 cwl / 中彩网双色球）：省名N注
    if not out:
        for m in _REGION_RE.finditer(s):
            region = _ALIAS.get(m.group(1), m.group(1))
            n = int(m.group(2))
            if n > 0:
                out.append((region, n))

    # 同一省重复出现（如「广东3注，广东1注」）合并
    if out:
        merged: dict[str, int] = {}
        for r, n in out:
            merged[r] = merged.get(r, 0) + n
        out = list(merged.items())
    return out


def parse_total_from_text(text: str) -> int | None:
    """取「共N注」；没有则返回 None（不要用地区和代替，两者可能不等）。

    括号格式（大乐透）没有「共N注」结尾，此时返回 None——
    头奖总注数应由各地区注数求和或奖级注数另行补足。
    """
    if not text:
        return None
    m = _TOTAL_RE.search(str(text))
    return int(m.group(1)) if m else None


def parse_non_region_buckets(text: str) -> list[tuple[str, int]]:
    """取「非地域口径」的中奖注数，如「积分换彩1注」= 用积分兑换的票中奖，没有省份。

    ★为什么不写进 prize_region：没有省可归，硬塞一个假地区等于伪造。
    ★为什么校验要认它：官方一等奖总数**包含**这些注 ⇒ 不认就会每轮报「偏少」
      （实测 qlc 2020033 分省 1 + 积分换彩 1 = 官方 2）。
    """
    if not text:
        return []
    s = re.sub(r"[\s\u3000]+", "", str(text))
    return [(m.group(1), int(m.group(2))) for m in _BUCKET_RE.finditer(s)]


# ------------------------------------------------------------------ 通用工具

def to_int(v) -> int | None:
    """金额/注数归一化为整数元。源里可能是 '304,050,337' 或 '796,723,787.40'。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v)))
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def norm_nums(values) -> list[int] | None:
    nums = []
    for v in values:
        s = str(v).strip()
        if not s:
            continue
        try:
            nums.append(int(s))
        except ValueError:
            return None
    return nums or None


def split_issue(issue: str, lottery: str) -> tuple[int | None, int | None]:
    """把期号拆成 (年, 年内序号)，仅用于缺期检测；解析不了返回 (None, None)。

    实测位数：双色球 7 位(2026120)/4 位(3089，2003 年旧写法)；3D、快乐8 7 位；
    体彩 5 位(26119)。★不能直接整串相减——跨年会误报。
    """
    s = str(issue).strip()
    if not s.isdigit():
        return None, None
    if len(s) == 7:
        y, n = int(s[:4]), int(s[4:])
        if 1990 <= y <= 2100 and n > 0:
            return y, n
        return None, None
    if len(s) == 5:
        y, n = int(s[:2]), int(s[2:])
        if n > 0:
            return 2000 + y, n
        return None, None
    if len(s) == 4 and lottery in ("ssq", "dlt"):
        # 早年旧写法：双色球 3089 => 2003 年第 89 期
        #             大乐透 7001 => 2007 年第 1 期（2007-2009 用了 4 位）
        y, n = int(s[0]), int(s[1:])
        if 3 <= y <= 9 and n > 0:
            return 2000 + y, n
    return None, None


def _get(url: str, referer: str | None = None, timeout: int = 40,
         retries: int = 3, as_json: bool = True):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8", "replace")) if as_json else raw
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError("请求失败 %s：%s" % (url[:110], last))


# ------------------------------------------------------------------ 福彩官方 cwl

CWL_BASE = ("https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
            "?name=%s&issueCount=&pageNo=%d&pageSize=%d&systemType=PC")
CWL_REFERER = "https://www.cwl.gov.cn/ygkj/wqkjgg/"

_CWL_PAGE = 500


def iter_cwl(code: str, max_pages: int = 40, sleep: float = 0.6):
    """翻页抓福彩官方。★翻页超界返回 state=0 + result:[]，不报错，必须按长度判停。

    yield (draw_row, prize_rows, region_rows)
    """
    meta = LOTTERIES[code]
    name = meta["cwl"]
    page = 1
    seen: set[str] = set()
    while page <= max_pages:
        d = _get(CWL_BASE % (name, page, _CWL_PAGE), referer=CWL_REFERER)
        recs = d.get("result") or []
        if not recs:
            break
        new = 0
        for x in recs:
            issue = str(x.get("code") or "").strip()
            if not issue or issue in seen:
                continue
            seen.add(issue)
            new += 1
            yield _cwl_normalize(code, x)
        if new == 0:
            break
        page += 1
        time.sleep(sleep)


# 福彩官方 prizegrades 的 type 含义按彩种不同，不能一律当「一等奖」
#   双色球/七乐彩：1..N = 一..N 等奖
#   福彩3D：1=单选 2=组选3 3=组选6（★不是一二三等奖）
#   快乐8：type 是 'x10z10' 这类字符串（选十中十），没有等级序号
_CWL_LEVEL_NAMES = {
    "ssq": {1: "一等奖", 2: "二等奖", 3: "三等奖", 4: "四等奖", 5: "五等奖", 6: "六等奖"},
    "qlc": {1: "一等奖", 2: "二等奖", 3: "三等奖", 4: "四等奖", 5: "五等奖",
            6: "六等奖", 7: "七等奖"},
    "sd":  {1: "单选", 2: "组选3", 3: "组选6"},
}


def _cwl_normalize(code: str, x: dict):
    issue = str(x.get("code")).strip()
    draw_date = (str(x.get("date") or "")[:10]) or None

    front = norm_nums(str(x.get("red") or "").split(","))
    back = norm_nums(str(x.get("blue") or "").split(","))

    grades = x.get("prizegrades") or []
    prize_rows = []
    level_map = _CWL_LEVEL_NAMES.get(code, {})
    # ★福彩3D 的 typemoney 是「总奖金」而不是单注奖金
    #   （13837 注 × 约 1000 元 ≈ 13,823,200，若当单注则是 1382 万/注，显然荒谬）
    #   其余彩种（双色球/七乐彩/快乐8）是单注奖金。
    amount_kind = "total" if code == "sd" else "per_bet"
    for idx, g in enumerate(grades, start=1):
        raw_type = g.get("type")
        lvl = to_int(raw_type)
        if lvl is None:
            # 快乐8 这类非等级制：type 是 'x10z10'，没有等级序号，
            # ★必须用出现顺序当 level——否则 39 个奖级全落在 level=0，
            #   主键 (lottery,issue,source,level) 会让它们互相覆盖，只剩 1 行。
            lvl = idx
        winners = to_int(g.get("typenum"))
        amount = to_int(g.get("typemoney"))
        name = level_map.get(lvl) or (str(raw_type) if to_int(raw_type) is None else None)
        # 注数与奖金都为空的行不落库（3D 全空即是此情形）
        if winners is None and amount is None:
            continue
        prize_rows.append(dict(level=lvl, level_name=name, winners=winners,
                               amount=amount, amount_kind=amount_kind))

    content = (x.get("content") or "").strip()
    region_rows = [dict(region=r, winners=n) for r, n in parse_region_text(content)]

    # 头奖总注数优先取原文「共N注」——它和分省明细同源，比 prizegrades 更一致
    total = parse_total_from_text(content)
    if total is None and grades:
        total = to_int(grades[0].get("typenum"))

    y, n = split_issue(issue, code)
    draw_row = dict(
        lottery=code, issue=issue, source="cwl",
        issue_year=y, issue_seq=n, draw_date=draw_date,
        front=json.dumps(front, ensure_ascii=False) if front else None,
        back=json.dumps(back, ensure_ascii=False) if back else None,
        seq_front=None, seq_back=None,
        sales=to_int(x.get("sales")), pool=to_int(x.get("poolmoney")),
        first_winners=total,
        first_region_raw=content or None,
    )
    return draw_row, prize_rows, region_rows


# ------------------------------------------------------------------ 体彩官方

SP_BASE = ("https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
           "?gameNo=%s&provinceId=0&pageSize=%d&isVerify=1&pageNo=%d")
SP_REFERER = "https://static.sporttery.cn/"
_SP_PAGE = 100  # 实测上限就是 100，传 500 也只返 100


def iter_sporttery(code: str, max_pages: int = 120, sleep: float = 0.5):
    meta = LOTTERIES[code]
    game = meta["game"]
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        d = _get(SP_BASE % (game, _SP_PAGE, page), referer=SP_REFERER)
        lst = ((d.get("value") or {}).get("list") or [])
        if not lst:
            break
        new = 0
        for x in lst:
            issue = str(x.get("lotteryDrawNum") or "").strip()
            if not issue or issue in seen:
                continue
            seen.add(issue)
            new += 1
            yield _sp_normalize(code, x)
        if new == 0:
            break
        time.sleep(sleep)


def _sp_normalize(code: str, x: dict):
    issue = str(x.get("lotteryDrawNum")).strip()
    draw_date = (str(x.get("lotteryDrawTime") or "")[:10]) or None

    # ★lotteryDrawResult 是排序后；lotteryUnsortDrawresult 是出球顺序
    result = norm_nums(str(x.get("lotteryDrawResult") or "").split())
    unsort = norm_nums(str(x.get("lotteryUnsortDrawresult") or "").split())

    zones = LOTTERIES[code]["zones"]
    # ★数字型（排3/排5/七星彩/3D）每个 zone 只有 1 位，全部属于「前区」，
    #   不能按 zones[0].count=1 去切，否则 47 1 会被拆成 [4]+[7,1]。
    #   只有乐透型（大乐透 5+2、双色球 6+1）才需要切前后区。
    digital = all(z["count"] == 1 for z in zones)

    front, back, seq_front, seq_back = None, None, None, None
    if result:
        if digital:
            front = result
        else:
            n_front = zones[0]["count"]
            front = result[:n_front]
            back = result[n_front:] or None
    if unsort and len(unsort) == len(result or []):
        if digital:
            seq_front = unsort
        else:
            seq_front = unsort[:len(front)]
            seq_back = unsort[len(front):] or None

    prize_rows = []
    for idx, g in enumerate((x.get("prizeLevelList") or []), start=1):
        name = str(g.get("prizeLevel") or "").strip()
        if not name:
            continue
        lvl = _level_from_name(name)
        if lvl == 0:
            # 非等级制（排列3 的直选/组选3/组选6、排列5 的直选）：
            # 按出现顺序编号，否则三个奖级全落在 level=0 互相覆盖。
            lvl = idx
        winners = to_int(g.get("stakeCount"))
        amount = to_int(g.get("stakeAmountFormat") or g.get("stakeAmount"))
        if winners is None and amount is None:
            continue
        prize_rows.append(dict(level=lvl, level_name=name, winners=winners,
                               amount=amount, amount_kind="per_bet"))

    y, n = split_issue(issue, code)
    draw_row = dict(
        lottery=code, issue=issue, source="sporttery",
        issue_year=y, issue_seq=n, draw_date=draw_date,
        front=json.dumps(front, ensure_ascii=False) if front else None,
        back=json.dumps(back, ensure_ascii=False) if back else None,
        seq_front=json.dumps(seq_front, ensure_ascii=False) if seq_front else None,
        seq_back=json.dumps(seq_back, ensure_ascii=False) if seq_back else None,
        sales=to_int(x.get("totalSaleAmount")),
        # ★奖池：poolBalance 是「本期开奖前」，poolBalanceAfterdraw 是「本期开奖后」。
        #   现有 CSV 存的是开奖后（904 期能对上的就是它），取 after 才与 CSV 同口径，
        #   否则每期都会差一个头奖派发额，被误判成数据冲突。
        pool=to_int(x.get("poolBalanceAfterdraw")) or to_int(x.get("poolBalance")),
        first_winners=None,
        first_region_raw=None,   # 体彩官方不提供分省中奖分布
    )
    return draw_row, prize_rows, []


_LEVEL_WORDS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
                "七": 7, "八": 8, "九": 9, "十": 10}


_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
           6: "六", 7: "七", 8: "八", 9: "九"}


def _level_from_name(name: str) -> int:
    """奖级名 → level。

    「一等奖」→1；「直选」「组选3」→ 0（非等级制，用 0 占位）。
    ★「一等奖(追加)」不能也映射成 1——大乐透同时有一等奖和一等奖追加，
      同一个 level 会让两条互相覆盖，只剩一条。追加档统一放到 100+n。
    """
    is_append = "追加" in name
    for ch, n in _LEVEL_WORDS.items():
        if ch + "等奖" in name:
            return 100 + n if is_append else n
    return 0


# ------------------------------------------------------------------ 中彩网

ZHCW_BASE = ("https://jc.zhcw.com/port/client_json.php?transactionType=10001001"
             "&lotteryId=%d&issueCount=%d&type=0&pageNum=1&pageSize=%d&tt=0.1&_=1")
ZHCW_REFERER = "https://www.zhcw.com/kjxx/"


def fetch_zhcw(code: str, count: int = 4000):
    """中彩网一次取全。★pageNum 无效（>=5 返空），必须用 issueCount 拉。"""
    lid = LOTTERIES[code]["zhcw"]
    if not lid:
        return
    d = _get(ZHCW_BASE % (lid, count, count), referer=ZHCW_REFERER, timeout=90)
    for x in (d.get("data") or []):
        yield _zhcw_normalize(code, x)


def _zhcw_normalize(code: str, x: dict):
    issue = str(x.get("issue") or "").strip()
    draw_date = (str(x.get("openTime") or "")[:10]) or None

    front = norm_nums(str(x.get("frontWinningNum") or "").split())
    back = norm_nums(str(x.get("backWinningNum") or "").split())
    seq_front = norm_nums(str(x.get("seqFrontWinningNum") or "").split())
    seq_back = norm_nums(str(x.get("seqBackWinningNum") or "").split())

    # winnerDetails 只有奖级注数，没有地区
    prize_rows = []
    for i, w in enumerate(x.get("winnerDetails") or [], start=1):
        base = w.get("baseBetWinner") or {}
        lvl = to_int(w.get("awardEtc")) or i
        winners = to_int(base.get("awardNum"))
        amount = to_int(base.get("awardMoney"))
        if winners is None and amount is None:
            continue
        prize_rows.append(dict(level=lvl, level_name=str(base.get("remark") or ""),
                               winners=winners, amount=amount,
                               amount_kind="per_bet"))

    y, n = split_issue(issue, code)
    draw_row = dict(
        lottery=code, issue=issue, source="zhcw",
        issue_year=y, issue_seq=n, draw_date=draw_date,
        front=json.dumps(front, ensure_ascii=False) if front else None,
        back=json.dumps(back, ensure_ascii=False) if back else None,
        seq_front=json.dumps(seq_front, ensure_ascii=False) if seq_front else None,
        seq_back=json.dumps(seq_back, ensure_ascii=False) if seq_back else None,
        sales=to_int(x.get("saleMoney")), pool=to_int(x.get("prizePoolMoney")),
        first_winners=None, first_region_raw=None,
    )
    return draw_row, prize_rows, []


# ------------------------------------------------------------------ 中彩网：详情页头奖分省

# ★中彩网列表接口（10001001）的 winnerDetails 只有奖级注数、没有地区；
#   头奖分省藏在「详情页接口」里：kjxq 页面用 avalon.js 模板 {{j1zjqk}} 占位，
#   由 transactionType=10001002 的 JSONP 接口返回 firstPrizeAddress 字段填充。
#   该字段只对乐透型（双色球/七乐彩/快乐8/大乐透）有意义，
#   数字型（3D/排3/排5/七星彩）返回空串/None —— 数字型本就没有「头奖」概念。
ZHCW_DETAIL_BASE = ("https://jc.zhcw.com/port/client_json.php"
                    "?transactionType=10001002&lotteryId=%s&issue=%s&tt=0.1")

# lotteryId 与列表接口（10001001）用的是同一套 czid 映射
ZHCW_DETAIL_ID = {
    "ssq": 1, "sd": 2, "qlc": 3, "kl8": 6,
    "dlt": 281, "pl3": 283, "pl5": 284, "qxc": 282,
}


def fetch_zhcw_region(code: str, issue: str) -> tuple[str | None, list[tuple[str, int]]]:
    """抓中彩网详情页接口的头奖分省。

    返回 (first_region_raw, region_rows)，其中 region_rows 是 [('辽宁', 1), ...]。
    - 源头没分省（数字型 / 该期无人中头奖 / 接口无此字段）→ 返回 (None, [])。
    - 绝不猜测：firstPrizeAddress 为空时直接返回空，不伪造「共0注」。

    ★★请求本身失败（网络/限流/断连）必须**抛异常**，不能返回 (None, [])：
      两者在调用方眼里长得一模一样，会把「抓失败」记成「无人中」。
      2026-11-01 实踩：中彩网在约 1600 次请求后开始 `RemoteDisconnected`，
      旧代码静默返回空 ⇒ 统计口径里全算成 empty，看着像「数据本来就没有」。
    
    格式实测（2026-11-01）：
      大乐透  「辽宁(基本1注)、湖北(基本3注 追加1注)、湖南(基本1注)、重庆(基本1注)。」
      双色球  「江苏1注，山东2注，广东1注，广西2注，四川1注，西藏1注，共8注。」
      七乐彩  「河南1注，陕西1注，共2注。」
    """
    lid = ZHCW_DETAIL_ID.get(code)
    if lid is None:
        return None, []
    # ★失败就让它抛出去（见 docstring）：绝不把「抓不到」伪装成「没有」
    raw = _get(ZHCW_DETAIL_BASE % (lid, issue), referer=ZHCW_REFERER, timeout=40,
               as_json=False)
    # JSONP：callback({...})；直接用 json.loads 可能失败，剥掉 callback 外壳
    txt = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None, []
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, []
    if d.get("resCode") != "000000":
        return None, []
    fpa = (d.get("firstPrizeAddress") or "").strip()
    if not fpa:
        return None, []
    region_rows = [(r, n) for r, n in parse_region_text(fpa)]
    return fpa, region_rows


# ------------------------------------------------------------------ 财政部

MOF_INDEX = "https://zhs.mof.gov.cn/zonghexinxi/"


def _sq(v) -> str:
    """压掉所有空白（含全角空格）再比较。

    ★2008 年早期的表头是排版用的「地   区」「月    份」「比上年同 期增长%」，
      直接 `"地区" in s` 匹配不到，整张表会被判成「没有分省数据」。
    """
    return re.sub(r"[\s\u3000]+", "", str(v))


def _pick_region_sheet(path: str, probe_rows: int = 45) -> str | None:
    """自动挑出附件里「各地区彩票销售情况」那张表。

    ★ 绝不写死 sheet 名 —— 实测十余年里命名换过四代：
        2008-08  附件2.xls → 「计算」
        2012-05  ...表.xls → 「Sheet1」（而且同月有 3 个附件，另两个是总表/分类型表）
        2017-09  附件.xlsx → 「2017年9月全国各地区彩票销售情况表」
        2026-09  附件.xlsx → 「各地区彩票销售情况」
      写死名字的结果是「2017 年之前的月份全部解析为空」，而不是报错。

    判据：首列出现 ≥3 个省级行政区名，且表内文本含「销售额」。
    """
    import pandas as pd

    try:
        names = pd.ExcelFile(path).sheet_names
    except Exception:
        return None
    best, best_score = None, 0
    for sh in names:
        try:
            df = pd.read_excel(path, sheet_name=sh, header=None)
        except Exception:
            continue
        # 空 sheet / 只有一两列的说明页直接跳过（否则 df.iloc[:, 0] 会 IndexError）
        if df.empty or df.shape[1] < 3:
            continue
        hits = 0
        for v in df.iloc[:probe_rows, 0].tolist():
            t = _sq(v)
            if not t or t.lower() == "nan":
                continue
            if _ALIAS.get(t, t) in REGIONS:
                hits += 1
        if hits < 3:
            continue
        head = " ".join(str(v) for v in df.iloc[:20].values.flatten())
        score = hits * 2 + (1 if "销售额" in head else 0)
        if score > best_score:
            best, best_score = sh, score
    return best


def parse_mof_xlsx(path: str, ym: str, sheet: str | None = None) -> list[dict]:
    """解析财政部《全国彩票销售情况》附件里的「各地区彩票销售情况」表。

    ★实测是三段式表头（不是一行）：
        row3: 地区 | 福利彩票 | 体育彩票
        row4:      | 本月 | 本年累计 | 本月 | 本年累计
        row5:      | 销售额 | 比上年同期增长% | ...
    所以把 hdr 行到数据首行之间的所有文本按列拼接成列名，再按关键字定位。
    列位置随月份会漂移，绝不写死列号。

    `sheet=None` 时由 `_pick_region_sheet` 自动挑选。
    """
    import pandas as pd

    sh = sheet or _pick_region_sheet(path)
    if not sh:
        return []
    df = pd.read_excel(path, sheet_name=sh, header=None)
    if df.empty:
        return []
    if df.shape[1] < 3:
        return []

    # 1) 找「地区」所在行
    hdr_row = None
    for i in range(min(12, len(df))):
        vals = [_sq(v) for v in df.iloc[i].tolist()]
        if "地区" in vals or "省份" in vals:
            hdr_row = i
            break
    if hdr_row is None:
        return []

    # 2) 找数据首行：第一列是省级行政区名
    data_start = None
    for i in range(hdr_row + 1, len(df)):
        name = _sq(df.iloc[i, 0])
        if name in REGIONS or _ALIAS.get(name) in REGIONS:
            data_start = i
            break
    if data_start is None:
        return []

    # 3) 合并表头 + 向下填充机构列（「福利彩票」只写在第 3 行第 2 列）
    ncol = df.shape[1]
    cols: list[str] = []
    for j in range(ncol):
        parts = []
        for i in range(hdr_row, data_start):
            t = _sq(df.iloc[i, j])
            if t and t.lower() != "nan":
                parts.append(t)
        cols.append("".join(parts))

    last_agency = ""
    merged: list[str] = []
    for j, c in enumerate(cols):
        if "福利彩票" in c or "福彩" in c:
            last_agency = "福彩"
        elif "体育彩票" in c or "体彩" in c:
            last_agency = "体彩"
        elif "合计" in c:
            last_agency = "合计"
        merged.append((last_agency + "|" + c) if j > 0 else c)

    # 4) 定位列。★不能靠「本月」定位：合并单元格后「本月」只写在第 2 列，
    #    第 3/4 列的文本是「比上年同期增长%」。改为按出现顺序取：
    #    销售额 第1个=本月、第2个=累计；增长% 第1个=本月同比、第2个=累计同比。
    def find_all(agency: str, key: str) -> list[int]:
        out = []
        for j, c in enumerate(merged):
            if j == 0 or not c.startswith(agency + "|"):
                continue
            if key in c.split("|", 1)[1]:
                out.append(j)
        return out

    idx = {"region": 0}
    for agency in ("福彩", "体彩"):
        pre = "fc" if agency == "福彩" else "tc"
        sales = find_all(agency, "销售额")
        yoys = find_all(agency, "增长")
        idx[pre + "_now"] = sales[0] if len(sales) > 0 else None
        idx[pre + "_cum"] = sales[1] if len(sales) > 1 else None
        idx[pre + "_yoy"] = yoys[0] if len(yoys) > 0 else None

    rows = []
    for i in range(data_start, len(df)):
        name = _sq(df.iloc[i, 0])
        if not name or name.lower() == "nan":
            continue
        region = _ALIAS.get(name, name)
        if region not in REGIONS:
            continue   # 跳过「合计」「全国」等非省份行
        for agency, pre in (("福彩", "fc"), ("体彩", "tc")):
            rows.append(dict(
                ym=ym, region=region, agency=agency,
                sales_wan=_num(df, i, idx.get(pre + "_now")),
                yoy=_num(df, i, idx.get(pre + "_yoy")),
                cumulative_wan=_num(df, i, idx.get(pre + "_cum")),
            ))
    return rows


def _num(df, i, j):
    if j is None:
        return None
    try:
        v = df.iloc[i, j]
    except Exception:
        return None
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    if not s or s.lower() == "nan" or s in ("-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ 财政部：公告清单与附件

MOF_INDEX = "https://zhs.mof.gov.cn/zonghexinxi/"
MOF_MAX_PAGES = 40          # 实测 33 页，留点余量

# 两种标题并存，实测都指向同一份月报：
#   「2008年9月份全国彩票销售情况」（2008-08 起）
#   「彩票销售情况简报2008年第9期」  （2008-01 ~ 2008-09，正文首句写死「X 月份」）
# ★只匹配第一种会漏掉 2008 年 1~7 月和 9 月，看起来像"源头没数据"，其实是标题换了皮。
_MOF_TITLE_RE = re.compile(r"(\d{4})年(\d{1,2})月份全国彩票销售情况")
_MOF_BRIEF_RE = re.compile(r"彩票销售情况简报\s*(\d{4})\s*年第\s*(\d{1,2})\s*期")


def _mof_url(url: str) -> str:
    """财政部链接统一走 https。

    ★2017-03 那期公告挂在 www.mof.gov.cn/gp/xxgkml/... 且原文是 http 链接，
      直接请求返回 403 Forbidden，换成 https 200。这不是网络抖动，是站点策略。
    """
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def iter_mof_posts(max_pages: int = 3, log=None, sleep: float = 0.25,
                   want=None) -> dict:
    """抓「全国彩票销售情况」公告清单，返回 {ym: {"url","title"}}。

    ★分页规律：`createPageHTML(33, 0, "index", "htm")` ⇒ index.htm + index_1..32.htm，
      每页约 10 条彩票公告。2008-08 起共 217 个月，只缺 2008-09。
    ★栏目页会混排非彩票内容，必须按标题正则过滤，不能按位置取。
    `want` 给定时，找齐这些月份就停（补特定月份不必扫完 33 页）。
    """
    out = {}
    want = set(want) if want else None
    for p in range(max(1, max_pages)):
        url = MOF_INDEX + ("index.htm" if p == 0 else "index_%d.htm" % p)
        try:
            html = _get(url, as_json=False).decode("utf-8", "replace")
        except Exception as e:
            if log:
                log("  [mof] 第 %d 页抓取失败：%s" % (p, e))
            continue
        n = 0
        for href, name in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{0,80})</a>', html):
            name = name.strip()
            m = _MOF_TITLE_RE.search(name) or _MOF_BRIEF_RE.search(name)
            if not m:
                continue
            mon = int(m.group(2))
            if not 1 <= mon <= 12:
                continue
            ym = "%s-%02d" % (m.group(1), mon)
            out.setdefault(ym, {"url": urllib.parse.urljoin(url, href), "title": name})
            n += 1
        if log and n:
            log("  [mof] 第 %-2d 页 %d 篇" % (p, n))
        if n == 0 and p > 0:
            break                      # 已经翻过最后一页
        if want and want <= set(out):
            break                      # 目标月份已找齐
        time.sleep(sleep)
    return out


def mof_attachments(post_url: str) -> list:
    """从公告页取附件列表 [(文件名, 绝对 URL)]。只认 xls/xlsx。"""
    html = _get(_mof_url(post_url), as_json=False).decode("utf-8", "replace")
    out, seen = [], set()
    for href, name in re.findall(r'href="([^"]+\.(?:xlsx|xls))"[^>]*>([^<]{0,80})', html, re.I):
        full = _mof_url(urllib.parse.urljoin(post_url, href))
        if full in seen:      # 同一附件在页面上常被写 3 遍（附件 / 下载 / 预览）
            continue
        seen.add(full)
        out.append((name.strip() or "附件", full))
    return out


def fetch_mof_month(ym: str, post_url: str, cache_dir: str, log=None) -> dict:
    """下载某月的全部附件并解析，返回 {"rows","file","sheet","files"}。

    ★早期一个月可能有 3 个附件（总表 / 分类型 / 各地区），逐个试、取省份行最多的那份，
      不要「取第一个」—— 2012-05 第一个附件是总表，解析出来是 0 行。
    ★xls 与 xlsx 混用（2017 起才是 xlsx），两者都靠 pandas + 对应引擎读。
    """
    import os

    os.makedirs(cache_dir, exist_ok=True)
    try:
        atts = mof_attachments(post_url)
    except Exception as e:
        return {"rows": [], "error": "附件列表失败：%s" % e, "files": 0}

    best = {"rows": [], "file": None, "sheet": None, "files": len(atts),
            "error": None, "no_region": False}
    if not atts:
        best["no_region"] = True
        return best
    for name, url in atts:
        ext = os.path.splitext(url)[1].lower() or ".xlsx"
        safe = re.sub(r"[^\w\u4e00-\u9fa5]", "", name)[:24] or "a"
        fp = os.path.join(cache_dir, "%s_%s%s" % (ym, safe, ext))
        if not os.path.exists(fp) or os.path.getsize(fp) == 0:
            try:
                blob = _get(url, as_json=False)
                with open(fp, "wb") as f:
                    f.write(blob)
            except Exception as e:
                best["error"] = "下载失败 %s：%s" % (name, e)
                continue
        try:
            rows = parse_mof_xlsx(fp, ym)
        except Exception as e:
            best["error"] = "解析失败 %s：%s" % (name, e)
            continue
        if len(rows) > len(best["rows"]):
            best.update(rows=rows, file=os.path.basename(fp),
                        sheet=_pick_region_sheet(fp), error=None)
    # ★附件都下下来了、只是里头根本没有「分地区」表（如 2020-02 疫情停售，
    #   财政部当月只发了全国汇总）—— 这是源头缺数据，不是我们抓失败。
    if not best["rows"] and not best["error"]:
        best["no_region"] = True
    return best


# ------------------------------------------------------------------ 现有 CSV（只读）

CSV_MAP = {
    "ssq": "双色球历史数据.csv",
    "dlt": "大乐透历史数据.csv",
    "pl3": "排列3历史数据.csv",
    "pl5": "排列5历史数据.csv",
    "sd":  "福彩3D历史数据.csv",
    "qxc": "七星彩历史数据.csv",
}


def iter_csv(lottery_data_dir: str, code: str):
    """只读现有 CSV，产出与官方源同形状的行（source='csv500'）。

    ★绝对不写回 CSV。编码不统一（实测 2 个 GBK、4 个 UTF-8-sig），逐个试。
    """
    import csv as _csv
    import os

    fn = CSV_MAP.get(code)
    if not fn:
        return
    path = os.path.join(lottery_data_dir, fn)
    if not os.path.exists(path):
        return

    text = None
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                text = f.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return

    reader = _csv.DictReader(text.splitlines())
    zones = LOTTERIES[code]["zones"]
    total_slots = sum(z["count"] for z in zones)
    for r in reader:
        issue = (r.get("期号") or r.get("期数") or "").strip()
        if not issue:
            continue

        # 两种形状：
        #   乐透型  红球1..N + 蓝球1..M（双色球/大乐透）
        #   数字型  号码1..K，每位是一个 0-9 的 zone（排3/排5/3D/七星彩）
        if "号码1" in r:
            front = norm_nums([r.get("号码%d" % (k + 1)) for k in range(total_slots)])
            back = []
        else:
            # CSV 里乐透型统一叫「红球N/蓝球N」，而 zone 名是「前区/后区」等，
            # 所以按位置回退到红/蓝，别只认 zone.name（否则大乐透号码会全丢）
            front, back = [], []
            for z_i, z in enumerate(zones):
                names = [z["name"]] + (["红球"] if z_i == 0 else ["蓝球"])
                nums = None
                for nm in names:
                    if "%s1" % nm in r:
                        nums = norm_nums([r.get("%s%d" % (nm, k))
                                          for k in range(1, z["count"] + 1)])
                        if nums:
                            break
                if nums:
                    (front if z_i == 0 else back).extend(nums)

        date_col = r.get("开奖日期") or r.get("日期") or ""
        draw_date = _norm_date(date_col)
        y, n = split_issue(issue, code)

        sales = to_int(r.get("总投注额(元)") or r.get("投注总额") or r.get("销售额"))
        pool = to_int(r.get("奖池奖金(元)") or r.get("奖池"))

        # CSV 只带 1、2 等奖，能补一点是一点（其余奖级只有官方源有）
        prize_rows = []
        for lvl, nk, ak in ((1, "一等奖注数", "一等奖奖金（元）"),
                            (2, "二等奖注数", "二等奖奖金（元）")):
            if nk not in r:
                continue
            w, a = to_int(r.get(nk)), to_int(r.get(ak))
            if w is None and a is None:
                continue
            # 统一用中文数字，和各官方源口径一致（别出现「1等奖」与「一等奖」并列）
            prize_rows.append(dict(level=lvl, level_name=_CN_NUM[lvl] + "等奖",
                                   winners=w, amount=a, amount_kind="per_bet"))

        yield dict(
            lottery=code, issue=issue, source="csv500",
            issue_year=y, issue_seq=n, draw_date=draw_date,
            front=json.dumps(front, ensure_ascii=False) if front else None,
            back=json.dumps(back, ensure_ascii=False) if back else None,
            seq_front=None, seq_back=None,
            sales=sales, pool=pool,
            first_winners=next((p["winners"] for p in prize_rows if p["level"] == 1), None),
            first_region_raw=None,
        ), prize_rows, []


def _norm_date(v: str) -> str | None:
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % tuple(int(g) for g in m.groups())
    return None
