-- market.db — 旁路补充库（只读现有 CSV，不改动任何既有数据）
-- 定位：补现有 CSV 拿不到的四类数据
--   1) 头奖分省分布（福彩 cwl 的 content 字段，2013 起 100% 覆盖）
--   2) 出球顺序（体彩 lotteryUnsortDrawresult / 中彩网 seqFrontWinningNum）
--   3) 分省月销量（财政部 xlsx，31 省 × 福彩/体彩）
--   4) 官方奖级注数（cwl prizegrades / sporttery prizeLevelList）
--
-- 铁律：
--   - 本库只被读取，不被现有流水线引用
--   - 不修改 lottery_data/*.csv 一个字节
--   - 删除 market.db 即完全回滚

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 1. 彩种字典
CREATE TABLE IF NOT EXISTS lottery (
  code        TEXT PRIMARY KEY,          -- ssq/dlt/pl3/pl5/qxc/sd/qlc/kl8
  name        TEXT NOT NULL,             -- 双色球/大乐透...
  family      TEXT NOT NULL,             -- cwl(福彩) / sporttery(体彩)
  issue_width INTEGER,                   -- 期号规范宽度（仅描述，不用于补零）
  zones       TEXT,                      -- JSON: [{name,min,max,count}]
  note        TEXT
);

-- ---------------------------------------------------------------- 2. 开奖主表
-- ★ source 进主键：同期在多个源各存一行，冲突不覆盖，可对账
CREATE TABLE IF NOT EXISTS draw (
  lottery     TEXT NOT NULL,
  issue       TEXT NOT NULL,             -- 原样存，不补零
  source      TEXT NOT NULL,             -- cwl/sporttery/zhcw/csv500
  issue_year  INTEGER,                   -- 尽力解析，失败为 NULL（仅用于缺期检测）
  issue_seq   INTEGER,
  draw_date   TEXT,                      -- ISO YYYY-MM-DD
  front       TEXT,                      -- JSON 数组，已排序
  back        TEXT,                      -- JSON 数组
  seq_front   TEXT,                      -- ★出球顺序（原始摇出次序）
  seq_back    TEXT,
  sales       INTEGER,                   -- 元
  pool        INTEGER,                   -- 元
  first_winners INTEGER,                 -- 头奖总注数
  first_region_raw TEXT,                 -- ★原文：「内蒙古7注，上海1注，共9注。」
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (lottery, issue, source)
);
CREATE INDEX IF NOT EXISTS idx_draw_date   ON draw (lottery, draw_date);
CREATE INDEX IF NOT EXISTS idx_draw_year   ON draw (lottery, issue_year, issue_seq);

-- ---------------------------------------------------------------- 3. 奖级明细
CREATE TABLE IF NOT EXISTS prize (
  lottery    TEXT NOT NULL,
  issue      TEXT NOT NULL,
  source     TEXT NOT NULL,
  level      INTEGER NOT NULL,           -- 等级制=1..N；快乐8 这类用出现顺序 1..39
  level_name TEXT,
  winners    INTEGER,                    -- 中奖注数
  amount     INTEGER,                    -- 金额，量纲见 amount_kind
  amount_kind TEXT,                      -- per_bet(单注) / total(总奖金)
  PRIMARY KEY (lottery, issue, source, level)
);

-- ---------------------------------------------------------------- 4. ★头奖分省
-- 这是现有 CSV 完全没有的维度，也是「哪些地方有中奖」的唯一结构化来源
CREATE TABLE IF NOT EXISTS prize_region (
  lottery  TEXT NOT NULL,
  issue    TEXT NOT NULL,
  source   TEXT NOT NULL,
  region   TEXT NOT NULL,               -- 规范省名：内蒙古/上海/安徽...
  winners  INTEGER NOT NULL,
  PRIMARY KEY (lottery, issue, source, region)
);
CREATE INDEX IF NOT EXISTS idx_pr_region ON prize_region (region, lottery);

-- ---------------------------------------------------------------- 5. 分省月销量
CREATE TABLE IF NOT EXISTS region_sales (
  ym             TEXT NOT NULL,         -- 2026-06
  region         TEXT NOT NULL,
  agency         TEXT NOT NULL,         -- 福彩/体彩
  sales_wan      REAL,                  -- 当月销量（万元）
  yoy            REAL,                  -- 同比 %
  cumulative_wan REAL,                  -- 本年累计（万元）
  source         TEXT NOT NULL DEFAULT 'mof',
  fetched_at     TEXT,
  PRIMARY KEY (ym, region, agency)
);

-- ---------------------------------------------------------------- 6. 多源对账
CREATE TABLE IF NOT EXISTS reconcile (
  lottery    TEXT, issue TEXT, field TEXT,
  src_a TEXT, val_a TEXT, src_b TEXT, val_b TEXT,
  agree      INTEGER NOT NULL,
  checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rec_diff ON reconcile (agree, lottery, issue);

-- ---------------------------------------------------------------- 7. 同步审计
CREATE TABLE IF NOT EXISTS sync_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  task      TEXT NOT NULL,              -- cwl:ssq / sporttery:dlt / mof / csv
  started_at TEXT, finished_at TEXT,
  rows      INTEGER,
  status    TEXT,                       -- ok/partial/fail
  note      TEXT
);

-- ---------------------------------------------------------------- 视图

-- 按期取「最可信」一行：官方 > 中彩网 > CSV(500)
DROP VIEW IF EXISTS v_draw;
CREATE VIEW v_draw AS
SELECT d.* FROM draw d
WHERE d.source = (
  SELECT x.source FROM draw x
  WHERE x.lottery = d.lottery AND x.issue = d.issue
  ORDER BY CASE x.source
             WHEN 'cwl' THEN 0 WHEN 'sporttery' THEN 0
             WHEN 'zhcw' THEN 1 ELSE 2 END
  LIMIT 1
);

-- 分省头奖汇总：哪个省中得多（注数 / 期数）
DROP VIEW IF EXISTS v_region_prize;
CREATE VIEW v_region_prize AS
SELECT lottery, region,
       SUM(winners)              AS 注数,
       COUNT(*)                  AS 期数,
       ROUND(AVG(winners), 3)    AS 平均每期注数
FROM prize_region
GROUP BY lottery, region;

-- 缺期检测：★必须按 issue_year 分区，否则跨年重置必误报
DROP VIEW IF EXISTS v_draw_gaps;
CREATE VIEW v_draw_gaps AS
SELECT lottery, issue_year, prev_seq, issue_seq, issue_seq - prev_seq AS gap
FROM (
  SELECT lottery, issue_year, issue_seq,
         LAG(issue_seq) OVER (PARTITION BY lottery, issue_year ORDER BY issue_seq) AS prev_seq
  FROM draw
  WHERE issue_year IS NOT NULL AND issue_seq IS NOT NULL
    AND source = (SELECT 'cwl')
  UNION ALL
  SELECT lottery, issue_year, issue_seq,
         LAG(issue_seq) OVER (PARTITION BY lottery, issue_year ORDER BY issue_seq) AS prev_seq
  FROM draw
  WHERE issue_year IS NOT NULL AND issue_seq IS NOT NULL
    AND source = (SELECT 'sporttery')
)
WHERE prev_seq IS NOT NULL AND (issue_seq - prev_seq) > 1;
