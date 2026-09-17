"""
方向B 测试：反馈闭环对新彩种（数字型）的兼容 + 双色球零回归。

测试策略：
1. 纯函数层：_hit_detail / _classify_prize_generic 正确性（新彩种 vs 双色球）
2. 文件集成层：把 feedback.FEEDBACK_DIR 重定向到临时目录（隔离），注入 pending
   → evaluate → 断言反馈记录字段正确（新彩种含分区命中/总命中/中奖等级，
   无红球命中 KeyError）。全程不碰真实 training/feedback 数据。
3. 回归层：get_feedback_summary 对双色球不崩且保留旧字段（读真实数据，不写）。
"""
import sys
import tempfile
import traceback
from pathlib import Path

# 允许从项目根目录导入 config / data / prediction 等模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOTTERY_CONFIG
from data.loader import load_lottery
from data.schema import schema_from_cfg
import data.feedback as fb
from data.feedback import (
    _hit_detail,
    _classify_prize_generic,
    _classify_prize,
    evaluate_pending_predictions,
    get_feedback_summary,
)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        FAILED.append(name)


def test_hit_detail_unit():
    print("\n[1] 纯函数层 _hit_detail / 奖级")
    # 新彩种：排列5
    data = load_lottery("排列5")
    actual = data.records[-1]
    schema = schema_from_cfg(LOTTERY_CONFIG["排列5"], "排列5")
    ps = {"号码": actual.zone_numbers, "策略": "高频策略"}
    zh, th, tc, is_rb = _hit_detail("排列5", ps, actual, schema)
    check("排列5 is_redblue=False", is_rb is False)
    check("排列5 total_choose=5", tc == 5, f"got {tc}")
    check("排列5 预测=实际 全中", th == tc, f"th={th} tc={tc}")
    # 2026-08-31 修订：数字型官方无"一等"概念，全中=直选（_group_winning）
    lv_ok = fb._group_winning(
        "排列5", fb._digits_from_zones(actual.zone_numbers),
        fb._digits_from_zones(actual.zone_numbers), "")[0]
    check("排列5 全中 → 直选", lv_ok == "直选", f"got {lv_ok}")

    # 部分命中：预测号全错位一位
    wrong = {k: [(v[0] + 1) % 10] for k, v in actual.zone_numbers.items()}
    ps2 = {"号码": wrong, "策略": "高频策略"}
    zh2, th2, tc2, _ = _hit_detail("排列5", ps2, actual, schema)
    check("排列5 错位后命中数<总数", th2 < tc2, f"th2={th2}")
    lv_bad = fb._group_winning(
        "排列5", fb._digits_from_zones(wrong),
        fb._digits_from_zones(actual.zone_numbers), "")[0]
    check("排列5 错位 → 未中", lv_bad is None, f"got {lv_bad}")

    # 双色球（无序乐透型，集合交集）
    d2 = load_lottery("双色球")
    a2 = d2.records[-1]
    s2 = schema_from_cfg(LOTTERY_CONFIG["双色球"], "双色球")
    ps_rb = {"号码": a2.zone_numbers, "红球": a2.红球, "蓝球": a2.蓝球}
    zh3, th3, tc3, is_rb3 = _hit_detail("双色球", ps_rb, a2, s2)
    check("双色球 is_redblue=True", is_rb3 is True)
    check("双色球 预测=实际 红6蓝1", zh3.get("红球") == 6 and zh3.get("蓝球") == 1,
          f"zh={zh3}")
    check("双色球 _classify_prize(6,1)=一等", _classify_prize(6, 1) == "一等")


def test_evaluate_integration():
    print("\n[2] 文件集成层 evaluate（FEEDBACK_DIR 隔离到临时目录）")
    # 重定向反馈目录到临时目录，彻底隔离真实数据（避开 Windows 文件锁问题）
    orig = fb.FEEDBACK_DIR
    tmp = Path(tempfile.mkdtemp())
    fb.FEEDBACK_DIR = tmp
    try:
        lottery = "排列5"
        data = load_lottery(lottery)
        actual = data.records[-1]
        issue = actual.期号
        pred = {
            "预测日期": str(actual.开奖日期),
            "生成时间": "2026-01-01T00:00:00",
            "来源": "train",  # 绕过马后炮防御
            "目标期号": issue,
            "预测号码": [
                {"号码": actual.zone_numbers, "策略": "高频策略", "置信度": 0.5},
                {"号码": {k: [(v[0] + 1) % 10] for k, v in actual.zone_numbers.items()},
                 "策略": "遗漏值策略", "置信度": 0.4},
            ],
            "状态": "pending",
        }
        fb.save_pending(lottery, [pred])

        result = evaluate_pending_predictions(lottery)
        check("evaluate 新增反馈=2", result["new_feedback_count"] == 2,
              f"got {result['new_feedback_count']}")
        check("evaluate 仍 pending=0", result["still_pending"] == 0)

        hist = fb.load_feedback_history(lottery)
        check("历史记录数=2", len(hist) == 2, f"got {len(hist)}")
        fb0 = hist[0]
        check("新彩种反馈含 分区命中", "分区命中" in fb0, f"keys={list(fb0.keys())}")
        check("新彩种反馈含 总命中", "总命中" in fb0)
        check("新彩种反馈含 总选择", "总选择" in fb0)
        check("新彩种反馈含 中奖等级", "中奖等级" in fb0)
        check("新彩种反馈无 红球命中 键", "红球命中" not in fb0)
        check("预测=实际 → 直选", fb0["中奖等级"] == "直选", f"got {fb0['中奖等级']}")
        check("预测=实际 → 总命中=5", fb0["总命中"] == 5, f"got {fb0['总命中']}")

        # 权重更新
        w = result["updated_weights"]
        check("权重含 高频策略", "高频策略" in w)
        check("权重含 遗漏值策略", "遗漏值策略" in w)
        check("权重之和≈1", abs(sum(w.values()) - 1.0) < 1e-6, f"sum={sum(w.values())}")
    except Exception:
        traceback.print_exc()
        FAILED.append("evaluate 集成异常")
    finally:
        fb.FEEDBACK_DIR = orig  # 恢复真实目录，临时目录由系统回收


def test_summary_regression():
    print("\n[3] 回归层 get_feedback_summary（双色球 不崩 + 旧字段保留）")
    try:
        s = get_feedback_summary("双色球")
        check("双色球 summary 不崩", True)
        if s.get("strategies"):
            for name, st in s["strategies"].items():
                if st.get("样本数", 0) > 0:
                    check(f"双色球策略[{name}] 保留 平均红球命中",
                          "平均红球命中" in st, f"keys={list(st.keys())}")
                    check(f"双色球策略[{name}] 保留 平均蓝球命中",
                          "平均蓝球命中" in st)
                    break
        else:
            print("  （双色球暂无历史反馈样本，跳过字段断言）")
    except Exception:
        traceback.print_exc()
        FAILED.append("summary 回归异常")


if __name__ == "__main__":
    test_hit_detail_unit()
    test_evaluate_integration()
    test_summary_regression()
    print("\n" + "=" * 50)
    if FAILED:
        print(f"[FAIL] 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    else:
        print("[OK] 全部通过：新彩种反馈闭环可用，双色球零回归")
