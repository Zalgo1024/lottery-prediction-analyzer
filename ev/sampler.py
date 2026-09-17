"""
ev/sampler.py —— 约束采样出号（任务 2.4，方案B 点5+6 的落点）

不声称"哪个号更可能出"（裁判层已证伪预测），只做两件可辩护的事：
① 反大众：避开生日号/顺子/对称/重复等公众偏好 → 降低撞号（七星彩浮动头奖被分摊风险）；
   有 crowd 模型时直接用「拥挤分位」打分（数据驱动），无模型时用启发式模式惩罚。
② 形态约束：按用户风险形态出号（和值/奇偶比/区间分布/最大连号长度）。
③ 多组低重叠：组间数字重叠最小化，提高覆盖面。

接口：
  sample_tickets(name, n=5, avoid_popular=True, constraints=None, seed=None)
  constraints: {"sum_range": (a,b), "odd_range": (a,b), "max_repeat": k,
                "max_consec": k, "zone_spread": n}
"""
import random

_CROWD_CACHE = {}


def _crowd(name):
    """人群模型：与 ev/engine 同口径（v2 Poisson MLE 优先，退化 v1）。

    ⚠️ 曾直接 fit_crowd_model(v1)：七星彩一等奖注数 0/1/2 的 log-最小二乘
    是噪声模型，且与 engine 的撞号分薄(v2)口径不一致（2026-08-31 修复）。
    """
    if name not in _CROWD_CACHE:
        from ev.engine import _get_crowd
        _CROWD_CACHE[name] = _get_crowd(name)
    return _CROWD_CACHE[name]


# ---------------- 启发式公众偏好检测（无 crowd 时兜底） ----------------
def _popularity_heuristic(nums: list) -> int:
    """返回公众偏好惩罚分（越大越大众）。基于模式：生日/顺子/对称/重复/吉利。"""
    pen = 0
    s = [int(d) for d in nums]
    # 生日模式（前2位=01-12 月，中2位=01-31 日）→ 大众
    if len(s) >= 4 and 1 <= s[0] * 10 + s[1] <= 12 and 1 <= s[2] * 10 + s[3] <= 31:
        pen += 3
    # 全重复（AAA / AAAAA）
    if len(set(s)) == 1:
        pen += 3
    # 顺子（012 / 123...）
    for i in range(len(s) - 2):
        if s[i] + 1 == s[i + 1] and s[i + 1] + 1 == s[i + 2]:
            pen += 2
            break
    # 对称（回文）
    if s == s[::-1]:
        pen += 2
    # 吉利数字（8 多）
    if sum(1 for d in s if d == 8) >= 2:
        pen += 1
    # 头尾相同 / 双连
    if len(s) >= 2 and s[0] == s[-1]:
        pen += 1
    return pen


# ---------------- 形态约束 ----------------
def _check_constraints(nums: list, constraints: dict) -> bool:
    s = [int(d) for d in nums]
    total = sum(s)
    odds = sum(1 for d in s if d % 2 == 1)
    # 和值
    sr = constraints.get("sum_range")
    if sr and not (sr[0] <= total <= sr[1]):
        return False
    # 奇偶比
    orr = constraints.get("odd_range")
    if orr and not (orr[0] <= odds <= orr[1]):
        return False
    # 最大重复数字次数
    mr = constraints.get("max_repeat")
    if mr and max(s.count(d) for d in set(s)) > mr:
        return False
    # 最大连号长度
    mc = constraints.get("max_consec")
    if mc:
        best = 1
        for i in range(1, len(s)):
            best = best + 1 if s[i] == s[i - 1] + 1 else 1
            if best > mc:
                return False
    return True


# ---------------- 组间重叠 ----------------
# ⚠️ 已删除 _overlap(a, b) = len(set(a) & set(b))。
# 它是死代码（全项目无调用点），且语义对数字型是错的：set 会丢掉位置信息，
# "123" vs "321" 会被算成 overlap=3，而实际同位同号只有 1 位（第 2 位的 2）。
# 数字型的重叠一律按位比较，见下方 sample_tickets 内的
# sum(1 for i in range(n_pos) if cand[i] != t[i])。


def sample_tickets(name: str, n: int = 5, avoid_popular: bool = True,
                   constraints: dict = None, min_group_gap: int = 2,
                   seed: int = None) -> dict:
    """
    约束采样出号。
    name: 彩种（排列3/福彩3D/排列5/七星彩）
    n: 组数
    avoid_popular: 反大众开关
    constraints: 形态约束 {sum_range, odd_range, max_repeat, max_consec}
    min_group_gap: 任意两组最小数字重叠数（数字型每位0-9，gap按位不同数计）
    """
    rng = random.Random(seed)
    zones = [10] * (5 if name == "排列5" else 3)
    if name == "七星彩":
        zones = [10] * 6 + [15]
    n_pos = len(zones)
    constraints = constraints or {}

    model = _crowd(name)

    tickets = []
    attempts = 0
    while len(tickets) < n and attempts < 5000:
        attempts += 1
        # 随机生成
        cand = [rng.randrange(z) for z in zones]
        # 反大众：crowd 分位 < 0.5（冷门半区）或启发式惩罚 < 阈值
        if avoid_popular:
            if model is not None:
                if model.crowding_quantile(cand) > 0.5:
                    continue
            else:
                if _popularity_heuristic(cand) >= 3:
                    continue
        # 形态约束
        if not _check_constraints(cand, constraints):
            continue
        # 组间低重叠（按位不同数 ≥ min_group_gap）
        if tickets:
            if all(sum(1 for i in range(n_pos) if cand[i] != t[i]) >= min_group_gap
                   for t in tickets):
                tickets.append(cand)
        else:
            tickets.append(cand)

    # 补充：约束过严导致不足 n 组时，放宽反大众（保证至少 n 组）
    if len(tickets) < n and avoid_popular:
        while len(tickets) < n and attempts < 10000:
            attempts += 1
            cand = [rng.randrange(z) for z in zones]
            if not _check_constraints(cand, constraints):
                continue
            if tickets and all(sum(1 for i in range(n_pos) if cand[i] != t[i]) >= min_group_gap
                               for t in tickets):
                tickets.append(cand)
            elif not tickets:
                tickets.append(cand)

    rows = []
    for t in tickets:
        q = model.crowding_quantile(t) if model is not None else None
        rows.append({
            "号码": "".join(str(d) for d in t),
            "拥挤分位": round(q, 4) if q is not None else None,
            "大众惩罚(启发式)": _popularity_heuristic(t),
            "和值": sum(t),
            "奇偶": f"{sum(1 for d in t if d%2==1)}奇/{sum(1 for d in t if d%2==0)}偶",
        })

    return {
        "彩种": name,
        "组数": len(rows),
        "约束": constraints,
        "反大众": avoid_popular,
        "号码组": rows,
        "说明": "约束采样：不预测开奖，只降低撞号概率+按形态出号（数字型每位0-9）",
    }


if __name__ == "__main__":
    import json, sys
    name = sys.argv[1] if len(sys.argv) > 1 else "排列3"
    r = sample_tickets(name, n=5, constraints={"sum_range": (9, 18), "max_consec": 2})
    print(json.dumps(r, ensure_ascii=False, indent=1))
