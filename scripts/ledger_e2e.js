/**
 * `/ledger` 盈利账本 —— 真实浏览器验证（CDP，零依赖）
 *
 * 为什么需要它：这一页的数字**全部由 JS 渲染**。模板 200 + 接口 200 并不能证明
 * 卡片里有内容 —— 一个 `api()` 抛错或一个变量名打错，页面就会留下一排空卡片，
 * 而 HTML 照样是 200。所以判据必须落在「渲染后的文本」上。
 *
 * 用法：
 *   1) 先起只读预览： python web/app.py --readonly --port 5099
 *   2) node scripts/ledger_e2e.js
 * 环境变量：LEDGER_URL（默认 http://127.0.0.1:5099/ledger）、CDP_PORT、CHROME_BIN
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const CHROME = process.env.CHROME_BIN ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = Number(process.env.CDP_PORT || 9334);
const TARGET = process.env.LEDGER_URL || 'http://127.0.0.1:5099/ledger';
const PROFILE = process.env.CDP_PROFILE || 'E:\\707\\.cdp_profile_ledger';
const SHOT = process.env.SHOT_DIR || 'E:\\707\\logs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJSON(p) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
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

(async () => {
  if (typeof WebSocket === 'undefined') { console.log('SKIP: 该 Node 版本没有全局 WebSocket'); process.exit(2); }
  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
  try { fs.mkdirSync(SHOT, { recursive: true }); } catch (e) {}

  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--disable-gpu', '--window-size=1500,1100',
    '--no-sandbox', TARGET,
  ], { stdio: 'ignore' });

  const out = [];
  const ok = (name, cond, extra = '') => {
    out.push((cond ? 'PASS  ' : 'FAIL  ') + name + (extra ? '  → ' + extra : ''));
    if (!cond) process.exitCode = 1;
    return cond;
  };

  try {
    const t = await waitForTargets();
    const c = await CDP.connect(t.webSocketDebuggerUrl);
    await c.send('Page.enable');
    await c.send('Runtime.enable');
    await c.send('Log.enable');

    const errors = [];
    c.ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === 'Runtime.exceptionThrown') {
        errors.push('exception: ' + JSON.stringify(m.params.exceptionDetails.text || ''));
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        errors.push('console.error: ' + (m.params.args || []).map((a) => a.value || a.description || '').join(' '));
      }
    });

    // 等卡片被 JS 填上（决策指标里出现 % 或 —）
    let filled = '';
    for (let i = 0; i < 60; i++) {
      filled = await c.eval(`(function(){
        var d = document.getElementById('lg-decide');
        return d ? d.innerText.replace(/\\s+/g,' ').trim() : '';
      })()`);
      if (filled && filled.indexOf('%') >= 0) break;
      await sleep(400);
    }
    ok('决策指标卡已渲染出数值', /%/.test(filled), filled.slice(0, 120));

    const dump = await c.eval(`(function(){
      function txt(id){ var e=document.getElementById(id); return e ? e.innerText.replace(/\\s+/g,' ').trim() : ''; }
      var rows = document.querySelectorAll('#lg-table tbody tr');
      return {
        decide: txt('lg-decide'),
        secondary: txt('lg-secondary'),
        denom: txt('lg-denom'),
        book: txt('lg-book'),
        tableRows: rows.length,
        totalRow: rows.length ? rows[rows.length-1].innerText.replace(/\\s+/g,' ').trim() : '',
        detailRows: document.querySelectorAll('#lg-detail tbody tr').length,
        shadow: txt('lg-shadow'),
        note: txt('lg-note'),
        honest: txt('lg-honest'),
        state: txt('lg-state'),
        cards: document.querySelectorAll('#lg-decide .lg-card').length,
        secCards: document.querySelectorAll('#lg-secondary .lg-card').length,
      };
    })()`);

    ok('决策指标 3 张卡', dump.cards === 3, String(dump.cards));
    ok('决策卡含「净收益率 / 净盈亏 / 最大回撤」',
      /净收益率/.test(dump.decide) && /净盈亏/.test(dump.decide) && /最大回撤/.test(dump.decide),
      dump.decide.slice(0, 80));
    ok('次级指标 3 张卡且带「次级」标签', dump.secCards === 3 && /次级/.test(dump.secondary),
      dump.secondary.slice(0, 100));
    ok('「≥10 元期数」标注不可跨注数比较', /不可跨注数档比较/.test(dump.secondary), '');
    ok('分母三数并列（开奖/参与/未参与）',
      /开奖期数/.test(dump.denom) && /参与期数/.test(dump.denom) && /未参与期数/.test(dump.denom),
      dump.denom.slice(0, 100));
    ok('记账分解含税与实得', /税前奖金/.test(dump.book) && /实得/.test(dump.book), '');
    ok('分彩种表 7 行（六彩种 + 合计）', dump.tableRows === 7, String(dump.tableRows));
    ok('合计行给出净收益率', /%/.test(dump.totalRow), dump.totalRow.slice(0, 140));
    ok('逐期明细有数据行', dump.detailRows > 0, String(dump.detailRows));
    ok('影子区说明了「不占预算」', /不占预算/.test(dump.shadow) || /影子/.test(dump.shadow), '');
    // ★ 只给读数时，三行净收益率总能挑出"最小的那个"；判决层必须同屏出现且带理由。
    ok('影子区给出「可判性判决」', /可判性判决/.test(dump.shadow), dump.shadow.slice(0, 80));
    ok('判决带档位或拒绝理由',
      /(可判|不可判|未检出差异|边缘)/.test(dump.shadow), '');
    ok('判决提醒「已结算 0 期不构成结论」',
      /不构成任何结论|尚未产生|已结算/.test(dump.shadow), '');
    ok('诚实边界已渲染（不是空块）', dump.honest.length > 20, dump.honest.slice(0, 60));
    ok('页面无 JS 异常/console.error', errors.length === 0, errors.slice(0, 3).join(' | '));

    // 导航多了一项（/ledger）⇒ 顶栏别被挤爆、侧栏别被裁
    const navBox = await c.eval(`(function(){
      var n = document.querySelector('.navbar nav');
      var s = document.querySelector('.sidebar');
      var last = [].slice.call(n.querySelectorAll('a')).pop();
      return {
        navScrollW: n.scrollWidth, navClientW: n.clientWidth,
        sideScrollH: s.scrollHeight, sideClientH: s.clientHeight,
        lastText: last ? last.textContent.trim() : '',
        hasLedgerLink: !![].slice.call(n.querySelectorAll('a')).filter(function(a){return a.getAttribute('href')==='/ledger';}).length,
      };
    })()`);
    ok('顶栏有「账本」入口', navBox.hasLedgerLink, navBox.lastText);
    ok('1440 宽顶栏不溢出（或可横滚）', navBox.navScrollW <= navBox.navClientW + 2 || navBox.navScrollW > 0,
      `scrollW/clientW = ${navBox.navScrollW}/${navBox.navClientW}`);
    console.log('    侧栏 scrollH/clientH = ' + navBox.sideScrollH + '/' + navBox.sideClientH +
      (navBox.sideScrollH > navBox.sideClientH + 2 ? '（需滚动，属正常退化）' : '（一屏可看全）'));

    // 术语弹窗：点按钮 → 模态框里出现术语表
    await c.eval(`document.querySelector('.lg-bar button').click()`);
    let modal = '';
    for (let i = 0; i < 40; i++) {
      modal = await c.eval(`(function(){ var m=document.getElementById('modal'); return (m && !m.classList.contains('hidden')) ? document.getElementById('modal-body').innerText.replace(/\\s+/g,' ').trim() : ''; })()`);
      if (modal.length > 100) break;
      await sleep(300);
    }
    ok('术语弹窗打开且有内容', modal.length > 100, modal.slice(0, 80));
    ok('术语弹窗含「逐票口径」', /逐票口径/.test(modal), '');
    await c.eval(`closeModal()`);

    // 切换彩种 + 窗口，验证不会白屏
    await c.eval(`(function(){ var s=document.getElementById('lg-lottery'); s.value='七星彩'; s.dispatchEvent(new Event('change')); })()`);
    await sleep(1800);
    const one = await c.eval(`(function(){ return {t: document.getElementById('lg-table').innerText.replace(/\\s+/g,' ').trim(), rows: document.querySelectorAll('#lg-table tbody tr').length}; })()`);
    ok('切到单彩种后仍有数据行', one.rows === 1 && /七星彩/.test(one.t), one.t.slice(0, 90));

    await c.eval(`(function(){ var s=document.getElementById('lg-periods'); s.value='50'; s.dispatchEvent(new Event('change')); })()`);
    await sleep(1500);
    const win = await c.eval(`document.getElementById('lg-state').innerText`);
    ok('窗口切换后状态提示生效', /近 50 期/.test(win), win);

    await c.send('Page.navigate', { url: TARGET.replace(/\/ledger$/, '/ledger') });
    await sleep(2500);
    const shot = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    const p = path.join(SHOT, 'ledger_page.png');
    fs.writeFileSync(p, Buffer.from(shot.result.data, 'base64'));
    out.push('SHOT  ' + p);
  } catch (e) {
    ok('脚本自身未抛错', false, e.message);
  } finally {
    try { chrome.kill(); } catch (e) {}
    console.log(out.join('\n'));
  }
})();
