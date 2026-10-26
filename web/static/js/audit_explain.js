/*
 * `/audit` 测量与证据看板 —— 解释层
 *
 * ★ 四种解释形态的实现：
 *   ① 全局「解释：开/关」开关存储在 localStorage，关闭时只隐藏常驻灰字与卡片说明面板，
 *      **保留**术语虚线与 hover 气泡——解释能力不能整包消失。
 *   ② 术语 hover 气泡：`annotate()` 在每张卡的 innerHTML 渲染完之后，遍历文本节点把已知
 *      术语包成 <span class="term">，**不侵入** 8 个 loader 的渲染逻辑。
 *   ③ 每张卡的「这张卡在测什么 / 关键名词 / 口径与诚实边界」三段 <details>，内容来自
 *      `/api/audit/glossary`（不在本文件里手写免责文案）。
 *   ④ 名词速查抽屉：右侧滑入，支持搜索与按卡片/分类筛选。
 *
 * ★ 加载位置：本文件在 `base.html` 的 `{% block scripts %}` 里、页面内联脚本**之前**引入，
 *   所以 loader 可以直接调用 `EXPL.onCardRendered(...)`。
 *
 * ★ 纪律：名词与卡片文案一律从后端取。本文件只负责「怎么显示」，不负责「说什么」。
 */

window.EXPL = (function () {
  "use strict";

  // ★ 折叠语义：存的是「展开的卡片」而不是「收起的卡片」。
  //   原因是本页的默认态必须是**全部收起**：列表 localStorage 为空天然表示「没有一张被展开」，
  //   这样即使用户清了缓存，默认也是最紧的那一档（而不是最松的那一档）。
  var KEY_EXPLAIN = "audit.explainOn";
  var KEY_OPEN = "audit.open";

  var S = {
    terms: {},            // key -> {名,类,一句话,口径,scope,别名}
    cardMeta: {},         // cardKey -> 后端返回的卡片元数据
    cards: [],            // [{key, el, badge, oneline, summaryCell, anchorEl, dot}]
    last: {},             // cardKey -> {el, payload} 供术语晚到时补标
    explainOn: true,
    open: {},             // cardKey -> 1 表示展开；缺省 = 收起
    drawerFilter: { card: "*", kind: "*", q: "" },
    popEl: null,
    drawerEl: null,
    maskEl: null,
  };

  var STATUS_TEXT = {
    ok: "正常", warn: "注意", none: "无结论",
    nodata: "数据不足", unmeasured: "未测量", error: "加载失败",
  };
  var STATUS_KIND = {
    ok: "ok", warn: "warn", none: "none",
    nodata: "muted", unmeasured: "unmeasured", error: "error",
  };

  // ---------------------------------------------------------------- 工具

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  /** 后端常量里带 Markdown 记号（`code` / **bold**），渲染到 HTML 前做一次轻量转换。
   *  ★ 不能改后端原文——那几个常量受 `tests/test_narrative_unbinding.py` 逐字锁定。 */
  function mdLite(s) {
    return esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\n/g, "<br>");
  }
  function num(x, d) {
    if (x == null) return "-";
    if (typeof x === "number") return x.toFixed(d == null ? 4 : d);
    return String(x);
  }
  function pct(x, d) { return x == null ? "-" : (x * 100).toFixed(d == null ? 1 : d) + "%"; }
  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  function nl2br(s) { return esc(s).replace(/\n/g, "<br>"); }
  var md = mdLite;
  function containerEl(key) { return document.getElementById("hn-" + key); }
  function lsGet(k, dflt) { try { var v = localStorage.getItem(k); return v == null ? dflt : v; } catch (e) { return dflt; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式下忽略 */ } }

  // ---------------------------------------------------------------- 解释开关

  function applyExplain() {
    document.body.classList.toggle("explain-off", !S.explainOn);
    var b = document.getElementById("btn-explain");
    if (b) b.textContent = "解释：" + (S.explainOn ? "开" : "关");
  }
  function setExplain(on) { S.explainOn = !!on; lsSet(KEY_EXPLAIN, on ? "1" : "0"); applyExplain(); }
  function toggleExplain() { setExplain(!S.explainOn); }

  // ---------------------------------------------------------------- 折叠

  function persistOpen() { lsSet(KEY_OPEN, JSON.stringify(Object.keys(S.open))); }

  function applyCollapse(key) {
    var c = find(key); if (!c) return;
    var open = !!S.open[key];
    c.el.setAttribute("data-expanded", open ? "1" : "0");
    var btn = c.el.querySelector(".audit-card-toggle");
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  function find(key) { for (var i = 0; i < S.cards.length; i++) if (S.cards[i].key === key) return S.cards[i]; return null; }

  function expandCard(key) { var c = find(key); if (!c) return; S.open[key] = 1; applyCollapse(key); }
  function collapseCard(key) { var c = find(key); if (!c) return; delete S.open[key]; applyCollapse(key); }
  function toggleCard(key) {
    if (S.open[key]) collapseCard(key); else expandCard(key);
    persistOpen();
  }
  function expandAll() { S.open = {}; S.cards.forEach(function (c) { S.open[c.key] = 1; applyCollapse(c.key); }); persistOpen(); }
  function collapseAll() { S.open = {}; S.cards.forEach(function (c) { applyCollapse(c.key); }); persistOpen(); }
  function scrollToCard(key) {
    var c = find(key); if (!c) return;
    expandCard(key);
    c.el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------------------------------------------------------------- 卡片状态

  function has(v) { return v != null && v !== ""; }

  function deriveStatus(key, d) {
    d = d || {};
    if (d.__error || d.error) return "error";
    if (key === "quality") {
      if (!d.tests || !d.tests.length) return "nodata";
      var hit = (d.tests || []).some(function (t) { return !!t.实质显著; });
      if (hit) return "warn";
      var sc = Number(d.trust_score);
      if (!isFinite(sc)) return "nodata";
      return sc >= 75 ? "ok" : "warn";
    }
    if (key === "ev") {
      if (d.__rollover) return d.参与信号 ? "warn" : "ok";
      if (!d.名义EV表 || !d.名义EV表.length) return "nodata";
      return "ok";
    }
    if (key === "pl") {
      if (!Number(d.total_records) || !Number(d.valid_records)) return "nodata";
      return "ok";
    }
    if (key === "distance") {
      var n = Number(d.样本数);
      if (!n) return "nodata";
      if (n < 30) return "none";
      var c = String(d.结论 || "");
      if (c.indexOf("比随机瞎猜还远") >= 0 || c.indexOf("显著大于基线") >= 0) return "warn";
      if (c.indexOf("显著小于基线") >= 0) return "warn";
      return "ok";
    }
    if (key === "payout") {
      var fit = d.流行度检验 || {};
      if (!fit.applicable) return "none";
      if (!fit.go) return "none";
      return "ok";
    }
    if (key === "trials") {
      var t = d.对照实验 || {}, dr = d.漂移监控 || {};
      var tj = String(t.裁决 || "");
      if (tj.indexOf("主终点异常") >= 0) return "warn";       // 可能是实现缺陷，优先级最高
      var st;
      if (t.等价性成立 === true) st = "ok";
      else if (!Number(t.已结算期数) || Number(t.已结算期数) < 2) st = "none";
      else st = "none";
      if (!dr.go) return st === "ok" ? "none" : st;
      var dj = String(dr.裁决 || "");
      if (dj.indexOf("漂移告警") >= 0) return "warn";
      if (dj === "稳定" || dj.indexOf("结论仍稳定") >= 0) return st;
      return st;
    }
    if (key === "attribution") {
      if (!Number(d.总注数)) return "nodata";
      return "ok";
    }
    if (key === "gap") {
      if (d.未测量) return "unmeasured";
      if (d.状态 === "已激活") return "warn";
      var v = (d.快照 || {}).裁决;
      if (v === "strong") return "warn";
      return "none";
    }
    return "nodata";
  }

  function deriveOneLine(key, d) {
    d = d || {};
    if (d.__error) return "加载失败：" + d.__error;
    try {
      if (key === "quality") {
        var nHit = (d.tests || []).filter(function (t) { return t.实质显著; }).length;
        return "trust_score " + d.trust_score + "/100 · " +
               String(d.grade || "").replace(/（[^）]*）/g, "").trim() + " 级 · " +
               (d.n_records || "-") + " 期 · 实质显著 " + nHit + " 项";
      }
      if (key === "ev") {
        if (d.__rollover) return "总EV " + num(d.总EV, 4) + " 元/注 · " + (d.参与信号 ? "处于 EV 天花板窗口" : "不在 EV 天花板窗口");
        var evs = (d.名义EV表 || []).map(function (x) { return Number(x.名义EV); }).filter(isFinite);
        return evs.length ? ("名义 EV " + Math.min.apply(null, evs).toFixed(3) + " ~ " + Math.max.apply(null, evs).toFixed(3) + " 元/注")
                          : "无可计算的名义 EV";
      }
      if (key === "pl")
        return "共 " + (d.total_records || 0) + " 条反馈（真预测 " + (d.valid_records || 0) + "）· 待开奖 " + (d.pending_count || 0);
      if (key === "distance")
        return "观测中位距离 " + num(d.观测中位距离, 1) + " vs 基线 " + num(d.基线中位距离, 1) +
               " · 近距离率 " + pct(d.近距离率) + " / " + pct(d.基线近距离率);
      if (key === "payout") {
        var fit = d.流行度检验 || {}, cf = d.反事实回测 || {};
        if (!fit.applicable) return "本彩种不适用";
        if (!fit.go) return "流行度模型未通过 Go 门槛，整体不给结论";
        if (cf.go) return "实得提升倍数 " + num(cf["实得提升倍数_中位"], 2) + "×（冷门 vs 热门，中位）";
        return "流行度模型 Go，但本次未跑反事实回测";
      }
      if (key === "trials") {
        var t = d.对照实验 || {};
        return "已结算 " + (t.已结算期数 || 0) + " 期 · " + (t.等价性成立 === true ? "等价性成立" : "积累中");
      }
      if (key === "attribution")
        return (d.总注数 || 0) + " 注 · 中奖 " + (d.总中奖注数 || 0) + " 注 · 命中率 " + pct(d.总命中率, 2) +
               (d.奖金未知注数 ? "（含 " + d.奖金未知注数 + " 注奖金未知）" : "");
      if (key === "gap") {
        if (d.未测量) return "尚未测量，请先运行 " + (S.cardMeta.gap && d.测量命令 ? d.测量命令 : "python cli.py gap");
        return "裁决：" + (((d.快照 || {}).裁决 === "strong") ? "检出缝隙（多轨一致）" :
               ((d.快照 || {}).裁决 === "weak") ? "弱线索（单轨）" : "未检出");
      }
    } catch (e) { /* fallthrough */ }
    return "";
  }

  function onCardRendered(key, el, payload) {
    S.last[key] = { el: el, payload: payload };
    var st = deriveStatus(key, payload);
    setCardStatus(key, st, deriveOneLine(key, payload));
    renderDetails(key);
    annotate(el, key);
    // 异常态自动展开（不写盘：下次加载仍从「默认收起」开始，再按状态自动展开）
    if (st === "warn" || st === "unmeasured" || st === "error") expandCard(key);
  }

  function setCardStatus(key, status, oneLine) {
    var c = find(key); if (!c) return;
    c.el.setAttribute("data-status", status);
    if (c.badge) { c.badge.className = "audit-badge audit-badge--" + (STATUS_KIND[status] || "muted"); c.badge.textContent = STATUS_TEXT[status] || status; }
    if (c.oneline) c.oneline.textContent = oneLine || "";
    if (c.summaryCell) {
      c.summaryCell.setAttribute("data-s", status);
      var bs = c.summaryCell.querySelector(".audit-sum-badge");
      if (bs) { bs.className = "audit-badge audit-sum-badge audit-badge--" + (STATUS_KIND[status] || "muted"); bs.textContent = STATUS_TEXT[status] || status; }
      var sl = c.summaryCell.querySelector(".audit-sum-line");
      if (sl) sl.textContent = oneLine || "";
    }
    if (c.dot) c.dot.setAttribute("data-s", status);
  }

  // ---------------------------------------------------------------- 术语

  function loadTerms() {
    var x = new XMLHttpRequest();
    x.open("GET", "/api/audit/glossary", true);
    x.onreadystatechange = function () {
      if (x.readyState !== 4) return;
      if (x.status !== 200) return;                 // 取不到就降级：页面照常显示，只是没有解释层
      var d;
      try { d = JSON.parse(x.responseText); } catch (e) { return; }
      S.terms = d["术语"] || {};
      (d["卡片"] || []).forEach(function (c) { S.cardMeta[c.key] = c; });
      // 页面级诚实边界（与 //api 同源，不手抄）
      var cv = document.getElementById("audit-page-caveat");
      if (cv && d["诚实边界"]) { cv.innerHTML = md(d["诚实边界"]); cv.style.display = ""; }
      S.cards.forEach(renderDetails);
      // 术语晚到时给已经渲染过的卡片补标（innerHTML 未变，需重置 annotated 标记）
      Object.keys(S.last).forEach(function (k) {
        var e = S.last[k].el;
        if (e) { e.removeAttribute("data-annotated"); annotate(e, k); }
      });
      renderDrawer();
    };
    x.send();
  }

  function buildMatcher(cardKey) {
    var names = [], map = {};
    Object.keys(S.terms).forEach(function (key) {
      var t = S.terms[key], sc = t.scope || ["*"];
      if (sc[0] !== "*" && sc.indexOf(cardKey) < 0) return;
      [t["名"]].concat(t["别名"] || []).forEach(function (n) {
        if (!n) return;
        names.push(n); map[n] = key;
      });
    });
    if (!names.length) return null;
    names.sort(function (a, b) { return b.length - a.length; });   // 长词优先，防「p 值」吞掉「p 值(MC)」
    return { re: new RegExp("(" + names.map(escapeRe).join("|") + ")", "g"), map: map };
  }

  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, CODE: 1, PRE: 1, OPTION: 1, TEXTAREA: 1, TITLE: 1, SVG: 1, PATH: 1, BUTTON: 1 };

  function annotate(root, cardKey) {
    if (!root || !Object.keys(S.terms).length) return;
    if (root.getAttribute("data-annotated") === "1") return;      // 幂等
    var m = buildMatcher(cardKey);
    if (!m) return;
    root.setAttribute("data-annotated", "1");

    // ★ 先快照再替换：边遍历边改 DOM 会漏节点甚至死循环
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var nodes = [], nd;
    while ((nd = walker.nextNode())) nodes.push(nd);

    var seen = {};
    for (var i = 0; i < nodes.length; i++) {
      var txt = nodes[i].nodeValue;
      if (!txt || !txt.trim()) continue;
      var p = nodes[i].parentElement;
      if (!p || SKIP_TAGS[p.tagName]) continue;
      if (p.closest(".term, [title], .audit-card-details")) continue;   // 已包过 / 已有原生 tooltip / 本身就是解释
      m.re.lastIndex = 0;
      if (!m.re.test(txt)) continue;

      m.re.lastIndex = 0;
      var frag = document.createDocumentFragment(), last = 0, hit;
      while ((hit = m.re.exec(txt)) !== null) {
        var key = m.map[hit[1]];
        if (hit.index > last) frag.appendChild(document.createTextNode(txt.slice(last, hit.index)));
        var ck = cardKey + "::" + key;
        if (key && !seen[ck]) {
          seen[ck] = 1;
          var sp = document.createElement("span");
          sp.className = "term";
          sp.setAttribute("data-term", key);
          sp.setAttribute("tabindex", "0");
          sp.appendChild(document.createTextNode(hit[1]));
          frag.appendChild(sp);
        } else {
          frag.appendChild(document.createTextNode(hit[1]));
        }
        last = hit.index + hit[1].length;
        if (hit.index === m.re.lastIndex) m.re.lastIndex++;        // 防零长匹配死循环
      }
      if (last < txt.length) frag.appendChild(document.createTextNode(txt.slice(last)));
      p.replaceChild(frag, nodes[i]);
    }
  }

  // ---------------------------------------------------------------- 卡片三段说明

  function renderDetails(key) {
    var meta = S.cardMeta[key];
    var box = document.getElementById("dbody-" + key);
    if (!meta || !box || box.getAttribute("data-filled") === "1") return;
    box.setAttribute("data-filled", "1");
    var items = (meta["名词"] || []).map(function (k) {
      var t = S.terms[k];
      return t ? "<dt>" + esc(t["名"]) + "</dt><dd>" + esc(t["一句话"]) + "</dd>" : "";
    }).join("");
    box.innerHTML =
      '<section><h4>这张卡在测什么</h4><p>' + esc(meta["在测什么"] || "") + '</p></section>' +
      '<section><h4>关键名词</h4><dl>' + items + '</dl></section>' +
      '<section><h4>口径与诚实边界</h4><p>' + esc(meta["口径说明"] || "") + '</p>' +
      '<p>' + md(meta["诚实边界"] || "") + '</p></section>';
  }

  // ---------------------------------------------------------------- 第一层：摘要 + 锚点

  function buildSummary() {
    var grid = document.getElementById("audit-sum-grid");
    if (!grid) return;
    grid.innerHTML = S.cards.map(function (c) {
      return '<div class="audit-sum-card" data-card="' + esc(c.key) + '" data-s="nodata" ' +
             'onclick="EXPL.scrollToCard(\'' + esc(c.key) + '\')">' +
             '<div class="audit-sum-no">' + esc(c.no) + '</div>' +
             '<div class="audit-sum-name">' + esc(c.name) + '</div>' +
             '<div><span class="audit-badge audit-sum-badge audit-badge--muted">加载中</span></div>' +
             '<div class="audit-sum-line"></div></div>';
    }).join("");
    S.cards.forEach(function (c) { c.summaryCell = grid.querySelector('[data-card="' + c.key + '"]'); });
  }

  function buildAnchors() {
    var box = document.getElementById("audit-anchors-list");
    if (!box) return;
    box.innerHTML = S.cards.map(function (c) {
      return '<a href="javascript:void(0)" data-card="' + esc(c.key) + '" ' +
             'onclick="EXPL.scrollToCard(\'' + esc(c.key) + '\')">' +
             '<span class="audit-anchor-dot" data-s="nodata"></span>' + esc(c.no + " " + c.name) + '</a>';
    }).join("");
    S.cards.forEach(function (c) {
      var a = box.querySelector('[data-card="' + c.key + '"]');
      c.anchorEl = a;
      c.dot = a ? a.querySelector(".audit-anchor-dot") : null;
    });
  }

  function observe() {
    if (typeof IntersectionObserver === "undefined") return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        var key = e.target.getAttribute("data-card");
        var c = find(key);
        if (c && c.anchorEl) c.anchorEl.classList.toggle("active", e.isIntersecting);
      });
    }, { rootMargin: "-20% 0px -70% 0px" });
    S.cards.forEach(function (c) { io.observe(c.el); });
  }

  // ---------------------------------------------------------------- 气泡

  function ensurePop() {
    if (S.popEl) return S.popEl;
    var d = document.createElement("div");
    d.id = "audit-pop";
    d.className = "audit-pop";
    d.style.display = "none";
    document.body.appendChild(d);
    S.popEl = d;
    return d;
  }

  function showPop(el, key) {
    var t = S.terms[key];
    if (!t) return;
    var pop = ensurePop();
    pop.innerHTML =
      '<h5>' + esc(t["名"]) + '<span class="p-kind">' + esc(t["类"]) + '类</span></h5>' +
      '<div>' + esc(t["一句话"]) + '</div>' +
      (t["口径"] ? '<div class="p-cal">口径：' + esc(t["口径"]) + '</div>' : '') +
      '<div class="p-more"><button data-more="1">查看全部名词 →</button></div>';
    pop.style.display = "";
    var r = el.getBoundingClientRect();
    var left = Math.min(Math.max(8, r.left), window.innerWidth - pop.offsetWidth - 8);
    var top = r.bottom + 6;
    if (top + pop.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - pop.offsetHeight - 6);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  function hidePop() { if (S.popEl) S.popEl.style.display = "none"; }

  // ---------------------------------------------------------------- 抽屉

  function ensureDrawer() {
    if (S.drawerEl) return S.drawerEl;
    var root = document.createElement("div");
    root.id = "audit-drawer-root";
    root.style.display = "none";
    root.innerHTML =
      '<div class="audit-drawer-mask" onclick="EXPL.closeDrawer()"></div>' +
      '<aside class="audit-drawer" id="audit-drawer">' +
      '<div class="audit-drawer-head">' +
      '<button class="audit-drawer-close" onclick="EXPL.closeDrawer()" aria-label="关闭">&times;</button>' +
      '<strong>名词速查</strong>' +
      '<div style="font-size:12px;color:var(--gray);margin-top:2px">本页全部专业名词与数据口径。点词条可在卡片间跳转说明。</div>' +
      '<input id="audit-drawer-q" type="search" placeholder="搜索名词或释义…" oninput="EXPL.setQuery(this.value)">' +
      '<div class="audit-drawer-chips" id="audit-drawer-kinds"></div>' +
      '<div class="audit-drawer-chips" id="audit-drawer-cards"></div>' +
      '</div><div class="audit-drawer-list" id="audit-drawer-list"></div></aside>';
    document.body.appendChild(root);
    S.drawerEl = root;
    S.listEl = document.getElementById("audit-drawer-list");
    S.kindBox = document.getElementById("audit-drawer-kinds");
    S.cardBox = document.getElementById("audit-drawer-cards");
    return root;
  }

  function openDrawer(opts) {
    ensureDrawer();
    if (opts && opts.card) S.drawerFilter.card = opts.card;
    if (opts && opts.q) S.drawerFilter.q = String(opts.q);
    S.drawerEl.style.display = "";
    renderDrawer();
    var box = document.getElementById("audit-drawer-q");
    if (box) { box.value = S.drawerFilter.card !== "*" ? "" : S.drawerFilter.q; box.focus(); }
  }
  function closeDrawer() { if (S.drawerEl) S.drawerEl.style.display = "none"; }
  function setQuery(q) { S.drawerFilter.q = q || ""; renderDrawer(); }
  function setFilterCard(k) { S.drawerFilter.card = k; renderDrawer(); }
  function setFilterKind(k) { S.drawerFilter.kind = k; renderDrawer(); }

  function renderDrawer() {
    if (!S.listEl || !Object.keys(S.terms).length) return;
    var f = S.drawerFilter;

    var kinds = [["*", "全部"], ["数据", "数据类"], ["统计", "统计类"]];
    S.kindBox.innerHTML = kinds.map(function (p) {
      return '<button class="audit-drawer-chip' + (f.kind === p[0] ? " on" : "") +
             '" onclick="EXPL.setFilterKind(\'' + p[0] + '\')">' + p[1] + '</button>';
    }).join("");

    S.cardBox.innerHTML = [['*', "全部卡片"]].concat(S.cards.map(function (c) {
      return [c.key, c.no + " " + c.name];
    })).map(function (p) {
      return '<button class="audit-drawer-chip' + (f.card === p[0] ? " on" : "") +
             '" onclick="EXPL.setFilterCard(\'' + p[0] + '\')">' + esc(p[1]) + '</button>';
    }).join("");

    var rows = Object.keys(S.terms).map(function (k) { return S.terms[k]; });
    if (f.kind !== "*") rows = rows.filter(function (t) { return t["类"] === f.kind; });
    if (f.card !== "*") {
      var meta = S.cardMeta[f.card];
      var want = (meta && meta["名词"]) || [];
      rows = rows.filter(function (t) { return want.indexOf(t["名"]) >= 0; });
    }
    var q = (f.q || "").trim();
    if (q) {
      rows = rows.filter(function (t) {
        return (t["名"] + " " + (t["别名"] || []).join(" ") + " " + t["一句话"] + " " + (t["口径"] || ""))
          .indexOf(q) >= 0;
      });
    }
    if (!rows.length) { S.listEl.innerHTML = '<div class="audit-drawer-empty">没有匹配的名词。试试清空搜索或切回「全部」。</div>'; return; }
    S.listEl.innerHTML = rows.map(function (t) {
      return '<div class="audit-drawer-item"><h4>' + esc(t["名"]) +
             ' <span class="p-kind" style="color:var(--gray);font-weight:400">' + esc(t["类"]) + '类</span></h4>' +
             '<p>' + esc(t["一句话"]) + '</p>' +
             (t["口径"] ? '<p style="color:var(--gray)">口径：' + esc(t["口径"]) + '</p>' : '') + '</div>';
    }).join("");
  }

  // ---------------------------------------------------------------- 事件

  function bindEvents() {
    var termOf = function (e) {
      var el = e.target;
      if (!el || !el.closest) return null;
      return el.closest(".term");
    };
    document.addEventListener("mouseover", function (e) {
      var t = termOf(e); if (t) showPop(t, t.getAttribute("data-term"));
    });
    document.addEventListener("mouseout", function (e) {
      var t = termOf(e);
      if (!t) return;
      var to = e.relatedTarget;
      if (to && to.closest && to.closest(".term") === t) return;
      if (to === S.popEl || (to && to.closest && to.closest("#audit-pop"))) return;
      hidePop();
    });
    document.addEventListener("focusin", function (e) { var t = termOf(e); if (t) showPop(t, t.getAttribute("data-term")); });
    document.addEventListener("focusout", function (e) { if (termOf(e)) hidePop(); });
    // 触屏：点一下也出解释
    document.addEventListener("click", function (e) {
      var t = termOf(e);
      if (t) { showPop(t, t.getAttribute("data-term")); return; }
      var more = e.target && e.target.closest && e.target.closest("[data-more]");
      if (more) { openDrawer({}); return; }
      if (!e.target.closest || !e.target.closest("#audit-pop, #audit-drawer-root")) hidePop();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { hidePop(); closeDrawer(); }
    });
    window.addEventListener("scroll", hidePop, { passive: true });
  }

  // ---------------------------------------------------------------- init

  function init() {
    S.cards = Array.prototype.slice.call(document.querySelectorAll(".audit-card")).map(function (el) {
      var key = el.getAttribute("data-card");
      var titleEl = el.querySelector(".audit-card-title");
      var full = titleEl ? titleEl.textContent.trim() : key;
      var no = (full.match(/^[①②③④⑤⑥⑦⑧]/) || [""])[0];
      return {
        key: key, el: el, no: no, name: full.replace(/^[①②③④⑤⑥⑦⑧]\s*/, ""),
        badge: document.getElementById("badge-" + key),
        oneline: document.getElementById("oneline-" + key),
      };
    });

    S.explainOn = lsGet(KEY_EXPLAIN, "1") !== "0";
    applyExplain();

    // 恢复上次展开的卡片（缺省一个都不展开 ⇒ 默认全收起）
    var saved = [];
    try { saved = JSON.parse(lsGet(KEY_OPEN, "[]")) || []; } catch (e) { saved = []; }
    saved.forEach(function (k) { S.open[k] = 1; });
    S.cards.forEach(function (c) { applyCollapse(c.key); });

    ensurePop(); ensureDrawer();
    buildSummary(); buildAnchors(); bindEvents(); observe();
    loadTerms();

    // 深链支持：`/audit#glossary` 直接打开名词速查抽屉（便于别人分享某个名词页）
    if (location.hash === "#glossary") openDrawer({});
  }

  return {
    init: init,
    setExplain: setExplain, toggleExplain: toggleExplain,
    onCardRendered: onCardRendered, setCardStatus: setCardStatus,
    deriveStatus: deriveStatus, deriveOneLine: deriveOneLine,
    annotate: annotate, renderDetails: renderDetails,
    showPop: showPop, hidePop: hidePop,
    openDrawer: openDrawer, closeDrawer: closeDrawer, setQuery: setQuery,
    setFilterCard: setFilterCard, setFilterKind: setFilterKind, renderDrawer: renderDrawer,
    expandCard: expandCard, collapseCard: collapseCard, toggleCard: toggleCard,
    expandAll: expandAll, collapseAll: collapseAll, scrollToCard: scrollToCard,
    loadTerms: loadTerms,
    badge: function (txt, kind) { return '<span class="audit-badge audit-badge--' + esc(kind || "muted") + '">' + esc(txt) + '</span>'; },
    containerEl: containerEl,
    STATUS_TEXT: STATUS_TEXT,
    state: S,
  };
})();
