/**
 * 真实浏览器里验证 /hits 的「命中条件」下拉：分「通用」与「彩种专属」两组，
 * 切彩种时选项跟着变、不适用的旧条件回落「不限」、含义说明随选中出现。
 * 走 CDP（Chrome DevTools Protocol），用 Node 自带 WebSocket，不装任何依赖。
 *
 * 用法：先起一个只读实例（web/app.py --readonly --port 5099），
 *      再 node scripts/hits_cond_e2e.js
 *
 * ★ headless 下 CDP 注入的鼠标事件会被浏览器忽略（实测 mouseover 0 次），
 *   所以这里不点鼠标，直接调页面函数 / 设置 select.value 再派发 change。
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const CHROME = process.env.CHROME_BIN ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = Number(process.env.CDP_PORT || 9334);
const BASE = process.env.HITS_URL || 'http://127.0.0.1:5099';
const PROFILE = process.env.CDP_PROFILE || 'E:\\707\\.cdp_profile_hits';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJSON(path) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path }, (r) => {
      let b = '';
      r.on('data', (c) => (b += c));
      r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    }).on('error', rej);
  });
}

async function waitForTargets() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await getJSON('/json/list');
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (e) { /* 还没起来 */ }
    await sleep(500);
  }
  throw new Error('Chrome 调试端口没起来');
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && c.pending.has(m.id)) { c.pending.get(m.id)(m); c.pending.delete(m.id); }
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res) => this.pending.set(id, res));
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return (r.result && r.result.result && r.result.result.value);
  }
}

// 读取下拉现状：各组名字与选项标签
const READ_SEL = `(function(){
  var sel = document.getElementById('hits-cond');
  if (!sel) return null;
  var groups = [];
  for (var i = 0; i < sel.children.length; i++) {
    var n = sel.children[i];
    if (n.tagName === 'OPTGROUP') {
      groups.push({ label: n.label, items: Array.prototype.map.call(n.children, function(o){ return o.textContent.trim(); }) });
    }
  }
  return {
    value: sel.value,
    plain: Array.prototype.filter.call(sel.children, function(c){ return c.tagName === 'OPTION'; }).map(function(o){ return o.textContent.trim(); }),
    groups: groups,
    note: (document.getElementById('hits-cond-note') || {}).textContent || '',
    noteShown: !!(document.getElementById('hits-cond-note') && document.getElementById('hits-cond-note').style.display !== 'none'),
    matched: (document.getElementById('hits-count-match') || {}).textContent || '',
    stat: (document.getElementById('hits-stat') || {}).textContent || ''
  };
})()`;

async function setCond(c, value) {
  return c.eval(`(function(){
    var sel = document.getElementById('hits-cond');
    sel.value = ${JSON.stringify(value)};
    sel.dispatchEvent(new Event('change'));
    loadHits();
    return sel.value;
  })()`);
}

async function pickLottery(c, name, wait = 1500) {
  await c.eval(`(async function(){
    var sel = document.getElementById('hits-lottery');
    sel.value = ${JSON.stringify(name)};
    await _onHitsLotteryChange();
    return 1;
  })()`);
  await sleep(wait);
}

async function goto(c, url, wait = 2200) {
  await c.send('Page.navigate', { url });
  await sleep(wait);
}

(async () => {
  if (typeof WebSocket === 'undefined') { console.log('SKIP: 该 Node 版本没有全局 WebSocket'); process.exit(2); }
  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}

  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--disable-gpu', '--window-size=1400,1000',
    '--no-sandbox', `${BASE}/hits`,
  ], { stdio: 'ignore' });

  const out = [];
  const ok = (name, cond, extra = '') => { out.push((cond ? 'PASS  ' : 'FAIL  ') + name + (extra ? '  → ' + extra : '')); return cond; };
  let allOk = true;

  try {
    const page = await waitForTargets();
    const c = await CDP.connect(page.webSocketDebuggerUrl);
    await c.send('Page.enable');
    await c.send('Runtime.enable');
    await sleep(2500);

    // ---- 用例 1：默认「全部彩票」⇒ 只有「通用」组，且只有 7 档 ----
    let s = await c.eval(READ_SEL);
    allOk &= ok('默认：下拉存在', !!s);
    allOk &= ok('默认：只有一个分组（通用）', s && s.groups.length === 1, s && s.groups.map(g => g.label).join(' | '));
    allOk &= ok('默认：通用组 7 档（≥1…≥7）', s && s.groups[0] && s.groups[0].items.length === 7, s && s.groups[0] && s.groups[0].items.join(','));
    allOk &= ok('默认：有「不限」占位项', s && s.plain.some(t => t.indexOf('不限') >= 0), s && s.plain.join(','));

    // ---- 用例 2：切到双色球 ⇒ 出现「双色球 · 按分区」专属组 ----
    await pickLottery(c, '双色球');
    s = await c.eval(READ_SEL);
    allOk &= ok('双色球：两组（通用 + 专属）', s && s.groups.length === 2, s && s.groups.map(g => g.label).join(' | '));
    allOk &= ok('双色球：专属组标题点名彩种', s && s.groups[1] && s.groups[1].label.indexOf('双色球') >= 0, s && s.groups[1] && s.groups[1].label);
    allOk &= ok('双色球：专属含「红球 ≥ 4 且蓝球命中」', s && s.groups[1] && s.groups[1].items.some(t => t.indexOf('红球 ≥ 4 且蓝球命中') >= 0));

    // ---- 用例 3：选专属档 ⇒ 列表真的按该条件筛，且含义说明出现 ----
    await setCond(c, '红球:4,蓝球:1');
    await sleep(1600);
    s = await c.eval(READ_SEL);
    allOk &= ok('选「红球≥4 且蓝球命中」：选中值保留', s && s.value === '红球:4,蓝球:1', s && s.value);
    allOk &= ok('含义说明出现且含「四等奖」', s && s.noteShown && s.note.indexOf('四等奖') >= 0, s && s.note);
    allOk &= ok('列表筛出 1 注（真实数据 4+1 只有 1 条）', s && s.matched.trim() === '1', s && s.matched);

    // ---- 用例 4：切到 3D ⇒ 通用档只有 3 档（不再出现「≥7」这种假选项）----
    await pickLottery(c, '福彩3D');
    s = await c.eval(READ_SEL);
    const g3 = s && s.groups[0] ? s.groups[0].items : [];
    allOk &= ok('3D：通用组只有 3 档', g3.length === 3, g3.join(' | '));
    allOk &= ok('3D：不再出现「总命中 ≥ 7」', !g3.some(t => t.indexOf('≥ 7') >= 0), g3.join(' | '));
    allOk &= ok('3D：用「百位/十位/个位」而非「第1位」作标签',
      s && s.groups[1] && s.groups[1].items.some(t => t.indexOf('百位') >= 0), s && s.groups[1] && s.groups[1].items.join(','));
    allOk &= ok('切彩种后旧条件（红球≥4）已回落「不限」', s && s.value === '', s && s.value);

    // ---- 用例 5：七星彩 ⇒ 用官方 X/Y 记法的「第 7 位（后区）」 ----
    await pickLottery(c, '七星彩');
    s = await c.eval(READ_SEL);
    const gq = s && s.groups[1] ? s.groups[1].items : [];
    allOk &= ok('七星彩：专属含「第 7 位（后区）命中」', gq.some(t => t.indexOf('第 7 位') >= 0 && t.indexOf('后区') >= 0), gq.join(' | '));
    allOk &= ok('七星彩：专属含「前 6 位命中 ≥ 3」', gq.some(t => t.indexOf('前 6 位') >= 0), gq.join(' | '));

    // ---- 用例 6：深链恢复（?lottery=七星彩&zones=后区:1）----
    await goto(c, `${BASE}/hits?lottery=${encodeURIComponent('七星彩')}&zones=${encodeURIComponent('后区:1')}`);
    await sleep(1200);
    s = await c.eval(READ_SEL);
    allOk &= ok('深链：条件被恢复成「后区:1」', s && s.value === '后区:1', s && s.value);
    allOk &= ok('深链：筛出 95 注（= 第7位命中的中奖记录）', s && s.matched.trim() === '95', s && s.matched);
    allOk &= ok('深链：含义说明也跟着出来', s && s.noteShown, s && s.note.slice(0, 40));

    // ---- 用例 7：旧的 red_hits 链接仍能映射（向后兼容）----
    await goto(c, `${BASE}/hits?lottery=${encodeURIComponent('双色球')}&red_hits=4`);
    await sleep(1200);
    s = await c.eval(READ_SEL);
    allOk &= ok('老链接 red_hits=4 → 映射成「红球:4」', s && s.value === '红球:4', s && s.value);
    allOk &= ok('老链接：筛出 10 注（与后端直查一致）', s && s.matched.trim() === '10', s && s.matched);

    // ---- 用例 8：非法/不适用条件 ⇒ 明确提示，而不是静默筛成「全部」----
    await goto(c, `${BASE}/hits?lottery=${encodeURIComponent('全部')}&zones=${encodeURIComponent('红球:4')}`);
    await sleep(1500);
    s = await c.eval(READ_SEL);
    allOk &= ok('不适用条件：下拉回落「不限」', s && s.value === '', s && s.value);
    allOk &= ok('不适用条件：明确提示「已清除」而非静默',
      s && s.noteShown && s.note.indexOf('不适用') >= 0 && s.note.indexOf('红球:4') >= 0, s && s.note);
    allOk &= ok('不适用条件：提示指向「先选单个彩种」', s && s.note.indexOf('选定单个彩种') >= 0, s && s.note);
  } catch (e) {
    allOk = false;
    out.push('ERROR ' + (e && e.message ? e.message : e));
  } finally {
    try { chrome.kill(); } catch (e) {}
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
  }

  console.log(out.join('\n'));
  console.log('\n' + (allOk ? '全部通过' : '存在失败项') + '（' + out.filter(l => l.startsWith('PASS')).length + '/' + out.length + '）');
  process.exit(allOk ? 0 : 1);
})();
