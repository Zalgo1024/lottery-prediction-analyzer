# CI 环境依赖隔离_结论

**一句话**：CI 自有历史 17/17 全红，逐条查过 traceback 后确认 40 个失败（36 FAILED + 4 ERROR）**没有一条是代码缺陷**，全部是环境依赖；用「三个标记 + conftest 守卫」隔离后，模拟 CI 端到端验证 **288 passed / 40 skipped / 0 failed**，本机全环境仍全量照跑。

## 1. 问题形态

`gh run list` 显示 CI 自 2026-10-21 建立以来每一次运行都是 failure。失败分两类形态：

- **FAILED（36 例）**：用例跑起来了，但断言/异常挂在环境缺失上；
- **ERROR（4 例）**：`tests/test_profit_robustness.py` 模块级 fixture 就挂（`T.outer_tilt` 无 market.db 时返回 None，`.source`  AttributeError），整模块 4 例连带报错。

危险在于形态的迷惑性：`AssertionError: 登记假设数 0 < 25`、`assert 'Go门槛' == '训练样本量'`、`KeyError: '一等'` 这些看起来都像**真 bug**。逐个挖 traceback 后确认全部是下游环境缺失的投影。

## 2. 根因分类（40 项台账）

| # | 根因 | 项数 | 涉及文件（用例数） | CI 报错形态 |
|---|------|------|--------------------|--------------|
| ① | `market/market.db` 不入库（旁路补偿库，删即回滚） | 14 | test_market_backtest(11)、test_arena_payout(3) | `RuntimeError: 无法以只读方式打开 market.db`；`PA.preload` 载入 0 期 → 断言 0==1 |
| ② | `training/hypotheses` 登记册是发现流水线生成物，克隆里没有 | 2 | test_always_valid(1)、test_gap_hypothesis_registry(1) | `登记假设数 0 < 25`；`真登记册变成 0 条` |
| ③ | statsmodels 是不在 requirements 的可选科学栈（流行度 GLM / 销量模型 / walk-forward β），CI 没装 | 19 | test_arena_arms(3)、test_arena_crowd(4)、test_popularity_proxy(2)、test_lowcrowd_select(1)、test_payout_eff(2)、test_trials(1)、test_profit_beta_mode(1)、test_threshold(2)、test_gap_hygiene(1)、test_sales_model(1) | `No module named 'statsmodels'`； popularity 模型「建过模但未达 Go 门槛：GLM 拟合失败」→ 下游 NotImplementedError / 断言文案不符 |
| ①+③ | 两者都要（robustness 夹具 + GLM） | 4 | test_profit_robustness(4，整模块) | fixture `'NoneType' object has no attribute 'source'` |
| ④ | 中文字体只有 Windows 有（matplotlib `_FONTS` 前三项），Linux 回落 DejaVu 缺字形 | 2 | test_push_notify(2) | 渲染时大量 `Glyph ... missing` 警告，`assertFalse([...])` 红 |

**核实过的关键点**（防「把真 bug 标成跳过」）：

- `test_wf_params_no_lookahead` 的 CI 失败是 `assert (False)` 形态，酷似前视缺陷；traceback 显示 `params is None`，captured log 明写 `walk-forward β 拟合失败: No module named 'statsmodels'` —— 是 statsmodels 缺失，不是训练集混入未来。
- `test_profit_robustness` 的 4 个 ERROR 不是 statsmodels 收集期错误，是 `fx` 夹具里 `T.outer_tilt` 在无 market.db 时返回 None。
- `test_threshold` 两例虽 monkeypatch 了 `_load_history`，但 μ 估计走 popularity/GLM 链，环境不全即降级为「门槛不可估」——测试语义就是「全环境下必须给出诚实边界」。

## 3. 机制：三个标记 + 一个守卫

**pytest.ini 登记三个标记**（`--strict-markers` 下未登记即报错，这是第一道闸）：

- `realdata`：需要本地研究数据或生成物（market.db、training/hypotheses 登记册）；
- `statsmodels`：需要可选科学栈；
- `winfont`：需要 Windows 中文字体。

**conftest.py 新增 `pytest_collection_modifyitems` 守卫**：收集后逐项检查，标记用例在组件缺失时**自动加 skip**，理由逐项点名缺什么（如 `realdata 环境不满足：缺少 market/market.db、training/hypotheses 登记册（发现流水线生成物）（CI/全新克隆/无数据环境自动跳过，本机全量照跑）`）。三条设计约束：

1. **未标记用例永不受影响**——守卫只碰带标记的 item（牙齿测试有钉）；
2. **探测与事实自洽**——`HAS_MARKET_DB` 等常量在 import 时按 repo 相对路径实测，牙齿测试逐项比对文件系统/导入系统真实状态；
3. **本机全环境照跑**——三个组件都在时一个都不跳（本地全量 1,726 例的前提）。

CI 工作流**无需改动**：跳过不计失败，`pytest -q` 自然转绿；且全新克隆（任何人、任何机器）同样受益，不只是 CI。

## 4. 顺带修复：market_features 硬编码绝对路径

`ev/market_features.py` 原来硬编码 `DB_PATH = "E:/707/market/market.db"`——换一台机器就静默指向不存在的文件，报错信息还挂着本机路径。已改为与 `data/prize_amounts._db_path` / `market/build.py` 一致的 repo 相对解析（本机解析结果逐字相同，零行为变化）。这是本次分类时暴露的同类环境耦合：CI 日志里 22 条 `RuntimeError` 的路径字段全是 `E:/707/...`，正是它的指纹。

## 5. 验证

| 验证 | 结果 |
|------|------|
| 模拟 CI 端到端（临时插件强制四组件缺失，跑 16 个受影响文件） | **288 passed, 40 skipped, 0 failed**——跳过的正是 40 项标记用例，零附带损伤 |
| 守卫牙齿测试 `tests/test_env_guard.py`（9 例） | 全过：标记登记、标记确有使用（防真空守卫）、探测自洽、缺什么跳什么、未标记不跳、全环境不跳、理由点名组件 |
| 变异验证（钩子条件取反） | 6 例红——守卫的牙是真牙 |
| 本机全量回归 | 1,726 passed, 0 failed（40 项标记用例在本机全部实际执行，未退化成本地跳过） |
| CI 实跑（推送后） | 见当次 run；失败集合应为空 |

## 6. 纪律与边界

- **新用例归类规则**：按同样根因打标；`realdata` 只给「本地数据/生成物确实不可复现」的用例。**不得把"懒得造数据"的用例标成 realdata**——那是用跳过掩盖可构造的测试，比红更糟。
- **标记是显式契约**：三个标记的语义写在 pytest.ini 的 docstring 里，改动需同步牙齿测试。
- **已知边界**：① statsmodels 未进 requirements.lock.txt——项目语义上它是可选栈（`ev/popularity.py` / `ev/sales_model.py` 的 import 都在函数体内、失败可被降级路径接住），本次不改依赖清单，只用标记隔离；若未来要让它进 CI，方向是入 lock + 撤掉相关 statsmodels 标记，属独立议题。② `test_env_guard.py` 的「探测自洽」测试在**被强制伪造的环境**下会红——那是设计使然（牙齿就该报告谎言），不是缺陷。
