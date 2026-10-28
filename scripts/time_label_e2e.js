/**
 * 真实浏览器里验证「时间字段语义」修复（本轮事故：
 * 把 `预测日期` 当成预测时刻显示，导致「预测于 10-27 · 开奖 10-27」）。
 *
 * 走 CDP，用 Node 自带 WebSocket，不装依赖。
 *
 * 用法：先确保有实例在跑（默认读 5000，仅 GET，不写数据）
 *       node scripts/time_label_e2e.js
 *   自定义：BASE=http://127.0.0.1:5099 node scripts/time_label_e2e.js
 *
 * 验三件事：
 *   1. /hits 命中详情展开后，显示「出号时刻」而不是「预测日期」；
 *   2. 对比卡片在**有**出号时刻时显示它、**没有**时明说「未记录」（不拿日期顶替）；
 *   3. 全站不再出现「预测于 X」这种把目标开奖日当预测时刻的写法。
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const CHROME = process.env.CHROME_BIN ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = Number(process.env.CDP_PORT || 9337);
const BASE = process.env.BASE || 'http://127.0.0.1:5000';
const PROFILE = process.env.CDP_PROFILE || 'E:\\707\\.cdp_profile_timelabel';

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
    const r = await this.send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.result && r.result.exceptionDetails) {
      throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400));
    }
    return (r.result && r.result.result && r.result.result.value);
  }
}

async function goto(c, url, wait = 2500) {
  await c.send('Page.navigate', { url });
  await sleep(wait);
}

// 展开 /hits 第一条并取「详情行」的文本
const READ_FIRST_DETAIL = `(function(){
  var items = document.querySelectorAll('#hits-result .hit-item');
  if (!items.length) return null;
  var head = items[0].querySelector('.hit-header');
  if (head) head.click();
  var body = items[0].querySelector('.hit-body');
  var detail = items[0].querySelector('.hit-detail');
  return {
    count: items.length,
    detail: detail ? detail.textContent.replace(/\\s+/g, ' ').trim() : '',
    expanded: body ? body.style.display : ''
  };
})()`;

// 直接调渲染函数，喂构造数据 —— 比等接口、点页面稳，且能覆盖「无出号时刻」分支
const CARD_HTML = (payload) => `(function(){
  try { return _renderFbCompareCard(${JSON.stringify(payload)}); }
  catch (e) { return 'ERR:' + e.message; }
})()`;

const CARD_BASE = {
  期号: 26123, 策略: '高频策略', 中奖等级: '未中', valid_prediction: true,
  预测日期: '2026-10-27', 开奖日期: '2026-10-27',
  预测红球: [1, 2, 14, 16, 20, 32], 预测蓝球: [15],
  实际红球: [4, 5, 20, 21, 26, 30], 实际蓝球: [12],
  红球命中: 1, 蓝球命中: 0, 总命中: 1,
  view: { zones: [], total_hit: 1, total_choose: 7, is_redblue: true },
};

(async () => {
  if (typeof WebSocket === 'undefined') { console.log('SKIP: 该 Node 版本没有全局 WebSocket'); process.exit(2); }
  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}

  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--disable-gpu', '--window-size=1400,1000',
    '--no-sandbox', `${BASE}/hits`,
  ], { stdio: 'ignore' });

  const out = [];
  let allOk = true;
  const ok = (name, cond, extra = '') => {
    out.push((cond ? 'PASS  ' : 'FAIL  ') + name + (extra ? '  → ' + extra : ''));
    if (!cond) allOk = false;
    return cond;
  };

  let c;
  try {
    const target = await waitForTargets();
    c = await CDP.connect(target.webSocketDebuggerUrl);
    await c.send('Page.enable');
    await c.send('Runtime.enable');

    // ---------- 1. /hits 命中详情 ----------
    await goto(c, `${BASE}/hits`, 3000);
    // 等首屏记录加载（最多 8 秒）
    for (let i = 0; i < 16; i++) {
      const n = await c.eval(`document.querySelectorAll('#hits-result .hit-item').length`);
      if (n > 0) break;
      await sleep(500);
    }
    const d = await c.eval(READ_FIRST_DETAIL);
    ok('命中列表渲染出记录', !!(d && d.count > 0), d ? `${d.count} 条` : 'null');
    if (d && d.detail) {
      ok('详情含「出号时刻」', d.detail.includes('出号时刻'), d.detail.slice(0, 160));
      ok('详情不再出现「预测日期」', !d.detail.includes('预测日期'));
      const hasTime = /\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(d.detail);
      const saysMissing = d.detail.includes('未记录');
      ok('出号时刻要么是时间、要么明说「未记录」', hasTime || saysMissing,
        hasTime ? '有具体时间' : (saysMissing ? '显示未记录' : '两者皆无'));
      ok('详情保留了「开奖日期」', d.detail.includes('开奖日期'));
    } else {
      ok('详情含「出号时刻」', false, '没能展开第一条');
    }

    // ---------- 2. 对比卡片两个分支 ----------
    const withGen = await c.eval(CARD_HTML(Object.assign({}, CARD_BASE, {
      批次时间: '2026-10-27T01:10:54.749939',
    })));
    ok('卡片(有出号时刻)不出现「预测于」', !String(withGen).includes('预测于'),
      String(withGen).slice(0, 120));
    ok('卡片(有出号时刻)显示真实时刻', /出号\s*2026-10-27\s*01:10/.test(String(withGen)));

    const noGen = await c.eval(CARD_HTML(Object.assign({}, CARD_BASE, {
      批次时间: '',
    })));
    ok('卡片(无出号时刻)不出现「预测于」', !String(noGen).includes('预测于'));
    ok('卡片(无出号时刻)明说「未记录」', String(noGen).includes('出号时刻未记录'),
      String(noGen).slice(0, 120));
    ok('卡片(无出号时刻)不拿预测日期冒充时刻',
      !/出号\s*2026-10-27\b/.test(String(noGen)));

    // ---------- 3. 页面级兜底扫描 ----------
    const pageText = await c.eval(`document.body.innerText.replace(/\\s+/g,' ')`);
    ok('整页不出现「预测于」', !String(pageText).includes('预测于'));

    await goto(c, `${BASE}/feedback`, 2500);
    const fbText = await c.eval(`document.body.innerText.replace(/\\s+/g,' ')`);
    ok('/feedback 页不出现「预测于」', !String(fbText).includes('预测于'));
  } catch (e) {
    ok('脚本未抛异常', false, String(e).slice(0, 200));
  } finally {
    try { chrome.kill(); } catch (e) {}
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
  }

  console.log(out.join('\n'));
  const fails = out.filter((s) => s.startsWith('FAIL')).length;
  console.log(`\n${out.length - fails}/${out.length} 通过` + (fails ? `，${fails} 项失败` : ''));
  process.exit(fails ? 1 : 0);
})();
