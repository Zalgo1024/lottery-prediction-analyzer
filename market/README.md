# market —— 旁路补充库

补现有 `lottery_data/*.csv` **拿不到**的数据。不改动任何既有模块，删掉 `market.db` 即完全回滚。

## 它补的四类数据

| 数据 | 现有 CSV | market.db | 来源 |
|---|---|---|---|
| 头奖分省分布 | ❌ 没有 | ✅ `prize_region` | 福彩官方 `content` 字段；体彩系走**中彩网详情接口** `firstPrizeAddress` |
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

# ★中彩网详情接口：补头奖分省（体彩系唯一来源）
#   默认补 dlt/ssq/qlc/kl8 里「还没有分省行」的期；已有 cwl 原文的期跳过（防双计）
#   还会跳过「参考一等奖注数=0」的期（本来就无人中，不可能有分省）
python -m market.build --zhcw-region
python -m market.build --zhcw-region --zhcw-region-codes dlt      # 只补大乐透
python -m market.build --zhcw-region --zhcw-region-limit 50       # 每彩种最多 50 期（试跑）
python -m market.build --zhcw-region --zhcw-region-sleep 2        # 冷却期放慢到 2 秒/次
python -m market.build --zhcw-region-reparse                      # 只重建 zhcw 源（旧入口）
python -m market.build --region-reparse-all                       # 重建全部源（cwl+zhcw）
python -m market.build --region-reparse-all --region-dry-run      # 只看会改多少，不写库
python -m market.build --zhcw-region-check                        # 自检：分省合计 vs 参考值

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

### 头奖分省从哪来，哪些彩种补得到

`prize_region` 的两条来源不同：

1. **福彩官方 `cwl`**：`content` 字段原文形如「内蒙古7注，上海1注，共9注。」，
   覆盖 `ssq/qlc/kl8`（`sd` 没有头奖概念，content 里没有分省）。
2. **中彩网详情接口**（`--zhcw-region`）：体彩官方（`sporttery`）**不提供**分省中奖分布，
   旧代码直接写 `first_region_raw=None` ⇒ 大乐透 2942 期分省长期是 0 行。
   中彩网开奖详情页用一个 avalon 模板占位 `{{j1zjqk}}`，其背后的接口
   `client_json.php?transactionType=10001002&lotteryId={id}&issue={issue}`
   返回 `firstPrizeAddress`，这是体彩系分省唯一的结构化来源。

实测各彩种能不能补（2026-11-01）：

| 彩种 | 能否补 | 说明 |
|---|---|---|
| 大乐透 dlt | ✅ 全史可补 | 原文「辽宁(基本1注)、湖北(基本3注 追加1注)…」 |
| 七乐彩 qlc | ✅ 部分 | 2007 起多数期有；无人中的期返回「共0注。」 |
| 双色球 ssq | ✅ 部分 | 2011 起有；2003~2010 老期接口返回空 |
| 快乐8 kl8 | ⚠️ 很少 | 缺口大多是「选十中十无人中」，不是数据缺失 |
| 3D/排3/排5/七星彩 | ❌ 无 | 固定奖/数字型，本就没有「头奖分省」，接口返回空串 |

**缺口里要先分「无人中」和「真缺」**。参考一等奖注数（各源 `level=1` 取**最大值**）为 0 的期
不可能有分省数据，`build_zhcw_region` 默认直接跳过、不发请求（`skip_no_winner=True`）。
这一步把四彩种 6233 期候选砍到 3395 期，而且根治了「永远重抓」：无人中的期填不进
`prize_region`，`only_missing` 每轮都会把它们当缺口再抓一遍。

| 彩种 | 缺口 | 有头奖（值得抓） | 无人中（跳） |
|---|---|---|---|
| dlt | 1929 | 1404 | 525 |
| ssq | 1457 | 1182 | 275 |
| qlc | 1562 | 798 | 764 |
| kl8 | 1285 | 11 | 1274 |

> 判据的实证依据：已填充的 5346 期里，参考>0 的占 **100%**、参考=0 的 **0 期**
> ⇒ 没有把一个真有分省的期误判成「无人中」。取 max 而非「优先某源」，是因为派奖行是子集；
> 取 max 只会让判据**多抓**、不会漏。参考值**未知**（无奖项行）时照抓，不拿缺数据当「无人中」。
> 怀疑参考值失真时用 `--zhcw-region-force` 关掉这个过滤复核。

六条纪律（都写进了 `tests/test_market_region.py`）：

- **追加不是额外的注**：`湖北(基本3注 追加1注)` = 3 注一等奖（其中 1 注是追加票）。
  若把追加也加上，26124 的分省合计会变成 7，而奖级表一等奖是 6 —— 凭空多一注。
- **「其中…」之后是子集，必须截断**：`其中复式投注为：`、`其中复式或胆拖投注为：`、
  `其中一等奖特别奖为：`（派奖期）都是前面总数的**细分**。旧解析把这一段也累加 ⇒
  双色球 176 期、七乐彩 21 期虚高（2016132 分省 51 vs 官方 37）。
- **「深圳」是独立地区**：福彩原文与「广东」**并排**（广东 2 注 + 深圳 1 注 = 3 注），
  广东口径不含深圳。旧省名表没有深圳 ⇒ 这些注整条丢失，是双色球 301 期偏少的唯一原因。
  ⚠️ 财政部《分省销量》的「广东」**含**深圳 ⇒ 算「头奖/销量」比值时深圳没有分母，应显示「—」。
- **「积分换彩N注」不是省份**：用积分兑换的票中奖，没有地域。不写进 `prize_region`（没省可归），
  但官方一等奖总数包含它 ⇒ 一致性校验要把它算成「已解释」，否则天天报假警报。
- **只在缺口期写**：`v_region_prize` / `region_prize_board` 是**跨源求和、不按 source 过滤**，
  同一期若同时有 cwl 与 zhcw 两套地区行，注数会被算两遍。所以只补「一条分省行都没有」的期。
  ★自检里 `相符` 与 `多源假相符` 要分开报：两源各 1 注相加恰好等于参考 2，数字「对」口径错。
- **不覆盖已有原文**：只填 `first_region_raw` 的空值；cwl 的福彩原文优先级更高。
- **老期原文有排版空格、缩写、全角括号、截断**：`甘 肃(基本1注)`、`浙 江(基本1注)`、`追加 1注`、
  `内蒙(基本1注)`、`上海（基本1 注）`（2014 年前后用**全角括号**）、以及末尾缺右括号的
  `浙江(基本2注 追加2注`（dlt 16098 少 2 注）。只认半角括号 ⇒ 整期解析成空 ⇒ 写不进
  `prize_region` ⇒ 每轮都被当缺口重抓（实测 38 期）。解析前先压掉所有空白、别名并入正则候选、
  全角半角括号都认、串尾单独兜底未闭合括号。
  改了解析器可**离线修数**（原文已存库，不发网络）：
  `python -m market.build --region-reparse-all [--region-dry-run]`（覆盖 cwl 与 zhcw 两个源，
  并会补建「有原文但一条行都没有」的期）。

**自检**：`python -m market.build --zhcw-region-check` —— 逐期比对分省合计 vs 参考值，
输出「相符 / 非地域已解释 / 源内矛盾 / 偏少 / 偏多 / 无参考」，并单独点名同一期多源分省行。
2026-11-02 全量自检：**8024 期，0 期偏少、0 期偏多**；1 期「源内矛盾」+1 期「非地域已解释」。
（dlt 1756 / ssq 3194 / qlc 2237 / kl8 837）

> 「**源内矛盾**」= 分省合计等于**原文自述的「共N注」**，但奖级表的一等奖注数与它对不上
> （实测 ssq 2008065：原文 11 注、奖级表 4 注）。这时**解析是忠实的，矛盾在源头**；
> 若把它当解析错去「修」，只会越改越假 —— 所以单列一类，不计入偏多/偏少。

**「查过、源头没有」≠「缺口」**：中彩网对**部分期**只返回空的 `firstPrizeAddress`
（大乐透 14048~14055 全空，而 14056 有 `上海（基本1 注）`；有些期直接返回字符串 `无`）。
这类期写不出任何行，在 `prize_region` 里与「还没查过」完全一样 —— 不记下来就会**永久重抓**。
所以每次尝试都写进 `region_probe(lottery, issue, outcome, checked_at)`，
`pending_region_issues()` 用它把「已探过」排除掉。`--status` 会把两类分开报：
「源头无分省 N 期」/「有数据 N 期」。源头后来补了数据时用 `--zhcw-region-force` 重抓。

> ★判据只有一处：`pending_region_issues()`。四道过滤（已有行 / 已探过 / 无人中 / 期号写法）
> 全在那里，批处理脚本和每日钩子都调它 —— 两处各写一份必然漂移。

> ⚠️ 校验一致性时**别拿 `prize.level=1` 直接比**：派奖期（如大乐透 21039~21051）该行名字是
> 「一等奖基本派奖」，`winners` 是**派奖子集**，不是一等奖总数（21039 该值=4，实际=7）。
> 正确口径：**各源 `level=1` 取最大值**（派奖子集只会更小，max 不会低估）。

> ⚠️ **中彩网会限流**：约 1600 次请求后开始 `RemoteDisconnected`。
> `sources.fetch_zhcw_region` 失败时**抛异常**（绝不返回空——那会把「抓不到」记成「无人中」），
> `build_zhcw_region` 连续失败 20 次即中止并跳过后续彩种。已写入的都是真数据，冷却后
> 用同样的命令续跑（`only_missing` 幂等）：`--zhcw-region-sleep 2`（间隔） / `--zhcw-region-maxfail 20`。

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

## 两个新彩种的口径（七乐彩 / 快乐8）

这两者的**开奖宽度 ≠ 票面宽度**，是所有口径分歧的根源。

| | 七乐彩 qlc | 快乐8 kl8 |
|---|---|---|
| 票面选几个 | 基本号 7 个（1~30） | 10 个（1~80），玩法选一~选十 |
| 开奖开几个 | 7 个 + **特别号 1 个** | **20 个** |
| 分区模型 | `基本号 choose=7/draw=7` + `特别号 choose=0/draw=1/draw_only=True` | `基本号 choose=10/draw=20` |
| 奖级 | 7 档（一~三等浮动，四~七等固定） | **39 档**（10 玩法 × 中 k 个） |
| 金额性质 | 浮动档按期公布 | **全部 39 档都是受规则约束档**（名义值 + 限赔/封顶） |

**「特别号」不是票面分区。** 它在 `draw` 里是第 8 个号（`front` 的后一位），
票面永远不选。`config.py` 用 `draw_only=True` + `choose=0` 表达这一点，
`Zone.draw_count` 是它的开奖宽度。任何按 `choose` 算宽度的地方都要改用 `draw_count`
（`credibility/data_quality.py` 曾按 `choose=0` 算出 `comb(30,0)=1` ⇒ 除零 500）。

**快乐8 的 39 档不是「约 50% 返奖」。** 10 个玩法的名义返奖率都落在 **57.1%~58.1%**，
是规则设计的（选十 中0 个也返 2 元就占了不少）。所以不能把 README 里
「单注长期 EV ≈ −50%」直接套到这个彩种上。

**「按名义还是按实测」是硬分叉。** 39 档全都带限赔/封顶，同一奖级不同期金额不同
（实测：`选十中十` 名义 30 万，2026 年多期实际 25 万；`选七中七` 名义 1 万，实际 8500）。
所有金额必须走 `ev/profit/amounts.grade_amount`（实测优先 → 名义兜底，且**带来源标签**），
不许在别处写死数字。

**CSV 是从 market.db 派生的。** `lottery_data/七乐彩历史数据.csv`、`快乐8历史数据.csv`
由 `scripts/export_lottery_csv.py` 生成，同目录的 `_<彩种>_export_meta.json` 记录了导出时间与源行数。
手改这两个 CSV 会在下次导出时被覆盖 —— 要改数据请改 `market.db`（或上游源），再重跑导出。

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
