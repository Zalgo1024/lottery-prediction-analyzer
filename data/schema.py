"""
数据结构定义
使用 dataclass 定义双色球和大乐透的开奖记录结构，含合法性校验
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from config import LOTTERY_CONFIG


class ValidationError(ValueError):
    """数据校验失败"""
    pass


@dataclass
class DrawRecord:
    """单期开奖记录

    阶段0/阶段2地基：号码统一存于 `zone_numbers`（分区名 -> 号码列表）。
    红球/蓝球 作为属性兼容旧代码（双色球/大乐透的区名即"红球"/"蓝球"）。
    新彩种（数字型/乐透型）直接以各自分区名存储，例如 排列5 为
    {"第1位":[..],"第2位":[..],...}。
    """
    期号: int
    开奖日期: Optional[date] = None
    zone_numbers: Dict[str, List[int]] = field(default_factory=dict)
    奖池奖金: Optional[int] = None
    一等奖注数: Optional[int] = None
    一等奖奖金: Optional[int] = None
    二等奖注数: Optional[int] = None
    二等奖奖金: Optional[int] = None
    总投注额: Optional[int] = None

    @property
    def 红球(self) -> List[int]:
        return self.zone_numbers.get("红球", [])

    @红球.setter
    def 红球(self, v: List[int]):
        self.zone_numbers["红球"] = list(v)

    @property
    def 蓝球(self) -> List[int]:
        return self.zone_numbers.get("蓝球", [])

    @蓝球.setter
    def 蓝球(self, v: List[int]):
        self.zone_numbers["蓝球"] = list(v)

    @property
    def lottery_type(self) -> str:
        """自动识别彩票类型（仅用于双色球/大乐透兼容）"""
        if len(self.红球) == 6 and len(self.蓝球) == 1:
            return "双色球"
        elif len(self.红球) == 5 and len(self.蓝球) == 2:
            return "大乐透"
        return "未知"

    def validate(self, lottery_name: Optional[str] = None) -> List[str]:
        """
        校验号码合法性，返回错误信息列表（空列表表示完全合法）。
        优先按传入/识别的彩票名的 Schema 分区校验；无法识别时回退旧逻辑。
        """
        errors = []
        name = lottery_name or self.lottery_type
        cfg = LOTTERY_CONFIG.get(name)
        if cfg is None:
            return [f"未知彩票类型：红球{len(self.红球)}个，蓝球{len(self.蓝球)}个"]

        # 统一按 Schema 分区校验（双色球/大乐透的区即 红球/蓝球，行为与原逻辑一致）
        from data.schema import schema_from_cfg
        schema = schema_from_cfg(cfg, name)
        for zone in schema.zones:
            nums = self.zone_numbers.get(zone.name, [])
            lo, hi = zone.range_tuple()
            # ★ 用 draw_count 而非 choose：本方法校验的是**开奖记录**。
            #   快乐8 开 20 / 票选 10、七乐彩特别号票面不含，都必须按开奖数判。
            if len(nums) != zone.draw_count:
                errors.append(f"{zone.name}数量应为{zone.draw_count}，实际{len(nums)}")
            for i, n in enumerate(nums, 1):
                if not (lo <= n <= hi):
                    errors.append(f"{zone.name}{i}值{n}超出范围[{lo},{hi}]")
            if not zone.repeatable and len(set(nums)) != len(nums):
                errors.append(f"{zone.name}存在重复号码")
        return errors


@dataclass
class LotteryData:
    """某彩票的完整数据集"""
    lottery_name: str
    records: List[DrawRecord] = field(default_factory=list)

    @property
    def total_records(self) -> int:
        return len(self.records)

    def get_red_matrix(self):
        """返回红球号码矩阵 (records × red_count)"""
        import numpy as np
        return np.array([r.红球 for r in self.records])

    def get_blue_matrix(self):
        """返回蓝球号码矩阵 (records × blue_count)"""
        import numpy as np
        return np.array([r.蓝球 for r in self.records])


def parse_draw_date(date_str: str) -> Optional[date]:
    """
    解析开奖日期字符串为 date 对象
    支持格式：d/m/yyyy、d/m/yy、yyyy-m-d
    """
    if not date_str or date_str.startswith("#"):
        return None

    date_str = date_str.strip()
    # d/m/yyyy
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            from datetime import datetime
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


# ============================================================
# 号码结构 Schema（阶段0地基）
# 把"红/蓝两区"抽象为通用的"区(Zone)"列表，供特征/训练/预测统一取数，
# 为后续接入乐透型(快乐8)、数字型(排列/3D/七星彩)打好地基。
# 双色球/大乐透的区顺序固定为 [红球, 蓝球]，保证现有行为零破坏。
# ============================================================

@dataclass
class Zone:
    """号码分区定义（泛型）

    - name: 区名（"红球"/"蓝球"/"基本号"/"第1位"…）
    - min / max: 号码取值区间（含端点）
    - choose: **票面**在该区选几个号（既有语义，勿改）
    - repeatable: 区内是否允许重复号码（数字型每位可 0-9 重复时为 True）
    - ordered: 该区是否按位有序（数字型为 True；乐透型为 False）
    - draw: **开奖**在该区摇出几个号。None ⇒ 等于 choose（既有 6 彩种零改动）
    - draw_only: 该区只出现在开奖、票面不含（七乐彩"特别号"）

    ★ 为什么 `draw` 是新增而不是改 `choose` 的语义（2026-11-01）
    接入七乐彩/快乐8 时暴露出「一个 choose 同时当两件事用」：
      · 快乐8：开奖摇 20 个，票面选 10 个（选十玩法）—— 20 ≠ 10；
      · 七乐彩：特别号从剩余 23 个里摇出，**票面根本没有这一区** —— 1 ≠ 0。
    `choose` 已被 20+ 处当作"票面数"消费（prediction/engine.py 出号、web/hit_filters.py
    命中统计、data/schema.total_choose…），改它的语义等于全局引爆。
    因此保留 `choose` = 票面语义，新增 `draw` = 开奖语义，`draw is None` 时两者恒等，
    既有 6 个彩种的配置、行为、序列化结果**逐字节不变**。
    """

    name: str
    min: int
    max: int
    choose: int
    repeatable: bool = False
    ordered: bool = False
    draw: Optional[int] = None
    draw_only: bool = False

    @property
    def size(self) -> int:
        return self.max - self.min + 1

    @property
    def draw_count(self) -> int:
        """开奖时该区应摇出几个号（校验开奖记录用这个）。"""
        return int(self.choose) if self.draw is None else int(self.draw)

    @property
    def ticket_count(self) -> int:
        """票面在该区应选几个号（出号/取特征/去重用这个）。draw_only 区恒为 0。"""
        return 0 if self.draw_only else int(self.choose)

    def range_tuple(self) -> Tuple[int, int]:
        return (self.min, self.max)

    def to_cfg(self) -> dict:
        """导出回配置 dict（只写非默认键，保证既有彩种往返一致）。"""
        d: dict = {"name": self.name, "min": self.min, "max": self.max, "choose": self.choose}
        if self.repeatable:
            d["repeatable"] = True
        if self.ordered:
            d["ordered"] = True
        if self.draw is not None and int(self.draw) != int(self.choose):
            d["draw"] = int(self.draw)
        if self.draw_only:
            d["draw_only"] = True
        return d


class LotterySchema:
    """彩票号码结构的统一描述（阶段0地基）"""

    def __init__(self, lottery_name: str, zones: List[Zone],
                 plays: Optional[Dict[str, int]] = None,
                 default_play: Optional[str] = None):
        self.name = lottery_name
        self.zones = zones
        # ★ 玩法维度（快乐8 选一~选十）：{玩法名: 票面选号数}。其余彩种为空 ⇒ 无玩法概念。
        #   为什么放进 Schema 而不是建 10 个彩种：pending/账本/预算/停发台账/推送全部以
        #   「彩种」为键，10 个彩种会把注数、预算、台账、health() 联锁全部乘 10。
        self.plays: Dict[str, int] = dict(plays or {})
        self.default_play: str = default_play or (next(iter(self.plays), "") if self.plays else "")

    def play_names(self) -> Tuple[str, ...]:
        return tuple(self.plays)

    def picks_for(self, play: Optional[str] = None) -> Dict[str, int]:
        """该玩法下票面各区应选几个号：{区名: 票面数}（draw_only 区不出现）。

        非玩法彩种返回 {区名: zone.choose}，与改造前逐项相等。
        """
        p = play or self.default_play
        zs = [z for z in self.zones if not z.draw_only]
        if not self.plays:
            return {z.name: int(z.choose) for z in zs}
        if p not in self.plays:
            raise ValueError(f"{self.name} 无此玩法：{p}（可选：{'/'.join(self.plays)}）")
        if len(zs) != 1:
            # ★ 当前没有多区玩法彩种。与其猜一套"按比例分配"，不如显式失败：
            #   猜错了会静默出出号码数不对的票，而票数错了整批比较都不可比。
            raise NotImplementedError(
                f"{self.name} 的玩法 {p} 涉及 {len(zs)} 个区，"
                "多区玩法尚未定义票面分配规则")
        return {zs[0].name: int(self.plays[p])}

    def pick_count(self, play: Optional[str] = None) -> int:
        """该玩法票面总选号数（= sum(picks_for(play).values())）。"""
        return sum(self.picks_for(play).values())

    @property
    def is_lottery_type(self) -> bool:
        """乐透型：所有区都无序且不可重复（双色球/大乐透/快乐8）"""
        return all(not z.ordered and not z.repeatable for z in self.zones)

    @property
    def is_sequence_type(self) -> bool:
        """数字型：至少有区按位有序（排列/3D/七星彩）"""
        return any(z.ordered for z in self.zones)

    @property
    def total_choose(self) -> int:
        """票面总选号数（draw_only 区计 0）"""
        return sum(z.ticket_count for z in self.zones)

    @property
    def total_draw(self) -> int:
        """开奖总号数"""
        return sum(z.draw_count for z in self.zones)

    def zone_by_name(self, name: str) -> Optional[Zone]:
        for z in self.zones:
            if z.name == name:
                return z
        return None

    @property
    def red_zone(self) -> Optional[Zone]:
        return self.zone_by_name("红球")

    @property
    def blue_zone(self) -> Optional[Zone]:
        return self.zone_by_name("蓝球")

    def to_cfg(self) -> dict:
        """导出与旧 LOTTERY_CONFIG 等价的 red_range/blue_range 等键（向后兼容）

        ★ 只在字段**偏离默认**时才追加新键：既有 6 彩种的 draw 恒为 None、plays 为空，
          因此它们的返回值与改造前逐字节相同（有 golden 测试锁死）。
        """
        d = {}
        red = self.red_zone
        blue = self.blue_zone
        if red:
            d["red_range"] = red.range_tuple()
            d["red_count"] = red.choose
        if blue:
            d["blue_range"] = blue.range_tuple()
            d["blue_count"] = blue.choose
        if any(z.draw is not None or z.draw_only for z in self.zones):
            d["zones"] = [z.to_cfg() for z in self.zones]
        if self.plays:
            d["plays"] = dict(self.plays)
            d["default_play"] = self.default_play
        return d


_SCHEMA_CACHE: dict = {}


def schema_from_cfg(cfg: dict, name: str = "?") -> LotterySchema:
    """从 LOTTERY_CONFIG 的单条配置构建 Schema（容错：无 zones 时回退红/蓝）"""
    raw_zones = cfg.get("zones")
    if raw_zones:
        zones = [Zone(**z) for z in raw_zones]
    else:
        zones = []
        if "red_range" in cfg:
            zones.append(Zone("红球", cfg["red_range"][0], cfg["red_range"][1], cfg["red_count"]))
        if "blue_range" in cfg:
            zones.append(Zone("蓝球", cfg["blue_range"][0], cfg["blue_range"][1], cfg["blue_count"]))
    return LotterySchema(name, zones,
                         plays=cfg.get("plays"),
                         default_play=cfg.get("default_play"))


def get_schema(lottery_name: str) -> LotterySchema:
    """获取某彩票的号码结构 Schema（带缓存）"""
    if lottery_name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[lottery_name]
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg, lottery_name)
    _SCHEMA_CACHE[lottery_name] = schema
    return schema


def is_redblue(lottery_name: str) -> bool:
    """是否为红/蓝双区彩种（双色球/大乐透）；数字型/乐透型返回 False"""
    schema = get_schema(lottery_name)
    return bool(schema.red_zone and schema.blue_zone)


def record_zone_numbers(rec, zone: Zone) -> List[int]:
    """从开奖记录对象中按区名取出号码列表（统一从 zone_numbers 字典读，兼容 红球/蓝球 属性）"""
    zns = getattr(rec, "zone_numbers", None)
    if isinstance(zns, dict):
        return list(zns.get(zone.name, []) or [])
    # 旧式对象（无 zone_numbers 字段）回退到属性
    return list(getattr(rec, zone.name, []) or [])


# ============================================================
# 旧分区名归一化（数字型历史遗留：万位/千位/… → 第N位）
# ============================================================
# 数字型 2026 年前后曾用「中文位名」作分区名（排列5 的 万位..个位、3D/排列3 的 百位..个位），
# 之后 schema 统一为「第1位..第N位」。历史 feedback 记录里仍残留旧名，
# 若直接 `zones.get("第1位")` 会取到空列表 → 命中被 **静默算成 0**（fail-silent，见 2026-09-11 审计）。
# 这里提供统一归一化，映射按「从右往左数位」对齐：个位 = 最后一位、十位 = 倒数第二位…
#   n=5：万位→第1位 … 个位→第5位；  n=3：百位→第1位 … 个位→第3位
_DIGIT_RANK_SUFFIX = ["万位", "千位", "百位", "十位", "个位"]  # 高位→低位（列表长度即最大位数 5）


def _legacy_digit_aliases(n_zones: int) -> dict:
    """构造「旧位名 → 第N位」别名表。位数 n_zones 决定对齐方式（从右往左）。"""
    out = {}
    m = len(_DIGIT_RANK_SUFFIX)
    if n_zones > 0:
        for i, suffix in enumerate(_DIGIT_RANK_SUFFIX):
            # 个位=最后一位(第n位)、十位=第n-1位…；suffix 在列表中越靠前，位次越高。
            idx = n_zones - 1 - (m - 1 - i)  # = n_zones - m + i
            if 0 <= idx < n_zones:
                out[suffix] = f"第{idx + 1}位"
        # 七星彩早期「第一位..第七位」等价写法
        for k in range(1, min(n_zones, 7) + 1):
            out.setdefault(f"{'一二三四五六七'[k - 1]}位", f"第{k}位")
    return out


def normalize_zone_names(zones: dict, schema: Optional[LotterySchema] = None,
                         n_zones: Optional[int] = None) -> dict:
    """把历史遗留的数字型分区名归一化为现行 schema 的「第N位」。

    - 传入 schema 时按 `len(schema.zones)` 决定位数；也可直接传 n_zones。
    - 已是「第N位」「红球」「蓝球」等现行名的键原样保留。
    - 无法识别的键保持原样（交由调用方判断是否为异常）。
    """
    if not isinstance(zones, dict):
        return {}
    if schema is not None:
        n = len(schema.zones)
    elif n_zones is not None:
        n = n_zones
    else:
        n = 0
    mapping = _legacy_digit_aliases(n)
    return {mapping.get(k, k): v for k, v in zones.items()}


def zone_name_aliases(schema: LotterySchema) -> dict:
    """返回该 schema 下「旧名 → 现行名」的别名表（供校验/告警使用）。"""
    return _legacy_digit_aliases(len(schema.zones))
