# -*- coding: utf-8 -*-
"""重启 Flask 后验证 /api/hits/batch-detail（真实数据冒烟）。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:5000"


def post_restart():
    req = urllib.request.Request(BASE + "/api/system/restart", method="POST",
                                 data=b"{}", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print("restart:", r.status, r.read(200).decode("utf-8", "replace"))
    except Exception as e:
        print("restart-err:", e)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    post_restart()
    time.sleep(6)  # 等看门狗拉起新进程
    st, hits = get("/api/hits?limit=5")
    print("hits:", st, "returned=", hits.get("returned"))
    rec = hits["records"][0]
    print("first:", rec["lottery"], rec["期号"], rec["批次号"], "|", rec["批次标签"])
    bid = urllib.parse.quote(str(rec["批次号"]))
    lot = urllib.parse.quote(rec["lottery"])
    st, d = get(f"/api/hits/batch-detail?lottery={lot}&batch_id={bid}")
    print("detail:", st)
    print(json.dumps({k: v for k, v in d.items() if k != "号码"},
                     ensure_ascii=False, indent=1))
    print("号码条数:", len(d.get("号码", [])))
    print("首注:", json.dumps(d["号码"][0], ensure_ascii=False))
    # 找一条 derived 历史批次（批次来源=derived）验证日级回退
    st, hits2 = get("/api/hits?limit=200&sort_order=asc")
    drv = next((r for r in hits2["records"] if r["批次来源"] == "derived"), None)
    if drv:
        bid2 = urllib.parse.quote(str(drv["批次号"]))
        lot2 = urllib.parse.quote(drv["lottery"])
        st, d2 = get(f"/api/hits/batch-detail?lottery={lot2}&batch_id={bid2}")
        print("derived-detail:", st, d2.get("出号时间"), "精确=", d2.get("出号时间精确"),
              "注数=", d2.get("注数"), "中奖=", d2.get("中奖注数"))
    # 404 路径
    try:
        get("/api/hits/batch-detail?lottery=%E5%8F%8C%E8%89%B2%E7%90%83&batch_id=NOPE")
        print("404-check: FAIL (no error)")
    except urllib.error.HTTPError as e:
        print("404-check:", e.code, e.read(120).decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
