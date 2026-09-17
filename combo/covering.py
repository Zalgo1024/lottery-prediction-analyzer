"""
combo/covering.py —— P3-1 覆盖设计（路线图 L3）

要回答的问题："预算 N 注，怎么选能让『保底』最高？"——纯确定性组合数学，
**完全不依赖预测**：选一组 k 元子集（票），使任意开出的 t 元子集都被至少
一票包含（覆盖设计 C(v,k,t)），从而对任意开奖给出『至少一注命中 ≥t 个红球』
的**确定性保证**。

方法（贪心 + 冗余修剪，不引 OR-Tools 重依赖）：
1. greedy_build：从空集开始，每步采样候选票（随机 t-子集打底 + 按"新增覆盖
   t-子集数"贪心补到 k 个号），加入覆盖增量最大的票，直到预算用尽或全覆盖。
2. trim：全覆盖达成后，尝试逐票删除（删除后仍全覆盖即冗余），收敛出紧凑解。
3. partial：预算 < 满覆盖注数 → 输出覆盖率（覆盖的 t-子集占比）与说明
   （此时是概率性而非确定性——诚实标注）。

诚实口径（对齐项目其余模块）：
- 覆盖设计的目标是**保底/最坏情形**，会牺牲命中大奖的分散度与期望——与
  --groups 随机撒注**并存**，别混用（随机模式期望小奖票数更高）。
- ⚠️『≥t 红』≠ 中奖：双色球 3+0、大乐透 3+0 都不是奖级。给完整游戏的保底
  需把红区 t-子集与蓝区号码**联合**覆盖（注数按 ×蓝号数 量级上升），
  本模块对红区给确定保证、对联合/奖级给出诚实说明而不夸大。
- 适用：乐透型红区（双色球 33/6、大乐透 35/5）。数字型每期按位独立、固定
  赔率，无"组合覆盖"语义，返回不适用说明。
"""
import itertools
import math
import random

import numpy as np

# ---------------------------------------------------------------- 覆盖核心
def _triples_of_ticket(ticket, t: int):
    """票（k 元子集，0-based 升序）所含全部 t-子集 → frozenset 编码集合。"""
    return set(itertools.combinations(ticket, t))


def _greedy_build(v: int, k: int, t: int, budget: int, rng,
                  candidates: int = 24, seed_triples: int = 8) -> tuple:
    """贪心构建：预算内覆盖尽可能多的 t-子集。

    返回 (tickets, covered_set)。covered_set: frozenset 的 t-子集 → 已覆盖全集。
    """
    tickets = []
    covered = set()
    universe_size = math.comb(v, t)
    vals = list(range(v))
    while len(tickets) < budget and len(covered) < universe_size:
        best_ticket, best_gain = None, -1
        # 候选票：以随机未覆盖 t-子集打底，再贪心补号（每次选新增覆盖最大者）
        uncovered_pool = list(set(itertools.combinations(range(v), t)) - covered)
        base_pool = rng.sample(uncovered_pool,
                               min(seed_triples, len(uncovered_pool)))
        for base in base_pool:
            ticket = list(base)
            have = set(ticket)
            while len(ticket) < k:
                best_num, best_num_gain = None, -1
                for num in rng.sample([x for x in vals if x not in have],
                                      min(candidates, v - len(have))):
                    cand = ticket + [num]
                    gain = sum(1 for tr in itertools.combinations(cand, t)
                               if tr not in covered)
                    if gain > best_num_gain:
                        best_num, best_num_gain = num, gain
                if best_num is None:      # 理论不可达（k<t 等）保护
                    break
                ticket.append(best_num)
                have.add(best_num)
            if len(ticket) == k:
                tt = tuple(sorted(ticket))
                gain = sum(1 for tr in itertools.combinations(tt, t)
                           if tr not in covered)
                if gain > best_gain:
                    best_gain, best_ticket = gain, tt
        if best_ticket is None or best_gain <= 0:
            # 无增量候选（全覆盖或退化）——兜底随机补一张去重票
            pool = [x for x in vals]
            tt = tuple(sorted(rng.sample(pool, k)))
            if tt in tickets:
                break
            best_ticket, best_gain = tt, 0
        tickets.append(best_ticket)
        covered |= _triples_of_ticket(best_ticket, t)
    return tickets, covered


def _trim(v: int, k: int, t: int, tickets, covered, full: int,
          rng, rounds: int = 3) -> list:
    """冗余修剪：删除后仍保持全覆盖的票逐张移除（需 covered 恰好=全集）。"""
    if len(covered) < full:
        return tickets
    cur = list(tickets)
    for _ in range(rounds):
        removed_any = True
        while removed_any:
            removed_any = False
            order = list(range(len(cur)))
            rng.shuffle(order)
            for i in order:
                rest = cur[:i] + cur[i + 1:]
                if len(rest) == 0:
                    continue
                cov = set()
                for tk in rest:
                    cov |= _triples_of_ticket(tk, t)
                if len(cov) == full:      # 该票冗余 → 删除
                    cur.pop(i)
                    removed_any = True
                    break
    return cur


def covering_tickets(v: int, k: int, t: int, budget: int = None,
                     seed: int = 0) -> dict:
    """通用覆盖设计：返回 {tickets, full(是否全覆盖), 覆盖数, 覆盖率, 理论下界}。

    - budget=None：自动贪心到全覆盖并修剪 → 紧凑满覆盖（返回实际用票数）。
    - budget<N_full：部分覆盖（概率性，诚实标注）。
    """
    if not (2 <= t < k < v):
        raise ValueError(f"需满足 2<=t<k<v，got t={t},k={k},v={v}")
    rng = random.Random(seed)
    full = math.comb(v, t)
    lb = math.ceil(full / math.comb(k, t))      # 覆盖数理论下界
    tickets, covered = _greedy_build(v, k, t, budget or full + 10000,
                                     rng)
    if len(covered) == full:
        tickets = _trim(v, k, t, tickets, covered, full, rng)
        # 修剪后若预算受限需回切：budget 模式保留前 budget 张
        if budget and len(tickets) > budget:
            # 修剪得到的是满覆盖紧凑解，若仍超预算则不可满覆盖 → 回退贪心部分解
            tickets, covered = _greedy_build(v, k, t, budget, rng)
    n_covered = len(covered)
    return {
        "v": v, "k": k, "t": t,
        "覆盖空间": full,
        "已覆盖": n_covered,
        "覆盖率": round(n_covered / full, 4),
        "满覆盖": bool(n_covered == full),
        "理论下界注数": lb,
        "实际注数": len(tickets),
        "tickets": tickets,
    }


# ---------------------------------------------------------------- 彩种级
def plan(lottery: str, t: int = 3, budget: int = None, seed: int = 0,
         mc_draws: int = 2000) -> dict:
    """乐透型红区覆盖出号方案 + 诚实保证说明。

    budget=None → 输出紧凑满覆盖（保底任意开奖 ≥t 红）；否则按预算部分覆盖。
    """
    from data.schema import get_schema
    if lottery not in ("双色球", "大乐透"):
        return {"applicable": False,
                "reason": f"{lottery} 为数字型/固定赔率，无组合覆盖语义"
                          f"（每期按位独立，保底概念不适用）"}
    schema = get_schema(lottery)
    red = schema.red_zone
    if t >= red.choose:
        return {"applicable": False,
                "reason": f"t={t} ≥ 每票红球数 {red.choose}：保底 t 红需单票全中"
                          f"该 t 个号，覆盖无意义"}
    rep = covering_tickets(red.max - red.min + 1, red.choose, t,
                           budget=budget, seed=seed)
    v, k = rep["v"], rep["k"]
    tickets = rep.pop("tickets")
    rep["tickets"] = [
        tuple(sorted(ti + red.min for ti in tk)) for tk in tickets]
    rep["彩种"] = lottery
    rep["分区"] = red.name
    # 蓝区策略：满覆盖时均匀轮转蓝号（使每个蓝号至少出现一次，N≥蓝号数时）
    blue = schema.blue_zone
    if blue is not None:
        bl = list(range(blue.min, blue.max + 1))
        rep["蓝号轮转"] = "每票轮流配蓝号（从蓝1号起循环）；蓝号数与红票数独立"
    # 随机同预算对比（精确覆盖率，无需 MC 即可算）
    if budget:
        rng = random.Random(seed + 1)
        n_rand = min(budget, 5000)
        rand_covered = set()
        full_space = math.comb(v, t)
        for _ in range(n_rand):
            tk = tuple(sorted(rng.sample(range(v), k)))
            rand_covered |= _triples_of_ticket(tk, t)
        f_rand = len(rand_covered) / full_space
        rep["随机对比"] = {
            "随机同注数覆盖率": round(f_rand, 4),
            "覆盖增益": round(rep["覆盖率"] - f_rand, 4),
        }
    # MC 验证：随机抽期数，统计每期"≥1 票命中 ≥t 红"的最差/平均票数
    # （用 0..v-1 位掩码 + bit_count，避免逐票 set 交集拖慢大票集）
    if rep["满覆盖"] and mc_draws > 0:
        rng2 = random.Random(seed + 2)
        ticket_masks = []
        # tickets 局部变量为 0..v-1 基（与抽样位掩码同空间），直接 1<<x，
        # 勿再减 red.min（min=1 时票内 0 号 → 1<<-1 负移位崩溃）
        for tk in tickets:
            m = 0
            for x in tk:
                m |= 1 << x
            ticket_masks.append(m)
        worst = None
        avg = 0.0
        for _ in range(mc_draws):
            dm = 0
            for x in rng2.sample(range(v), k):
                dm |= 1 << x
            hits = sum(1 for tm in ticket_masks
                       if (dm & tm).bit_count() >= t)
            avg += hits
            worst = hits if worst is None else min(worst, hits)
        rep["MC验证"] = {
            "随机抽期": mc_draws,
            "每期≥t红票数-最差": worst,
            "每期≥t红票数-平均": round(avg / mc_draws, 2),
            "结论": ("确定性保证成立：任意抽样期 ≥1 票命中 ≥t 红"
                    if worst and worst >= 1 else "⚠️ MC 发现未覆盖情形"),
        }
    # 诚实说明
    note = (f"覆盖设计保证『任意开奖至少一注红球命中 ≥{t} 个』（t={t}），"
            f"纯确定性，与预测无关。")
    if lottery == "双色球":
        note += (f"⚠️ 双色球 3+0 不是奖级：本保证是红区命中地板，不等于中奖；"
                 f"要保底 六等奖(2+1 等) 需把红 t-子集与蓝号联合覆盖"
                 f"（注数≈红覆盖×蓝号数量级）")
    if lottery == "大乐透":
        note += (f"⚠️ 大乐透 3+0 属九等奖(5元)，本保证（红 ≥{t}）可直接对应"
                 f"部分奖级，但完整奖级表仍依赖后区。")
    rep["诚实说明"] = note
    return rep


if __name__ == "__main__":
    import json
    import sys
    lottery = sys.argv[1] if len(sys.argv) > 1 else "双色球"
    t = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    budget = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    r = plan(lottery, t=t, budget=(budget or None))
    print(json.dumps({k: v for k, v in r.items() if k != "tickets"},
                     ensure_ascii=False, indent=1))
    if "tickets" in r:
        print(f"票数 {len(r['tickets'])}，前 8 张: "
              + "; ".join(map(str, r["tickets"][:8])))
