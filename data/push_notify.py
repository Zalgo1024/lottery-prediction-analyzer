"""微信推送通知（企业微信群机器人 / PushPlus / Server酱）—— 出号与中奖记录送上门。

设计（2026-09-16，2026-09-17 增企业微信群机器人）：
  - 自动流水线（run_auto_pipeline_core）每跑完一个彩种，把「上期结算 + 本期出号
    + 本次出号数量」组装成一条消息推送出去；
  - 渠道：**企业微信群机器人**（qyapi.weixin.qq.com，免费且不限额，推荐）/
    PushPlus（pushplus.plus）/ Server酱（sct.ftqq.com，免费 5 条/天）；
  - 配置持久化 config/push_notify.json（与 ticket_size 同款原子写；config/ 整体 gitignore）；
  - **失败永不阻塞主流水线**：send() 内部吞掉一切异常只返回结果 dict；
  - 每日推送上限（默认 10 条）保护免费额度，超限静默跳过并记入 stats。

渠道差异：
  - 企业微信群机器人**只能单向推送**（webhook 无回执通道），因此在群里无法
    反过来调整出号数量；调数量请在看板「出号数量」卡操作（可配「看板地址」，
    推送末尾会带一条直达链接）。
  - 群机器人没有独立 title 字段，标题并入正文首行；markdown 语法也与标准不同
    （不支持 `-`/`1.` 列表渲染），由 _wecom_render 做适配。

诚实边界：推送只是"通知渠道"，内容与看板完全同源；不含任何"下期必中"含义，
单注中奖概率恒定（期望线性性），整体 EV 为负。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
WECOM_MAX_BYTES = 4096   # 企业微信群机器人 markdown 单条上限（UTF-8 字节）

DEFAULTS = {
    "provider": PROVIDER_OFF,
    "token": "",
    "enabled": False,
    "推号码": True,
    "推结算": True,
    "每日上限": 10,
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
    try:
        limit = max(1, min(50, int(payload.get("每日上限", DEFAULTS["每日上限"]))))
    except (TypeError, ValueError):
        limit = DEFAULTS["每日上限"]
    stats = payload.get("stats")
    cfg = {
        "provider": provider,
        "token": str(payload.get("token") or "").strip(),
        "enabled": bool(payload.get("enabled", False)) and provider != PROVIDER_OFF,
        "推号码": bool(payload.get("推号码", DEFAULTS["推号码"])),
        "推结算": bool(payload.get("推结算", DEFAULTS["推结算"])),
        "每日上限": limit,
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
# 每日额度
# ------------------------------------------------------------
def _today_str() -> str:
    return date.today().isoformat()


def _quota_ok(cfg: Dict[str, Any]) -> bool:
    stats = cfg.get("stats") or {}
    if stats.get("日期") != _today_str():
        return True  # 跨天自动清零
    return int(stats.get("今日已推", 0) or 0) < int(cfg.get("每日上限", 10))


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
    """命中情况文案，如『红中5 蓝中1』；数字型/七星彩无此字段则返回空串。"""
    if r.get("红球命中") is not None or r.get("蓝球命中") is not None:
        parts = []
        if r.get("红球命中") is not None:
            parts.append(f"红中{r['红球命中']}")
        if r.get("蓝球命中") is not None:
            parts.append(f"蓝中{r['蓝球命中']}")
        return " ".join(parts)
    return ""


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
# 对外入口：流水线收尾调用（内部吞异常）
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
    """推送本次流水线结果。未启用/额度耗尽/无内容时返回 None；否则返回 send 结果。"""
    try:
        cfg = sanitize(load_config())
        if not effective_ready(cfg):
            return None
        # 无新内容不推送：既没有新结算，也没有新出号（复用 pending 的重复运行不打扰）
        has_new_eval = int((eval_result or {}).get("new_feedback_count", 0) or 0) > 0
        has_new_nums = bool(fresh_prediction) and cfg.get("推号码", True)
        if not has_new_eval and not has_new_nums:
            return None
        if not _quota_ok(cfg):
            logger.info(f"微信推送跳过（已达每日上限 {cfg.get('每日上限')}）: {lottery}")
            _mark_pushed(cfg, False, f"跳过：已达每日上限 {cfg.get('每日上限')}")
            return None
        title, body = build_pipeline_message(
            lottery, predictions=predictions if cfg.get("推号码", True) else None,
            eval_result=eval_result if cfg.get("推结算", True) else None,
            hit_summary=hit_summary, hit_records=hit_records,
            draw_numbers=draw_numbers, pred_info=pred_info,
            size_note=_size_note(lottery, len(predictions or [])),
            board_url=str(cfg.get("看板地址") or ""),
            fresh_prediction=fresh_prediction)
        if not title:
            return None
        result = send(title, body, cfg)
        _mark_pushed(cfg, result.get("ok", False), result.get("detail", ""))
        logger.info(f"微信推送 {lottery}: ok={result.get('ok')} ({result.get('detail', '')[:80]})")
        return result
    except Exception as e:  # pragma: no cover - 推送绝不影响主流水线
        logger.warning(f"微信推送异常 {lottery}: {e}")
        return {"ok": False, "detail": str(e)[:200]}


# ------------------------------------------------------------
# 全彩种每日简报（2026-09-17）：一份覆盖本项目所有彩种，
# 每个彩种既列出算好的全部号码，又列出当晚中奖的号码与奖级。
# ------------------------------------------------------------
ALL_LOTTERIES = ["双色球", "大乐透", "排列三", "排列五", "3D", "七星彩"]


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


def build_lottery_report(lottery: str, *, max_show: int = 30) -> tuple:
    """为单个彩种组装 (标题, 正文)：本期出号 + 近期中奖明细 + 最新开奖行。

    返回 (None, None) 表示该彩种既无出号也无中奖，可跳过。
    """
    from data import feedback as fb
    pending = fb.load_pending(lottery)
    predictions = (pending[0].get("预测号码") or []) if pending else []
    pred_info = f"目标期号 {pending[0].get('目标期号')}" if pending else ""
    draw_numbers = _latest_draw(lottery)

    hist = fb.load_feedback_history(lottery, lookback=200)
    wins = [r for r in hist if r.get("中奖等级") and r.get("中奖等级") != "未中"]
    hit_records = [{
        "num": _fmt_feedback_ticket(r),
        "match": _fmt_match(r),
        "prize": r.get("中奖等级"),
        "issue": r.get("期号") or r.get("目标期号"),
        "valid": bool(r.get("valid_prediction", True)),
        "type": r.get("记录类型", "预测" if r.get("valid_prediction", True) else "训练"),
    } for r in wins[-8:]] if wins else []

    if not predictions and not wins:
        return None, None

    hit_summary = ""
    if wins:
        from collections import Counter as _C
        grades = _C(r.get("中奖等级") for r in wins)
        non_zero = {k: v for k, v in grades.items() if k != "未中"}
        hit_summary = "🎉 命中 " + "，".join(f"{k}×{v}" for k, v in non_zero.items())

    title, body = build_pipeline_message(
        lottery, predictions=predictions,
        eval_result={"new_feedback_count": len(wins)} if wins else {},
        hit_summary=hit_summary, hit_records=hit_records,
        draw_numbers=draw_numbers, pred_info=pred_info,
        size_note=_size_note(lottery, len(predictions)), max_show=max_show,
        fresh_prediction=bool(predictions))
    return title, body


def push_daily_digest(max_show: int = 30, lotteries=None) -> List[Dict[str, Any]]:
    """全彩种每日简报：逐一发送每个彩种的出号 + 中奖明细。

    返回每彩种的发送结果列表（空内容/未启用/超额度的会被跳过）。
    群机器人 20 条/分钟限速，逐条顺序发送足够安全。
    """
    cfg = sanitize(load_config())
    if not effective_ready(cfg):
        return [{"ok": False, "detail": "未启用或 token 为空"}]
    lotteries = lotteries or ALL_LOTTERIES
    out = []
    for lot in lotteries:
        try:
            if not _quota_ok(cfg):
                logger.info(f"每日简报跳过 {lot}：已达每日上限")
                out.append({"lottery": lot, "ok": False, "detail": "已达每日上限"})
                continue
            title, body = build_lottery_report(lot, max_show=max_show)
            if not title:
                out.append({"lottery": lot, "ok": False, "detail": "无出号且无中奖，跳过"})
                continue
            res = send(title, body, cfg)
            _mark_pushed(cfg, res.get("ok", False), res.get("detail", ""))
            out.append({"lottery": lot, **res})
        except Exception as e:
            out.append({"lottery": lot, "ok": False, "detail": str(e)[:200]})
    return out

