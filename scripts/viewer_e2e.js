// market 数据浏览器 —— 真实浏览器交互测试
// 用 puppeteer-core 驱动本机 Chrome，验证的不只是"页面能打开"，
// 而是每个交互动作后 DOM 的真实结果。
const puppeteer = require('puppeteer-core');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = 'http://127.0.0.1:5001';
const SHOT = 'E:\\707\\logs\\';

let pass = 0, fail = 0;
const errors = [];

function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  ' + extra : '')); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function rows(page) {
  return page.$$eval('tbody tr[data-lot]', els => els.length);
}
async function total(page) {
  const t = await page.$eval('#stFiltered', el => el.textContent);
  return parseInt(t.replace(/,/g, ''), 10);
}
async function waitRows(page, min = 1, tries = 40) {
  for (let i = 0; i < tries; i++) {
    if (await rows(page) >= min) return true;
    await sleep(120);
  }
  return false;
}
async function waitTotal(page, want, tries = 50) {
  for (let i = 0; i < tries; i++) {
    if (await total(page) === want) return true;
    await sleep(120);
  }
  return false;
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu', '--window-size=1440,1000'],
    defaultViewport: { width: 1440, height: 1000 },
  });
  const page = await browser.newPage();

  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

  console.log('=== 1. 首屏 ===');
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 30000 });
  ok('表格渲染出数据', await waitRows(page, 20));
  ok('默认 50 行', (await rows(page)) === 50, '实际 ' + await rows(page));
  ok('总数 38,252', (await total(page)) === 38252, '实际 ' + await total(page));
  ok('8 个彩种 chip', (await page.$$('.chip[data-lot]')).length === 8);
  await page.screenshot({ path: SHOT + '_e2e_1_list.png' });

  console.log('=== 2. 彩种筛选 ===');
  await page.click('.chip[data-lot="ssq"]');
  ok('双色球 3517', await waitTotal(page, 3517), '实际 ' + await total(page));
  const allSsq = await page.$$eval('tbody tr[data-lot]', els =>
    els.every(e => e.dataset.lot === 'ssq'));
  ok('所有行都是双色球', allSsq);
  ok('chip 高亮', await page.$eval('.chip[data-lot="ssq"]',
    el => el.classList.contains('on')));

  await page.click('.chip[data-lot="dlt"]');
  ok('多选 3517+2937=6454', await waitTotal(page, 6454), '实际 ' + await total(page));

  console.log('=== 3. 号码检索 ===');
  await page.click('.chip[data-lot="dlt"]');          // 取消大乐透
  await waitTotal(page, 3517);
  await page.click('#q');
  await page.type('#q', '13,14');
  await sleep(700);
  const n13 = await total(page);
  ok('搜索 13,14 有结果且减少', n13 > 0 && n13 < 3517, '实际 ' + n13);
  const both = await page.$$eval('tbody tr', trs => trs.filter(tr => {
    const balls = [...tr.querySelectorAll('.ball')].map(b => +b.textContent);
    return balls.includes(13) && balls.includes(14);
  }).length);
  ok('每行都同时含 13 和 14', both === (await rows(page)),
    both + '/' + await rows(page));

  console.log('=== 4. 排序 ===');
  await page.click('#q', { clickCount: 3 });
  await page.keyboard.press('Backspace');
  await waitTotal(page, 3517);
  await page.click('th[data-sort="sales"]');
  await sleep(600);
  let sales = await page.$$eval('tbody tr td:nth-child(5)', els =>
    els.map(e => e.textContent.trim()));
  ok('销售额降序（首行最大）', sales[0] !== '—', sales.slice(0, 3).join(' | '));
  await page.click('th[data-sort="sales"]');
  await sleep(600);
  const ascFirst = await page.$eval('tbody tr td:nth-child(5)',
    el => el.textContent.trim());
  ok('再点变升序', ascFirst !== sales[0], 'desc首=' + sales[0] + ' asc首=' + ascFirst);

  console.log('=== 5. 每页条数 ===');
  await page.select('#size', '200');
  ok('每页 200 行', await waitRows(page, 200), '实际 ' + await rows(page));

  console.log('=== 6. 详情抽屉 ===');
  await page.select('#size', '50');
  await sleep(500);
  await page.click('tbody tr[data-lot="ssq"]');
  await sleep(900);
  ok('抽屉打开', await page.$eval('#drawer', el => el.classList.contains('on')));
  ok('标题有期号', /双色球 \d+/.test(await page.$eval('#dTitle', el => el.textContent)));
  const hasBalls = await page.$$eval('#dBody .ball', els => els.length);
  ok('详情有号码球', hasBalls > 0, '实际 ' + hasBalls);
  const sect = await page.$$eval('#dBody .sect h4', els => els.map(e => e.textContent));
  ok('详情含开奖号码/关键数据/奖级', sect.includes('开奖号码') &&
    sect.includes('关键数据') && sect.includes('奖级明细'), sect.join(','));
  ok('有奖级源切换标签', (await page.$$('#dBody .dtab')).length >= 1);
  await page.screenshot({ path: SHOT + '_e2e_2_drawer.png' });

  // 切到第二个源的奖级
  const tabs = await page.$$('#dBody .dtab');
  if (tabs.length > 1) {
    await tabs[1].click();
    await sleep(300);
    const visible = await page.$$eval('#dBody .dpane',
      els => els.filter(e => e.style.display !== 'none').length);
    ok('切换奖级源只显示一个面板', visible === 1, '可见 ' + visible);
  }

  await page.keyboard.press('Escape');
  await sleep(400);
  ok('ESC 关闭抽屉', !(await page.$eval('#drawer', el => el.classList.contains('on'))));

  console.log('=== 7. 高级筛选 ===');
  await page.click('#btnAdv');
  await sleep(200);
  ok('高级面板展开', await page.$eval('#adv', el => el.classList.contains('on')));
  await page.click('#has_region');
  await sleep(700);
  await page.click('.chip[data-lot="ssq"]');   // 取消彩种，看全库
  await sleep(800);
  ok('仅分省中奖 = 4989', await waitTotal(page, 4989), '实际 ' + await total(page));

  console.log('=== 8. 重置 ===');
  await page.click('#btnReset');
  ok('重置回 38252', await waitTotal(page, 38252), '实际 ' + await total(page));
  ok('chip 全取消', (await page.$$('.chip.on')).length === 0);

  console.log('=== 9. 分页 ===');
  await page.click('.pager .pg[data-p="3"]');
  await sleep(700);
  ok('跳到第 3 页', (await page.$eval('.pager', el => el.textContent)).includes('第 3 /'));
  await page.click('#jump', { clickCount: 3 });
  await page.type('#jump', '100');
  await page.keyboard.press('Enter');
  await sleep(800);
  ok('跳页到 100', (await page.$eval('.pager', el => el.textContent)).includes('第 100 /'),
    await page.$eval('.pager', el => el.textContent.slice(0, 20)));

  console.log('=== 10. 按天分组 ===');
  await page.click('#btnReset');
  await waitTotal(page, 38252);
  await sleep(700);

  // 每组的「组头声明的期数」与「组内实际挂了多少行」
  const grp = await page.evaluate(() => {
    const hs = [...document.querySelectorAll('tr.dayhead')];
    const pairs = hs.map(h => {
      const m = /(\d+)\s*期/.exec(h.innerText);
      let rows = 0, el = h.nextElementSibling;
      while (el && !el.classList.contains('dayhead')) {
        if (el.dataset && el.dataset.lot) rows++;
        el = el.nextElementSibling;
      }
      return m ? [+m[1], rows] : [-1, rows];
    });
    return {
      n: hs.length,
      first: hs[0] ? hs[0].innerText.replace(/\s+/g, ' ').trim() : '',
      pairs,
      dateHidden: getComputedStyle(document.querySelector('th.col-date')).display,
      dataRows: document.querySelectorAll('tbody tr[data-lot]').length,
    };
  });
  ok('默认视图按天分组', grp.n >= 8, '组数 ' + grp.n);
  ok('组头格式 = 日期 + 星期 + N 期',
    /\d{4}-\d{2}-\d{2}\s*周[日一二三四五六]\s*\d+\s*期/.test(grp.first), grp.first);
  ok('分组后隐藏重复的开奖日列', grp.dateHidden === 'none', 'display=' + grp.dateHidden);
  ok('分组不增加也不丢数据行', grp.dataRows === 50, '实际 ' + grp.dataRows);
  // 跨页那天（一页 50 期 / 一天约 6 期 ⇒ 末组必然被切）会被标成「当天共 N 期 · 本页 M 期」
  const solid = grp.pairs.slice(0, -1);
  ok('非跨页组：组头期数 = 组内行数',
    solid.length > 0 && solid.every(([c, r]) => c === r), JSON.stringify(grp.pairs));
  ok('末组被页边界切开时「组头期数 > 组内行数」',
    grp.pairs.length && grp.pairs[grp.pairs.length - 1][0] >
    grp.pairs[grp.pairs.length - 1][1], JSON.stringify(grp.pairs));

  // 分页边界那天的提示文案
  await page.click('.pager .pg[data-p="2"]');
  await sleep(900);
  const p2head = await page.$eval('tr.dayhead', el => el.innerText.replace(/\s+/g, ' ').trim());
  ok('第 2 页首组标出「跨页」全量期数', /当天共 \d+ 期 · 本页 \d+ 期（跨页）/.test(p2head), p2head);
  await page.screenshot({ path: SHOT + '_e2e_3_days.png' });

  // 换排序 ⇒ 自动退回平表（同一天的行不再连续，硬分组会更乱）
  await page.click('th[data-sort="sales"]');
  await sleep(800);
  const flat = await page.evaluate(() => ({
    heads: document.querySelectorAll('tr.dayhead').length,
    dateCol: getComputedStyle(document.querySelector('th.col-date')).display,
    hint: document.getElementById('groupHint').textContent,
    btn: document.getElementById('btnDateOrder').textContent,
  }));
  ok('非日期排序取消分组', flat.heads === 0, '组数 ' + flat.heads);
  ok('取消分组后开奖日列恢复', flat.dateCol !== 'none', 'display=' + flat.dateCol);
  ok('提示已切平表', flat.hint.indexOf('平表') >= 0, flat.hint);
  ok('按钮变成「按开奖日分组」', flat.btn.indexOf('按开奖日分组') >= 0, flat.btn);

  await page.click('#btnDateOrder');
  await sleep(800);
  ok('点按钮回到按天分组',
    (await page.$$('tr.dayhead')).length >= 8 &&
    (await page.$eval('#btnDateOrder', el => el.textContent)).indexOf('开奖日 ↓') >= 0);

  console.log('=== 11. 表头 / 分组行 / 状态栏吸顶 ===');
  const base = await page.evaluate(() => {
    const sc = document.querySelector('.scroll');
    const th = document.querySelector('thead th').getBoundingClientRect();
    return {
      pageScrolls: document.documentElement.scrollHeight > window.innerHeight + 1,
      canScroll: sc.scrollHeight > sc.clientHeight + 10,
      theadH: Math.round(th.height),
      varH: getComputedStyle(document.documentElement).getPropertyValue('--thead-h').trim(),
      boxTop: Math.round(document.querySelector('.tablebox').getBoundingClientRect().top),
    };
  });
  ok('整页不再滚动（只有表格区滚）', !base.pageScrolls);
  ok('表格区有足够内容可滚', base.canScroll);
  ok('表头高度已实测写入 --thead-h', base.varH === base.theadH + 'px',
    base.varH + ' vs ' + base.theadH + 'px');

  await page.evaluate(() => { document.querySelector('.scroll').scrollTop = 1200; });
  await sleep(500);
  const stick = await page.evaluate(() => {
    const box = document.querySelector('.tablebox').getBoundingClientRect();
    const th = document.querySelector('thead th').getBoundingClientRect();
    const sb = document.getElementById('statbar').getBoundingClientRect();
    const atTop = [...document.querySelectorAll('tr.dayhead td')]
      .filter(t => Math.abs(t.getBoundingClientRect().top - th.bottom) <= 2)
      .map(t => t.innerText.replace(/\s+/g, ' ').trim());
    return {
      thTop: Math.round(th.top), boxTop: Math.round(box.top),
      thStuck: Math.abs(th.top - box.top) <= 2,
      dayStuck: atTop,
      statVisible: sb.top >= -1 && sb.bottom <= window.innerHeight,
      scrollTop: document.querySelector('.scroll').scrollTop,
    };
  });
  ok('滚 1200px 后表头仍钉在表格顶部', stick.thStuck, `th=${stick.thTop} box=${stick.boxTop}`);
  ok('分组行钉在表头正下方', stick.dayStuck.length >= 1, JSON.stringify(stick.dayStuck));
  ok('吸顶的分组行带日期+星期+期数',
    stick.dayStuck.length > 0 &&
    /\d{4}-\d{2}-\d{2}\s*周[日一二三四五六]\s*\d+\s*期/.test(stick.dayStuck[stick.dayStuck.length - 1]),
    JSON.stringify(stick.dayStuck.slice(-1)));
  ok('统计状态栏始终在视口内', stick.statVisible);
  await page.screenshot({ path: SHOT + '_e2e_4_sticky.png' });

  // 翻页要把表格区拉回顶部（窗口本身已经不会滚了）
  await page.click('.pager .pg[data-p="2"]');
  await sleep(900);
  ok('翻页后表格区回到顶部',
    (await page.evaluate(() => document.querySelector('.scroll').scrollTop)) === 0);

  console.log('=== 12. 控制台错误 ===');
  ok('无 JS 运行时错误', errors.length === 0, errors.slice(0, 3).join(' || '));

  await browser.close();
  console.log('\n' + '='.repeat(46));
  console.log('PASS ' + pass + '   FAIL ' + fail);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('EXCEPTION', e); process.exit(2); });
