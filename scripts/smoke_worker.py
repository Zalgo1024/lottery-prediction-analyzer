# -*- coding: utf-8 -*-
"""L4 第2期 worker 端到端冒烟：真实库上认领+执行+心跳+事件基线"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from web.task_store import task_store
from web.worker import ContinuousWorker, WORKER_STATE_FILE, EVENT_STATE_FILE

# 1) 入队一个轻量鲁棒性任务（真实执行，写真实报告）
tid = task_store.create("robustness_light", {"lottery": "福彩3D", "mode": "light"})
print(f"enqueued task={tid}", flush=True)

# 2) 启动 worker（线程模式，模拟嵌入）
w = ContinuousWorker(poll_interval=10, mode="standalone", worker_id="smoke-worker")
w.start()

# 3) 等待认领+执行（light_check 秒级，给足余量；同时事件轮询会记录 6 彩种基线）
status = None
deadline = time.time() + 90
while time.time() < deadline:
    t = task_store.get(tid)
    status = t["status"]
    if status in ("done", "failed"):
        break
    time.sleep(3)

w.stop()
time.sleep(1)

print(f"task status = {status}", flush=True)
print(f"task result = {json.dumps(t.get('result'), ensure_ascii=False)}", flush=True)
print(f"task error  = {t.get('error')}", flush=True)

hb = json.loads(WORKER_STATE_FILE.read_text(encoding="utf-8"))
print(f"heartbeat: id={hb['worker_id']} last_beat={hb['last_beat']} stats={hb['stats']}", flush=True)
ev = json.loads(EVENT_STATE_FILE.read_text(encoding="utf-8"))
print(f"event baseline issues: { {k: v.get('last_issue') for k, v in ev.items()} }", flush=True)

rep = Path("training/feedback/robustness/福彩3D.json")
print(f"report exists = {rep.exists()}", flush=True)
if rep.exists():
    d = json.loads(rep.read_text(encoding="utf-8"))
    print(f"report verdict = {d.get('verdict')}, mode = {d.get('mode')}", flush=True)

ok = status == "done" and rep.exists()
print("SMOKE " + ("PASS" if ok else "FAIL"), flush=True)
sys.exit(0 if ok else 1)
