# 时间序列分析框架

> **把「看起来有效」和「真的有效」分开。** 一套可复现的检验框架，用 8 个彩种、数万期公开开奖数据当压力测试场。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB) ![Tests](https://img.shields.io/badge/pytest-2922%20passed-3B6D11) ![Lotteries](https://img.shields.io/badge/%E5%BD%A9%E7%A7%8D-8-854F0B) ![License](https://img.shields.io/badge/License-AGPL--3.0-blue)

## 0. 30 秒读懂

| 问题 | 回答 |
|---|---|
| **这是什么** | 一套「结论准入」框架：任何「这个特征有效 / 这条策略能赚钱」的主张，都要先过预注册、样本外、随机基线、效应量、多重校正五道关。 |
| **这不是什么** | 不是荐号工具，不是收益系统，也不对开奖过程本身下结论。 |
| **现在能说的** | 25 条预注册候选结构性边全部为「未检出」；单注长期 EV 按彩种分别读均为负；`E[达标注数] = K·p`，与选哪些票无关。 |
| **现在不能说的** | 「某特征已被证实有效」「多买就能中」「开奖的公平性已被验证」——这三类话本项目都不写。 |
| **怎么读结论** | 「未检出」仅表示已测维度在该样本量下未见异常，不构成证明；「缺席 ≠ 通过」，未测 / 未参与 / 不适用必须显式写原因。 |

四条不随版本变化的边界：

- 不声称下一期号码可被预测，也不对开奖公平性作证明或否定；测量层只给效应量、置信区间与「未检出 / 已激活」三态。
- 出号、结算、账本、推送是让「检验能被验收」的配套链路，产出的是可核验的对照样本，不构成购彩建议。
- 改注数不改变单注中奖概率（期望线性性），也不改变整体 EV 为负这一结论；真正可动的只有覆盖设计、实得优化与 Rollover 时机。
- 仅供学习研究使用，请理性购彩。

## 1. 问题：噪声会被读成信号

时序分析里最常见的失败不是算错，而是**把噪声读成信号**：试 20 个指标总有一个 p<0.05；在几千期数据上换几十种切分，总有一条回测曲线漂亮。这不是水平问题，是多重比较的数学必然。

所以重心不在「下一期开什么」，而在**凭什么相信某个结论**（`credibility/` + `discovery/`）：

```
预注册（先固定假设与终点，不许事后挑）
  → 严格样本外回测（滚动窗口，评估段从未参与优化）
  → 随机基线军团（同池随机多次，与策略同尺度对照）
  → 效应量门控（统计显著 ≠ 经济上可用）
  → FDR 多重比较校正（BH，控制错误发现率）
```

五道全过才写进结论；没过的一律记「未检出」。**数据不足、功效不够、确实不存在是三件不同的事**，不许合并成一句「没有用」。

## 2. 写进代码与测试的诚实纪律

这些不是措辞偏好，每一条都有测试锁住：

| 纪律 | 含义 |
|---|---|
| **未检出 ≠ 已证明** | 只表示已测维度在该样本量下未见异常，不构成证明；非显著不等于无效果 |
| **缺席 ≠ 通过** | 未测 / 未参与 / 不适用必须显式写原因，不静默少一行；前端「不适用」要带类型标记 |
| **分母三数并列** | 开奖期数 / 参与期数 / 未参与期数同时给出；空期也要建行进分母，不许剔除后缩分母 |
| **反事实标签** | 无逐期真实购买记录 ⇒ 所有「前 n 注」读数都标反事实；未购买期只披露、不进主判据 |
| **读数 ≠ 结论** | BH 通过不等于结论成立；多臂对照必须同屏给「可判性判决」，Δ 只由单期撑起时看集中度 |
| **单一真源** | 彩种清单、注数上限、口径文案、开奖时刻表、采集频率各只有一处；新增数字不套进维护脚本的固定句式就会永久脱管 |

## 3. 为什么拿彩票当试验场

判据硬：规则公开、样本量大、每期独立且无协变量、结果逐期可外部核验。一套检验方法在这里都跑不出稳定信号，在噪声更大的业务数据上更不会有奇效。

围绕这个试验场，项目把整条链路做到了可无人值守运行——**检验层是目的，其余是配套**：

- 📊 数据管道：抓取、清洗、完整性体检、外部逐期核对；8 个彩种完整覆盖（清单以 `config.LOTTERY_CONFIG` 为准）
- 🖥️ Web 看板 + CLI 双入口：统计 / 训练 / 出号 / 命中历史 / 期望时机 / 证据看板 / 盈利账本
- ⚖️ **测量与证据看板**（`/audit`）：开奖质量、EV、P/L 反馈、距离与实得画像、A/B 对照、中奖归因、机制缝隙四轨、策略鲁棒性；每张卡自带「在测什么 / 名词 / 口径与诚实边界」
- 🔬 **机制缝隙测量**（`ev/mechanism/`）：只给效应量、CI 与判定，不预测号码
- 📈 **期望时机**：Rollover-EV——奖池滚存是本场景中唯一与选号无关的数学杠杆
- 🤖 自动流水线：定时调度 / 事件驱动 / 启动自愈三路触发，统一入口 `web/utils.py::run_auto_pipeline_core`

## 4. 系统分层与仓库地图

```
结论层   credibility/  discovery/  ev/mechanism/  ev/prefix_*        ← 目的
证据层   ev/draw_evidence.py  ev/shadow_*  web/evidence_schedule.py
测量层   ev/payout.py  ev/gap.py  web/audit_glossary.py  →  /audit
数据层   data/  lottery_data/  market/market.db
配套层   pipeline/  train/  prediction/  web/  cli.py  scripts/     ← 让检验可验收
```

```
├── cli.py / run.py / config.py       # CLI 入口 / 看门狗启动器 / 全局配置
├── data/                            # CSV 加载清洗、分区 schema、奖级表、抓取、休市日历、反馈闭环
├── pipeline/ train/ prediction/     # 计算管道 / 训练引擎 / 出号引擎
├── credibility/ discovery/          # 可信度层（OOS 回测 + 随机基线 + FDR） / 假设登记册
├── ev/                              # 结构性边建模、实得口径 payout_eff、EV、预算闸门
│   ├── mechanism/                   # 机制缝隙四轨测量
│   ├── order_freeze / prefix_*      # 前缀口径研究（离线，不接生产）
│   ├── draw_evidence.py             # 方案 B：外部开奖证据（时间方向判据）
│   ├── shadow_bridge.py             # 四包外唯一编排层
│   └── shadow_settle.py             # 影子登记 → 结算
├── combo/                           # 覆盖设计 + 共形预测
├── web/                             # Flask 看板：app / worker / scheduler / routes / templates / static
│   └── evidence_schedule.py         # 证据采集频率的单一真源
├── scripts/                         # 运维与研究脚本（体检、审计、采集、离线报告）
├── tests/                           # pytest 套件
├── config/                          # 判据类配置（入库） + 运行时状态（不入库）
├── lottery_data/                    # 历史开奖 CSV（入库做数据备份）
└── logs/                            # 专题报告与运行日志
```

> 仓库只保留代码与判据类配置；`logs/` 下的运行日志、专题报告，以及 `config/` 下带运行时状态的文件都**不随仓库分发**（`logs/` 仅保留 `logs/logger.py`）。下文提到的 `logs/*.md` 都是本机运行产物，克隆后按对应脚本重算即可，不是仓库里可直接打开的文档。

## 5. 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
#    推荐用 lock：版本已钉死，保证你跑出来的数字和报告里的一致
pip install -r requirements.lock.txt
#    不限版本用 requirements.txt（全是 >=，可能拉到大版本，数值会漂）

# 2. 启动 Web 看板（Windows 可双击 启动项目.bat）
python run.py                # 看门狗模式，访问 http://127.0.0.1:5000

# 3. 或者只用 CLI
python cli.py --help

# 4. 跑测试套件（全量 2922 例）
pytest -q                    # 约 15–20 分钟（慢点集中在真实 predict 全流程的契约测试）
```

> 改 Python 代码后需重启服务（点看板右上角「重启服务」，或写 `config/restart_flag` 触发热重启）；改静态资源与 Jinja 模板不用重启，新增路由必须重启。配置类改动由 `load_config()` 热读。

## 6. 数据与彩种

`lottery_data/*.csv` 是历史开奖的唯一数据源。乐透型日期格式是 **`D/M/YYYY`（日在前）**，数字型是 `YYYY-MM-DD`；解析口径见 `data/schema.py::parse_draw_date`。

| 彩种 | 代码 | 结构 | 数据来源 |
|---|---|---|---|
| 双色球 / 大乐透 | ssq / dlt | 乐透型，含奖池与浮动奖 | 500.com |
| 排列5 / 排列3 / 福彩3D / 七星彩 | pl5 / pl3 / sd / qxc | 数字型，按位有序 | 500.com |
| 七乐彩 | qlc | 7 个基本号 + 1 个**特别号**（票面不含特别号） | `market/market.db` 派生 |
| 快乐8 | kl8 | 80 选 20 开出，票面按 10 种玩法选 1–10 个 | `market/market.db` 派生 |

四个容易读错的口径：

- **「每期取号数」的唯一真源是 `Zone.draw_count`**（`draw` 优先、缺省 `choose`）。七乐彩特别号 `choose=0 / draw=1`、快乐8 `choose=10 / draw=20`；用 `choose` 会让随机性审计出现 `inf` 与发散的假告警。
- **快乐8 名义返奖率约 57.5%**（规则设定，不是本仓估计；`tests/test_kl8.py` 锁住），明显高于按 50% 返奖设计的彩种 ⇒ 所有 EV 数字必须**按彩种分别读**，真源是 `ev/payout.payout_eff`，笼统写「单注长期 EV ≈ −50%」会对这个彩种偏悲观。
- 七乐彩与快乐8 的 CSV 是**派生文件**，真源 `market/market.db`；更新顺序：`python -m market.update` → `python scripts/export_lottery_csv.py`。直接手改会在下次导出时被覆盖。
- 机制缝隙 A/B 轨需要销售额与奖级注数三列，这两个彩种没有 ⇒ 报告里显式写「不适用 + 原因」，而不是悄悄少两行。

只读校验（放心跑）：

```bash
python scripts/health_check.py --no-push     # 体检总入口；不带 --no-push 会推企微
python scripts/scan_data_integrity.py        # 缺期 / 号码越界 / 空值 / 重复期号
python scripts/verify_draws_vs_source.py     # 与上游逐期外部核对
python scripts/audit_records.py              # 命中记录审计（清单由注册表推导，判级调生产同一函数）
```

## 7. 现有读数（截至 2026-11）

下表全部可用仓库里的数据与命令复现。被检验的是 **25 条具体候选关系**，而不是「彩票」这个笼统对象。

| 项目 | 读数 | 怎么读 |
|---|---|---|
| 预注册候选结构性边 | 25 条 | 覆盖奖级结构、冷热门、跨位依赖、Rollover 等 |
| 通过 BH 多重校正 | 0 条 | 不构成「存在可稳定获利的偏差」的证据 |
| 单注长期 EV | 按彩种分别读，均为负 | 多数彩种约 −50%；快乐8 约 57.5% 名义返奖率；实测金额受限赔只会更低 |
| 机制缝隙（双色球 / 大乐透等） | R 与 ū 的区间分别含 1 与 0.5 | 未检出偏离；B 轨功效偏低，判定更保守 |
| 选号对中奖率的影响 | `E[达标注数] = K·p` | 与「选哪些号」无关，只与注数 K 和覆盖结构有关 |

> 正确读法：**未检出**只表示「在现有样本与检验功效下看不到稳定偏差」，既不是「已证明不存在」，也不是「多买就能中」。真正可动的只有覆盖设计、实得优化与 Rollover 时机。

数字型的玩法口径是**显式缺口**：EV 层只建模直选，排列3 / 福彩3D 的组选3、组选6 在 `data/prize_table.py` 有奖金（结算认得），但没有概率口径所以进不了 EV。前提是生产票面为直选；改玩法前必须先扩 `grade_table`。

**受规则约束档**（七乐彩一~三等、快乐8 全部 39 档）的单注金额按期由官方公布，既不是固定奖也不是奖池浮动奖。展示层 `data/prize_table.py::prize_payout` 与判级 / 结算层共用 `ev/profit/amounts.grade_amount` 这一条取值链：当期实测优先；只有名义值时只作展示、不进金额合计；读不到任何当期数据时按「金额未知」单独计数并由界面披露，**不按 0 元计**。命中历史的「N 等奖及以上」筛选对这套按玩法命名的奖级不适用，被排除的注数会显式给出，不静默漏计。

## 8. 影子验证与方案 B（当前状态）

**动机**：看板、结算、预算台账都按「系统整批 K 注」算，而实际购买通常只取清单前几注。整批口径在 K 大时既饱和又不代表真实投入。这一支只做**口径翻译与证据资格判定**，不改生产选号 / 注数 / 预算闸门 / 看板结算。

**时间方向是最容易搞反的一环**（搞反就放进前视）：官方不公布秒级摇奖时刻，`config/draw_schedule.json` 的声明值是**开奖直播开始时刻 = 开奖事件的下界**，只有它能判「批次时间早于开奖」；外部采集到的「首次可观测时刻」是**上界**，**不能**当判据，只能**否定下界**——若它早于声明值，说明声明值偏高，该期判不合格并告警。唯一实现是 `ev/draw_evidence.derive_draw_lower_bound()`，由 `test_evidence_earlier_than_declared_kills_lower_bound` 锁死。证据必须按彩种取全部历史期、逐条与其自己那期的声明下界比对，任一矛盾 ⇒ 该彩种时刻表整体不可信、本期保守拒登；无证据时写「未发现矛盾 ≠ 已验证」。

| 部件 | 作用 |
|---|---|
| `config/draw_schedule.json` | 8 彩种开奖时刻表（判据类，入库） |
| `ev/shadow_bridge.py` | 四包外唯一编排层；开关 `config/prefix_shadow.json` 缺失即关闭 |
| `cli.py` / `web/utils.py` | 两处接线，只登记合格的生产出号冻结快照 |
| `scripts/collect_draw_evidence.py` | 外部证据采集，**只读**：8/8 彩种覆盖，不写 CSV / pending / 台账 / 预算 |
| `web/evidence_schedule.py` | 采集频率单一真源：窗口 21:05–21:45、每 2 分钟、单轮超时 110s |
| `config/prefix_shadow_rules.json` | 预注册：`评估批起始 = 2026-12-01`；缺文件绝不默认评估批 |

采集只接进 Flask 内置调度器（`web/scheduler.py::_tick`），**不装系统计划任务**（`schtasks.exe` 被本机安全策略列入黑名单，不做绕过）；代价是**只有服务在跑才采**。手动补采：`python scripts/collect_evidence_window.py --once`（或 `--window` / `--status`）。

`tests/_import_guard.py` 用两档白名单锁住边界：研究模块只准出现在编排层，编排层只准被那两处生产入口调用；连注释里出现模块名也会被拦。`conftest._TARGETS` 同步隔离落盘路径，测试不会写进真身台账。

**机制闭环已跑到结算**：2026-11-06 完成第一次真实开奖结算。此前自动流水线的结算整批失败——生产冻结快照的票面是分区 dict（`第N位` / `基本号`），而判级层 `grade_ticket()` 只认扁平数字列表，于是七星彩抛 `KeyError: 0`、数字型与七乐彩/快乐8 抛 `int('第1位')` / `int('基本号')`。修复方式是扩展判级层可接受的输入格式（不改判级语义），补 8 类彩种生产票面的回归测试，并让「结算口径」跟随 `_settle_pl` 实际走的那条路标注，不再笼统记成规则表。

**现场状态**（唯一真源：`python scripts/prefix_shadow_report.py` → `logs/prefix_shadow_status_report.md`）：

| 项 | 怎么读 |
|---|---|
| 快照分母 | 目录内文件总数；回放 / 不合格全部留在分母内并逐份写原因，不许筛掉后报数 |
| 登记 / 结算 | 台账按「阶段」分别计数，两条记录各自只追加、不回改；结算必须引用已登记的票序哈希 |
| 批次性质 | 默认探索批；`评估批起始 = 2026-12-01` 起才算独立评估批，缺规则文件绝不默认评估批 |
| 当前判决 | **未开判**，门槛 17,141 期（真源 `prefix_curve.verdict_curve()` 的 n\*_保守）；探索批只作描述，不能充当确认批 |

> 上表给的是**口径**，不是快照数字：服务每轮出号都会追加快照与登记，具体期数持续变化，请以重算报告为准。带时间戳的一次重算（2026-11-06 23:11）为 438 份快照 / 24 期登记 / 8 期结算 / 独立评估批 0 期，全部为探索批。
>
> **登记不等于通过，0 期也不等于通过。** 现在不做 BH、不写「检出」、不接生产选号。缺口在证据侧（未来真实开奖 + 独立评估批），不在统计侧。历史回放一律不具登记资格，不许拿它冒充开奖前的确认力。

## 9. 配套链路：出号、预算、推送

这些模块让检验有可核验的输入与账本，**本身不是结论**。

**出号数量**（`data/ticket_size.py` + `config/ticket_size.json`）：固定 / 动态两模式，滑轨值即最终出号注数。生效值在 `dynamic`/`fixed`，`limits` 是可拖上限，`HARD_CAP` 是绝对天花板；文件缺失会触发 `sanitize()` 兜底把模式改成 fixed ⇒ 用 `health()` 判安全，不安全就停发。当前 6 个彩种上限 10000 注、福彩3D / 排列3 各 1000 注（直选空间只有 1000）。**具体数值以配置文件为准，不在文档里复述成固定值。**
**重新出号**（单彩种按钮 / 统一按钮）会把卡片上用户此刻看到的值先落盘、再走 `resolve_groups`：此前请求只带彩种名，拖完滑块没点「保存」就直接重出会按磁盘旧值出号，成功后界面重绘又弹回旧值。落盘只写当前模式那一个字段、值相同不写盘，`groups` 仍为 `None`（注数真源与预算闸门不变），确认框会明说本次按多少注出号并保存。引擎层的单次封顶与 `HARD_CAP` 对齐，不再出现「界面能设 1w、引擎悄悄只出 2000」的第二道更小封顶。

**预算闸门**（`ev/budget_gate.py` + `config/budget_aggregate.json`）：`月度总额` 与 `单期上限` 由 `load_limit()` 热读，改配置无需重启。额度是**系统出号的假设成本上限，不是用户真实投入**（用户实际通常只取清单前几注，主口径假设 n=3）；`单期上限` 的语义是**单彩种 · 单批**，没有跨彩种或跨批聚合，跨彩种合计由月度总额约束。记账幂等键含重出批号；真出号路径用原子预占（带 TTL）杜绝并发超支；额度用尽 ⇒ 注数夹 0 ⇒ 界面直接写「本期会停发」。口径与调整留痕见 `logs/budget_period_cap_audit.md`、`logs/budget_adjust_window.md`。

**消息推送**（`data/push_notify.py`）：每个彩种一组「组头 → 开奖 → 中奖 → 号码」，组内用进程内锁 + 跨进程文件锁互斥；企业微信机器人只有 markdown / image、没有 caption，所以诚实声明画进图里。失败分「全成功 / 部分失败 / 全部失败」，不把丢条当成功。

## 10. CLI 与 Web 速查

```bash
python cli.py update                 # 抓最新开奖
python cli.py auto [彩种|--all]      # 自动流水线：抓取 → 评估 → 权重 → 出号
python cli.py stats 双色球           # 频率 / 冷热号 / 遗漏
python cli.py gap all --out PATH     # 机制缝隙四轨测量
python cli.py payout 快乐8 --nums ... --play 选九   # 实得口径表
python scripts/shadow_report.py           # 影子三臂观察点报告（只读）
python scripts/prefix_shadow_report.py    # 影子接线状态（只读，不写台账）
python scripts/prefix_real_report.py      # 真实生产票序前缀读数（离线）
python scripts/budget_monitor.py          # 预算闸门只读调额决策表
python scripts/collect_evidence_window.py --status  # 证据采集状态
```

> ⚠️ 冒烟 / 验证出号请加 `--no-record`（`predict` / `auto` 均支持），Web API 对应 `{"record_pending": false}`：只算不落盘，不覆盖生产 pending。

| 页面 | 路径 | 内容 |
|---|---|---|
| 数据看板 / 统计概览 / 全流程 | `/` `/stats` `/pipeline` | 状态、频率冷热、5 步管道 |
| 训练 / 滚动 / 出号 | `/train` `/rolling` `/predict` | 模型训练与回测、多策略出号 |
| 命中历史 / 策略战绩 | `/hits` `/records` | 单注奖金、批次与票序标注、前瞻对照 |
| 期望时机 / 盈利账本 | `/ev` `/ledger` | Rollover 信号、成本与净盈亏、三臂影子对照 |
| **测量与证据看板** / 鲁棒性 | `/audit` `/robustness` | 九块只读指标，每卡带口径与诚实边界 |

## 11. 测试、维护与备份

```bash
pytest -q              # 2922 passed（全量；含奖金表、写保护与分区归一化、批次契约、出号数量、消息推送、休市日历、机制缝隙四轨、影子接线与证据时间方向、预算并发预占、采集频率、文档维护护栏等）
pytest tests/          # 2913 passed（仅 tests/ 目录；另有 9 例在 scripts/ 下，故用 pytest -q 跑全量）
```

本机跑全量需 `PYTEST_DEBUG_TEMPROOT=E:/707_backup_p5_2200/pytest_tmp PYTHONUTF8=1`；`pytest.ini` 用 `--strict-markers` 注册 `realdata` / `statsmodels` / `winfont` 等标记，避免选错测试集却不报错。

> ⚠️ pytest 退出尾部可能带清理期的 traceback（本机删除守卫与 atexit 冲突，被吞后主进程退出码仍为 0）⇒ **判全量请看 `N passed / M failed` 那一行**。

```bash
python scripts/sync_counts.py --check   # 文档里的测试数是否与真实一致（改 README 后必跑）
python scripts/sync_lock.py --check     # 依赖锁是否与环境一致
python scripts/prune_logs.py            # 预览可清理旧日志（加 --apply 才删）
```

- `run.py` 自己不会随开机启动；本机用用户级注册表 Run 项（`scripts/install_autostart.bat`），不需要管理员。重启后确认服务真的起来。
- **单一路径出号**：只用 Flask 内置调度器，不要再挂一条独立的系统计划任务——两条路径之间没有跨进程互斥，同批可能被记成两笔钱。
- 备份：`git bundle create E:/backups/707_<日期>.bundle --all`；`lottery_data/` 与 `training/feedback/` 建议另行打包。
- README 里的计数由 `scripts/sync_counts.py` 按固定句式同步；配置类数值会变，一律以 `config/` 下的文件为真源，不在文档里复述成固定值。

## 12. 深入阅读

本机 `logs/` 下按专题留有过程与判据文档（**不随仓库分发**，见 §4 说明）：`logs/prefix_shadow_wiring.md`（接线）、`logs/prefix_shadow_b_plan.md`（方案 B 与采集频率）、`logs/prefix_shadow_status_report.md`（当前状态）、`logs/budget_period_cap_audit.md`（单期上限语义）、`logs/budget_adjust_window.md`、`logs/prefix_real_all_s5_report.md`（真实票序前缀读数）、`logs/prefix_curve_formal_ssq_K100_p360_s5_report.md`（离线判决）。

克隆仓库后可用脚本重算其中可再生的部分，例如 `python scripts/prefix_shadow_report.py` 会生成 `logs/prefix_shadow_status.json` 与 `logs/prefix_shadow_status_report.md`；历史过程记录不在版本控制内，需从本机备份取回。

## 许可证

本项目基于 [AGPL-3.0](LICENSE) 发布，仓库为公开仓库。

- 任何基于本项目的修改版 / 衍生作品，**若作为网络服务提供**，也必须以 AGPL-3.0 开源其完整源代码（含修改部分）——这是 AGPL 第 13 条区别于 GPL 的核心条款。
- 仅限个人本地学习研究使用不受影响。
- `lottery_data/` 下的开奖数据与 `training/` 下的运行数据不属于代码，不随许可证分发。
