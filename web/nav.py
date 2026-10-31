"""
导航菜单单一真源（Single Source of Truth）

★ 为什么要有这个文件：顶栏（base.html 的 .navbar nav）和左侧栏（.sidebar）
  此前是两份手写的 HTML，加页面时只改一边，于是出现「鲁棒性只在上面有、
  左边没有」这种漂移。现在两边都由本文件的 NAV_GROUPS 渲染：
    - 顶栏 = 扁平化后的全部条目（按分组顺序）
    - 侧栏 = 分组 + 分组标题
  新增页面时只改 NAV_GROUPS 一处，两边必然同步。

字段说明：
  href   —— 路由路径
  label  —— 顶栏用的短名（≤4 字，避免顶栏过挤）
  side   —— 侧栏用的全名；缺省时回退 label
  icon   —— 侧栏 SVG 的内层 path 内容（顶栏不显示图标）
  danger —— 是否用警示红（当前无页面使用；保留字段以防回退）
  note   —— 悬停提示
"""

from __future__ import annotations

# 图标 = 24x24 viewBox 下的 SVG 内层元素字符串
_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>'
                 '<rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
    "stats": '<path d="M18 20V10M12 20V4M6 20v-6"/>',
    "pipeline": '<circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="18" r="2"/>'
                '<path d="M7 6h10M6 7.5l4.5 8.5M18 7.5l-4.5 8.5"/>',
    # 数据库/档案：圆柱 + 检索行
    "market": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>'
              '<path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    "train": '<path d="M12 3v18M5 8l7-5 7 5M5 16l7 5 7-5"/>',
    "rolling": '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>',
    "predict": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"/>',
    "compare": '<path d="M4 6h7M4 12h7M4 18h7M17 5v14"/><path d="M14 5l3-1 3 1v14l-3-1-3 1z"/>',
    "robustness": '<path d="M12 3l8 3v6c0 5-3.4 8.2-8 9-4.6-.8-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "anomaly": '<path d="M12 9v4M12 17h.01"/>'
               '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    "history": '<path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="9"/>',
    "hits": '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27'
            'l6.91-1.01L12 2z"/>',
    "records": '<path d="M8 21h8M12 17v4M7 4h10v6a5 5 0 0 1-10 0z"/>',
    "ev": '<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
    "feedback": '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.6 8.6 0 0 1-3.8-.8L3 21l1.9-5.2a8.4 8.4 0 1 1 16.1-4.3z"/>',
    "audit": '<path d="M12 9v4M12 17h.01"/>'
             '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    "ledger": '<path d="M4 20h16"/><rect x="6" y="10" width="3" height="7"/>'
              '<rect x="11" y="6" width="3" height="11"/><rect x="16" y="13" width="3" height="4"/>',
}


def _item(href, label, side=None, icon="", danger=False, note=""):
    return {
        "href": href,
        "label": label,
        "side": side or label,
        "icon": _ICONS.get(icon, ""),
        "danger": danger,
        "note": note,
    }


# ★ 唯一清单。顶栏与侧栏都从这里生成。
NAV_GROUPS = [
    {
        "title": "分析工具",
        "items": [
            _item("/", "看板", "数据看板", "dashboard", note="总览：各彩种最新期号、奖池、待办状态"),
            _item("/stats", "统计", "统计概览", "stats"),
            _item("/pipeline", "全流程", "全流程报告", "pipeline"),
            _item("/market", "数据库", "开奖数据库", "market",
                  note="逐期开奖明细：检索／筛选／排序／分省头奖／出球顺序"),
        ],
    },
    {
        "title": "建模预测",
        "items": [
            _item("/train", "训练", "训练工作台", "train"),
            _item("/rolling", "持续训练", "持续训练中心", "rolling"),
            _item("/predict", "预测", "号码预测", "predict"),
            _item("/compare", "对比", "对比分析", "compare"),
            _item("/robustness", "鲁棒性", "鲁棒性检验", "robustness"),
        ],
    },
    {
        "title": "检测 & 记录",
        "items": [
            _item("/anomaly", "异常", "异常检测", "anomaly"),
            _item("/history", "历史", "历史记录", "history"),
            _item("/hits", "命中", "命中历史", "hits"),
            _item("/records", "战绩", "策略战绩", "records"),
            _item("/ev", "期望", "期望时机", "ev"),
            _item("/feedback", "反馈", "反馈闭环", "feedback"),
            _item("/audit", "证据看板", "测量与证据看板", "audit",
                  note="开奖质量 · EV · P/L · 距离与实得画像 · A/B 对照 · 中奖归因 · 机制缝隙（只读，不触发测量）"),
            _item("/ledger", "账本", "盈利账本", "ledger",
                  note="每期成本 / 奖金 / 净盈亏 / 回撤 + 同预算影子对照（只读，不写盘、不触发重建）"),
        ],
    },
]


def nav_flat():
    """顶栏用：把分组压平成一维列表（保持分组顺序）。"""
    out = []
    for g in NAV_GROUPS:
        out.extend(g["items"])
    return out


def nav_lookup():
    """按 href 索引，便于模板/测试查询。"""
    return {it["href"]: it for it in nav_flat()}
