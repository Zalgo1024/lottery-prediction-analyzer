"""导入环闸门：`import ev` 期间不许触碰 `ev.profit.*`。

★ 起因（2026-11-01 01:01，日志里从 10-25 起已发生 5 次）
    GET /api/settings/ticket-size → 500
    _frozen_importlib._DeadlockError: deadlock detected by _ModuleLock('ev.profit.rules')
    堆栈：web/routes/settings.py:20 `from ev.payout import grade_probabilities`
        → ev/__init__.py:16 `from .rollover import ...`
        → ev/rollover.py:93 `_FIXED_PRIZES = {n: fixed_prizes_for(n) ...}`
        → ev/rollover.py:74 `from ev.profit.rules import rules_for`

为什么这不只是「一个接口 500」
    异常逃出请求线程 ⇒ 应用崩溃 ⇒ supervisor 重启 ⇒ 启动自愈发现
    「pending 注数 ≠ 滑轨」⇒ **按新注数重生成预测**。
    当晚就因此把 11-01 那批 50 注重生成成 500 注，在用户表态之前花掉 3,200 元。
    ⇒ 导入期的隐性副作用，代价是钱。

这两条测试锁的是「成因」而不是「症状」：
    ① 干净解释器里 `import ev` 之后 `ev.profit.rules` 不该在 sys.modules；
    ② `ev/__init__.py` eager import 的模块，模块级代码不许 import 或**调用**
       任何会拉 `ev.profit.*` 的东西（`_FIXED_PRIZES` 就是「函数内 import +
       模块级调用」这种绕过静态 import 检查的写法）。
"""
from __future__ import annotations

import ast
import io
import pathlib
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent


def _eager_modules() -> list:
    """`ev/__init__.py` 里 `from .X import ...` 的 X 列表（即导入 ev 就会执行的模块）。"""
    tree = ast.parse(io.open(BASE / "ev" / "__init__.py", encoding="utf-8").read())
    out = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            out.append(node.module)
    return out


def test_import_ev_does_not_pull_profit_rules():
    """① 干净解释器：`import ev` 不该把 `ev.profit.rules` 拉进来。

    只要它被拉进来，`ev.profit.rules → config → ev.budget_gate → ev` 这条回边
    就有机会在并发导入时形成环。
    """
    code = "import sys, ev; print('RULES_IN_SYS=' + str('ev.profit.rules' in sys.modules))"
    r = subprocess.run([sys.executable, "-c", code], cwd=str(BASE),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    assert "RULES_IN_SYS=False" in r.stdout, (
        "import ev 把 ev.profit.rules 拖进来了 —— 导入环回来了。\n" + r.stdout)


def test_eager_modules_have_no_module_level_profit_import():
    """② `ev/__init__.py` eager import 的模块，模块级不许 import `ev.profit.*`。

    这是静态形式的那条闸门；函数内 import 由下一条测试管。
    """
    bad = []
    for mod in _eager_modules():
        path = BASE / "ev" / (mod.replace(".", "/") + ".py")
        if not path.exists():
            continue
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in tree.body:                      # 只看模块级
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                if m == "ev.profit" or m.startswith("ev.profit."):
                    bad.append(f"ev/{mod}.py:{node.lineno} → {m}")
    assert not bad, (
        "ev/__init__ eager import 的模块在**模块级**依赖 ev.profit.*，"
        "与 ev → rollover → ev.profit.rules → config → ev.budget_gate → ev 构成导入环：\n  "
        + "\n  ".join(bad))


def test_eager_modules_do_not_call_rule_helpers_at_import_time():
    """★★ 绕开 import 检查的那种写法：函数内 import + **模块级调用**。

    `_FIXED_PRIZES = {n: fixed_prizes_for(n) ...}` 就是典型 —— AST 上看不到
    任何模块级 import，但执行起来照样把 `ev.profit.rules` 拽进导入期。
    所以这里扫的是「模块级语句里有没有调用这些会触规则表的函数」。
    """
    HELPERS = {"fixed_prizes_for", "fixed_prizes_robust", "fixed_prize_ev",
               "rules_for", "grade_probabilities", "payout_eff"}
    bad = []
    for mod in _eager_modules():
        path = BASE / "ev" / (mod.replace(".", "/") + ".py")
        if not path.exists():
            continue
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        # ★ 只扫**模块级会执行**的语句：函数/类的遗体不算（里面的调用是运行时才发生），
        #   但装饰器与默认参数算（它们在 def 语句求值时执行）。
        for node in tree.body:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                    and getattr(node.test.left, "id", None) == "__name__":
                continue                            # if __name__ == "__main__": 导入期不执行
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                to_scan = list(node.decorator_list)
                if not isinstance(node, ast.ClassDef):
                    to_scan += [d for d in node.args.defaults + node.args.kw_defaults if d]
                for head in to_scan:
                    for sub in ast.walk(head):
                        if isinstance(sub, ast.Call):
                            name = (getattr(sub.func, "id", None)
                                    or getattr(sub.func, "attr", None))
                            if name in HELPERS:
                                bad.append(f"ev/{mod}.py:{sub.lineno} 装饰器/默认参数调用 {name}()")
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name in HELPERS:
                    bad.append(f"ev/{mod}.py:{sub.lineno} 模块级调用 {name}()")
    assert not bad, (
        "模块级调用了会读规则表的函数 ⇒ 导入期就跨模块 import，导入环风险：\n  "
        + "\n  ".join(bad) + "\n改法：惰性化（PEP 562 模块级 __getattr__）或首次使用时才算。")


def test_fixed_prizes_still_importable_and_correct():
    """惰性化不能改变对外行为：`from ev.rollover import _FIXED_PRIZES` 仍须可用。"""
    code = (
        "from ev.rollover import _FIXED_PRIZES, fixed_prizes_robust\n"
        "assert sorted(_FIXED_PRIZES) == ['双色球', '大乐透'], sorted(_FIXED_PRIZES)\n"
        "for n in ('双色球', '大乐透'):\n"
        "    assert _FIXED_PRIZES[n] == fixed_prizes_robust(n, None), n\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=str(BASE),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    assert "OK" in r.stdout


def test_concurrent_import_of_both_sides_is_clean():
    """并发从两侧进入（请求线程 A 走 ev.payout，线程 B 直接走 ev.profit.rules）。

    ★ 只用**常驻**线程：短命线程在 Windows 上会让 `_ModuleLock.owner` 记录的
    ident 被复用，制造 `_DeadlockError` 假阳性（本人踩过：9 个短命线程一遍
    就报 `ev.payout` 死锁，换成 4 个常驻线程 1,600 次调用零错误）。
    """
    code = "\n".join([
        "import threading",
        "errs = []",
        "def a():",
        "    for _ in range(300):",
        "        import ev.payout, ev.rollover",
        "def b():",
        "    from ev.profit.rules import rules_for",
        "    for _ in range(300): rules_for('双色球', None)",
        "def c():",
        "    from ev.payout import grade_probabilities",
        "    for _ in range(300): grade_probabilities('双色球')",
        "def d():",
        "    import ev",
        "    for _ in range(300): ev.payout_eff",
        "def run(tag, fn):",
        "    try:",
        "        fn()",
        "    except BaseException as e:",
        "        errs.append(tag + ':' + type(e).__name__ + ':' + str(e)[:100])",
        "ts = [threading.Thread(target=run, args=(t, f)) for t, f in",
        "      (('A', a), ('B', b), ('C', c), ('D', d))]",
        "for t in ts: t.start()",
        "for t in ts: t.join(120)",
        "print('ERRS=' + repr(errs[:4]))",
        "print('ALIVE=' + str(sum(1 for t in ts if t.is_alive())))",
    ])
    r = subprocess.run([sys.executable, "-c", code], cwd=str(BASE),
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stderr[-1500:]
    assert "ERRS=[]" in r.stdout, "并发导入出现异常（导入环或锁序问题）：\n" + r.stdout
    assert "ALIVE=0" in r.stdout, "有线程卡住未结束（互等模块锁）：\n" + r.stdout
