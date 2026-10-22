// 导航一致性 + /market 内嵌页 —— 真实浏览器验证
// 目的：顶栏和侧栏是不是真的渲染成同一份清单、有没有溢出被裁、iframe 是否真加载出数据。
const puppeteer = require('puppeteer-core');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = 'http://127.0.0.1:5000';
const SHOT = 'E:\\707\\logs\\';

let pass = 0, fail = 0;
const fails = [];
const consoleErrors = [];
const badResponses = [];

function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; fails.push(name + (extra ? '  ' + extra : '')); console.log('  FAIL  ' + name + (extra ? '  ' + extra : '')); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu'],
    defaultViewport: { width: 1440, height: 1000 },
  });
  const page = await browser.newPage();
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', e => consoleErrors.push('pageerror: ' + e.message));
  page.on('response', r => {
    if (r.status() >= 400) badResponses.push(r.status() + ' ' + r.url());
  });

  // ---------- 1. 1440 宽：导航结构 ----------
  console.log('\n[1] 1440x1000 导航渲染');
  await page.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(500);

  const nav = await page.evaluate(() => {
    const navEl = document.querySelector('.navbar nav');
    const links = [...navEl.querySelectorAll('a')];
    const side = [...document.querySelectorAll('.sidebar a')];
    return {
      navHrefs: links.map(a => a.getAttribute('href')),
      navTexts: links.map(a => a.textContent.trim()),
      sideHrefs: side.map(a => a.getAttribute('href')),
      sideTexts: side.map(a => a.textContent.trim()),
      sections: [...document.querySelectorAll('.sidebar .section-title')].map(e => e.textContent.trim()),
      navScrollW: navEl.scrollWidth,
      navClientW: navEl.clientWidth,
      sidebarScrollable: (() => { const s = document.querySelector('.sidebar'); return s.scrollHeight > s.clientHeight; })(),
      sidebarH: document.querySelector('.sidebar').clientHeight,
      sidebarScrollH: document.querySelector('.sidebar').scrollHeight,
    };
  });

  ok('顶栏项数 = 侧栏项数', nav.navHrefs.length === nav.sideHrefs.length,
     `nav=${nav.navHrefs.length} side=${nav.sideHrefs.length}`);
  ok('顶栏与侧栏 href 顺序完全一致',
     JSON.stringify(nav.navHrefs) === JSON.stringify(nav.sideHrefs),
     '\n    nav =' + JSON.stringify(nav.navHrefs) + '\n    side=' + JSON.stringify(nav.sideHrefs));
  ok('侧栏含「鲁棒性检验」', nav.sideTexts.some(t => t.includes('鲁棒性')), nav.sideTexts.join('|'));
  ok('侧栏含「对比分析」', nav.sideTexts.some(t => t.includes('对比')), '');
  ok('顶栏含「命中」', nav.navTexts.some(t => t.includes('命中')), '');
  ok('顶栏含「数据体检」', nav.navTexts.some(t => t.includes('数据体检')), '');
  ok('两侧都有「开奖数据库」', nav.navTexts.some(t => t.includes('数据库')) && nav.sideTexts.some(t => t.includes('数据库')), '');
  ok('侧栏分成 3 组', nav.sections.length === 3, JSON.stringify(nav.sections));

  console.log('    顶栏 scrollW/clientW = ' + nav.navScrollW + '/' + nav.navClientW);
  ok('1440 宽顶栏不溢出（不靠横滚）', nav.navScrollW <= nav.navClientW + 2,
     `溢出 ${nav.navScrollW - nav.navClientW}px`);
  console.log('    侧栏 scrollH/clientH = ' + nav.sidebarScrollH + '/' + nav.sidebarH);
  ok('1440 高侧栏无需滚动即可看全', nav.sidebarScrollH <= nav.sidebarH + 2,
     `超出 ${nav.sidebarScrollH - nav.sidebarH}px`);

  await page.screenshot({ path: SHOT + 'nav_1440.png' });

  // 导航变多后「重启服务」按钮曾被挤成两行（"重启服" / "务"），单独锁一下
  const restartBtn = await page.evaluate(() => {
    const b = document.getElementById('btn-restart');
    const r = b.getBoundingClientRect();
    return { h: Math.round(r.height), w: Math.round(r.width), text: b.textContent.trim() };
  });
  console.log('    重启按钮 ' + restartBtn.w + 'x' + restartBtn.h);
  ok('「重启服务」按钮保持单行未被挤压', restartBtn.h <= 34 && restartBtn.w >= 80,
     `${restartBtn.w}x${restartBtn.h}`);

  // ---------- 2. 1280 宽：顶栏溢出必须仍可滚动到最后一个 ----------
  console.log('\n[2] 1280x800 窄屏');
  await page.setViewport({ width: 1280, height: 800 });
  await sleep(400);
  const narrow = await page.evaluate(() => {
    const navEl = document.querySelector('.navbar nav');
    const last = [...navEl.querySelectorAll('a')].pop();
    const r = last.getBoundingClientRect();
    const nb = navEl.getBoundingClientRect();
    // 滚到底，看最后一项是否进入可视区
    navEl.scrollLeft = navEl.scrollWidth;
    const r2 = last.getBoundingClientRect();
    return {
      overflow: navEl.scrollWidth - navEl.clientWidth,
      lastBeforeRight: r.right, lastAfterRight: r2.right, boxRight: nb.right,
      canScroll: navEl.scrollWidth > navEl.clientWidth,
      scrollBehavior: getComputedStyle(navEl).overflowX,
    };
  });
  console.log('    溢出 ' + narrow.overflow + 'px, overflowX=' + narrow.scrollBehavior);
  if (narrow.canScroll) {
    ok('窄屏顶栏可横向滚动，最后一项能滚进可视区',
       narrow.lastAfterRight <= narrow.boxRight + 2,
       `last.right=${Math.round(narrow.lastAfterRight)} box.right=${Math.round(narrow.boxRight)}`);
  } else {
    ok('窄屏顶栏未溢出，无需滚动', true, '');
  }
  await page.screenshot({ path: SHOT + 'nav_1280.png' });

  // ---------- 3. /market 内嵌页 ----------
  console.log('\n[3] /market 内嵌数据浏览器');
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(BASE + '/market', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);

  const badge = await page.$eval('#mk-status', el => el.textContent.trim());
  console.log('    状态徽章: ' + badge);
  ok('状态徽章显示在线', /在线/.test(badge), badge);

  // 找 iframe 里的 frame（跨端口，靠 CDP 拿句柄）
  let dataFrame = null;
  for (let i = 0; i < 25; i++) {
    dataFrame = page.frames().find(f => f.url().includes(':5001'));
    if (dataFrame) break;
    await sleep(300);
  }
  ok('iframe 已加载 5001 页面', !!dataFrame, dataFrame ? dataFrame.url() : '(none)');

  if (dataFrame) {
    let rowCount = 0;
    for (let i = 0; i < 30; i++) {
      rowCount = await dataFrame.evaluate(() =>
        document.querySelectorAll('tbody tr[data-lot]').length).catch(() => 0);
      if (rowCount > 0) break;
      await sleep(300);
    }
    ok('iframe 内表格有数据行', rowCount > 0, 'rows=' + rowCount);

    const embed = await dataFrame.evaluate(() => ({
      bodyClass: document.body.className,
      h1Hidden: (() => {
        const h = document.querySelector('.brand h1');
        return !h || getComputedStyle(h).display === 'none';
      })(),
      hasChips: document.querySelectorAll('#lotChips .chip, #lotChips button, #lotChips a').length,
      hasSearch: !!document.querySelector('#q'),
    }));
    ok('内嵌模式已生效（body.embed）', embed.bodyClass.includes('embed'), embed.bodyClass);
    ok('内嵌模式隐藏重复标题 h1', embed.h1Hidden, '');
    ok('彩种 chips 仍在', embed.hasChips >= 8, 'chips=' + embed.hasChips);
    ok('搜索框仍在', embed.hasSearch, '');
  }

  const layout = await page.evaluate(() => {
    const box = document.getElementById('mk-frame-box');
    const r = box.getBoundingClientRect();
    const fr = document.getElementById('mk-frame');
    const offline = document.getElementById('mk-offline');
    return {
      boxH: r.height, boxW: r.width,
      frameH: fr.getBoundingClientRect().height,
      offlineHidden: offline.classList.contains('hidden'),
      docScrollH: document.documentElement.scrollHeight,
      winH: window.innerHeight,
    };
  });
  console.log('    外框 ' + Math.round(layout.boxW) + 'x' + Math.round(layout.boxH) + ', iframe 高 ' + Math.round(layout.frameH));
  // 外框有 1px 边框，iframe 的 100% 是内容高，两者天然差 2px
  ok('iframe 撑满外框高度', Math.abs(layout.boxH - layout.frameH) <= 4, `${layout.boxH} vs ${layout.frameH}`);
  ok('外框高度合理（>500px）', layout.boxH > 500, layout.boxH);
  ok('在线时离线兜底层已隐藏', layout.offlineHidden, '');

  // 更新数据的入口与状态文案（不真点：一次全量要 3~5 分钟）
  const upd = await page.evaluate(() => {
    const b = document.getElementById('mk-btn-update');
    return {
      hasBtn: !!b,
      label: b ? b.textContent.trim() : '',
      disabled: b ? b.disabled : null,
      meta: (document.getElementById('mk-meta') || {}).textContent || '',
    };
  });
  ok('工具栏有「更新数据」按钮', upd.hasBtn, '');
  ok('更新按钮可点（未卡在禁用态）', upd.disabled === false, String(upd.disabled));
  ok('状态栏显示补库时间或计划', /上次补库|从未补库/.test(upd.meta), upd.meta);

  // 接口层：状态里必须带 update 段（页面轮询就靠它）
  const st = await page.evaluate(() => fetch('/api/market/status').then(r => r.json()));
  ok('/api/market/status 带 update 段', !!(st.update && st.update.schedule), JSON.stringify(st.update || {}));

  await page.screenshot({ path: SHOT + 'nav_market.png', fullPage: false });

  // ---------- 4. 侧栏点击导航是否真的跳转 ----------
  console.log('\n[4] 侧栏点击导航');
  await page.goto(BASE + '/', { waitUntil: 'networkidle2' });
  await sleep(300);
  const target = await page.evaluate(() => {
    const a = [...document.querySelectorAll('.sidebar a')].find(x => x.getAttribute('href') === '/robustness');
    if (!a) return null;
    a.click();
    return a.getAttribute('href');
  });
  ok('侧栏能点到 /robustness', target === '/robustness', String(target));
  await sleep(1200);
  ok('确实跳转到 /robustness', page.url().includes('/robustness'), page.url());

  const activeState = await page.evaluate(() => ({
    navActive: [...document.querySelectorAll('.navbar nav a.active')].map(a => a.getAttribute('href')),
    sideActive: [...document.querySelectorAll('.sidebar a.active')].map(a => a.getAttribute('href')),
  }));
  ok('顶栏高亮当前页', activeState.navActive.includes('/robustness'), JSON.stringify(activeState.navActive));
  ok('侧栏高亮当前页', activeState.sideActive.includes('/robustness'), JSON.stringify(activeState.sideActive));

  // ---------- 汇总 ----------
  console.log('\n控制台错误 ' + consoleErrors.length + ' 条');
  consoleErrors.slice(0, 6).forEach(e => console.log('    ! ' + e));
  ok('控制台无 JS 错误', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '));

  // 资源 404 单独看：favicon 之类不影响功能，但要留痕以便判断
  console.log('HTTP 4xx/5xx ' + badResponses.length + ' 条');
  [...new Set(badResponses)].slice(0, 8).forEach(u => console.log('    ! ' + u));

  console.log('\n===== 通过 ' + pass + '，失败 ' + fail + ' =====');
  if (fail) fails.forEach(f => console.log('  ✗ ' + f));

  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => {
  console.error('FATAL', e);
  process.exit(2);
});
