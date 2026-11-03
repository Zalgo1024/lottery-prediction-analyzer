"""微信推送通知（企业微信群机器人 / PushPlus / Server酱）—— 出号与中奖记录送上门。

设计（2026-09-16 建立；2026-09-17 增企业微信群机器人；2026-09-18 升级为**图片推送**；
2026-10-24 改为「按彩种分组：一条分隔头 + 一组图」）：
  - 自动流水线每跑完一个彩种，把「今晚开奖 + 本期出号 + 上期结算」推出去；
  - **每个彩种一组**：① 一条纯文本组头（`（双色球）彩票号码 / 中奖情况`，正文只留
    诚实条件与看板链接）→ ② 开奖图 1 张 → ③ 开奖结算图 M 张 → ④ 本期出号图 N 张
    （全量，`PAGE_TICKETS` 注/页）。
    ★ 2026-11-03 用户要求：图片顺序改为 **开奖 → 中奖 → 号码**，组头压成一行标签。
    三类内容各自成图且**图内有分区条**，不再把号码、开奖、中奖全堆在一条消息里；
  - 图片为主（`data/push_image.py` 渲染 PNG）：号码与中奖**全部展示、不截断**
    —— 企业微信 markdown 单条只有 4096 字节，号码一多必被砍；图片没有这个限制；
  - 渠道：**企业微信群机器人**（qyapi.weixin.qq.com，免费且不限额，推荐）/
    PushPlus（pushplus.plus）/ Server酱（sct.ftqq.com，免费 5 条/天）；
    **只有企微支持图片**，其它渠道整条降级为**一条** markdown 文本
    （不许"逐图失败各回退一次"，那会让对方收到 4~5 条重复长文）；
  - 配置持久化 config/push_notify.json（与 ticket_size 同款原子写；config/ 整体 gitignore）；
  - **失败永不阻塞主流水线**：所有对外入口内部吞掉一切异常，只返回结果 dict；
  - **每日上限默认 0 = 不限制**；达到上限时的计数按**消息条数**累计（一个彩种 4~7 条）；
  - **自动节流**：企微每个机器人 20 条/分钟。`_pace()` 管进程内相邻间隔，
    `_pace_global()` 把上次发送时间落到 `config/.push_pace.json` 管**跨进程**
    （`scripts/auto_scheduled.ps1` 每个彩种一个 `cli.py auto` 进程，进程内状态会被重置）。
  - ★★ **整组互斥**（2026-11-03）：节流只管「两条之间隔多久」，**管不住「谁插进来」** ——
    调度器「立即运行全部」/ worker 事件触发 / 启动自愈 / 计划任务可能同时向同一个
    webhook 推送，A 彩种的组头与图片会被 B 彩种的消息拆开（用户报障「没有严格按照
    顺序推送」）。故 `push_lottery_images` 用 `_group_spin_lock()` 把「组头 + 全部图」
    圈成一个原子段：**线程锁 + 跨进程文件锁**两层（Windows 下同一进程第二个线程
    打开同一锁文件会直接 PermissionError，见 `data/shadow.py` 的实测记录）。

渠道差异：
  - 企业微信群机器人**只能单向推送**（webhook 无回执通道），因此在群里无法
    反过来调整出号数量；调数量请在看板「出号数量」卡操作（可配「看板地址」，
    推送末尾会带一条直达链接）。
  - 群机器人没有独立 title 字段，标题并入正文首行；markdown 语法也与标准不同
    （不支持 `-`/`1.` 列表渲染），由 _wecom_render 做适配。
  - **图片消息同样没有 title/caption**，所以彩种名、期号、注数都画进图里。

诚实边界：推送只是"通知渠道"，内容与看板完全同源；不含任何"下期必中"含义，
单注中奖概率恒定（期望线性性），整体 EV 为负。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from data.file_lock import file_lock

logger = logging.getLogger(__name__)


def _ensure_logger() -> logging.Logger:
    """把本模块挂到 `logs/logger` 的具名 handler 上 —— 否则 INFO 全部丢弃。

    ★ 为什么必须补（2026-11-03 实查）：本模块原来是裸 `logging.getLogger(__name__)`，
      而 `logs/logger.py::setup_logger` **只给具名 logger 挂 handler、从不配置 root**
      （全仓无 `basicConfig`）。于是 `logger.info("微信推送 … 共 N 条")` 全部丢弃，
      只剩 `logging.lastResort` 把 WARNING 打到 stderr。后果是：真实发送顺序与
      企微 errcode **没有任何日志可查** —— 用户报「顺序乱了」时无法证伪也无法验收。
      补齐后每组会打印 `[PUSH] 组=… 序号=i/n 类型=… 段=…`，顺序问题变成可判定事实。

    ★ 只在真跑推送时调用（不在 import 期建 handler）：否则测试会凭空多出文件写入。
    ★ 名字必须是 `__name__`（即 `data.push_notify`）—— 写成 `"push_notify"` 会是
      **另一个 logger 对象**，本模块的日志照样进不去。
    """
    global logger
    if getattr(logger, "handlers", None):
        return logger
    try:
        from logs.logger import setup_logger
        logger = setup_logger(__name__)
    except Exception:                                     # noqa: BLE001
        # 日志初始化失败绝不阻断推送：挂个 NullHandler 让「有 handler」成立
        logger.addHandler(logging.NullHandler())
    return logger


_BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = _BASE_DIR / "config"
STORE_PATH = CONFIG_DIR / "push_notify.json"
# 跨进程节流状态（`scripts/auto_scheduled.ps1` 每个彩种起一个进程，
#   进程内的 `_LAST_SEND_TS` 在进程边界会被重置 ⇒ 必须落盘共享）
PACE_PATH = CONFIG_DIR / ".push_pace.json"

# ★ 整组消息的互斥（2026-11-03）。`file_lock` 会在目标路径后追加 `.lock`，
#   所以最终锁文件是 `config/.push_group.lock`（`config/*` 已在 .gitignore 里）。
#   为什么不按彩种分锁：用户要的是「A 组连在一起」，按彩种分锁 A、B 两组照样交错。
GROUP_LOCK_PATH = CONFIG_DIR / ".push_group"
# ★ 必须配一把线程锁：Windows 上**同一进程的第二个线程** `open(lock_file, "a+")`
#   会直接 PermissionError（`data/file_lock.py` 的 open 在 try 之外），
#   于是 file_lock 在「同进程多线程」场景下等于没锁。这与 `data/shadow.py`
#   的实测结论一致，那里也是「线程锁 + 文件锁」两层。
_GROUP_THREAD_LOCK = threading.Lock()

PROVIDER_OFF = "off"
PROVIDER_WECOM = "wecom"
PROVIDER_PUSHPLUS = "pushplus"
PROVIDER_SERVERCHAN = "serverchan"
VALID_PROVIDERS = (PROVIDER_OFF, PROVIDER_WECOM, PROVIDER_PUSHPLUS, PROVIDER_SERVERCHAN)

WECOM_WEBHOOK_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
WECOM_MAX_BYTES = 4096            # 企业微信群机器人 markdown 单条上限（UTF-8 字节）
WECOM_MAX_IMAGE_BYTES = 2 * 1024 * 1024   # 图片消息 base64 前的原始图片上限
WECOM_MAX_PER_MIN = 20            # 每个机器人 20 条/分钟（节流依据）
# 由 WECOM_MAX_PER_MIN 反算的最小安全间隔（3.0s）。`推送间隔秒` 小于它就会超速，
# 而企微超速是**静默丢消息**（errcode=45009），所以这里给出告警依据。
_MIN_INTERVAL = 60.0 / WECOM_MAX_PER_MIN
# 节流单次最长等待。有组锁后正常情况下 wait ≤ interval(3.2s)；一旦远超，
# 说明时钟跳变或 `.push_pace.json` 被外部写坏，需要告警而不是静默长睡。
PACE_MAX_WAIT = 120.0

FALLBACK_MAX_SHOW = 60            # 图片失败回退文本时，最多展示多少注号码
SETTLE_TEXT_MAX = 20              # 结算短文字最多列多少条（图片里另有全量）
HEALTH_MAX_ITEMS = 8              # 体检/自检推送最多列多少条明细

DEFAULTS = {
    "provider": PROVIDER_OFF,
    "token": "",
    "enabled": False,
    "推号码": True,
    "推结算": True,
    "图片推送": True,
    "每日上限": 0,                 # 0 = 不限制
    "推送间隔秒": 3.2,             # 企微节流最小间隔（20 条/分钟）
    "看板地址": "",
    "组级互斥": True,              # 关掉即回滚到「无锁发送」（顺序可能错，但绝不丢消息）
    "组锁超时秒": 420.0,           # 拿不到锁就降级为无锁发送并告警
}


# ------------------------------------------------------------
# 原子读写与配置归一
# ------------------------------------------------------------
def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> Dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    clean = sanitize(payload)
    _atomic_write_json(STORE_PATH, clean)
    return clean


def sanitize(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = payload or {}
    provider = str(payload.get("provider") or DEFAULTS["provider"]).lower()
    if provider not in VALID_PROVIDERS:
        provider = PROVIDER_OFF
    # 每日上限：0（默认）= 不限制；>0 才启用保护。上限放开到 100000。
    try:
        limit = int(payload.get("每日上限", DEFAULTS["每日上限"]))
    except (TypeError, ValueError):
        limit = DEFAULTS["每日上限"]
    limit = max(0, min(100000, limit))
    try:
        pace = float(payload.get("推送间隔秒", DEFAULTS["推送间隔秒"]))
    except (TypeError, ValueError):
        pace = DEFAULTS["推送间隔秒"]
    pace = max(0.0, min(60.0, pace))
    # 组锁超时：60~1800 秒。上限 1800 只是防手误写成天文数字；
    # ★ 实际不要超过 ~450：组锁等待发生在流水线内部（任务状态回写之前），
    #   而 `web/scheduler.py` 的自动触发只等 600 秒，等太久会被调度器判成超时。
    try:
        lock_timeout = float(payload.get("组锁超时秒", DEFAULTS["组锁超时秒"]))
    except (TypeError, ValueError):
        lock_timeout = DEFAULTS["组锁超时秒"]
    lock_timeout = max(60.0, min(1800.0, lock_timeout))
    stats = payload.get("stats")
    cfg = {
        "provider": provider,
        "token": str(payload.get("token") or "").strip(),
        "enabled": bool(payload.get("enabled", False)) and provider != PROVIDER_OFF,
        "推号码": bool(payload.get("推号码", DEFAULTS["推号码"])),
        "推结算": bool(payload.get("推结算", DEFAULTS["推结算"])),
        "图片推送": bool(payload.get("图片推送", DEFAULTS["图片推送"])),
        "每日上限": limit,
        "推送间隔秒": pace,
        "看板地址": str(payload.get("看板地址") or DEFAULTS["看板地址"]).strip()[:200],
        "组级互斥": bool(payload.get("组级互斥", DEFAULTS["组级互斥"])),
        "组锁超时秒": lock_timeout,
        "stats": stats if isinstance(stats, dict) else {},
    }
    return cfg


def effective_ready(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """配置是否达到可推送状态（启用 + 渠道有效 + token 非空）。"""
    cfg = cfg if cfg is not None else sanitize(load_config())
    return bool(cfg.get("enabled")) and cfg.get("provider") in (
        PROVIDER_WECOM, PROVIDER_PUSHPLUS, PROVIDER_SERVERCHAN) and bool(cfg.get("token"))


# ------------------------------------------------------------
# 每日额度（0 = 不限制）
# ------------------------------------------------------------
def _today_str() -> str:
    return date.today().isoformat()


def _quota_ok(cfg: Dict[str, Any]) -> bool:
    try:
        limit = int(cfg.get("每日上限", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return True                      # 不限量（默认）
    stats = cfg.get("stats") or {}
    if stats.get("日期") != _today_str():
        return True                      # 跨天自动清零
    return int(stats.get("今日已推", 0) or 0) < limit


def _mark_pushed(cfg: Dict[str, Any], ok: bool, detail: str, count: int = 1) -> None:
    """记一次推送。`count` = 本次实际发出的**消息条数**（一个彩种现在 4~7 条）。"""
    stats = cfg.get("stats") or {}
    if stats.get("日期") != _today_str():
        stats = {"日期": _today_str(), "今日已推": 0}
    if ok:
        stats["今日已推"] = int(stats.get("今日已推", 0) or 0) + max(1, int(count or 1))
    stats["最近推送"] = {
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "结果": "成功" if ok else "失败",
        "说明": detail[:200],
    }
    cfg["stats"] = stats
    save_config(cfg)


# ------------------------------------------------------------
# 发送节流（企微 20 条/分钟）
# ------------------------------------------------------------
_PACE_LOCK = threading.Lock()
_LAST_SEND_TS = 0.0


def _pace(cfg: Dict[str, Any]) -> None:
    """保证相邻发送间隔 ≥ cfg['推送间隔秒']（默认 3.2s ≈ 18.75 条/分钟 < 20）。

    企微超速会直接丢消息且不报错，所以宁可慢一点。失败绝不影响发送本身。
    """
    global _LAST_SEND_TS
    try:
        interval = float(cfg.get("推送间隔秒", DEFAULTS["推送间隔秒"]) or 0)
    except (TypeError, ValueError):
        interval = DEFAULTS["推送间隔秒"]
    if interval <= 0:
        return
    try:
        with _PACE_LOCK:
            wait = _LAST_SEND_TS + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _LAST_SEND_TS = time.monotonic()
    except Exception:  # pragma: no cover - 节流本身绝不阻断发送
        pass


def _reset_pace() -> None:
    """仅供测试：清空节流时间戳（进程内 + 跨进程状态文件）。"""
    global _LAST_SEND_TS
    _LAST_SEND_TS = 0.0
    try:
        PACE_PATH.unlink()
    except OSError:
        pass


def _pace_global(cfg: Dict[str, Any]) -> None:
    """**跨进程**最小发送间隔（默认同 `推送间隔秒`）。

    为什么必须有：`scripts/auto_scheduled.ps1` 对每个彩种单独起一个
    `python cli.py auto`，进程边界会把 `_LAST_SEND_TS` 重置 —— 6 个彩种连着跑，
    等于 6 次"本进程第一次发送"，进程内节流全部失效，企微会按 20 条/分钟限速丢消息。

    做法：`config/.push_pace.json` 存上次发送的 wall-clock 时间。**先占位再睡**：
    写入 `max(now, last+interval)` 后按差值 sleep，这样并发方读到的是"已被预约"的
    时间点，不会两个进程一起发。异常一律**放行**——限流绝不能阻断推送。

    ★ 2026-11-03：读-算-写原来**不加锁**，两个线程/进程同时读到同一个 `last`
      会算出同一个 `wait`、写入同一个预约点 ⇒ 同时发出两条，节流被击穿（企微 45009
      静默丢消息）。现在用 `file_lock(PACE_PATH)` 把「读 → 算 → 写」包成原子段，
      `sleep` 放在锁外（绝不持锁睡）。
    ★ 锁顺序全局唯一：**组锁 → pace 锁**；本函数从不反向去拿组锁 ⇒ 无环、无死锁。
    """
    try:
        interval = float(cfg.get("推送间隔秒", DEFAULTS["推送间隔秒"]) or 0)
    except (TypeError, ValueError):
        interval = DEFAULTS["推送间隔秒"]
    if interval <= 0:
        return
    # 间隔低于企微安全下界 ⇒ 超速会静默丢消息，必须让人看见（不阻断）
    if interval < _MIN_INTERVAL:
        logger.warning(
            f"「推送间隔秒」{interval:g}s < {_MIN_INTERVAL:.1f}s，"
            f"将超过企微 {WECOM_MAX_PER_MIN} 条/分钟（超速是**静默丢消息**）")
    try:
        wait = 0.0
        with file_lock(PACE_PATH, timeout=2.0):
            last = 0.0
            try:
                raw = json.loads(PACE_PATH.read_text(encoding="utf-8"))
                last = float((raw or {}).get("ts") or 0)
            except (OSError, ValueError, TypeError, AttributeError):
                last = 0.0
            now = time.time()
            wait = last + interval - now
            _atomic_write_json(PACE_PATH, {"ts": max(now, last + interval)})
        if wait > 0:
            if wait > PACE_MAX_WAIT:
                # 有组锁后正常不会超过 interval；超过说明时钟跳变或状态文件被写坏
                logger.warning(
                    f"节流等待 {wait:.1f}s 被截断为 {PACE_MAX_WAIT:.0f}s"
                    "（系统时钟跳变或 .push_pace.json 被外部改写？）")
            time.sleep(min(wait, PACE_MAX_WAIT))
    except Exception:  # pragma: no cover - 节流本身绝不阻断发送
        pass


# ------------------------------------------------------------
# 整组消息互斥（一个彩种 = 一条组头 + 若干图，必须连在一起）
# ------------------------------------------------------------
@contextlib.contextmanager
def _group_spin_lock(label: str, *, timeout: float = 420.0):
    """一个彩种整组消息的互斥：**线程锁 + 跨进程文件锁**两层。**永不抛出**。

    yield 的字符串说明这次到底拿到没拿到锁：
      `"ok"`              —— 两层都拿到，整组不会被打断；
      `"degraded-thread"` —— 同进程线程锁超时（有人在推别的彩种），降级为无锁；
      `"degraded-file"`   —— 跨进程文件锁超时/打开失败，降级为无锁；
      `"disabled"`        —— 配置里关掉了组级互斥。

    ★ 为什么两层都要（不是保险，是必需）：
      - `file_lock` 的 `open(lock_file, "a+")` 写在 try 之外（`data/file_lock.py:55`），
        Windows 上**同一进程的第二个线程**会直接 `PermissionError` —— 只靠文件锁时，
        同进程多线程（调度器一次起 8 个线程推 8 个彩种）根本没有排队效果。
        `data/shadow.py:82-87` 记着同一条实测结论。
      - 而线程锁只管本进程，跨进程（Flask + 计划任务各起一个 `cli.py auto`）
        必须靠文件锁。
    ★ 拿不到锁时**不丢消息**：降级为无锁发送 + warning + 结果里带锁状态。
      宁可顺序错，也不让用户收不到通知；但也不许静默 —— 降级必须留痕。
    """
    t0 = time.time()
    got_thread = False
    try:
        got_thread = _GROUP_THREAD_LOCK.acquire(timeout=max(0.0, timeout))
    except Exception:                                     # noqa: BLE001
        got_thread = False
    if not got_thread:
        logger.warning(f"[PUSH] 组={label} 组锁降级：线程锁 {timeout:.0f}s 未获取到，"
                       "本次改为无锁发送（顺序可能被打断）")
        yield "degraded-thread"
        return
    try:
        remain = max(1.0, timeout - (time.time() - t0))
        try:
            with file_lock(GROUP_LOCK_PATH, timeout=remain):
                yield "ok"
        except (TimeoutError, OSError) as e:
            # ★ 必须显式捕 OSError：Windows 上 PermissionError 会从 file_lock 的
            #   open() 直接冒出来，不捕就会把整组推送打挂。
            logger.warning(f"[PUSH] 组={label} 组锁降级：文件锁失败（{e}），"
                           "本次改为无锁发送（顺序可能被打断）")
            yield "degraded-file"
    finally:
        try:
            _GROUP_THREAD_LOCK.release()
        except Exception:                                 # noqa: BLE001
            pass


def _group_lock_cfg(cfg: Dict[str, Any]) -> float:
    """组锁超时（秒），来自配置并已 sanitize 夹取；异常时回落默认。"""
    try:
        return float(cfg.get("组锁超时秒", DEFAULTS["组锁超时秒"]))
    except (TypeError, ValueError):
        return float(DEFAULTS["组锁超时秒"])


# ------------------------------------------------------------
# 企业微信群机器人适配
# ------------------------------------------------------------
def _wecom_webhook_url(token: str) -> str:
    """兼容两种粘贴方式：完整 Webhook 地址，或只粘 `?key=` 后面那串。"""
    t = (token or "").strip()
    if t.startswith("http://") or t.startswith("https://"):
        return t
    return WECOM_WEBHOOK_BASE + urllib.parse.quote(t, safe="")


def _truncate_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节截断（企业微信上限按字节算，中文 1 字 3 字节）。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = "\n…（消息过长已截断）"
    room = max(0, limit - len(suffix.encode("utf-8")))
    return raw[:room].decode("utf-8", "ignore") + suffix


def _wecom_render(title: str, body: str, *, heading: bool = True) -> str:
    """把标准 Markdown 适配成企业微信群机器人的写法。

    企业微信 markdown 不渲染 `-` / `1.` 列表（会原样显示成符号），
    故无序项改用 `·`；群机器人无独立 title 字段，标题并入正文首行。

    ★ `heading=False`：不加 `# ` 前缀。企微里 `#` 是**一级标题**，字号明显更大且
      强制另起段落，一行二十来字就会折成两行 —— 与「组头压成一行」的目标相反。
      彩种分组的视觉分隔已由 `（双色球）` 这个标签本身承担，不需要 H1 装饰。
    """
    out = []
    for raw in body.split("\n"):
        s = raw.strip()
        indent = raw[: len(raw) - len(raw.lstrip())]
        if s.startswith("- "):
            out.append("　" * (len(indent) // 2) + "· " + s[2:])
        else:
            out.append(raw)
    prefix = (f"# {title}\n" if heading else f"{title}\n") if title else ""
    return _truncate_bytes(prefix + "\n".join(out), WECOM_MAX_BYTES)


# ------------------------------------------------------------
# 渠道发送（内部吞异常，绝不抛出）
# ------------------------------------------------------------
def send(title: str, content: str, cfg: Optional[Dict[str, Any]] = None,
         *, heading: bool = True) -> Dict[str, Any]:
    """发一条 markdown。`heading=False` 时不加 `# `（见 `_wecom_render`）。

    ★ `heading` 必须 keyword-only：`web/routes/settings.py` 的「测试推送」
      是以位置参数调 `send(title, body, cfg)` 的。
    """
    _ensure_logger()
    cfg = cfg if cfg is not None else sanitize(load_config())
    provider, token = cfg.get("provider"), cfg.get("token")
    if not effective_ready(cfg):
        return {"ok": False, "provider": provider, "detail": "未启用或 token 为空"}
    try:
        if provider == PROVIDER_WECOM:
            _pace(cfg)
            _pace_global(cfg)
            body = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": _wecom_render(title, content, heading=heading)},
            }).encode("utf-8")
            req = urllib.request.Request(
                _wecom_webhook_url(token), data=body, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ok = (data.get("errcode") == 0)
            return {"ok": ok, "provider": provider,
                    "detail": data.get("errmsg") or ("errcode=%s" % data.get("errcode"))}
        if provider == PROVIDER_PUSHPLUS:
            body = json.dumps({
                "token": token, "title": title, "content": content,
                "template": "markdown",
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://www.pushplus.plus/send", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ok = (data.get("code") == 200)
            return {"ok": ok, "provider": provider,
                    "detail": data.get("msg") or ("code=%s" % data.get("code"))}
        if provider == PROVIDER_SERVERCHAN:
            form = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
            url = f"https://sctapi.ftqq.com/{urllib.parse.quote(token)}.send"
            req = urllib.request.Request(url, data=form, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ok = (data.get("code") == 0)
            return {"ok": ok, "provider": provider,
                    "detail": data.get("message") or ("code=%s" % data.get("code"))}
        return {"ok": False, "provider": provider, "detail": f"未知渠道 {provider}"}
    except Exception as e:  # 网络/解析一切失败都降级为 ok=False
        logger.warning(f"微信推送失败({provider}): {e}")
        return {"ok": False, "provider": provider, "detail": str(e)[:200]}


def send_image(png: bytes, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """发一张图片（**仅企业微信群机器人支持**）。

    企微图片消息体：`{"msgtype":"image","image":{"base64":…,"md5":…}}`
    （base64 为原始图片的 b64；md5 为原始 bytes 的 hex 摘要）。
    pushplus / serverchan 不支持图片 → 返回 ok=False，由调用方降级为文本。
    """
    cfg = cfg if cfg is not None else sanitize(load_config())
    provider, token = cfg.get("provider"), cfg.get("token")
    if not effective_ready(cfg):
        return {"ok": False, "provider": provider, "detail": "未启用或 token 为空"}
    if not png:
        return {"ok": False, "provider": provider, "detail": "图片为空"}
    if provider != PROVIDER_WECOM:
        return {"ok": False, "provider": provider, "detail": f"渠道 {provider} 不支持图片"}
    if len(png) > WECOM_MAX_IMAGE_BYTES:
        return {"ok": False, "provider": provider,
                "detail": f"图片过大 {len(png)} 字节（上限 {WECOM_MAX_IMAGE_BYTES}）"}
    try:
        _pace(cfg)
        _pace_global(cfg)
        body = json.dumps({
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(png).decode("ascii"),
                "md5": hashlib.md5(png).hexdigest(),
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            _wecom_webhook_url(token), data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ok = (data.get("errcode") == 0)
        return {"ok": ok, "provider": provider, "bytes": len(png),
                "detail": data.get("errmsg") or ("errcode=%s" % data.get("errcode"))}
    except Exception as e:
        logger.warning(f"微信图片推送失败({provider}): {e}")
        return {"ok": False, "provider": provider, "detail": str(e)[:200]}


# ------------------------------------------------------------
# 消息组装
# ------------------------------------------------------------
def format_ticket_line(t: Dict[str, Any]) -> str:
    """一张票压成一行：乐透型 `01 05 … + 07`；数字型按位拼接 `292`。"""
    zones = t.get("号码")
    if not isinstance(zones, dict) or not zones:
        # 旧票面兜底：只有顶层红球/蓝球
        red, blue = t.get("红球"), t.get("蓝球")
        parts = []
        if red:
            parts.append(" ".join(f"{int(n):02d}" for n in red))
        if blue:
            parts.append("+ " + " ".join(f"{int(n):02d}" for n in blue))
        return " ".join(parts) if parts else "?"

    if any("位" in str(k) for k in zones):
        # 数字型 / 七星彩：按位顺序拼接
        def _idx(k: str) -> int:
            try:
                return int(str(k).replace("第", "").replace("位", ""))
            except ValueError:
                return 99
        digits = []
        for k in sorted(zones.keys(), key=_idx):
            v = zones[k]
            if isinstance(v, list) and v:
                digits.append(str(v[0]))
        return " ".join(digits) if digits else "?"

    # 乐透型（双/大）：红球… + 蓝球…
    red = zones.get("红球") or []
    blue = zones.get("蓝球") or []
    if red or blue:
        line = " ".join(f"{int(n):02d}" for n in red)
        if blue:
            line += " + " + " ".join(f"{int(n):02d}" for n in blue)
        return line or "?"

    # 其余分区型票面（七乐彩/快乐8）：按原顺序拼，空分区（如七乐彩特别号）自然跳过
    # 七乐彩票面只选基本号 7 个，特别号票面不含 ⇒ 只显示基本号。
    parts = []
    for k, v in zones.items():
        if isinstance(v, list) and v:
            parts.append(" ".join(f"{int(n):02d}" for n in v))
    return " + ".join(parts) if parts else "?"


def _zone_sort_key(k: Any) -> int:
    """`第2位` → 2。数字型/七星彩的分区键按**数值**排序（字符串排序会 10 位排在 2 位前）。"""
    s = str(k)
    digits = "".join(ch for ch in s if ch.isdigit())
    try:
        return int(digits) if digits else 99
    except ValueError:
        return 99


def _fmt_draw_numbers(draw: Optional[Dict[str, Any]]) -> str:
    if not draw or not draw.get("期号"):
        return ""
    issue = draw.get("期号")
    day = draw.get("日期") or ""
    # ★ 同 `_latest_draw`：数字型的 红球 是 []，`[] is not None` 为真会让
    #   数字型走进乐透型分支，拼出一个空号码行。必须用真值判断。
    nums = draw.get("红球") or []
    if nums:  # 乐透型
        blue = draw.get("蓝球") or []
        line = " ".join(f"{int(n):02d}" for n in nums)
        if blue:
            line += " + " + " ".join(f"{int(n):02d}" for n in blue)
        return f"最新开奖 {issue}（{day}）：{line}"
    zones = draw.get("号码") or {}
    digits = []
    for k in sorted(zones.keys(), key=_zone_sort_key):
        v = zones[k]
        if isinstance(v, list) and v:
            digits.append(str(v[0]))
    return f"最新开奖 {issue}（{day}）：{' '.join(digits)}" if digits else f"最新开奖 {issue}（{day}）"


def _fmt_feedback_ticket(r: Dict[str, Any]) -> str:
    """把一条反馈记录里的『预测号码』压成一行（乐透/数字型通用）。"""
    red = r.get("预测红球")
    blue = r.get("预测蓝球")
    if isinstance(red, list) and red:
        s = " ".join(f"{int(n):02d}" for n in red)
        if isinstance(blue, list) and blue:
            s += " + " + " ".join(f"{int(n):02d}" for n in blue)
        return s
    z = r.get("预测号码") or {}
    if isinstance(z, dict) and z:
        return format_ticket_line({"号码": z})
    return "?"


def _fmt_match(r: Dict[str, Any]) -> str:
    """命中情况文案，如『红中5 蓝中1』；数字型（排列3/5、3D、七星彩）回退成『命中N位』。"""
    if r.get("红球命中") is not None or r.get("蓝球命中") is not None:
        parts = []
        if r.get("红球命中") is not None:
            parts.append(f"红中{r['红球命中']}")
        if r.get("蓝球命中") is not None:
            parts.append(f"蓝中{r['蓝球命中']}")
        return " ".join(parts)
    # 数字型：没有红/蓝分区，用「总命中位」表达（七星彩/排列3/排列5/3D）
    if r.get("总命中") is not None:
        return f"命中{r['总命中']}位"
    return ""


def _grade_of(r: Dict[str, Any]) -> str:
    """取中奖等级文本。乐透型=`中奖等级`；数字型的记录两个键都有（`中奖等级`=`中奖玩法`），
    这里做双键兜底，避免任一字段缺失时把中奖记录静默漏掉。"""
    return str(r.get("中奖等级") or r.get("中奖玩法") or "")


def _record_zones(r: Dict[str, Any], which: str) -> Dict[str, Any]:
    """历史记录 → **结构化号码**（乐透 红/蓝球，数字型 各位置）。

    ★ 卡片图要回答「具体哪几个球中了」：球珠靠分区信息定颜色、靠逐位比对定
      实心/灰环，所以这里必须给结构化 dict，不能只给 `num` 那一串文本。
    """
    if which == "pred":
        z, keys = r.get("预测号码"), ("预测红球", "预测蓝球")
    else:
        z, keys = r.get("实际号码"), ("实际红球", "实际蓝球")
    if isinstance(z, dict) and z:
        return {k: v for k, v in z.items() if v}
    out: Dict[str, Any] = {}
    for k in keys:
        v = r.get(k)
        if v:
            out[k.replace("预测", "").replace("实际", "")] = v
    return out


def _hit_record(r: Dict[str, Any], lottery: str) -> Dict[str, Any]:
    """一条中奖历史 → 推送卡片结构（**保留原有键**，另加卡片所需字段）。

    保留 num/match/prize/issue/valid/type 是为了向后兼容：文本推送与行式回退仍在用。
    """
    grade = _grade_of(r)
    try:
        from data import prize_table as _pt
        pay = _pt.prize_payout(lottery, grade, r.get("期号"))
    except Exception:              # 奖金查不到不影响推送（卡片少一行药丸而已）
        pay = {}
    return {
        "num": _fmt_feedback_ticket(r),
        "match": _fmt_match(r),
        "prize": grade,
        "issue": r.get("期号") or r.get("目标期号"),
        "valid": bool(r.get("valid_prediction", True)),
        "type": r.get("记录类型", "预测" if r.get("valid_prediction", True) else "训练"),
        # ---- 卡片字段 ----
        "lottery": lottery,
        "valid_prediction": bool(r.get("valid_prediction", True)),
        "预测号码": _record_zones(r, "pred"),
        "实际号码": _record_zones(r, "act"),
        "预测红球": r.get("预测红球"), "预测蓝球": r.get("预测蓝球"),
        "实际红球": r.get("实际红球"), "实际蓝球": r.get("实际蓝球"),
        "红球命中": r.get("红球命中"), "蓝球命中": r.get("蓝球命中"),
        "分区命中": r.get("分区命中"),
        "总命中": r.get("总命中"), "总选择": r.get("总选择"),
        "策略": r.get("策略"), "置信度": r.get("置信度"),
        "预测日期": r.get("预测日期"), "开奖日期": r.get("开奖日期"),
        "批次内序号": r.get("批次内序号"), "批次规模": r.get("批次规模"),
        "机制γ": r.get("机制γ"), "机制倾斜": r.get("机制倾斜"),
        "来源": r.get("来源"),
        "奖金": ({k: pay.get(k) for k in ("金额", "浮动", "来源", "文本")}
                 if isinstance(pay, dict) else {}),
    }


def _size_note(lottery: str, n: int) -> str:
    """本次出号数量说明：滑轨模式 + 当前生效注数（读不到就返回空串，绝不影响推送）。"""
    if n <= 0:
        return ""
    try:
        from data import ticket_size as ts
        cfg = ts.sanitize(ts.load_config())
        mode = "固定" if cfg.get("mode") == ts.MODE_FIXED else "动态"
        note = f"出号数量：{mode}滑轨，当前生效 {ts.effective_count(lottery, cfg)} 注"
        pol = cfg.get("policy") or {}
        if mode == "动态" and pol.get("彩种") == lottery and pol.get("理由"):
            note += f"（{str(pol['理由'])[:60]}）"
        return note
    except Exception:
        return ""


# 诚实条件的**单一真源**（文本推送与分隔头共用，避免出现两套口径）。
# P5：原句「单注中奖概率恒定」是无前提的断言，只在「开奖对全部组合等可能」下成立
# ——把前提写出来，并指明由谁检验。
FOOTER_CONDITION = ("仅供研究记录；在「开奖对全部组合等可能」的前提下单注中奖概率恒定、"
                    "整体期望为负，请勿用于投注决策。该前提由四轨测量独立检验"
                    "（见 `python cli.py gap`）；未检出偏离 ≠ 证明开奖公平。")


def build_pipeline_message(
    lottery: str,
    *,
    predictions: Optional[List[Dict[str, Any]]] = None,
    eval_result: Optional[Dict[str, Any]] = None,
    hit_summary: str = "",
    hit_records: Optional[List[Dict[str, Any]]] = None,
    draw_numbers: Optional[Dict[str, Any]] = None,
    pred_info: str = "",
    size_note: str = "",
    board_url: str = "",
    fresh_prediction: bool = False,
    max_show: int = 10,
) -> tuple:
    """组装 (标题, Markdown 正文)。eval/predict 都为空时返回 (None, None) 表示无需推送。

    ★ 这是**纯文本**组装（图片渲染失败时的回退路径；也是 `push_daily_digest` 的正文来源）。
      正常路径由 `push_lottery_images` 发图片，号码不截断。

    size_note  = 本次出号数量/滑轨状态一行说明（可空）
    board_url  = 看板直达地址（可空；填了在结尾附一条链接，用于手机上调数量）
    max_show   = 本期出号最多展示前 N 注（数字型/乐透型注数多时避免超长，其余见看板）
    """
    sections = []

    # —— 上期结算（哪些计算好的号码中奖 + 中到几等）——
    eval_result = eval_result or {}
    new_cnt = int(eval_result.get("new_feedback_count", 0) or 0)
    if new_cnt > 0:
        lines = [f"- {hit_summary or '有新反馈'}"]
        for r in (hit_records or [])[:8]:
            tag = "预测" if r.get("valid") else "训练"
            num = r.get("num") or _fmt_feedback_ticket(r)
            grade = r.get("prize") or r.get("grade") or "未中"
            m = r.get("match") or _fmt_match(r)
            detail = f"{num}" + (f" {m}" if m else "") + f" → **{grade}**"
            issue = r.get("issue")
            prefix = f"  - {issue} 期 " if issue else "  - "
            lines.append(prefix + detail + f"（{tag}）")
        if len(hit_records or []) > 8:
            lines.append(f"  - …共 {len(hit_records)} 条中奖记录")
        sections.append("**开奖结算**\n" + "\n".join(lines))

    # —— 本期出号 ——
    if predictions:
        issue_note = pred_info or ""
        head = f"**本期出号** 共 {len(predictions)} 注"
        if issue_note:
            head += f"（{issue_note}）"
        if size_note:
            head += f"\n> {size_note}"
        lines = []
        show = predictions[:max_show]
        for i, t in enumerate(show, 1):
            lines.append(f"{i}. {format_ticket_line(t)}")
        if len(predictions) > max_show:
            lines.append(f"…其余 {len(predictions) - max_show} 注见看板")
        sections.append(head + "\n" + "\n".join(lines))

    if not sections:
        return None, None

    head = _fmt_draw_numbers(draw_numbers)
    if head:
        sections.insert(0, head)
    title = f"彩票助手｜{lottery}"
    body = "\n\n".join(sections)
    body += "\n\n> " + FOOTER_CONDITION
    _tilt = _mechanism_footer_line(lottery)
    if _tilt:
        body += f"\n> {_tilt}"
    if board_url:
        body += f"\n\n[打开看板 · 调出号数量 / 看全部记录]({board_url})"
    return title, body


# ------------------------------------------------------------
# 对外入口①：图片推送（一个彩种多条：短文字 + 号码图 [+ 结算文字 + 中奖图]）
# ------------------------------------------------------------
def _image_or_text(
    *,
    render: Callable[[], bytes],
    fallback: Callable[[], tuple],
    cfg: Dict[str, Any],
    tag: str,
) -> Dict[str, Any]:
    """优先发图片；渲染或发送失败则回退 markdown 文本。**永不抛出**。"""
    try:
        png = render()
        res = send_image(png, cfg)
        if res.get("ok"):
            return res
        logger.info(f"{tag}: 图片未发出（{res.get('detail')}），回退文本")
    except Exception as e:
        logger.warning(f"{tag}: 图片渲染失败（{e}），回退文本")
    try:
        title, body = fallback()
        if not title:
            return {"ok": False, "detail": f"{tag} 无内容可回退"}
        return send(title, body, cfg)
    except Exception as e:  # pragma: no cover - 回退也失败，返回失败而非抛出
        return {"ok": False, "detail": f"{tag} 回退亦失败: {str(e)[:150]}"}


def _mechanism_footer_line(lottery: str) -> str:
    """
    推送 footer 的「机制倾斜」状态行。**只在真的激活时才返回非空**。

    为什么 γ=0 时整行不出现：推送是高频通知，天天挂一句「机制倾斜未激活」
    只会变成噪音，而那句话本身也不携带信息（默认状态就是未激活）。
    **激活是异常事件，必须显式告知**——出号权重被动了，不能悄悄改。
    """
    try:
        from ev.mechanism.gap_select import tilt_state        # noqa: PLC0415
        from ev.mechanism.snapshot import mechanism_snapshot  # noqa: PLC0415
        snap = mechanism_snapshot(lottery)
        # ★ 传**原始 γ** + 显著位，由 tilt_state 统一过红线。
        #   只看 γ≠0 会绕过红线：双色球 γ=-0.0185 但显著=False，照判就会误报「已激活」。
        st = tilt_state(lottery, gamma=(snap or {}).get("γ"),
                        significant=(snap or {}).get("γ显著"),
                        reason=None if snap else "无测量缓存")
        if not st.get("倾斜"):
            return ""
        return (f"机制倾斜**已激活**：γ={st.get('γ')}（{st.get('方向', '')}）——"
                "出号已按 exp(-γ·z) 加权，与开奖倾向同向。")
    except Exception:                                          # noqa: BLE001
        return ""


def _pending_meta_lines(lottery: str, predictions: List[Dict[str, Any]]) -> List[str]:
    """从最新 pending 记录取图片头部的元信息行（目标期号/目标开奖/质量压缩说明）。

    与看板弹窗同源（load_pending），拿不到就返回空——绝不因元信息失败阻塞推送。
    """
    try:
        from data.feedback import load_pending
        recs = load_pending(lottery)
        rec = None
        for r in recs or []:
            if isinstance(r, dict) and r.get("预测号码") == predictions:
                rec = r
                break
        if rec is None and recs and isinstance(recs[0], dict):
            rec = recs[0]
        if not rec:
            return []
        lines = []
        issue, date = rec.get("目标期号"), rec.get("预测日期")
        if issue or date:
            lines.append(f"第 {issue or '?'} 期 · 目标开奖 {date or '?'}")
        c = rec.get("压缩")
        if isinstance(c, dict) and c.get("压缩前注数") != c.get("压缩后注数"):
            lines.append(
                f"质量压缩：出号 {c.get('压缩前注数')} 注 → 登记 {c.get('压缩后注数')} 注"
                f"（低质量淘汰 {c.get('质量淘汰', 0)} · 重复 {c.get('重复淘汰', 0)}"
                f" · 高重叠 {c.get('重叠淘汰', 0)}）；只省成本，单注中奖概率不变")
        return lines
    except Exception:
        return []


def _render_numbers_png(lottery: str, predictions: List[Dict[str, Any]], subtitle: str) -> bytes:
    """号码图渲染：优先「看板弹窗同款」票面图（球珠+分组），票面缺结构时回退旧行式。"""
    from data import push_image as _pimg
    errors = []
    try:
        return _pimg.render_tickets_image(
            predictions, title=f"{lottery} · 本期出号", subtitle=subtitle,
            meta_lines=_pending_meta_lines(lottery, predictions))
    except Exception as e:
        errors.append(f"ticket:{e}")
    try:
        return _pimg.render_numbers_image(
            [format_ticket_line(t) for t in predictions],
            title=f"{lottery} 本期出号", subtitle=subtitle)
    except Exception as e:
        errors.append(f"lines:{e}")
    raise RuntimeError("; ".join(errors)[:200])


# ------------------------------------------------------------
# 分隔头 + 「一组图」编排小件（2026-10-24 改版）
# ------------------------------------------------------------
DIVIDER_CHAR = "━"
PUSH_MAX_MSGS = 12        # 单次软预算：超了只告警，**绝不砍内容**（注数必须全画）


def _divider_title(lottery: str) -> str:
    """彩种分隔头：`━━━ 双色球 ━━━`——「以彩票分类做文字间隔」的载体。

    ★ 2026-11-03 起**只用于**「整条降级为纯文本」与看板「测试推送」这两种场景。
      正常推送的组头改用 `_group_header()`（一行标签，见下）——用户要的是
      `（双色球）彩票号码 / 中奖情况` 这种一眼能认出彩种的写法，而不是带一堆
      统计数字的段落。
    """
    bar = DIVIDER_CHAR * 3
    return f"{bar} {lottery} {bar}"


def _group_header(lottery: str, *, has_nums: bool, has_settle: bool,
                  has_draw: bool = False) -> str:
    """正常推送的**组头**：一行标签，只说「这是哪个彩种、这批图是什么」。

    形态（用户 2026-11-03 确认）：
        （双色球）彩票号码 / 中奖情况      ← 出号与结算都有
        （双色球）彩票号码                ← 只有出号
        （双色球）中奖情况                ← 只有结算
        （双色球）开奖结果                ← 只有开奖（极少：既没出号也没结算）

    ★ 为什么不带期号/注数/张数清单：那些信息图里都有（开奖图副标题有期号、
      出号图元信息与组头有注数、结算卡有奖级与奖金）。组头塞满统计后要在手机
      上占 5~6 行，反而把「一眼分组」的作用抵消掉。
    ★ 「只出号」与「只结算」必须分开写：两种情况下的字面若都写成
      「彩票号码 / 中奖情况」，用户会以为少发了一张图（silent 缺图是本项目老坑）。
    """
    bits = []
    if has_nums:
        bits.append("彩票号码")
    if has_settle:
        bits.append("中奖情况")
    if not bits:
        bits.append("开奖结果" if has_draw else "本期无内容")
    return f"（{lottery}）" + " / ".join(bits)


def _grade_text(r: Dict[str, Any]) -> str:
    """中奖记录的等级文本：乐透型在 `prize`，数字型可能落在 `grade`/`中奖玩法`。"""
    return str(r.get("prize") or r.get("grade") or _grade_of(r) or "未中")


def _grade_tally(hit_records: Sequence[Dict[str, Any]], top: int = 4) -> str:
    """`六等奖×2、五等奖×1`——按出现次数降序，多的排前面。"""
    cnt = Counter(_grade_text(r) for r in (hit_records or []))
    if not cnt:
        return ""
    items = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
    s = "、".join(f"{k}×{v}" for k, v in items[:top])
    return s + ("…" if len(items) > top else "")


def build_divider_message(
    lottery: str,
    *,
    predictions: Optional[List[Dict[str, Any]]] = None,
    eval_result: Optional[Dict[str, Any]] = None,
    hit_summary: str = "",
    hit_records: Optional[List[Dict[str, Any]]] = None,
    draw_numbers: Optional[Dict[str, Any]] = None,
    pred_info: str = "",
    size_note: str = "",
    board_url: str = "",
    n_draw: int = 0,
    n_num_pages: int = 0,
    n_win_pages: int = 0,
    header: Optional[str] = None,
) -> tuple:
    """组装彩种**组头**——该彩种本次唯一的一条文本。无内容返回 (None, None)。

    2026-11-03 起正文只留三块（用户确认）：
      ① 组头一行标签（默认 `（双色球）彩票号码 / 中奖情况`，可用 `header=` 覆盖）；
      ② 诚实声明（`FOOTER_CONDITION`，**一字不动**：它的前提与「未检出偏离 ≠ 证明开奖公平」
         是单一真源，不许因为「想短一点」而弱化）；
      ③ 看板链接 + （仅机制倾斜激活时）倾斜说明。

    ★ 删掉的是**能在图里看到**的东西：期号（开奖图副标题 / 出号图元信息）、
      出号注数（出号图副标题与组头）、张数清单（数一眼就有）、开奖号码（开奖图）。
      删不掉的是诚实口径与「机制异常必须告知」——这两类不是装饰。
    ★ `segs` 仍要算：它承担「三类图全无 ⇒ 不发空头」的判定，删了会让上游发出
      一条只有免责声明的空消息。
    """
    predictions = predictions or []
    hit_records = hit_records or []
    new_cnt = int((eval_result or {}).get("new_feedback_count", 0) or 0)

    segs = []
    if n_draw:
        segs.append(f"开奖结果 {n_draw} 张")
    if n_win_pages:                                  # ★ 顺序与发送顺序一致
        segs.append(f"开奖结算 {n_win_pages} 张")
    if n_num_pages:
        segs.append(f"本期出号 {n_num_pages} 张")
    if not segs:
        return None, None

    parts: List[str] = [f"> {FOOTER_CONDITION}"]
    if board_url:
        parts.append(f"[打开看板 · 调出号数量 / 看全部记录]({board_url})")

    tilt = _mechanism_footer_line(lottery)
    if tilt:                             # ★ 以前这行只存在于"图片失败回退"的文本里，
        parts.append(f"> {tilt}")        #   正常推送根本发不出去；现在并入组头。
    title = header if header is not None else _divider_title(lottery)
    return title, "\n\n".join(parts)


def _draw_flat_numbers(draw: Optional[Dict[str, Any]]) -> List[int]:
    """开奖 dict → 扁平号码列表（只用于判断"有没有号码"，不改顺序语义）。"""
    if not draw:
        return []
    reds = draw.get("红球") or []
    if reds:
        return [int(n) for n in reds] + [int(n) for n in (draw.get("蓝球") or [])]
    out: List[int] = []
    zones = draw.get("号码") or {}
    for k in sorted(zones.keys(), key=_zone_sort_key):
        v = zones[k]
        if isinstance(v, list):
            for n in v:
                try:
                    out.append(int(n))
                except (TypeError, ValueError):
                    pass
    return out


def _draw_has_numbers(draw: Optional[Dict[str, Any]]) -> bool:
    return bool(_draw_flat_numbers(draw))


_WEEK_CN = "一二三四五六日"


def _draw_title(lottery: str, draw: Optional[Dict[str, Any]]) -> str:
    """开奖日 == 今天才写「今晚开奖」，否则写「开奖结果」（不夸大）。"""
    day = str((draw or {}).get("日期") or "")[:10].replace("/", "-")
    when = "今晚开奖" if day and day == _today_str() else "开奖结果"
    return f"{lottery} · {when}"


def _draw_subtitle(draw: Optional[Dict[str, Any]]) -> str:
    draw = draw or {}
    issue = draw.get("期号")
    day = str(draw.get("日期") or "")[:10]
    wk = ""
    try:
        d = datetime.strptime(day.replace("/", "-"), "%Y-%m-%d")
        wk = f"（周{_WEEK_CN[d.weekday()]}）"
    except (ValueError, TypeError):
        wk = ""
    parts = []
    if issue:
        parts.append(f"第 {issue} 期")
    if day:
        parts.append(f"{day}{wk}")
    return " · ".join(parts)


def _draw_notes(
    predictions: Optional[List[Dict[str, Any]]],
    hit_records: Optional[List[Dict[str, Any]]],
    new_cnt: int,
    *,
    pred_info: str = "",
    size_note: str = "",
) -> List[str]:
    """开奖图「本期摘要」行：把出号/结算口径写在同一张图上，避免只看图就看不懂。"""
    notes: List[str] = []
    if predictions:
        s = f"本期出号 {len(predictions)} 注"
        if pred_info:
            s += f"（{pred_info}）"
        notes.append(s)
        if size_note:
            notes.append(size_note)
    if new_cnt > 0 or hit_records:
        tally = _grade_tally(hit_records or [])
        s = f"本期结算 {len(hit_records or []) or new_cnt} 条"
        if tally:
            s += f"：{tally}"
        notes.append(s)
    return notes


def _render_set(renders: Sequence[Callable[[], bytes]], tag: str) -> List[bytes]:
    """把一组渲染函数**先全部渲染完**再返回 PNG 列表。

    任一页失败 → 返回 `[]`：整组降级为一条文本，**不半页发送**
    （否则用户看到"第 1/4 页"，剩下 3 页消失且没有任何说明）。
    """
    out: List[bytes] = []
    for i, fn in enumerate(renders, 1):
        try:
            out.append(fn())
        except Exception as e:
            logger.warning(f"{tag}: 第 {i}/{len(renders)} 张渲染失败（{e}）")
            return []
    return out


def _send_pngs(pngs: Sequence[bytes], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """逐张发送已渲染好的 PNG（发送失败只记该张，不抛出）。"""
    return [send_image(png, cfg) for png in (pngs or [])]


def push_lottery_images(
    lottery: str,
    *,
    predictions: Optional[List[Dict[str, Any]]] = None,
    eval_result: Optional[Dict[str, Any]] = None,
    hit_summary: str = "",
    hit_records: Optional[List[Dict[str, Any]]] = None,
    draw_numbers: Optional[Dict[str, Any]] = None,
    pred_info: str = "",
    size_note: str = "",
    board_url: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """把一个彩种本次结果发成「一条组头 + 一组图」。**永不抛出**。

    消息序列（每个彩种一组，**整组原子**）：
      ① 组头（唯一文本）：`（双色球）彩票号码 / 中奖情况` + 诚实声明 + 看板链接
      ② 今晚开奖图  —— 有开奖号码时 1 张
      ③ 开奖结算图  —— 仅本次有新结算且命中记录非空，`PAGE_WINS` 行/页
      ④ 本期出号图  —— **全量**，`PAGE_TICKETS` 注/页（双色球 200 注 = 4 张）

    ★ 2026-11-03 两处变更（用户要求）：
      · 顺序改为 **开奖 → 中奖 → 号码**（原为 开奖 → 号码 → 中奖）；
      · ①~④ 用 `_group_spin_lock` 圈成一个原子段 —— 此前多条流水线线程
        （「立即运行全部」/ worker / 启动自愈 / 计划任务）会同时推同一 webhook，
        A 组的头和图片被 B 组消息拆开，用户看到的就是「没按顺序推」。

    `图片推送=false` 或非企微渠道 → **只发一条** markdown 文本
    （不许"逐图失败各回退一次"，否则 pushplus 会收到 4~5 条重复长文）。
    返回每条发送的结果列表（每条带 `lock` 标记说明这次有没有拿到组锁）；
    未启用 / 无内容 → 空列表。
    """
    try:
        cfg = cfg if cfg is not None else sanitize(load_config())
        if not effective_ready(cfg):
            return []
        predictions = predictions or []
        hit_records = hit_records or []
        size_note = size_note or _size_note(lottery, len(predictions))
        board_url = board_url or str(cfg.get("看板地址") or "")

        new_cnt = int((eval_result or {}).get("new_feedback_count", 0) or 0)
        want_nums = bool(predictions) and bool(cfg.get("推号码", True))
        want_settle = new_cnt > 0 and bool(cfg.get("推结算", True))
        if not want_nums and not want_settle:
            return []

        # ★ 只有企微群机器人吃图片。其它渠道走图片只会"每张失败→回退一次"，
        #   于是 pushplus/serverchan 收到 4~5 条重复长文本 —— 一律走单条文本。
        use_image = bool(cfg.get("图片推送", True)) and cfg.get("provider") == PROVIDER_WECOM

        if not use_image:
            title, body = build_pipeline_message(
                lottery,
                predictions=predictions if want_nums else None,
                eval_result=eval_result if want_settle else None,
                hit_summary=hit_summary, hit_records=hit_records,
                draw_numbers=draw_numbers, pred_info=pred_info,
                size_note=size_note, board_url=board_url,
                max_show=FALLBACK_MAX_SHOW)
            return [send(title, body, cfg)] if title else []

        from data import push_image as _pimg   # 延迟导入：matplotlib 较重

        num_pages = _pimg.paginate_tickets(predictions) if want_nums else []
        win_pages = ([hit_records[i:i + _pimg.PAGE_WINS]
                      for i in range(0, len(hit_records), _pimg.PAGE_WINS)]
                     if (want_settle and hit_records) else [])

        # ---- ① 先把图**全部渲染好**（失败只丢该组）----
        # 为什么先渲染再发：分隔头要写「本次 N 张图」，渲完才知道真实张数；
        # 先发头再渲染会出现"说 3 张、实际只来 1 张"的不一致。
        draw_png = b""
        if _draw_has_numbers(draw_numbers):
            try:
                draw_png = _pimg.render_draw_image(
                    draw_numbers, lottery=lottery,
                    title=_draw_title(lottery, draw_numbers),
                    subtitle=_draw_subtitle(draw_numbers),
                    notes=_draw_notes(predictions, hit_records, new_cnt,
                                      pred_info=pred_info, size_note=size_note))
            except Exception as e:
                logger.warning(f"{lottery} 开奖图渲染失败（{e}），跳过该图")

        num_pngs: List[bytes] = []
        num_renders: List[Callable[[], bytes]] = []
        if want_nums:
            meta = _pending_meta_lines(lottery, predictions)
            n = len(num_pages)
            for i, page_groups in enumerate(num_pages, 1):
                flat = [t for g in page_groups for t in g["tickets"]]
                lo = sum(len(g["tickets"]) for p in num_pages[:i - 1] for g in p) + 1
                hi = lo + len(flat) - 1
                sub = f"共 {len(predictions)} 注 · 第 {i}/{n} 页（第 {lo}–{hi} 注）"
                # ★ 循环变量必须用默认参数绑定：lambda 晚绑定会把每页都画成最后一页
                num_renders.append(lambda f=flat, g=page_groups, s=sub, k=i: (
                    _pimg.render_tickets_image(
                        f, title=f"{lottery} · 本期出号", subtitle=s,
                        meta_lines=(meta if k == 1 else ()), groups=g, page=(k, n))))
            num_pngs = _render_set(num_renders, f"{lottery} 出号图")

        win_pngs: List[bytes] = []
        if win_pages:
            n = len(win_pages)
            sub = hit_summary or f"新增 {new_cnt} 条命中"
            win_pngs = _render_set(
                [lambda c=c, k=k: _pimg.render_wins_image(
                    c, title=f"{lottery} · 开奖结算", subtitle=sub, page=(k, n))
                 for k, c in enumerate(win_pages, 1)],
                f"{lottery} 结算图")

        # ---- ②③④ 发送整段：**原子**（一个彩种 = 一条组头 + 一组图，不许被打断）----
        # ★ 锁只包「发送」：渲染已经在上面的锁外做完（顺序问题只发生在 send 之间，
        #   而渲染是 CPU 密集的 matplotlib，留在锁外可减小锁持有方差）。
        group_label = lottery
        _ensure_logger()
        t0 = time.time()
        if cfg.get("组级互斥", True):
            lock_ctx = _group_spin_lock(group_label, timeout=_group_lock_cfg(cfg))
        else:
            lock_ctx = contextlib.nullcontext("disabled")

        with lock_ctx as lock_state:
            out: List[Dict[str, Any]] = []

            def _emit(kind: str, seg: str, res: Dict[str, Any], page: str = "") -> None:
                """发一条并记账 —— 日志格式固定，用于**机械验证顺序**。

                `grep "\\[PUSH\\]" logs/<日期>.log` 后：同组 `序号` 必须 1..n 连续，
                任何他组行夹在本组「组头」与「结束」之间 ⇒ 组锁失效。
                """
                out.append(res)
                logger.info("[PUSH] 组=%s 序号=%d 类型=%s 段=%s%s ok=%s detail=%s",
                            group_label, len(out), kind, seg,
                            (f" 页={page}" if page else ""),
                            res.get("ok"), str(res.get("detail") or "")[:40])

            def _emit_pngs(pngs: List[bytes], seg: str) -> None:
                for k, r in enumerate(_send_pngs(pngs, cfg), 1):
                    _emit("image", seg, r,
                          page=(f"{k}/{len(pngs)}" if len(pngs) > 1 else ""))

            # ---- 组头（本彩种唯一的一条文本；标签按**实际有哪几类图**来写）----
            title, body = build_divider_message(
                lottery, predictions=predictions, eval_result=eval_result,
                hit_summary=hit_summary, hit_records=hit_records,
                draw_numbers=draw_numbers, pred_info=pred_info, size_note=size_note,
                board_url=board_url, n_draw=1 if draw_png else 0,
                n_num_pages=len(num_pngs), n_win_pages=len(win_pngs),
                header=_group_header(lottery, has_nums=want_nums,
                                     has_settle=bool(win_pngs or win_pages),
                                     has_draw=bool(draw_png)))
            if title:
                # ★ heading=False：企微的 `#` 是一级标题，会把一行标签折成两三行
                _emit("markdown", "组头", send(title, body, cfg, heading=False))

            # ---- ③ 今晚开奖 ----
            if draw_png:
                _emit_pngs([draw_png], "开奖")

            # ---- ④ 中奖（开奖结算）—— 用户要求排在号码之前 ----
            if win_pngs:
                _emit_pngs(win_pngs, "中奖")
            elif win_pages:
                t2, b2 = build_pipeline_message(
                    lottery, eval_result=eval_result, hit_summary=hit_summary,
                    hit_records=hit_records, max_show=FALLBACK_MAX_SHOW)
                if t2:
                    _emit("markdown", "中奖", send(t2, b2, cfg))

            # ---- ⑤ 号码（本期出号，全量 + 自动分页）----
            if want_nums:
                if not num_pngs:
                    # 票面图整组失败 → 行式图兜底（同样分页）
                    lines = [format_ticket_line(t) for t in predictions]
                    chunks = [lines[i:i + _pimg.PAGE_LINES]
                              for i in range(0, len(lines), _pimg.PAGE_LINES)]
                    num_pngs = _render_set(
                        [lambda c=c, k=k: _pimg.render_numbers_image(
                            c, title=f"{lottery} · 本期出号",
                            subtitle=f"共 {len(predictions)} 注", page=(k, len(chunks)))
                         for k, c in enumerate(chunks, 1)],
                        f"{lottery} 出号图(行式)")
                if num_pngs:
                    _emit_pngs(num_pngs, "号码")
                else:
                    t2, b2 = build_pipeline_message(
                        lottery, predictions=predictions, draw_numbers=draw_numbers,
                        pred_info=pred_info, size_note=size_note,
                        board_url=board_url, max_show=FALLBACK_MAX_SHOW)
                    if t2:
                        _emit("markdown", "号码", send(t2, b2, cfg))

            for r in out:
                # 把「这次到底有没有拿到组锁」带进结果 ⇒ 看板/日志都能看出是否降级
                r.setdefault("lock", lock_state)
            logger.info("[PUSH] 组=%s 结束 已发=%d 用时=%.1fs 锁=%s 失败=%d",
                        group_label, len(out), time.time() - t0, lock_state,
                        sum(1 for r in out if not r.get("ok")))

        if len(out) > PUSH_MAX_MSGS:
            logger.warning(f"{lottery} 单次推送 {len(out)} 条 > 软预算 {PUSH_MAX_MSGS}"
                           "（不砍内容：注数必须全画；必要时调大「推送间隔秒」）")
        return out
    except Exception as e:  # pragma: no cover - 推送绝不影响主流水线
        logger.warning(f"图片推送流程异常 {lottery}: {e}")
        return [{"ok": False, "detail": str(e)[:200]}]


# ------------------------------------------------------------
# 对外入口②：流水线收尾调用（所有自动化入口共用）
# ------------------------------------------------------------
def _summarize(results: Sequence[Dict[str, Any]]) -> tuple:
    """把逐条发送结果汇成 (ok, detail)。**丢消息不许报成功**。

    ★ 为什么不能再用 `any(r.get("ok"))`（2026-11-03）：一个彩种现在 4~21 条消息，
      `any` 意味着「21 条只成功 1 条」也算成功 —— 用户那边少了十几张图，
      而看板「最近推送」绿着，日志里也没有任何抱怨。这是最难发现的一类失真。
    ★ 也不走另一个极端：`okn == 0`（全挂）与「部分失败」必须区分，
      否则一次网络抖动会被报成「推送完全失败」，反而让人以为功能坏了。
    """
    n = len(results)
    okn = sum(1 for r in results if r.get("ok"))
    if n and okn == n:
        return True, str(results[-1].get("detail") or "")[:80]
    if okn == 0:
        last = str((results[-1] or {}).get("detail") or "") if results else ""
        return False, f"全部失败 0/{n}：{last[:60]}"
    bad = [str(r.get("detail") or "")[:30] for r in results if not r.get("ok")][:2]
    return False, f"部分失败 {okn}/{n}：{'；'.join(bad)}"


def push_pipeline_result(
    lottery: str,
    *,
    predictions: Optional[List[Dict[str, Any]]] = None,
    eval_result: Optional[Dict[str, Any]] = None,
    hit_summary: str = "",
    hit_records: Optional[List[Dict[str, Any]]] = None,
    draw_numbers: Optional[Dict[str, Any]] = None,
    pred_info: str = "",
    fresh_prediction: bool = False,
) -> Optional[Dict[str, Any]]:
    """推送本次流水线结果（图片 + 短文字）。未启用/无内容时返回 None。

    ★ 2026-09-18 起为**严格模式**：只要本次「有号码」或「有新结算」就推送，
      不再要求 `fresh_prediction` —— 白天自动化复用 pending 时也会推同样的号码
      （用户明确要求"每个自动化入口跑完都推一次"）。
      `fresh_prediction` 参数保留仅为兼容既有调用方，不再参与门槛判断。
      唯一不发的情况：既无号码又无结算（`skip_predict` 且无新反馈）→ 不发空消息。
    """
    try:
        cfg = sanitize(load_config())
        if not effective_ready(cfg):
            return None
        predictions = predictions or []
        new_cnt = int((eval_result or {}).get("new_feedback_count", 0) or 0)
        want_nums = bool(predictions) and bool(cfg.get("推号码", True))
        want_settle = new_cnt > 0 and bool(cfg.get("推结算", True))
        if not want_nums and not want_settle:
            return None
        if not _quota_ok(cfg):
            logger.info(f"微信推送跳过（已达每日上限 {cfg.get('每日上限')}）: {lottery}")
            _mark_pushed(cfg, False, f"跳过：已达每日上限 {cfg.get('每日上限')}")
            return None
        results = push_lottery_images(
            lottery, predictions=predictions, eval_result=eval_result,
            hit_summary=hit_summary, hit_records=hit_records,
            draw_numbers=draw_numbers, pred_info=pred_info, cfg=cfg)
        if not results:
            return None
        ok, detail = _summarize(results)
        # ★ 按**消息条数**计额度：改版后一个彩种 4~7 条，按调用次数计会严重低估
        _mark_pushed(cfg, ok, detail, count=len(results))
        logger.info(f"微信推送 {lottery}: ok={ok} 共 {len(results)} 条 ({detail[:80]})")
        return {"ok": ok, "detail": detail, "sent": len(results), "results": results}
    except Exception as e:  # pragma: no cover - 推送绝不影响主流水线
        logger.warning(f"微信推送异常 {lottery}: {e}")
        return {"ok": False, "detail": str(e)[:200]}


# ------------------------------------------------------------
# 对外入口③：自检推送（项目体检 / 启动恢复）
# ------------------------------------------------------------
def push_health_summary(
    real_problems: Optional[List[Any]] = None,
    *,
    report_path: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """`scripts/health_check.py` 跑完推送体检摘要：真实待处理问题 N 项 + 前若干条。"""
    try:
        cfg = cfg if cfg is not None else sanitize(load_config())
        if not effective_ready(cfg):
            return None
        items = [(str(a), str(b)) for a, b in (real_problems or [])]
        lines = [f"**项目体检** 真实待处理问题 **{len(items)} 项**"]
        if items:
            for name, detail in items[:HEALTH_MAX_ITEMS]:
                lines.append(f"- **{name}**：{detail}")
            if len(items) > HEALTH_MAX_ITEMS:
                lines.append(f"- …另有 {len(items) - HEALTH_MAX_ITEMS} 项，见体检报告")
        else:
            lines.append("- ✅ 数据完整性 / pending / 流水线快照全部通过")
        if report_path:
            lines.append(f"\n报告：{report_path}")
        # ★ 单条消息也要抢一次组锁（短超时）：否则它会插进某个彩种的一组图中间，
        #   用户看到的就是「双色球的图里混进一条体检」。抢不到就降级直接发 ——
        #   这类消息本身不带分组语义，顺序错了不影响理解，但不能因为它卡住流水线。
        with _group_spin_lock("体检", timeout=5.0):
            res = send("彩票助手｜项目体检", "\n".join(lines), cfg)
        _mark_pushed(cfg, res.get("ok", False), res.get("detail", ""))
        logger.info(f"体检推送: ok={res.get('ok')} ({res.get('detail', '')[:80]})")
        return res
    except Exception as e:
        logger.warning(f"体检推送异常: {e}")
        return {"ok": False, "detail": str(e)[:200]}


def push_digest(
    digest: Optional[Dict[str, Any]] = None,
    *,
    only_on_problem: bool = True,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """`web/health_digest.py` 的离线体检日报推送。

    **默认只在有问题时推送**（`only_on_problem=True`）。理由：这个项目的用户
    已经不能保证天天开机，一条每天都说"一切正常"的消息只会被静音，
    真正出问题时反而看不见。正常时返回值里 `skipped=True`，不消耗推送配额。
    """
    try:
        d = digest or {}
        problems = d.get("问题") or []
        if only_on_problem and not problems:
            return {"ok": True, "skipped": True, "detail": "无问题，静默不推送"}

        cfg = cfg if cfg is not None else sanitize(load_config())
        if not effective_ready(cfg):
            return None

        pl = d.get("流水线") or {}
        inv = d.get("注数一致性") or {}
        lag = d.get("数据滞后") or []
        lag_bad = [r for r in lag if isinstance(r.get("滞后天数"), int) and r["滞后天数"] > 0]

        lines = [
            f"**离线体检** 过去 {d.get('窗口天数', '?')} 天 · {d.get('结论', '')}",
            f"- 流水线：{pl.get('总次数', 0)} 次（成功 {pl.get('成功', 0)} / 失败 {pl.get('失败', 0)}）",
            f"- 注数：{'全部一致' if inv.get('全部一致') else '存在不一致'}"
            + (f"（{'; '.join(inv.get('不一致') or [])}）" if inv.get("不一致") else ""),
            f"- 数据滞后：{len(lag_bad)} 个彩种"
            + (f"（{'; '.join(str(r['彩种']) + ' ' + str(r['滞后天数']) + '天' for r in lag_bad[:3])}）"
               if lag_bad else ""),
        ]
        if problems:
            lines.append(f"- 待处理 **{len(problems)} 项**：")
            for p in problems[:HEALTH_MAX_ITEMS]:
                lines.append(f"  - **{p.get('彩种', '-')}**：{p.get('项', '')} — {p.get('详情', '')}")
            if len(problems) > HEALTH_MAX_ITEMS:
                lines.append(f"  - …另有 {len(problems) - HEALTH_MAX_ITEMS} 项")

        res = send("彩票助手｜离线体检", "\n".join(lines), cfg)
        _mark_pushed(cfg, res.get("ok", False), res.get("detail", ""))
        logger.info(f"离线体检推送: ok={res.get('ok')} 问题 {len(problems)} 项")
        return res
    except Exception as e:  # pragma: no cover - 推送失败绝不影响体检
        logger.warning(f"离线体检推送异常: {e}")
        return {"ok": False, "detail": str(e)[:200]}


def push_recovery_summary(
    results: Optional[Dict[str, Any]] = None,
    *,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """`web/startup_recovery.py` 跑完推送一次启动自检摘要。

    results = `recover_all_lotteries()` 的返回：{彩种: {fetched, latest_before, latest_after} | {error}}
    """
    try:
        cfg = cfg if cfg is not None else sanitize(load_config())
        if not effective_ready(cfg):
            return None
        changed, errored = [], []
        healed = results.get("_注数自愈") if isinstance(results, dict) else None
        for lot, r in (results or {}).items():
            if str(lot).startswith("_"):
                continue
            if not isinstance(r, dict):
                continue
            if r.get("error"):
                errored.append(f"- **{lot}**：抓取失败 {r['error']}")
                continue
            fetched = int(r.get("fetched", 0) or 0)
            if fetched > 0:
                changed.append(f"- **{lot}**：+{fetched} 期 "
                               f"({r.get('latest_before')}→{r.get('latest_after')})")
        lines = [f"**启动自检** 补数据 {len(changed)} 个彩种"
                 + (f"，失败 {len(errored)} 个" if errored else "")]
        if changed or errored:
            lines += changed[:HEALTH_MAX_ITEMS] + errored[:HEALTH_MAX_ITEMS]
        else:
            lines.append("- ✅ 无彩种滞后，一切已是最新")
        # 结构性自愈（2026-10-21）：下一期 pending 组数 ≠ 出号数量（被覆盖 / 引擎少注）
        if isinstance(healed, dict) and healed:
            lines.append(f"- ⚠️ 已按滑轨重生成下一期预测：{len(healed)} 个彩种")
            for lot, info in list(healed.items())[:HEALTH_MAX_ITEMS]:
                if isinstance(info, dict):
                    lines.append(f"  - **{lot}**：{info.get('原组数')} → {info.get('滑轨')} 注"
                                 f"（{info.get('动作') or '重生成'}）")
        # ★ 同 push_health_summary：单条消息抢一次短超时组锁，不抢也照发
        with _group_spin_lock("启动自检", timeout=5.0):
            res = send("彩票助手｜启动自检", "\n".join(lines), cfg)
        _mark_pushed(cfg, res.get("ok", False), res.get("detail", ""))
        logger.info(f"启动自检推送: ok={res.get('ok')} ({res.get('detail', '')[:80]})")
        return res
    except Exception as e:
        logger.warning(f"启动自检推送异常: {e}")
        return {"ok": False, "detail": str(e)[:200]}


# ------------------------------------------------------------
# 全彩种每日简报（2026-09-17）：一份覆盖本项目所有彩种，
# 每个彩种既列出算好的全部号码，又列出当晚中奖的号码与奖级。
# ------------------------------------------------------------
# 彩种**规范名**：必须与 config.LOTTERY_CONFIG 的键完全一致。
# ★2026-09-18 修：此前误写成「排列三 / 排列五 / 3D」，而全项目（config、数据文件、
#   流水线）用的是「排列3 / 排列5 / 福彩3D」。名字对不上 → load_pending /
#   load_feedback_history 按名取文件全部落空（返回 0），这 3 个彩种在全彩种简报里
#   被**静默跳过**（既不显示号码也不显示中奖），且不报任何错。
ALL_LOTTERIES = ["双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩", "七乐彩", "快乐8"]

# 常见别名 → 规范名：调用方（含人工调用）传「排列三」也能落到正确文件
_LOTTERY_ALIASES = {
    "排列三": "排列3", "排列五": "排列5",
    "3D": "福彩3D", "福彩3d": "福彩3D",
}


def canonical_lottery(name: Any) -> str:
    """把彩种别名归一到 config 的规范名（未收录的原样返回，不做猜测）。"""
    s = str(name or "").strip()
    return _LOTTERY_ALIASES.get(s, s)


def _latest_draw(lottery: str) -> Dict[str, Any]:
    """取该彩种最新一期开奖（供简报『最新开奖』行）。"""
    try:
        from data.loader import load_lottery
        rec = load_lottery(lottery).records[0]
        # ★ 必须用**真值**判断而不是 `is not None`：数字型（排列3/排列5/福彩3D）
        #   身上也有 `红球` 属性，但值是空列表 []，`[] is not None` 为真 ⇒
        #   旧写法会把数字型当成乐透型，返回 红球=[] ⇒「最新开奖 xxx：」后面空白。
        reds = getattr(rec, "红球", None) or []
        if reds:
            return {"期号": rec.期号, "红球": reds, "蓝球": getattr(rec, "蓝球", None) or [],
                    "日期": str(rec.开奖日期) if rec.开奖日期 else ""}
        zones = getattr(rec, "zone_numbers", {}) or {}
        return {"期号": rec.期号, "号码": zones,
                "日期": str(rec.开奖日期) if rec.开奖日期 else ""}
    except Exception:
        return {}


def digest_payload(lottery: str) -> Optional[Dict[str, Any]]:
    """为单个彩种组装推送/简报所需的全部字段（从 pending + 中奖历史读）。

    返回 dict（predictions / eval_result / hit_summary / hit_records /
    draw_numbers / pred_info / size_note），该彩种既无出号又无中奖时返回 None。
    """
    from data import feedback as fb
    lottery = canonical_lottery(lottery)   # 「排列三」→「排列3」，否则读不到文件
    # ★ 只推生产批：研究台账批（低拥挤 200 注）后写且注数大，
    #   取 pending[0] 会把 200 注研究票当成「本期出号」推到微信。
    pending = fb.load_pending(lottery)
    prod = fb.latest_production_pending(pending)
    predictions = (prod.get("预测号码") or []) if prod else []
    pred_info = f"目标期号 {prod.get('目标期号')}" if prod else ""
    draw_numbers = _latest_draw(lottery)

    # 中奖历史同样排除研究域；且必须**先过滤再截断**（load_feedback_history 的
    # lookback 是先截断，研究批一开就会把生产中奖记录挤出窗口）。
    hist = [r for r in fb.load_feedback_history(lottery)
            if fb._domain_of(r) == fb.DOMAIN_PROD][-200:]
    wins = [r for r in hist if _grade_of(r) and _grade_of(r) != "未中"]
    # ★ 2026-10-24：中奖图改成「逐注卡片」，卡片需要**结构化号码 + 奖金 + 策略**
    #   才能画出「哪几个球中了」，故统一走 _hit_record 组装（老键全部保留）。
    hit_records = [_hit_record(r, lottery) for r in wins[-12:]] if wins else []

    if not predictions and not wins:
        return None

    hit_summary = ""
    if wins:
        from collections import Counter as _C
        grades = _C(_grade_of(r) for r in wins)
        non_zero = {k: v for k, v in grades.items() if k != "未中"}
        hit_summary = "🎉 命中 " + "，".join(f"{k}×{v}" for k, v in non_zero.items())

    return {
        "predictions": predictions,
        "eval_result": {"new_feedback_count": len(wins)} if wins else {},
        "hit_summary": hit_summary,
        "hit_records": hit_records,
        "draw_numbers": draw_numbers,
        "pred_info": pred_info,
        "size_note": _size_note(lottery, len(predictions)),
    }


def build_lottery_report(lottery: str, *, max_show: int = 30) -> tuple:
    """为单个彩种组装 (标题, 纯文本正文)（回退/调试用；正式路径走图片）。

    返回 (None, None) 表示该彩种既无出号也无中奖，可跳过。
    """
    payload = digest_payload(lottery)
    if payload is None:
        return None, None
    title, body = build_pipeline_message(
        lottery, predictions=payload["predictions"], eval_result=payload["eval_result"],
        hit_summary=payload["hit_summary"], hit_records=payload["hit_records"],
        draw_numbers=payload["draw_numbers"], pred_info=payload["pred_info"],
        size_note=payload["size_note"], max_show=max_show,
        fresh_prediction=bool(payload["predictions"]))
    return title, body


def push_daily_digest(max_show: int = 30, lotteries=None) -> List[Dict[str, Any]]:
    """全彩种每日简报：逐一发送每个彩种的出号 + 中奖明细（图片，与流水线同款）。

    返回每彩种的发送汇总（空内容/未启用会被跳过）。逐条顺序发送 + `_pace()` 节流，
    满足企微 20 条/分钟限速。
    """
    cfg = sanitize(load_config())
    if not effective_ready(cfg):
        return [{"ok": False, "detail": "未启用或 token 为空"}]
    lotteries = [canonical_lottery(x) for x in (lotteries or ALL_LOTTERIES)]
    out = []
    for lot in lotteries:
        try:
            payload = digest_payload(lot)
            if payload is None:
                out.append({"lottery": lot, "ok": False, "detail": "无出号且无中奖，跳过"})
                continue
            res = push_lottery_images(lot, cfg=cfg, **payload)
            _ok, _detail = _summarize(res)
            out.append({"lottery": lot, "ok": _ok, "detail": _detail,
                        "sent": len(res), "results": res})
        except Exception as e:
            out.append({"lottery": lot, "ok": False, "detail": str(e)[:200]})
    return out
