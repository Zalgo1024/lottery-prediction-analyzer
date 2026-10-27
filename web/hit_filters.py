"""
web/hit_filters.py —— 「命中条件」筛选项的单一真源

/hits 页的命中条件下拉分两组，前端不再手抄任何规则：

  通用        总命中 ≥ N —— 所有彩种同一口径（红蓝型 = 红球+蓝球，数字型 = 对上的位数）
  彩种专属    该彩种规则下的命中结构（红球/蓝球、前区/后区、某一位是否命中）

★ 为什么必须分两组：同一个「总命中 ≥ 4」在双色球是 4+0（五等奖），在大乐透是另一个
  奖级，在 3D 是「三位全中」= 直选 —— 跨彩种根本不可比。通用档只保证「口径同一」，
  不保证「含义同一」；要知道含义必须看彩种专属档。

★ 规则依据（不在这里重新发明，全部指向已有真源）：
  · 双色球奖级   data/feedback.py::_classify_prize
  · 大乐透奖级   data/prize_versions.py::classify_dlt（**五段版本**：8/8/6/9/7 级）
  · 七星彩奖级   data/feedback.py::_qxc_prize（X=前6位命中, Y=第7位命中, T=X+Y）
  · 3D/排3/排5   data/prize_table.py 顶部（直选/组选）
★ 大乐透的五段版本会让「几等奖」的断言在任何一次改版后过期 ⇒ 它的专属档**只写命中
  结构、不标奖级**；双色球与七星彩的规则稳定，才标「至少几等奖」。

★ 分区名归一化复用 `data/schema.normalize_zone_names`：3D/排3/排5 有 83 条历史记录
  仍用「百位/十位/个位/千位/万位」作 `分区命中` 的键名（2026 年前后统一为「第N位」），
  不归一会让这些记录在分区筛选里被**静默筛漏**。
"""

from typing import Dict, List, Optional, Tuple

from data.schema import get_schema, is_redblue, normalize_zone_names

# 「全部彩票」时的通用档上界：取各彩种最大可能命中数
# （双色球 6+1 / 大乐透 5+2 / 七星彩 6+1 都是 7）
_ALL_MAX_TOTAL = 7

# 逻辑组：把若干分区折叠成一个可整体计数的量。
# 七星彩官方记法本身就是 X（前 6 位命中数）/ Y（后区是否命中）/ T=X+Y，
# 这里沿用同一套，避免在界面上让人自己心算六位。
_LOGICAL_GROUPS: Dict[str, Dict[str, List[str]]] = {
    "七星彩": {
        "前区": ["第1位", "第2位", "第3位", "第4位", "第5位", "第6位"],
        "后区": ["第7位"],
    },
}

# 数字型位名的口语叫法（仅用于展示：新记录叫「第N位」，老叫法按位数从右往左对齐）
_POS_HUMAN: Dict[int, List[str]] = {
    3: ["百位", "十位", "个位"],
    5: ["万位", "千位", "百位", "十位", "个位"],
}

# ---------------------------------------------------------------- 通用档说明

_GENERIC_NOTE = {
    "双色球": "红球 + 蓝球命中个数之和（红球最多 6、蓝球最多 1，故上限 7）",
    "大乐透": "前区 + 后区命中个数之和（前区最多 5、后区最多 2，故上限 7）",
    "福彩3D": "位置和数字都对上的位数（上限 3）。3D 只有三位全中才是直选，中 1~2 位只是部分命中",
    "排列3": "位置和数字都对上的位数（上限 3）。排列3 只有三位全中才是直选，中 1~2 位只是部分命中",
    "排列5": "位置和数字都对上的位数（上限 5）。排列5 只设直选一个奖级，五位全对才有奖",
    "七星彩": "前 6 位与第 7 位命中个数之和（上限 7）。按命中位数分级，≥3 位或第 7 位命中即六等奖",
}
_GENERIC_NOTE_ALL = (
    "红蓝型 = 红球 + 蓝球命中之和；数字型 = 位置和数字都对上的位数。"
    "口径一致但含义不同：同一档在不同彩种对应的奖级不一样，要按彩种看请看下方「专属」组"
)

# ---------------------------------------------------------------- 彩种专属档
# (传给后端的条件的 key, 下拉显示标签, 含义说明)
_SPECIFIC_SPEC: Dict[str, List[Tuple[str, str, str]]] = {
    "双色球": [
        ("蓝球:1", "蓝球命中", "六等奖起（单注 5 元）：红球 0~2 个且蓝球命中就是六等奖"),
        ("红球:4", "红球 ≥ 4", "至少五等奖（4+0，10 元）。注意 3+1 也是五等奖，但红球只有 3"),
        ("红球:4,蓝球:1", "红球 ≥ 4 且蓝球命中", "至少四等奖（4+1，200 元）"),
        ("红球:5", "红球 ≥ 5", "至少四等奖（5+0，200 元）"),
        ("红球:5,蓝球:1", "红球 ≥ 5 且蓝球命中", "至少三等奖（5+1，3000 元）"),
        ("红球:6", "红球 6 个全中", "至少二等奖（6+0，浮动，按当期奖池与中奖注数分）"),
        ("红球:6,蓝球:1", "6+1 全中", "一等奖（浮动，按当期奖池与中奖注数分）"),
    ],
    "大乐透": [
        ("蓝球:1", "后区 ≥ 1", "后区（12 选 2）命中至少 1 个"),
        ("红球:3", "前区 ≥ 3", "前区（35 选 5）命中至少 3 个"),
        ("红球:4", "前区 ≥ 4", "前区命中至少 4 个"),
        ("红球:5", "前区 5 个全中", "前区（35 选 5）5 个全部命中"),
        ("红球:3,蓝球:1", "前区 ≥ 3 且后区 ≥ 1", "3+1 结构"),
        ("红球:4,蓝球:2", "前区 ≥ 4 且后区 2 个全中", "4+2 结构"),
        ("红球:5,蓝球:1", "前区 5 个 + 后区 1 个", "5+1 结构"),
        ("红球:5,蓝球:2", "5+2 全中", "前区后区全中"),
    ],
    "福彩3D": [
        ("第1位:1", "百位命中", "第 1 位数字与位置都对上"),
        ("第2位:1", "十位命中", "第 2 位数字与位置都对上"),
        ("第3位:1", "个位命中", "第 3 位数字与位置都对上"),
        ("第1位:1,第2位:1", "百位 + 十位都中", "两位都对上；仍是部分命中，本身不含奖项"),
        ("第1位:1,第2位:1,第3位:1", "三位全中（直选）", "直选 1040 元。组选3/组选6 不要求位置对上，不在此列"),
    ],
    "排列3": [
        ("第1位:1", "百位命中", "第 1 位数字与位置都对上"),
        ("第2位:1", "十位命中", "第 2 位数字与位置都对上"),
        ("第3位:1", "个位命中", "第 3 位数字与位置都对上"),
        ("第1位:1,第2位:1", "百位 + 十位都中", "两位都对上；仍是部分命中，本身不含奖项"),
        ("第1位:1,第2位:1,第3位:1", "三位全中（直选）", "直选 1040 元。组选3/组选6 不要求位置对上，不在此列"),
    ],
    "排列5": [
        ("第1位:1", "第 1 位（万位）命中", "位置和数字都对上"),
        ("第2位:1", "第 2 位（千位）命中", "位置和数字都对上"),
        ("第3位:1", "第 3 位（百位）命中", "位置和数字都对上"),
        ("第4位:1", "第 4 位（十位）命中", "位置和数字都对上"),
        ("第5位:1", "第 5 位（个位）命中", "位置和数字都对上"),
        ("第1位:1,第2位:1,第3位:1", "前三位全中", "前三位都对上；排列5 只设直选一个奖级，仍未中奖"),
        ("第1位:1,第2位:1,第3位:1,第4位:1,第5位:1", "五位全中（直选）", "排列5 唯一的奖级：直选 10 万元"),
    ],
    "七星彩": [
        ("后区:1", "第 7 位（后区）命中", "至少六等奖（单注 5 元）：后区单独命中就够六等奖"),
        ("前区:3", "前 6 位命中 ≥ 3", "至少六等奖（T≥3）"),
        ("前区:5", "前 6 位命中 ≥ 5", "至少四等奖（500 元）：X≥5 ⇒ T≥5"),
        ("前区:6", "前 6 位全中", "二等奖起：后区未中是二等，后区也中是一等"),
        ("前区:1,后区:1", "前 6 位中 ≥1 且第 7 位命中", "六等奖结构：T≥2 且后区命中"),
        ("前区:6,后区:1", "七位全中（T=7）", "一等奖（浮动，名义上限 500 万）"),
    ],
}


# ---------------------------------------------------------------- 基础工具


def _lottery_schema(lottery: str):
    if not lottery or lottery == "全部":
        return None
    try:
        return get_schema(lottery)
    except Exception:
        return None


def max_total(lottery: str) -> int:
    """该彩种「总命中」的理论上限（用于生成通用档，避免出现 3D 也能中 7 个的假选项）。"""
    schema = _lottery_schema(lottery)
    if schema is None:
        return _ALL_MAX_TOTAL
    return sum(int(z.choose) for z in schema.zones)


def zone_groups(lottery: str) -> Dict[str, List[str]]:
    """逻辑组 → 成员分区名。「总」恒为全部分区。"""
    schema = _lottery_schema(lottery)
    if schema is None:
        return {"总": []}
    groups = {"总": [z.name for z in schema.zones]}
    groups.update(_LOGICAL_GROUPS.get(lottery, {}))
    return groups


def zone_hits(rec: dict, lottery: str) -> Dict[str, int]:
    """取一条记录的逐分区命中，键已归一化到现行分区名。

    红蓝型读 `红球命中` / `蓝球命中`；数字型读 `分区命中`（旧位名归一化为 `第N位`）。
    """
    schema = _lottery_schema(lottery)
    if schema is not None and is_redblue(lottery):
        return {
            "红球": int(rec.get("红球命中") or 0),
            "蓝球": int(rec.get("蓝球命中") or 0),
        }
    raw = rec.get("分区命中") or {}
    if not isinstance(raw, dict):
        return {}
    if schema is not None:
        raw = normalize_zone_names(raw, schema=schema)
    out = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v or 0)
        except (TypeError, ValueError):
            continue
    return out


def group_hits(rec: dict, lottery: str) -> Dict[str, int]:
    """单分区命中 + 逻辑组合计命中（总 / 前区 / 后区）。"""
    zh = zone_hits(rec, lottery)
    out = dict(zh)
    for gname, members in zone_groups(lottery).items():
        if gname == "总":
            # 总命中优先用记录自带的字段（权威值），缺失才回落到逐分区求和
            total = rec.get("总命中")
            if total in (None, ""):
                total = sum(zh.get(m, 0) for m in members) if members else sum(zh.values())
            try:
                out["总"] = int(total or 0)
            except (TypeError, ValueError):
                out["总"] = sum(zh.get(m, 0) for m in members) if members else 0
        else:
            out[gname] = sum(zh.get(m, 0) for m in members)
    return out


def known_names(lottery: str) -> List[str]:
    """该彩种可用的条件原子名（单分区 + 逻辑组 + 总）。"""
    names = ["总"]
    schema = _lottery_schema(lottery)
    if schema is not None:
        names += [z.name for z in schema.zones]
    names += [g for g in _LOGICAL_GROUPS.get(lottery, {}) if g not in names]
    return names


# ---------------------------------------------------------------- 条件解析


def parse_condition(text: str) -> Dict[str, int]:
    """解析 `红球:4,蓝球:1` 形式的命中条件。空串 ⇒ {}（不筛）。

    原子名可以是单分区名（红球/蓝球/第N位），也可以是逻辑组名（总/前区/后区）。
    ★ 格式不对直接抛 ValueError，绝不静默忽略——静默忽略 = 筛选悄悄失效、页面假绿。
    """
    cond: Dict[str, int] = {}
    text = (text or "").strip()
    if not text:
        return cond
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError("命中条件格式应为「名称:最小命中」，收到 %r" % part)
        name, _, val = part.partition(":")
        name = name.strip()
        if not name:
            raise ValueError("命中条件 %r 缺少分区名" % part)
        try:
            need = int(val)
        except (TypeError, ValueError):
            raise ValueError("命中条件 %r 的最小命中不是整数" % part)
        if need < 0:
            raise ValueError("命中条件 %r 的最小命中不能为负" % part)
        cond[name] = max(need, cond.get(name, 0))
    return cond


def describe_condition(cond: Dict[str, int]) -> str:
    """把条件转回人话（用于日志/接口回显）。"""
    if not cond:
        return "不限"
    return " 且 ".join("%s ≥ %d" % (k, v) for k, v in cond.items())


def validate_condition(cond: Dict[str, int], lottery: str) -> None:
    """校验条件里的原子名在该彩种下存在。不存在 ⇒ ValueError（同样不许静默忽略）。"""
    if not cond:
        return
    if not lottery:
        # 「全部彩种」没有统一的分区：同一个「红球 ≥4」在数字型上根本不存在，
        # 若放行会把数字型记录按不存在的字段静默筛掉。
        bad = [n for n in cond if n != "总"]
        if bad:
            raise ValueError(
                "「全部彩种」下只能用「总命中」筛选（收到 %s）；要看分区命中请先在上方选定单个彩种"
                % "、".join(bad)
            )
        return
    names = set(known_names(lottery))
    for name in cond:
        if name not in names:
            raise ValueError(
                "%s 没有分区「%s」，可用：%s"
                % (lottery or "全部彩种", name, "、".join(sorted(names)))
            )


def match_condition(cond: Dict[str, int], hits: Dict[str, int]) -> bool:
    """逐项比较（调用前须已 validate_condition）。"""
    for name, need in cond.items():
        if int(hits.get(name, 0)) < need:
            return False
    return True


# ---------------------------------------------------------------- 下拉选项


def _generic_options(lottery: str) -> List[dict]:
    note = _GENERIC_NOTE.get(lottery, _GENERIC_NOTE_ALL)
    return [
        {
            "key": "总:%d" % n,
            "label": "总命中 ≥ %d" % n,
            "note": note,
        }
        for n in range(1, max_total(lottery) + 1)
    ]


def _specific_group(lottery: str) -> Optional[dict]:
    spec = _SPECIFIC_SPEC.get(lottery)
    if not spec:
        return None
    return {
        "标题": "%s · 按分区（该彩种规则）" % lottery,
        "档位": [{"key": k, "label": lb, "note": nt} for k, lb, nt in spec],
    }


def options_payload(lottery: str) -> dict:
    """`/api/hits/filters` 的返回体：页面下拉的全部内容，前端只负责渲染。"""
    lottery = (lottery or "全部").strip() or "全部"
    schema = _lottery_schema(lottery)
    pos_hint = ""
    if schema is not None and not is_redblue(lottery):
        human = _POS_HUMAN.get(len(schema.zones))
        if human:
            pos_hint = " · ".join(
                "第%d位=%s" % (i + 1, h) for i, h in enumerate(human)
            )
    return {
        "彩票": lottery,
        "通用": _generic_options(lottery),
        "专属": _specific_group(lottery),
        "上限": max_total(lottery),
        "分区": (
            [{"name": z.name, "max": int(z.choose)} for z in schema.zones]
            if schema is not None else []
        ),
        "位置对照": pos_hint,
        "口径说明": (
            "「总命中」红蓝型 = 红球 + 蓝球命中个数之和；数字型 = 位置和数字都对上的位数。"
            "「专属」组按该彩种官方奖级规则给出的命中结构，只影响显示范围，不改任何记录。"
        ),
    }
