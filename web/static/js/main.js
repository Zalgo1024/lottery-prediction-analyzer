/* Web 前端交互脚本 */

// ===== 工具函数 =====
async function api(url, opts = {}) {
    try {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...opts
        });
        const ctype = (resp.headers.get('content-type') || '').toLowerCase();
        const isHtml = ctype.includes('text/html');
        if (!resp.ok || isHtml) {
            const text = await resp.text().catch(() => '');
            const firstLine = text.replace(/\n/g, ' ').slice(0, 120);
            if (resp.status === 404) {
                throw new Error(`接口未找到（404）：${url}。如果刚加新接口，请先重启 Flask 服务后再试。`);
            }
            throw new Error(`HTTP ${resp.status}${isHtml ? '（返回了 HTML 页面）' : ''}：${firstLine || resp.statusText}`);
        }
        return resp.json();
    } catch (e) {
        // 网络层失败：Flask 没启动、端口被占用、CORS 等
        if (e.name === 'TypeError' || String(e.message).includes('Failed to fetch')) {
            throw new Error(`无法连接后端服务（${url}）。请先双击「启动项目.bat」启动 Flask 后再试。`);
        }
        throw e;
    }
}

function $(id) { return document.getElementById(id); }

function show(el) { if (typeof el === 'string') el = $(el); if (el) el.classList.remove('hidden'); }
function hide(el) { if (typeof el === 'string') el = $(el); if (el) el.classList.add('hidden'); }

function html(el, content) { if (typeof el === 'string') el = $(el); if (el) el.innerHTML = _fixEmoji(content); }

// 策略名 → 展示徽标：ML策略 用 SVG 机器人图标，避免 emoji 在 Windows 系统字体里丢失
function _strategyBadge(name) {
    const n = name || '';
    if (n === 'ML策略') {
        const robotSvg = ico('robot').replace('class="ico"', 'style="width:14px;height:14px;margin-right:0;vertical-align:middle;flex-shrink:0"');
        return '<span title="训练好的机器学习/统计模型，按反馈权重参与预测" style="display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:11px;font-size:12px;font-weight:700;background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe">' + robotSvg + ' ML策略</span>';
    }
    return '<strong>' + n + '</strong>';
}

// 内联 SVG 图标库（不依赖系统 emoji 字体，任何环境都能渲染）
// viewBox 统一 0 0 24 24；多数用 currentColor 描边以跟随文字色，红蓝圈固定颜色以区分奖球
const ICO_SVG = {
    board: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8" rx="1"/><rect x="12" y="6" width="3" height="12" rx="1"/><rect x="17" y="13" width="3" height="5" rx="1"/></svg>',
    trend: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>',
    inbox: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M5 5h14v14H5z"/><path d="M3 12h5l2 3h4l2-3h5"/></svg>',
    target: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/></svg>',
    robot: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="5" y="8" width="14" height="11" rx="2"/><path d="M12 4v4M9 13h.01M15 13h.01M9 16h6"/><path d="M3 12v3M21 12v3"/></svg>',
    clipboard: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4V3h6v1"/><path d="M9 9h6M9 13h6M9 17h6"/></svg>',
    redCircle: '<svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#e53935"/></svg>',
    blueCircle: '<svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#1e88e5"/></svg>',
    refresh: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
    refreshCw: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>',
    party: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg>',
    fire: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3c2 3 4 4 4 8a4 4 0 0 1-8 0c0-1 .5-2 1-3 .5 1 1 1.5 1 2 0-2 1-4 2-7z"/></svg>',
    megaphone: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 11v2l13 4V7L3 11z"/><path d="M16 8a4 4 0 0 1 0 8"/><path d="M3 13h2"/></svg>',
    billiard: '<svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#222"/><circle cx="9" cy="9" r="1.2" fill="#fff"/><circle cx="14.5" cy="10" r="1.2" fill="#fff"/><circle cx="10" cy="14.5" r="1.2" fill="#fff"/></svg>',
    search: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>',
    scroll: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 4h11a2 2 0 0 1 2 2v12H6z"/><path d="M6 4a2 2 0 0 0-2 2v0a2 2 0 0 0 2 2h2"/></svg>',
    page: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
    trophy: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M8 4h8v4a4 4 0 0 1-8 0V4z"/><path d="M8 5H5v2a3 3 0 0 0 3 3M16 5h3v2a3 3 0 0 1-3 3"/><path d="M12 12v4M9 20h6M10 16h4l-1 4h-2z"/></svg>',
    microscope: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 20h12"/><path d="M12 4l4 7-5 3-4-7z"/><path d="M12 14l-3 6"/></svg>',
    bolt: '<svg class="ico" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M13 2L4 14h6l-1 8 9-12h-6z"/></svg>',
    pin: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 21s7-6 7-11a7 7 0 0 0-14 0c0 5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>',
    spy: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><circle cx="12" cy="7" r="3"/><path d="M5 21c0-4 3-6 7-6s7 2 7 6"/><path d="M9 13h6"/></svg>',
    pushpin: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M9 3h6l-1 7 3 3H7l3-3-1-7z"/><path d="M12 13v8"/></svg>',
    link: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 14a4 4 0 0 0 6 .5l2-2a4 4 0 0 0-6-6l-1 1"/><path d="M14 10a4 4 0 0 0-6-.5l-2 2a4 4 0 0 0 6 6l1-1"/></svg>',
    ruler: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M4 20L20 4M7 17l-3 3M11 13l-3 3M15 9l-3 3M19 5l-3 3"/></svg>',
    rocket: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3c3 2 5 6 5 10l-3 3H10l-3-3c0-4 2-8 5-10z"/><circle cx="12" cy="9" r="1.5"/><path d="M9 19l-2 2M15 19l2 2"/></svg>',
    dumbbell: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 9v6M6 7v10M18 7v10M21 9v6M6 12h12"/></svg>',
    chartUp: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 7-7"/><path d="M14 8h7v7"/></svg>',
    hourglass: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M7 3h10M7 21h10M7 3c0 5 5 5 5 9s-5 4-5 9M17 3c0 5-5 5-5 9s5 4 5 9"/></svg>',
    down: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M6 13l6 6 6-6"/></svg>',
    scales: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3v18M5 7h14"/><path d="M5 7l-2 6h4zM19 7l-2 6h4z"/></svg>',
    check: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg>',
    cross: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    warning: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg>',
};

function ico(name) { return ICO_SVG[name] || ''; }

// emoji 字符 → 图标名 映射（覆盖扫描发现的所有彩色 emoji 与偏风险符号）
const EMOJI_MAP = {
    '📊': 'board', '🤖': 'robot', '📋': 'clipboard', '🔴': 'redCircle', '🔵': 'blueCircle',
    '🔄': 'refresh', '🎯': 'target', '📥': 'inbox', '🎉': 'party', '🔥': 'fire',
    '📣': 'megaphone', '🎱': 'billiard', '🔍': 'search', '📜': 'scroll', '📄': 'page',
    '🏆': 'trophy', '🔬': 'microscope', '⚡': 'bolt', '📍': 'pin', '🕵': 'spy',
    '📌': 'pushpin', '🔗': 'link', '📐': 'ruler', '🚀': 'rocket', '🏋': 'dumbbell',
    '🔁': 'refreshCw', '📈': 'chartUp', '⏳': 'hourglass',
    '⬇️': 'down', '⚠️': 'warning', '⚖️': 'scales',
    '✅': 'check', '❌': 'cross',
};

// 把字符串里的 emoji 统一替换为内联 SVG，避免 Windows 缺字体时显示成方框
// 同时把少数在 textContent 里出现的箭头类替换为纯文本安全字符
function _fixEmoji(text) {
    if (!text) return text;
    let out = '';
    const s = String(text);
    for (let i = 0; i < s.length; i++) {
        const ch = s[i];
        const pair = ch + (s[i + 1] || '');
        if (EMOJI_MAP[pair]) { out += ico(EMOJI_MAP[pair]); i++; continue; }
        if (EMOJI_MAP[ch]) { out += ico(EMOJI_MAP[ch]); continue; }
        if (ch === '⬇') { out += '↓'; continue; }
        if (ch === '⏳') { out += '…'; continue; }
        out += ch;
    }
    return out;
}

// ===== 号码可视化工具：把红球/蓝球统一渲染成带标签的徽章 =====
function toBallArray(v) {
    if (Array.isArray(v)) return v.map(n => String(n));
    if (typeof v === 'string') {
        const s = v.replace(/\s+/g, '');
        const arr = [];
        for (let i = 0; i < s.length; i += 2) arr.push(s.slice(i, i + 2));
        return arr;
    }
    return [];
}

function renderBalls(red, blue, size = 24, showLabels = true) {
    const reds = toBallArray(red);
    const blues = toBallArray(blue);
    const fs = Math.max(10, Math.round(size * 0.44));
    const labelStyle = 'font-size:12px;color:var(--gray);white-space:nowrap;';
    const ballStyle = `display:inline-flex;align-items:center;justify-content:center;width:${size}px;height:${size}px;border-radius:50%;font-weight:700;font-size:${fs}px;margin:1px;box-shadow:0 1px 2px rgba(0,0,0,0.12);`;
    let out = '<span style="display:inline-flex;align-items:center;flex-wrap:wrap;gap:2px 1px;">';
    if (showLabels) out += `<span style="${labelStyle}margin-right:4px;">红球</span>`;
    out += reds.map(n => `<span class="red-ball" style="${ballStyle}">${String(n).padStart(2, '0')}</span>`).join('');
    if (blues.length) {
        if (showLabels) out += `<span style="${labelStyle}margin-left:8px;margin-right:4px;">蓝球</span>`;
        out += blues.map(n => `<span class="blue-ball" style="${ballStyle}">${String(n).padStart(2, '0')}</span>`).join('');
    }
    out += '</span>';
    return out;
}

// ===== 通用分区渲染（方向A：红蓝 ↔ 数字型统一）=====
// zones: [{name, pred, actual, hit, choose, cls}]，cls="red"/"blue" 走红蓝球样式，否则按调色板着色。
// field: 渲染哪个字段（"pred" 预测球 / "actual" 实际球）。
// opts.hitHighlight: 预测球是否按"是否命中实际"做暗化（战绩页用）。
const ZONE_PALETTE = ['#E24B4A', '#1E90FF', '#2E9E5B', '#E0A020', '#8E44AD', '#16A085', '#D35400', '#34495E'];
function renderZoneBalls(zones, field, opts = {}) {
    const size = opts.size || 24;
    const showLabels = opts.showLabels !== false;
    const hitHighlight = opts.hitHighlight === true;
    const fs = Math.max(10, Math.round(size * 0.44));
    // 基础球样式（无背景/边框，由每个球叠加 extra）
    const ballStyle = `display:inline-flex;align-items:center;justify-content:center;width:${size}px;height:${size}px;border-radius:50%;font-weight:700;font-size:${fs}px;margin:1px;position:relative;box-sizing:border-box;`;
    let out = '<span style="display:inline-flex;align-items:center;flex-wrap:wrap;gap:2px 1px;">';
    let first = true;
    (zones || []).forEach((z, idx) => {
        const balls = toBallArray(z[field] || []);
        if (!balls.length) return;
        if (showLabels) {
            const sep = first ? '' : '<span style="width:8px;display:inline-block"></span>';
            const lblColor = z.cls === 'red' ? '#E24B4A' : z.cls === 'blue' ? '#1E90FF' : 'var(--gray)';
            out += `${sep}<span style="font-size:12px;color:${lblColor};white-space:nowrap;margin-right:4px;">${z.name}</span>`;
        }
        // 命中对照：预测号码对照实际开奖，实际开奖对照预测号码
        const against = field === 'pred' ? 'actual' : field === 'actual' ? 'pred' : null;
        const againstSet = hitHighlight && against ? (z[against] || []).map(String) : [];
        // 数字型组选判定：号码不在本位、但在该期开奖其它位出现 → 仅加一个小橙点标记
        const againstAll = hitHighlight && against
            ? (zones || []).reduce((acc, zz) => acc.concat((zz[against] || []).map(String)), [])
            : [];
        let cls = '', baseExtra = 'box-shadow:0 1px 2px rgba(0,0,0,0.12);';
        if (z.cls === 'red') cls = 'red-ball';
        else if (z.cls === 'blue') cls = 'blue-ball';
        else baseExtra += `background:${ZONE_PALETTE[idx % ZONE_PALETTE.length]};color:#fff;`;
        balls.forEach(n => {
            const s = String(n);
            const hit = hitHighlight ? againstSet.includes(s) : true;
            // 数字型（cls 为空、按位有序）：号码不在本位，但在实际开奖的其它位出现
            const posWrong = hitHighlight && !hit && !cls && againstAll.includes(s);
            let useCls = cls, extra = baseExtra, dimCls = '';
            if (hitHighlight && !hit) {
                if (cls) {
                    dimCls = ' dim';
                } else {
                    // 数字型未命中（含组选错位）：带透明度的灰球——透明度只加在灰底上（rgba），
                    // 文字保持纯白不透明，半透明观感与号码可读兼得（严禁 opacity 整球叠加，前两轮教训）
                    extra = `background:rgba(73,80,87,0.72);color:#fff;box-shadow:none;`;
                }
            } else if (hitHighlight && hit && !cls) {
                // 数字型命中：与双/大红球同款亮面球 + 金色边框/发光 + 对勾标记
                useCls = 'red-ball';
                extra = 'border:2px solid #FFD700;box-shadow:0 0 5px #FFD700, 0 1px 2px rgba(0,0,0,0.15);';
            }
            const checkSvg = (hitHighlight && hit)
                ? `<svg style="position:absolute;top:-3px;right:-3px;width:${Math.max(9, Math.round(size*0.42))}px;height:${Math.max(9, Math.round(size*0.42))}px;background:#FFD700;border-radius:50%;padding:1px;box-shadow:0 1px 2px rgba(0,0,0,0.35);z-index:1" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg>`
                : '';
            const posWrongDot = posWrong
                ? `<span title="号码中了但位置不对（组选中）" style="position:absolute;top:-2px;left:-2px;width:${Math.max(6, Math.round(size*0.28))}px;height:${Math.max(6, Math.round(size*0.28))}px;background:#FF8C00;border-radius:50%;z-index:1"></span>`
                : '';
            out += `<span class="${useCls}${dimCls}" style="${ballStyle}${extra}">${s.padStart(2, '0')}${checkSvg}${posWrongDot}</span>`;
        });
        first = false;
    });
    out += '</span>';
    return out;
}

// 看板/首页开奖号码：兼容 红球/蓝球（双/大）与 号码(dict)（数字型）
function renderDrawNumbers(dn) {
    if (!dn) return '';
    if (dn.号码 && typeof dn.号码 === 'object') {
        const zones = Object.entries(dn.号码).map(([name, pred]) => ({ name, pred, cls: null }));
        return renderZoneBalls(zones, 'pred', { size: 24, showLabels: true });
    }
    return renderBalls(dn.红球, dn.蓝球, 24);
}

// 渲染一组预测号码（PredictSet.to_dict 的 号码 分区字典；红球→红/蓝光球，数字型按位着色）
function renderPredNumbers(num, opts = {}) {
    const zones = Object.entries(num || {}).map(([name, pred]) => ({
        name, pred, cls: name === '红球' ? 'red' : name === '蓝球' ? 'blue' : null
    }));
    return renderZoneBalls(zones, 'pred', opts);
}

// 票面 → 分区字典：优先 `号码` 字段；旧 pending 票面（如规则优选）可能只有顶层
// 红球/蓝球（无 `号码`），兜底构造，避免弹窗里只显示元信息不显示号码。
// ⚠️ 命名为 _ticketZoneDict：文件后部已有同形的 _ticketZones(nums)（吃字典吐
// zone 数组、配 renderZoneBalls），同名会被后者覆盖导致全站号码渲染为空。
function _ticketZoneDict(t) {
    if (t && t.号码 && typeof t.号码 === 'object') return t.号码;
    if (t && (Array.isArray(t.红球) || Array.isArray(t.蓝球)))
        return { 红球: t.红球 || [], 蓝球: t.蓝球 || [] };
    return null;
}

// ===== 数据状态面板（首页） =====
async function loadDataStatus() {
    const data = await api('/api/data-status');
    for (const [lottery, status] of Object.entries(data)) {
        // 首页目前只有双色球/大乐透两张主卡片，数字型/七星彩作为附属展示
        // 不跳过的话，6 彩种会全部复写 dlt 卡片，最后留下排序最末那条数据
        if (lottery !== '双色球' && lottery !== '大乐透') continue;
        const prefix = lottery === '双色球' ? 'ssq' : 'dlt';
        html(`${prefix}-records`, status.total_records);
        const drawEl = $(`${prefix}-latest-draw`);
        const dateEl = $(`${prefix}-latest-date`);
        if (drawEl) drawEl.textContent = (status.latest_draw || '-') + '期';
        if (dateEl) dateEl.textContent = status.latest_date || '-';
        html(`${prefix}-size`, status.csv_size_kb + ' KB');

        // 下一期预测日期：按开奖日规则从最近开奖日期推算，不再依赖 pending（pending 可能缺失/过期）
        const nextEl = $(`${prefix}-next`);
        if (nextEl) {
            if (status.next_draw_date) {
                const nd = String(status.next_draw_date);
                nextEl.textContent = nd.slice(5) + '期';
                nextEl.title = '按官方开奖日规则计算：' + status.latest_date + ' 之后最近一期是 ' + status.next_draw_date;
                nextEl.style.color = status.data_lag_days > 0 ? '#c62828' : '';
            } else {
                nextEl.textContent = '未生成';
                nextEl.title = '';
            }
        }

        // 数据新鲜度提示
        const hintEl = $(`${prefix}-fresh-hint`);
        if (hintEl && status.data_lag_days > 0) {
            hintEl.innerHTML = `最新开奖数据已滞后 <strong style="color:#c62828">${status.data_lag_days} 天</strong>（最新 ${status.latest_date}），请点「更新开奖数据」`;
        } else if (hintEl) {
            hintEl.innerHTML = '最新期号决定预测准不准——开奖后记得点「<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M6 13l6 6 6-6"/></svg> 更新开奖数据」';
        }
    }
    // 下一期预测日期（从 pending 预测取最新）——已废弃，改为 backend 计算 next_draw_date
    // 保留以下空块以便未来需要时扩展

    // 按发行机构展示同部门其他彩种（福彩中心 / 体彩中心），不区分玩法类型
    const dependents = {
        'ssq-dependents': {
            title: '福彩中心其他彩种',
            accent: '#e74c3c',
            bg: '#fff5f5',
            border: '#ffcccc',
            lots: ['福彩3D'],
        },
        'dlt-dependents': {
            title: '体彩中心其他彩种',
            accent: '#3498db',
            bg: '#f0f9ff',
            border: '#b3e0ff',
            lots: ['七星彩', '排列3', '排列5'],
        },
    };
    for (const [containerId, cfg] of Object.entries(dependents)) {
        const container = $(containerId);
        if (!container) continue;
        let htmlStr = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
            <span style="display:inline-block;width:4px;height:18px;border-radius:2px;background:${cfg.accent}"></span>
            <span style="font-size:14px;font-weight:700;color:${cfg.accent};letter-spacing:.3px">${cfg.title}</span>
        </div>`;
        htmlStr += '<div style="display:flex;flex-wrap:wrap;gap:10px">';
        for (const lot of cfg.lots) {
            const s = data[lot] || {};
            const next = s.next_draw_date ? String(s.next_draw_date).slice(5) : '未生成';
            const lag = s.data_lag_days || 0;
            const lagBadge = lag > 0 ? `<span style="font-size:11px;font-weight:600;color:#fff;background:#c62828;padding:1px 6px;border-radius:10px;margin-left:4px">滞后${lag}天</span>` : '';
            htmlStr += `<div style="flex:1;min-width:150px;padding:12px;border-radius:8px;background:${cfg.bg};border:1px solid ${cfg.border};border-top:3px solid ${cfg.accent};box-shadow:0 1px 2px rgba(0,0,0,0.04)">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <span style="font-size:14px;font-weight:700;color:${cfg.accent}">${lot}</span>
                    <span style="font-size:12px;font-weight:600;color:#fff;background:${cfg.accent};padding:2px 8px;border-radius:10px">${s.total_records || 0} 条</span>
                </div>
                <div style="margin-top:8px;font-size:18px;font-weight:700;color:var(--dark)">${s.latest_draw || '-'}</div>
                <div style="font-size:12px;color:var(--gray)">最新期号 · ${s.latest_date || '-'}</div>
                <div style="margin-top:6px;font-size:12px;color:var(--gray);display:flex;align-items:center;flex-wrap:wrap">
                    <span style="font-weight:600;color:${s.next_draw_date ? cfg.accent : '#999'}">下一期 ${next}</span>
                    ${lagBadge}
                </div>
            </div>`;
        }
        htmlStr += '</div>';
        container.innerHTML = htmlStr;
    }

    // 全局数据滞后告警横幅 + 一键补齐
    renderDataLagBanner(data);
}

function renderDataLagBanner(data) {
    const banner = $('data-lag-banner');
    if (!banner) return;
    const lagLots = Object.entries(data)
        .filter(([_, s]) => (s.data_lag_days || 0) > 0)
        .map(([name, s]) => ({ name, lag: s.data_lag_days, latest: s.latest_date }));
    if (!lagLots.length) {
        banner.innerHTML = '';
        banner.style.display = 'none';
        return;
    }
    banner.style.display = 'block';
    const items = lagLots.map(l => `<span style="display:inline-block;margin-right:12px"><strong>${l.name}</strong> 滞后 ${l.lag} 天（最新 ${l.latest}）</span>`).join('');
    banner.innerHTML = `<div class="alert" style="background:#fff3f0;border-left:4px solid #c62828;color:#5d2315;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${ico('warning')}<strong>数据已滞后</strong><span style="font-size:13px">${items}</span></div>
        <button class="btn btn-sm" id="recover-all-btn" onclick="runRecoverAll()" style="background:#c62828;color:#fff;border:none">${ico('refresh')} 一键补齐</button>
    </div>`;

    // 智能自动补齐：检测到滞后且 30 分钟内没自动跑过，自动触发一次补齐（无需手动点按钮）
    autoRecoverIfLagging(lagLots);
}

// 自动补齐防抖：同一浏览器会话 30 分钟内只自动跑一次，避免轮询/刷新反复触发
let autoRecoverLastAt = 0;
function autoRecoverIfLagging(lagLots) {
    if (!lagLots || !lagLots.length) return;
    if (recoverPollTimer) return;          // 已有补齐任务在跑
    const now = Date.now();
    if (now - autoRecoverLastAt < 30 * 60 * 1000) return;  // 30 分钟冷却
    autoRecoverLastAt = now;
    showAlert('info', '检测到数据滞后，正在自动补齐（无需手动操作）...');
    runRecoverAll();
}

async function runRecoverAll() {
    const btn = $('recover-all-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = ico('hourglass') + ' 补齐中...'; }
    if (recoverPollTimer) { clearInterval(recoverPollTimer); recoverPollTimer = null; }
    try {
        const data = await api('/api/recover', { method: 'POST', body: JSON.stringify({ force: false }) });
        if (!data.task_id) {
            throw new Error('后端未返回任务ID');
        }
        recoverTaskId = data.task_id;
        showAlert('info', '一键补齐任务已启动，正在后台补齐...');
        pollRecoverStatus();
    } catch (e) {
        showAlert('danger', '一键补齐失败: ' + e.message);
        if (btn) { btn.disabled = false; btn.innerHTML = ico('refresh') + ' 一键补齐'; }
    }
}

function pollRecoverStatus() {
    if (!recoverTaskId) return;
    const btn = $('recover-all-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = ico('hourglass') + ' 补齐中...'; }
    if (recoverPollTimer) clearInterval(recoverPollTimer);
    recoverPollTimer = setInterval(async () => {
        try {
            const data = await api(`/api/auto/status/${recoverTaskId}`);
            if (data.status === 'running') {
                if (btn) btn.innerHTML = ico('hourglass') + ' ' + (data.message || '补齐中...');
                return;
            }
            clearInterval(recoverPollTimer);
            recoverPollTimer = null;
            if (btn) { btn.disabled = false; btn.innerHTML = ico('refresh') + ' 一键补齐'; }
            if (data.status === 'error') {
                showAlert('danger', '一键补齐失败: ' + (data.error || '未知错误'));
            } else {
                const result = data.result || {};
                const results = result.results || {};
                const changed = Object.entries(results).filter(([_, r]) => r && r.fetched > 0);
                if (changed.length) {
                    showAlert('success', `已补齐 ${changed.length} 个彩种：${changed.map(([n, r]) => n + ' +' + r.fetched).join('，')}`);
                } else {
                    showAlert('info', result.message || '所有彩种远端暂无更新，已是最新');
                }
            }
            loadDataStatus();
        } catch (e) {
            clearInterval(recoverPollTimer);
            recoverPollTimer = null;
            if (btn) { btn.disabled = false; btn.innerHTML = ico('refresh') + ' 一键补齐'; }
            showAlert('danger', '轮询补齐状态失败: ' + e.message);
        }
    }, 1500);
}

async function refreshData(lottery, btn) {
    // 优先用显式传入的按钮引用，回退到全局 event.target，避免耦合全局 event
    btn = btn || (typeof event !== 'undefined' && event && event.target);
    if (!btn) { console.warn('refreshData: 未找到触发按钮'); return; }
    btn.disabled = true; btn.textContent = '刷新中...';
    const result = await api('/api/refresh', {
        method: 'POST',
        body: JSON.stringify({ lottery })
    });
    if (result.success) {
        showAlert('success', result.message);
        loadDataStatus();
        loadCompare(lottery);
    } else {
        showAlert('danger', '刷新失败: ' + (result.error || '未知错误'));
    }
    btn.disabled = false; btn.textContent = '刷新数据';
}

function showAlert(type, msg) {
    const container = $('alert-container');
    if (!container) return;
    const div = document.createElement('div');
    div.className = `alert alert-${type}`;
    div.textContent = msg;
    container.appendChild(div);
    setTimeout(() => div.remove(), 5000);
}

// 渲染自动化命中摘要（真预测/训练回测标签 + × 关闭按钮）
function _renderAutoHitSection(record, lottery) {
    const hitRecords = record.hit_records || [];
    const hitSummary = record.hit_summary || '';
    if (!hitRecords.length && !hitSummary) return '';

    // 结构化记录存在：逐条渲染标签 + × 关闭按钮
    if (hitRecords.length) {
        let html = `<div style="margin-top:8px"><strong style="font-size:12px">${ico('target')} 命中摘要：</strong>`;
        for (const h of hitRecords) {
            const valid = h.valid !== false;
            const tag = valid ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 真预测' : '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 开奖后回测·训练';
            const key = _dismissKey('detail-hit', lottery, record) + ':' + (valid ? '1' : '0') + ':' + h.prize;
            if (_isDismissed(key)) continue;
            const hot = valid;
            html += `<div data-dismiss-key="${key}" style="margin:4px 0;padding:6px 32px 6px 8px;border-radius:6px;font-size:13px;position:relative;display:flex;align-items:center;flex-wrap:wrap;gap:6px;${hot ? 'background:#fff7e6;color:#b26a00;font-weight:bold' : 'background:#f5f5f5;color:var(--gray);border-left:3px dashed #aaa'}">
                <span>${tag}</span>
                <span>命中 ${h.prize}×${h.count || 1}</span>
                ${h.issue ? `<span style="font-size:12px;font-weight:normal;color:var(--gray)">期号${h.issue}</span>` : ''}
                <button onclick="_dismissAlert('${key}')" title="关闭这条命中通知" style="position:absolute;right:4px;top:50%;transform:translateY(-50%);background:transparent;border:none;color:#999;cursor:pointer;font-size:18px;line-height:1;padding:0 6px" onmouseenter="this.style.color='#c62828'" onmouseleave="this.style.color='#999'">×</button>
            </div>`;
        }
        html += `</div>`;
        return html;
    }

    // 兼容旧数据：只有 hit_summary 字符串时按原样渲染
    const hot = hitSummary.includes('<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg>');
    return `<div style="margin-top:8px;padding:6px 8px;border-radius:6px;font-size:13px;${hot ? 'background:#fff7e6;color:#b26a00;font-weight:bold' : 'color:var(--gray)'}">${hitSummary}</div>`;
}

// ===== 自动化运行状态（看板） =====

// 渲染单个彩种的运行状态卡片
function _renderOneLotteryStatus(lottery, record) {
    if (!record) {
        return `<div style="padding:10px;border-radius:8px;background:#f8f9fa;border:1px dashed var(--border);margin-bottom:10px">
            <div style="font-size:15px;font-weight:bold;color:var(--gray)"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#222"/><circle cx="9" cy="9" r="1.2" fill="#fff"/><circle cx="14.5" cy="10" r="1.2" fill="#fff"/><circle cx="10" cy="14.5" r="1.2" fill="#fff"/></svg> ${lottery}</div>
            <div style="margin-top:6px;color:var(--gray);font-size:13px">暂无该彩种的自动化运行记录。</div>
        </div>`;
    }

    const okColor = record.success ? '#2e7d32' : '#c62828';
    const bgColor = record.success ? '#f0faf0' : '#fdf0f0';
    const dn = record.draw_numbers || {};
    const preds = record.predictions || [];
    const flags = record.anomaly_flags || [];
    const rt = record.retrain || null;

    let htmlStr = `<div style="padding:10px;border-radius:8px;background:${bgColor};margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">
            <div style="font-size:15px;font-weight:bold"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#222"/><circle cx="9" cy="9" r="1.2" fill="#fff"/><circle cx="14.5" cy="10" r="1.2" fill="#fff"/><circle cx="10" cy="14.5" r="1.2" fill="#fff"/></svg> ${lottery}</div>
            <div style="font-size:14px"><strong style="color:${okColor}">${record.success ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 上次运行成功' : '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg> 上次运行失败'}</strong>
            <span style="color:var(--gray)">${(record.time||'').replace('T',' ').slice(0,16)} · ${record.duration_sec ? record.duration_sec+'s' : ''}</span></div>
        </div>
        <div style="margin-top:4px;color:var(--gray)">${record.message || ''}</div>`;

    // 抓取到的开奖号
    if (dn && dn.期号) {
        htmlStr += `<div style="margin-top:8px"><strong style="font-size:12px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M5 5h14v14H5z"/><path d="M3 12h5l2 3h4l2-3h5"/></svg> 抓取到的开奖 ${dn.期号}${dn.日期 ? '（' + dn.日期 + '）' : ''}：</strong><br>
            ${renderDrawNumbers(dn)}</div>`;
    }
    // 本次生成的预测
    if (preds.length) {
        htmlStr += `<div style="margin-top:8px"><strong style="font-size:12px">${ico('target')} 本次生成下期预测 ${preds.length} 组：</strong>`;
        preds.slice(0, 5).forEach((g, i) => {
            const conf = (g.置信度 != null) ? Number(g.置信度).toFixed(2) : '-';
            htmlStr += `<div style="margin:6px 0;font-size:12px;display:flex;align-items:center;flex-wrap:wrap;gap:4px 0">第${i + 1}组
                ${renderPredNumbers(_ticketZoneDict(g), { size: 22, showLabels: false })}
                <span style="color:var(--gray);margin-left:6px">${g.策略 || ''} · 置信度${conf}</span></div>`;
        });
        htmlStr += `</div>`;
    }
    // 命中摘要（真预测/训练回测标签 + × 关闭按钮）
    htmlStr += _renderAutoHitSection(record, lottery);
    // 异常告警
    if (flags.length) {
        htmlStr += `<div style="margin-top:8px"><strong style="font-size:12px;color:#e67e22"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 异常检测告警：</strong><br>` +
            flags.map(f => `<span style="display:inline-block;margin:2px;padding:2px 8px;background:#fff3e0;color:#e67e22;border-radius:10px;font-size:12px">${f}</span>`).join('') + `</div>`;
    }
    // 重训结果
    if (rt) {
        htmlStr += `<div style="margin-top:8px;font-size:12px;color:var(--gray)"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg> 模型重训：窗口 ${rt.window} · 命中率 ${(Number(rt.hit_rate) * 100).toFixed(2)}% (${rt.model})</div>`;
    }
    htmlStr += `</div>`;
    return htmlStr;
}

async function loadAutomationStatus() {
    const el = $('auto-status-content');
    if (!el) return;
    const badge = $('auto-status-badge');
    const data = await api('/api/automation-status');
    window._autoData = data; // 供 showAutoDetail / _renderAutoBrief 复用

    const byLottery = data.last_run_by_lottery || {};
    const mainLotteries = ['双色球', '大乐透'];
    const allLotteries = ['双色球', '大乐透', '排列5', '福彩3D', '排列3', '七星彩'];
    const dependentMap = {
        '福彩3D': '双色球',
        '七星彩': '大乐透',
        '排列3': '大乐透',
        '排列5': '大乐透',
    };

    // 看板顶部「今日简报」主动通知
    _renderAutoBrief(data);

    // 失败告警徽标（综合全部彩种）
    // 失败告警徽标：以「最近一次事件是不是失败」为准。
    // 旧逻辑只看最近 50 条历史里的失败条数（failed_count），一次两三天前的网络中断
    // 会让徽标长期挂红（此后连续成功数十次也不例外），看起来像自动化一直在失败。
    const failIsLatest = !!data.last_failed
        && (!data.last_success || data.last_failed > data.last_success);
    if (failIsLatest) {
        if (badge) badge.innerHTML = `<span style="padding:2px 10px;border-radius:10px;font-size:12px;background:var(--color-background-danger,#fdeaea);color:#c62828"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 最近有失败（${fmtSchedTime(data.last_failed)}）</span>`;
    } else if (data.failed_count > 0) {
        if (badge) badge.innerHTML = `<span style="padding:2px 10px;border-radius:10px;font-size:12px;background:var(--color-background-secondary,#f2f3f5);color:#5f6b7a" title="历史上出现过失败，但最近一次运行是成功的">已恢复（历史失败 ${data.failed_count} 次，最近一次 ${fmtSchedTime(data.last_failed)}）</span>`;
    } else if (allLotteries.every(l => !byLottery[l] || byLottery[l].success)) {
        if (badge) badge.innerHTML = '<span style="padding:2px 10px;border-radius:10px;font-size:12px;background:var(--color-background-success,#e6f4e6);color:#2e7d32"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 运行正常</span>';
    }

    // 没有任何记录时提示
    const hasAny = allLotteries.some(l => byLottery[l]);
    if (!hasAny) {
        el.innerHTML = '<div class="alert alert-info">还没有运行过流水线。点上方「一键流水线」或等定时任务（开奖日 21:30）自动运行。</div>';
        if (badge) badge.innerHTML = '';
        return;
    }

    // 分别渲染双色球、大乐透主卡片
    let htmlStr = '';
    for (const lottery of mainLotteries) {
        htmlStr += _renderOneLotteryStatus(lottery, byLottery[lottery]);
    }

    // 按发行机构展示同部门其他彩种运行状态
    const centerGroups = [
        { title: '福彩中心其他彩种', color: '#e74c3c', lots: ['福彩3D'] },
        { title: '体彩中心其他彩种', color: '#3498db', lots: ['七星彩', '排列3', '排列5'] },
    ];
    for (const group of centerGroups) {
        const depRecords = group.lots.map(l => ({ lottery: l, record: byLottery[l] })).filter(r => r.record);
        if (!depRecords.length) continue;
        htmlStr += `<div style="margin-top:10px;padding:10px;border-radius:8px;background:var(--color-background-secondary,#f8f9fa);border:1px solid var(--color-border-tertiary,#eee)">
            <div style="font-size:12px;font-weight:bold;color:${group.color};margin-bottom:6px">${group.title}</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px">`;
        for (const { lottery, record } of depRecords) {
            const okColor = record.success ? '#2e7d32' : '#c62828';
            const hit = record.hit_summary || '';
            const preds = record.predictions || [];
            const predCount = preds.length;
            // 预览前 2 组号码（避免卡片过高）
            const preview = preds.slice(0, 2).map((g, i) => {
                const conf = (g.置信度 != null) ? Number(g.置信度).toFixed(2) : '-';
                return `<div style="display:flex;align-items:center;gap:6px;margin-top:4px">
                    <span style="font-size:11px;color:var(--gray);min-width:32px;flex-shrink:0">第${i + 1}组</span>
                    <span style="display:inline-flex;align-items:center;flex-wrap:wrap">${renderPredNumbers(_ticketZoneDict(g), { size: 18, showLabels: false })}</span>
                    <span style="font-size:11px;color:var(--gray);margin-left:auto">· ${g.策略 || ''} ${conf}</span>
                </div>`;
            }).join('');
            const more = predCount > 2 ? `<div style="font-size:11px;color:var(--gray);margin-top:2px">…等共 ${predCount} 组</div>` : '';
            htmlStr += `<div style="flex:1;min-width:180px;padding:8px;border-radius:6px;background:#fff;border:1px solid var(--color-border-tertiary,#eee)">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <strong style="font-size:13px">${lottery}</strong>
                    <span style="color:${okColor};font-size:12px">${record.success ? '✓' : '✗'}</span>
                </div>
                <div style="font-size:12px;color:var(--gray);margin-top:2px">${(record.time||'').replace('T',' ').slice(0,16)} · ${record.message || ''}</div>
                ${predCount ? `<div style="font-size:12px;color:var(--gray);margin-top:2px">生成预测 ${predCount} 组</div>${preview}${more}` : ''}
                ${hit ? `<div style="font-size:12px;color:#b26a00;margin-top:4px">${hit}</div>` : ''}
            </div>`;
        }
        htmlStr += '</div></div>';
    }

    // 运行历史（每个彩种最近一条，保证 6 彩种都能看到，不会被高频彩种挤出）
    const historyByLottery = {};
    for (const h of (data.history || [])) {
        const l = h.lottery;
        if (!historyByLottery[l] || (h.time || '') > (historyByLottery[l].time || '')) {
            historyByLottery[l] = h;
        }
    }
    const history = Object.values(historyByLottery).sort((a, b) => (b.time || '').localeCompare(a.time || ''));
    if (history.length > 1) {
        htmlStr += `<div style="margin-top:10px"><strong style="font-size:12px">最近运行记录</strong>
        <table style="font-size:12px;margin-top:4px"><tr><th>时间</th><th>彩票</th><th>结果</th><th>摘要</th></tr>`;
        for (const h of history) {
            const c = h.success ? '#2e7d32' : '#c62828';
            htmlStr += `<tr>
                <td style="white-space:nowrap">${(h.time||'').replace('T',' ').slice(0,16)}</td>
                <td>${h.lottery}</td>
                <td style="color:${c}">${h.success ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg>' : '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>'}</td>
                <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${h.message || ''}</td>
            </tr>`;
        }
        htmlStr += `</table></div>`;
    }

    el.innerHTML = htmlStr;

    // 期望时机提醒条（全部彩种，主彩种优先）
    const ticker = $('ev-ticker');
    if (ticker) {
        let tickerHtml = '<div style="border-top:1px solid var(--color-border-tertiary,#eee);padding-top:8px"><strong style="font-size:12px">' + ico('target') + ' 当期期望建议：</strong>';
        for (const l of allLotteries) {
            try {
                const a = await api(`/api/ev/advice/${encodeURIComponent(l)}`);
                const good = a.是否值得买;
                const c = good ? '#c62828' : '#888';
                tickerHtml += ` <span style="font-size:12px;margin-right:12px">${l}: <strong style="color:${c}">${a.单注期望}</strong> ${good ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3c2 3 4 4 4 8a4 4 0 0 1-8 0c0-1 .5-2 1-3 .5 1 1 1.5 1 2 0-2 1-4 2-7z"/></svg>值得买' : '常规期'}</span>`;
            } catch (e) { /* 单条失败不影响 */ }
        }
        tickerHtml += `<a href="/ev" style="font-size:12px;color:var(--primary)">详情 →</a></div>`;
        ticker.innerHTML = tickerHtml;
    }
}

// ===== 内置自动调度器（实时状态，替代 Windows 定时任务，无静态数据） =====
function escSched(x) {
    return String(x == null ? '' : x).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmtSchedTime(s) {
    if (!s) return '—';
    return s.replace('T', ' ').slice(0, 16);
}

function schedStatusText(s) {
    const map = { done: '完成', success: '完成', running: '运行中', failed: '失败', error: '失败' };
    return map[String(s).toLowerCase()] || s;
}

async function loadSchedulerStatus() {
    const box = $('sched-status');
    if (!box) return;
    let data;
    try {
        data = await api('/api/scheduler/status');
    } catch (e) {
        box.innerHTML = `<span style="color:#c62828">调度器状态读取失败：${escSched(e)}</span>`;
        return;
    }
    const en = $('sched-enabled');
    if (en) {
        en.checked = !!data.enabled;
        const lbl = $('sched-enabled-label');
        if (lbl) lbl.textContent = data.enabled ? '启用中' : '已停用';
    }
    const lots = data.lotteries || [];
    let html = '<table style="width:100%;border-collapse:collapse;margin-top:4px">'
        + '<thead><tr style="text-align:left;color:var(--gray);font-weight:600">'
        + '<th style="padding:4px 6px">彩种</th><th style="padding:4px 6px">下次运行</th><th style="padding:4px 6px">上次运行</th><th style="padding:4px 6px">状态</th></tr></thead><tbody>';
    for (const l of lots) {
        const st = l.running
            ? '<span style="color:#1565c0;font-weight:600">运行中</span>'
            : (l.overdue
                ? '<span style="color:#e65100;font-weight:600" title="该彩种开奖日的触发时刻已过，但当天没有运行记录">待补跑</span>'
                : (l.last_status ? escSched(schedStatusText(l.last_status)) : '—'));
        const nextTip = l.due_run ? ` title="最近应触发：${fmtSchedTime(l.due_run)}"` : '';
        html += `<tr style="border-top:1px solid var(--color-border-tertiary,#eee)">`
            + `<td style="padding:4px 6px">${escSched(l.lottery)}</td>`
            + `<td style="padding:4px 6px"${nextTip}>${fmtSchedTime(l.next_run)}</td>`
            + `<td style="padding:4px 6px">${l.last_run ? fmtSchedTime(l.last_run) : '从未'}</td>`
            + `<td style="padding:4px 6px">${st}</td></tr>`;
    }
    html += '</tbody></table>';
    box.innerHTML = html;
}

async function toggleScheduler(on) {
    try {
        await api('/api/scheduler/toggle', { method: 'POST', body: JSON.stringify({ enabled: on }) });
        const lbl = $('sched-enabled-label');
        if (lbl) lbl.textContent = on ? '启用中' : '已停用';
        loadSchedulerStatus();
    } catch (e) {
        alert('操作失败：' + e);
    }
}

async function runSchedulerNow() {
    try {
        const d = await api('/api/scheduler/run-now', { method: 'POST', body: JSON.stringify({}) });
        if (d && d.launched && d.launched.length) {
            alert('已触发：' + d.launched.join('、'));
            setTimeout(loadSchedulerStatus, 2000);
        } else {
            alert('没有可运行的彩种');
        }
    } catch (e) {
        alert('触发失败：' + e);
    }
}

// ===== 出号数量（固定 / 动态滑轨，2026-09-14）=====
// 命名一律加 ts 前缀，避免与 main.js 全局作用域既有函数重名（曾踩 _ticketZones 坑）。
let _tsState = null;

async function tsLoad() {
    const rows = $('ts-rows');
    if (!rows) return;
    let d;
    try {
        d = await api('/api/settings/ticket-size');
    } catch (e) {
        rows.innerHTML = `<div class="alert alert-danger">读取失败：${escSched(e)}</div>`;
        return;
    }
    _tsState = d;
    tsRender(d);
}

function tsRender(d) {
    const rows = $('ts-rows');
    const badge = $('ticket-size-badge');
    const mode = d.mode || 'fixed';
    const isDyn = mode === 'dynamic';
    document.querySelectorAll('input[name="ts-mode"]').forEach(r => { r.checked = (r.value === mode); });
    if (badge) {
        badge.textContent = isDyn ? '动态' : '固定';
        badge.style.background = isDyn ? '#e8f4ff' : '#f0f0f0';
        badge.style.color = isDyn ? '#0d6efd' : '#555';
        badge.style.padding = '2px 8px';
        badge.style.borderRadius = '10px';
    }
    const pol = d.policy || {};
    const polEl = $('ts-policy');
    if (polEl) {
        polEl.textContent = pol['理由'] ? `最近调整（${pol['更新于'] || ''}）：${pol['理由']}` : '';
    }
    const applyBtn = $('ts-apply-btn');
    if (applyBtn) applyBtn.style.display = isDyn ? '' : 'none';

    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px">';
    for (const it of (d.lotteries || [])) {
        const val = isDyn ? (it['动态值'] != null ? it['动态值'] : it['默认'])
                          : (it['配置值'] != null ? it['配置值'] : it['默认']);
        const cur = it['当前生效'];
        const sug = it['建议'] || {};
        let adv = '';
        if (isDyn && sug.next != null) {
            const dir = sug.next > cur ? '↑' : (sug.next < cur ? '↓' : '=');
            const col = sug.next > cur ? '#c62828' : (sug.next < cur ? '#2e7d32' : 'var(--gray)');
            adv = `<div style="font-size:11px;color:${col};margin-top:2px" title="${escSched(sug.reason || '')}">下一期建议 ${dir} ${sug.next}${sug['冗余度'] != null ? '（冗余 ' + Math.round(sug['冗余度'] * 100) + '%）' : ''}</div>`;
        }
        html += `<div style="border:1px solid var(--color-border-tertiary,#eee);border-radius:8px;padding:8px 10px">
            <div style="display:flex;justify-content:space-between;align-items:baseline">
              <span style="font-size:13px;font-weight:600">${escSched(it.lottery)}</span>
              <span style="font-size:11px;color:var(--gray)">${it['数字型'] ? '数字型' : '乐透型'} · 上限${it['上限']}</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
              <input type="range" id="ts-range-${tsIdx(it.lottery)}" min="${it['下限']}" max="${it['上限']}" value="${val}"
                     style="flex:1" oninput="tsOnInput('${escSched(it.lottery)}', this.value)">
              <input type="number" id="ts-num-${tsIdx(it.lottery)}" min="${it['下限']}" max="${it['上限']}" value="${val}"
                     style="width:64px;padding:2px 6px;border:1px solid #ddd;border-radius:5px"
                     oninput="tsOnInput('${escSched(it.lottery)}', this.value)">
            </div>
            <div style="font-size:11px;color:var(--gray);margin-top:4px">当前生效 <b id="ts-cur-${tsIdx(it.lottery)}">${cur}</b> 注${isDyn ? '（动态策略维护）' : ''}</div>
            ${adv}
        </div>`;
    }
    html += '</div>';
    rows.innerHTML = html;
}

function tsIdx(lottery) {
    // 彩种名 → 稳定下标（避免中文作 id）
    const map = { '双色球': 0, '大乐透': 1, '排列5': 2, '福彩3D': 3, '排列3': 4, '七星彩': 5 };
    return map[lottery] != null ? map[lottery] : 99;
}

function tsOnInput(lottery, value) {
    const i = tsIdx(lottery);
    const num = $('ts-num-' + i), range = $('ts-range-' + i);
    let v = parseInt(value, 10);
    if (isNaN(v)) return;
    if (num) num.value = v;
    if (range) range.value = v;
}

function tsCollectValues() {
    const out = {};
    for (const [lot, i] of Object.entries({ '双色球': 0, '大乐透': 1, '排列5': 2, '福彩3D': 3, '排列3': 4, '七星彩': 5 })) {
        const num = $('ts-num-' + i);
        if (num) {
            const v = parseInt(num.value, 10);
            if (!isNaN(v)) out[lot] = v;
        }
    }
    return out;
}

async function tsSetMode(mode) {
    try {
        const d = await api('/api/settings/ticket-size', {
            method: 'POST', body: JSON.stringify({ mode })
        });
        _tsState = d; tsRender(d);
        tsMsg('已切换到「' + (mode === 'dynamic' ? '动态数量' : '固定数量') + '」');
    } catch (e) {
        alert('切换失败：' + e);
    }
}

async function tsSave() {
    const mode = (_tsState && _tsState.mode) || 'fixed';
    const vals = tsCollectValues();
    const body = { mode, fixed: vals };
    if (mode === 'dynamic') body.dynamic = vals;
    try {
        const d = await api('/api/settings/ticket-size', { method: 'POST', body: JSON.stringify(body) });
        _tsState = d; tsRender(d);
        tsMsg('已保存');
    } catch (e) {
        alert('保存失败：' + e);
    }
}

async function tsApplyDynamic() {
    try {
        const d = await api('/api/settings/ticket-size/apply', { method: 'POST', body: JSON.stringify({}) });
        _tsState = d; tsRender(d);
        tsMsg('已按策略调整');
    } catch (e) {
        alert('调整失败：' + e);
    }
}

async function tsReset() {
    if (!confirm('恢复默认出号数量（数字型 5 / 乐透型 100）？')) return;
    try {
        const d = await api('/api/settings/ticket-size/reset', { method: 'POST', body: JSON.stringify({}) });
        _tsState = d; tsRender(d);
        tsMsg('已恢复默认');
    } catch (e) {
        alert('恢复失败：' + e);
    }
}

function tsMsg(t) {
    const el = $('ts-msg');
    if (!el) return;
    el.textContent = t;
    el.style.color = '#2e7d32';
    setTimeout(() => { el.textContent = ''; }, 3000);
}

// ===== 微信推送（出号 + 中奖记录送上门，2026-09-16）=====
// pn 前缀独占命名，避免与全局作用域既有函数重名。
async function pnLoad() {
    const box = $('pn-status');
    if (!box && !$('pn-enabled')) return;
    let d;
    try {
        d = await api('/api/settings/push-notify');
    } catch (e) {
        if (box) box.textContent = '推送配置读取失败：' + e;
        return;
    }
    const en = $('pn-enabled'); if (en) en.checked = !!d.enabled;
    const prov = $('pn-provider'); if (prov) prov.value = d.provider || 'wecom';
    const tok = $('pn-token');
    if (tok) {
        tok.placeholder = (d.provider === 'wecom')
            ? '粘贴 Webhook 地址（或 ?key= 后面那串）' : '粘贴 token / SendKey';
        if (d.token_set) tok.placeholder = '已配置（' + (d.token || '') + '），留空则不修改';
    }
    const nums = $('pn-nums'); if (nums) nums.checked = !!d['推号码'];
    const settle = $('pn-settle'); if (settle) settle.checked = !!d['推结算'];
    const img = $('pn-image'); if (img) img.checked = d['图片推送'] !== false;
    const lim = $('pn-limit'); if (lim) lim.value = (d['每日上限'] === undefined ? 0 : d['每日上限']);
    const gap = $('pn-gap'); if (gap) gap.value = (d['推送间隔秒'] === undefined ? 3.2 : d['推送间隔秒']);
    // 图片只有企业微信支持；其他渠道给个直观提示
    const imgWrap = $('pn-image') ? $('pn-image').parentElement : null;
    if (imgWrap) {
        const okImg = (d.provider === 'wecom');
        imgWrap.style.opacity = okImg ? '1' : '0.5';
        imgWrap.title = okImg ? '' : '当前渠道不支持图片消息，将自动降级为文字推送';
    }
    const board = $('pn-board'); if (board) board.value = d['看板地址'] || '';
    const badge = $('pn-badge');
    if (badge) {
        badge.textContent = d.ready ? '已启用' : '未启用';
        badge.style.background = d.ready ? '#e6f6e9' : '#f0f0f0';
        badge.style.color = d.ready ? '#2e7d32' : '#888';
        badge.style.padding = '2px 8px';
        badge.style.borderRadius = '10px';
    }
    if (box) {
        const st = (d.stats && d.stats['最近推送']) || null;
        const today = (d.stats && d.stats['今日已推']) || 0;
        const limit = d['每日上限'] || 0;
        box.innerHTML = (st
            ? `最近推送：${escSched(st['时间'] || '')} · ${escSched(st['结果'] || '')}（${escSched(st['说明'] || '')}）`
            : '尚未推送过')
            + ` · 今日已推 ${today} 条`
            + ` · 每日上限 ${limit > 0 ? limit : '不限'}`
            + ` · 图片推送 ${d['图片推送'] === false ? '关' : (d.provider === 'wecom' ? '开' : '开（渠道不支持，降级文字）')}`;
    }
}

async function pnSave() {
    const numOr = (id, dv) => {
        const el = $(id);
        if (!el) return dv;
        const v = parseFloat(el.value);
        return isNaN(v) ? dv : v;
    };
    const body = {
        enabled: !!($('pn-enabled') && $('pn-enabled').checked),
        provider: $('pn-provider') ? $('pn-provider').value : 'wecom',
        '推号码': !!($('pn-nums') && $('pn-nums').checked),
        '推结算': !!($('pn-settle') && $('pn-settle').checked),
        '图片推送': !!($('pn-image') && $('pn-image').checked),
        '每日上限': Math.max(0, Math.round(numOr('pn-limit', 0))),
        '推送间隔秒': numOr('pn-gap', 3.2),
        '看板地址': $('pn-board') ? $('pn-board').value.trim() : '',
    };
    const tok = $('pn-token');
    if (tok && tok.value.trim()) body.token = tok.value.trim();
    try {
        const d = await api('/api/settings/push-notify', { method: 'POST', body: JSON.stringify(body) });
        if (d.ok === false && d.error) { alert(d.error); return; }
        pnLoad();
        pnMsg('已保存');
    } catch (e) {
        alert('保存失败：' + e);
    }
}

async function pnTest() {
    try {
        const d = await api('/api/settings/push-notify/test', { method: 'POST', body: JSON.stringify({}) });
        pnLoad();
        const img = d.image ? (d.image.ok ? '，图片通道正常' : '，但图片失败：' + (d.image.detail || '')) : '';
        pnMsg(d.ok ? ('测试消息已发出' + img + '，请查看群消息') : ('发送失败：' + (d.detail || d.error || '')));
        if (!d.ok) alert('测试失败：' + (d.detail || d.error || '未知错误'));
    } catch (e) {
        alert('测试失败：' + e);
    }
}

function pnMsg(t) {
    const el = $('pn-msg');
    if (!el) return;
    el.textContent = t;
    el.style.color = '#2e7d32';
    setTimeout(() => { el.textContent = ''; }, 4000);
}

// 切换渠道时提示对应的凭据格式（企业微信是 Webhook 地址，PushPlus/Server酱是 token）
function pnHint() {
    const prov = $('pn-provider'), tok = $('pn-token');
    if (!prov || !tok || tok.value.trim()) return;
    tok.placeholder = (prov.value === 'wecom')
        ? '粘贴 Webhook 地址（或 ?key= 后面那串）' : '粘贴 token / SendKey';
}

// ===== 自动流水线（看板） =====
let autoTaskId = null;
let autoPollTimer = null;

// ===== 一键补齐 =====
let recoverTaskId = null;
let recoverPollTimer = null;

function runAutoPipeline(opts) {
    const lottery = (opts && opts.lottery) || $('auto-lottery').value;
    const mode = $('auto-mode').value;
    const modes = (opts && opts.modes !== undefined) ? opts.modes : mode;
    const skipPredict = $('auto-skip-predict').checked;
    const btn = $('auto-run-btn');
    const btnAllL = $('auto-run-all-lotteries');
    const btnAllM = $('auto-run-all-modes');
    const btnAllB = $('auto-run-all-both');

    btn.disabled = true;
    if (btnAllL) btnAllL.disabled = true;
    if (btnAllM) btnAllM.disabled = true;
    if (btnAllB) btnAllB.disabled = true;
    html('auto-result', '<div class="spinner"></div> 流水线执行中...');

    api('/api/auto', {
        method: 'POST',
        body: JSON.stringify({ lottery, modes, mode, skip_predict: skipPredict, groups: 5 })
    }).then(data => {
        autoTaskId = data.task_id;
        pollAutoStatus();
    });
}

function _reEnableAutoButtons() {
    const ids = ['auto-run-btn','auto-run-all-lotteries','auto-run-all-modes','auto-run-all-both'];
    ids.forEach(id => { const b = $(id); if (b) b.disabled = false; });
}

function _renderAutoBatchResult(r) {
    const subs = (r && r.sub_results) || [];
    const total = (r && r.total) || subs.length || 1;
    const done = (r && r.done != null) ? r.done : subs.length;
    let htmlStr = `<div class="alert alert-success"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 批流水线完成: ${done}/${total}</div>`;
    htmlStr += `<div style="font-size:12px;line-height:1.7">`;
    const byLot = {};
    for (const s of subs) {
        if (!byLot[s.lottery]) byLot[s.lottery] = [];
        byLot[s.lottery].push(s);
    }
    for (const lot of Object.keys(byLot)) {
        htmlStr += `<div style="margin-top:6px"><b>${lot}</b></div><ul style="margin:2px 0 0 18px;padding:0">`;
        for (const s of byLot[lot]) {
            const statusIcon = s.status === 'done'
                ? '✅'
                : (s.status === 'error' ? '❌' : (s.status === 'running' ? '⏳' : '·'));
            const tip = s.prediction ? ` — ${s.prediction}` : (s.error ? ` — ${s.error}` : '');
            htmlStr += `<li>${statusIcon} <code>${s.mode}</code>${tip}</li>`;
        }
        htmlStr += `</ul>`;
    }
    htmlStr += `</div>`;
    return htmlStr;
}

function pollAutoStatus() {
    if (!autoTaskId) return;
    autoPollTimer = setInterval(async () => {
        const data = await api(`/api/auto/status/${autoTaskId}`);
        if (data.status === 'running') {
            const r2 = data.result || {};
            const total = r2.total;
            const done = r2.done != null ? r2.done : 0;
            let info;
            if (total && total > 1) {
                info = `${data.message || '执行中...'}（${done}/${total}）`;
            } else {
                info = data.message || '执行中...';
            }
            html('auto-result', `<div class="spinner"></div> ${info}`);
        } else if (data.status === 'done') {
            clearInterval(autoPollTimer);
            const r = data.result;
            if (r && r.sub_results && r.sub_results.length) {
                html('auto-result', _renderAutoBatchResult(r));
            } else {
                let htmlStr = `<div class="alert alert-success"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 流水线完成</div><ul style="font-size:13px;line-height:1.8">`;
                for (const s of r.steps) htmlStr += `<li>${s}</li>`;
                htmlStr += `</ul>`;
                html('auto-result', htmlStr);
            }
            _reEnableAutoButtons();
            loadDataStatus();
            if (r.lottery) loadCompare(r.lottery);
            loadAutomationStatus();
        } else if (data.status === 'error') {
            clearInterval(autoPollTimer);
            html('auto-result', `<div class="alert alert-danger"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg> 流水线失败: ${data.error || '未知错误'}</div>`);
            _reEnableAutoButtons();
            loadAutomationStatus();
        } else if (data.status === 'not_found') {
            clearInterval(autoPollTimer);
            html('auto-result', '<div class="alert alert-danger">任务不存在</div>');
            _reEnableAutoButtons();
        }
    }, 1000);
}

// ===== 主动通知：看板顶部「今日自动化简报」 =====
const _DISMISS_KEY = 'lottery_dismissed_alerts';

function _getDismissed() {
    try { return JSON.parse(localStorage.getItem(_DISMISS_KEY) || '[]'); }
    catch (e) { return []; }
}
function _setDismissed(keys) {
    try { localStorage.setItem(_DISMISS_KEY, JSON.stringify(keys.slice(-200))); }
    catch (e) {}
}
function _dismissKey(type, lottery, run) {
    const t = (run && run.time) ? run.time.replace(/[^0-9]/g, '') : 'none';
    return `${type}:${lottery}:${t}`;
}
function _isDismissed(key) { return _getDismissed().includes(key); }
function _dismissAlert(key) {
    const list = _getDismissed();
    if (!list.includes(key)) { list.push(key); _setDismissed(list); }
    const el = document.querySelector(`[data-dismiss-key="${key}"]`);
    if (el) { el.style.opacity = '0'; el.style.transform = 'translateY(-8px)'; setTimeout(() => el.remove(), 250); }
}
function _clearDismissedAlerts() {
    _setDismissed([]);
    loadAutomationStatus();
}

function _renderAutoBrief(data) {
    const box = $('auto-brief');
    if (!box) return;
    const byLottery = data.last_run_by_lottery || {};
    const mainLotteries = ['双色球', '大乐透'];
    const allLotteries = ['双色球', '大乐透', '排列5', '福彩3D', '排列3', '七星彩'];
    const hasAny = allLotteries.some(l => byLottery[l]);
    if (!hasAny) { box.innerHTML = ''; return; }

    const closeBtn = (key) => `<button onclick="_dismissAlert('${key}')" title="关闭这条通知"
        style="margin-left:auto;background:none;border:none;font-size:18px;line-height:1;color:#999;cursor:pointer;padding:0 2px"
        onmouseenter="this.style.color='#c62828'" onmouseleave="this.style.color='#999'">×</button>`;

    // 高等级命中 → 醒目告警（检查全部彩种）
    let alertHtml = '';
    for (const lottery of allLotteries) {
        const run = byLottery[lottery];
        if (!run) continue;
        const hit = run.hit_summary || '';
        if (!hit.includes('<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg>')) continue;
        const key = _dismissKey('alert', lottery, run);
        if (_isDismissed(key)) continue;
        // 数字型命中等级为「精确全中」等，红蓝为一至六等；取第一个命中描述作为筛选参数
        const prize = hit.match(/命中\s*([^，×]+)/)?.[1]?.trim() || '全部';
        alertHtml += `<div data-dismiss-key="${key}" style="margin-bottom:10px;padding:12px 40px 12px 16px;border-radius:10px;
            background:linear-gradient(90deg,#fff3e0,#ffe0e0);border:1px solid #ffb74d;color:#b26a00;
            font-weight:bold;font-size:15px;position:relative;transition:all .25s ease">
            <div style="display:flex;align-items:flex-start;gap:8px">
                <span><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg> ${lottery} 有预测命中！</span>
                ${closeBtn(key)}
            </div>
            <div style="font-size:13px;font-weight:normal;margin-top:2px">${hit} —— 快去
            <a href="/hits?lottery=${encodeURIComponent(lottery)}&min_prize=${encodeURIComponent(prize)}" style="color:#c62828">命中历史记录</a> 查看明细。</div></div>`;
    }

    // 双色球 / 大乐透 各一张简报卡片
    let cardsHtml = '';
    let dismissedCount = 0;
    for (const lottery of mainLotteries) {
        const run = byLottery[lottery];
        if (!run) {
            cardsHtml += `<div class="card" style="margin-bottom:15px;background:var(--color-background-secondary,#f8f9fa);border-left:4px solid #bbb">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <strong style="font-size:15px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 11v2l13 4V7L3 11z"/><path d="M16 8a4 4 0 0 1 0 8"/><path d="M3 13h2"/></svg> ${lottery} 自动化简报</strong>
                    <span style="font-size:12px;color:#888">暂无记录</span>
                </div>
                <div class="alert alert-info" style="margin-top:10px;font-size:13px">该彩种尚未运行过自动流水线。点击上方「一键流水线」选择「${lottery}」即可开始。</div>
            </div>`;
            continue;
        }
        const key = _dismissKey('brief', lottery, run);
        if (_isDismissed(key)) { dismissedCount++; continue; }
        const okColor = run.success ? '#2e7d32' : '#c62828';
        const hit = run.hit_summary || '';
        const dn = run.draw_numbers || {};
        const preds = run.predictions || [];
        const dnHtml = (dn && dn.期号) ? `<span style="font-size:12px;color:var(--gray)">
            ${lottery} 最新开奖 ${dn.期号}：</span>
            ${renderDrawNumbers(dn)}` : '';
        const predHtml = preds.length ? `<span style="font-size:12px;color:var(--gray)">
            已生成下一期预测 <strong>${preds.length}</strong> 组</span>` : '';
        const hitBadge = hit ? `<span style="margin-left:auto;font-size:12px;padding:2px 8px;border-radius:10px;${hit.includes('<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg>') ? 'background:#fff3e0;color:#b26a00;font-weight:bold' : 'color:var(--gray)'}">${hit}</span>` : '';

        cardsHtml += `<div data-dismiss-key="${key}" class="card" style="margin-bottom:15px;background:linear-gradient(135deg,#f5f9ff,#eef4fb);
            border-left:4px solid ${lottery === '双色球' ? '#e74c3c' : '#3498db'};transition:all .25s ease">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
                <strong style="font-size:15px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 11v2l13 4V7L3 11z"/><path d="M16 8a4 4 0 0 1 0 8"/><path d="M3 13h2"/></svg> ${lottery} 自动化简报</strong>
                ${closeBtn(key)}
            </div>
            <div style="margin-top:4px;display:flex;align-items:center;flex-wrap:wrap;gap:8px">
                <span style="font-size:12px;color:${okColor}">${run.success ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg>' : '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>'} ${(run.time || '').replace('T', ' ').slice(0, 16)}</span>
                ${hitBadge}
            </div>
            <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:10px;align-items:center">
                ${dnHtml} ${predHtml}
            </div>
            <div style="margin-top:8px;font-size:13px"><strong>运行结论：</strong>
                <span style="color:var(--gray)">${run.message || ''}</span></div>
            <div style="margin-top:6px">
                <button class="btn btn-sm btn-info" onclick="try{showAutoDetail('${lottery}');}catch(e){alert('页面脚本未加载，请刷新页面（Ctrl/Cmd+Shift+R）');}" title="查看 ${lottery} 自动流水线最近一次做了什么：抓到的开奖号、生成的预测、异常告警、命中情况、执行步骤"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4V3h6v1"/><path d="M9 9h6M9 13h6M9 17h6"/></svg> 查看 ${lottery} 明细</button>
                <a href="/hits?lottery=${encodeURIComponent(lottery)}" class="btn btn-sm btn-default" style="margin-left:6px">${ico('target')} ${lottery} 命中历史</a>
            </div>
        </div>`;
    }

    let restoreHtml = '';
    if (dismissedCount > 0) {
        restoreHtml = `<div style="text-align:right;margin-bottom:10px;font-size:12px">
            <a href="javascript:_clearDismissedAlerts()" style="color:var(--gray);text-decoration:underline">已关闭 ${dismissedCount} 条简报，点击重新显示</a>
        </div>`;
    }

    box.innerHTML = `${alertHtml}${restoreHtml}<div class="grid-2" style="margin-bottom:0">${cardsHtml}</div>`;
}

// ===== 查看运行明细（弹开 auto-detail 面板） =====
// 设计要点：点击“立即”弹出面板并给加载提示，绝不静默；
// 优先复用首页已加载的数据，否则自己拉一次，不依赖首页初始化是否成功。
// lottery: 指定彩种（双色球/大乐透）；不传则回退到全局 last_run。
async function showAutoDetail(lottery = null) {
    const box = $('auto-detail');
    if (!box) return;
    const title = lottery ? `${lottery} 运行明细` : '最近一次运行明细';
    // ① 立即给出可见反馈，杜绝“点了没反应”
    box.style.display = 'block';
    box.innerHTML = `<div class="card" style="margin-top:10px"><div class="alert alert-info"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M7 3h10M7 21h10M7 3c0 5 5 5 5 9s-5 4-5 9M17 3c0 5-5 5-5 9s5 4 5 9"/></svg> 正在加载 ${title}...</div></div>`;
    if (box.scrollIntoView) { try { box.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) {} }

    // ② 取数据：优先复用，否则自己拉
    let data = window._autoData;
    if (!data) {
        try {
            data = await api('/api/automation-status');
            window._autoData = data;
        } catch (e) {
            box.innerHTML = '<div class="card" style="margin-top:10px"><div class="alert alert-danger"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 加载失败：' +
                (e && e.message ? e.message : e) + '<br>请确认 Web 服务已启动，然后重试或刷新页面。</div></div>';
            return;
        }
    }
    renderAutoDetail(data, lottery);
}

// 真正渲染明细内容（与 showAutoDetail 解耦，便于复用）
function renderAutoDetail(data, lottery = null) {
    const box = $('auto-detail');
    if (!box) return;
    // 指定彩种优先从 last_run_by_lottery 取；未指定则使用全局 last_run
    let last = data.last_run;
    if (lottery && data.last_run_by_lottery && data.last_run_by_lottery[lottery]) {
        last = data.last_run_by_lottery[lottery];
    }
    const title = lottery || (last && last.lottery) || '最近一次运行';
    if (!last) {
        box.innerHTML = `<div class="card" style="margin-top:10px"><div class="alert alert-info">` +
            `${ico('inbox')} ${lottery || '该彩种'} 还没有运行记录。点上方「${ico('robot')} 一键流水线」跑一次，或等开奖日 21:30 定时任务自动运行后，` +
            `这里就会显示上次自动做了什么（抓到的开奖号、生成的预测、异常告警、命中情况）。</div></div>`;
        return;
    }
    const dn = last.draw_numbers || {};
    const preds = last.predictions || [];
    const flags = last.anomaly_flags || [];
    const rt = last.retrain || null;

    let html = `<div class="card" style="margin-top:10px">
        <h3 style="display:flex;justify-content:space-between;align-items:center">
            <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg> ${title} 运行明细
            <button class="btn btn-sm btn-default" onclick="document.getElementById('auto-detail').style.display='none'">✕ 关闭</button>
        </h3>
        <div style="font-size:13px"><strong>${last.lottery}</strong> · ${(last.time || '').replace('T', ' ')}
            · 耗时 ${last.duration_sec || '-'}s · ${last.success ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 成功' : '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg> 失败'}</div>`;

    if (dn.期号) {
        html += `<div style="margin-top:8px"><strong style="font-size:12px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M5 5h14v14H5z"/><path d="M3 12h5l2 3h4l2-3h5"/></svg> 抓取开奖 ${dn.期号}${dn.日期 ? '（' + dn.日期 + '）' : ''}：</strong><br>
            ${renderDrawNumbers(dn)}</div>`;
    }
    if (preds.length) {
        html += `<div style="margin-top:8px"><strong style="font-size:12px">${ico('target')} 生成预测 ${preds.length} 组：</strong>`;
        preds.forEach((g, i) => {
            const conf = (g.置信度 != null) ? Number(g.置信度).toFixed(2) : '-';
            html += `<div style="margin:6px 0;font-size:12px;display:flex;align-items:center;flex-wrap:wrap;gap:4px 0">第${i + 1}组
                ${renderPredNumbers(_ticketZoneDict(g), { size: 22, showLabels: false })}
                <span style="color:var(--gray);margin-left:6px">${g.策略 || ''} · 置信度${conf}</span></div>`;
        });
        html += `</div>`;
    }
    if (flags.length) {
        html += `<div style="margin-top:8px"><strong style="font-size:12px;color:#e67e22"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 异常告警：</strong><br>` +
            flags.map(f => `<span style="display:inline-block;margin:2px;padding:2px 8px;background:#fff3e0;color:#e67e22;border-radius:10px;font-size:12px">${f}</span>`).join('') + `</div>`;
    }
    if (rt) {
        html += `<div style="margin-top:8px;font-size:12px;color:var(--gray)"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg> 模型重训：窗口 ${rt.window}
            · 命中率 ${(Number(rt.hit_rate) * 100).toFixed(2)}% (${rt.model})</div>`;
    }
    // 命中摘要（真预测/训练回测标签 + × 关闭按钮）
    html += _renderAutoHitSection(last, title);
    html += `<div style="margin-top:10px"><strong style="font-size:12px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 4h11a2 2 0 0 1 2 2v12H6z"/><path d="M6 4a2 2 0 0 0-2 2v0a2 2 0 0 0 2 2h2"/></svg> 执行步骤：</strong>
        <ol style="font-size:12px;color:var(--gray);margin:4px 0 0 18px">`;
    (last.steps || []).forEach(s => { html += `<li>${s}</li>`; });
    html += `</ol></div>`;
    html += `<div style="margin-top:8px">
        <button class="btn btn-sm btn-default" onclick="loadAutoLog()"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg> 查看原始流水线日志</button></div>
        <div id="auto-log-box" style="display:none;margin-top:8px;max-height:220px;overflow:auto;
        background:#1e1e1e;color:#d4d4d4;padding:10px;border-radius:6px;font-size:11px;white-space:pre-wrap"></div>`;
    html += `</div>`;
    box.innerHTML = html;
}

// ===== 拉取当日流水线原始日志 =====
async function loadAutoLog() {
    const box = $('auto-log-box');
    if (!box) return;
    if (box.style.display === 'block') { box.style.display = 'none'; return; }
    box.textContent = '加载流水线日志...';
    box.style.display = 'block';
    try {
        const data = await api('/api/automation-log');
        box.textContent = (data && data.content) ? data.content : '（无日志内容）';
    } catch (e) {
        box.textContent = '日志加载失败: ' + e;
    }
}

// ===== 从 500.com 更新开奖数据（异步任务） =====
let updateTaskTimers = {};

function updateLotteryData(lottery) {
    const btnId = lottery === '双色球' ? 'ssq-update-btn' : 'dlt-update-btn';
    const btn = $(btnId);
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = '更新中...';

    api('/api/update/' + encodeURIComponent(lottery), { method: 'POST' }).then(data => {
        if (data.task_id) {
            pollUpdateStatus(lottery, data.task_id);
        } else {
            btn.disabled = false; btn.innerHTML = ico('down') + ' 更新开奖数据';
            showAlert('danger', '启动更新失败: ' + (data.error || '未知错误'));
        }
    }).catch(err => {
        btn.disabled = false; btn.innerHTML = ico('down') + ' 更新开奖数据';
        showAlert('danger', '请求失败: ' + err);
    });
}

function pollUpdateStatus(lottery, taskId) {
    const btnId = lottery === '双色球' ? 'ssq-update-btn' : 'dlt-update-btn';
    const btn = $(btnId);

    if (updateTaskTimers[taskId]) clearInterval(updateTaskTimers[taskId]);
    updateTaskTimers[taskId] = setInterval(async () => {
        const data = await api(`/api/update/status/${taskId}`);
        if (data.status === 'running') {
            if (btn) btn.innerHTML = ico('hourglass') + ' ' + (data.message || '更新中...');
        } else if (data.status === 'done') {
            clearInterval(updateTaskTimers[taskId]);
            delete updateTaskTimers[taskId];
            if (btn) { btn.disabled = false; btn.innerHTML = ico('down') + ' 更新开奖数据'; }

            const r = data.result || {};
            const fetched = r.fetched || 0;
            if (fetched > 0) {
                showAlert('success', `${lottery} 更新完成：新增 ${fetched} 期（${r.latest_before} → ${r.latest_after}）`);
            } else {
                showAlert('info', `ℹ ${lottery} 已是最新（最新期号 ${r.latest_after}）`);
            }
            // 刷新数据状态卡片
            loadDataStatus();
            loadCompare(lottery);
        } else if (data.status === 'error') {
            clearInterval(updateTaskTimers[taskId]);
            delete updateTaskTimers[taskId];
            if (btn) { btn.disabled = false; btn.innerHTML = ico('down') + ' 更新开奖数据'; }
            showAlert('danger', `${lottery} 更新失败: ${data.error || '未知错误'}`);
        } else if (data.status === 'not_found') {
            clearInterval(updateTaskTimers[taskId]);
            delete updateTaskTimers[taskId];
            if (btn) { btn.disabled = false; btn.innerHTML = ico('down') + ' 更新开奖数据'; }
            showAlert('danger', '更新任务不存在');
        }
    }, 1000);
}

// ===== 对比分析（首页 / 对比页） =====
async function loadCompare(lottery) {
    const box = $('compare-content');
    if (box) box.innerHTML = '<div class="alert alert-info"><span class="spinner"></span> 正在加载对比数据...</div>';

    const data = await api(`/api/compare/${lottery}`);
    if (data.error) {
        html('compare-content', `<div class="alert alert-warning">${data.error}</div>`);
        return;
    }

    html('compare-content', _renderCompareBlock(data, false));
}

// 渲染单个彩种的「预测 vs 实际」区块（首页内联 / 对比页复用）
function _renderCompareBlock(data, standalone = false) {
    if (data.error) {
        return `<div class="card" style="margin-bottom:15px"><h3>${data.lottery || '未知彩种'}</h3><div class="alert alert-warning">${data.error}</div></div>`;
    }

    const zones = data.actual_zones || [];
    const isRb = data.is_redblue;
    const actualBalls = renderZoneBalls(zones, 'numbers', { size: standalone ? 30 : 32, showLabels: true });

    let htmlStr = `
    <div class="compare-banner">
        <div class="compare-banner-title">最新开奖 <strong>${data.actual_draw}期</strong> (${data.actual_date || '未知日期'})</div>
        <div class="compare-banner-balls">${actualBalls}</div>
    </div>`;

    if (data.predictions.length > 0) {
        const p = data.predictions[0];
        htmlStr += `<div class="compare-pred-title">最近预测: <span class="tag tag-predict">${p.record_dir}</span></div>`;

        // 动态表头：按实际开奖分区渲染
        let header = '<tr><th style="width:50px">#</th>';
        for (const z of zones) {
            header += `<th>${z.name}</th>`;
        }
        header += '<th style="width:90px">总命中</th><th style="width:100px">置信度</th></tr>';
        htmlStr += `<table class="compare-table"><thead>${header}</thead><tbody>`;

        for (const g of p.groups_detail) {
            const predZones = g.pred_zones || [];
            const zoneHits = g.zone_hits || {};
            const predMap = {};
            for (const pz of predZones) predMap[pz.name] = pz;

            let cells = `<td><span class="compare-group-id">#${g.group}</span></td>`;
            for (const z of zones) {
                const pz = predMap[z.name] || { numbers: [] };
                const nums = renderZoneBalls([{ name: z.name, pred: pz.numbers, cls: z.cls }], 'pred', { size: standalone ? 24 : 28, showLabels: false });
                cells += `<td>${nums}<div style="font-size:11px;color:var(--gray);margin-top:2px">命中 ${zoneHits[z.name] || 0}</div></td>`;
            }
            const hitLevel = g.total_hits >= 4 ? 'high' : g.total_hits >= 2 ? 'mid' : g.total_hits > 0 ? 'low' : 'none';
            cells += `<td><span class="compare-hit hit-${hitLevel}">${g.total_hits}</span></td><td><span class="compare-conf">${Number(g.confidence || 0).toFixed(4)}</span></td>`;
            htmlStr += `<tr>${cells}</tr>`;
        }
        htmlStr += `</tbody></table>`;

        if (data.summary) {
            const s = data.summary;
            const avgTotal = Number(s.avg_total_hits_per_group || 0);
            const verdictType = avgTotal >= 1 ? 'success' : 'warning';
            let summaryHtml = '<div class="grid-4 compare-summary">';
            // 红蓝彩种保留红/蓝专属指标，数字型用通用指标
            if (isRb) {
                summaryHtml += `
                  <div class="card"><div class="stat-value">${s.avg_red_hits_per_group}</div><div class="stat-label">平均红球命中/组</div></div>
                  <div class="card"><div class="stat-value">${s.avg_blue_hits_per_group}</div><div class="stat-label">平均蓝球命中/组</div></div>
                  <div class="card"><div class="stat-value">${s.overall_red_hit_rate}</div><div class="stat-label">红球命中率</div></div>`;
            } else {
                summaryHtml += `
                  <div class="card"><div class="stat-value">${s.avg_total_hits_per_group}</div><div class="stat-label">平均总命中/组</div></div>
                  <div class="card"><div class="stat-value">${s.overall_hit_rate}</div><div class="stat-label">总命中率</div></div>
                  <div class="card"><div class="stat-value">${s.historical_avg}</div><div class="stat-label">历史热号基准</div></div>`;
            }
            summaryHtml += `
              <div class="card"><div class="stat-value">${s.best_group}</div><div class="stat-label">最佳组</div></div>
            </div>
            <div class="alert alert-${verdictType}">${s.verdict}</div>`;
            htmlStr += summaryHtml;
        }
    } else {
        htmlStr += `<div class="alert alert-warning">暂无预测记录，请先运行预测</div>`;
    }
    return htmlStr;
}

// 对比页专用：同时渲染全部彩种
async function loadCompareBoth() {
    const box = $('compare-both-content');
    if (!box) return;
    box.innerHTML = '<div class="alert alert-info"><span class="spinner"></span> 正在加载全部彩种同轨对比...</div>';

    const ALL_LOTTERIES = ['双色球', '大乐透', '排列5', '福彩3D', '排列3', '七星彩'];
    try {
        const results = await Promise.all(ALL_LOTTERIES.map(l =>
            api(`/api/compare/${encodeURIComponent(l)}`).catch(e => ({ lottery: l, error: e.message || e }))
        ));

        let htmlStr = '';
        for (const data of results) {
            const lottery = data.lottery || data.lottery_name || '未知彩种';
            if (data.error) {
                htmlStr += `<div class="card" style="margin-bottom:15px"><h3>${lottery}</h3><div class="alert alert-warning">${data.error}</div></div>`;
                continue;
            }
            const iconColor = EV_COLORS[lottery] || '#666';
            htmlStr += `
            <div class="card" style="margin-bottom:25px">
                <div class="compare-section-header">
                    <h3 style="margin:0"><svg width="14" height="14" viewBox="0 0 24 24" style="vertical-align:-2px;margin-right:4px"><circle cx="12" cy="12" r="9" fill="${iconColor}"/></svg> ${lottery}</h3>
                    <span class="tag tag-predict">最新 ${data.actual_draw}期</span>
                </div>
                ${_renderCompareBlock(data, true)}
            </div>`;
        }
        box.innerHTML = htmlStr;
    } catch (e) {
        box.innerHTML = `<div class="alert alert-danger">加载失败：${e.message || e}</div>`;
    }
}

// ===== 统计面板 =====
async function loadStats(lottery) {
    html('stats-result', '<div class="spinner"></div> 加载中...');
    const data = await api(`/api/stats/${lottery}`);

    const totalWinPct = (data.total_win_prob * 100).toFixed(2);
    const ev = data.expected_value.单注期望收益;
    const headProb = data.expected_value.各奖级期望明细[0].probability;
    const headOdds = headProb > 0 ? (1 / headProb).toFixed(0) : '?';

    let htmlStr = `
    <div class="grid-4">
      <div class="card">
        <div class="stat-value">${data.summary.total_records}</div>
        <div class="stat-label">总期数</div>
        <div class="stat-hint">已收录的开奖记录数，数据越全统计越稳</div>
      </div>
      <div class="card">
        <div class="stat-value">${totalWinPct}%</div>
        <div class="stat-label">单注中奖率</div>
        <div class="stat-hint">买 1 注能中任意奖级（含5元末奖）的概率</div>
      </div>
      <div class="card">
        <div class="stat-value">${ev}</div>
        <div class="stat-label">单注期望(元)</div>
        <div class="stat-hint">${ev < 0 ? '长期看每花2元平均亏 ' + Math.abs(ev).toFixed(3) + ' 元——这是彩票的正常状态' : '当前奖池让期望转正，值得关注'}</div>
      </div>
      <div class="card">
        <div class="stat-value">1 / ${headOdds}</div>
        <div class="stat-label">头奖概率</div>
        <div class="stat-hint">${(data.total_win_prob*100).toFixed(2)}% 是"中奖率"，这个才是"中头奖"的难度</div>
      </div>
    </div>

    <div class="card">
      <h3>奖级概率表</h3>
      <p class="stat-hint" style="margin:4px 0 10px">每注号码命中各奖级的概率。奖金越高概率越低——这就是彩票的"分层赔率"设计</p>
      <table>
        <tr><th>奖级</th><th>条件</th><th>概率</th><th>奖金</th></tr>`;

    for (const l of data.prize_levels) {
        htmlStr += `<tr><td>${l.prize_name}</td><td>${l.condition}</td><td>${(l.probability * 100).toFixed(6)}%</td><td>${l.fixed_amount || '浮动'}</td></tr>`;
    }
    htmlStr += `</table></div>`;

    // 冷热号（通用：遍历 summary.zones，兼容红球/蓝球与数字型各分区）
    const s = data.summary;
    const zoneBalls = (arr, cls) => (arr || []).map(([n, f]) =>
      `<span class="${cls}" style="margin:2px">${String(n).padStart(2, '0')}</span> <span style="font-size:12px;color:var(--gray)">${f}次</span>&nbsp;`).join('');
    let zoneCards = '';
    for (const [zname, z] of Object.entries(s.zones || {})) {
      const ballCls = zname === '红球' ? 'red-ball' : zname === '蓝球' ? 'blue-ball' : 'red-ball';
      const coldCls = ballCls + ' cold';
      zoneCards += `<div class="card" style="flex:1;min-width:220px">
        <h3>${zname} Top5 热号</h3>
        ${zoneBalls(z.hot_5, ballCls)}
        <h3 style="margin-top:15px">${zname} Bottom5 冷号</h3>
        ${zoneBalls(z.cold_5, coldCls)}
        <p class="stat-hint" style="margin-top:10px">热号 = 历史上出现多的号码，冷号 = 出现少的。它们只是历史统计，不代表"更容易出"。</p>
      </div>`;
    }
    htmlStr += `<div style="display:flex;flex-wrap:wrap;gap:12px">${zoneCards}</div>`;

    html('stats-result', htmlStr);
}

// ===== Pipeline =====
let pipelineTaskId = null;
let pipelinePollTimer = null;

function loadPipeline(lottery) {
    html('pipeline-result', '<div class="spinner"></div> 执行 pipeline 中...');

    api(`/api/pipeline/${lottery}`).then(data => {
        pipelineTaskId = data.task_id;
        pollPipelineStatus();
    });
}

function pollPipelineStatus() {
    if (!pipelineTaskId) return;
    pipelinePollTimer = setInterval(async () => {
        const data = await api(`/api/pipeline/status/${pipelineTaskId}`);
        if (data.status === 'running') {
            html('pipeline-result', `<div class="spinner"></div> 计算中... ${data.message || ''}`);
        } else if (data.status === 'done') {
            clearInterval(pipelinePollTimer);
            renderPipelineResult(data.result);
        } else if (data.status === 'error') {
            clearInterval(pipelinePollTimer);
            html('pipeline-result', `<div class="alert alert-danger">计算失败: ${data.error || '未知错误'}</div>`);
        } else if (data.status === 'not_found') {
            clearInterval(pipelinePollTimer);
            html('pipeline-result', '<div class="alert alert-danger">任务不存在</div>');
        }
    }, 1000);
}

function renderPipelineResult(data) {
    const stepNames = {1:'基础概率', 2:'收益期望', 3:'统计分布', 4:'风险度量', 5:'风控投注'};
    let htmlStr = '';

    for (const [stepNum, stepData] of Object.entries(data)) {
        const sn = stepNames[stepNum] || `Step ${stepNum}`;
        htmlStr += `<div class="card"><h3>Step ${stepNum}: ${sn}</h3>`;

        if (stepNum === '1') {
            htmlStr += `<p>总组合数: <strong>${stepData.total_combinations.toLocaleString()}</strong> | 头奖概率: <strong>1/${Math.round(1/stepData.head_prize_prob).toLocaleString()}</strong> | 总中奖概率: <strong>${(stepData.total_win_prob*100).toFixed(2)}%</strong></p>`;
            htmlStr += `<table><tr><th>奖级</th><th>条件</th><th>概率</th><th>奖金</th></tr>`;
            for (const l of stepData.prize_levels) {
                htmlStr += `<tr><td>${l.name}</td><td>${l.condition}</td><td>${(l.prob*100).toFixed(6)}%</td><td>${l.amount}</td></tr>`;
            }
            htmlStr += `</table>`;
        } else if (stepNum === '2') {
            htmlStr += `<p>单注期望收益: <strong>${stepData.expected_return}</strong> 元 | 理论返奖率: <strong>${stepData.payout_rate}%</strong></p>`;
            htmlStr += `<div class="alert alert-warning">${stepData.conclusion}</div>`;
        } else if (stepNum === '3') {
            htmlStr += `<p>100期至少中一次: <strong>${(stepData.binom_100*100).toFixed(2)}%</strong> | 首次中奖期望: <strong>${stepData.first_win_expect}</strong> 期 | 模拟盈利比例: <strong>${stepData.sim_profit_rate}%</strong></p>`;
        } else if (stepNum === '4') {
            htmlStr += `<p>标准差: <strong>${stepData.std_dev}</strong> | 信息熵: <strong>${stepData.entropy}</strong> | 1000期 95%置信区间: <strong>(${stepData.ci_95})</strong></p>`;
        } else if (stepNum === '5') {
            htmlStr += `<p>凯利比例: <strong>${stepData.kelly}</strong> | 夏普比率: <strong>${stepData.sharpe}</strong></p>`;
            if (stepData.fairness_red && stepData.fairness_blue) {
                htmlStr += `<p>红球公平性: ${stepData.fairness_red} | 蓝球公平性: ${stepData.fairness_blue}</p>`;
            } else if (stepData.fairness_zones) {
                const fairnessItems = Object.entries(stepData.fairness_zones).map(([z, c]) => `${z}: ${c}`).join(' · ');
                htmlStr += `<p>分区公平性: ${fairnessItems}</p>`;
            }
        }
        htmlStr += `</div>`;
    }
    html('pipeline-result', htmlStr);
}

// ===== 训练 =====
let trainTaskId = null;
let trainPollTimer = null;

function startTrain() {
    const lottery = $('train-lottery').value;
    const iterations = parseInt($('train-iterations').value) || 100;
    const modelType = $('train-model').value;
    const patience = parseInt($('train-patience').value) || 10;
    const windowList = ($('train-windows').value || '30,50,100').split(',').map(Number);

    hide('train-result');
    show('train-progress');
    html('train-progress-text', '启动训练...');
    html('train-progress-fill', '0%');
    $('train-progress-fill').style.width = '0%';
    $('train-start').disabled = true;

    api('/api/train/start', {
        method: 'POST',
        body: JSON.stringify({ lottery, iterations, model_type: modelType, patience, window_list: windowList })
    }).then(data => {
        trainTaskId = data.task_id;
        pollTrainStatus();
    });
}

function pollTrainStatus() {
    if (!trainTaskId) return;
    trainPollTimer = setInterval(async () => {
        const data = await api(`/api/train/status/${trainTaskId}`);
        if (data.status === 'running') {
            html('train-progress-text', data.message || '训练中...');
            $('train-progress-fill').style.width = data.progress + '%';
            html('train-progress-fill', data.progress + '%');
        } else if (data.status === 'done') {
            clearInterval(trainPollTimer);
            html('train-progress-fill', '100%');
            html('train-progress-text', '训练完成！');
            hide('train-progress');
            show('train-result');
            $('train-start').disabled = false;

            const r = data.result;
            const selAdv = r.selection_advantage != null ? r.selection_advantage : 0;
            const sigClass = r.selection_significance && r.selection_significance.includes('显著优于') ? 'alert-success'
                : r.selection_significance && r.selection_significance.includes('显著差于') ? 'alert-danger'
                : 'alert-info';
            html('train-result', `
              <div class="alert ${sigClass}">
                <strong>训练完成！</strong> 最优窗口: ${r.best_window} 期
                （损失: ${r.best_loss.toFixed(4)}）<br>
                选号平均命中: ${(r.selection_total_hits || 0).toFixed(3)}
                · 优势 over 随机: ${selAdv >= 0 ? '+' : ''}${selAdv.toFixed(3)}
                · p=${(r.selection_p_value || 1).toFixed(4)}
                · ${r.selection_significance || '未计算'}
              </div>
              <div class="card"><h3>多窗口对比（选号命中 vs 随机基线）</h3>
              <table>
                <tr><th>窗口</th><th>最优损失</th><th>选号命中</th><th>随机基线</th><th>优势 (Δ)</th><th>p 值</th><th>显著性</th></tr>
                ${Object.entries(r.window_results || {}).map(([w, wr]) => {
                  const adv = wr.total_advantage != null ? wr.total_advantage : 0;
                  const sig = wr.significance_note || '未计算';
                  return `<tr>
                    <td>${w} 期</td>
                    <td>${wr.best_loss.toFixed(4)}</td>
                    <td>${(wr.selection_total_hits || 0).toFixed(3)}</td>
                    <td>${(wr.expected_total_hits || 0).toFixed(3)}</td>
                    <td>${adv >= 0 ? '+' : ''}${adv.toFixed(3)}</td>
                    <td>${(wr.p_value || 1).toFixed(4)}</td>
                    <td>${sig}</td>
                  </tr>`;
                }).join('')}
              </table></div>
              <p>报告目录: ${r.record_dir}</p>
            `);
        } else if (data.status === 'error') {
            clearInterval(trainPollTimer);
            html('train-progress-text', '训练失败');
            hide('train-progress');
            $('train-start').disabled = false;
            showAlert('danger', '训练失败: ' + (data.error || '未知错误'));
        }
    }, 1000);
}

// ===== 滚动预测训练 =====
let rollingTaskId = null;
let rollingPollTimer = null;

function startRolling() {
    const lottery = $('roll-lottery').value;
    const windowSize = parseInt($('roll-window').value) || 50;
    const evalPeriods = parseInt($('roll-eval-periods').value) || 100;

    hide('roll-result');
    show('roll-progress');
    html('roll-progress-text', '启动滚动训练...');
    html('roll-progress-fill', '0%');
    $('roll-progress-fill').style.width = '0%';
    $('roll-start').disabled = true;

    api('/api/rolling/start', {
        method: 'POST',
        body: JSON.stringify({ lottery, window_size: windowSize, eval_periods: evalPeriods })
    }).then(data => {
        rollingTaskId = data.task_id;
        pollRollingStatus();
    });
}

function pollRollingStatus() {
    if (!rollingTaskId) return;
    rollingPollTimer = setInterval(async () => {
        const data = await api(`/api/rolling/status/${rollingTaskId}`);
        if (data.status === 'running') {
            html('roll-progress-text', data.message || '滚动训练中...');
            $('roll-progress-fill').style.width = data.progress + '%';
            html('roll-progress-fill', data.progress + '%');
        } else if (data.status === 'done') {
            clearInterval(rollingPollTimer);
            html('roll-progress-fill', '100%');
            html('roll-progress-text', '滚动训练完成！');
            hide('roll-progress');
            show('roll-result');
            $('roll-start').disabled = false;

            const r = data.result;
            const isRb = !!r.is_redblue;  // 双色球/大乐透为 true，数字型为 false
            let statsRows = '';
            for (const [name, s] of Object.entries(r.strategy_stats || {})) {
                if (s.error) {
                    const errColspan = isRb ? 7 : 6;
                    statsRows += `<tr><td>${name}</td><td colspan="${errColspan}">无数据</td></tr>`;
                    continue;
                }
                if (isRb) {
                    statsRows += `<tr>
                        <td>${_strategyBadge(name)}</td>
                        <td>${s.样本数}</td>
                        <td>${s.平均红球命中}</td>
                        <td>${s.平均蓝球命中}</td>
                        <td>${s.平均总命中}</td>
                        <td>${s.中奖次数}</td>
                        <td>${(s.中奖率*100).toFixed(2)}%</td>
                    </tr>`;
                } else {
                    const zoneHits = Object.entries(s.分区平均命中 || {})
                        .map(([z, v]) => `${z}:${v}`).join(' ');
                    statsRows += `<tr>
                        <td>${_strategyBadge(name)}</td>
                        <td>${s.样本数}</td>
                        <td>${s.平均总命中}</td>
                        <td>${zoneHits}</td>
                        <td>${s.中奖次数}</td>
                        <td>${(s.中奖率*100).toFixed(2)}%</td>
                    </tr>`;
                }
            }
            const statsHeader = isRb
                ? `<tr><th>策略</th><th>样本数</th><th>平均红球命中</th><th>平均蓝球命中</th><th>平均总命中</th><th>中奖次数</th><th>中奖率</th></tr>`
                : `<tr><th>策略</th><th>样本数</th><th>平均总命中</th><th>分区平均命中</th><th>中奖次数</th><th>中奖率</th></tr>`;
            html('roll-result-content', `
              <div class="alert alert-success">滚动训练完成！窗口 ${r.window_size} 期，评估 ${r.eval_periods} 期，共 ${r.total_predictions} 次预测</div>
              <div class="card"><h3>各策略表现对比</h3>
              <table>${statsHeader}
              ${statsRows}
              </table></div>
              <p><button class="btn btn-sm btn-info" onclick="viewRollingReport('${r.record_dir}')"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg> 查看完整报告</button></p>
            `);
        } else if (data.status === 'error') {
            clearInterval(rollingPollTimer);
            html('roll-progress-text', '滚动训练失败');
            hide('roll-progress');
            $('roll-start').disabled = false;
            showAlert('danger', '滚动训练失败: ' + (data.error || '未知错误'));
        }
    }, 1000);
}

async function viewRollingReport(recordDir) {
    const data = await api(`/api/rolling/report/${recordDir}`);
    if (!data.exists) { showAlert('warning', '报告不存在'); return; }

    // Markdown 转简单 HTML（与 viewRecord 一致）
    const mdHtml = data.content
        .replace(/^### (.+)$/gm, '<h4>$1</h4>')
        .replace(/^## (.+)$/gm, '<h3>$1</h3>')
        .replace(/^# (.+)$/gm, '<h2>$1</h2>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/^\- (.+)$/gm, '<li>$1</li>')
        .replace(/\n/g, '<br>');
    html('modal-body', `<div style="background:var(--light);padding:15px;border-radius:5px;font-size:13px;line-height:1.6">${mdHtml}</div>`);
    show('modal');
}

// ===== 预测 =====
let predictTaskId = null;
let predictPollTimer = null;

function doPredict() {
    const lottery = $('pred-lottery').value;
    const groups = parseInt($('pred-groups').value) || 5;
    const mode = $('pred-mode').value;

    hide('pred-result');
    html('pred-loading', '<div class="spinner"></div> 生成预测中...');

    api('/api/predict', {
        method: 'POST',
        body: JSON.stringify({ lottery, groups, mode })
    }).then(data => {
        predictTaskId = data.task_id;
        pollPredictStatus();
    });
}

function pollPredictStatus() {
    if (!predictTaskId) return;
    predictPollTimer = setInterval(async () => {
        const data = await api(`/api/predict/status/${predictTaskId}`);
        if (data.status === 'running') {
            html('pred-loading', `<div class="spinner"></div> 预测中... ${data.message || ''}`);
        } else if (data.status === 'done') {
            clearInterval(predictPollTimer);
            html('pred-loading', '');
            show('pred-result');
            const r = data.result;
            html('pred-info', `
              <div class="alert alert-info">
                预测日期: <strong>${r.date}</strong> | 模式: <strong>${r.mode}</strong> |
                组数: <strong>${r.groups}</strong> | 近50期命中率: <strong>${(r.hit_rate*100).toFixed(1)}%</strong>
              </div>
            `);

            let htmlStr = '<div style="display:flex;flex-wrap:wrap">';
            for (const ps of r.numbers) {
                const zones = Object.entries(ps.号码 || {}).map(([name, pred]) => ({
                    name, pred, cls: name === '红球' ? 'red' : name === '蓝球' ? 'blue' : null
                }));
                const balls = renderZoneBalls(zones, 'pred', { size: 24, showLabels: true });
                const note = ps.备注 ? `<div style="font-size:11px;color:var(--gray);margin-top:4px">${ps.备注}</div>` : '';
                htmlStr += `<div class="number-card">
                  <div>${balls}</div>
                  <div><span class="strategy-tag">${_strategyBadge(ps.策略)} | 置信度: ${ps.置信度.toFixed(4)}</span></div>
                  ${note}
                </div>`;
            }
            htmlStr += '</div>';
            html('pred-numbers', htmlStr);
        } else if (data.status === 'error') {
            clearInterval(predictPollTimer);
            html('pred-loading', '');
            showAlert('danger', '预测失败: ' + (data.error || '未知错误'));
        } else if (data.status === 'not_found') {
            clearInterval(predictPollTimer);
            html('pred-loading', '');
            showAlert('danger', '预测任务不存在');
        }
    }, 1000);
}

// ===== 异常检测 =====
let anomalyTaskId = null;
let anomalyPollTimer = null;
let _anoLastResult = null;   // 缓存最近一次 renderAnomalyResult 的 payload（供排序开关重渲染）
let _anoSortDir = 'desc';    // 号码显示方向：'desc' 默认降序 | 'asc'

function loadAnomaly() {
    const lottery = $('ano-lottery').value;
    const threshold = $('ano-threshold').value;

    // 修正： spinner 应写到真实存在的 #ano-summary
    html('ano-summary', '<div class="spinner"></div> 分析中...');
    html('ano-detail', '');
    html('ano-chart', '');

    api(`/api/anomaly/${lottery}?threshold=${threshold}`).then(data => {
        anomalyTaskId = data.task_id;
        pollAnomalyStatus();
    }).catch(err => {
        html('ano-summary', `<div class="alert alert-danger">启动检测失败: ${err.message || err}</div>`);
    });
}

function pollAnomalyStatus() {
    if (!anomalyTaskId) return;
    anomalyPollTimer = setInterval(async () => {
        const data = await api(`/api/anomaly/status/${anomalyTaskId}`);
        if (data.status === 'running') {
            html('ano-summary', `<div class="spinner"></div> 检测中... ${data.message || ''}`);
        } else if (data.status === 'done') {
            clearInterval(anomalyPollTimer);
            renderAnomalyResult(data.result);
        } else if (data.status === 'error') {
            clearInterval(anomalyPollTimer);
            html('ano-summary', `<div class="alert alert-danger">异常检测失败: ${data.error || '未知错误'}</div>`);
        } else if (data.status === 'not_found') {
            clearInterval(anomalyPollTimer);
            html('ano-summary', '<div class="alert alert-danger">检测任务不存在</div>');
        }
    }, 1000);
}

// 把可能被截断的期号补全为完整格式（如 26090 -> 2026090）
function formatIssue(issue, dateStr) {
    if (issue === undefined || issue === null || issue === '') return '-';
    let s = String(issue).trim();
    if (s.length >= 7) return s;
    let year = '';
    if (dateStr) {
        const m = String(dateStr).match(/(\d{4})/);
        if (m) year = m[1];
    }
    if (!year) {
        if (s.length === 5) return '20' + s;
        const padded = s.padStart(5, '0');
        return '20' + padded;
    }
    const suffix = s.slice(-3).padStart(3, '0');
    return year + suffix;
}
function formatWindowPeriod(wp, dateStr) {
    if (!wp) return '';
    return wp.split('~').map(s => formatIssue(s.trim(), dateStr)).join(' ~ ');
}

// 号码排序助手：按当前方向返回副本（不改动原 payload —— 重渲染复用同一缓存对象）
function _anoSortNums(arr) {
    const a = (arr || []).slice();
    a.sort((x, y) => _anoSortDir === 'asc' ? Number(x) - Number(y) : Number(y) - Number(x));
    return a;
}

// 排序开关：切换方向并重渲染（未检测过则静默忽略）
function _anoToggleSort(isDesc) {
    _anoSortDir = isDesc ? 'desc' : 'asc';
    if (_anoLastResult) renderAnomalyResult(_anoLastResult);
}

function renderAnomalyResult(data) {
    _anoLastResult = data;   // 缓存，供排序开关重渲染
    html('ano-summary', '');
    html('ano-chart', '');
    html('ano-detail', '');
    let detailHtml = '';
    // 频率图数据（两种模式都需要，提前计算避免重复声明）
    const freq = data.freq_data;
    const hasFreqChart = !!(freq && freq.zones && Object.keys(freq.zones).length > 0);
    if (data.comparison) {
        // 三档对比
        // 动态收集所有方法名（双色球/大乐透 与 数字型 方法命名不同，统一自适应）
        const methodNames = Array.from(new Set(
            Object.values(data.comparison).flatMap(c => Object.keys(c.各方法异常数 || {}))
        ));
        let htmlStr = `<div class="card"><h3>三档阈值对比</h3><div style="overflow-x:auto"><table><tr><th>阈值</th><th>z阈值</th><th>总条数</th>`;
        for (const m of methodNames) htmlStr += `<th>${m}</th>`;
        htmlStr += `</tr>`;
        for (const [t, c] of Object.entries(data.comparison)) {
            htmlStr += `<tr><td>${t}</td><td>${c.阈值.z_score}</td><td>${c.异常总条数}</td>`;
            for (const m of methodNames) htmlStr += `<td>${c.各方法异常数[m]||0}</td>`;
            htmlStr += `</tr>`;
        }
        htmlStr += `</table></div></div>`;
        html('ano-summary', htmlStr);
        // 对比模式也展示号码频率图
        if (hasFreqChart) {
            html('ano-chart', _anoFreqChartCardHTML(freq, data.generated_at));
            requestAnimationFrame(() => requestAnimationFrame(() => drawAnoFreqChart(freq)));
        }
        return;
    }

    let htmlStr = `
    <div class="card">
      <h3>检测结果</h3>
      <p>总期数: ${data.total_records || data.总期数 || '-'} | 异常总条数: ${data.total_anomalies || data.异常总条数 || 0}</p>`;

    const details = data.details || data.异常明细 || [];
    // 等级分布统计 + 结论
    if (details.length > 0) {
        const levels = {};
        for (const d of details) levels[d.等级] = (levels[d.等级]||0) + 1;
        const severe = levels['严重']||0, suspect = levels['可疑']||0, ref = levels['参考']||0;
        let verdict;
        if (severe === 0 && suspect === 0) {
            verdict = '<span style="color:#2e7d32">近期号码分布正常，未发现明显异常</span>';
        } else if (severe === 0) {
            verdict = `<span style="color:#b8860b">发现 ${suspect} 处可疑偏离，建议关注但不需担心</span>`;
        } else if (severe <= 3) {
            verdict = `<span style="color:#c62828">发现 ${severe} 处严重偏离（历史异常期），${ref} 条参考信息</span>`;
        } else {
            verdict = `<span style="color:#c62828">发现 ${severe} 处严重偏离——多为历史某时段号码偏热所致，是真实统计信号而非误报</span>`;
        }
        htmlStr += `<div style="padding:10px 14px;border-radius:8px;background:var(--color-background-info,#eef4fb);margin:10px 0;font-size:13px">
          <strong>结论：</strong>${verdict}<br>
          <span style="color:var(--gray)">等级分布：严重 ${severe} · 可疑 ${suspect} · 参考 ${ref}（共 ${details.length} 条，完整明细见 CSV）</span>
        </div>`;
    }

    const byMethod = data.by_method || data.各方法异常数 || {};
    if (Object.keys(byMethod).length > 0) {
        htmlStr += `<table><tr><th>检测方法</th><th>异常数</th></tr>`;
        for (const [method, count] of Object.entries(byMethod)) {
            htmlStr += `<tr><td>${method}</td><td>${count}</td></tr>`;
        }
        htmlStr += `</table>`;
    }
    htmlStr += `</div>`;
    html('ano-summary', htmlStr);

    // ===== 异常明细（BBS 上半部分：期号列表）=====
    if (details.length > 0) {
        // 号码球渲染（红球红底/蓝球蓝底）
        function ball(n, isBlue) {
            const bg = isBlue ? '#1E90FF' : '#E24B4A';
            return `<span style="display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:50%;background:${bg};color:#fff;font-size:11px;font-weight:700;margin:1px">${String(n).padStart(2,'0')}</span>`;
        }
        // 生成每条异常的"人话解读"
        function interpret(d) {
            try {
                if (d.类型.includes('卡方')) {
                    const dev = (d.最大偏差比例 * 100).toFixed(0);
                    const rel = d.相关号码;
                    let ballStr = '';
                    if (rel && rel.最热) {
                        ballStr = `<br><span style="color:var(--gray)">最热：</span>${rel.最热.map(n=>ball(n)).join('')} <span style="color:var(--gray)">最冷：</span>${rel.最冷.map(n=>ball(n)).join('')}`;
                    }
                    return `窗口内号码分布不均匀，最偏号码超出均匀期望约 <strong>${dev}%</strong>${ballStr}`;
                }
                if (d.类型.includes('区间')) {
                    const dist = d.区间分布 || {};
                    const entries = Object.entries(dist).sort((a,b) => b[1]-a[1]);
                    if (entries.length >= 2) {
                        const [hi, hiCnt] = entries[0], [lo, loCnt] = entries[entries.length-1];
                        const total = Object.values(dist).reduce((s,v)=>s+v,0);
                        const exp = total / entries.length;
                        let hotBall = '';
                        const rel = d.相关号码;
                        if (rel && rel[hi] && rel[hi].号码) hotBall = `，区间内最热：${ball(rel[hi].号码)}`;
                        return `${hi}最热（${hiCnt}次，期望${exp.toFixed(0)}次，约${(hiCnt/exp*100).toFixed(0)}%）${hotBall}；${lo}最冷（${loCnt}次）`;
                    }
                    return '区间分布有偏离';
                }
                if (d.类型.includes('冷热号')) {
                    const num = d.号码, trend = d.趋势, gf = (d.全局频率*100).toFixed(1), rf = (d.近期频率*100).toFixed(1);
                    const isBlue = d.类型.includes('蓝球');
                    return `${ball(num, isBlue)} 号码 <strong>${num}</strong> 近期${trend === '显著升温' ? '升温' : '降温'}（近30期频率 ${rf}% vs 历史 ${gf}%）`;
                }
                if (d.类型.includes('Benford')) {
                    const dist = d.首位数分布 || {};
                    const top = Object.entries(dist).sort((a,b)=>b[1]-a[1]).slice(0,3).map(([k,v])=>`${k}开头${(v*100).toFixed(1)}%`).join('、');
                    return `首位数分布：${top}（彩票号码范围受限，本检验仅作参考）`;
                }
                if (d.类型.includes('连号')) {
                    if (d.连号期数 !== undefined) {
                        // 时间窗口聚集
                        const issues = (d.涉及期号 || []).map(x => String(x)).slice(0, 12).join('、');
                        const more = (d.涉及期号 || []).length > 12 ? `…（共${d.涉及期号.length}期）` : '';
                        return `时间窗口 <strong>${formatWindowPeriod(d.窗口期)}</strong> 内，有 <strong>${d.连号期数}</strong> 期开出连号（历史期望约 ${d.期望期数} 期，占比 ${(d.占比*100).toFixed(0)}%），属于连号聚集期。涉及期号：${issues}${more}`;
                    }
                    // 单期极端连号
                    const longest = _anoSortNums(d.连号号码).map(n => ball(n)).join('');
                    const sorted = _anoSortNums(d.排序红球).map(n => ball(n)).join('');
                    return `本期最长 <strong>${d.最长连号}</strong> 连号（${longest}），整组排序：${sorted}（历史罕见，z=${d.z_score}）`;
                }
                if (d.类型.includes('形态')) {
                    const sorted = _anoSortNums(d.排序红球).map(n => ball(n)).join('');
                    return `号码形态<strong>${d.形态}</strong>：跨度 ${d.跨度}（最小间距 ${d.最小间距} / 最大间距 ${d.最大间距}）；排序后：${sorted}（z=${d.z_score}）`;
                }
                if (d.类型.includes('周期')) {
                    if (d.显著周期) {
                        return `${d.说明 || ''}（等级仅参考，彩票本身无真实周期）`;
                    }
                    if (d.周期号码 && d.周期号码.length) {
                        const ps = d.周期号码.map(p => `${ball(p.号码)} 约每 ${p.间隔} 期出现（共 ${p.出现次数} 次，${p.首期}~${p.末期}）`).join('；');
                        return `${d.说明 || ''}：${ps}`;
                    }
                    return d.说明 || d.类型;
                }
                return d.类型;
            } catch (e) {
                return d.类型 || '未知';
            }
        }

        detailHtml = `<div class="card" style="margin-top:15px">
            <h3>异常明细（全部 ${details.length} 条）</h3>
            <div style="max-height:420px;overflow-y:auto;border:1px solid var(--color-border-tertiary,#eee);border-radius:6px">
            <table style="font-size:13px"><tr><th>期号</th><th>检测方法</th><th>等级</th><th>具体情况</th></tr>`;
        const levelColor = { '严重': '#c62828', '可疑': '#b8860b', '参考': '#5f5e5a', '正常': '#2e7d32' };
        const levelDesc = { '严重': '实质偏离（真异常）', '可疑': '统计显著但幅度中等', '参考': '仅展示' };
        for (const d of details) {
            const lc = levelColor[d.等级] || '#888';
            const wf = d.窗口号码频次;
            const hasFull = wf && wf.length > 0;
            const expFreq = (d.相关号码 && d.相关号码.期望频次) || 0;
            const fullBtn = hasFull
                ? `<br><a href="javascript:void(0)" onclick="toggleAnoDetail(${d.期号})" style="font-size:11px;color:var(--primary)">查看窗口全部号码 ▼</a>`
                : '';
            detailHtml += `<tr>
                <td>${formatIssue(d.期号, d.日期)}<br><span style="font-size:11px;color:var(--gray)">${d.日期||''}</span>${fullBtn}</td>
                <td>${d.类型}</td>
                <td><span style="color:${lc};white-space:nowrap">${d.等级}</span><br><span style="font-size:11px;color:var(--gray)">${levelDesc[d.等级]||''}</span></td>
                <td style="max-width:420px">${interpret(d)}</td>
            </tr>`;
            if (hasFull) {
                detailHtml += `<tr id="ano-detail-${d.期号}" class="hidden"><td colspan="4" style="background:var(--color-background-info,#f7f9fc)">
                    <div style="font-size:12px;color:var(--gray);margin-bottom:4px">窗口期 ${formatWindowPeriod(d.窗口期, d.日期)} 内全部号码出现频次（红=超期望40%+ · 灰=低于期望40%）</div>
                    <div>${renderFullNumberDist(wf, expFreq)}</div>
                </td></tr>`;
            }
        }
        detailHtml += `</table></div>`;
        detailHtml += `<p style="font-size:12px;color:var(--gray);margin-top:8px">等级含义：<strong style="color:#c62828">严重</strong>=号码分布实质偏离 &nbsp; <strong style="color:#b8860b">可疑</strong>=统计显著但幅度不大 &nbsp; <strong style="color:#5f5e5a">参考</strong>=方法不适用仅展示</p>`;
        detailHtml += `</div>`;
    }

    html('ano-detail', detailHtml);

    // ===== 交互式号码频率图（下半部分：数据图表）=====
    if (hasFreqChart) {
        html('ano-chart', _anoFreqChartCardHTML(freq, data.generated_at));
    }

    // 图表绘制独立进行，失败不影响已渲染内容
    if (hasFreqChart) {
        try {
            requestAnimationFrame(() => requestAnimationFrame(() => drawAnoFreqChart(freq)));
        } catch (e) {
            console.error('图表绘制失败（不影响明细显示）:', e);
        }
    }
}

// 号码频率图卡片 HTML：含分区切换（红/蓝球或数字型各分区）、HTML tooltip 容器
function _anoFreqChartCardHTML(freq, generatedAt) {
    const zoneNames = (freq && freq.zones) ? Object.keys(freq.zones) : [];
    if (!_anoChartZone || !zoneNames.includes(_anoChartZone)) _anoChartZone = zoneNames[0] || null;
    let tabs = '';
    for (const z of zoneNames) {
        const active = (z === _anoChartZone) ? 'btn-primary' : 'btn-info';
        tabs += `<button class="btn btn-sm ${active}" onclick="_anoSwitchChartZone('${z}')" style="padding:5px 14px">${z}</button>`;
    }
    return `<div class="card" style="margin-top:15px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:10px">
            <h3 style="margin:0">${ico('board')} 号码出现频率分布
                <span style="font-size:12px;color:var(--gray);font-weight:normal">（红色虚线 = 理论均匀频率 · 数据更新于 ${generatedAt || '未知'}）</span>
            </h3>
            <div style="display:flex;gap:6px">${tabs}</div>
        </div>
        <div style="position:relative">
            <canvas id="ano-freq-chart" width="900" height="260" style="width:100%;height:260px;display:block"></canvas>
            <div id="ano-freq-tooltip" style="position:absolute;display:none;background:rgba(255,255,255,0.98);border:1px solid #ddd;border-radius:6px;padding:10px 12px;box-shadow:0 4px 12px rgba(0,0,0,0.12);font-size:12px;pointer-events:none;z-index:10;min-width:160px;line-height:1.5"></div>
        </div>
        <p style="font-size:12px;color:var(--gray);margin-top:10px">
            悬停查看每个号码的实际出现次数与理论次数；红色柱为超过理论频率 40% 以上的“过热”号码。
        </p>
    </div>`;
}

let _anoChartZone = null; // 当前展示的区名（红球/蓝球/万位...）
function _anoSwitchChartZone(zone) {
    _anoChartZone = zone;
    if (_anoLastFreq) drawAnoFreqChart(_anoLastFreq);
}

// 展开/收起异常明细的全部号码分布
function toggleAnoDetail(drawNo) {
    const row = document.getElementById('ano-detail-' + drawNo);
    if (row) row.classList.toggle('hidden');
}

// 渲染窗口内全部号码频次（红=过热/灰=过冷/蓝=正常）
function renderFullNumberDist(windowFreq, expected) {
    if (!windowFreq || windowFreq.length === 0) return '无数据';
    const exp = expected || 1;
    let html = '';
    // 号码按当前排序方向显示（对象数组，独立排序避免 号码 重复时歧义）
    const _sortedWf = windowFreq.slice().sort((a, b) =>
        _anoSortDir === 'asc' ? Number(a.号码) - Number(b.号码) : Number(b.号码) - Number(a.号码));
    for (const d of _sortedWf) {
        const ratio = d.频次 / exp;
        let bg = '#185FA5'; // 正常
        if (ratio > 1.4) bg = '#E24B4A';      // 过热
        else if (ratio < 0.6) bg = '#9E9E9E'; // 过冷
        html += `<span title="号码${d.号码}：${d.频次}次（期望${exp.toFixed(1)}，${(ratio*100).toFixed(0)}%）"
            style="display:inline-block;width:26px;height:26px;line-height:26px;text-align:center;border-radius:50%;background:${bg};color:#fff;font-size:11px;margin:2px;cursor:default">${String(d.号码).padStart(2,'0')}</span>`;
    }
    return html;
}

// 交互式号码频率柱状图（Canvas + hover，DPR 高清渲染）
function drawAnoFreqChart(freq) {
    const canvas = $('ano-freq-chart');
    const tooltip = $('ano-freq-tooltip');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    // 高清渲染：按显示宽度 × devicePixelRatio 设置位图
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(Math.round(canvas.getBoundingClientRect().width) || 900, 320);
    const cssH = 260;
    canvas.style.height = cssH + 'px';
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const W = cssW, H = cssH;
    const padL = 50, padR = 18, padT = 18, padB = 32;

    const zoneNames = Object.keys(freq.zones || {});
    const zone = (_anoChartZone && zoneNames.includes(_anoChartZone)) ? _anoChartZone : (zoneNames[0] || null);
    if (!zone) {
        ctx.fillStyle = '#888'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center';
        ctx.fillText('暂无频率数据', W / 2, H / 2);
        return;
    }
    const series = freq.zones[zone] || [];
    const theoryRate = (freq.理论频率 && freq.理论频率[zone]) || 0;
    const perIssue = (freq.每区选取数 && freq.每区选取数[zone]) || 1;
    const totalIssues = freq.总期数 || 0;
    const theory = theoryRate * totalIssues * perIssue;
    const maxCnt = Math.max(...series.map(d => d.次数), theory) * 1.12;

    _anoLastFreq = freq;
    if (!_anoChartZone || !zoneNames.includes(_anoChartZone)) _anoChartZone = zone;

    function render(hoverIdx) {
        ctx.clearRect(0, 0, W, H);
        const n = series.length;
        if (n === 0) {
            ctx.fillStyle = '#888'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center';
            ctx.fillText('暂无频率数据', W / 2, H / 2);
            return;
        }
        const bw = (W - padL - padR) / n;

        // 网格线 + Y 轴标签
        ctx.font = '11px sans-serif';
        ctx.fillStyle = '#888';
        ctx.textAlign = 'right';
        for (let g = 0; g <= 4; g++) {
            const gy = padT + (H - padT - padB) * g / 4;
            const val = maxCnt * (1 - g / 4);
            ctx.strokeStyle = '#eee'; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
            ctx.fillText(Math.round(val), padL - 6, gy + 4);
        }

        // 理论线
        if (theory > 0) {
            const ty = padT + (H - padT - padB) * (1 - theory / maxCnt);
            ctx.strokeStyle = '#E24B4A'; ctx.setLineDash([5, 4]); ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.moveTo(padL, ty); ctx.lineTo(W - padR, ty); ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = '#A32D2D'; ctx.font = '11px sans-serif'; ctx.textAlign = 'right';
            ctx.fillText('理论 ' + Math.round(theory), W - padR - 6, ty - 6);
        }

        // 柱子
        series.forEach((d, i) => {
            const x = padL + i * bw;
            const h = (H - padT - padB) * (d.次数 / maxCnt);
            const y = H - padB - h;
            const isHot = d.次数 > theory * 1.4;
            const isHover = i === hoverIdx;
            ctx.fillStyle = isHot ? (isHover ? '#c62828' : '#E24B4A') : (isHover ? '#2a7ab8' : '#185FA5');
            ctx.fillRect(x + 1, y, bw - 2, h);
            // 号码标签（最多显示约 30 个）
            const step = Math.max(1, Math.ceil(n / 30));
            if (i % step === 0) {
                ctx.fillStyle = '#888'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
                ctx.fillText(d.号码, x + bw / 2, H - 10);
            }
        });

        // hover 高亮框
        if (hoverIdx >= 0 && series[hoverIdx]) {
            const d = series[hoverIdx];
            const x = padL + hoverIdx * bw;
            ctx.strokeStyle = 'rgba(0,0,0,0.5)'; ctx.lineWidth = 2;
            ctx.strokeRect(x + 1, H - padB - (H - padT - padB) * (d.次数 / maxCnt), bw - 2, (H - padT - padB) * (d.次数 / maxCnt));
        }
    }

    // 鼠标悬停：HTML tooltip + 重绘高亮
    canvas.onmousemove = (e) => {
        const rect = canvas.getBoundingClientRect();
        const sx = (e.clientX - rect.left) * (W / rect.width);
        const n = series.length;
        const bw = n ? (W - padL - padR) / n : 0;
        const idx = Math.floor((sx - padL) / bw);
        if (idx < 0 || idx >= n) {
            render(-1);
            if (tooltip) tooltip.style.display = 'none';
            return;
        }
        render(idx);
        const d = series[idx];
        const dev = theory ? ((d.次数 - theory) / theory * 100) : 0;
        const devText = dev > 0 ? `+${dev.toFixed(1)}%` : `${dev.toFixed(1)}%`;
        const devColor = d.次数 > theory * 1.4 ? '#c62828' : (d.次数 < theory * 0.6 ? '#9E9E9E' : '#185FA5');
        if (tooltip) {
            tooltip.innerHTML = `<div style="font-weight:700;margin-bottom:4px">${zone} ${String(d.号码).padStart(2, '0')}</div>
                <div>实际出现：<strong>${d.次数}</strong> 次</div>
                <div>理论期望：约 <strong>${Math.round(theory)}</strong> 次</div>
                <div style="color:${devColor};margin-top:4px">偏离：${devText}</div>`;
            tooltip.style.display = 'block';
            const wrapRect = canvas.parentElement.getBoundingClientRect();
            let left = e.clientX - wrapRect.left + 14;
            let top = e.clientY - wrapRect.top + 14;
            if (left + 180 > wrapRect.width) left = e.clientX - wrapRect.left - 190;
            if (top + 100 > wrapRect.height) top = e.clientY - wrapRect.top - 110;
            tooltip.style.left = left + 'px';
            tooltip.style.top = top + 'px';
        }
    };
    canvas.onmouseleave = () => {
        render(-1);
        if (tooltip) tooltip.style.display = 'none';
    };

    render(-1);

    // 窗口缩放后重绘
    if (!_anoResizeBound) {
        _anoResizeBound = true;
        let rt = null;
        window.addEventListener('resize', () => {
            clearTimeout(rt);
            rt = setTimeout(() => { if (_anoLastFreq) drawAnoFreqChart(_anoLastFreq); }, 200);
        });
    }
}

let _anoLastFreq = null;
let _anoResizeBound = false;

// ===== 历史记录 =====
async function loadHistory(days) {
    html('history-result', '<div class="spinner"></div> 加载中...');
    const data = await api(`/api/history?days=${days}`);

    if (data.length === 0) {
        html('history-result', '<div class="alert alert-info">暂无记录</div>');
        return;
    }

    let htmlStr = `<table><tr><th>类型</th><th>名称</th><th>日期</th><th>大小</th><th>图表</th><th>操作</th></tr>`;
    for (const r of data) {
        const tagClass = `tag-${r.type.toLowerCase()}`;
        htmlStr += `<tr>
          <td><span class="tag ${tagClass}">${r.type}</span></td>
          <td>${r.name}</td>
          <td>${r.date}</td>
          <td>${r.size_kb} KB</td>
          <td>${r.charts}张</td>
          <td><button class="btn btn-sm btn-info" onclick="viewRecord('${r.name}')">查看</button></td>
        </tr>`;
    }
    htmlStr += `</table>`;
    html('history-result', htmlStr);
}

async function viewRecord(name) {
    const data = await api(`/api/history/view/${name}`);
    if (data.error) { showAlert('danger', data.error); return; }

    let htmlStr = `<div class="card"><h3>${name}</h3>`;

    if (data.charts.length > 0) {
        for (const chart of data.charts) {
            htmlStr += `<div class="chart-container"><img src="/training/${chart}" alt="chart" style="max-width:80%"></div>`;
        }
    }

    if (data.report) {
        // Markdown 转简单 HTML
        const mdHtml = data.report
            .replace(/^### (.+)$/gm, '<h4>$1</h4>')
            .replace(/^## (.+)$/gm, '<h3>$1</h3>')
            .replace(/^# (.+)$/gm, '<h2>$1</h2>')
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/^- (.+)$/gm, '<li>$1</li>')
            .replace(/\n/g, '<br>');
        htmlStr += `<div style="background:var(--light);padding:15px;border-radius:5px;font-size:13px;line-height:1.6">${mdHtml}</div>`;
    }

    htmlStr += `</div>`;

    // 弹出模态框
    const modal = $('modal');
    html('modal-body', htmlStr);
    show(modal);
}

function closeModal() {
    hide('modal');
}

// ===== 命中历史（/hits 页面） =====

// 「目前已有多少中奖」常驻数字：分母 = 全部命中注数（不加任何筛选，全彩种）
function _renderHitsStat(universe) {
    const el = $('hits-stat');
    if (!el) return;
    el.innerHTML = `目前已中奖 <strong style="color:#c62828;font-size:15px">${universe}</strong> 注` +
        ` <span style="opacity:.72">（全部彩种 · 全部奖级，含「开奖后回测·训练」）</span>`;
}

// 工具条内的常驻数字：筛出 xx 注 / 共 xxxx 注（总数）
// 未加筛选时两者相等；一旦有筛选，筛选态高亮，避免"以为总数变了"。
function _renderHitsCount(matched, universe) {
    const box = $('hits-count');
    const m = $('hits-count-match');
    const t = $('hits-count-total');
    if (!box || !m || !t) return;
    m.textContent = matched;
    t.textContent = universe;
    const filtered = matched !== universe;
    box.style.background = filtered ? '#fdecec' : '#fdf5f5';
    box.style.borderColor = filtered ? '#e3a9a9' : '#f0d0d0';
    box.title = filtered
        ? `已筛选：筛出 ${matched} 注 / 共 ${universe} 注（总数）`
        : `未筛选：当前共 ${universe} 注中奖记录`;
}

function _onHitsLotteryChange() {
    const lottery = $('hits-lottery') ? $('hits-lottery').value : '全部';
    const isRedBlue = lottery === '双色球' || lottery === '大乐透';
    const redBox = $('hits-red');
    const blueBox = $('hits-blue');
    const totalBox = $('hits-total');
    const sortBox = $('hits-sort');
    if (redBox) redBox.style.display = isRedBlue ? '' : 'none';
    if (blueBox) blueBox.style.display = isRedBlue ? '' : 'none';
    if (totalBox) totalBox.style.display = isRedBlue ? 'none' : '';
    // 当切换彩种时，排序选项中红/蓝命中对数字型无意义，但保留也无害；
    // 为更友好，当选择数字型时把排序切换到 total_hits 的默认逻辑不强制，
    // 仅调整可选项文字已足够，这里只保证筛选框可见性。
}

// ===== 命中历史的时间筛选：日历选择器（点起点→点终点）+ 快捷预设 =====

function _isoLocal(d) {
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// 日历内部状态：view=当前展示的月份(1号)，from/to=已选区间，picking=正在选起点
const _cal = { view: null, from: '', to: '', picking: false };

function _hitsTimeBounds() {
    // 区间落在两个隐藏 input 里（hits.html），URL 参数恢复也写回这里
    const b = {};
    const f = $('hits-date-from'), t = $('hits-date-to');
    if (f && f.value) b.from = f.value;
    if (t && t.value) b.to = t.value;
    return b;
}

function _calSyncLabel() {
    const b = _hitsTimeBounds();
    const el = $('hits-time-label');
    const btn = $('hits-time-btn');
    if (!el) return;
    if (!b.from && !b.to) {
        el.textContent = '全部时间';
        if (btn) btn.style.borderColor = '#ddd';
        return;
    }
    if (btn) btn.style.borderColor = '#c62828';
    el.textContent = b.from === b.to ? b.from
        : (b.from && b.to ? `${b.from} ~ ${b.to}` : (b.from ? `${b.from} 起` : `至 ${b.to}`));
}

function _toggleCalendar(e) {
    if (e) e.stopPropagation();
    const box = $('hits-calendar');
    if (!box) return;
    if (box.style.display === 'none') {
        const now = new Date();
        if (!_cal.view) {
            // 有已选区间就定位到区间末月，否则当前月
            const b = _hitsTimeBounds();
            const base = b.to || b.from;
            _cal.view = base ? (() => { const d = new Date(base + 'T00:00:00'); return new Date(d.getFullYear(), d.getMonth(), 1); })()
                             : new Date(now.getFullYear(), now.getMonth(), 1);
        }
        box.innerHTML = _renderCalendar();
        box.style.display = 'block';
        if (!_cal._outside) {
            _cal._outside = true;
            document.addEventListener('click', ev => {
                const wrap = $('hits-time-wrap');
                if (wrap && !wrap.contains(ev.target)) _closeCalendar();
            });
        }
    } else {
        _closeCalendar();
    }
}

function _closeCalendar() {
    const box = $('hits-calendar');
    if (box) box.style.display = 'none';
}

function _calNav(delta) {
    _cal.view = new Date(_cal.view.getFullYear(), _cal.view.getMonth() + delta, 1);
    const box = $('hits-calendar');
    if (box) box.innerHTML = _renderCalendar();
}

function _renderCalendar() {
    const v = _cal.view, y = v.getFullYear(), m = v.getMonth();
    const presets = [['all', '全部'], ['this-month', '本月'], ['last-month', '上月'], ['7d', '近 7 天'], ['30d', '近 30 天']];
    const presetBtns = presets.map(([k, label]) =>
        `<button type="button" onclick="_calPreset('${k}')" style="padding:3px 8px;border:1px solid #e0e0e0;border-radius:12px;background:#fff;font-size:12px;cursor:pointer;color:#455a64">${label}</button>`).join('');

    // 周一起始的日历网格
    const firstDow = (new Date(y, m, 1).getDay() + 6) % 7;
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    const b = _hitsTimeBounds();
    // 正在选起点时（tentative），网格里优先高亮还没落盘的新起点
    const tentative = _cal.from && !_cal.to;
    const aFrom = tentative ? _cal.from : b.from;
    const aTo = tentative ? '' : b.to;
    const todayIso = _isoLocal(new Date());
    let cells = ['一', '二', '三', '四', '五', '六', '日'].map(w =>
        `<span style="text-align:center;font-size:11px;color:#9e9e9e;padding:2px 0">${w}</span>`).join('');
    for (let i = 0; i < firstDow; i++) cells += '<span></span>';
    for (let d = 1; d <= daysInMonth; d++) {
        const iso = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        const isEdge = iso === aFrom || iso === aTo;
        const inRange = aFrom && aTo && iso > aFrom && iso < aTo;
        const isToday = iso === todayIso;
        let style = 'text-align:center;padding:4px 0;font-size:12px;border-radius:6px;cursor:pointer;border:1px solid transparent;';
        if (isEdge) style += 'background:#c62828;color:#fff;font-weight:700;';
        else if (inRange) style += 'background:#fdecec;color:#b71c1c;';
        else style += 'color:#37474f;';
        if (isEdge && isToday) style += 'border-color:#ffcdd2;';
        else if (!isEdge && isToday) style += 'border-color:#e0b4b4;';
        cells += `<span style="${style}" onclick="_calPick('${iso}')" onmouseover="this.style.outline='1px solid #e57373'" onmouseout="this.style.outline='none'">${d}</span>`;
    }

    const hint = (_cal.from && !_cal.to)
        ? `<div style="font-size:11px;color:#c62828;margin:2px 2px 6px">起点 ${_cal.from} 已选，点击结束日期完成（再点同一天=只看当天，点更早日期=重新开始）</div>`
        : '<div style="font-size:11px;color:#9e9e9e;margin:2px 2px 6px">点第一个日期定起点，再点第二个日期定终点</div>';
    const clearBtn = (b.from || b.to)
        ? `<button type="button" onclick="_calPreset('all')" style="padding:3px 10px;border:none;background:none;color:#c62828;font-size:12px;cursor:pointer">清除筛选</button>` : '';

    return `<div>
        <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">${presetBtns}</div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <button type="button" onclick="_calNav(-1)" style="border:none;background:none;font-size:15px;cursor:pointer;color:#607d8b;padding:2px 8px" title="上个月">‹</button>
            <strong style="font-size:13px;color:#37474f">${y} 年 ${m + 1} 月</strong>
            <button type="button" onclick="_calNav(1)" style="border:none;background:none;font-size:15px;cursor:pointer;color:#607d8b;padding:2px 8px" title="下个月">›</button>
        </div>
        ${hint}
        <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:1px">${cells}</div>
        <div style="display:flex;justify-content:flex-end;margin-top:6px;min-height:20px">${clearBtn}</div>
    </div>`;
}

function _calPick(iso) {
    if (!_cal.from || (_cal.from && _cal.to)) {
        _cal.from = iso; _cal.to = '';
    } else if (iso < _cal.from) {
        _cal.from = iso;                     // 点到更早的日期 → 重新从这开始
    } else {
        _cal.to = iso;
        // 区间完整：写回隐藏 input、刷新列表、收起日历
        $('hits-date-from').value = _cal.from;
        $('hits-date-to').value = _cal.to;
        _calSyncLabel();
        _closeCalendar();
        _cal.from = ''; _cal.to = '';
        loadHits();
        return;
    }
    const box = $('hits-calendar');
    if (box) box.innerHTML = _renderCalendar();
}

function _calPreset(kind) {
    const now = new Date();
    let from = '', to = '';
    if (kind === 'this-month') { from = _isoLocal(new Date(now.getFullYear(), now.getMonth(), 1)); to = _isoLocal(new Date(now.getFullYear(), now.getMonth() + 1, 0)); }
    else if (kind === 'last-month') { from = _isoLocal(new Date(now.getFullYear(), now.getMonth() - 1, 1)); to = _isoLocal(new Date(now.getFullYear(), now.getMonth(), 0)); }
    else if (kind === '7d' || kind === '30d') {
        const days = kind === '7d' ? 7 : 30;
        const s = new Date(now); s.setDate(s.getDate() - (days - 1));
        from = _isoLocal(s); to = _isoLocal(now);
    }
    $('hits-date-from').value = from;
    $('hits-date-to').value = to;
    _cal.from = ''; _cal.to = '';
    if (kind !== 'all') {
        // 日历跳到区间末月，方便微调
        const base = to || from;
        const d = new Date(base + 'T00:00:00');
        _cal.view = new Date(d.getFullYear(), d.getMonth(), 1);
    }
    _calSyncLabel();
    _closeCalendar();
    loadHits();
}

// 「第 X 批 · 第 Y 注 · 原始 N 注/G 组」的悬停详情：把批次/注号口径一次说清
function _batchTitle(r) {
    const origPart = (r.原始注数 && r.原始注数 !== r.批次规模)
        ? `，质量压缩前原始出号 ${r.原始注数} 注/${r.原始组合数} 组`
        : '';
    return [
        `第 ${r.期号内批号} 批（该期共 ${r.期号内批数} 批）`,
        `第 ${r.批次内序号} 注（该批登记 ${r.批次规模} 注${origPart}）`,
        `出号时间 ${r.批次时间 || '—'}`,
        r.批次内序号推算 ? '注号按组内位置推算' : '',
        r.批次来源 === 'derived' ? '批次按评估时间推算' : '',
        '点击查看该批出的全部号码',
    ].filter(Boolean).join(' · ').replace(/"/g, '&quot;');
}

// ===== 批次详情弹窗：这一批是什么时候出的、整批都出了哪些号 =====

async function _showBatchDetail(idx) {
    const r = (window.__hitsRecords || [])[idx];
    if (!r || !r.批次号) return;
    html('modal-body', '<div class="spinner"></div> 加载批次详情...');
    show('modal');
    try {
        const url = `/api/hits/batch-detail?lottery=${encodeURIComponent(r.lottery)}&batch_id=${encodeURIComponent(r.批次号)}`;
        const d = await api(url);
        html('modal-body', _renderBatchDetail(d));
    } catch (e) {
        html('modal-body', `<div class="alert alert-danger">加载批次详情失败：${e.message || e}</div>`);
    }
}

// 单注号码 → 通用 zones 结构，直接复用命中页的 renderZoneBalls 渲染（同一套球面设计）
function _ticketZones(nums) {
    const keys = Object.keys(nums || {});
    if (keys.includes('红球') || keys.includes('蓝球')) {
        return [
            { name: '红球', pred: nums['红球'] || [], cls: 'red' },
            { name: '蓝球', pred: nums['蓝球'] || [], cls: 'blue' },
        ];
    }
    // 数字型：分区名→该区选的数字（键顺序即出号时的分区顺序）
    return keys.map(k => ({ name: k, pred: Array.isArray(nums[k]) ? nums[k] : [nums[k]] }));
}

function _renderBatchDetail(d) {
    const winStyle = {
        '一等': 'background:linear-gradient(90deg,#ffd700,#ffed4a);color:#7a5c00;font-weight:bold',
        '二等': 'background:linear-gradient(90deg,#ffd699,#ffe0b2);color:#b26a00;font-weight:bold',
        '三等': 'background:#e3f2fd;color:#1565c0;font-weight:bold',
        '四等': 'background:#e8f5e9;color:#2e7d32',
        '五等': 'background:#f3e5f5;color:#6a1b9a',
        '六等': 'background:#fff3e0;color:#e65100',
        '七等': 'background:#e0f7fa;color:#00838f',
        '八等': 'background:#efebe9;color:#4e342e',
        '九等': 'background:#f5f5f5;color:#616161',
        '一等奖': 'background:linear-gradient(90deg,#ffd700,#ffed4a);color:#7a5c00;font-weight:bold',
        '二等奖': 'background:linear-gradient(90deg,#ffd699,#ffe0b2);color:#b26a00;font-weight:bold',
        '三等奖': 'background:#e3f2fd;color:#1565c0;font-weight:bold',
        '四等奖': 'background:#e8f5e9;color:#2e7d32',
        '五等奖': 'background:#f3e5f5;color:#6a1b9a',
        '六等奖': 'background:#fff3e0;color:#e65100',
        '直选': 'background:linear-gradient(90deg,#ffd700,#ffed4a);color:#7a5c00;font-weight:bold',
        '组选3': 'background:#e3f2fd;color:#1565c0;font-weight:bold',
        '组选6': 'background:#e8f5e9;color:#2e7d32',
    };
    const lvText = lv => /^[一二三四五六七八九]等$/.test(lv) ? (lv + '奖') : lv;
    const genTip = d.出号时间精确
        ? ''
        : ' <span style="font-size:12px;color:#e65100">（历史记录未落盘具体时刻，按预测日期推算）</span>';
    const srcTip = d.批次来源 === 'explicit'
        ? '<span style="font-size:12px;color:#2e7d32;background:#e8f5e9;border-radius:10px;padding:1px 8px">显式批次号</span>'
        : '<span style="font-size:12px;color:#e65100;background:#fff3e0;border-radius:10px;padding:1px 8px" title="该批无批次号，按评估时间间隔推算归组">批次按评估时间推算</span>';

    // 整批号码：复用命中页同款球面；**每 5 注一组**，组与组之间用标题条 + 细分隔线区分
    const tickets = d.号码 || [];
    let rows = '';
    for (let i = 0; i < tickets.length; i++) {
        const t = tickets[i];
        if (i % 5 === 0) {
            const end = Math.min(i + 5, tickets.length);
            rows += `<div style="background:#f6f8fa;border-top:1px solid #e8edf2;padding:3px 12px;font-size:11px;color:var(--gray);font-weight:600">第 ${i + 1}–${end} 注</div>`;
        }
        const lv = t.中奖等级 || '未中';
        const isWin = lv !== '未中';
        const ps = winStyle[lv] || '';
        rows += `<div style="display:flex;align-items:center;gap:10px;padding:6px 12px;border-bottom:1px solid #f4f6f8;${isWin ? 'background:#fffdf5' : ''}">
            <span style="min-width:40px;text-align:right;color:var(--gray);font-size:12px;font-weight:600">第${t.序号}注</span>
            <span style="flex:1">${renderZoneBalls(_ticketZones(t.号码), 'pred', { size: 24, showLabels: false, hitHighlight: false })}</span>
            <span style="min-width:48px;text-align:center;font-size:12px;color:var(--gray)">命中 ${t.总命中}</span>
            <span style="min-width:52px;text-align:center;padding:1px 8px;border-radius:10px;font-size:12px;${ps || 'background:#f5f5f5;color:#9e9e9e'}">${isWin ? lvText(lv) : '未中'}</span>
        </div>`;
    }

    return `<div>
        <h3 style="margin:0 0 4px">${d.彩种} · 第${d.期号}期 — 第 ${d.期号内批号} 批 / 共 ${d.期号内批数} 批 ${srcTip}</h3>
        <div style="font-size:13px;color:var(--gray);margin-bottom:10px;line-height:1.8">
            <span>批次号：<strong>${d.批次号}</strong></span> ·
            <span>出号时间：<strong style="color:#455a64">${d.出号时间 || '—'}</strong>${genTip}</span> ·
            <span>结算时间：<strong>${d.结算时间 || '—'}</strong></span> ·
            <span>${d.原始注数 && d.原始注数 !== d.注数
                ? `原始出号 <strong>${d.原始注数}</strong> 注，质量压缩后登记 <strong>${d.注数}</strong> 注`
                : `本批共 <strong>${d.注数}</strong> 注`}，其中中奖 <strong style="color:#c62828">${d.中奖注数}</strong> 注</span>
        </div>
        <div style="border:1px solid #eee;border-radius:6px;max-height:55vh;overflow:auto">
            <div style="display:flex;gap:10px;padding:6px 12px;background:#fafafa;border-bottom:1px solid #eee;font-size:12px;color:var(--gray)">
                <span style="min-width:40px;text-align:right">注号</span><span style="flex:1">号码（按出号顺序，每 5 注一组）</span>
                <span style="min-width:48px;text-align:center">命中</span><span style="min-width:52px;text-align:center">结果</span>
            </div>
            ${rows || '<div style="padding:12px;color:var(--gray)">该批没有号码记录</div>'}
        </div>
        <p style="font-size:12px;color:var(--gray);margin:8px 0 0">「该批全部注」= 当时一次出号产出的所有号码（含未中奖的）。中奖与否只是事后结果，样本几十~一百注不构成任何策略优劣的证据。</p>
    </div>`;
}

async function loadHits() {
    const lottery = $('hits-lottery') ? $('hits-lottery').value : '全部';
    const prize = $('hits-prize') ? $('hits-prize').value : '全部';
    const type = $('hits-type') ? $('hits-type').value : '全部';
    const limitSel = $('hits-limit');
    const limit = limitSel ? limitSel.value : '100';
    const redHits = (lottery === '双色球' || lottery === '大乐透') && $('hits-red') ? $('hits-red').value : '';
    const blueHits = (lottery === '双色球' || lottery === '大乐透') && $('hits-blue') ? $('hits-blue').value : '';
    const totalHits = (lottery !== '双色球' && lottery !== '大乐透') && $('hits-total') ? $('hits-total').value : '';
    const amount = $('hits-amount') ? $('hits-amount').value : '';
    const sortSel = $('hits-sort');
    let sortBy = 'time', sortOrder = 'desc';
    if (sortSel && sortSel.selectedOptions.length) {
        const opt = sortSel.selectedOptions[0];
        sortBy = opt.value;
        sortOrder = opt.getAttribute('data-order') || 'desc';
    }
    const box = $('hits-result');
    if (box) box.innerHTML = '<div class="alert alert-info">加载中...</div>';

    try {
        let url = `/api/hits?lottery=${encodeURIComponent(lottery)}&min_prize=${encodeURIComponent(prize)}&type=${encodeURIComponent(type)}&limit=${encodeURIComponent(limit)}&sort_by=${encodeURIComponent(sortBy)}&sort_order=${encodeURIComponent(sortOrder)}`;
        if (redHits !== '') url += `&red_hits=${encodeURIComponent(redHits)}`;
        if (blueHits !== '') url += `&blue_hits=${encodeURIComponent(blueHits)}`;
        if (totalHits !== '') url += `&total_hits=${encodeURIComponent(totalHits)}`;
        if (amount !== '') url += `&min_amount=${encodeURIComponent(amount)}`;
        const tb = _hitsTimeBounds();
        if (tb.from) url += `&date_from=${encodeURIComponent(tb.from)}`;
        if (tb.to) url += `&date_to=${encodeURIComponent(tb.to)}`;
        const data = await api(url);
        // 批次弹窗按下标回查记录，避免把中文批次号塞进内联 onclick 的转义坑
        window.__hitsRecords = data.records || [];
        // 「筛出 xx 注 / xxxx 注（总数）」：分子=命中筛选的注数，分母=不加筛选的全部命中注数
        const universe = Number(data.universe || 0);
        const matched = Number(data.total != null ? data.total : (data.records || []).length);
        const shown = Number(data.returned != null ? data.returned : (data.records || []).length);
        const trunc = !!(data.limit && data.limit > 0 && matched > shown);
        const unknownAmt = Number(data.amount_unknown || 0);
        _renderHitsStat(universe);
        _renderHitsCount(matched, universe);
        if (!data.records || !data.records.length) {
            html('hits-result', `<div class="alert alert-info">筛出 <strong>0</strong> 注 / <strong>${universe}</strong> 注（总数）——没有符合条件的命中记录。${lottery !== '全部' ? `（${lottery}）` : ''} 预测开奖并评估后即可在此看到。</div>`);
            return;
        }

        const prizeStyle = {
            '一等': 'background:linear-gradient(90deg,#ffd700,#ffed4a);color:#7a5c00;font-weight:bold',
            '二等': 'background:linear-gradient(90deg,#ffd699,#ffe0b2);color:#b26a00;font-weight:bold',
            '三等': 'background:#e3f2fd;color:#1565c0;font-weight:bold',
            '四等': 'background:#e8f5e9;color:#2e7d32',
            '五等': 'background:#f3e5f5;color:#6a1b9a',
            '六等': 'background:#fff3e0;color:#e65100',
            '七等': 'background:#e0f7fa;color:#00838f',
            '八等': 'background:#efebe9;color:#4e342e',
            '九等': 'background:#f5f5f5;color:#616161',
            // 七星彩（一等奖~六等奖，自带"奖"字）
            '一等奖': 'background:linear-gradient(90deg,#ffd700,#ffed4a);color:#7a5c00;font-weight:bold',
            '二等奖': 'background:linear-gradient(90deg,#ffd699,#ffe0b2);color:#b26a00;font-weight:bold',
            '三等奖': 'background:#e3f2fd;color:#1565c0;font-weight:bold',
            '四等奖': 'background:#e8f5e9;color:#2e7d32',
            '五等奖': 'background:#f3e5f5;color:#6a1b9a',
            '六等奖': 'background:#fff3e0;color:#e65100',
            // 数字型玩法名
            '直选': 'background:linear-gradient(90deg,#ffd700,#ffed4a);color:#7a5c00;font-weight:bold',
            '组选3': 'background:#e3f2fd;color:#1565c0;font-weight:bold',
            '组选6': 'background:#e8f5e9;color:#2e7d32',
        };

        const trainCount = data.records.filter(r => !r.valid_prediction).length;
        let htmlStr = `<div style="margin-bottom:10px;font-size:13px;color:var(--gray);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <span>筛出 <strong style="color:#c62828;font-size:15px">${matched}</strong> 注 / <strong>${universe}</strong> 注（总数）` +
            (trunc ? ` · 已显示前 <strong>${shown}</strong> 条` : ``) +
            (trainCount ? ` · 本页 <strong style="color:#e65100">${trainCount}</strong> 注为「开奖后回测·训练」` : ``) +
            (unknownAmt ? `<br><span style="color:#e65100">另有 <strong>${unknownAmt}</strong> 注浮动奖级当期金额未知，未参与奖金筛选</span>` : ``) +
            `</span>
            <button class="btn btn-default btn-sm" onclick="_toggleAllHits(this)">全部展开</button>
        </div>`;
        htmlStr += '<div class="hits-accordion">';
        for (let i = 0; i < data.records.length; i++) {
            const r = data.records[i];
            const ps = prizeStyle[r.中奖等级] || 'background:#f5f5f5;color:#333';
            const lv = r.中奖等级 || '未中';
            // 仅乐透型"一等~六等"补"奖"字；七星彩"一等奖~六等奖"自带"奖"、
            // 数字型"直选/组选3/组选6"是玩法名 → 都不再追加，
            // 否则会渲染出"一等奖奖""直选奖"这种怪值。
            const lvText = /^[一二三四五六七八九]等$/.test(lv) ? (lv + '奖') : lv;
            const isTrain = !r.valid_prediction;
            const typeTag = isTrain
                ? '<span style="display:inline-block;margin-left:8px;padding:2px 10px;border-radius:12px;font-size:12px;background:#fff3e0;color:#e65100;font-weight:bold"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 训练</span>'
                : '<span style="display:inline-block;margin-left:8px;padding:2px 10px;border-radius:12px;font-size:12px;background:#e8f5e9;color:#2e7d32"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 真预测</span>';
            const v = r.view || { zones: [], total_hit: 0, total_choose: 0 };
            const predBox = renderZoneBalls(v.zones, 'pred', { size: 24, showLabels: false, hitHighlight: true });
            const actBox = renderZoneBalls(v.zones, 'actual', { size: 24, showLabels: false, hitHighlight: true });
            const hitParts = v.zones.map(z => `${z.name}${z.hit}/${z.choose}`).join(' · ');
            // 「这一注可能中多少钱」——固定奖级=确定金额；浮动奖级（双/大/七星彩一、二等）
            // 优先显示当期实际单注奖金，读不到则显示浮动+名义上限。文案与来源由后端统一给出。
            const pz = r.奖金 || {};
            const payText = pz.文本 || '—';
            const payTitle = String(pz.备注 || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
            const payIcon = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 6v12"/><path d="M15.5 9.2A3.2 3.2 0 0 0 12.6 8h-1.3a2.2 2.2 0 0 0 0 4.4h1.4a2.2 2.2 0 0 1 0 4.4h-1.3a3.2 3.2 0 0 1-2.9-1.2"/></svg>';
            const payStyle = pz.浮动
                ? 'background:#fff3e0;color:#e65100'
                : 'background:linear-gradient(90deg,#ffd700,#ffed4a);color:#7a5c00';
            const payBadge = `<span title="${payTitle}" style="display:inline-block;margin-left:6px;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:bold;${payStyle}">${payIcon}${payText}</span>`;
            const payDetail = `<span title="${payTitle}">单注奖金：<strong style="color:${pz.浮动 ? '#e65100' : '#2e7d32'}">${payText}</strong>${pz.来源 ? `（${pz.来源}）` : ''}</span>`;
            htmlStr += `<div class="hit-item ${isTrain ? 'train' : ''}">
                <div class="hit-header" onclick="_toggleHitItem(${i})">
                    <div class="hit-title">
                        <span style="display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;${ps}">${lvText}</span>
                        <strong style="font-size:14px">${r.lottery} · 第${r.期号}期</strong>
                        ${typeTag}
                        ${payBadge}
                    </div>
                    <div class="hit-meta">
                        <span class="hit-hit">命中 <strong>${v.total_hit}</strong>/${v.total_choose}</span>
                        ${r.批次标签 ? `<span title="${_batchTitle(r)}" onclick="_showBatchDetail(${i})" style="padding:2px 9px;border-radius:12px;font-size:12px;background:#eef2f7;color:#455a64;font-weight:600;white-space:nowrap;cursor:pointer;border:1px dashed #b0bec5" onmouseover="this.style.background='#e0e7ee'" onmouseout="this.style.background='#eef2f7'">${r.批次标签} ▾</span>` : ''}
                        <span>${(r.评估时间||'').replace('T',' ').slice(0,16)}</span>
                        <span class="hit-arrow" id="hit-arrow-${i}">▶</span>
                    </div>
                </div>
                <div class="hit-body" id="hit-body-${i}" style="display:none">
                    <div class="hit-row">
                        <span class="hit-label">预测号码</span>
                        <div class="hit-balls">${predBox}</div>
                    </div>
                    <div class="hit-row">
                        <span class="hit-label">实际开奖</span>
                        <div class="hit-balls">${actBox}</div>
                    </div>
                    <div class="hit-detail">
                        ${payDetail}
                        <span>分区命中：<strong>${hitParts}</strong></span>
                        <span>策略：<strong>${r.策略 || '-'}</strong></span>
                        <span>来源：<strong>${r.来源 || '-'}</strong></span>
                        <span>预测日期：<strong>${r.预测日期}</strong></span>
                        <span>开奖日期：<strong>${r.开奖日期}</strong></span>
                    </div>
                </div>
            </div>`;
        }
        htmlStr += '</div>';
        html('hits-result', htmlStr);
    } catch (e) {
        html('hits-result', `<div class="alert alert-danger">加载失败：${e.message || e}</div>`);
    }
}

function _toggleHitItem(idx) {
    const body = $(`hit-body-${idx}`);
    const arrow = $(`hit-arrow-${idx}`);
    if (!body) return;
    const open = body.style.display === 'block';
    body.style.display = open ? 'none' : 'block';
    if (arrow) arrow.textContent = open ? '▶' : '▼';
}

function _toggleAllHits(btn) {
    const bodies = document.querySelectorAll('.hit-body');
    const arrows = document.querySelectorAll('.hit-arrow');
    if (!bodies.length) return;
    const allOpen = Array.from(bodies).every(b => b.style.display === 'block');
    bodies.forEach(b => b.style.display = allOpen ? 'none' : 'block');
    arrows.forEach(a => a.textContent = allOpen ? '▶' : '▼');
    if (btn) btn.textContent = allOpen ? '全部展开' : '全部收起';
}

// ===== 反馈闭环 =====
// 根据下拉决定要渲染的彩种列表
function _fbLotteries() {
    const sel = $('fb-lottery').value;
    return sel === '全部彩种' ? ['双色球', '大乐透', '排列5', '福彩3D', '排列3', '七星彩'] : [sel];
}

// 切换彩种时，控制红/蓝命中筛选（仅红蓝型）与总命中筛选（仅数字型）的显示
function _onFbLotteryChange() {
    const lottery = $('fb-lottery') ? $('fb-lottery').value : '全部彩种';
    const isRedBlue = lottery === '全部彩种' || lottery === '双色球' || lottery === '大乐透';
    const redBox = $('fb-red-hits');
    const blueBox = $('fb-blue-hits');
    const totalBox = $('fb-total-hits');
    if (redBox) redBox.style.display = isRedBlue ? '' : 'none';
    if (blueBox) blueBox.style.display = isRedBlue ? '' : 'none';
    if (totalBox) totalBox.style.display = isRedBlue ? 'none' : '';
    _refreshFeedbackHistoryIfVisible();
}

// 若反馈详情已展开，自动刷新历史命中表格（用于筛选器 onchange）
function _refreshFeedbackHistoryIfVisible() {
    const detail = $('fb-detail');
    if (!detail || detail.classList.contains('hidden')) return;
    const lotteries = _fbLotteries();
    for (const lottery of lotteries) {
        loadFeedbackHistory(lottery);
    }
}

async function loadFeedbackSummary() {
    _onFbLotteryChange();  // 初始化筛选器可见性
    const lotteries = _fbLotteries();
    const lookback = $('fb-lookback').value;
    html('fb-summary', '<div class="spinner"></div> 加载中...');
    hide('fb-detail');
    hide('fb-compare');

    // 摘要：每个彩种一块
    let summaryHtml = '';
    for (const lottery of lotteries) {
        const data = await api(`/api/feedback/${encodeURIComponent(lottery)}/summary?lookback=${lookback}`);
        if (data.error) { summaryHtml += `<div class="alert alert-danger">${lottery}：${data.error}</div>`; continue; }
        summaryHtml += _renderFbSummaryCard(lottery, data);
    }
    html('fb-summary', summaryHtml);

    // 详情容器：每个彩种一个 三档对比 + 待开奖 + 历史 区块
    let detailHtml = '';
    for (const lottery of lotteries) {
        detailHtml += `
        <div class="card" style="margin:12px 0">
          <h3>${ico('target')} ${lottery} · 待开奖预测</h3>
          <div id="fb-tiercomp-${lottery}"></div>
          <div id="fb-pending-${lottery}"><span style="color:var(--gray);font-size:13px">加载中...</span></div>
        </div>
        <div class="card" style="margin:12px 0">
          <h3>${ico('board')} ${lottery} · 历史命中记录</h3>
          <div id="fb-history-${lottery}"><span style="color:var(--gray);font-size:13px">加载中...</span></div>
        </div>`;
    }
    html('fb-detail', detailHtml);
    show('fb-detail');

    for (const lottery of lotteries) {
        loadFeedbackPending(lottery);
        loadFeedbackHistory(lottery);
    }
}

// 单个彩种的策略权重 + 各策略表现卡片
function _renderFbSummaryCard(lottery, data) {
    let weightsHtml = '';
    for (const [name, w] of Object.entries(data.weights || {})) {
        const pct = (w * 100).toFixed(2);
        weightsHtml += `<div style="margin:8px 0">
          <div style="display:flex;justify-content:space-between;font-size:13px">
            <span>${_strategyBadge(name)}</span><strong>${pct}%</strong>
          </div>
          <div style="background:var(--gray);height:8px;border-radius:4px;margin-top:4px">
            <div style="background:var(--primary);height:100%;width:${pct}%;border-radius:4px"></div>
          </div>
        </div>`;
    }

    let statsHtml = '';
    const stratEntries = data.strategies ? Object.entries(data.strategies) : [];
    if (stratEntries.length > 0) {
        const isRedBlue = data.is_redblue !== false;
        const hitHeaders = isRedBlue
            ? `<th>平均红球命中</th><th>平均蓝球命中</th>`
            : `<th>平均命中率</th>`;
        const colspan = isRedBlue ? 5 : 4;
        statsHtml = `<table><tr><th>策略</th><th>样本数</th>${hitHeaders}<th>中奖率</th><th>中奖等级分布</th></tr>`;
        for (const [name, s] of stratEntries) {
            if (s.样本数 === 0) {
                statsHtml += `<tr><td>${name}</td><td colspan="${colspan}" style="color:var(--gray)">无数据</td></tr>`;
                continue;
            }
            const gradeDist = Object.entries(s.中奖等级分布 || {})
                .map(([k, v]) => `${k}:${v}`).join(' ');
            const hitCells = isRedBlue
                ? `<td>${s.平均红球命中}</td><td>${s.平均蓝球命中}</td>`
                : `<td>${(s.平均命中率 * 100).toFixed(2)}%</td>`;
            statsHtml += `<tr>
              <td>${_strategyBadge(name)}</td>
              <td>${s.样本数}</td>
              ${hitCells}
              <td>${(s.中奖率 * 100).toFixed(2)}%</td>
              <td style="font-size:12px">${gradeDist}</td>
            </tr>`;
        }
        statsHtml += `</table>`;
    } else {
        statsHtml = `<div class="alert alert-info">暂无反馈数据，请先预测并等待开奖后执行评估</div>`;
    }

    return `<div class="card" style="margin:12px 0">
      <h3>${ico('target')} ${lottery} · 反馈摘要</h3>
      <div class="grid-2">
        <div>
          <h4 style="margin:6px 0"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3v18M5 7h14"/><path d="M5 7l-2 6h4zM19 7l-2 6h4z"/></svg> 当前策略权重</h4>
          <p style="font-size:12px;color:var(--gray)">基于${data.lookback ? '近 ' + data.lookback + ' 期' : '全部历史'}反馈动态调整</p>
          ${weightsHtml}
      <p style="font-size:12px;color:var(--gray);margin-top:10px">
        真预测样本: <strong>${data.valid_records || 0}</strong>（开奖前生成）|
        开奖后回测: <strong style="color:#e65100">${(data.total_records||0)-(data.valid_records||0)}</strong>（训练，不计入表现）|
        待开奖预测:
        <button class="btn btn-sm btn-info" style="margin-left:4px" ${(data.pending_count || 0) === 0 ? 'disabled' : ''} onclick="showPendingPreview('${lottery}')">
          ${ico('target')} ${data.pending_count || 0} 组
        </button>
      </p>
        </div>
        <div>
          <h4 style="margin:6px 0">${ico('board')} 各策略表现（仅真预测）</h4>
          ${statsHtml}
        </div>
      </div>
    </div>`;
}

async function evaluatePending() {
    const lotteries = _fbLotteries();
    html('fb-summary', '<div class="spinner"></div> 评估中...');
    const results = [];
    for (const lottery of lotteries) {
        const data = await api(`/api/feedback/${encodeURIComponent(lottery)}/evaluate`, { method: 'POST' });
        if (data.error) { results.push(`<div class="alert alert-danger">${lottery}：${data.error}</div>`); continue; }
        let weightsStr = Object.entries(data.updated_weights || {})
            .map(([k, v]) => `${k}: ${(v * 100).toFixed(2)}%`).join(' | ');
        results.push(`<div class="alert alert-success">
          <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> ${lottery} 评估完成<br>
          已评估: <strong>${data.evaluated_count}</strong> 条预测 |
          新增反馈: <strong>${data.new_feedback_count}</strong> 条 |
          仍 pending: <strong>${data.still_pending}</strong> 条<br>
          更新后权重: ${weightsStr}
        </div>`);
    }
    html('fb-summary', results.join(''));
    setTimeout(() => loadFeedbackSummary(), 1500);
}

// 把 N 张号码按 groupSize 一组打包成「复式组」：每组 5 注便于一次性下注，
// 组内按推荐分降序，整组按「组内最大推荐分」降序，Top1 标 recommended。
// 用途：renderPendingRecords / _renderEvPending 共用同一份打包逻辑。
function _bundleIntoGroups(tickets, groupSize = 5) {
    if (!Array.isArray(tickets) || tickets.length === 0) return [];
    const groups = [];
    for (let i = 0; i < tickets.length; i += groupSize) {
        groups.push(tickets.slice(i, i + groupSize));
    }
    groups.forEach(g => g.sort((a, b) => (b.推荐分 || 0) - (a.推荐分 || 0)));
    const decorated = groups.map((g, idx) => {
        const topScore = g.reduce((m, t) => Math.max(m, t.推荐分 || 0), 0);
        const meanConf = g.reduce((s, t) => s + (t.置信度 || 0), 0) / g.length;
        const strategies = [...new Set(g.map(t => t.策略 || '—'))].join('+');
        return { idx, tickets: g, topScore, meanConf, strategies };
    });
    decorated.sort((a, b) => b.topScore - a.topScore);
    if (decorated.length > 0) decorated[0].recommended = true;
    return decorated;
}

// 三档档位 Tab 切换（renderPendingRecords 渲染的档位分组）
let _tierTabSeq = 0;
window._switchTierTab = function (uid, idx, btn) {
    const root = document.getElementById(uid);
    if (!root) return;
    root.querySelectorAll('[data-tier-body]').forEach(el => {
        el.style.display = el.getAttribute('data-tier-body') === String(idx) ? 'block' : 'none';
    });
    root.querySelectorAll('[data-tier-btn]').forEach(b => {
        b.style.background = (b === btn) ? 'var(--primary, #2c5fa8)' : 'transparent';
        b.style.color = (b === btn) ? '#fff' : 'var(--gray)';
    });
};

const _TIER_COLORS = { '一般': '#7f8c9b', '稳健': '#e67e22', '高鲁棒': '#27ae60' };

function _renderPendingList(records, groupSize, tierLabel) {
    let htmlStr = '';
    // 全部待开奖记录（同档位内按 期号+档位 去重后），按目标期号升序
    const sorted = [...records].sort((a, b) => String(a.目标期号).localeCompare(String(b.目标期号)));
    for (const p of sorted) {
        const srcTag = p.来源 === '训练'
            ? '<span class="tag tag-train" style="margin-left:5px">训练</span>'
            : '<span class="tag tag-predict" style="margin-left:5px">预测</span>';
        const tierColor = _TIER_COLORS[tierLabel] || '#7f8c9b';
        const tierTag = `<span style="background:${tierColor};color:#fff;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:4px">${tierLabel}档</span>`;
        const targetIssue = p.目标期号 ? `第 ${p.目标期号} 期 · ` : '';
        const bundles = _bundleIntoGroups(p.预测号码 || [], groupSize);
        const groupsHtml = bundles.map((g, gi) => {
            const head = `<div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin:8px 0 4px">
                <span style="font-size:12px;font-weight:700;color:var(--gray)">第${gi + 1}组（${g.tickets.length} 注）</span>
                <span style="font-size:11px;color:var(--gray)">组内最大推荐分 ${g.topScore.toFixed(3)} · 平均置信度 ${(g.meanConf * 100).toFixed(0)}% · 策略 ${g.strategies}</span>
                ${g.recommended ? '<span style="background:#27ae60;color:#fff;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px">★ 推荐使用</span>' : ''}
            </div>`;
            const ticketsHtml = g.tickets.map(t => {
                const conf = (t.置信度 != null) ? (t.置信度 * 100).toFixed(0) + '%' : '—';
                const w = (t.策略权重 != null) ? (t.策略权重 * 100).toFixed(0) + '%' : '—';
                const recScore = (t.推荐分 != null) ? t.推荐分.toFixed(3) : '—';
                const rob = t.鲁棒性;
                const robStr = rob
                    ? ` · 前列命中 ${( (rob['前列命中率'] || 0) * 100).toFixed(0)}% · 半区命中 ${((rob['半区命中率'] || 0) * 100).toFixed(0)}%`
                    : '';
                return `<div style="margin:3px 0;font-size:12px;display:flex;align-items:center;flex-wrap:wrap;gap:4px 0;padding-left:6px;border-left:2px solid ${g.recommended ? '#27ae60' : '#eee'}">
                    ${renderPredNumbers(_ticketZoneDict(t), { size: 22, showLabels: false })}
                    <span style="color:var(--gray);margin-left:6px">${t.策略 || ''}</span>
                    <span style="color:var(--gray);font-size:11px;margin-left:8px">置信度 ${conf} · 权重 ${w} · 推荐分 ${recScore}${robStr}</span>
                </div>`;
            }).join('');
            return head + ticketsHtml;
        }).join('');
        const empty = bundles.length === 0
            ? '<div style="font-size:12px;color:var(--gray);margin-top:6px">无预测号码</div>'
            : '';
        htmlStr += `<div class="card" style="margin:5px 0;padding:10px">
          <div style="font-size:13px;font-weight:700">${targetIssue}目标开奖 ${p.预测日期 || '未知'}${tierTag}</div>
          <div style="font-size:11px;color:var(--gray);margin:3px 0">生成于 ${p.记录时间 ? p.记录时间.slice(0,19).replace('T',' ') : '未知'} ${srcTag}</div>
          ${p.鲁棒性说明 ? `<div style="font-size:11px;color:var(--gray);margin:2px 0">${p.鲁棒性说明}</div>` : ''}
          ${p.压缩 && p.压缩.压缩前注数 !== p.压缩.压缩后注数 ? `<div style="font-size:11px;color:var(--gray);margin:2px 0">质量压缩：出号 ${p.压缩.压缩前注数} 注 → 登记 ${p.压缩.压缩后注数} 注（低质量淘汰 ${p.压缩.质量淘汰} · 重复 ${p.压缩.重复淘汰} · 高重叠 ${p.压缩.重叠淘汰}）；只省成本，单注中奖概率不变</div>` : ''}
          ${groupsHtml}${empty}
        </div>`;
    }
    return htmlStr;
}

function renderPendingRecords(records, total, groupSize = 5) {
    let head = `<p style="font-size:13px;color:var(--gray)">共 ${total} 期待开奖预测（每 ${groupSize} 注为一组打包，按组内最大推荐分排序，Top1 标 ★ 推荐使用）</p>`;
    // 按档位分组（一般/稳健/高鲁棒；无档位记录归一般档，向后兼容）
    const tierOrder = ['一般', '稳健', '高鲁棒'];
    const byTier = {};
    for (const p of (records || [])) {
        const t = tierOrder.includes(p.档位) ? p.档位 : '一般';
        (byTier[t] = byTier[t] || []).push(p);
    }
    const tiers = tierOrder.filter(t => byTier[t] && byTier[t].length);
    if (tiers.length === 0) return head;
    // 单档：直接渲染（保持旧行为）
    if (tiers.length === 1) return head + _renderPendingList(byTier[tiers[0]], groupSize, tiers[0]);
    // 多档：Tab 切换
    const uid = 'tierTabs' + (++_tierTabSeq);
    const tabsHtml = `<div id="${uid}" style="display:flex;gap:6px;margin:8px 0">` + tiers.map((t, i) =>
        `<button data-tier-btn onclick="_switchTierTab('${uid}',${i},this)" style="border:1px solid var(--primary,#2c5fa8);border-radius:6px;padding:3px 12px;font-size:12px;cursor:pointer;background:${i === 0 ? 'var(--primary,#2c5fa8)' : 'transparent'};color:${i === 0 ? '#fff' : 'var(--gray)'}">${t}档（${byTier[t].length} 期）</button>`
    ).join('') + `</div>`;
    const bodies = tiers.map((t, i) =>
        `<div data-tier-body="${i}" style="display:${i === 0 ? 'block' : 'none'}">${_renderPendingList(byTier[t], groupSize, t)}</div>`
    ).join('');
    return head + tabsHtml + bodies;
}

async function showPendingPreview(lottery) {
    const modalHtml = `<div id="pending-preview-loading" class="spinner"></div> 加载 ${lottery} 待开奖预测...`;
    html('modal-body', modalHtml);
    show('modal');
    try {
        const data = await api(`/api/feedback/${encodeURIComponent(lottery)}/pending`);
        if (data.error || !data.total) {
            html('modal-body', `<div class="alert alert-info">${lottery} 当前无待开奖预测</div>`);
            return;
        }
        const body = `<h3>${ico('target')} ${lottery} · 待开奖预测</h3>` + renderPendingRecords(data.records, data.total);
        html('modal-body', body);
    } catch (e) {
        html('modal-body', `<div class="alert alert-danger">加载失败：${e.message || e}</div>`);
    }
}

async function loadFeedbackPending(lottery) {
    const el = $('fb-pending-' + lottery);
    if (!el) return;
    const data = await api(`/api/feedback/${encodeURIComponent(lottery)}/pending`);
    if (data.error || data.total === 0) {
        html('fb-pending-' + lottery, '<p style="color:var(--gray)">无待开奖预测</p>');
    } else {
        html('fb-pending-' + lottery, renderPendingRecords(data.records, data.total));
    }
    loadTierComparison(lottery);
}

// 三档命中对比（一般/稳健/高鲁棒）：数据来自档位化反馈结算
async function loadTierComparison(lottery) {
    const el = $('fb-tiercomp-' + lottery);
    if (!el) return;
    try {
        const data = await api(`/api/feedback/${encodeURIComponent(lottery)}/tier-comparison`);
        if (data.error || !data.tiers) { el.innerHTML = ''; return; }
        const order = ['一般', '稳健', '高鲁棒'];
        const rows = order.filter(t => data.tiers[t]).map(t => {
            const s = data.tiers[t];
            const hit = (s.平均命中得分 != null) ? s.平均命中得分.toFixed(3) : '—';
            const pl = (s.平均模拟盈亏 != null) ? '¥' + s.平均模拟盈亏.toFixed(2) : '—';
            return `<tr>
                <td style="padding:3px 8px;font-weight:700">${t}档</td>
                <td style="padding:3px 8px">${s.预测注数}（${s.覆盖期数} 期）</td>
                <td style="padding:3px 8px">${s.中奖次数}</td>
                <td style="padding:3px 8px">${(s.中奖率 * 100).toFixed(1)}%</td>
                <td style="padding:3px 8px">${hit}</td>
                <td style="padding:3px 8px">${pl}</td>
            </tr>`;
        }).join('');
        el.innerHTML = `<details style="margin-bottom:8px">
            <summary style="font-size:12px;color:var(--gray);cursor:pointer">三档命中对比（点开查看）</summary>
            <table style="border-collapse:collapse;font-size:12px;margin:6px 0">
                <thead><tr style="color:var(--gray)">
                    <th style="padding:3px 8px;text-align:left">档位</th>
                    <th style="padding:3px 8px;text-align:left">预测注数</th>
                    <th style="padding:3px 8px;text-align:left">中奖次数</th>
                    <th style="padding:3px 8px;text-align:left">中奖率</th>
                    <th style="padding:3px 8px;text-align:left">平均命中得分</th>
                    <th style="padding:3px 8px;text-align:left">平均模拟盈亏</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <div style="font-size:11px;color:var(--gray)">${data.说明 || ''}</div>
        </details>`;
    } catch (e) {
        el.innerHTML = '';
    }
}

async function loadFeedbackHistory(lottery) {
    const el = $('fb-history-' + lottery);
    if (!el) return;
    el.innerHTML = '<span style="color:var(--gray);font-size:13px">加载中...</span>';

    const source = $('fb-source') ? $('fb-source').value : 'all';
    const minPrize = $('fb-min-prize') ? $('fb-min-prize').value : '全部';
    const limitSel = $('fb-limit');
    const limit = limitSel ? limitSel.value : '100';
    const sortSel = $('fb-sort');
    let sortBy = 'time', sortOrder = 'desc';
    if (sortSel && sortSel.selectedOptions.length) {
        const opt = sortSel.selectedOptions[0];
        sortBy = opt.value;
        sortOrder = opt.getAttribute('data-order') || 'desc';
    }

    const isRedBlue = lottery === '双色球' || lottery === '大乐透';
    let redHits = '', blueHits = '', totalHits = '';
    if (isRedBlue) {
        redHits = $('fb-red-hits') ? $('fb-red-hits').value : '';
        blueHits = $('fb-blue-hits') ? $('fb-blue-hits').value : '';
    } else {
        totalHits = $('fb-total-hits') ? $('fb-total-hits').value : '';
    }

    let url = `/api/feedback/${encodeURIComponent(lottery)}/history?limit=${encodeURIComponent(limit)}&min_prize=${encodeURIComponent(minPrize)}&sort_by=${encodeURIComponent(sortBy)}&sort_order=${encodeURIComponent(sortOrder)}`;
    if (redHits !== '') url += `&red_hits=${encodeURIComponent(redHits)}`;
    if (blueHits !== '') url += `&blue_hits=${encodeURIComponent(blueHits)}`;
    if (totalHits !== '') url += `&total_hits=${encodeURIComponent(totalHits)}`;

    let data;
    try {
        data = await api(url);
    } catch (e) {
        html('fb-history-' + lottery, `<div class="alert alert-danger">加载失败：${e.message || e}</div>`);
        return;
    }

    if (data.error || data.total === 0) {
        html('fb-history-' + lottery, '<p style="color:var(--gray)">无历史反馈记录</p>');
        return;
    }

    // 类型筛选（全部 / 真预测 / 开奖后回测·训练）
    // 注意：以 valid_prediction 为准，而非来源字段——开奖后生成的马后炮一律归为训练回测
    let records = data.records;
    if (source === 'predict') records = records.filter(f => f.valid_prediction !== false);
    else if (source === '训练') records = records.filter(f => f.valid_prediction === false);

    if (records.length === 0) {
        html('fb-history-' + lottery, `<p style="color:var(--gray)">当前筛选（${source === '训练' ? '训练生成' : '日常预测'}）下无记录</p>`);
        return;
    }

    // 表头：预测/实际号码合并为完整红蓝球列
    const isRb = data.is_redblue !== false;
    const hitHeaders = isRb ? `<th>红</th><th>蓝</th>` : `<th>命中</th>`;
    let htmlStr = `<p style="font-size:12px;color:var(--gray)">共 ${records.length} 条记录${source !== 'all' ? '（已筛选）' : ''}</p>`;
    htmlStr += `<table style="font-size:12px"><tr><th>期号</th><th>来源</th><th>策略</th><th>预测号码</th><th>实际开奖</th>${hitHeaders}<th>等级</th><th>时间</th></tr>`;
    for (const f of records) {
        const v = f.view || { zones: [], total_hit: 0, total_choose: 0, is_redblue: true };
        const predBalls = renderZoneBalls(v.zones, 'pred', { size: 20, showLabels: false });
        const actBalls = renderZoneBalls(v.zones, 'actual', { size: 20, showLabels: false });
        const gradeClass = f.中奖等级 === '未中' ? 'color:var(--gray)' : 'color:var(--success);font-weight:bold';
        const evalTime = f.评估时间 ? f.评估时间.slice(0,10) : '';
        const isTrain = f.valid_prediction === false;
        const srcTag = isTrain
            ? '<span class="tag tag-train" title="开奖后才生成的号码，属回测/训练样本，非真预测"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 开奖后回测·训练</span>'
            : '<span class="tag tag-predict"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 真预测</span>';
        const hitCells = isRb ? `<td>${f.红球命中}</td><td>${f.蓝球命中}</td>` : `<td>${v.total_hit}/${v.total_choose}</td>`;
        htmlStr += `<tr>
          <td>${f.期号 || ''}</td>
          <td>${srcTag}</td>
          <td>${f.策略 || ''}</td>
          <td>${predBalls}</td>
          <td>${actBalls}</td>
          ${hitCells}
          <td style="${gradeClass}">${f.中奖等级}</td>
          <td style="font-size:11px;color:var(--gray);white-space:nowrap">预测:${f.预测日期 || ''}<br>开奖:${f.开奖日期 || ''}<br>评估:${evalTime}</td>
        </tr>`;
    }
    htmlStr += `</table>`;
    html('fb-history-' + lottery, htmlStr);
}

// ===== 对比视图：预测 vs 实际开奖 并排卡片 =====
async function loadFeedbackCompare() {
    const lotteries = _fbLotteries();
    const lookback = $('fb-lookback').value;
    const source = $('fb-source') ? $('fb-source').value : 'all';
    html('fb-compare', '<div class="spinner"></div> 加载对比数据...');
    hide('fb-detail');
    show('fb-compare');

    let allHtml = '';
    const chartTasks = [];
    for (const lottery of lotteries) {
        const data = await api(`/api/feedback/${encodeURIComponent(lottery)}/compare?limit=${lookback}`);
        if (data.error) { allHtml += `<div class="alert alert-danger">${lottery}：${data.error}</div>`; continue; }
        if (!data.cards || data.cards.length === 0) {
            allHtml += `<div class="card" style="margin:12px 0"><h3>${ico('target')} ${lottery}</h3><div class="alert alert-info">暂无对比数据。请先预测 → 等待开奖 → 点「触发评估」，再来这里看预测 vs 实际开奖。</div></div>`;
            continue;
        }

        // 类型筛选（全部 / 真预测 / 开奖后回测·训练），以 valid_prediction 为准
        let cards = data.cards;
        if (source === 'predict') cards = cards.filter(c => c.valid_prediction !== false);
        else if (source === '训练') cards = cards.filter(c => c.valid_prediction === false);
        if (cards.length === 0) {
            allHtml += `<div class="card" style="margin:12px 0"><h3>${ico('target')} ${lottery}</h3><div class="alert alert-info">当前筛选（${source === '训练' ? '训练生成' : '日常预测'}）下无对比数据。</div></div>`;
            continue;
        }

        // 每个彩种独立 canvas id
        const cumId = 'fb-cum-' + lottery;
        const stratId = 'fb-strat-' + lottery;
        const chartsHtml = `<div class="grid-2" style="margin-bottom:15px">
            <div class="card">
                <h3>${ico('trend')} ${lottery} 累计命中率</h3>
                <canvas id="${cumId}" width="520" height="220" style="width:100%;height:auto"></canvas>
            </div>
            <div class="card">
                <h3><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M8 4h8v4a4 4 0 0 1-8 0V4z"/><path d="M8 5H5v2a3 3 0 0 0 3 3M16 5h3v2a3 3 0 0 1-3 3"/><path d="M12 12v4M9 20h6M10 16h4l-1 4h-2z"/></svg> ${lottery} 各策略平均总命中</h3>
                <div id="${stratId}"></div>
            </div>
        </div>`;

        let cardsHtml = `<h3 style="margin:10px 0"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg> ${lottery} · 预测 vs 实际开奖（${cards.length} 条${source !== 'all' ? '·已筛选' : ''}）</h3>
        <p style="font-size:12px;color:var(--gray)">左 = 系统生成的预测号码 · 右 = 该期实际开奖号码 · 中间 = 命中数/等级 · 下方 = 差了几号</p>`;
        for (const c of cards) cardsHtml += _renderFbCompareCard(c);

        allHtml += `<div class="card" style="margin:12px 0"><h3>${ico('target')} ${lottery} 对比视图</h3>${chartsHtml}${cardsHtml}</div>`;
        chartTasks.push({ cumId, stratId, cum: data.cumulative, strat: data.strategy_avg });
    }

    html('fb-compare', allHtml);
    for (const t of chartTasks) {
        try { _fbRenderCumChartById(t.cum, t.cumId); } catch (e) { console.warn('cum chart', e); }
        try { _fbRenderStratBarsById(t.strat, t.stratId); } catch (e) { console.warn('strat bars', e); }
    }
}

// 单条「预测 vs 实际」对比卡片
function _renderFbCompareCard(c) {
    const predDate = c.预测日期 ? `预测于 ${c.预测日期}` : '预测时间未知';
    const drawDate = c.开奖日期 ? `开奖 ${c.开奖日期}` : '开奖时间未知';
    const isTrain = c.valid_prediction === false;
    const srcTag = isTrain
        ? '<span class="tag tag-train" style="margin-left:5px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> 开奖后回测·训练</span>'
        : '<span class="tag tag-predict" style="margin-left:5px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg> 真预测</span>';
    const v = c.view || { zones: [], total_hit: 0, total_choose: 0, is_redblue: false };
    const gradeClass = c.中奖等级 === '未中' ? 'color:var(--gray)' : 'color:var(--success);font-weight:bold';
    const predBox = renderZoneBalls(v.zones, 'pred', { size: 22, showLabels: false, hitHighlight: true });
    const actBox = renderZoneBalls(v.zones, 'actual', { size: 22, showLabels: false, hitHighlight: true });
    const hitParts = v.zones.map(z => `${z.name}${z.hit}/${z.choose}`).join(' · ');
    const diffParts = [];
    for (const z of v.zones) {
        const miss = (z.pred || []).filter(n => !(z.actual || []).includes(n));
        const col = z.cls === 'red' ? '#c62828' : z.cls === 'blue' ? '#1E90FF' : 'var(--gray)';
        if (miss.length) diffParts.push(`<span style="color:${col};font-size:12px">差${z.name}: ${miss.map(n => String(n).padStart(2, '0')).join(' ')}</span>`);
    }
    return `<div class="card" style="margin:10px 0;padding:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
            <div><strong>第 ${c.期号} 期</strong> ${srcTag}</div>
            <div style="font-size:12px;color:var(--gray)">${predDate} · ${drawDate}</div>
        </div>
        <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;margin-top:10px">
            <div>
                <div style="font-size:12px;color:var(--gray);margin-bottom:3px">预测号码（${c.策略}）</div>
                <div>${predBox}</div>
            </div>
            <div style="text-align:center">
                <div style="font-size:22px;font-weight:700">${v.total_hit}</div>
                <div style="font-size:11px;color:var(--gray)">命中 ${v.total_choose} 选</div>
                <div style="font-size:11px;color:var(--gray)">${hitParts}</div>
                <div style="${gradeClass};font-size:13px">${c.中奖等级}</div>
            </div>
            <div>
                <div style="font-size:12px;color:var(--gray);margin-bottom:3px">实际开奖号码</div>
                <div>${actBox}</div>
            </div>
        </div>
        <div style="margin-top:8px;display:flex;gap:12px;flex-wrap:wrap">${diffParts.join('')}</div>
    </div>`;
}

function _fbRenderCumChartById(cum, id) {
    const cv = document.getElementById(id);
    if (!cv || !cum || cum.length === 0) return;
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height, pad = 36;
    ctx.clearRect(0, 0, W, H);
    // 轴
    ctx.strokeStyle = '#ccc';
    ctx.beginPath();
    ctx.moveTo(pad, 10); ctx.lineTo(pad, H - pad); ctx.lineTo(W - 10, H - pad);
    ctx.stroke();
    const n = cum.length;
    const xAt = i => pad + (W - pad - 10) * (n === 1 ? 0.5 : i / (n - 1));
    const yAt = v => (H - pad) - (H - pad - 10) * v;  // v in 0..1
    // 数字型只有累计命中率；红蓝彩种额外画红/蓝命中率
    const hasRedBlue = cum.some(p => p.红球命中率 != null || p.蓝球命中率 != null);
    const series = hasRedBlue
        ? [['红球命中率', '#E24B4A'], ['蓝球命中率', '#1E90FF']]
        : [['累计命中率', '#185FA5']];
    for (const [key, color] of series) {
        ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
        cum.forEach((p, i) => {
            const v = p[key];
            if (v == null) return;
            const x = xAt(i), y = yAt(Math.min(1, v));
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
    }
    ctx.fillStyle = '#888'; ctx.font = '11px sans-serif';
    if (hasRedBlue) {
        ctx.fillText('红', 6, 16); ctx.fillStyle = '#E24B4A';
        ctx.fillText('●', 22, 16);
        ctx.fillStyle = '#888'; ctx.fillText('蓝', 6, 30);
        ctx.fillStyle = '#1E90FF'; ctx.fillText('●', 22, 30);
    } else {
        ctx.fillText('累计命中', 6, 16); ctx.fillStyle = '#185FA5';
        ctx.fillText('●', 58, 16);
    }
}

function _fbRenderStratBarsById(strat, id) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!strat || strat.length === 0) { el.innerHTML = '<p style="color:var(--gray)">无数据</p>'; return; }
    let h = '';
    const maxV = Math.max(...strat.map(s => s.平均总命中), 0.001);
    for (const s of strat) {
        const pct = (s.平均总命中 / maxV * 100).toFixed(0);
        h += `<div style="margin:8px 0">
            <div style="display:flex;justify-content:space-between;font-size:13px">
                <span>${s.策略} <span style="color:var(--gray);font-size:11px">(n=${s.样本数})</span></span>
                <strong>${s.平均总命中}</strong>
            </div>
            <div style="background:var(--gray);height:8px;border-radius:4px;margin-top:4px">
                <div style="background:var(--primary);height:100%;width:${pct}%;border-radius:4px"></div>
            </div>
        </div>`;
    }
    el.innerHTML = h;
}

// ===== 策略战绩榜 =====
async function loadRecords(lottery) {
    html('records-result', '<div class="spinner"></div> 加载战绩中...');
    const data = await api(`/api/records/${encodeURIComponent(lottery)}`);
    const isRb = data.is_redblue;

    let htmlStr = '';

    // ===== 实盘战绩 =====
    const fb = data.feedback || {};
    htmlStr += `<div class="card"><h3>${ico('board')} 实盘战绩（开奖后真实对比）</h3>`;
    htmlStr += `<p style="font-size:12px;color:var(--gray);margin:4px 0 10px">${ico('robot')} <strong>ML策略</strong> = 训练好的机器学习/统计模型，按反馈权重参与预测；其余为规则策略。早期标签 ML(logistic)/统计训练(Wxx) 已自动归并到此处。</p>`;
    if (fb.total === 0 || Object.keys(fb.strategies || {}).length === 0) {
        htmlStr += `<div class="alert alert-warning">暂无实盘数据。跑一次「一键更新+评估」后，这里会记录每个策略的真实命中表现。</div>`;
    } else {
        htmlStr += `<p style="font-size:12px;color:var(--gray)">共 ${fb.total} 条反馈 | 最近评估: ${fb.last_evaluated || '未知'}</p>`;
        const headerCols = isRb
            ? '<th>策略</th><th>样本数</th><th>平均红球命中</th><th>平均蓝球命中</th><th>中奖次数</th><th>中奖率</th><th>趋势</th>'
            : '<th>策略</th><th>样本数</th><th>平均总命中</th><th>中奖次数</th><th>中奖率</th><th>趋势</th>';
        htmlStr += `<table><tr>${headerCols}</tr>`;
        for (const [name, s] of Object.entries(fb.strategies)) {
            const gradeStr = Object.entries(s.中奖等级分布 || {})
                .filter(([k]) => k !== '未中')
                .map(([k, v]) => `${k}:${v}`).join(' ') || '—';
            const bodyCols = isRb
                ? `<td>${s.平均红球命中}</td><td>${s.平均蓝球命中}</td>`
                : `<td>${s.平均总命中}</td>`;
            htmlStr += `<tr>
                <td>${_strategyBadge(name)}</td>
                <td>${s.样本数}</td>
                ${bodyCols}
                <td>${s.中奖次数}</td>
                <td>${(s.中奖率*100).toFixed(2)}%</td>
                <td style="font-size:16px">${s.趋势}</td>
            </tr>`;
        }
        htmlStr += `</table>`;
    }
    htmlStr += `</div>`;

    // ===== 回测战绩 =====
    const rl = data.rolling;
    htmlStr += `<div class="card" style="margin-top:15px"><h3><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 20h12"/><path d="M12 4l4 7-5 3-4-7z"/><path d="M12 14l-3 6"/></svg> 回测战绩（历史滚动预测）</h3>`;
    if (!rl) {
        htmlStr += `<div class="alert alert-warning">暂无回测数据。在「滚动训练」页面跑一次即可生成各策略的历史回测表现。</div>`;
    } else {
        htmlStr += `<p style="font-size:12px;color:var(--gray)">窗口 ${rl.window_size} 期 × 评估 ${rl.eval_periods} 期，共 ${rl.total_predictions} 次预测 | ${rl.training_time || ''}</p>`;
        const rHeaderCols = isRb
            ? '<th>策略</th><th>样本数</th><th>平均红球命中</th><th>平均蓝球命中</th><th>平均总命中</th><th>中奖率</th>'
            : '<th>策略</th><th>样本数</th><th>平均总命中</th><th>中奖率</th>';
        const colSpan = isRb ? 5 : 3;
        htmlStr += `<table><tr>${rHeaderCols}</tr>`;
        for (const [name, s] of Object.entries(rl.strategy_stats || {})) {
            if (s.error) {
                htmlStr += `<tr><td>${name}</td><td colspan="${colSpan}">无数据</td></tr>`;
                continue;
            }
            const rBodyCols = isRb
                ? `<td>${s.平均红球命中}</td><td>${s.平均蓝球命中}</td><td>${s.平均总命中}</td>`
                : `<td>${s.平均总命中}</td>`;
            htmlStr += `<tr>
                <td>${_strategyBadge(name)}</td>
                <td>${s.样本数}</td>
                ${rBodyCols}
                <td>${(s.中奖率*100).toFixed(2)}%</td>
            </tr>`;
        }
        htmlStr += `</table>`;
        htmlStr += `<p style="font-size:12px;color:var(--gray);margin-top:8px">回测目录: ${rl.record_dir}</p>`;
    }
    htmlStr += `</div>`;

    html('records-result', htmlStr);
}

// ===== 预测页战绩速览 =====
async function loadPredRecords() {
    const lottery = $('pred-lottery').value;
    const el = $('pred-records');
    if (!el) return;

    const data = await api(`/api/records/${encodeURIComponent(lottery)}`);
    const fb = data.feedback || {};
    const strategies = fb.strategies || {};
    const isRb = data.is_redblue;

    if (fb.total === 0 || Object.keys(strategies).length === 0) {
        html('pred-records', `<div class="alert alert-warning">暂无实盘战绩。跑一次看板「<svg class="ico" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M13 2L4 14h6l-1 8 9-12h-6z"/></svg> 一键流水线」后，这里会显示各策略的真实命中表现。</div>`);
        return;
    }

    let htmlStr = `<div style="font-size:12px;color:var(--gray);margin-bottom:6px">共 ${fb.total} 条反馈，最近评估: ${fb.last_evaluated || '未知'}</div>`;
    const hitHeader = isRb ? '<th>平均红球命中</th>' : '<th>平均总命中</th>';
    htmlStr += `<table style="font-size:13px"><tr><th>策略</th><th>样本</th>${hitHeader}<th>中奖率</th><th>趋势</th></tr>`;
    for (const [name, s] of Object.entries(strategies)) {
        const hitCell = isRb ? `<td>${s.平均红球命中}</td>` : `<td>${s.平均总命中}</td>`;
        htmlStr += `<tr>
            <td><strong>${name}</strong></td>
            <td>${s.样本数}</td>
            ${hitCell}
            <td>${(s.中奖率*100).toFixed(2)}%</td>
            <td style="font-size:16px">${s.趋势}</td>
        </tr>`;
    }
    htmlStr += `</table>`;
    htmlStr += `<div style="font-size:12px;color:var(--gray);margin-top:6px">完整对比见 <a href="/records" style="color:var(--primary)"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M8 4h8v4a4 4 0 0 1-8 0V4z"/><path d="M8 5H5v2a3 3 0 0 0 3 3M16 5h3v2a3 3 0 0 1-3 3"/><path d="M12 12v4M9 20h6M10 16h4l-1 4h-2z"/></svg> 战绩榜</a></div>`;
    html('pred-records', htmlStr);
}

// ===== 期望时机（EV） =====
const EV_COLORS = {
    '双色球': '#185FA5',
    '大乐透': '#2E7D32',
    '排列5': '#E24B4A',
    '福彩3D': '#E0A020',
    '排列3': '#8E44AD',
    '七星彩': '#16A085'
};
let _evChartState = null; // {canvas, ctx, W, H, padL, padR, padT, padB, series, min, max, xFn, yFn}
let _evLastSeries = null; // 缓存最近一次序列，供窗口缩放后重绘
let _evResizeBound = false;

async function loadEv() {
    const lottery = $('ev-lottery').value;
    const rawLimit = $('ev-limit').value;
    // 选择器支持「近 N 天」(d 前缀，日历窗口，使不同频率彩种落在同一时间轴)
    // 与「近 N 期」(p 前缀，按期数)。默认日历窗口。
    let limit = 50, days = null;
    if (rawLimit && rawLimit[0] === 'd') {
        days = parseInt(rawLimit.slice(1), 10) || 120;
    } else if (rawLimit && rawLimit[0] === 'p') {
        limit = parseInt(rawLimit.slice(1), 10) || 50;
    }
    const isAll = lottery.includes('全部彩种');
    const ALL_LOTTERIES = ['双色球', '大乐透', '排列5', '福彩3D', '排列3', '七星彩'];
    const targets = isAll ? ALL_LOTTERIES : [lottery];

    // 1. 当前期建议（双彩种并行）
    const adviceWrap = document.getElementById('ev-advice');
    adviceWrap.innerHTML = '<div class="spinner"></div> 加载建议...';
    const advicePromises = targets.map(l =>
        api(`/api/ev/advice/${encodeURIComponent(l)}`).then(a => ({...a, lottery: l})).catch(e => ({lottery: l, error: e.message || '加载失败'}))
    );
    const advices = await Promise.all(advicePromises);
    let adviceHtml = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px">';
    for (const a of advices) {
        const good = a.是否值得买 && !a.error;
        const color = good ? '#2e7d32' : (a.error ? '#888' : '#c62828');
        const bg = good ? '#f0faf0' : (a.error ? '#f8f8f8' : '#fdf0f0');
        const poolText = a.奖池奖金 ? (a.奖池奖金/1e8).toFixed(2)+'亿' : '未知';
        adviceHtml += `<div style="padding:12px 15px;border-radius:8px;background:${bg};border:1px solid rgba(0,0,0,0.05)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <strong style="color:${color};font-size:15px">${a.error ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/></svg> '+a.lottery : (a.lottery + ' ' + (a.期号 || '') + '期')}</strong>
            <span class="tag" style="background:${color};color:#fff">${good ? '正期望' : (a.error ? '无数据' : '常规期')}</span>
          </div>
          <div style="font-size:13px;color:var(--gray)">
            ${a.error ? a.error : `建议：<strong style="color:${color}">${a.建议}</strong> · 单注期望 <strong style="color:${color}">${a.单注期望}</strong> 元（固定奖 ${a.固定奖期望} + 浮动奖 ${a.浮动奖期望}）· 奖池 ${poolText}`}
          </div>
        </div>`;
    }
    adviceHtml += '</div>';
    adviceWrap.innerHTML = adviceHtml;

    // 2. 序列数据（双彩种并行）
    const detailCard = $('ev-detail-card');
    if (detailCard) { detailCard.classList.add('hidden'); detailCard.innerHTML = ''; }
    const dataResults = await Promise.all(targets.map(l =>
        api(`/api/ev/${encodeURIComponent(l)}?${days ? 'days=' + days : 'limit=' + limit}`).then(d => ({...d, lottery: l})).catch(e => ({lottery: l, error: e.message || '加载失败', periods: [], stats: {}}))
    ));

    // 3. 指标卡
    const metrics = [];
    for (const d of dataResults) {
        const st = d.stats || {};
        const current = (d.periods && d.periods[0]) || {};
        metrics.push({
            lottery: d.lottery,
            currentIssue: current.期号 || '-',
            currentEv: current.单注期望 !== undefined ? current.单注期望 : '-',
            avgEv: st.平均期望 !== undefined ? st.平均期望 : '-',
            positiveRate: st.正期望占比 !== undefined ? st.正期望占比 : '-',
            validCount: st.有效期数 || 0
        });
    }
    _renderEvMetrics(metrics);

    // 4. 统计文案
    const statParts = dataResults.filter(d => !d.error && d.stats && d.stats.有效期数)
        .map(d => `<strong>${d.lottery}</strong>：平均 ${d.stats.平均期望} · 正期望 ${d.stats.正期望期数}/${d.stats.有效期数} 期 (${d.stats.正期望占比}%)`);
    html('ev-stats', statParts.length ? statParts.join(' · ') : '暂无统计数据');

    // 5. 画交互曲线
    const seriesList = dataResults.filter(d => !d.error && d.periods && d.periods.length)
        .map(d => ({lottery: d.lottery, periods: d.periods, color: EV_COLORS[d.lottery] || '#185FA5'}));
    _renderEvLegend(seriesList);
    drawEvChart(seriesList);

    // 5.5 曲线下方：训练诚实化指标 + 待开奖推荐清单
    _renderEvTrainingHonesty(dataResults);
    _renderEvPending(dataResults);

    // 6. 明细表格
    let htmlStr = `<table style="font-size:13px"><tr>${isAll ? '<th>彩种</th>' : ''}<th>期号</th><th>日期</th><th>单注期望</th><th>固定奖</th><th>浮动奖</th><th>奖池(亿)</th><th>判断</th></tr>`;
    const allRows = [];
    for (const d of dataResults) {
        if (!d.periods) continue;
        for (const p of d.periods) {
            allRows.push({...p, lottery: d.lottery});
        }
    }
    // 按日期/期号统一排序（新在前）
    allRows.sort((a, b) => String(b.开奖日期 || '').localeCompare(String(a.开奖日期 || '')));
    for (const p of allRows) {
        const c = p.是否值得买 ? '#2e7d32' : '#888';
        const lotteryTag = `<span class="tag" style="background:${EV_COLORS[p.lottery] || '#666'};color:#fff">${p.lottery}</span>`;
        htmlStr += `<tr data-issue="${p.期号}" data-lottery="${p.lottery}">
            ${isAll ? `<td>${lotteryTag}</td>` : ''}
            <td>${p.期号}</td>
            <td style="white-space:nowrap">${p.开奖日期 || ''}</td>
            <td><strong style="color:${c}">${p.单注期望}</strong></td>
            <td>${p.固定奖期望}</td>
            <td>${p.浮动奖期望}</td>
            <td>${p.奖池奖金 ? (p.奖池奖金/1e8).toFixed(2) : '-'}</td>
            <td style="color:${c}">${p.是否值得买 ? '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3c2 3 4 4 4 8a4 4 0 0 1-8 0c0-1 .5-2 1-3 .5 1 1 1.5 1 2 0-2 1-4 2-7z"/></svg> 买点' : ''}</td>
        </tr>`;
    }
    htmlStr += `</table>`;
    html('ev-table-wrap', htmlStr);
}

function _renderEvTrainingHonesty(dataResults) {
    const el = document.getElementById('ev-training-honesty');
    if (!el) return;
    const parts = [];
    for (const d of dataResults) {
        const th = d.training_honesty;
        if (!th || !th.has_training) {
            parts.push(`<div style="padding:8px 0;font-size:13px;color:var(--gray)"><strong>${d.lottery}</strong>：${th && th.note ? th.note : '暂无训练记录，请先运行 train'}</div>`);
            continue;
        }
        const sev = th.selection_eval || {};
        const obs = th.selection_total_hits;
        const exp = sev.expected_total_hits;
        const adv = sev.total_advantage;
        const p = sev.p_value;
        const sig = sev.significant_05;
        const n = sev.n_periods;
        let sigColor = '#888', sigText = '无显著差异';
        if (sig && adv > 0) { sigColor = '#2e7d32'; sigText = '显著优于随机'; }
        else if (sig && adv < 0) { sigColor = '#c62828'; sigText = '显著差于随机'; }
        else if (sig) { sigColor = '#888'; sigText = '显著(无方向)'; }
        const advStr = (adv == null) ? '-' : ((adv >= 0 ? '+' : '') + adv.toFixed(4));
        parts.push(`<div style="padding:10px 0;border-bottom:1px solid #f3f3f3">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <strong>${d.lottery}</strong>
                <span class="tag" style="background:${sigColor};color:#fff">${sigText}</span>
            </div>
            <div style="font-size:13px;color:var(--gray);margin-top:5px">
                模型 <strong>${th.model_type || '-'}</strong> · 窗口 W${th.best_window != null ? th.best_window : '-'} ·
                选号均值命中 <strong style="color:#185FA5">${obs != null ? obs.toFixed(3) : '-'}</strong> /
                随机基线 <strong style="color:#888">${exp != null ? exp.toFixed(3) : '-'}</strong> ·
                优势 Δ <strong style="color:${adv >= 0 ? '#2e7d32' : '#c62828'}">${advStr}</strong>
            </div>
            <div style="font-size:12px;color:var(--gray);margin-top:3px">
                p=${p != null ? p.toFixed(4) : '-'}（评估 ${n != null ? n : '?'} 期）
                ${th.note ? ' · ' + th.note : ''}
            </div>
        </div>`);
    }
    el.innerHTML = parts.length ? parts.join('') : '<div style="font-size:13px;color:var(--gray)">暂无数据</div>';
}

function _renderEvPending(dataResults) {
    const el = document.getElementById('ev-pending');
    if (!el) return;
    const parts = [];
    for (const d of dataResults) {
        const pr = d.pending_recommended;
        const total = pr ? pr.total : 0;
        let line = `<div style="padding:10px 0;border-bottom:1px solid #f3f3f3">
            <div style="font-size:13px;margin-bottom:6px"><strong>${d.lottery}</strong> · 共 ${total} 期待开奖`;
        if (pr && pr.strategy_weights) {
            const ws = Object.entries(pr.strategy_weights).map(([k, v]) => `${k} ${(v * 100).toFixed(0)}%`).join(' · ');
            line += ` <span style="color:var(--gray)">[权重 ${ws}]</span>`;
        }
        line += `</div>`;
        if (!total) {
            line += `<div style="font-size:12px;color:var(--gray)">暂无待开奖预测</div></div>`;
            parts.push(line);
            continue;
        }
        const recs = [...pr.records].sort((a, b) => String(a.目标期号 || '').localeCompare(String(b.目标期号 || '')));
        for (const rec of recs) {
            const srcTag = rec.来源 === '训练'
                ? '<span class="tag tag-train" style="margin-left:5px">训练</span>'
                : '<span class="tag tag-predict" style="margin-left:5px">预测</span>';
            line += `<div style="margin:6px 0;padding-left:8px;border-left:3px solid #eee">
                <div style="font-size:12px;color:var(--gray)">目标期号 ${rec.目标期号} · ${rec.预测日期 || '未知'} ${srcTag}</div>`;
            const bundles = _bundleIntoGroups(rec.预测号码 || [], 5);
            for (const b of bundles) {
                line += `<div style="margin:3px 0;padding:2px 0;border-left:2px solid ${b.recommended ? '#27ae60' : '#eee'};padding-left:6px">
                    <div style="font-size:11px;color:var(--gray)">第${b.idx + 1}组（${b.tickets.length} 注）· 组内最大推荐分 ${b.topScore.toFixed(3)}${b.recommended ? ' · ★推荐使用' : ''}</div>`;
                for (const g of b.tickets) {
                    const conf = (g.置信度 != null) ? (g.置信度 * 100).toFixed(0) + '%' : '—';
                    const w = (g.策略权重 != null) ? (g.策略权重 * 100).toFixed(0) + '%' : '—';
                    const recScore = (g.推荐分 != null) ? g.推荐分.toFixed(3) : '—';
                    line += `<div style="margin:2px 0;font-size:12px;display:flex;align-items:center;flex-wrap:wrap;gap:4px 0">
                        ${renderPredNumbers(_ticketZoneDict(g), { size: 22, showLabels: false })}
                        <span style="color:var(--gray);margin-left:6px">${g.策略 || ''}</span>
                        <span style="color:var(--gray);font-size:11px;margin-left:8px">置信度 ${conf} · 权重 ${w} · 推荐分 ${recScore}</span>
                    </div>`;
                }
                line += `</div>`;
            }
            line += `</div>`;
        }
        line += `</div>`;
        parts.push(line);
    }
    el.innerHTML = parts.length ? parts.join('') : '<div style="font-size:13px;color:var(--gray)">暂无数据</div>';
}

function _renderEvMetrics(metrics) {
    const el = document.getElementById('ev-metrics');
    if (!el) return;
    let h = '';
    for (const m of metrics) {
        const color = typeof m.currentEv === 'number' && m.currentEv >= 0 ? '#2e7d32' : '#c62828';
        h += `<div class="card" style="padding:12px 15px">
            <div style="font-size:12px;color:var(--gray);margin-bottom:4px">${ico('target')} ${m.lottery} · 当前期望</div>
            <div style="font-size:22px;font-weight:700;color:${color}">${m.currentEv}</div>
            <div style="font-size:12px;color:var(--gray);margin-top:4px">期号 ${m.currentIssue} · 平均 ${m.avgEv} · 正期望 ${m.positiveRate}%</div>
        </div>`;
    }
    el.innerHTML = h;
}

function _renderEvLegend(seriesList) {
    const el = document.getElementById('ev-legend');
    if (!el) return;
    el.innerHTML = '';
    for (const s of seriesList) {
        const item = document.createElement('div');
        item.className = 'ev-legend-item';
        item.dataset.lottery = s.lottery;
        item.style.cssText = 'display:flex;align-items:center;gap:6px;cursor:pointer;padding:4px 8px;border-radius:4px;transition:opacity .2s,background .2s';
        item.innerHTML = `<span class="ev-legend-line" style="width:18px;height:3px;background:${s.color};border-radius:2px;transition:opacity .2s"></span>
            <span>${s.lottery} <span style="color:var(--gray)">(${s.periods.length}期)</span></span>`;
        item.addEventListener('click', () => _evToggleSeries(s.lottery));
        el.appendChild(item);
    }
}

function _evToggleSeries(lottery) {
    if (!_evChartState) return;
    const active = _evChartState.activeSeries || {};
    active[lottery] = active[lottery] !== false; // default true, click toggles
    active[lottery] = !active[lottery];
    _evChartState.activeSeries = active;
    // update legend opacity
    document.querySelectorAll('.ev-legend-item').forEach(item => {
        const line = item.querySelector('.ev-legend-line');
        const on = active[item.dataset.lottery] !== false;
        item.style.opacity = on ? '1' : '0.45';
        if (line) line.style.opacity = on ? '1' : '0.2';
    });
    _evChartRedrawBase();
    // hide detail card because data changed
    const card = $('ev-detail-card');
    if (card) { card.classList.add('hidden'); card.innerHTML = ''; }
}

function drawEvChart(seriesList) {
    const canvas = $('ev-chart');
    const tooltip = $('ev-tooltip');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    // 高清渲染：按显示宽度 × devicePixelRatio 设置位图并缩放上下文，
    // 避免大屏 / 高分屏（Retina）下 canvas 被拉伸导致模糊
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(Math.round(canvas.getBoundingClientRect().width) || 900, 320);
    const cssH = 360;
    canvas.style.height = cssH + 'px';
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = cssW, H = cssH;
    const padL = 52, padR = 24, padT = 28, padB = 36;
    ctx.clearRect(0, 0, W, H);
    _evLastSeries = seriesList;

    // 解绑旧事件
    if (_evChartState && _evChartState._handlers) {
        canvas.removeEventListener('mousemove', _evChartState._handlers.move);
        canvas.removeEventListener('mouseleave', _evChartState._handlers.leave);
        canvas.removeEventListener('click', _evChartState._handlers.click);
    }

    if (!seriesList || seriesList.length === 0 || seriesList.every(s => !s.periods || s.periods.length === 0)) {
        ctx.fillStyle = '#888'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center';
        ctx.fillText('暂无数据', W/2, H/2);
        _evChartState = null;
        if (tooltip) tooltip.style.display = 'none';
        return;
    }

    // 合并求全局 Y 范围
    const allEvs = seriesList.flatMap(s => s.periods.map(p => p.单注期望));
    const min = Math.min(-0.5, ...allEvs);
    const max = Math.max(0.5, ...allEvs);
    const range = Math.max(max - min, 0.5);

    // 按日期求最大重叠范围：统一 X 轴
    const dateSet = new Set();
    seriesList.forEach(s => s.periods.forEach(p => dateSet.add(p.开奖日期)));
    const dates = Array.from(dateSet).sort();
    const maxLen = dates.length;
    const xAt = i => padL + (W - padL - padR) * (maxLen <= 1 ? 0.5 : i / (maxLen - 1));
    const yAt = v => padT + (H - padT - padB) * (1 - (v - min) / range);

    // 网格与 Y 轴标签
    ctx.strokeStyle = '#e8e8e8';
    ctx.lineWidth = 1;
    ctx.font = '11px sans-serif';
    ctx.fillStyle = '#888';
    ctx.textAlign = 'right';
    for (let g = 0; g <= 4; g++) {
        const gy = padT + (H - padT - padB) * g / 4;
        ctx.beginPath();
        ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy);
        ctx.stroke();
        const val = max - range * g / 4;
        ctx.fillText(val.toFixed(1), padL - 6, gy + 4);
    }
    // X 轴日期标签（取 5 个）
    ctx.textAlign = 'center';
    for (let i = 0; i < 5; i++) {
        const idx = Math.round(i * (maxLen - 1) / 4);
        const d = dates[idx] || '';
        ctx.fillText(d.slice(5) || '-', xAt(idx), H - 10);
    }

    // 零线
    const zeroY = yAt(0);
    if (zeroY > padT && zeroY < H - padB) {
        ctx.strokeStyle = '#E24B4A';
        ctx.setLineDash([6, 4]);
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(padL, zeroY); ctx.lineTo(W - padR, zeroY);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // 为每个 series 按统一日期对齐坐标
    const renderedSeries = [];
    for (const s of seriesList) {
        const pts = [];
        for (let i = 0; i < dates.length; i++) {
            const p = s.periods.find(x => x.开奖日期 === dates[i]);
            if (p) pts.push({x: xAt(i), y: yAt(p.单注期望), period: p, dateIdx: i});
        }
        renderedSeries.push({...s, pts});
    }

    // 画曲线（带动画可用逐帧，这里用直接绘制保持响应）
    for (const s of renderedSeries) {
        if (s.pts.length === 0) continue;
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        s.pts.forEach((pt, i) => {
            if (i === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y);
        });
        ctx.stroke();

        // 买点标记
        ctx.fillStyle = '#E24B4A';
        for (const pt of s.pts) {
            if (pt.period.是否值得买) {
                ctx.beginPath();
                ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        // 最后一点高亮
        const last = s.pts[s.pts.length - 1];
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(last.x, last.y, 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
        ctx.stroke();
    }

    // 保存状态用于交互
    const activeSeries = {};
    for (const s of renderedSeries) activeSeries[s.lottery] = true;
    _evChartState = {canvas, ctx, W, H, padL, padR, padT, padB, series: renderedSeries, dates, min, max, xAt, yAt, activeSeries};

    // 绑定事件
    const hMove = e => _evChartHover(e);
    const hLeave = () => { if (tooltip) tooltip.style.display = 'none'; _evChartUnhighlight(); };
    const hClick = e => _evChartClick(e);
    canvas.addEventListener('mousemove', hMove);
    canvas.addEventListener('mouseleave', hLeave);
    canvas.addEventListener('click', hClick);
    _evChartState._handlers = {move: hMove, leave: hLeave, click: hClick};

    // 窗口缩放后按新尺寸重绘，保持高清
    if (!_evResizeBound) {
        _evResizeBound = true;
        let rt = null;
        window.addEventListener('resize', () => {
            clearTimeout(rt);
            rt = setTimeout(() => { if (_evLastSeries) drawEvChart(_evLastSeries); }, 200);
        });
    }
}

function _evChartHover(e) {
    const st = _evChartState;
    const tooltip = $('ev-tooltip');
    if (!st || !tooltip) return;
    const rect = st.canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (st.W / rect.width);

    // 找最近 x 索引
    let nearestI = Math.max(0, Math.min(st.dates.length - 1, Math.round((mx - st.padL) / (st.W - st.padL - st.padR) * (st.dates.length - 1))));

    // 收集该日期上的点（只显示未隐藏的 series）
    const active = st.activeSeries || {};
    const points = [];
    for (const s of st.series) {
        if (active[s.lottery] === false) continue;
        const pt = s.pts.find(p => p.dateIdx === nearestI);
        if (pt) points.push({lottery: s.lottery, color: s.color, ...pt});
    }
    if (points.length === 0) {
        tooltip.style.display = 'none';
        _evChartUnhighlight();
        return;
    }

    // 重绘并加垂线
    _evChartRedrawBase();
    st.ctx.strokeStyle = 'rgba(0,0,0,0.15)';
    st.ctx.setLineDash([4, 3]);
    st.ctx.lineWidth = 1;
    const x0 = points[0].x;
    st.ctx.beginPath();
    st.ctx.moveTo(x0, st.padT);
    st.ctx.lineTo(x0, st.H - st.padB);
    st.ctx.stroke();
    st.ctx.setLineDash([]);

    for (const pt of points) {
        st.ctx.strokeStyle = pt.color; st.ctx.lineWidth = 2;
        st.ctx.beginPath();
        st.ctx.arc(pt.x, pt.y, 7, 0, Math.PI * 2);
        st.ctx.stroke();
    }

    // tooltip 内容
    const d = st.dates[nearestI] || '-';
    let content = `<div style="font-weight:700;margin-bottom:6px">${d}</div>`;
    for (const pt of points) {
        const p = pt.period;
        const good = p.是否值得买;
        content += `<div style="margin:4px 0;display:flex;align-items:center;gap:6px">
            <span style="width:10px;height:10px;border-radius:50%;background:${pt.color}"></span>
            <span style="flex:1">${pt.lottery} 第${p.期号}期</span>
            <strong style="color:${good ? '#2e7d32' : '#c62828'}">${p.单注期望}</strong>
        </div>
        <div style="font-size:11px;color:var(--gray);padding-left:16px">
            固定奖 ${p.固定奖期望} · 浮动奖 ${p.浮动奖期望} · 奖池 ${p.奖池奖金 ? (p.奖池奖金/1e8).toFixed(2)+'亿' : '-'}
            ${good ? ' · <span style="color:#2e7d32"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3c2 3 4 4 4 8a4 4 0 0 1-8 0c0-1 .5-2 1-3 .5 1 1 1.5 1 2 0-2 1-4 2-7z"/></svg> 值得买</span>' : ''}
        </div>`;
    }
    tooltip.innerHTML = content;
    tooltip.style.display = 'block';

    // tooltip 定位：不超出 canvas
    const wrapRect = st.canvas.parentElement.getBoundingClientRect();
    let left = e.clientX - wrapRect.left + 14;
    let top = e.clientY - wrapRect.top + 14;
    if (left + 220 > wrapRect.width) left = e.clientX - wrapRect.left - 230;
    if (top + tooltip.offsetHeight > wrapRect.height) top = e.clientY - wrapRect.top - tooltip.offsetHeight - 10;
    tooltip.style.left = left + 'px';
    tooltip.style.top = top + 'px';
}

function _evChartUnhighlight() {
    if (!_evChartState) return;
    _evChartRedrawBase();
}

function _evChartRedrawBase() {
    const st = _evChartState;
    if (!st) return;
    st.ctx.clearRect(0, 0, st.W, st.H);

    // 网格
    st.ctx.strokeStyle = '#e8e8e8'; st.ctx.lineWidth = 1;
    st.ctx.font = '11px sans-serif'; st.ctx.fillStyle = '#888'; st.ctx.textAlign = 'right';
    const range = Math.max(st.max - st.min, 0.5);
    for (let g = 0; g <= 4; g++) {
        const gy = st.padT + (st.H - st.padT - st.padB) * g / 4;
        st.ctx.beginPath();
        st.ctx.moveTo(st.padL, gy); st.ctx.lineTo(st.W - st.padR, gy);
        st.ctx.stroke();
        const val = st.max - range * g / 4;
        st.ctx.fillText(val.toFixed(1), st.padL - 6, gy + 4);
    }
    st.ctx.textAlign = 'center';
    for (let i = 0; i < 5; i++) {
        const idx = Math.round(i * (st.dates.length - 1) / 4);
        const d = st.dates[idx] || '';
        st.ctx.fillText(d.slice(5) || '-', st.xAt(idx), st.H - 10);
    }

    // 零线
    const zeroY = st.yAt(0);
    if (zeroY > st.padT && zeroY < st.H - st.padB) {
        st.ctx.strokeStyle = '#E24B4A'; st.ctx.setLineDash([6, 4]); st.ctx.lineWidth = 1.5;
        st.ctx.beginPath();
        st.ctx.moveTo(st.padL, zeroY); st.ctx.lineTo(st.W - st.padR, zeroY);
        st.ctx.stroke();
        st.ctx.setLineDash([]);
    }

    // 曲线和点
    const active = st.activeSeries || {};
    for (const s of st.series) {
        if (active[s.lottery] === false) continue;
        if (s.pts.length === 0) continue;
        st.ctx.strokeStyle = s.color; st.ctx.lineWidth = 2.5;
        st.ctx.beginPath();
        s.pts.forEach((pt, i) => { if (i === 0) st.ctx.moveTo(pt.x, pt.y); else st.ctx.lineTo(pt.x, pt.y); });
        st.ctx.stroke();

        st.ctx.fillStyle = '#E24B4A';
        for (const pt of s.pts) {
            if (pt.period.是否值得买) {
                st.ctx.beginPath(); st.ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2); st.ctx.fill();
            }
        }
        const last = s.pts[s.pts.length - 1];
        st.ctx.fillStyle = s.color;
        st.ctx.beginPath(); st.ctx.arc(last.x, last.y, 6, 0, Math.PI * 2); st.ctx.fill();
        st.ctx.strokeStyle = '#fff'; st.ctx.lineWidth = 2; st.ctx.stroke();
    }
}

function _evChartClick(e) {
    if (!_evChartState) return;
    const st = _evChartState;
    const rect = st.canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (st.W / rect.width);
    const idx = Math.max(0, Math.min(st.dates.length - 1, Math.round((mx - st.padL) / (st.W - st.padL - st.padR) * (st.dates.length - 1))));
    const d = st.dates[idx];
    if (!d) return;
    _evShowDetailCard(d);
}

function _evShowDetailCard(date) {
    if (!_evChartState) return;
    const st = _evChartState;
    const active = st.activeSeries || {};
    const card = $('ev-detail-card');
    if (!card) return;

    // 收集该日期各可见彩种数据
    const items = [];
    for (const s of st.series) {
        if (active[s.lottery] === false) continue;
        const pt = s.pts.find(p => p.开奖日期 === date);
        if (pt) items.push({lottery: s.lottery, color: s.color, ...pt.period});
    }
    if (items.length === 0) { card.classList.add('hidden'); card.innerHTML = ''; return; }

    let inner = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <strong style="font-size:15px"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 21s7-6 7-11a7 7 0 0 0-14 0c0 5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/></svg> ${date} 详情</strong>
        <button onclick="document.getElementById('ev-detail-card').classList.add('hidden')" style="border:none;background:transparent;font-size:18px;cursor:pointer;color:var(--gray)">&times;</button>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">`;
    for (const it of items) {
        const good = it.是否值得买;
        const tag = good ? '<span class="tag" style="background:#2e7d32;color:#fff"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3c2 3 4 4 4 8a4 4 0 0 1-8 0c0-1 .5-2 1-3 .5 1 1 1.5 1 2 0-2 1-4 2-7z"/></svg> 值得买</span>' : '<span class="tag" style="background:#c62828;color:#fff">常规期</span>';
        inner += `<div class="card" style="padding:12px 15px;border-left:4px solid ${it.color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>${it.lottery} 第${it.期号}期</strong>
                ${tag}
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;font-size:13px;color:var(--gray)">
                <div>单注期望：<strong style="color:${good ? '#2e7d32' : '#c62828'}">${it.单注期望}</strong></div>
                <div>奖池：<strong>${it.奖池奖金 ? (it.奖池奖金/1e8).toFixed(2)+'亿' : '-'}</strong></div>
                <div>固定奖期望：<strong>${it.固定奖期望}</strong></div>
                <div>浮动奖期望：<strong>${it.浮动奖期望}</strong></div>
            </div>
            <div style="margin-top:10px;font-size:12px;color:var(--gray)">建议：<span style="color:${good ? '#2e7d32' : '#c62828'}">${good ? '期望 ≥ 0，理论上“值得买”' : '期望为负，建议小注或不买'}</span></div>
        </div>`;
    }
    inner += '</div>';
    card.innerHTML = inner;
    card.classList.remove('hidden');
}

// ===== 页面初始化 =====
document.addEventListener('DOMContentLoaded', function() {
    // 高亮当前导航
    const path = window.location.pathname;
    document.querySelectorAll('.navbar nav a').forEach(a => {
        if (a.getAttribute('href') === path) a.classList.add('active');
    });
    document.querySelectorAll('.sidebar a').forEach(a => {
        if (a.getAttribute('href') === path) a.classList.add('active');
    });

    // 页面特定初始化
    if (path === '/') {
        loadDataStatus();
        loadAutomationStatus();
        tsLoad();
        pnLoad();
    } else if (path === '/feedback') {
        loadFeedbackSummary();
    } else if (path === '/predict') {
        loadPredRecords();
    }

    // 全页面：实时服务状态（PID / 运行时长），确认当前为新进程
    loadSystemStatus();
});

// ===== 轻量 Toast 提示 =====
function showToast(msg, kind) {
    const container = $('alert-container');
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || 'info');
    el.textContent = msg;
    container.appendChild(el);
    // 入场
    requestAnimationFrame(() => el.classList.add('show'));
    // 自动消失
    setTimeout(() => {
        el.classList.remove('show');
        setTimeout(() => el.remove(), 300);
    }, 3200);
}

// ===== 服务重启（写 flag，由 run.py 看门狗热重启并加载最新代码）=====
async function restartService() {
    const btn = $('btn-restart');
    if (btn && btn.disabled) return;
    if (!confirm('确定重启 Web 服务？会加载磁盘上的最新代码，约 1~2 秒，当前页面会自动刷新。')) return;
    if (btn) { btn.disabled = true; btn.innerHTML = '重启中…'; }
    try {
        const data = await api('/api/system/restart', { method: 'POST' });
        if (data && data.ok) {
            showToast('服务重启中，稍后自动刷新…', 'info');
            // 给看门狗完成“杀旧+拉新”留足时间后再刷新
            setTimeout(() => location.reload(), 3000);
        } else {
            showToast('重启请求失败：' + ((data && data.msg) || '未知错误'), 'error');
            if (btn) { btn.disabled = false; btn.innerHTML = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg> 重启服务'; }
        }
    } catch (e) {
        showToast('重启请求异常：' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.innerHTML = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg> 重启服务'; }
    }
}

// 拉取服务状态（实时 PID / 运行时长），并在前端每秒更新，避免定成静态数字
let _systemStatusTimer = null;
let _systemStatusSyncTimer = null;

function _updateSystemBadge() {
    const badge = $('sys-status-badge');
    if (!badge || !window._sysStatus) return;
    const sec = Math.floor((Date.now() - window._sysStatus.startedAt) / 1000);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    badge.textContent = `PID ${window._sysStatus.pid} · 运行 ${m}分${s}秒`;
}

async function loadSystemStatus() {
    try {
        const data = await api('/api/system/status');
        if (data && data.ok) {
            window._sysStatus = {
                pid: data.pid,
                startedAt: new Date(data.started_at).getTime()
            };
            _updateSystemBadge();
            if (!_systemStatusTimer) {
                _systemStatusTimer = setInterval(_updateSystemBadge, 1000);
                // 每 30 秒与服务器同步一次，修正本地时钟漂移并检测服务端是否重启
                _systemStatusSyncTimer = setInterval(loadSystemStatus, 30000);
            }
        }
    } catch (e) { /* 忽略：状态非关键 */ }
}
