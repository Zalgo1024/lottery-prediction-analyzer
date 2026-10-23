# -*- coding: utf-8 -*-
"""web/app.py 启动开关测试：默认行为不变，只读实例必须两个子系统都关。

背景（2026-10-23）：此前 `web/app.py` **没有任何开关**，只要起第二个实例
（换端口做对照验证 / 反向代理后的只读实例）就会同时拉起
调度器 + worker + 启动自愈 + 训练循环，直接写生产 pending。
实测第二个实例 10 秒内触发 2 条 auto_pipeline（双色球/排列3）。

出号路径必须是单写者 ⇒ 默认行为保持不变，关闭必须显式声明。
"""

from web.app import _build_arg_parser, _resolve_runtime_flags


def _flags(*argv):
    return _resolve_runtime_flags(_build_arg_parser().parse_args(list(argv)))


def test_default_keeps_full_runtime():
    """不带参数 ⇒ 调度器与 worker 都要启动（生产默认行为不许变）。"""
    assert _flags() == (False, False)


def test_readonly_disables_both():
    assert _flags("--readonly") == (True, True)


def test_no_scheduler_only_disables_scheduler():
    """只关调度器时 worker 仍在（可以消费队列，但本实例不出号）。"""
    assert _flags("--no-scheduler") == (True, False)


def test_no_worker_only_disables_worker():
    assert _flags("--no-worker") == (False, True)


def test_flags_combine_with_port_and_debug():
    """开关与既有参数互不干扰。"""
    args = _build_arg_parser().parse_args(["--port", "5055", "--debug", "--readonly"])
    assert args.port == 5055
    assert args.debug is True
    assert _resolve_runtime_flags(args) == (True, True)


def test_default_port_is_5000():
    assert _build_arg_parser().parse_args([]).port == 5000
