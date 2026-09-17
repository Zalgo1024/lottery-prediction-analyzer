# 时间序列证伪分析框架

> **带严格证伪机制的时间序列分析框架（彩票作为示例场景）**

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB) ![Tests](https://img.shields.io/badge/pytest-374%20passed-3B6D11) ![Lotteries](https://img.shields.io/badge/%E5%BD%A9%E7%A7%8D-6-854F0B) ![License](https://img.shields.io/badge/License-AGPL--3.0-blue)

**为什么用彩票做示例场景？** 开奖数据足够纯——无协变量、无趋势、大样本、完全随机。这是检验一套时序分析方法"是否真有预测力"的最严苛测试场：任何看似有效的策略都会在这里被拆穿。本框架的价值不在于"预测中了多少"，而在于**给出可复现的证伪结论**。

核心是一套**证伪流水线**（`credibility/` + `discovery/`）：

```
假设登记 → 严格样本外回测 → 随机基线军团对照 → 效应量门控 → FDR 多重比较校正 → 只放行穿越随机的假设
```

截至当前的运行结论：**6 彩种 25 个 edge 假设全部 rejected，存活 0**——这正是系统给出的最重要发现：彩票开奖不可预测，且本框架能严格证明这一点。

在此基础上，框架围绕示例场景提供完整的数据与展示链路：

- 📊 **6 彩种数据管道**：双色球/大乐透/排列5/福彩3D/排列3/七星彩——500.com 抓取、清洗、完整性体检、外部逐期核对
- 🖥️ **Web 看板（14 页面）+ CLI 双入口**：统计/训练/预测/命中历史/期望时机/诚实看板
- 🎯 **预测-评估闭环**：多策略出号（每期 100 组）→ 开奖自动评估命中/奖级（含单注奖金）→ 策略权重自适应
- ⚖️ **诚实看板**：开奖质量监控、EV 看板（限号/撞号修正）、P/L 反馈、鲁棒性三档
- 📈 **期望时机**：Rollover-EV 分析——奖池滚存是本场景中唯一的真实数学杠杆
- 🤖 **自动流水线**：三路触发（调度/事件/自愈）全程无人值守，见下文「自动化」

> ⚠️ **免责声明**：本工具仅供学习研究使用。彩票开奖是完全随机的——本项目的可信度层（严格样本外回测 + FDR 多重比较校正 + NIST 随机性审计）已反复证明：**任何号码预测策略都不优于随机**。请理性购彩。

---

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 启动 Web 看板
#    Windows：双击 启动项目.bat（等价于下面的命令，自动清残留进程 + 开浏览器）
python run.py                # 看门狗模式，访问 http://127.0.0.1:5000

# 3. 或者只用 CLI
python cli.py --help

# 4. 跑测试套件（374 例）
pytest tests/
```

> 改代码后刷新生效：点网页右上角「重启服务」，或写 `config/restart_flag` 由看门狗热重启。

---

## 项目结构

```
├── cli.py                   # CLI 入口（所有子命令，--verbose 开 DEBUG 日志）
├── run.py                   # 看门狗启动器（清残留进程 → 拉起 Flask → 热重启监听）
├── config.py                # 全局配置（彩票规则、路径、阈值、AUTO_GROUPS=100）
│
├── data/                    # 数据层
│   ├── schema.py            # DrawRecord/LotteryData + 分区 schema + 分区名归一化
│   ├── loader.py            # CSV 加载（编码自动探测 utf-8-sig/gbk）+ 列名映射
│   ├── cleaner.py           # 数据清洗 + 清洗报告
│   ├── feedback.py          # 反馈闭环（pending / 命中历史 / 策略权重）
│   ├── prize_table.py       # 奖级 → 单注奖金权威映射（固定+浮动）
│   ├── fetcher.py           # 500.com 抓取 + 缺失列回填（奖池/注数/奖金）
│   ├── fetch_sales.py       # 数字型销售/奖级数据更新
│   ├── holiday.py           # 节假日休市（chinese_calendar）
│   └── cache.py             # 统计指标缓存
│
├── pipeline/                # 计算管道（统计 → 概率 → 期望 → 分布 → 风险 → 风控 → EV）
├── train/                   # 训练引擎（早停/多窗口/LightGBM/sklearn + 报告图表）
├── prediction/              # 预测引擎（4 策略 + 置信度评分 + 去重 + robust_tiers 三档）
│
├── credibility/             # 可信度层：OOS 回测 + 随机基线军团 + FDR + 贝叶斯校准
├── discovery/               # 假设登记册 + 重训闸门（只放行穿越随机的假设）
├── ev/                      # 结构性边建模：rulebook/crowd/rollover/kelly/bankroll
├── combo/                   # 覆盖设计（保底地板）+ 共形预测（K 注保证）
│
├── web/                     # Flask 看板
│   ├── app.py               # 应用入口（14 个蓝图路由）
│   ├── worker.py            # 持续计算 worker（队列消费 + 事件驱动 + 心跳）
│   ├── scheduler.py         # 每日 22:05 自动流水线调度
│   ├── startup_recovery.py  # 启动自愈（补抓 + 流水线新鲜度检查）
│   ├── routes/              # dashboard/stats/predict/hits/ev/honest/...
│   ├── templates/ + static/ # 页面与前端（main.js 渲染命中历史奖金徽章与「第 X 批 · 第 Y 注」标签）
│   └── utils.py             # run_auto_pipeline_core（三路流水线统一入口）
│
├── scripts/                 # 运维工具（体检/审计/修复/回归，见下文）
├── tests/                   # pytest 测试套件（374 例）
├── logs/logger.py           # 分级日志模块（运行时依赖，入 git）
│
├── lottery_data/            # ★ 历史开奖数据（不入 git，见「数据文件详解」）
├── training/                # 运行记录 + 反馈闭环数据（自动生成，不入 git）
├── config/                  # 运行时状态（worker 心跳/调度状态，不入 git）
├── cache/                   # 统计缓存（自动生成，不入 git）
└── logs/*.log               # 运行日志（不入 git）
```

---

## ★ 数据文件详解

### 1. `lottery_data/` —— 历史开奖（唯一数据源，抓取自 500.com）

| 文件 | 内容 |
|---|---|
| `双色球历史数据.csv` / `大乐透历史数据.csv` | 乐透型全历史开奖（2003/2007 至今），含奖池与一二等奖金 |
| `排列5/排列3/福彩3D/七星彩历史数据.csv` | 数字型全历史开奖（按位有序） |
| `{数字型彩种}销售奖级数据.csv` | 销售额、奖池、一等奖注数/单注奖金（EV 分析用） |
| `双色球/大乐透历史数据_cleaned.csv` | `clean` 命令产物，loader 优先使用 |

**列结构：**

- 乐透型（双色球示例，15 列）：
  `期号, 红球1..红球6, 蓝球1, 奖池奖金(元), 一等奖注数, 一等奖奖金（元）, 二等奖注数, 二等奖奖金（元）, 总投注额(元), 开奖日期`
  大乐透为红球 1-5 + 蓝球 1-2。⚠️ **开奖日期格式为 `M/D/YYYY`**（如 `10/9/2026`）
- 数字型（福彩3D 示例）：
  `期号, 开奖日期, 号码1, 号码2, 号码3`（排列5/七星彩列数相应为 5/7，日期为 `YYYY-MM-DD`）

> 注：`lottery_data/` 不入 git（数据体量大且可从 500.com 重建）。已知唯一缺口：排列5 期 04006/04007（数据源本身缺失）。

### 2. `training/feedback/` —— 预测-评估闭环（自动生成）

| 文件 | 内容 |
|---|---|
| `{彩种}_pending.json` | **待开奖预测**：目标期号、预测模式、号码组数、预测号码数组（策略/置信度/评分明细/号码） |
| `{彩种}_feedback_history.json` | **已评估命中记录**：期号、预测 vs 实际号码、红/蓝/总命中、`中奖等级`、`valid_prediction`（非法票隔离标记）、`记录类型`（预测/训练）、评估时间 |
| `{彩种}_strategy_weights.json` | **策略权重**：开奖后按真实命中自适应更新 |

### 3. 数据怎么看

**方式一：Excel / WPS 直接打开** `lottery_data/*.csv`（UTF-8 编码，双击即可）。

**方式二：pandas**

```python
import pandas as pd

# 乐透型（注意日期是 M/D/YYYY）
ssq = pd.read_csv("lottery_data/双色球历史数据.csv", encoding="utf-8-sig")
ssq["开奖日期"] = pd.to_datetime(ssq["开奖日期"], format="%m/%d/%Y")
print(ssq.tail())                     # 最新几期
print(ssq["一等奖奖金（元）"].describe())  # 头奖奖金分布

# 数字型（日期是 YYYY-MM-DD）
p5 = pd.read_csv("lottery_data/排列5历史数据.csv", encoding="utf-8-sig")

# 命中记录
import json
hist = json.load(open("training/feedback/双色球_feedback_history.json", encoding="utf-8"))
wins = [r for r in hist if r["中奖等级"] != "未中" and r.get("valid_prediction", True)]
```

**方式三：Web 看板**（启动后浏览器访问）
- `/stats` 统计概览 · `/history` 历史开奖 · `/hits` **命中历史（含单注奖金徽章与「第 X 批 · 第 Y 注」标注，可按彩种/奖级/命中位数/奖金过滤）** · `/ev` 期望时机 · `/honest` 诚实看板

**方式四：CLI**

```bash
python cli.py update          # 抓最新开奖（全部彩种）
python cli.py stats 双色球    # 频率/冷热号/遗漏
python cli.py history --days 30
```

### 4. 数据可靠性校验（都是只读，放心跑）

```bash
python scripts/health_check.py           # ★ 体检总入口：6 类检查 → logs/项目体检_*.md
python scripts/scan_data_integrity.py    # 缺期/号码越界/空值/重复期号
python scripts/verify_draws_vs_source.py # 与 500.com 逐期外部核对
python scripts/audit_records.py          # 命中记录审计（命中/奖级 vs 真实开奖）
python scripts/repair_records.py         # 记录修复（默认 dry-run，--apply 自动备份）
```

---

## 自动化

系统全程自动运转，无需人工干预：每天自动抓新开奖、评估昨日预测、更新策略权重、生成下一期预测、刷新看板。

### 三个触发路径（统一入口 `web/utils.py::run_auto_pipeline_core`，口径一致、并发去重，绝不重复跑）

| 路径 | 实现 | 机制 |
|---|---|---|
| ① 定时调度 | `web/scheduler.py` | 每个彩种**开奖日 22:05** 自动触发；如果 22:05 时应用没在运行，之后启动会**补跑当天漏掉的**（按「最近一个应触发的开奖日 22:05」判定，不顺延到明天）；21:00–23:00 间还有晚间滞后补跑兜底 |
| ② 事件驱动 | `web/worker.py` | worker 持续轮询数据目录，发现新开奖 → 立即入队评估与流水线任务（非每日开奖的乐透型为主，数字型每日开走调度即可） |
| ③ 启动自愈 | `web/startup_recovery.py` | 应用每次启动时：对比各彩种看板快照期号 vs CSV 最新期，**落后就补跑**；顺带补抓缺失数据 |

### 单次流水线做什么

```
抓取最新开奖（500.com，含奖池/奖金回填）
  → 评估 pending 预测（写入命中/奖级/策略权重）
  → 预测下一期（AUTO_GROUPS=100 组；目标期已预测则跳过，不重复落库）
  → 异常检测 → EV/期望快照
  → 写 logs/automation/status.json（看板首页卡片的数据源）
```

### Worker 任务队列（`web/worker.py`）

- **任务类型**：`auto_pipeline`（流水线）、`evaluate`（评估）、`predict_tiered`（三档预测）、`robustness_full / robustness_light`（鲁棒性）
- **串行纪律**：同一时刻只执行一个任务，前一个没跑完只做心跳续期、不认领新任务
- **心跳与孤儿回收**：执行中任务每 30 秒续一次心跳；超过 10 分钟无心跳的 running 任务视为孤儿，启动时与周期性自动标为 failed
- **状态文件**：`config/worker_state.json`（pid/心跳/当前任务，看板可读）

### 手动控制（Web API）

```bash
GET  /api/automation-status     # 各彩种最后完整流水线快照（与看板卡片同源）
GET  /api/automation-log        # 当日自动化日志
GET  /api/scheduler/status      # 调度器开关状态、各彩种下次触发时间
POST /api/scheduler/toggle      # {"enabled": true/false} 启停调度器
POST /api/scheduler/run-now     # 立即触发一轮（可指定彩种）
POST /api/system/restart        # 热重启（写 config/restart_flag，看门狗拉起新进程）
```

CLI 等价命令：`python cli.py auto [彩种]`（单彩种）/ `python cli.py auto --all`。

### 出号数量：固定 / 动态（`config/ticket_size.json`）

看板首页「出号数量」卡提供每个彩种的滑轨，控制**每期出几注**：

| 模式 | 行为 |
| --- | --- |
| **固定数量** | 每次出号恒等于设定值（数字型上限 50、乐透型上限 200，下限 1） |
| **动态数量** | 随每期评估自适应：以上批「压缩冗余淘汰占比」为主信号，冗余高且命中率贴近理论期望时逐步走低；命中率显著偏离理论值（\|z\|>2 个标准误）时回补，硬下限 1 注 |

配置经 `config.resolve_groups` 统一下发，自动流水线 / 内置调度器 / worker / 启动恢复 /
CLI / Windows 计划任务六条路径全部生效；`python cli.py predict <彩种> --groups N` 仍可临时绕过。

```bash
GET  /api/settings/ticket-size          # 各彩种当前值/上限/默认 + 动态建议
POST /api/settings/ticket-size          # {"mode":"fixed|dynamic","fixed":{...},"dynamic":{...}}
POST /api/settings/ticket-size/apply    # 动态模式：立刻按策略算一次并应用
POST /api/settings/ticket-size/reset    # 恢复默认（数字型 5 / 乐透型 100）
```

**诚实边界**：改注数只改变成本与规模，单注中奖概率恒定（期望线性性），整体 EV 仍为负；
动态下调不是"预测更准"，只是省钱与冗余控制。

### 消息推送：出号 / 数量 / 中奖记录推到手机（`config/push_notify.json`）

自动流水线每跑完一期，把「本期出号（含注数与滑轨状态）+ 上期结算 + 最新开奖号码」
组装成一条消息推送到手机。在看板首页「微信推送」卡配置渠道：

| 渠道 | 获取方式 | 额度 |
| --- | --- | --- |
| **企业微信群机器人**（推荐） | 手机装企业微信 → 建一个只有自己的群 → 群设置 → 群机器人 → 添加 → 复制 Webhook 地址 | 免费、不限条数 |
| PushPlus | pushplus.plus 微信扫码 | 免费额度大 |
| Server酱 | sct.ftqq.com | 免费 5 条/天 |

- Webhook 地址可整段粘贴，也可只粘 `?key=` 后面那串。
- token 回显一律掩码，掩码回传不会覆盖真实值；每日额度守卫（默认 10 条，跨天清零）防刷屏。
- 「看板地址」选填（如 Tailscale 内网地址 `http://100.x.x.x:5000`），推送末尾会附一条直达链接。
- **企业微信群机器人只能单向推送**，无法在群里改配置；调出号数量仍在看板完成。
- 未配置或网络异常一律降级为日志告警，绝不阻塞主流水线。

```bash
GET  /api/settings/push-notify          # 渠道/开关/掩码 token/最近推送/今日已推
POST /api/settings/push-notify          # {"provider":"wecom|pushplus|serverchan|off", ...}
POST /api/settings/push-notify/test     # 发一条测试消息（未配置返回 400）
```

**诚实边界**：推送只是通知渠道，不改变任何概率与 EV 结论。

---

## CLI 命令速查

| 命令 | 用途 |
|------|------|
| `update [彩种]` | 从 500.com 抓取最新开奖数据 |
| `auto [彩种]` | **自动流水线一条龙**（抓取→评估→权重→预测） |
| `clean 彩种` | 清洗原始 CSV → `*_cleaned.csv` |
| `stats 彩种` | 统计概览（频率/冷热号/遗漏） |
| `pipeline --all 彩种` | 5 步计算管道（概率→期望→分布→风险→风控） |
| `train 彩种 --model logistic` | 训练（statistical / logistic / random_forest） |
| `predict 彩种 --mode fresh` | 预测（fresh/high_freq/missing/balanced/trained/rule） |
| `anomaly 彩种 --threshold all` | 异常检测（strict/normal/loose 三档对比） |
| `rolling 彩种` | 滚动回测（含三策略泛化评估） |
| `history / logs` | 历史记录 / 日志摘要 |

---

## Web 页面一览

| 页面 | 路径 | 说明 |
|------|------|------|
| 数据看板 | `/` | 各彩种状态 + 最新开奖 + 预测卡片 |
| 统计概览 | `/stats` | 频率/冷热号/遗漏图表 |
| 全流程报告 | `/pipeline` | 5 步管道结果 |
| 训练工作台 / 滚动训练 | `/train` `/rolling` | 模型训练与滚动回测 |
| 号码预测 | `/predict` | 多策略出号 |
| 命中历史 | `/hits` | 中奖记录 + **单注奖金**（固定奖级官方金额，浮动奖级读当期实际值）+ **「第 X 批 · 第 Y 注」标注**（可看出这条中奖号码是当时第几次出号、该批第几注） |
| 策略战绩 | `/records` | 各策略胜率/盈亏 |
| 期望时机 | `/ev` | EV 序列 + 当期购买建议（Rollover 信号） |
| 诚实看板 | `/honest` | 开奖质量 + EV 看板 + P/L 反馈 |
| 鲁棒性 | `/robustness` | 三档判定卡片 + 趋势图 |

---

## 测试

```bash
pytest tests/          # 450 passed（含奖金表 33 例、写保护/分区归一化回归、撞号度量与中奖归因、批次契约、出号数量固定/动态、消息推送）
```

## 备份

```bash
git bundle create E:/backups/707_<日期>.bundle --all   # git 全量备份
# 数据备份：lottery_data/ + training/feedback/ 打包（repair_records --apply 也会自动备份）
```

## 文档

- [结题报告](docs/结题报告.md)（[Word](docs/结题报告.docx) / [PDF](docs/结题报告.pdf)）—— 完整的证伪流水线方法论、实得优化轨道与工程体系说明
- 配图与画图脚本：`docs/assets/`、`scripts/report_figures.py`

## 许可证

本项目基于 [AGPL-3.0](LICENSE)（GNU Affero 通用公共许可证 v3.0）发布。

- 任何基于本项目的修改版/衍生作品，**若作为网络服务提供**，也必须以 AGPL-3.0 开源其完整源代码（含修改部分）——这是 AGPL 区别于 GPL 的核心条款（第 13 条）。
- 仅限个人本地学习研究使用不受影响。
- 注意：`lottery_data/` 下的开奖数据与 `training/` 下的运行数据不属于代码，不随许可证分发。
