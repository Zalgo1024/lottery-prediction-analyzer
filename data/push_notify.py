"""微信推送通知（企业微信群机器人 / PushPlus / Server酱）—— 出号与中奖记录送上门。

设计（2026-09-16 建立；2026-09-17 增企业微信群机器人；2026-09-18 升级为**图片推送**）：
  - 自动流水线每跑完一个彩种，把「最新开奖 + 上期结算 + 本期出号」推出去；
  - **图片为主**（`data/push_image.py` 渲染 PNG）：号码与中奖**全部展示、不截断**
    —— 企业微信 markdown 单条只有 4096 字节，号码一多必被砍；图片没有这个限制。
    每个彩种最多 4 条：① 出号短文字 → ② 号码图 → ③ 结算短文字 → ④ 中奖图
    （无中奖时只有 ①②；`图片推送=false` 时全部回退为文本）；
  - 渠道：**企业微信群机器人**（qyapi.weixin.qq.com，免费且不限额，推荐）/
    PushPlus（pushplus.plus）/ Server酱（sct.ftqq.com，免费 5 条/天）；
    后两者不支持图片 → 自动降级为 markdown 文本；
  - 配置持久化 config/push_notify.json（与 ticket_size 同款原子写；config/ 整体 gitignore）；
  - **失败永不阻塞主流水线**：所有对外入口内部吞掉一切异常，只返回结果 dict；
  - **每日上限默认 0 = 不限制**（2026-09-18 用户确认取消；设为 >0 可重新启用保护）；
  - **自动节流**：企微每个机器人 20 条/分钟，`_pace()` 保证相邻发送间隔 ≥ `推送间隔秒`
    （默认 3.2s ≈ 18.75 条/分钟），避免一个开奖夜（6 彩种 × 最多 4 条）被限速丢弃。

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
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = _BASE_DIR / "config"
STORE_PATH = CONFIG_DIR / "push_notify.json"

PROVIDER_OFF = "off"
PROVIDER_WECOM = "wecom"
PROVIDER_PUSHPLUS = "pushplus"
PROVIDER_SERVERCHAN = "serverchan"
VALID_PROVIDERS = (PROVIDER_OFF, PROVIDER_WECOM, PROVIDER_PUSHPLUS, PROVIDER_SERVERCHAN)

WECOM_WEBHOOK_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
WECOM_MAX_BYTES = 4096            # 企业微信群机器人 markdown 单条上限（UTF-8 字节）
WECOM_MAX_IMAGE_BYTES = 2 * 1024 * 1024   # 图片消息 base64 前的原始图片上限
WECOM_MAX_PER_MIN = 20            # 每个机器人 20 条/分钟（节流依据）

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


def _mark_pushed(cfg: Dict[str, Any], ok: bool, detail: str) -> None:
    stats = cfg.get("stats") or {}
    if stats.get("日期") != _today_str():
        stats = {"日期": _today_str(), "今日已推": 0}
    if ok:
        stats["今日已推"] = int(stats.get("今日已推", 0) or 0) + 1
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
    """仅供测试：清空节流时间戳。"""
    global _LAST_SEND_TS
    _LAST_SEND_TS = 0.0


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


def _wecom_render(title: str, body: str) -> str:
    """把标准 Markdown 适配成企业微信群机器人的写法。

    企业微信 markdown 不渲染 `-` / `1.` 列表（会原样显示成符号），
    故无序项改用 `·`；群机器人无独立 title 字段，标题并入正文首行。
    """
    out = []
    for raw in body.split("\n"):
        s = raw.strip()
        indent = raw[: len(raw) - len(raw.lstrip())]
        if s.startswith("- "):
            out.append("　" * (len(indent) // 2) + "· " + s[2:])
        else:
            out.append(raw)
    return _truncate_bytes(f"# {title}\n" + "\n".join(out), WECOM_MAX_BYTES)


# ------------------------------------------------------------
# 渠道发送（内部吞异常，绝不抛出）
# ------------------------------------------------------------
def send(title: str, content: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg if cfg is not None else sanitize(load_config())
    provider, token = cfg.get("provider"), cfg.get("token")
    if not effective_ready(cfg):
        return {"ok": False, "provider": provider, "detail": "未启用或 token 为空"}
    try:
        if provider == PROVIDER_WECOM:
            _pace(cfg)
            body = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": _wecom_render(title, content)},
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

    # 乐透型：红球… + 蓝球…
    red = zones.get("红球") or []
    blue = zones.get("蓝球") or []
    line = " ".join(f"{int(n):02d}" for n in red)
    if blue:
        line += " + " + " ".join(f"{int(n):02d}" for n in blue)
    return line or "?"


def _fmt_draw_numbers(draw: Optional[Dict[str, Any]]) -> str:
    if not draw or not draw.get("期号"):
        return ""
    issue = draw.get("期号")
    day = draw.get("日期") or ""
    nums = draw.get("红球")
    if nums is not None:  # 乐透型
        blue = draw.get("蓝球") or []
        line = " ".join(f"{int(n):02d}" for n in nums)
        if blue:
            line += " + " + " ".join(f"{int(n):02d}" for n in blue)
        return f"最新开奖 {issue}（{day}）：{line}"
    zones = draw.get("号码") or {}
    digits = []
    for k in sorted(zones.keys(), key=lambda s: str(s)):
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
    body += "\n\n> 仅供研究记录；单注中奖概率恒定，整体期望为负，请勿用于投注决策。"
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
    """把一个彩种本次结果拆成多条「一行文字 + 图片」发出。**永不抛出**。

    返回每条发送的结果列表（未启用 / 无内容 → 空列表）：
      ① 出号短文字 → ② 号码图
      ③ 结算短文字 → ④ 中奖图   （仅当本次有新结算且命中记录非空）

    号码**全部展示**（图片不受 4096 字节限制）；`图片推送=false` 或渲染失败时
    自动降级成 markdown 文本（受 4096 字节限制，最多展示 FALLBACK_MAX_SHOW 注）。
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
        use_image = bool(cfg.get("图片推送", True))
        if use_image:
            from data import push_image as _pimg   # 延迟导入：matplotlib 较重

        out: List[Dict[str, Any]] = []

        # ① 本期出号：一行短文字 + 号码图（全部注数，不截断）
        if want_nums:
            head = [f"**{lottery} 本期出号** 共 {len(predictions)} 注"
                    + (f"（{pred_info}）" if pred_info else "")]
            _d = _fmt_draw_numbers(draw_numbers)
            if _d:
                head.append(_d)
            if size_note:
                head.append(f"> {size_note}")
            if board_url:
                head.append(f"[打开看板 · 调出号数量 / 看全部记录]({board_url})")
            out.append(send(f"彩票助手｜{lottery}", "\n\n".join(head), cfg))

            if use_image:
                subtitle = " · ".join(x for x in (pred_info, f"共 {len(predictions)} 注") if x)
                out.append(_image_or_text(
                    render=lambda: _pimg.render_numbers_image(
                        [format_ticket_line(t) for t in predictions],
                        title=f"{lottery} 本期出号", subtitle=subtitle),
                    fallback=lambda: build_pipeline_message(
                        lottery, predictions=predictions, draw_numbers=draw_numbers,
                        pred_info=pred_info, size_note=size_note, board_url=board_url,
                        max_show=FALLBACK_MAX_SHOW),
                    cfg=cfg, tag=f"{lottery} 号码图"))

        # ② 开奖结算：一行短文字 + 中奖图
        if want_settle:
            head = [f"**{lottery} 开奖结算** {hit_summary or f'新增 {new_cnt} 条反馈'}"]
            for r in hit_records[:SETTLE_TEXT_MAX]:
                num = r.get("num") or _fmt_feedback_ticket(r)
                grade = r.get("prize") or r.get("grade") or "未中"
                m = r.get("match") or _fmt_match(r)
                issue = r.get("issue")
                head.append("- " + (f"{issue} 期 " if issue else "") + num
                            + (f" {m}" if m else "") + f" → **{grade}**")
            if len(hit_records) > SETTLE_TEXT_MAX:
                head.append(f"- …共 {len(hit_records)} 条中奖记录")
            out.append(send(f"彩票助手｜{lottery} 开奖结算", "\n".join(head), cfg))

            if use_image and hit_records:
                out.append(_image_or_text(
                    render=lambda: _pimg.render_wins_image(
                        hit_records, title=f"{lottery} 开奖结算",
                        subtitle=hit_summary or f"新增 {new_cnt} 条命中"),
                    fallback=lambda: build_pipeline_message(
                        lottery, eval_result=eval_result, hit_summary=hit_summary,
                        hit_records=hit_records, max_show=FALLBACK_MAX_SHOW),
                    cfg=cfg, tag=f"{lottery} 中奖图"))

        return out
    except Exception as e:  # pragma: no cover - 推送绝不影响主流水线
        logger.warning(f"图片推送流程异常 {lottery}: {e}")
        return [{"ok": False, "detail": str(e)[:200]}]


# ------------------------------------------------------------
# 对外入口②：流水线收尾调用（所有自动化入口共用）
# ------------------------------------------------------------
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
        ok = any(r.get("ok") for r in results)
        detail = str(results[-1].get("detail", ""))
        _mark_pushed(cfg, ok, detail)
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
        res = send("彩票助手｜项目体检", "\n".join(lines), cfg)
        _mark_pushed(cfg, res.get("ok", False), res.get("detail", ""))
        logger.info(f"体检推送: ok={res.get('ok')} ({res.get('detail', '')[:80]})")
        return res
    except Exception as e:
        logger.warning(f"体检推送异常: {e}")
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
        for lot, r in (results or {}).items():
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
ALL_LOTTERIES = ["双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩"]

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
        if hasattr(rec, "红球") and rec.红球 is not None:
            return {"期号": rec.期号, "红球": rec.红球, "蓝球": rec.蓝球,
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
    pending = fb.load_pending(lottery)
    predictions = (pending[0].get("预测号码") or []) if pending else []
    pred_info = f"目标期号 {pending[0].get('目标期号')}" if pending else ""
    draw_numbers = _latest_draw(lottery)

    hist = fb.load_feedback_history(lottery, lookback=200)
    wins = [r for r in hist if _grade_of(r) and _grade_of(r) != "未中"]
    hit_records = [{
        "num": _fmt_feedback_ticket(r),
        "match": _fmt_match(r),
        "prize": _grade_of(r),
        "issue": r.get("期号") or r.get("目标期号"),
        "valid": bool(r.get("valid_prediction", True)),
        "type": r.get("记录类型", "预测" if r.get("valid_prediction", True) else "训练"),
    } for r in wins[-12:]] if wins else []

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
            out.append({"lottery": lot, "ok": any(r.get("ok") for r in res),
                        "sent": len(res), "results": res})
        except Exception as e:
            out.append({"lottery": lot, "ok": False, "detail": str(e)[:200]})
    return out
