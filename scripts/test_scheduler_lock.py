"""
Regression test: scheduler launch must not deadlock when invoked from a locked
section, matching the automatic tick path.
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.utils as web_utils
from web.scheduler import AutoScheduler


def main():
    original = web_utils.start_auto_pipeline
    web_utils.start_auto_pipeline = lambda lottery, params: "fake-task"
    try:
        scheduler = AutoScheduler()
        scheduler._save = lambda: None

        finished = []

        def worker():
            with scheduler._lock:
                finished.append(scheduler._launch("双色球"))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=1.0)

        if thread.is_alive():
            print("FAIL scheduler launch deadlocked while lock was already held")
            return 1
        if finished != [True]:
            print(f"FAIL scheduler launch returned {finished!r}")
            return 1
        print("OK scheduler launch lock regression")
        return 0
    finally:
        web_utils.start_auto_pipeline = original


if __name__ == "__main__":
    raise SystemExit(main())
