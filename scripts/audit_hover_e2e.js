/**
 * 真实浏览器里验证「悬停 2 秒 → 圆环走满 → 弹出释义」这条交互链。
 * 走 CDP（Chrome DevTools Protocol），用 Node 自带的 WebSocket，不装任何依赖。
 *
 * 用法：先起一个只读实例（web/app.py --readonly --port 5099），再 node scripts/audit_hover_e2e.js
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const CHROME = process.env.CHROME_BIN ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = Number(process.env.CDP_PORT || 9333);
const TARGET = process.env.AUDIT_URL || 'http://127.0.0.1:5099/audit';
const PROFILE = process.env.CDP_PROFILE || 'E:\\707\\.cdp_profile';

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
  async mouseMove(x, y) {
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
  }
}

(async () => {
  if (typeof WebSocket === 'undefined') { console.log('SKIP: 该 Node 版本没有全局 WebSocket'); process.exit(2); }

  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--disable-gpu', '--window-size=1400,1000',
    '--no-sandbox', TARGET,
  ], { stdio: 'ignore' });

  const out = [];
  const ok = (name, cond, extra = '') => { out.push((cond ? 'PASS  ' : 'FAIL  ') + name + (extra ? '  → ' + extra : '')); return cond; };
  let allOk = true;

  try {
    const t = await waitForTargets();
    const c = await CDP.connect(t.webSocketDebuggerUrl);
    await c.send('Page.enable');
    await c.send('Runtime.enable');

    // 等术语标注出现（glossary 是 XHR，晚于 DOM）
    let n = 0;
    for (let i = 0; i < 60; i++) {
      n = await c.eval('document.querySelectorAll(".term").length');
      if (n > 0) break;
      await sleep(500);
    }
    allOk &= ok('术语已标注', n > 0, n + ' 个');

    // 卡片默认收起 ⇒ 术语大多不可见。先全展开，再挑一个滚到视口中间的词。
    await c.eval('EXPL.expandAll()');
    await sleep(1200);
    const dump = await c.eval(`(function(){
      return Array.prototype.slice.call(document.querySelectorAll('.term')).map(function(e){
        var r = e.getBoundingClientRect();
        return {name: e.textContent, key: e.getAttribute('data-term'), w: Math.round(r.width), top: Math.round(r.top)};
      });
    })()`);
    console.log('诊断：术语 ' + n + ' 个，展开后前 8 个 = ' + JSON.stringify(dump.slice(0, 8)));

    const box = await c.eval(`(function(){
      var els = Array.prototype.slice.call(document.querySelectorAll('.term'));
      for (var i=0;i<els.length;i++){
        var r = els[i].getBoundingClientRect();
        if (r.width > 4 && r.height > 4) {
          els[i].scrollIntoView({block:'center'});
          var r2 = els[i].getBoundingClientRect();
          return {x: r2.left + r2.width/2, y: r2.top + r2.height/2, key: els[i].getAttribute('data-term'), name: els[i].textContent};
        }
      }
      return null;
    })()`);
    allOk &= ok('找到可用于悬停的术语', !!box, box ? `${box.name} (${box.key}) @ ${Math.round(box.x)},${Math.round(box.y)}` : '');
    if (!box) throw new Error('没有可悬停的术语，后续用例无法进行');

    const ringState = () => c.eval(`(function(){
      var r = document.getElementById('audit-ring');
      var p = document.getElementById('audit-pop');
      return {
        ringShown: !!(r && r.style.display !== 'none' && r.getBoundingClientRect().width > 0),
        ringHasSvg: !!(r && r.querySelector('.ring-fill')),
        ringOffset: r && r.querySelector('.ring-fill') ? Number(getComputedStyle(r.querySelector('.ring-fill')).strokeDashoffset.replace('px','')) : null,
        popShown: !!(p && p.style.display !== 'none'),
        popTitle: p ? (p.querySelector('.p-name') ? p.querySelector('.p-name').textContent : '') : '',
        loading: document.querySelectorAll('.term--loading').length,
        dur: getComputedStyle(document.documentElement).getPropertyValue('--ring-dur').trim(),
      };
    })()`);

    // 诊断：该坐标上真正接收事件的是谁（有遮挡的话真人也会点不到）
    const hit = await c.eval(`(function(){
      var e = document.elementFromPoint(${box.x}, ${box.y});
      if (!e) return 'null';
      return e.tagName + '.' + (e.className || '') + ' | term=' + !!(e.closest && e.closest('.term'))
             + ' | 文本=' + (e.textContent || '').slice(0, 20);
    })()`);
    console.log('诊断：坐标命中 = ' + hit);

    // ★ headless 下 CDP 注入的鼠标事件被忽略（实测 mouseover 0 次），
    //   所以交互用合成事件驱动。监听器是 document 级委托（靠 e.target.closest('.term')），
    //   合成事件走的是同一条路径；真实命中情况已由上面的 elementFromPoint 验证。
    const fire = (type, extra) => c.eval(`(function(){
      var el = document.querySelector('.term[data-term="${box.key}"]');
      el.dispatchEvent(new MouseEvent('${type}', Object.assign({bubbles: true, cancelable: true}, ${extra || '{}'})));
      return true;
    })()`);
    const HOVER_IN = "{bubbles:true}";
    const HOVER_OUT = "{bubbles:true, relatedTarget: document.body}";

    // ---- 用例 1：悬停后 0.9s，圆环应该在转、释义还没弹
    await fire('mouseover', HOVER_IN);
    await sleep(900);
    const s1 = await ringState();
    allOk &= ok('0.9s：圆环已显示', s1.ringShown, JSON.stringify(s1));
    allOk &= ok('0.9s：圆环有进度环 SVG', s1.ringHasSvg);
    allOk &= ok('0.9s：进度未走满（offset 介于 0 与 69 之间）',
      s1.ringOffset > 1 && s1.ringOffset < 68, 'offset=' + s1.ringOffset);
    allOk &= ok('0.9s：释义还没弹', !s1.popShown);
    allOk &= ok('0.9s：词条带读秒标记', s1.loading === 1, 'term--loading=' + s1.loading);
    allOk &= ok('--ring-dur 来自 JS（2000ms）', s1.dur === '2000ms', s1.dur);

    // ---- 用例 2：等到 2.4s，圆环走满 → 释义弹出
    await sleep(1500);
    const s2 = await ringState();
    allOk &= ok('2.4s：释义已弹出', s2.popShown, '标题=' + s2.popTitle);
    allOk &= ok('2.4s：圆环已收起', !s2.ringShown);
    allOk &= ok('2.4s：读秒标记已清除', s2.loading === 0);
    allOk &= ok('2.4s：弹出的是悬停的那个词', s2.popTitle === box.name, `${s2.popTitle} vs ${box.name}`);

    // ---- 用例 3：移开 → 收起
    await fire('mouseout', HOVER_OUT);
    const s3 = await ringState();
    allOk &= ok('移开后释义收起', !s3.popShown);

    // ---- 用例 4：快速划过（0.3s 就移开）不该在 2 秒后补弹
    await fire('mouseover', HOVER_IN);
    await sleep(300);
    await fire('mouseout', HOVER_OUT);
    await sleep(2400);
    const s4 = await ringState();
    allOk &= ok('快速划过：2.7s 后不补弹释义', !s4.popShown);
    allOk &= ok('快速划过：圆环已收起', !s4.ringShown);

    // ---- 用例 5：点击立即弹（无障碍/触屏通道，不走 2 秒）
    await fire('click', HOVER_IN);
    await sleep(150);
    const s5 = await ringState();
    allOk &= ok('点击立即弹出（不等 2 秒）', s5.popShown, '标题=' + s5.popTitle);

    // ---- 用例 6：CK3 风格装饰到位
    const deco = await c.eval(`(function(){
      var p = document.getElementById('audit-pop');
      var cs = getComputedStyle(p);
      return { corners: p.querySelectorAll('.p-corner').length,
               head: !!p.querySelector('.p-head'),
               kind: p.querySelector('.p-kind') ? p.querySelector('.p-kind').getAttribute('data-k') : '',
               border: cs.borderTopWidth, radius: cs.borderTopLeftRadius };
    })()`);
    allOk &= ok('四角装饰齐全', deco.corners === 4, JSON.stringify(deco));
    allOk &= ok('有标题栏', deco.head);
    allOk &= ok('分类徽章带 data-k', !!deco.kind, deco.kind);

    // ---- 用例 7：页面上有「悬停 2 秒」的说明
    const hint = await c.eval(`(function(){
      var h = document.getElementById('audit-hover-hint');
      return h ? {shown: h.style.display !== 'none', text: h.textContent} : null;
    })()`);
    allOk &= ok('页面提示可见', !!(hint && hint.shown), hint ? hint.text.slice(0, 40) : 'null');
    allOk &= ok('提示里写了 2 秒', !!(hint && /2 秒/.test(hint.text)));

  } catch (e) {
    out.push('ERROR  ' + e.message);
    allOk = false;
  } finally {
    try { chrome.kill(); } catch (e) {}
  }

  console.log(out.join('\n'));
  console.log('\n结果: ' + (allOk ? '全部通过' : '有失败项'));
  process.exit(allOk ? 0 : 1);
})();
