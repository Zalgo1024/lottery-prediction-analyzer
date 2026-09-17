"""
阶段2 数字型接入冒烟测试：
对 排列5/福彩3D/排列3/七星彩 做 加载 + 三策略预测，捕获任何崩溃。
"""
import sys
import traceback
sys.path.insert(0, r"E:\707")

from data.loader import load_lottery
from pipeline.statistics import frequency_analysis, missing_value_analysis, summary_stats
from prediction.engine import predict

NEW = ["排列5", "福彩3D", "排列3", "七星彩"]

for lot in NEW:
    print("=" * 60)
    print(f"彩种：{lot}")
    try:
        data = load_lottery(lot)
        print(f"  加载记录数：{data.total_records}")
        if not data.records:
            print("  ⚠️ 无记录，跳过")
            continue
        # 统计
        freqs = frequency_analysis(data)
        missing = missing_value_analysis(data)
        summ = summary_stats(data)
        print(f"  分区：{list(freqs.keys())}")
        print(f"  摘要分区：{list(summ['zones'].keys())}")

        # 预测三种模式 + fresh（冒烟试算：不落 pending，避免污染生产预测）
        for mode in ["fresh", "high_freq", "missing", "balanced"]:
            res = predict(lot, groups=3, mode=mode, record_pending=False)
            ps = res.get("预测号码", [])
            print(f"  [{mode}] 输出组数={len(ps)} "
                  f"目标期号={res.get('目标期号')} "
                  f"历史命中率={res.get('历史命中率(近50期)')}")
            if ps:
                first = ps[0]
                print(f"        示例1 号码={first.get('号码')} "
                      f"策略={first.get('策略')} 置信度={first.get('置信度')}")
    except Exception as e:
        print(f"  ❌ 崩溃：{e}")
        traceback.print_exc()
        print("  (该彩种冒烟失败)")
print("=" * 60)
print("冒烟测试结束")
