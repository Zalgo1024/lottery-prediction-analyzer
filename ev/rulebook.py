"""
ev/rulebook.py —— 赔率表 + 概率（名义 EV 的地基）

数据依据（官方规则，2026-08-25 录入）：
- 排列3 / 福彩3D：直选 1040 / 1‰，组选3 346 / 3‰，组选6 173 / 6‰（每注 2 元）
- 排列5：**只设 1 个奖级**，直选 100000 / 1‱(1/10万)（每注 2 元）。
  ⚠️ 没有"组选"奖级：六形态是「直选全组合」投注方式(打包买下该形态全部排列)，
  中奖仍按直选 100000 兑 1 注；240/120/60/40/20/10 = 排列数×2元 = 投注成本而非奖金。
  直选单注奖金受《风险控制办法》约束，井喷时 = 最高返奖总额/中注数（如 55555 期实付 84034）。
- 七星彩（2020-10-11 起，前6位 000000-999999 + 后区 0-14，总组合 10^6×15=1500 万）：
    ⚠️ 按「任意位置匹配位数」计奖，**不要求连续**（"连续位数"是 2004-2020 旧版）
    设 X=前6位命中数，Y=后区是否命中，T=X+Y（不兼中兼得，取最高奖级）
    一等奖 T=7（浮动，名义上限 500 万）
    二等奖 X=6 且 Y=0，即前6位全中（浮动）
    三等奖 X=5 且 Y=1 → 3000
    四等奖 T=5 → 500
    五等奖 T=4 → 30
    六等奖 T>=3 或 Y=1 → 5（含"仅后区中"与"前6位任意1位+后区"）

名义 EV = -cost + Σ payout_k × p_k（无任何打折，纯数学期望）
"""
from typing import Dict

COST = 2  # 单注 2 元

# 概率计算函数
def _prob_pl3(name: str) -> Dict[str, float]:
    return {"直选": 1 / 1000, "组选3": 3 / 1000, "组选6": 6 / 1000}


def _prob_pl5() -> Dict[str, float]:
    """排列5 只设 1 个奖级：直选（5 位全对且顺序一致），1/10 万。

    ⚠️ 六形态(五不同/二同/两组二同/三同/三同二同/四同)是「直选全组合」投注方式，
    不是独立奖级：中奖概率 = 该形态排列数/10 万，但赔付仍是直选 100000（只中 1 注），
    成本 = 排列数×2 元 → EV = 100000×(k/1e5) - 2k = -k，
    EV 率 -50%，与单注 EV 率完全相同，不改善期望。
    """
    return {"直选": 1 / 100000}


def _prob_qxc() -> Dict[str, float]:
    """七星彩各奖级概率（前6位 000000-999999 + 后区 0-14，分母 15,000,000）。

    官方中奖注数（已用组合数自证，精确吻合）：
        一等 1 / 二等 14 / 三等 54 / 四等 1971 / 五等 31590 / 六等 1188270
    判定：X=前6位命中数，Y=后区是否命中，T=X+Y
        一等 T=7；二等 X=6,Y=0；三等 X=5,Y=1；四等 T=5；五等 T=4；六等 T>=3 或 Y=1
    ⚠️ 旧值 9/207/1863/597051（分母1.5亿）比官方小 20~170 倍，已弃用。
    """
    d = 15000000
    return {
        "一等奖": 1 / d,
        "二等奖": 14 / d,
        "三等奖": 54 / d,
        "四等奖": 1971 / d,
        "五等奖": 31590 / d,
        "六等奖": 1188270 / d,
    }


def _prizes_pl3d() -> Dict[str, Dict]:
    return {
        "直选": {"cost": COST, "payout": 1040},
        "组选3": {"cost": COST, "payout": 346},
        "组选6": {"cost": COST, "payout": 173},
    }


def _prizes_qxc() -> Dict[str, Dict]:
    return {
        "一等奖": {"cost": COST, "payout": 5000000, "浮动": True},
        "二等奖": {"cost": COST, "payout": 0, "浮动": True},
        "三等奖": {"cost": COST, "payout": 3000},
        "四等奖": {"cost": COST, "payout": 500},
        "五等奖": {"cost": COST, "payout": 30},
        "六等奖": {"cost": COST, "payout": 5},
    }


def _prizes_pl5_payout() -> Dict[str, Dict]:
    """排列5 只设 1 个奖级：直选 100000（5 位全对且顺序一致）。

    ⚠️ 240/120/60/40/20/10 不是奖金，而是「直选全组合」各形态的**投注成本**
       （排列数 120/60/30/20/10/5 × 2 元）。曾误作 payout 录入，已删除。
    """
    return {
        "直选": {"cost": COST, "payout": 100000},
    }


PAYOUT_TABLE = {
    "排列3": {**{k: {"cost": v["cost"], "payout": v["payout"]} for k, v in _prizes_pl3d().items()},
              "_prob": _prob_pl3("排列3")},
    "福彩3D": {**{k: {"cost": v["cost"], "payout": v["payout"]} for k, v in _prizes_pl3d().items()},
               "_prob": _prob_pl3("福彩3D")},
    "排列5": {**_prizes_pl5_payout(), "_prob": _prob_pl5()},
    "七星彩": {**{k: {"cost": v["cost"], "payout": v["payout"], **({"浮动": True} if v.get("浮动") else {})}
                  for k, v in _prizes_qxc().items()},
               "_prob": _prob_qxc()},
}


def get_rulebook(name: str) -> Dict:
    """返回 {彩种: {奖级: {cost, payout(名义), prob}, ...}}（剥离内部 _prob 键，合并进各奖级）"""
    if name not in PAYOUT_TABLE:
        raise ValueError(f"rulebook 未收录彩种: {name}")
    rb = PAYOUT_TABLE[name]
    probs = rb["_prob"]
    return {k: {**v, "prob": probs[k]} for k, v in rb.items() if not k.startswith("_")}


def nominal_ev(name: str, prize: str = None,
               prize_payout_override: Dict[str, float] = None) -> Dict:
    """
    名义 EV（每注 2 元，纯数学期望，无打折）。

    票型规则：
    - 排列3/福彩3D：一注属于某玩法（直选/组选3/组选6），EV = -2 + payout×prob，prize 必填。
    - 排列5：直选 EV = -2 + 100000×(1/10万) = -1.0；另有组选六形态（五不同/二同/两组二同/三同/三同二同/四同），EV 均约 -1.9~-1.98（固定赔率，选号不改期望）。直选单注奖金受风险控制约束。
    - 七星彩：一注同时参与所有奖级（按命中位数互斥落级），EV = -2 + Σ payout_k×p_k。

    prize_payout_override：覆盖浮动奖级（如读当期 一等奖单注奖金），key=奖级名。
    返回 {"ticket_type", "ev_per_ticket", "detail": {...}}
    """
    rb = get_rulebook(name)
    if name == "七星彩":
        detail, ev = {}, -COST
        for k, v in rb.items():
            payout = prize_payout_override.get(k, v["payout"]) if prize_payout_override else v["payout"]
            contrib = payout * v["prob"]
            ev += contrib
            detail[k] = {"payout": payout, "prob": v["prob"], "contribution": round(contrib, 6)}
        return {"ticket_type": "单票(多奖级)", "ev_per_ticket": round(ev, 4), "detail": detail}

    # 3D 类 / 排列5：prize 指定玩法
    if prize is None:
        # 未指定 → 返回全部玩法各自的单注 EV
        out = {}
        for k, v in rb.items():
            payout = prize_payout_override.get(k, v["payout"]) if prize_payout_override else v["payout"]
            out[k] = round(-v["cost"] + payout * v["prob"], 4)
        return {"ticket_type": "多玩法(按注)", "ev_per_ticket": out, "detail": out}
    if prize not in rb:
        raise ValueError(f"{name} 无玩法: {prize}（可选 {list(rb.keys())}）")
    v = rb[prize]
    payout = prize_payout_override.get(prize, v["payout"]) if prize_payout_override else v["payout"]
    ev = -v["cost"] + payout * v["prob"]
    return {
        "ticket_type": prize,
        "ev_per_ticket": round(ev, 4),
        "detail": {prize: {"payout": payout, "prob": v["prob"], "contribution": round(payout * v["prob"], 6)}},
    }


if __name__ == "__main__":
    for name in ["排列3", "福彩3D", "排列5", "七星彩"]:
        if name == "七星彩":
            r = nominal_ev(name)
            print(f"[{name}] 单票名义EV = {r['ev_per_ticket']} 元")
            for k, v in r["detail"].items():
                print(f"    {k}: 赔付{v['payout']} × p={v['prob']:.3e} → {v['contribution']} 元")
        else:
            r = nominal_ev(name)
            for k, v in r["detail"].items():
                print(f"[{name}] {k} 名义EV = {v} 元")
        print()
