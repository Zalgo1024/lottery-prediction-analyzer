# P-1b：拥挤度闸门「数符号 ≥60%」口径修正

> 配套代码：`ev/popularity.py::fit_popularity`（CV 裁决段）、`ev/popularity_proxy.py::go_evidence`、
> `ev/arena/crowd.py`（原因文案）、`tests/test_popularity_proxy.py::test_go_verdict_driven_by_significance_not_sign_count`。
> 诊断脚本：`scripts/oneoff/_p1b_gate_criterion.py`（复刻 production CV，保留逐期偏差做显著性检验）。

## 0. 背景

P-1（大乐透拥挤度重建）收尾时，发现一个比「大乐透 No-Go」本身更值得修的问题：
production 闸门的「Go / No-Go」判定用的是一句**纯数值符号计数**：

```python
cv_better = sum(1 for a, b in zip(cv_full, cv_null) if a < b - 1e-9)
go = (lrt_p < 0.01) and cv_better >= ceil(len(cv_full) * 0.6)
```

只要某折 full 的平均泊松偏差比 null 低 `1e-9` 就算「该折更优」，完全不检验这个差异
是否显著。在 φ≈8~10 的过度离散数据上，少数离群期（如一等奖注数 y=106）会把平均偏差
压低一点点，于是「符号更优」被虚高——即便该折的改进在统计上纯属噪声。

## 1. 修正方案

把「符号计数」换成**逐折显著性检验**，口径与旧准则一一对应（仍是「≥60% 折好」，
但「好」的定义从「低 1e-9」升级为「显著低」）：

- 每折保留**逐期**泊松偏差差 `d_i = dev_null_i − dev_full_i`（>0 表示 full 更好）。
- 逐折做**单侧 Wilcoxon 符号秩检验**（H0: 中位数=0，H1: >0）；`p < 0.05` 才算该折显著更优。
  Wilcoxon 只看符号与秩、不放大离群幅度，对过度离散鲁棒——正好避开 P-1 发现的
  「泊松偏差被离群期支配」陷阱。
- `cv_sig_folds` = 显著折数；`go` 改用 `cv_sig_folds >= ceil(折数*0.6)`。
- 另附**全局池化随机化检验**（各折逐期 d_i 合并，置换符号得单一 p）作为补充证据。
- 旧 `cv_better_folds`（纯符号）保留在报告里**仅供对账**，不再进入决策。

## 2. 复跑结果（双色球 / 大乐透，全量样本）

| 口径 | 双色球 (n=3506) | 大乐透 (n=2932) |
|---|---|---|
| LRT p | 3.0e-198 | 1.7e-68 |
| 旧（符号计数）cv_better | 5/5 → Go | 2/5 → No-Go |
| **新（Wilcoxon 显著）cv_sig** | **4/5 → Go** | **1/5 → No-Go** |
| 全局池化随机化 p | **0.017** | **0.75** |
| 逐折 Wilcoxon p | [.0095, .007, .111, .0002, .0013] | [.138, 0.0, .138, .88, .92] |

结论：**两个彩种的 verdict 都与旧口径一致——双色球 Go、大乐透 No-Go。**
即本次修正**不改变任何生产行为**，只把「双色球为什么 Go」从「5/5 折数值更低」
（理由脆弱）升级为「4/5 折逐期偏差显著更低、全局随机化 p=0.017」（理由站得住）。

## 3. 对上一轮一处说法的更正

上一轮收尾报告里我写了「双色球『数符号 ≥60%』口径虚高（仅 2 折显著优）」。
这句话**不准确**，在此更正：那个「2/5 显著」来自 P-1 里一个**方向写反的配对 t 检验**
（把上尾误当显著优），并非干净分析。用正确的单侧 Wilcoxon 重做，双色球是 **4/5 显著**。
所以：

- 双色球的 Go 不是被虚高判据「误判」出来的——它在本项目的显著性口径下**确实达标**；
- 真正的问题是「旧判据给的理由不对」，而不是「结论错了」。修正让理由与结论一致。

（附带：配对 t 检验因过度离散放大标准误，只给出 2/5 显著，偏保守；NB2 偏差复核因
θ 估计写得粗糙只给 1/5，不足以采信。稳健结论落在 Wilcoxon + 全局随机化上。）

## 4. 行为影响与变更清单

- `go_or_none("双色球")` 仍为 `True`，`go_or_none("大乐透")` 仍为 `False`：
  **生产出号路径不变**，双色球低拥挤臂照常工作，大乐透仍显式降级。
- `ev/popularity.py`：`fit_popularity` 的 CV 段改为保留逐期偏差、算 cv_sig_folds /
  cv_fold_wilcoxon_p / cv_global_perm_p，go 改由显著性驱动。
- `ev/popularity_proxy.py::go_evidence`：新增透出 cv_sig_folds / cv_fold_wilcoxon_p / cv_global_perm_p。
- `ev/arena/crowd.py`：大乐透 No-Go 原因文案优先用「显著折数」，旧缓存缺字段时退回符号计数。
- `tests/test_popularity_proxy.py`：新增哨兵 `test_go_verdict_driven_by_significance_not_sign_count`，
  锁死 go 由 cv_sig_folds 驱动、双色球达标 / 大乐透未达标、全局随机化同向。
- 缓存 `cache/popularity_model_双色球.json` / `大乐透.json` 已按新口径重算落盘。

## 5. 仍待厘清（不在本任务范围）

- 逐折 Wilcoxon 折 3 对双色球 p=0.111（边际），若日后数据增长该折转显著，cv_sig 可能
  变成 5/5；反之若某折退化为噪声，也可能掉到 3/5 临界。判据本身对这点鲁棒（门槛 60%）。
- 全局随机化 p 用了固定种子（20261025）与 B=4000 置换；如需更紧的 p 可加大 B，但对
  当前结论（双显著 / 大不显著）无影响。
- 若未来要更彻底，可把逐折 Wilcoxon 升级为「以 NB2 偏差为输入的似然比折内检验」，
  但需要先写好 θ 的稳健估计（本任务未做）。
