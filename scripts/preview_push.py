# -*- coding: utf-8 -*-
"""推送预览（**只渲染、不发送**）：把三类图落到 logs/push_preview_v3/。

用法：
    E:/Python/python.exe scripts/preview_push.py            # 全部彩种
    E:/Python/python.exe scripts/preview_push.py 双色球      # 只跑一个彩种

为什么要它：企微图片消息**发出去就收不回**，改版期间的版式必须先本地看图。
本脚本全程不碰 `push_notify.send*`，也不读 webhook。
"""
from __future__ import annotations

import io
import os
import struct
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from data import push_image as pi          # noqa: E402
from data import push_notify as pn         # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "logs", "push_preview_v3")

LOTS = ["双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩", "七乐彩", "快乐8"]


def _dim(png: bytes):
    """从 IHDR 里读宽高（不依赖 Pillow）。"""
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def _save(name: str, png: bytes) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "wb") as f:
        f.write(png)
    w, h = _dim(png)
    print(f"  {name:44s} {w:5d}x{h:<5d} {len(png) / 1024:7.1f} KB")


def preview_lottery(lottery: str) -> None:
    from data import feedback as fb
    print(f"\n=== {lottery} ===")

    # ★ 文件编号必须与**真实推送顺序**一致（2026-11-03 起：开奖 → 中奖 → 号码）。
    #   否则预览目录里 `_2_tickets` 排在 `_3_wins` 前面，看图的直觉会与手机收到的相反。
    # ---- ① 今晚开奖 ----
    draw = pn._latest_draw(lottery)
    try:
        png = pi.render_draw_image(
            draw, lottery=lottery,
            title=f"{lottery} · 开奖结果",
            subtitle=f"第 {draw.get('期号', '?')} 期 · {draw.get('日期', '')}",
            notes=["本期摘要 · 示例行一", "示例行二：开奖号码已与本地库对账"],
        )
        _save(f"{lottery}_1_draw.png", png)
    except ValueError as e:
        print(f"  [跳过开奖图] {e}")

    # ---- ② 开奖结算（拿历史中奖记录做样例）----
    recs = pn.digest_payload(lottery)
    hits = (recs or {}).get("hit_records") or []
    if not hits:
        print("  [跳过结算图] 没有中奖记录")
    else:
        wpages = [hits[i:i + pi.PAGE_WINS] for i in range(0, len(hits), pi.PAGE_WINS)][:2]
        for i, chunk in enumerate(wpages, 1):
            png = pi.render_wins_image(
                chunk, title=f"{lottery} · 开奖结算",
                subtitle=f"新增 {len(chunk)} 条中奖", page=(i, len(wpages)),
            )
            _save(f"{lottery}_2_wins_p{i}.png", png)

    # ---- ③ 本期出号（按真实分页）----
    pending = fb.load_pending(lottery)
    prod = fb.latest_production_pending(pending)
    tickets = (prod or {}).get("预测号码") or []
    if not tickets:
        print("  [跳过出号图] pending 里没有生产批")
        return
    pages = pi.paginate_tickets(tickets, group_size=5)
    n = len(pages)
    total = len(tickets)
    for i, page_groups in enumerate(pages, 1):
        flat = [t for g in page_groups for t in g["tickets"]]
        lo = sum(len(g["tickets"]) for p in pages[:i - 1] for g in p) + 1
        hi = lo + len(flat) - 1
        meta = [f"第 {prod.get('目标期号')} 期 · 目标开奖 · 共 {total} 注"] if i == 1 else []
        png = pi.render_tickets_image(
            flat, title=f"{lottery} · 本期出号",
            subtitle=f"共 {total} 注 · 第 {i}/{n} 页（第 {lo}–{hi} 注）",
            meta_lines=meta, groups=page_groups, page=(i, n),
        )
        _save(f"{lottery}_3_tickets_p{i}.png", png)


def main() -> int:
    targets = sys.argv[1:] or LOTS
    for lot in targets:
        preview_lottery(lot)
    print(f"\n预览已落盘：{os.path.abspath(OUT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
