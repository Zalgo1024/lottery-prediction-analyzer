"""
Unified local test runner.

Default suite is deterministic and offline:
  1. unittest tests/
  2. script smoke checks that do not update data or baseline files

Use --include-regression to also run scripts/regression_check.py, which is
expected to fail when local lottery data has advanced beyond _reg_ref.json.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run_step(name: str, args: list[str]) -> int:
    print("\n" + "=" * 72, flush=True)
    print(f"[RUN] {name}", flush=True)
    print(" ".join(args), flush=True)
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(args, cwd=ROOT, env=env)
    if result.returncode == 0:
        print(f"[OK] {name}", flush=True)
    else:
        print(f"[FAIL] {name} exit={result.returncode}", flush=True)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the project's repeatable test suite.")
    parser.add_argument(
        "--include-regression",
        action="store_true",
        help="Also run scripts/regression_check.py against the local data snapshot.",
    )
    args = parser.parse_args()

    python = sys.executable
    steps = [
        ("unit tests", [python, "-m", "unittest", "discover", "-s", "tests", "-v"]),
        ("feedback compatibility script", [python, "scripts/test_feedback_generic.py"]),
        ("new lottery smoke script", [python, "scripts/smoke_new_lotteries.py"]),
    ]
    if args.include_regression:
        steps.append(("snapshot regression script", [python, "scripts/regression_check.py"]))

    failures = 0
    for name, cmd in steps:
        failures += 1 if run_step(name, cmd) else 0

    print("\n" + "=" * 72, flush=True)
    if failures:
        print(f"[FAIL] test run finished with {failures} failing step(s)", flush=True)
        return 1
    print("[OK] all selected test steps passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
