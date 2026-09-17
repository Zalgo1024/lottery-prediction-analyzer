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
            if len(nums) != zone.choose:
                errors.append(f"{zone.name}数量应为{zone.choose}，实际{len(nums)}")
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
    - name: 区名（"红球"/"蓝球"，未来可扩展为"第1位"等）
    - min / max: 号码取值区间（含端点）
    - choose: 该区每期选取的号码个数
    - repeatable: 区内是否允许重复号码（数字型每位可 0-9 重复时为 True）
    - ordered: 该区是否按位有序（数字型为 True；乐透型为 False）
    """

    name: str
    min: int
    max: int
    choose: int
    repeatable: bool = False
    ordered: bool = False

    @property
    def size(self) -> int:
        return self.max - self.min + 1

    def range_tuple(self) -> Tuple[int, int]:
        return (self.min, self.max)


class LotterySchema:
    """彩票号码结构的统一描述（阶段0地基）"""

    def __init__(self, lottery_name: str, zones: List[Zone]):
        self.name = lottery_name
        self.zones = zones

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
        return sum(z.choose for z in self.zones)

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
        """导出与旧 LOTTERY_CONFIG 等价的 red_range/blue_range 等键（向后兼容）"""
        d = {}
        red = self.red_zone
        blue = self.blue_zone
        if red:
            d["red_range"] = red.range_tuple()
            d["red_count"] = red.choose
        if blue:
            d["blue_range"] = blue.range_tuple()
            d["blue_count"] = blue.choose
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
    return LotterySchema(name, zones)


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
