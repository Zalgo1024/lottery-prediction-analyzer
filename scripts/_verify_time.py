# -*- coding: utf-8 -*-
"""重启后验证：时间筛选 + 批次详情弹窗数据。"""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5000"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    req = urllib.request.Request(BASE + "/api/system/restart", method="POST", data=b"{}")
    urllib.request.urlopen(req, timeout=15).read()
    time.sleep(6)

    # 本月（2026-09）
    d = get("/api/hits?date_from=2026-09&date_to=2026-09&limit=0")
    print("9月:", d["total"], "/ universe", d["universe"], "bounds", d["date_from"], d["date_to"])
    # 8 月
    d2 = get("/api/hits?date_from=2026-08&date_to=2026-08&limit=0")
    print("8月:", d2["total"], {r["lottery"] for r in d2["records"]})
    # 近 7 天（由前端算，这里模拟 09-08..09-14）
    d3 = get("/api/hits?date_from=2026-09-08&date_to=2026-09-14&limit=0")
    print("近7天:", d3["total"])

    # 批次详情仍正常（取一条记录的批次）
    hits = get("/api/hits?limit=1")
    r0 = hits["records"][0]
    bid = urllib.parse.quote(str(r0["批次号"]))
    lot = urllib.parse.quote(r0["lottery"])
    det = get(f"/api/hits/batch-detail?lottery={lot}&batch_id={bid}")
    print("detail:", det["批次号"], "注数", det["注数"],
          "首注字段", sorted(det["号码"][0].keys()))
    # 弹窗渲染需要号码字段结构：乐透型应有 红球/蓝球 或 分区字典
    print("首注号码:", json.dumps(det["号码"][0]["号码"], ensure_ascii=False)[:120])


if __name__ == "__main__":
    main()
