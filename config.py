"""
全局配置管理
所有可变参数集中在此文件，修改配置不碰业务逻辑
"""

from pathlib import Path

# ===== 路径配置 =====
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
TRAINING_DIR = BASE_DIR / "training"
LOGS_DIR = BASE_DIR / "logs"
CACHE_DIR = BASE_DIR / "cache"

# 专用数据目录：所有历史开奖 CSV 集中存放，避免散落项目根目录
# 注意：不要与代码模块目录 `data/`（DATA_DIR）混淆
LOTTERY_DATA_DIR = BASE_DIR / "lottery_data"

# CSV 文件路径（原始数据，位于 LOTTERY_DATA_DIR）
SSQ_CSV = LOTTERY_DATA_DIR / "双色球历史数据.csv"
DLT_CSV = LOTTERY_DATA_DIR / "大乐透历史数据.csv"

# 清洗后输出路径（同名 *_cleaned.csv 写入同目录）
SSQ_CLEANED = SSQ_CSV.parent / "双色球历史数据_cleaned.csv"
DLT_CLEANED = DLT_CSV.parent / "大乐透历史数据_cleaned.csv"

# 优先使用的文件路径（清洗后数据不存在时回退到原始文件）
CLEANED_FILE_PATHS = {
    "双色球": SSQ_CLEANED,
    "大乐透": DLT_CLEANED,
}

# ===== 彩票规则 =====
LOTTERY_CONFIG = {
    "双色球": {
        "red_range": (1, 33),
        "red_count": 6,
        "blue_range": (1, 16),
        "blue_count": 1,
        "draw_days": [1, 3, 6],  # 0=周一…6=周日 => 二、四、日
        "name_en": "ssq",
        # 阶段0地基：泛型号码结构描述（红/蓝硬编码的单一真相来源）
        "zones": [
            {"name": "红球", "min": 1, "max": 33, "choose": 6, "repeatable": False, "ordered": False},
            {"name": "蓝球", "min": 1, "max": 16, "choose": 1, "repeatable": False, "ordered": False},
        ],
    },
    "大乐透": {
        "red_range": (1, 35),
        "red_count": 5,
        "blue_range": (1, 12),
        "blue_count": 2,
        "draw_days": [0, 2, 5],  # 一、三、六
        "name_en": "dlt",
        # 阶段0地基：泛型号码结构描述
        "zones": [
            {"name": "红球", "min": 1, "max": 35, "choose": 5, "repeatable": False, "ordered": False},
            {"name": "蓝球", "min": 1, "max": 12, "choose": 2, "repeatable": False, "ordered": False},
        ],
    },
    # ===== 阶段2 数字型（按位有序）=====
    # 每位是一个有序分区，号码范围 0-9，位间可重复。
    "排列5": {
        "name_en": "plw",
        "draw_days": [0, 1, 2, 3, 4, 5, 6],  # 每日开奖
        "zones": [
            {"name": "第1位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第2位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第3位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第4位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第5位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
        ],
        # 单注奖金模型（仅建模最高「精确全中」档，供 pipeline 通用 EV/概率使用）
        "payout": {"cost": 2, "exact_match": 100000},
    },
    "福彩3D": {
        "name_en": "sd",
        "draw_days": [0, 1, 2, 3, 4, 5, 6],
        "zones": [
            {"name": "第1位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第2位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第3位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
        ],
        # 单注奖金模型（仅建模最高「精确全中(直选)」档）
        "payout": {"cost": 2, "exact_match": 1040},
    },
    "排列3": {
        "name_en": "pls",
        "draw_days": [0, 1, 2, 3, 4, 5, 6],
        "zones": [
            {"name": "第1位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第2位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
            {"name": "第3位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True},
        ],
        # 单注奖金模型（仅建模最高「精确全中(直选)」档）
        "payout": {"cost": 2, "exact_match": 1040},
    },
    "七星彩": {
        "name_en": "qxc",
        "draw_days": [1, 4, 6],  # 周二/周五/周日（weekday(): 一=0…日=6）
        # 官方规则：前六位各从 0-9 摇出，第7位（后区）从 0-14 摇出 1 个数字
        "zones": (
            [{"name": f"第{i}位", "min": 0, "max": 9, "choose": 1, "repeatable": True, "ordered": True}
             for i in range(1, 7)]
            + [{"name": "第7位", "min": 0, "max": 14, "choose": 1, "repeatable": True, "ordered": True}]
        ),
        # 单注奖金模型（仅建模最高「精确全中(7位)」档，一等奖浮动封顶500万）
        "payout": {"cost": 2, "exact_match": 5000000},
    },
}

# ===== 节假日配置 =====
# 若自动节假日库不准确，可在此手工覆盖，格式：{年份: [(月, 日, 天数), ...]}
# 例：{2026: [(1, 28, 7)]} 表示 2026年1月28日起休市7天
HOLIDAY_OVERRIDE = {}

# ===== 异常检测阈值 =====
ANOMALY_THRESHOLDS = {
    "strict": {"z_score": 3.5, "p_value": 0.0005},
    "normal": {"z_score": 3.0, "p_value": 0.001},
    "loose": {"z_score": 2.5, "p_value": 0.01},
}
ANOMALY_DEFAULT = "normal"

# ===== 训练参数 =====
TRAIN_DEFAULTS = {
    "iterations": 500,
    "patience": 10,  # 早停容忍轮数
    "window": 50,  # 默认滚动窗口
    "window_list": [30, 50, 100],  # 多窗口对比
    "loss_alpha": 0.6,  # 损失函数：区间偏差权重
    "loss_beta": 0.4,  # 损失函数：遗漏值偏差权重
    "model": "statistical",  # statistical | logistic | random_forest | lightgbm
}

# ===== 预测参数 =====
PREDICT_DEFAULTS = {
    "groups": 5,  # 默认输出号码组数
    "max_groups": 2000,  # 单次预测组数上限（曾=10 截断大批量出号；配合引擎去重，3D 最多 1000 种）
    "confidence_w1": 0.4,  # 冷热号权重
    "confidence_w2": 0.35,  # 遗漏偏差分权重
    "confidence_w3": 0.25,  # 分布拟合度权重
    "mode": "fresh",  # fresh | trained
}

# 自动流水线（auto 命令 / 计划任务 auto_scheduled.ps1 / Flask 内置调度器）
# 每期生成的预测组数。统一从这里改，勿再散落写死（曾写死在 ps1 与 scheduler.py）。
# ⚠️ 含义提醒（改大前先想清楚用途）：
#   - 3D/排列3 号码空间仅 1000 种直选，groups>=1000 即"全包"：必中直选(1040 元)
#     但成本 2000 元，每期净亏 960 元，EV 依旧为负（-0.96/注不变）。
#   - 数据量线性增长：每天 3 数字型 × groups 条 pending，次日全部评估入历史，
#     一年约 3×365×groups 条记录（groups=50 → 5.5 万条/年）。
#   - 想临时大批量出号（几百~上千）做参考，用 `cli.py predict <彩种> --groups N`，
#     不必提高 AUTO_GROUPS。
# 2026-09-08：50 → 100（用户要求"多攒训练和战绩样本"）。
# 2026-09-13：本值改为**乐透型（双/大/七星）**口径；数字型另设 DIGITAL_GROUPS=5
#   （见下方 GROUPS_BY_LOTTERY + resolve_groups）。用户反馈"每期号码太多、眼花缭乱"。
# 代价与边界（改前必读）：
#   - 注数与中奖次数、成本同倍增长，单注 EV 不变（仍为负）。
#   - 数字型每日开奖：3 彩种 × 365 天 × 100 = 约 11 万条/年（原 5.5 万），
#     反馈 JSON 与 evaluate 耗时随之线性增长。
#   - 3D/排列3 号码空间仅 1000，groups≥1000 即"全包"必中直选但每期净亏
#     （2000 元成本换 1040 元），100 组远低于此线，安全。
AUTO_GROUPS = 100

# ===== 出号组数：按彩种分流（2026-09-13 起）=====
# 依据（4577 注实测，见 .workbuddy/memory/MEMORY.md「中奖率口径」）：
#   数字型 3 彩种占全部注数 75.6%，只贡献 9.7% 的中奖；排列5 单注概率 0.001%
#   （1359 注 0 中）。降量是"组合选择"——省钱、整体命中率反而从 2.03% 升到约 7.5%，
#   但**不是**预测能力变化（每注 EV 仍为负）。
DIGITAL_GROUPS = 5            # 福彩3D / 排列3 / 排列5
LOTTERY_STYLE_GROUPS = 100    # 双色球 / 大乐透 / 七星彩
GROUPS_BY_LOTTERY = {
    "福彩3D": DIGITAL_GROUPS, "排列3": DIGITAL_GROUPS, "排列5": DIGITAL_GROUPS,
    "双色球": LOTTERY_STYLE_GROUPS, "大乐透": LOTTERY_STYLE_GROUPS, "七星彩": LOTTERY_STYLE_GROUPS,
}

# 低重叠剪枝（ev/coverage.prune_predicted_sets）：
# ⚠️ 只改变注的**分布形状**（降低撞号 = 等效减注），**不提升任何单注中奖概率**
#    （每注 p 是组合数常数），也不改变期望奖金（期望线性性定理）。
# 效果：100 注若互相高度相似，有效覆盖可能只相当于数十注独立随机 → 裁掉冗余即省钱，
#       中奖率几乎不变。关掉即回到"生成多少登记多少"的旧行为。
PRUNE_ENABLED = True
PRUNE_MIN_PER_STRATEGY = 1    # 每策略保底注数（维持归因/权重学习的多样性）

# ===== 出号质量压缩（2026-09-14，用户要求"训练后尽可能压低出号数量"）=====
# 在 pending 入账前把「低质量 + 高重复」的注真正裁掉（区别于上面的低重叠剪枝：
# 那只剪违例、真实数据下近似 no-op）。压缩只影响登记/下注的票数——省成本，
# **不改变任何单注的中奖概率**（期望线性性，期望奖金与选号方式无关）。
# "质量"的含义 = 推荐分（策略权重学习产物，随每次训练/评估更新）——
# 排序保留的是"历史表现好的策略留下的票"，不是预测能力提升。
# 训练样本（来源=train）不压缩，评估口径不受影响；数字型注数已为 5，不压缩。
TICKET_COMPRESS_ENABLED = True
TICKET_COMPRESS_KEEP_RATIO = 0.4    # 目标注数上限 = ceil(n × 0.4)（乐透 100 → ≤40）
TICKET_COMPRESS_QUALITY = 0.8       # 质量线 = 中位数(推荐分) × 0.8，低于者直接淘汰
TICKET_COMPRESS_MIN_KEEP = 5        # 保底注数（不足时从被淘汰者按分数回填）

# A/B 前瞻对照每组注数（NetPayout WP5）。**与主流水线出号数显式解耦**：
# 降数字型注数不应削减 A/B 的功效预算（等价性检验需要固定注数）。
TRIAL_ARMS = 100


# ===== 出号数量：固定 / 动态（2026-09-14 起，用户可在看板滑轨自由调整）=====
# 用户诉求："项目内可以有地方自由调整每次出号的滑轨，分固定数量和动态数量"。
# 实现见 data/ticket_size.py，持久化在 config/ticket_size.json：
#   - 固定(fixed)：每次出号恒等于设定值；
#   - 动态(dynamic)：系统按「压缩冗余度为主 + 命中率偏离做安全阀」自适应，
#     冗余高/edge 稳定时逐步走低，命中率显著偏离理论期望时回补（硬下限 1 注）。
# 诚实边界：改注数只改成本/规模，单注概率恒定（期望线性性），EV 仍为负。
# 未配置时回落本文件的历史口径（数字型 5 / 乐透型 100）。
TICKET_SIZE_CONTROL_ENABLED = True


def resolve_groups(lottery, requested=None):
    """取"每期出号组数"。

    优先级（自高到低）：
      1. 显式 requested（`cli.py predict --groups N` 人工大批量，绕过所有配置）；
      2. data/ticket_size 的当前生效值（固定=设定值 / 动态=策略维护值）；
      3. 本文件的彩种默认（GROUPS_BY_LOTTERY / AUTO_GROUPS）。

    自动流水线各调用点统一走本函数，避免数字型与乐透型混用一个常量。
    """
    if requested is not None:
        return int(requested)
    if TICKET_SIZE_CONTROL_ENABLED:
        try:
            from data.ticket_size import effective_count
            return int(effective_count(lottery))
        except Exception:
            pass
    return GROUPS_BY_LOTTERY.get(lottery, AUTO_GROUPS)


# 前瞻 A/B 对照实验（NetPayout WP3）开关。
# 每期为双色球/大乐透各记录 100 注 A（按实得选号）+ 100 注 B（同池随机对照），
# 开奖后结算实得奖金 → training/trials/<彩种>.jsonl（training/ 已 gitignore，不入库）。
# 用途与边界（⚠️ 别期待它证明"更有效"）：
#   - 可完成的终点 = **中奖率等价性**（证明选号不改变中奖概率 / 检出实现 bug），
#     约 60 期（≈5 个月）可检出 20% 差异，229 期（≈1.5 年）可检出 10% 差异。
#   - 不可完成的终点 = 实得奖金差异（需真实中一/二等奖，双色球期望等待 ≈1136 年）。
#     实得提升的证据只来自历史反事实回测 ev/popularity.counterfactual_backtest。
# 关掉只影响数据积累，不影响任何现有功能。
TRIALS_ENABLED = True

# ===== 日志 =====
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
