# -*- coding: utf-8 -*-
"""前缀口径 · 影子验证接线状态报告（离线只读，不接生产）。

用法：
    E:/Python/python.exe scripts/prefix_shadow_report.py
    E:/Python/python.exe scripts/prefix_shadow_report.py \
        --verdict logs/prefix_curve_formal_ssq_K100_p360_s5.json \
        --out logs/prefix_shadow_status.json

★ 本报告**不产生新读数**：它回答「现有证据够不够格进入影子验证、还差什么」。
  台账文件只读取、不写入。「未开判」是当前结论，不是失败，也不是通过。
  **合格登记期数一律从台账读**：2026-11-06 接线后生产链路已开始写冻结快照，
  登记数已从 0 变为非 0，任何硬编码的「0 期」都会让报告自相矛盾。

产物：`--out` 指定的 JSON（含逐份体检明细）+ 同名 `_report.md`。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ev import prefix_curve as PC                          # noqa: E402
from ev import prefix_shadow as PS                         # noqa: E402

DEFAULT_VERDICT = ROOT / "logs" / "prefix_curve_formal_ssq_K100_p360_s5.json"
DEFAULT_OUT = ROOT / "logs" / "prefix_shadow_status.json"


def _fmt(x: Any) -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "是" if x else "否"
    if isinstance(x, float):
        return "∞" if not math.isfinite(x) else f"{x:,.4g}"
    if isinstance(x, int):
        return f"{x:,}"
    return str(x)


def _cell(x: Any) -> str:
    """★ 单元格禁写裸 `|`：会被当成列分隔符撑破表格（上一轮实踩）。"""
    return _fmt(x).replace("|", "／").replace("\n", " ")


def _sep(ncols: int) -> str:
    """分隔行按列数生成，不手数 `---` 段数。"""
    return "|" + "|".join(["---"] * ncols) + "|"


def _table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |",
           _sep(len(headers))]
    for r in rows:
        assert len(r) == len(headers), f"表格列数不一致：{len(r)} != {len(headers)}"
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return out


def _load_verdict(path: Path) -> Dict[str, Any]:
    """判决层读数是门槛的唯一真源；缺文件 / 结构不对都要显式写，不许默认 0。"""
    p = Path(path)
    if not p.is_file():
        return {"_缺失": f"判决读数文件不存在：{p}"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                     # noqa: BLE001
        return {"_缺失": f"判决读数解析失败：{e}"}
    v = d.get("判决") if isinstance(d, dict) else None
    return v if isinstance(v, dict) else {"_缺失": "判决读数缺 `判决` 字段"}


def render(audit: Dict[str, Any], status: Dict[str, Any],
           *, verdict_path: Path, verdict: Dict[str, Any]) -> str:
    pre = PC.PREREGISTRATION
    L: List[str] = []
    L.append("# 前缀口径 · 影子验证接线状态（离线研究，不接生产）")
    L.append("")
    L.append("> **性质声明**：本报告不产生新读数，只回答「现有证据够不够格进入"
             "影子验证、还差什么」。台账只读取、不写入；`未开判` 是当前结论，"
             "不是失败，更不是通过。**合格登记期数以台账实际读数为准**，"
             "不硬编码数字（2026-11-06 接线后已从 0 期变为非 0）。")
    L.append("")
    L.append("## 0. 预注册与口径回显（跑数据之前固定）")
    L.append("")
    L.extend(_table(
        ["项", "值"],
        [["预注册版本", pre["版本"]],
         ["锁定日", pre["锁定日"]],
         ["性质", pre["性质"]],
         ["口径", pre["口径"]],
         ["主口径 n", pre["主口径n"]],
         ["前缀档", "、".join(map(str, pre["前缀档"]))],
         ["门槛", "、".join(pre["门槛"])],
         ["名义 α", pre["名义α"]],
         ["探索窗比例", pre["探索窗比例"]],
         ["族规模·检验数", pre["族规模"]["检验数"]],
         ["停止规则", pre["停止规则"]],
         ["判决读数来源", verdict_path.name]]))
    L.append("")
    if verdict.get("_缺失"):
        L.append(f"> ⚠ {verdict['_缺失']} ⇒ 开判门槛无法确定，"
                 "按「未开判」处理（缺席≠通过）。")
        L.append("")
    L.append(f"- {PC.COUNTERFACTUAL_NOTE}")
    L.append(f"- {PC.NOT_PURCHASED_NOTE}")
    L.append(f"- {PC.TERMINOLOGY_NOTE}")
    L.append(f"- 接线状态：{status['接线状态']}")
    L.append("")

    L.append("## 1. 开奖前冻结硬闸门")
    L.append("")
    L.append(f"> {PS.FREEZE_GATE_NOTE}")
    L.append("")
    L.append("登记（开奖前）与结算（开奖后）是两条独立记录，各自只追加、不回改；"
             "结算必须引用已登记的票序哈希，不许事后补登记或换票。")
    L.append("")

    L.append("## 2. 现有冻结快照体检（分母 = 文件总数）")
    L.append("")
    L.extend(_table(
        ["项", "值"],
        [["快照目录", audit["目录"]],
         ["文件总数（分母）", audit["文件总数"]],
         ["具备登记资格", audit["合格数"]],
         ["不合格", audit["不合格数"]],
         ["分母口径", audit["分母口径"]],
         ["开奖先后", audit["开奖先后"]]]))
    L.append("")
    L.append("**时间点性质分布**（逐份全在分母内，不筛掉后报数）：")
    L.append("")
    L.extend(_table(["时间点性质", "份数"],
                    [[k, v] for k, v in audit["时间点性质分布"].items()]
                    or [["无快照", 0]]))
    L.append("")
    L.append("**不合格原因分布**（同一份可命中多条，故合计可大于文件数）：")
    L.append("")
    L.extend(_table(["原因", "命中份数"],
                    [[k, v] for k, v in audit["原因分布"].items()]
                    or [["无", 0]]))
    L.append("")
    L.append(f"> 分母 {audit['文件总数']} 份快照全部留在分母内：其中具备登记资格 "
             f"{audit['合格数']} 份（时间点性质为生产出号「开奖前冻结」），"
             "其余为回放 / 不合格并逐份写原因；回放快照只作历史探索性证据，"
             "不能充当未来确认力。逐份明细见同批 JSON 的 `快照体检.逐份`。")
    L.append("")

    L.append("## 3. 影子台账状态")
    L.append("")
    t = status["台账"]
    ev = status["独立评估批"]
    L.extend(_table(
        ["项", "值"],
        [["台账路径", t["路径"]],
         ["文件存在", t["存在"]],
         ["总行数", t["行数"]],
         ["坏行数", t["坏行数"]],
         ["登记期数", status["登记期数"]],
         ["结算期数", status["结算期数"]],
         ["独立评估批·登记", ev["登记期数"]],
         ["独立评估批·结算", ev["结算期数"]],
         ["当前状态", status["状态"]],
         ["可开判", status["可开判"]]]))
    L.append("")
    L.append("**未开判原因**（逐条显式，缺席≠通过）：")
    L.append("")
    for i, why in enumerate(status["未开判原因"], 1):
        L.append(f"{i}. {why}")
    if not status["未开判原因"]:
        L.append("- 无（须回到预注册判据另行判决，本报告不代判）")
    L.append("")

    L.append("## 4. 开判门槛（唯一真源 = 判决层 n*_保守）")
    L.append("")
    gate = status["门槛"]
    L.extend(_table(
        ["门槛", "臂", "n*保守", "可判"],
        [[c["门槛"], c["臂"], c["n*保守"], c["可判"]]
         for c in gate.get("逐格", [])]
        or [["无可用格", "—", "—", "否"]]))
    L.append("")
    L.extend(_table(
        ["项", "值"],
        [["主口径 n", gate.get("主口径n")],
         ["门槛可用", gate["可用"]],
         ["总门槛期数", gate["门槛期数"]],
         ["有限格数", gate["有限格数"]],
         ["理论不可判格数", gate["不可判格数"]],
         ["已攒独立评估批期数", ev["结算期数"]],
         ["还需期数", status["还需期数"]],
         ["门槛来源", gate.get("来源")]]))
    L.append("")
    L.append(f"> {PS.NO_VERDICT_NOTE}")
    L.append("> `∞` 表示该档空间上界为 0，无论攒多少期都不可判；"
             "它不阻断开判，但必须单独披露，不许读成「期数不够再攒攒就行」。")
    L.append("")

    L.append("## 5. 停止规则与接线状态")
    L.append("")
    L.append(f"- {PS.STOP_RULE_NOTE}")
    L.append(f"- {PS.NOT_OPEN_NOTE}")
    L.append(f"- {PS.COUNTERFACTUAL_NOTE}")
    L.append(f"- {PS.NOT_PURCHASED_NOTE}")
    L.append(f"- 接线状态：{status['接线状态']}")
    L.append("")

    L.append("## 6. 结论")
    L.append("")
    # ★ 期数必须**从台账读**，不许硬编码：2026-11-06 接线后登记数已从 0 变成非 0，
    #   固定文案会让同一份报告里「登记期数 2」与「合格登记 0 期」自相矛盾。
    L.append(f"**{status['状态']}**。当前合格登记 {status['登记期数']} 期、"
             f"开奖后结算 {status['结算期数']} 期、"
             f"独立评估批结算 {status['独立评估批']['结算期数']} 期；"
             f"距总门槛还差 {_fmt(status['还需期数'])} 期。"
             "这不是「未检出」，更不是通过，只表示已测维度在该样本量下"
             "还没有可判证据，不构成证明。")
    L.append("")
    L.append("三条必须随结论一起读的局限：")
    L.append("")
    L.append(f"1. 生产出号链路**已接线**（`cli.py` / `web/utils.py` 两处）：当前台账 "
             f"{status['登记期数']} 期登记、{status['结算期数']} 期结算；"
             f"快照池分母 {audit['文件总数']} 份中具备登记资格 {audit['合格数']} 份，"
             "其余逐份写原因（分母不缩水）。★ 但 `批次性质` 默认为**探索批**，"
             "而「可开判」只认**独立评估批**的结算数 ⇒ 若一直按探索批攒，"
             "攒够门槛也无法开判。哪些批次算评估批必须**事先预注册**，"
             "不能事后挑。")
    L.append("2. 无逐期真实购买记录 ⇒ 仍是「假设每期买前 n 注」的反事实，不是实测战绩。")
    L.append("3. 判决层门槛来自离线三臂回放的**离线 K**；生产生效注数可调且会漂移，"
             "与离线票序不同 ⇒ 前缀读数须按届时的生产票序冻结数据后重跑，不许外推。"
             "本离线模块不读取、也不固定任何生产注数。")
    L.append("")
    L.append(f"> {PC.TERMINOLOGY_NOTE}")
    return "\n".join(L) + "\n"


def build(*, verdict_path: Path, freeze_dir: Path, shadow_path: Path) -> Dict[str, Any]:
    verdict = _load_verdict(verdict_path)
    audit = PS.audit_freeze_dir(freeze_dir)
    status = PS.shadow_status(verdict=verdict, path=shadow_path)
    text = render(audit, status, verdict_path=verdict_path, verdict=verdict)
    return {"预注册": dict(PC.PREREGISTRATION), "判决读数": verdict,
            "快照体检": audit, "影子状态": status, "报告": text}


def main() -> int:
    ap = argparse.ArgumentParser(description="前缀影子验证接线状态（离线只读）")
    ap.add_argument("--verdict", type=Path, default=DEFAULT_VERDICT)
    ap.add_argument("--freeze-dir", type=Path, default=PS.FREEZE_DIR)
    ap.add_argument("--shadow", type=Path, default=PS.SHADOW_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    data = build(verdict_path=args.verdict, freeze_dir=args.freeze_dir,
                 shadow_path=args.shadow)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    md = out.with_name(out.stem + "_report.md")
    md.write_text(data["报告"], encoding="utf-8")
    s = data["影子状态"]
    print(f"快照体检：总数 {data['快照体检']['文件总数']}，"
          f"具备登记资格 {data['快照体检']['合格数']}")
    print(f"影子状态：{s['状态']}，可开判={s['可开判']}，"
          f"还需 {_fmt(s['还需期数'])} 期")
    print(f"未开判原因 {len(s['未开判原因'])} 条")
    print(f"落盘：{out} / {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
