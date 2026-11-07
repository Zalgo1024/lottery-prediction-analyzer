/* 出号设置页（/gen）前端逻辑 —— 只调既有接口，无新后端。
 * 与模板 web/templates/gen.html 配套；改这里不需要重启（静态文件 max-age=0）。 */
(function(){
  "use strict";

  var DASH = "—";
  var S = {
    ts: null,          // /api/settings/ticket-size
    inv: null,         // /api/system/invariants
    ds: null,          // /api/data-status
    batches: {},       // 彩种 -> {ok, rows[], error}
    open: {},          // 彩种 -> 是否展开
    busy: {},          // 彩种 -> 是否在出号
    edit: {}           // 彩种 -> 用户正在输入但还没保存的值（重绘要保住它）
  };

  function $(id){ return document.getElementById(id); }
  function v(x){ return (x === null || x === undefined || x === "") ? DASH : String(x); }
  function num(x){
    if (x === null || x === undefined || x === "") return DASH;
    if (typeof x === "number" && isFinite(x)) return x.toLocaleString("zh-CN");
    return String(x);
  }
  // ★ 标识符专用：期号/批次号/批号是**名字**不是数量，绝不能加千分位。
  //   福彩3D 的 2026297 被格式化成 "2,026,297" 之后，人对照官方期号会直接对不上。
  function idf(x){
    if (x === null || x === undefined || x === "") return DASH;
    return String(x);
  }
  function esc(s){
    return v(s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function pad(n){ return (n < 10 ? "0" : "") + n; }
  function hhmmss(){
    var d = new Date();
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function logLine(text){
    var box = $("log");
    if (box.firstChild && box.firstChild.className === "muted") box.innerHTML = "";
    var div = document.createElement("div");
    div.innerHTML = '<span class="t">' + hhmmss() + "</span>" + esc(text);
    box.insertBefore(div, box.firstChild);
    while (box.childNodes.length > 200) box.removeChild(box.lastChild);
  }

  async function api(path, opt){
    var r = await fetch(path, Object.assign({cache: "no-store"}, opt || {}));
    var txt = await r.text();
    var j;
    try { j = txt ? JSON.parse(txt) : {}; }
    catch(e){ throw new Error("返回不是 JSON（HTTP " + r.status + "）"); }
    if (!r.ok) throw new Error((j && (j.error || j.msg)) || ("HTTP " + r.status));
    if (j && j.ok === false) throw new Error(j.error || j.msg || "接口返回失败");
    return j;
  }

  // ---------------------------------------------------------------- 读取
  async function loadAll(){
    var errs = [];
    var res = await Promise.allSettled([
      api("/api/settings/ticket-size"),
      api("/api/system/invariants"),
      api("/api/data-status")
    ]);
    if (res[0].status === "fulfilled") S.ts = res[0].value; else errs.push("出号设置：" + res[0].reason.message);
    if (res[1].status === "fulfilled") S.inv = res[1].value; else errs.push("一致性体检：" + res[1].reason.message);
    if (res[2].status === "fulfilled") S.ds = res[2].value; else errs.push("数据状态：" + res[2].reason.message);
    return errs;
  }

  function invOf(lot){
    if (!S.inv || !S.inv["彩种"]) return null;
    for (var i = 0; i < S.inv["彩种"].length; i++){
      if (S.inv["彩种"][i]["彩种"] === lot) return S.inv["彩种"][i];
    }
    return null;
  }
  function dsOf(lot){ return (S.ds && S.ds[lot]) ? S.ds[lot] : null; }

  // ---------------------------------------------------------------- 渲染
  function renderHead(){
    var ts = S.ts;
    var bits = [];
    if (ts){
      bits.push("模式 <b>" + (ts.mode === "dynamic" ? "动态" : "固定") + "</b>");
      bits.push("锁定 <b>" + (ts["锁定"] ? "开启（后台不自动改注数）" : "关闭") + "</b>");
      if (ts.policy && ts.policy["彩种"]) {
        bits.push("最近一次策略判断来自 <b>" + esc(ts.policy["彩种"]) + "</b>：" + esc(ts.policy["理由"] || DASH));
      }
    }
    var uptime = S.inv ? "" : "";
    $("sub").innerHTML = bits.length ? bits.join(" · ") + uptime : "读数不可用";
    // 「按策略调整」只在动态模式下有意义：固定模式下点它等于空操作，直接禁用更诚实
    var dyn = !!(S.ts && S.ts.mode === "dynamic");
    $("btn-apply").disabled = !dyn;
    $("btn-apply").title = dyn ? "按当前策略评估一次并写回注数（不改判决，只改规模）"
                               : "当前是固定模式：动作用不到，请直接改注数";

    var b = $("banner");
    if (!ts){
      b.className = "banner bad";
      b.innerHTML = "<b>设置读不到</b>：不是「全部正常」，是本页拿不到 <code>/api/settings/ticket-size</code>。请看下方操作记录。";
      return;
    }
    var inv = S.inv;
    if (!inv){
      b.className = "banner warn";
      b.innerHTML = "<b>一致性体检不可用</b>：下面的「实际出号」列一律显示 " + DASH + "，不要读成 0。";
      return;
    }
    var bad = inv["不一致"] || [];
    var nStop = 0, rows = inv["彩种"] || [];
    for (var i = 0; i < rows.length; i++){
      if (String(rows[i]["状态"] || "").indexOf("停发") >= 0) nStop++;
    }
    if (bad.length){
      b.className = "banner bad";
      b.innerHTML = "<b>有 " + bad.length + " 个彩种的「设置注数」与实际出号不一致：</b>" +
                    bad.map(esc).join("、") +
                    "。刚改过注数的瞬态会在下一次流水线重生成后自愈；若长期不变，说明有旁路在覆盖注数。";
    } else if (nStop){
      b.className = "banner warn";
      b.innerHTML = "<b>" + nStop + " 个彩种处于「停发（预算包络）」</b>：月度额度用尽 ⇒ 本期可执行注数为 0，属既定行为，不是故障。";
    } else {
      b.className = "banner ok";
      b.innerHTML = "<b>全部一致</b>：每个彩种的「下一期 pending 组数」都等于设置注数。";
    }
  }

  function renderKpis(){
    var ts = S.ts, bud = (ts && ts["预算"]) || null;
    var cards = [];
    function add(k, val, note){ cards.push('<div class="kpi"><div class="k">' + esc(k) +
      '</div><div class="v">' + val + '</div><div class="n">' + esc(note) + "</div></div>"); }

    if (!ts){ $("kpis").innerHTML = ""; return; }

    var lots = ts.lotteries || [], total = 0, totalKnown = 0;
    for (var i = 0; i < lots.length; i++){
      var c = lots[i]["当前生效"];
      if (typeof c === "number"){ total += c; totalKnown++; }
    }
    add("彩种数", num(lots.length), "来自后端注册表（本页不写死清单）");
    add("当前生效注数合计", totalKnown ? num(total) : DASH,
        totalKnown === lots.length ? "逐彩种相加，仅表规模（×2 元 ≠ 单期上限，跨彩种由月度总额管）"
                                   : "有彩种读不到，合计不完整");

    if (bud && bud["可用"]){
      add("月度总额", num(bud["月度总额"]) + " 元", "假设成本上限，不是真实投入");
      add("本月已投", num(bud["已投"]) + " 元", "月份 " + v(bud["月份"]));
      add("本月剩余", num(bud["剩余"]) + " 元", bud["已用尽"] ? "已用尽 ⇒ 本期会停发" : "未用尽");
      add("单期上限", num(bud["单期上限"]) + " 元", "单彩种 · 单批（不是同期合计）");
    } else {
      add("预算闸门", "不可用", (bud && bud["原因"]) ? String(bud["原因"]).slice(0, 40) : "读不到预算快照");
    }
    $("kpis").innerHTML = cards.join("");
  }

  function statusPill(inv){
    if (!inv) return '<span class="pill">' + DASH + "</span>";
    var t = String(inv["状态"] || "");
    if (t.indexOf("停发") >= 0) return '<span class="pill warn" title="月度额度已用尽，本期可执行注数为 0，属既定行为">停发（预算包络）</span>';
    // 「无预测」是开奖后的正常空窗，不是错误：给它独立标签，别和「不一致」共用一种颜色。
    if (t.indexOf("无预测") >= 0) return '<span class="pill" title="下一期还没有 pending（开奖后的正常空窗，不是错误）">无预测（正常空窗）</span>';
    if (inv["一致"] === true) return '<span class="pill on">一致</span>';
    if (inv["一致"] === false) return '<span class="pill off">不一致</span>';
    return '<span class="pill">' + esc(t || DASH) + "</span>";
  }

  function renderRows(){
    var ts = S.ts;
    var body = $("rows");
    if (!ts){ body.innerHTML = '<tr><td colspan="11" class="muted">设置读不到，无法渲染（不是「没有彩种」）。</td></tr>'; return; }

    var html = [];
    var lots = ts.lotteries || [];
    for (var i = 0; i < lots.length; i++){
      var it = lots[i], lot = it.lottery;
      var inv = invOf(lot), ds = dsOf(lot);
      var eff = (typeof it["当前生效"] === "number") ? it["当前生效"] : null;
      var stopped = (eff === 0) || (inv && String(inv["状态"] || "").indexOf("停发") >= 0);
      // ★ 重绘要保住「正在输入但还没保存」的值：保存任意一个彩种都会重绘整表，
      //   若不保留，用户改了 A 又去点 B 的保存，A 的输入会被静默冲回旧值。
      var pendingEdit = (S.edit[lot] !== undefined) ? S.edit[lot] : null;
      var shown = pendingEdit !== null ? pendingEdit : (eff === null ? "" : eff);
      var isDirty = pendingEdit !== null && String(pendingEdit) !== String(eff);
      var b = S.batches[lot];
      var batchInfo = "";
      if (b && b.ok){
        batchInfo = '<div class="muted" style="font-size:11px">本期 ' + b.rows.length + " 批 / 最新重出批号 " +
                    esc(v(b.rows.length ? b.rows[0]["重出批号"] : null)) + "</div>";
      } else if (b && !b.ok){
        batchInfo = '<div class="muted" style="font-size:11px">批次读取失败</div>';
      }

      html.push('<tr data-lot="' + esc(lot) + '">' +
        "<td><b>" + esc(lot) + "</b>" + (it["数字型"] ? ' <span class="pill">数字型</span>' : "") + batchInfo + "</td>" +
        '<td class="num"><input type="number" data-role="num" data-lot="' + esc(lot) +
          '" value="' + esc(String(shown)) + '" min="' + v(it["下限"]) + '" max="' + v(it["上限"]) +
          '"' + (isDirty ? ' class="dirty"' : "") + (stopped ? " disabled" : "") + "></td>" +
        '<td class="num muted">' + num(it["上限"]) + "</td>" +
        '<td class="num muted">' + num(it["配置值"]) + "</td>" +
        '<td class="num muted">' + num(it["动态值"]) + "</td>" +
        '<td class="num">' + num(inv ? inv["组数"] : null) + "</td>" +
        "<td>" + idf(inv ? inv["目标期号"] : null) + "</td>" +
        "<td>" + v(ds ? ds.next_draw_date : null) + "</td>" +
        "<td>" + statusPill(inv) + (inv && inv["说明"] ? '<div class="muted" style="font-size:11px">' + esc(inv["说明"]) + "</div>" : "") + "</td>" +
        '<td class="muted">' + v(inv ? String(inv["生成时间"] || "").replace("T", " ") : null) + "</td>" +
        '<td class="row-acts">' +
          '<button class="btn sm" data-act="save" data-lot="' + esc(lot) + '">保存</button>' +
          '<button class="btn sm" data-act="regen" data-lot="' + esc(lot) + '"' + (stopped ? " disabled" : "") +
            ' title="按本行输入框的注数重出一批（旧批保留）">重出</button>' +
          '<button class="btn sm" data-act="det" data-lot="' + esc(lot) + '">' +
            (S.open[lot] ? "收起" : "批次明细") + "</button>" +
        "</td></tr>");

      if (S.open[lot]) html.push(detailsRow(lot, it, inv, ds, b));
    }
    body.innerHTML = html.join("");
  }

  function detailsRow(lot, it, inv, ds, b){
    var html = ['<tr class="det"><td colspan="11">'];

    // 建议明细（动态模式）
    if (it["建议"]){
      var s = it["建议"];
      html.push("<h4>策略建议（动态模式）</h4>");
      html.push('<div class="kv">' +
        "<span>建议注数 <b>" + num(s.next) + "</b></span>" +
        "<span>理由 <b>" + esc(v(s.reason)) + "</b></span>" +
        "<span>命中率 <b>" + (s["命中率"] == null ? DASH : (Number(s["命中率"]) * 100).toFixed(2) + "%") + "</b></span>" +
        "<span>理论率 <b>" + (s["理论率"] == null ? DASH : (Number(s["理论率"]) * 100).toFixed(2) + "%") + "</b></span>" +
        "<span>名义 z <b>" + (s.z == null ? DASH : Number(s.z).toFixed(2)) + "</b></span>" +
        "<span>样本够 <b>" + (s["样本够"] === true ? "是" : s["样本够"] === false ? "否" : DASH) + "</b></span>" +
        "</div>");
      if (s["诊断"]) html.push('<div class="warnbox">' + esc(s["诊断"]) + "</div>");
    }

    // 彩种规则与数据状态
    html.push("<h4>数据与期次</h4>");
    html.push('<div class="kv">' +
      "<span>设置上限 <b>" + num(it["上限"]) + "</b></span>" +
      "<span>设置下限 <b>" + num(it["下限"]) + "</b></span>" +
      "<span>默认注数 <b>" + num(it["默认"]) + "</b></span>" +
      "<span>最新已开奖 <b>" + v(ds ? ds.latest_draw : null) + "</b></span>" +
      "<span>最新开奖日 <b>" + v(ds ? ds.latest_date : null) + "</b></span>" +
      "<span>下一开奖日 <b>" + v(ds ? ds.next_draw_date : null) + "</b></span>" +
      "<span>数据滞后 <b>" + (ds && ds.data_lag_days != null ? v(ds.data_lag_days) + " 天" : DASH) + "</b></span>" +
      // ★ 来源与新鲜度必须分开写：本接口读的是本地文件，不是实时接口。
      //   只写「滞后 0 天」会被读成「刚从官网取过」。
      "<span>数据来源 <b>" + esc(v(ds ? ds["来源"] : null)) + "</b></span>" +
      "<span>新鲜度 <b>" + esc(v(ds ? ds["新鲜度"] : null)) + "</b>（" + esc(v(ds ? ds.csv_modified : null)) + "）</span>" +
      "<span>记录总数 <b>" + num(ds ? ds.total_records : null) + "</b></span>" +
      "</div>");

    // 批次明细（懒加载）
    html.push("<h4>本期批次（每次出号 = 一条 pending；重出会追加新批，旧批保留）</h4>");
    if (!b){
      html.push('<button class="btn sm" data-act="load" data-lot="' + esc(lot) + '">加载批次明细</button>' +
                '<span class="muted" style="font-size:12px;margin-left:8px">' +
                "按需加载：单个彩种的一批可能上千注，全部预加载会让页面卡住。</span>");
    } else if (b.loading){
      // ★ 「读取中」不是「读取失败」：把加载态画成红框会让人以为接口坏了。
      html.push('<div class="muted" style="font-size:12px">正在读取批次明细…</div>');
    } else if (!b.ok){
      html.push('<div class="errbox">批次明细读取失败：' + esc(b.error) +
                "（不是「本期没有批次」，是读不到）</div>");
    } else if (!b.rows.length){
      html.push('<div class="muted">这个彩种当前没有 pending 批次（开奖后的正常空窗）。</div>');
    } else {
      html.push("<table><thead><tr>" +
        "<th>批次号</th><th>重出批号</th><th>目标期号</th><th>目标开奖日</th><th>生成时间</th>" +
        '<th class="num">请求注数</th><th class="num">目标注数</th><th class="num">实际注数</th>' +
        '<th class="num">剪枝后</th><th class="num">截断</th><th>来源</th><th>最新</th>' +
        "</tr></thead><tbody>");
      for (var i = 0; i < b.rows.length; i++){
        var r = b.rows[i];
        var prune = r["剪枝统计"] && typeof r["剪枝统计"] === "object" ? r["剪枝统计"]["剪枝后注数"] : null;
        var cut = r["补齐统计"] && typeof r["补齐统计"] === "object" ? r["补齐统计"]["截断"] : null;
        html.push("<tr>" +
          "<td>" + esc(v(r["批次号"])) + "</td>" +
          "<td>" + esc(v(r["重出批号"])) + "</td>" +
          "<td>" + esc(v(r["目标期号"])) + "</td>" +
          "<td>" + esc(v(r["预测日期"])) + "</td>" +
          '<td class="muted">' + esc(v(String(r["生成时间"] || "").replace("T", " ")).slice(0, 19)) + "</td>" +
          '<td class="num">' + num(r["请求注数"]) + "</td>" +
          '<td class="num">' + num(r["目标注数"]) + "</td>" +
          '<td class="num"><b>' + num(r["实际注数"]) + "</b></td>" +
          '<td class="num muted">' + num(prune) + "</td>" +
          '<td class="num">' + (cut === 0 ? "0" : num(cut)) + "</td>" +
          "<td>" + esc(v(r["来源"])) + "</td>" +
          "<td>" + (r["最新批次"] === true ? '<span class="pill on">最新</span>' : '<span class="pill">旧批</span>') + "</td>" +
          "</tr>");
      }
      html.push("</tbody></table>");
      html.push('<div class="muted" style="font-size:12px;margin-top:6px">' +
        "实际注数 ≠ 目标注数时，差额来自额度夹取/截断；「截断」= 0 才表示没被截断。</div>");
    }

    html.push("</td></tr>");
    return html.join("");
  }

  // ---------------------------------------------------------------- 动作
  function readInput(lot){
    var el = document.querySelector('input[data-role="num"][data-lot="' + lot + '"]');
    if (!el) return null;
    var raw = String(el.value || "").trim();
    if (!raw) return null;
    var n = Number(raw);
    return isFinite(n) ? Math.round(n) : null;
  }

  async function saveOne(lot){
    var n = readInput(lot);
    if (n === null){ logLine(lot + "：注数输入为空，未提交（空值不等于 0）"); return false; }
    var mode = (S.ts && S.ts.mode) || "fixed";
    var body = {mode: mode};
    body[mode] = {};
    body[mode][lot] = n;                 // ★ 只提交当前模式那一个字段
    try {
      var j = await api("/api/settings/ticket-size", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      });
      S.ts = j;
      delete S.edit[lot];            // 已落盘 ⇒ 这个彩种不再算「未保存」
      logLine(lot + "：已保存注数 " + n + "（" + (mode === "dynamic" ? "动态" : "固定") + "模式）");
      refreshUI();
      return true;
    } catch(e){
      logLine(lot + "：保存失败 — " + e.message);
      return false;
    }
  }

  async function pollTask(taskId, lot){
    var started = Date.now();
    while (Date.now() - started < 240000){
      await new Promise(function(r){ setTimeout(r, 1500); });
      var st;
      try { st = await api("/api/predict/status/" + encodeURIComponent(taskId)); }
      catch(e){ continue; }
      if (st.status === "done" || st.status === "error" || st.status === "failed"){
        if (st.status !== "done"){
          throw new Error((st.error || st.message || st.status) + "");
        }
        return st.result || {};
      }
    }
    throw new Error("等待超时（4 分钟），出号可能仍在后台进行，稍后刷新读数确认");
  }

  async function regenOne(lot, opts){
    opts = opts || {};
    var n = readInput(lot);
    var body = {lottery: lot};
    // ★ 必须带卡片当前值：否则后端按磁盘里的旧注数出号，界面会「看起来被改回去了」
    if (n !== null) body["出号数量"] = n;
    S.busy[lot] = true;
    try {
      var r = await api("/api/predict/regenerate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      });
      logLine(lot + "：已提交重出（目标期 " + v(r["目标期号"]) + " / 重出批号 " + v(r["重出批号"]) +
              " / 上批 " + num(r["上一批注数"]) + " 注）");
      var res = await pollTask(r.task_id, lot);
      var got = res && (res["组数"] || res["groups"] || res["注数"]);
      logLine(lot + "：重出完成" + (got != null ? "，实际 " + got + " 注" : "") +
              "（" + v(r["出号数量来源"]) + "）");
      if (r["单期上限口径"]){
        var cap = r["单期上限口径"];
        logLine(lot + "：单期上限口径 — " + (cap["说明"] || cap["原因"] || (cap["适用"] ? "已核算" : "不可用")));
      }
      return true;
    } catch(e){
      logLine(lot + "：重出失败 — " + e.message);
      return false;
    } finally {
      S.busy[lot] = false;
    }
  }

  async function regenAll(){
    var lots = (S.ts && S.ts.lotteries) || [];
    var targets = [];
    for (var i = 0; i < lots.length; i++){
      if (typeof lots[i]["当前生效"] === "number" && lots[i]["当前生效"] > 0) targets.push(lots[i].lottery);
    }
    if (!targets.length){ logLine("没有可重出的彩种（全部停发或读不到注数）"); return; }
    var detail = targets.map(function(l){ return l + " " + readInput(l) + " 注"; }).join("\n· ");
    if (!confirm("将对 " + targets.length + " 个彩种**逐个串行**重出：\n· " + detail +
                 "\n\n旧号全部保留（开奖后照常结算），新批带重出批号并存。" +
                 "\n注数取本页输入框当前值，并保存为当前生效值。\n\n继续？")) return;
    $("btn-regen-all").disabled = true;
    var ok = 0, fail = 0;
    try {
      // ★ 串行：record_spend 是「读盘 → 改 → 原子写」，并发会互相覆盖
      for (var k = 0; k < targets.length; k++){
        var r = await regenOne(targets[k], {quiet: true});
        if (r) ok++; else fail++;
      }
    } finally {
      $("btn-regen-all").disabled = false;
    }
    logLine("全部重出结束：成功 " + ok + " / 失败 " + fail);
    await refresh();
  }

  async function applyDynamic(){
    if (!S.ts || S.ts.mode !== "dynamic"){
      logLine("当前不是动态模式，「按策略调整」不可用（固定模式请直接改注数）");
      return;
    }
    try {
      var j = await api("/api/settings/ticket-size/apply", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({})
      });
      S.ts = j;
      var names = Object.keys(j["调整"] || {});
      var changed = 0;
      names.forEach(function(k){
        var a = j["调整"][k];
        if (a && a["上一期注数"] !== a.next) changed++;
      });
      logLine("按策略调整完成：" + names.length + " 个彩种评估，其中 " + changed + " 个注数发生变化");
      refreshUI();
    } catch(e){
      logLine("按策略调整失败 — " + e.message);
    }
  }

  async function loadBatches(lot){
    S.batches[lot] = {loading: true};
    renderRows();
    try {
      var j = await api("/api/feedback/" + encodeURIComponent(lot) + "/pending");
      var rows = j.records || [];
      // 只保留字段，丢掉号码数组（一批上千注，把号码留在内存里没有意义）
      var slim = rows.map(function(r){
        return {
          "批次号": r["批次号"], "重出批号": r["重出批号"], "目标期号": r["目标期号"],
          "预测日期": r["预测日期"], "生成时间": r["生成时间"],
          "请求注数": r["请求注数"], "目标注数": r["目标注数"],
          "实际注数": r["实际注数"] != null ? r["实际注数"] : r["号码组数"],
          "剪枝统计": r["剪枝统计"], "补齐统计": r["补齐统计"],
          "来源": r["来源"], "最新批次": r["最新批次"], "台账域": r["台账域"]
        };
      });
      S.batches[lot] = {ok: true, rows: slim, error: ""};
      logLine(lot + "：已加载 " + slim.length + " 条批次明细");
    } catch(e){
      S.batches[lot] = {ok: false, rows: [], error: e.message};
      logLine(lot + "：批次明细读取失败 — " + e.message);
    }
    renderRows();
  }

  // ---------------------------------------------------------------- 交互
  function refreshUI(){
    renderHead();
    renderKpis();
    renderRows();
  }

  async function refresh(){
    var errs = await loadAll();
    if (errs.length) errs.forEach(function(e){ logLine(e); });
    refreshUI();
  }

  document.addEventListener("click", function(ev){
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    var act = t.getAttribute("data-act");
    if (!act) return;
    var lot = t.getAttribute("data-lot");
    if (act === "save"){
      saveOne(lot).then(function(){});
    } else if (act === "regen"){
      var n = readInput(lot);
      if (!confirm("对「" + lot + "」重出一批？\n\n· 注数：" + v(n) + " 注（本页输入框当前值，会保存为当前生效值）" +
                   "\n· 旧号全部保留，开奖后照常结算\n· 新批带重出批号，与旧批并存")) return;
      regenOne(lot).then(function(ok){ if (ok) return refresh(); });
    } else if (act === "det"){
      S.open[lot] = !S.open[lot];
      if (S.open[lot] && !S.batches[lot]) loadBatches(lot);
      else renderRows();
    } else if (act === "load"){
      loadBatches(lot);
    }
  });

  document.addEventListener("input", function(ev){
    var t = ev.target;
    if (!t || t.getAttribute("data-role") !== "num") return;
    S.edit[t.getAttribute("data-lot")] = t.value;
    t.classList.add("dirty");
  });

  $("btn-refresh").addEventListener("click", function(){ refresh().then(function(){ logLine("已刷新读数"); }); });
  $("btn-apply").addEventListener("click", applyDynamic);
  $("btn-regen-all").addEventListener("click", function(){ regenAll().then(function(){}); });

  refresh().then(function(){
    if (!S.ts) return;
    var mode = S.ts.mode === "dynamic" ? "动态" : "固定";
    logLine("页面就绪：模式 " + mode + "，共 " + (S.ts.lotteries || []).length + " 个彩种");
  });
})();
