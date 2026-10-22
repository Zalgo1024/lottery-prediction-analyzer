# market —— 旁路补充库

补现有 `lottery_data/*.csv` **拿不到**的数据。不改动任何既有模块，删掉 `market.db` 即完全回滚。

## 它补的四类数据

| 数据 | 现有 CSV | market.db | 来源 |
|---|---|---|---|
| 头奖分省分布 | ❌ 没有 | ✅ `prize_region` | 福彩官方 `content` 字段 |
| 出球顺序 | ❌ 没有 | ✅ `draw.seq_front` | 体彩 `lotteryUnsortDrawresult` / 中彩网 `seqFrontWinningNum` |
| 分省月销量 | ❌ 没有 | ✅ `region_sales` | 财政部月度附件 xlsx |
| 官方奖级注数 | 只有 1、2 等奖 | ✅ `prize`（全奖级） | cwl `prizegrades` / sporttery `prizeLevelList` |

## 表

```
lottery        彩种字典（8 个）
draw           开奖主表，PK = (lottery, issue, source)
prize          奖级明细，PK = (lottery, issue, source, level)
prize_region   ★头奖分省，PK = (lottery, issue, source, region)
region_sales   分省月销量，PK = (ym, region, agency)
reconcile      多源对账差异
sync_log       同步审计
```

视图：`v_draw`（按源优先级取一行）、`v_region_prize`（分省头奖汇总）、`v_draw_gaps`（缺期）。

## 用法

### 日常维护：一条命令（推荐）

```bash
python -m market.update            # 增量：只抓各源最近一页，约 1~2 分钟
python -m market.update --full     # 全量：翻到底，重建完整历史（10~20 分钟）
python -m market.update --only csv # 只跟本地 CSV，约 2 秒
```

它做四件事：按 `csv → cwl → sporttery → zhcw` 顺序跑、**单源失败不中断**、
写 `sync_log` 的 `update:all` 汇总、通知数据浏览器重建缓存。

**为什么默认是增量**：官方源都是「最新在前」，第 1 页就够覆盖最近的期
（福彩官方一页 500 期、体彩官方一页 100 期）。全量翻页在晚间高峰实测能跑到
20 分钟以上（体彩单彩种 78 页 × 0.5s sleep + 官方限流），而日常只需要补最新几期。
增量同样是 `INSERT OR REPLACE`，不会写错，只是少抓。

**自动跑**：主看板调度器每天 **23:10** 自动执行 —— **周日全量、其余日子增量**
（等 22:05 那批流水线把 CSV 写完再抓）。23:10 没开机也没关系 —— 当晚或次日
启动时会补跑，不会顺延一整天。两个入口共用一把 `.update.lock`，定时与手点不会
同时写库；上次进程被杀留下的陈旧锁（>40 分钟）会被自动接管。

单次还有 25 分钟总时限（`DEADLINE_SEC`）：超时就不启动剩余源，标为 `skipped`
（不算失败），保证最晚 23:35 收工、赶得上 23:40 的体检日报。

失败会进体检日报（`web/health_digest.py` 的「开奖数据库」段）：补库失败、
或超过 36 小时没更新（连续两个 23:10 档都没成功），都会作为问题推送出来。
正在跑的时候不会误报停滞。

页面上也有「更新数据」按钮（主看板 `/market`），异步执行 + 轮询进度。

### 分步手动跑（排查用）

```bash
# 建库 + 导入现有 CSV（只读，source=csv500）
python -m market.build --csv

# 福彩官方四彩种（双色球/3D/七乐彩/快乐8），含分省中奖
python -m market.build --cwl

# 中彩网（双色球 2003 起全量，含出球顺序）
python -m market.build --zhcw

# 体彩官方四彩种（全历史，含出球顺序）
python -m market.build --sporttery

# 财政部 31 省月销量：自动从公告页抓附件（2008-01 起共 224 个月）
python -m market.build --mof-all
# 只补指定月份（找齐就停，不会扫完 33 页）
python -m market.build --mof-months 2026-08,2026-09
# 已有本地 xlsx 时直接导入（离线兜底）
python -m market.build --mof path/to/附件.xlsx --ym 2026-06

# 看库里有什么
python -m market.build --status
```

全程 `INSERT OR REPLACE`，可重复跑、可断点续跑。单彩种失败不影响其它彩种，失败记进 `sync_log`。

### 界面

```bash
python -m market.viewer --open     # http://127.0.0.1:5001
python -m market.viewer --rebuild  # 启动时强制重建查询缓存
```

主看板侧栏「分析工具 → 开奖数据库」是同一个界面的内嵌版（iframe），
数据浏览器没启动时那里有兜底按钮可以就地拉起。

★ **缓存会自动跟随数据**：`viewer` 起了个后台线程，每 60 秒比对一次数据指纹
（draw 行数 + `MAX(fetched_at)` + `prize_region` 行数），变了就重建 `cache.db`。
所以补库完成后不用重启、也不用点「重建缓存」，一分钟内界面自己就新了。

## 三个设计决定

**1. `source` 进主键。** 同期在 cwl / sporttery / zhcw / csv500 各存一行。冲突不覆盖，能被 `reconcile` 查出来。这是相对 CSV 最大的区别：CSV 只能二选一，选完就不知道丢的是什么。

**2. 期号原样存字符串。** 官方双色球是 `2026120`，CSV 是 `26120`，体彩是 `26119`，3D 是 `2026279`。不补零、不转 int，靠 `(issue_year, issue_seq)` 做跨源对齐：

```
2026120 (cwl)  ┐
               ├─ split_issue → (2026, 120)  ← 对账键
26120 (csv500) ┘
```

**3. 号码存 JSON，不拆列。** 快乐8 一期 20 个号，双色球 7 个，定长列表格必然为最长者留 20 列。SQLite 有 `json_each()` 可直接展开。

## 红线（防止变成"替换"）

- ❌ 不改 `lottery_data/*.csv` 一个字节（只读打开）
- ❌ 不改 `data/loader.py`、不改流水线读取路径
- ❌ 不引 ORM、不加依赖（只用标准库 `sqlite3` + 已有的 pandas）
- ❌ 不设为默认数据源、不接入现有 5000 看板
- ✅ 删 `market.db` 即回到现状，零残留

> 浏览界面（`market/viewer.py`）是**独立进程、独立端口**，只读本库，
> 与现有 Web 系统零耦合，关掉即消失。

## 查询

```python
import sqlite3
from market import queries as Q

c = sqlite3.connect("market/market.db")

Q.region_prize_board(c, "ssq")          # 分省头奖排行
Q.region_vs_sales(c, "ssq", "2026-06")  # 销量占比 vs 头奖注数占比
Q.seq_first_ball(c, "ssq", "zhcw")      # 出球顺序：首球分布
Q.seq_position_entropy(c, "ssq")        # 各出球位置的号码均值
Q.latest_regions(c, "ssq", 10)          # 最近 10 期都出在哪些省
Q.coverage(c)                           # 各源覆盖情况
```

对账：

```python
from market import reconcile
reconcile.report(c)   # 官方 vs CSV 逐字段比对
```

## 浏览界面

双击根目录 **`打开数据浏览器.bat`**，或：

```bash
python -m market.viewer --open        # http://127.0.0.1:5001/
python -m market.viewer --rebuild     # 数据更新后强制重建查询缓存
```

8 个彩种、38,252 期，支持：

- **检索**：期号 / 日期 / 号码。号码用逗号分隔表示「同时包含」，走 `json_each` 精确匹配（搜 `13` 不会命中含 `31` 的期）
- **筛选**：彩种多选、日期范围、数据源、有分省中奖、有出球顺序、销售额区间、一等奖注数区间
- **排序**：点任意列头，升降序切换；空值恒排最后
- **详情**：点任意行 → 号码 + 出球顺序 + 和值/跨度/奇偶 + 多源对比 + 分省头奖条形 + 全档位奖级（可按源切换）
- **导出**：按当前筛选条件导 CSV（带 BOM，Excel 打开不乱码）

### 为什么要有 `cache.db`

多源合并需要窗口函数（每期取优先级最高的源），直接查 `market.db` 时带彩种筛选要 **15 秒**（实测双彩种 12 秒），交互不可用。

`viewer.py` 因此把合并结果物化到 `market/cache.db`（单表 + 4 个索引），查询降到 **3~16 毫秒**。

- `market.db` 始终以 `mode=ro` 打开，协议层就不可能写入
- 缓存按 `draw` 行数 + 最新 `fetched_at` + `prize_region` 行数判定过期，启动时自动重建
- 改过合并逻辑要 bump `CACHE_VER`，否则会命中旧缓存
- `cache.db` 可随时删除，下次启动自动重建

### 一处必须知道的坑

福彩官方源**没有出球顺序**（只有中彩网和体彩有），所以显示时必须跨源合并。
同理金额也可能只在某个源里有。合并规则：

```
号码/日期  → 取优先级最高的源那一行（多源对账已证明零冲突）
出球顺序   → COALESCE(主行, 任一源非空)
销售额/奖池 → COALESCE(主行, 任一源非空)
```

只取主行会让「有数据却显示—」——这个坑实际踩过一次。
