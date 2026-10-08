# -*- coding: utf-8 -*-
"""
阶段0 回归校验的「数据窗口钉死」工具
（scripts/regression_check.py 与 scripts/regression_ref.py 共用）

背景（2026-10-08 修）
--------------------
baseline `scripts/_reg_ref.json` 里存了两类东西：
  1) Xs_shape / Xr_shape —— 行数直接由 CSV 记录数决定
  2) preds —— 三种单策略在「当时最新数据」上的输出
CSV 每期追加，行数一涨，regression_check 就必然失败：
    双色球 Xs.shape [3461,103] != ref [3440,103]
**这不是代码回归，是数据在前进。** 一失败就去刷 baseline，等于把「当前行为」
固化成新基准，反而会掩盖真正的回归（这是本工具要防的事）。

方案
----
baseline 记录 `_meta.pinned_issue`：每个彩种采集时的最新期号。
校验与采集都把输入截断到该期号，使 Xs_shape 与 preds 在任意未来时点可复现。
期号按 **数值** 比较（双色球 26114 与 3001，字符串比会翻转）。
"""
import copy

META_KEY = "_meta"


def issue_num(v):
    """期号 -> 可比较值。期号是 int；非数字退化为字符串（仅兜底）。

    返回二元组，首位区分类型，避免 int 与 str 直接比较抛 TypeError。
    """
    if isinstance(v, bool):
        return (1, str(v))
    if isinstance(v, (int, float)):
        return (0, v)
    s = str(v or "")
    return (0, int(s)) if s.isdigit() else (1, s)


def truncate_records(records, pinned_issue):
    """保留 期号 <= pinned_issue 的记录。

    records 为新→旧序（最新在前），截断即丢掉比 pin 更新的期次。
    pinned_issue 为 None 时原样返回。
    """
    if pinned_issue is None:
        return list(records)
    p = issue_num(pinned_issue)
    return [r for r in records if issue_num(getattr(r, "期号", None)) <= p]


def truncate_data(data, pinned_issue):
    """返回截断后的 LotteryData 副本。

    用浅拷贝而不改原对象：load_lottery 有内存缓存，就地改会污染后续调用。
    """
    if pinned_issue is None:
        return data
    out = copy.copy(data)
    out.records = truncate_records(data.records, pinned_issue)
    return out


def read_pins(ref):
    """从 baseline 读 pinned_issue 映射；老格式（无 _meta）返回 None。"""
    meta = ref.get(META_KEY) if isinstance(ref, dict) else None
    if not isinstance(meta, dict):
        return None
    pins = meta.get("pinned_issue")
    return pins if isinstance(pins, dict) else None


def install_pinned_loader(module, pins):
    """把 module.load_lottery 换成「返回钉死窗口」的版本，返回原函数。

    prediction/engine.py 顶层是 `from data.loader import load_lottery`，
    predict() 内部直接用这个名字 —— 所以必须 patch **目标模块自己的属性**。
    去 patch data.loader.load_lottery 对已导入的名字无效（这是踩过的坑）。
    """
    if not pins:
        return None
    orig = module.load_lottery

    def _pinned(name):
        return truncate_data(orig(name), pins.get(name))

    module.load_lottery = _pinned
    return orig


def solve_pin(full_records, baseline_rows, window=50, max_probe=400):
    """从「当前全量数据 + baseline 记录的行数」反解出当时的最新期号。

    用于把 8-14 生成的老 baseline 迁移到带 pin 的新格式（preds 一字不动）。
    先用解析式猜 index，再实际构建特征验证；猜错就退化为线性搜索。
    找不到返回 None。
    """
    n = len(full_records)
    guess = n - window - baseline_rows
    if 0 <= guess < n:
        cand = full_records[guess]
        if len(truncate_records(full_records, getattr(cand, "期号", None))) - window == baseline_rows:
            return getattr(cand, "期号", None)
    for i in range(0, min(max_probe, n)):
        cand = full_records[i]
        pin = getattr(cand, "期号", None)
        if len(truncate_records(full_records, pin)) - window == baseline_rows:
            return pin
    return None
