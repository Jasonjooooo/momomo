/* X 时间工作流 · 页面
 * 步骤（可以加、改、删，可以只做一次或重复做）+ 回关助手发帖排期表里的帖子 + 每小时的关注轮次和重复动作
 * + X 自动回复流程的运行状态 */
"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const btn = (txt, cls, fn) => { const b = el("button", cls, txt); b.type = "button"; if (fn) b.addEventListener("click", fn); return b; };
const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
const today = () => dateKey(new Date());
const STALE_MS = 3 * 60000;   // 另外两个插件每分钟发一次数据；3 分钟没收到就算断开
const DRAG_STEP = 5, PX_PER_MIN = 1.2;

const S = {
  day: today(), steps: [], editing: false, notify: true, done: {}, runs: {}, openLogs: new Set(), dragging: false,
  hg: null, hgAt: 0, xrf: null, xrfAt: 0,
};

/* ---------- 读写存储 ---------- */
async function load() {
  const v = await chrome.storage.local.get(["snap", "xsnap", "wfSteps", "wfTimes", "wfNotify", "wfFollow", "wfFollowMigrated", "wfDone", "wfRuns"]);
  S.hg = v.snap || null; S.hgAt = (v.snap && v.snap.at) || 0;
  S.xrf = v.xsnap || null; S.xrfAt = (v.xsnap && v.xsnap.at) || 0;
  S.steps = loadSteps(v);
  if (!v.wfFollowMigrated) await chrome.storage.local.set({ wfSteps: S.steps, wfFollowMigrated: true });   // 「关注新人」变成普通步骤，只做一次
  S.notify = v.wfNotify !== false;
  S.done = v.wfDone || {};
  S.runs = v.wfRuns || {};
}
const saveSteps = () => chrome.storage.local.set({ wfSteps: S.steps });
const hgOk = () => S.hgAt && Date.now() - S.hgAt < STALE_MS;
const xrfOk = () => S.xrfAt && Date.now() - S.xrfAt < STALE_MS;
const H = (k, d) => (S.hg && S.hg[k] != null ? S.hg[k] : d);

/* 操作交给另外两个插件去做 */
async function send(to, msg) {
  let r;
  try { r = await chrome.runtime.sendMessage({ type: to, msg }); } catch (e) { r = { ok: false, reason: "插件刚更新过，刷新一下这个页面再试。" }; }
  if (!r || !r.ok) showErr((r && r.reason) || "没连上。");
  return r || { ok: false };
}
const ACT = {
  sched: () => send("hg", { type: "xfb-open-schedule" }),
  inbox: () => send("hg", { type: "xfb-open-inbox" }),
  pool: () => send("hg", { type: "wf-open-pool" }),
};
let errT = 0;
function showErr(t) { const e = $("#err"); e.textContent = t; e.hidden = false; clearTimeout(errT); errT = setTimeout(() => { e.hidden = true; }, 9000); }

/* ---------- 帖子（来自回关助手的排期表） ---------- */
function postsOf(day) {
  const items = (H("schedule", {}) || {})[day] || {};
  return Object.keys(items).sort().filter((hm) => hasContent(items[hm])).map((hm) => ({ hm, m: toMin(hm), ...items[hm] }));
}
function postState(day, p) {
  if (p.status === "xsched") return ["xsched", "X 已定时 ✓"];
  if (p.status === "sent") return ["sent", "已发 ✓"];
  const past = day < today() || (day === today() && nowMin() - p.m >= MISS_AFTER);
  return past ? ["miss", "错过了"] : ["wait", "待发 · 到点提醒你"];
}
function dayLabel(key) {
  const d = new Date(key + "T00:00:00");
  return "周" + "日一二三四五六"[d.getDay()] + ` ${d.getMonth() + 1}/${d.getDate()}`;
}

/* ---------- 顶部：连接状态、数字、互关池 ---------- */
function renderTop() {
  const hs = $("#st-hg"), xs = $("#st-xrf");
  hs.dataset.s = !S.hgAt ? "none" : hgOk() ? "ok" : "stale";
  xs.dataset.s = !S.xrfAt ? "none" : xrfOk() ? "ok" : "stale";
  const ps = postsOf(S.day);
  const cnt = { xsched: 0, sent: 0, wait: 0, miss: 0 };
  ps.forEach((p) => cnt[postState(S.day, p)[0]]++);
  const label = S.day === today() ? "今天" : dayLabel(S.day);
  let total7 = 0;
  for (let i = 0; i < 7; i++) total7 += postsOf(dateKey(new Date(Date.now() + i * 864e5))).length;
  $("#meta-hg").textContent = !S.hgAt ? "还没连上：装好新版回关助手，再打开一次 X 网页。下面的步骤现在就能用。"
    : (!hgOk() ? `断开了（上次同步 ${clock(S.hgAt)}），显示的是上次的排期 · ` : "已同步发帖排期表、互关池 · ")
      + (ps.length ? `${label} ${ps.length} 条：X 已定时 ${cnt.xsched}，已发 ${cnt.sent}，待发 ${cnt.wait}` + (cnt.miss ? `，错过 ${cnt.miss}` : "") : `${label}还没排帖子`) + ` · 未来 7 天 ${total7} 条`;
  const x = S.xrf || {}, st = x.status || {};
  $("#meta-xrf").textContent = !S.xrfAt ? "还没连上：装好新版 X 自动回复流程（1.1.0），再刷新一次 X 页面。"
    : (!xrfOk() ? `断开了（上次 ${clock(S.xrfAt)}）· ` : "已连接 · ") + `今天回复 ${x.todayDone || 0} 条 · ` + (st.active ? (st.paused ? "已暂停" : `运行中 ${st.done || 0}/${st.max || 0}`) : "现在空闲");

  $("#sum").hidden = !S.hgAt;
  $("#s-posts").textContent = ps.length;
  $("#s-posts-l").textContent = label + "要发的帖子";
  const r = H("replyToday", 0), goal = H("replyGoal", 500);
  $("#s-replies").textContent = `${r} / ${goal}`;
  $("#s-replies").className = r >= goal ? "ok" : "";
  $("#s-inbox").textContent = H("inboxTodo", 0);
  const rm = $("#s-remind");
  if (!H("remindOn", true)) rm.textContent = "已关闭";
  else if (H("remindDue", 0)) rm.textContent = "到点了";
  else if (H("remindNextAt", 0) > Date.now()) rm.textContent = Math.max(1, Math.round((H("remindNextAt", 0) - Date.now()) / 60000)) + " 分钟后";
  else rm.textContent = "进行中";
}
const clock = (t) => { const d = new Date(t); return `${d.getMonth() + 1}/${d.getDate()} ${fmt(d.getHours() * 60 + d.getMinutes())}`; };

function renderPool() {
  $("#pool").hidden = !S.hgAt;
  if (!S.hgAt) return;
  const p = H("pool", null) || { count: 0, top: [] };
  $("#pool-meta").textContent = `池子里 ${p.count} 人 · 今天已关注 ${H("followToday", 0)} 人`;
  const r = $("#pool-round"); r.textContent = "";
  if (H("remindDue", 0)) { r.className = "pool-round due"; r.textContent = "到点了：打开互关池，连按 F 关注这一轮。"; }
  else if (H("roundWorking", 0)) { r.className = "pool-round"; r.append("这一轮已关注 ", el("b", null, `${H("roundCount", 0)} / ${H("roundTarget", 15)}`), "，关满就开始下一轮倒计时"); }
  else if (!H("remindOn", true)) { r.className = "pool-round idle"; r.textContent = "回关助手的定时提醒关着。想每轮提醒，在回关助手面板里打开「定时提醒我去找互关帖」。"; }
  else if (H("remindNextAt", 0) > Date.now()) { r.className = "pool-round idle"; r.append("下一轮 ", el("b", null, Math.max(1, Math.round((H("remindNextAt", 0) - Date.now()) / 60000)) + " 分钟后"), " 提醒"); }
  else { r.className = "pool-round idle"; r.textContent = "等回关助手开始下一轮倒计时。"; }
  const ol = $("#pool-top"); ol.textContent = "";
  if (!p.top.length) ol.append(el("li", null, "池子是空的：打开几条互关帖，绿色「容易回关」的人会自动进池。"));
  p.top.forEach((x) => { const li = el("li"); li.title = "@" + x.handle; li.append(el("b", null, x.name || "@" + x.handle), el("small", null, `${x.fo} 粉 / ${x.fr} 关`)); ol.append(li); });
  const rm = H("remindMin", 0), w = $("#pool-warn");
  const fs = S.steps.find((s) => s.follow);
  w.hidden = !(fs && H("remindOn", true) && rm && rm !== fs.every);
  if (fs) w.textContent = `回关助手现在每 ${rm} 分钟提醒一次，时间线上的「关注新人」是每 ${everyText(fs.every)}一轮。想一致：在 X 首页回关助手面板 →「设置」→「定时提醒我去找互关帖」选「每 ${fs.every} 分钟」，或者在「编辑步骤」里改「关注新人」。`;
  $("#pool-set").textContent = fs ? `时间线上的「关注新人」：每 ${everyText(fs.every)}一轮，${fmt(fs.from)}–${fmt(fs.to)}，${daysText(fs.days)}。在「编辑步骤」里能改时间或删掉。`
    : "时间线上没有「关注新人」轮次。想加回来：「编辑步骤」→「恢复默认步骤」，或者加一个重复做的步骤。";
}

function renderDays() {
  const box = $("#days"); box.textContent = "";
  for (let i = 0; i < 7; i++) {
    const key = dateKey(new Date(Date.now() + i * 864e5));
    const b = btn(i === 0 ? `今天 ${new Date().getMonth() + 1}/${new Date().getDate()}` : dayLabel(key), null, () => { S.day = key; render(); });
    b.setAttribute("aria-pressed", String(key === S.day));
    b.append(el("small", null, postsOf(key).length + " 条"));
    box.append(b);
  }
}

/* ---------- 时间线 ---------- */
function renderTimeline() {
  const ol = $("#tl"); ol.textContent = "";
  const steps = S.steps.filter((s) => onDay(s, S.day));
  const ps = postsOf(S.day);
  const rows = [];
  steps.filter((s) => s.mode === "once").forEach((s) => rows.push({ m: s.t, o: 0, node: () => stepNode(s) }));
  if (S.editing) steps.filter((s) => s.mode === "repeat").forEach((s) => rows.push({ m: s.from, o: 0, node: () => stepNode(s) }));
  ps.forEach((p, i) => rows.push({ m: p.m, o: 1, node: () => postNode(p, i + 1) }));
  hourRows(steps).forEach((h) => rows.push({ m: h.h * 60, o: 2, node: () => hourNode(h) }));
  rows.sort((a, b) => a.m - b.m || a.o - b.o);
  if (S.editing) ol.append(addRow(-1, rows[0] ? rows[0].m : 9 * 60));
  rows.forEach((r, i) => {
    const li = r.node(); li.dataset.m = r.m; ol.append(li);
    if (S.editing && r.o !== 2) { const nx = rows.slice(i + 1).find((x) => x.o !== 2); ol.append(addRow(r.m, nx ? nx.m : null)); }
  });
  $("#empty").hidden = !S.hgAt || ps.length > 0 || S.editing;
  $("#empty-t").textContent = (S.day === today() ? "今天" : dayLabel(S.day)) + "还没有排帖子。在发帖排期表里写好，这里会自动出现。";
  mark();
}

/* 当前时段高亮：从这一行开始，到下一行之前 */
function mark() {
  const t = today(), n = nowMin();
  const lis = [...document.querySelectorAll("#tl > .step")];
  lis.forEach((li, i) => {
    const m = Number(li.dataset.m), next = i + 1 < lis.length ? Number(lis[i + 1].dataset.m) : DAY_END;
    li.classList.toggle("now", S.day === t && n >= m && n < next && !S.editing);
    li.classList.toggle("past", !S.editing && ((S.day === t && n >= next) || S.day < t));
  });
}

function stepNode(s) {
  const li = el("li", `step k-${s.kind}` + (s.custom ? " custom" : ""));
  const m = s.mode === "repeat" ? s.from : s.t;
  const t = el("div", "time mono", fmt(m)); if (s.idle) t.append(el("small", null, "挂机"));
  const ax = el("div", "axis"); const dot = el("span", "dot"); ax.append(dot);
  const c = el("div", "card");
  const h = el("h3", null, s.title + " ");
  if (s.mode === "repeat") h.append(el("span", "dur", `每 ${everyText(s.every)}，${fmt(s.from)}–${fmt(s.to)} · 一天 ${occurrences(s).length} 次`));
  else if (s.dur) h.append(el("span", "dur", s.dur));
  if (s.action.type === "xrf") h.append(el("span", "dur", `${s.action.count} 条 · ${s.action.tab}`), el("span", "chip xrf", "自动回复流程"));
  h.append(el("span", "rep", daysText(s.days)));
  if (s.custom) h.append(el("span", "mine", "我加的"));
  const run = s.action.type === "xrf" ? runOf(s) : null;
  if (run) h.append(el("span", "chip " + run.cls, run.label));
  h.append(el("span", "nowtag", "现在"));
  if (S.editing) {
    const ops = el("span", "ops");
    ops.append(btn("编辑", null, () => openDlg(s)), btn("删除", "danger", () => removeStep(s)));
    h.append(ops);
  }
  c.append(h);
  if (s.items.length) { const ul = el("ul"); s.items.forEach((x) => ul.append(el("li", null, x))); c.append(ul); }
  if (s.action.type === "xrf" && !S.editing) xrfBody(c, s, run);
  else {
    const f = el("div", "foot");
    if (s.tool) f.append(el("span", "tool", s.tool));
    if (!S.editing) actionButton(f, s);
    if (f.childNodes.length) c.append(f);
  }
  li.append(t, ax, c);
  if (!S.editing && s.mode === "once") bindDrag(li, s, t, ax, dot);
  return li;
}
function actionButton(f, s) {
  const a = s.action;
  if (a.type === "sched") f.append(btn("打开发帖排期表 ↗", null, ACT.sched));
  if (a.type === "inbox") {
    f.append(btn("打开待回复评论 ↗", null, ACT.inbox));
    if (S.hgAt) { const lv = el("span", "live", "待回复 "); lv.append(el("b", null, String(H("inboxTodo", 0))), " 条"); f.append(lv); }
  }
  if (a.type === "pool") f.append(btn("打开互关池，按 F 关注 ↗", null, ACT.pool));
  if (a.type === "url" && a.url) {
    const link = el("a", null, "打开 ↗"); link.href = /^https?:\/\//.test(a.url) ? a.url : "https://" + a.url; link.target = "_blank"; link.rel = "noopener";
    const b = btn("", null); b.append(link); b.addEventListener("click", (e) => { if (e.target === b) link.click(); }); f.append(b);
  }
}

/* ---------- 自动回复步骤：还没开始 / 运行中 / 已完成 ---------- */
function runOf(s) {
  const r = (S.runs[S.day] || {})[s.id];
  if (!r) return S.day === today() ? { cls: "idle", label: "还没开始" } : null;
  const st = (S.xrf && S.xrf.status) || {};
  if (r.active && xrfOk() && st.active) return { cls: "running", label: st.paused ? "⏸ 已暂停" : "● 运行中", r, st };
  if (r.active && !xrfOk()) return { cls: "idle", label: "状态未知", r };
  if (r.error && !r.endAt) return { cls: "fail", label: "没开始成功", r };
  if (!r.active && !r.endAt && Date.now() - r.startAt < 90000) return { cls: "running", label: "正在开始…", r, st: { step: "正在打开 X 页面", done: 0, max: 0 } };
  if (/已停止/.test(r.endStep || "")) return { cls: "idle", label: "⏹ 中途停止", r };
  if (/出错/.test(r.endStep || "")) return { cls: "fail", label: "出错停止", r };
  return { cls: "done", label: "✓ 已完成", r };
}
function logsOf(r, n) {
  const logs = ((S.xrf && S.xrf.logs) || []).filter((l) => l.t >= r.startAt - 2000 && (!r.endAt || l.t <= r.endAt + 5000));
  return n ? logs.slice(-n) : logs;
}
function logList(logs) {
  const ol = el("ol", "logs");
  [...logs].reverse().forEach((l) => { const d = new Date(l.t); ol.append(el("li", l.level || "", `${fmt(d.getHours() * 60 + d.getMinutes())} ${l.text}`)); });
  if (!logs.length) ol.append(el("li", null, "这一轮没有日志。"));
  return ol;
}
function xrfBody(c, s, run) {
  const f = el("div", "foot");
  if (run && run.cls === "running") {
    const st = run.st, max = st.max || s.action.count, done = st.done || 0;
    const bar = el("div", "bar"); const i = el("i"); i.style.width = Math.min(100, Math.round((done / Math.max(1, max)) * 100)) + "%"; bar.append(i);
    const line = el("div", "runline", "已回复 "); line.append(el("b", null, `${done} / ${max}`), ` · 跳过 ${st.failed || 0} 条 · 现在：${st.step || ""}`);
    c.append(bar, line, logList(logsOf(run.r, 3)));
    f.append(btn(st.paused ? "▶ 继续" : "⏸ 暂停", null, () => send("xrf", { type: st.paused ? "xrf-resume" : "xrf-pause" })),
      btn("⏹ 停止", null, () => send("xrf", { type: "xrf-stop" })), btn("切到那个 X 页面 ↗", null, () => send("xrf", { type: "xrf-focus" })));
  } else if (run && run.r) {
    const r = run.r;
    const line = el("div", "runline");
    if (run.cls === "fail") line.append(`没开始成功：${r.error || "X 页面没响应"}`);
    else { line.append("成功 ", el("b", null, String(r.done || 0)), " 条，跳过 ", el("b", null, String(r.failed || 0)), ` 条 · ${fmt(minOf(r.startAt))} 开始` + (r.endAt ? `，${fmt(minOf(r.endAt))} 结束` : "")); }
    if (r.endStep) line.append(el("span", "live", ` · ${r.endStep}`));
    c.append(line);
    const open = S.openLogs.has(s.id);
    f.append(btn(open ? "收起日志" : "查看这轮日志", null, () => { open ? S.openLogs.delete(s.id) : S.openLogs.add(s.id); renderTimeline(); }));
    if (S.day === today()) f.append(btn("再跑一轮", null, () => startXrf(s)));
    if (open) c.append(logList(logsOf(r)));
  } else {
    if (S.day === today()) f.append(btn(`▶ 开始自动回复（${s.action.count} 条）`, "primary", () => startXrf(s)));
    if (!s.items.length) c.append(el("div", "autoline", "到点前把一个 X 页面开着（最小化也行）。"));
  }
  if (S.xrf && S.day === today()) { const lv = el("span", "live", "今天自动回复 "); lv.append(el("b", null, String(S.xrf.todayDone || 0)), " 条"); f.append(lv); }
  c.append(f);
  if (s.action.auto) c.append(el("div", "autoline", `⏰ ${fmt(s.t)} 自动开始（Chrome 要开着）`));
}
const minOf = (t) => { const d = new Date(t); return d.getHours() * 60 + d.getMinutes(); };
async function startXrf(s) {
  const r = await send("xrf", { type: "xrf-start", count: s.action.count, tab: s.action.tab, stepId: s.id, date: today() });
  if (r && r.ok) { $("#err").hidden = true; }
}

/* ---------- 帖子卡片 ---------- */
function postNode(p, n) {
  const li = el("li", "step post k-post");
  const [st, stTxt] = postState(S.day, p);
  if (st === "miss") li.classList.add("missed");
  const t = el("div", "time mono", p.hm);
  const ax = el("div", "axis"); const dot = el("span", "dot"); dot.title = "来自发帖排期表，去排期表里改时间"; ax.append(dot);
  const c = el("div", "card");
  const h = el("h3", null, `第 ${n} 条帖子 `);
  h.append(el("span", "chip src", "排期表"), el("span", "chip " + st, stTxt), el("span", "nowtag", "现在"));
  const tw = el("div", "tw");
  tw.append(el("p", null, (p.text || "").trim() || "（只有图片或视频）"));
  tw.append(el("small", null, (p.vid ? "视频" : p.imgs ? `${p.imgs} 张图` : "纯文字") + (p.status === "xsched" ? " · 到点由 X 自己发，电脑关了也会发" : p.status === "sent" ? " · 已经发出" : "")));
  const fl = el("div", "follow", `↳ ${fmt(Math.min(DAY_END - 1, p.m + FOLLOW_AFTER))} 开始回这条的评论，发出后 30 分钟内回完`);
  const f = el("div", "foot");
  const future = S.day > today() || (S.day === today() && p.m > nowMin() + 2);
  if (!p.status && future) f.append(btn("交给 X 定时", "primary", () => send("hg", { type: "xfb-sched-x", date: S.day, time: p.hm })));
  if (st === "miss") f.append(btn("现在发", "primary", () => send("hg", { type: "xfb-sched-open", date: S.day, time: p.hm })));
  f.append(btn("在排期表里改 ↗", null, ACT.sched));
  c.append(h, tw, fl, f);
  li.append(t, ax, c);
  return li;
}

/* ---------- 每小时一行：关注轮次 + 重复动作（合并） ---------- */
function hourRows(steps) {
  const map = new Map();
  const get = (h) => { if (!map.has(h)) map.set(h, { h, reps: [] }); return map.get(h); };
  steps.filter((s) => s.mode === "repeat").forEach((s) => {
    const byHour = {};
    occurrences(s).forEach((m) => { (byHour[Math.floor(m / 60)] ||= []).push(m); });
    Object.entries(byHour).forEach(([h, ms]) => get(Number(h)).reps.push({ s, ms }));
  });
  return [...map.values()].sort((a, b) => a.h - b.h);
}
function hourNode(row) {
  const li = el("li", "step hour k-follow");
  const t = el("div", "time mono", fmt(row.h * 60));
  const ax = el("div", "axis"); ax.append(el("span", "dot"));
  const c = el("div", "card");
  const isToday = S.day === today(), n = nowMin(), dayStart = new Date(S.day + "T00:00:00").getTime();
  row.reps.filter((x) => x.s.follow).forEach(({ s, ms }) => {
    /* 关注新人：每一格这段时间关注了几个人（回关助手记的关注时间），自动统计，不用打勾 */
    const g = el("div", "grp k-follow");
    g.append(el("span", "gname", s.title), el("span", "gsub", `每 ${everyText(s.every)}一轮，一轮 ${H("roundTarget", 15)} 个`));
    const times = H("followTimes", []) || [];
    ms.forEach((m) => {
      const a = dayStart + m * 60000, b = a + s.every * 60000;
      const cnt = times.filter((x) => x >= a && x < b).length;
      let cls = "slot", txt = fmt(m);
      if (isToday && n >= m && n < m + s.every) { cls += " cur"; txt += cnt ? ` · 已关 ${cnt}` : " · 现在"; }
      else if ((isToday && n >= m + s.every) || S.day < today()) { if (cnt) { cls += " ok"; txt += ` ✓ ${cnt}`; } else cls += " skip"; }
      g.append(el("span", cls, txt));
    });
    c.append(g);
  });
  row.reps.filter((x) => !x.s.follow).forEach(({ s, ms }) => {
    const g = el("div", `grp k-${s.kind}`);
    g.append(el("span", "gname", s.title), el("span", "gsub", `每 ${everyText(s.every)}` + (s.dur ? `，约 ${s.dur}` : "")));
    const done = ((S.done[S.day] || {})[s.id]) || [];
    ms.forEach((m) => {
      const isDone = done.includes(m);
      let cls = "slot", txt = fmt(m);
      const cur = isToday && n >= m && n < m + s.every;
      const past = (isToday && n >= m + s.every) || S.day < today();
      const can = S.day < today() || (isToday && n >= m);
      if (isDone) { cls += " ok"; txt += " ✓"; }
      else if (cur) { cls += " cur"; txt += " · 现在，做完点一下"; }
      else if (past) cls += " skip";
      if (can) {
        const b = btn(txt, cls, () => toggleDone(s.id, m));
        b.title = isDone ? "点一下取消打勾" : "做完了点一下打勾";
        b.setAttribute("aria-pressed", String(isDone));
        g.append(b);
      } else g.append(el("span", cls, txt));
    });
    c.append(g);
  });
  li.append(t, ax, c);
  return li;
}
async function toggleDone(id, m) {
  const d = (S.done[S.day] ||= {}); const arr = (d[id] ||= []);
  const i = arr.indexOf(m); i >= 0 ? arr.splice(i, 1) : arr.push(m);
  const cut = dateKey(new Date(Date.now() - 14 * 864e5));
  for (const k of Object.keys(S.done)) if (k < cut) delete S.done[k];
  await chrome.storage.local.set({ wfDone: S.done });
}

/* ---------- 编辑模式：加、改、删 ---------- */
function addRow(m, next) {
  const li = el("li", "addrow");
  li.append(el("span"), el("span"), btn("＋ 在这里加一步", null, () => {
    let t = m < 0 ? Math.max(0, (next == null ? 9 * 60 : next) - 30) : next == null ? Math.min(DAY_END - 5, m + 30) : Math.round((m + next) / 2);
    openDlg(null, clampMin(t));
  }));
  return li;
}
let undoT = 0, undoFn = null;
function toast(text, undo) {
  $("#toast-t").textContent = text; $("#toast").hidden = false; undoFn = undo;
  clearTimeout(undoT); undoT = setTimeout(() => { $("#toast").hidden = true; undoFn = null; }, 8000);
}
async function removeStep(s) {
  const i = S.steps.findIndex((x) => x.id === s.id);
  if (i < 0) return;
  const before = S.steps.slice();
  S.steps.splice(i, 1);
  await saveSteps(); renderTimeline();
  toast(`已删除「${s.title}」`, async () => { S.steps = before; await saveSteps(); renderTimeline(); });
}

/* 编辑面板 */
const D = { step: null, mode: "once", kind: "other", days: [...ALL_DAYS] };
function setupDlg() {
  const ev = $("#d-every");
  EVERY_CHOICES.forEach((m) => ev.append(new Option(everyText(m), String(m))));
  ev.append(new Option("自己填几分钟…", "custom"));
  ev.addEventListener("change", () => { const c = ev.value === "custom"; $("#d-every-n").hidden = !c; $("#d-every-u").hidden = !c; preview(); });
  ["d-every-n", "d-from", "d-to"].forEach((id) => $("#" + id).addEventListener("input", preview));
  const kinds = $("#d-kinds");
  Object.entries(KINDS).forEach(([k, name]) => { const b = btn(name, "k-" + k, () => { D.kind = k; paintDlg(); }); b.dataset.k = k; kinds.append(b); });
  const days = $("#d-days");
  [1, 2, 3, 4, 5, 6, 0].forEach((d) => {
    const b = btn("日一二三四五六"[d], null, () => { D.days = D.days.includes(d) ? D.days.filter((x) => x !== d) : [...D.days, d]; paintDlg(); });
    b.dataset.d = d; b.setAttribute("aria-label", "周" + "日一二三四五六"[d]); days.append(b);
  });
  const act = $("#d-action");
  Object.entries(ACTIONS_LIST).forEach(([k, name]) => act.append(new Option(name, k)));
  act.addEventListener("change", paintDlg);
  XRF_TABS.forEach((t) => $("#d-tab").append(new Option(t, t)));
  $("#d-once").addEventListener("click", () => { D.mode = "once"; paintDlg(); });
  $("#d-rep").addEventListener("click", () => { D.mode = "repeat"; paintDlg(); });
  $("#d-title").addEventListener("input", paintDlg);
  for (const id of ["#d-x", "#d-cancel", "#shade"]) $(id).addEventListener("click", closeDlg);
  $("#d-save").addEventListener("click", saveDlg);
  $("#d-del").addEventListener("click", () => { const s = D.step; closeDlg(); if (s) removeStep(s); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#dlg").hidden) closeDlg(); });
}
function openDlg(s, t) {
  const n = s || normStep({ title: "", kind: "other", t: t != null ? t : 9 * 60, from: t != null ? t : 10 * 60, to: Math.min(DAY_END - 5, (t != null ? t : 10 * 60) + 12 * 60), custom: true, dur: "" });
  D.step = s; D.mode = n.mode; D.kind = n.kind; D.days = [...n.days]; D.follow = !!n.follow;
  $("#dlg-t").textContent = s ? "编辑步骤" : "加一步";
  $("#d-title").value = s ? n.title : "";
  $("#d-t").value = fmt(n.t); $("#d-dur").value = n.dur; $("#d-dur2").value = n.dur;
  const ev = $("#d-every");
  if (EVERY_CHOICES.includes(n.every)) { ev.value = String(n.every); $("#d-every-n").hidden = true; $("#d-every-u").hidden = true; }
  else { ev.value = "custom"; $("#d-every-n").hidden = false; $("#d-every-u").hidden = false; }
  $("#d-every-n").value = String(n.every);
  $("#d-from").value = fmt(n.from); $("#d-to").value = fmt(n.to);
  $("#d-items").value = n.items.join("\n");
  $("#d-tool").value = n.tool;
  $("#d-action").value = n.action.type;
  $("#d-url").value = n.action.url || "";
  $("#d-count-n").value = String(n.action.count || 30);
  $("#d-tab").value = n.action.tab || XRF_TABS[0];
  $("#d-auto").checked = !!n.action.auto;
  $("#d-idle").checked = n.idle; $("#d-notify").checked = n.notify;
  $("#d-del").hidden = !s; $("#d-err").hidden = true;
  $("#shade").hidden = false; $("#dlg").hidden = false;
  paintDlg();
  $("#d-title").focus();
}
function closeDlg() { $("#shade").hidden = true; $("#dlg").hidden = true; D.step = null; }
function paintDlg() {
  $("#d-seg").hidden = D.follow; $("#d-kindbox").hidden = D.follow; $("#d-actionbox").hidden = D.follow; $("#d-follownote").hidden = !D.follow;
  document.querySelectorAll("#d-kinds button").forEach((b) => { b.hidden = b.dataset.k === "follow"; });
  $("#d-once").setAttribute("aria-pressed", String(D.mode === "once"));
  $("#d-rep").setAttribute("aria-pressed", String(D.mode === "repeat"));
  $("#d-oncebox").hidden = D.mode !== "once"; $("#d-repbox").hidden = D.mode !== "repeat";
  document.querySelectorAll("#d-kinds button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.k === D.kind)));
  document.querySelectorAll("#d-days button").forEach((b) => b.setAttribute("aria-pressed", String(D.days.includes(Number(b.dataset.d)))));
  const a = $("#d-action").value;
  $("#d-urlbox").hidden = a !== "url"; $("#d-xrfbox").hidden = a !== "xrf";
  $("#d-idlebox").hidden = D.mode === "repeat";
  const name = $("#d-title").value.trim() || "这一步";
  $("#d-notify-t").textContent = D.mode === "repeat" ? `每次到点都弹提醒「该做了：${name}」` : `到点弹提醒「该做了：${name}」`;
  preview();
}
function readEvery() { const v = $("#d-every").value; return v === "custom" ? Math.round(Number($("#d-every-n").value) || 0) : Number(v); }
function preview() {
  if (D.mode !== "repeat") return;
  const every = readEvery(), from = toMin($("#d-from").value), to = toMin($("#d-to").value);
  const box = $("#d-chips"); box.textContent = "";
  if (!(every >= 5) || to < from) { $("#d-count").textContent = every < 5 ? "间隔至少 5 分钟。" : "结束时间要晚于开始时间。"; return; }
  const occ = occurrences({ mode: "repeat", every, from, to });
  $("#d-count").textContent = `一天 ${occ.length} 次：`;
  const show = occ.length > 10 ? [...occ.slice(0, 7), null, ...occ.slice(-2)] : occ;
  show.forEach((m) => box.append(el("span", null, m == null ? "…" : fmt(m))));
}
async function saveDlg() {
  const err = (t) => { $("#d-err").textContent = t; $("#d-err").hidden = false; };
  const title = $("#d-title").value.trim();
  if (!title) return err("名称不能空着。");
  if (!D.days.length) return err("至少选一天。");
  const a = $("#d-action").value;
  if (a === "url" && !$("#d-url").value.trim()) return err("填一下要打开的网址。");
  if (D.mode === "repeat") {
    const every = readEvery(), from = toMin($("#d-from").value), to = toMin($("#d-to").value);
    if (!(every >= 5)) return err("间隔至少 5 分钟。");
    if (to < from) return err("结束时间要晚于开始时间。");
  }
  const old = D.step || {};
  const n = normStep({
    ...old, title, kind: D.kind, mode: D.mode, days: D.days,
    t: toMin($("#d-t").value || fmt(old.t || 540)),
    dur: D.mode === "repeat" ? $("#d-dur2").value : $("#d-dur").value,
    every: readEvery(), from: toMin($("#d-from").value), to: toMin($("#d-to").value),
    items: $("#d-items").value.split("\n"), tool: $("#d-tool").value,
    action: { type: a, url: $("#d-url").value, count: Number($("#d-count-n").value), tab: $("#d-tab").value, auto: $("#d-auto").checked },
    idle: D.mode === "once" && $("#d-idle").checked, notify: $("#d-notify").checked, custom: D.step ? old.custom : true,
  });
  if (D.step) S.steps = S.steps.map((x) => (x.id === n.id ? n : x)); else S.steps.push(n);
  await saveSteps();
  closeDlg(); renderTimeline();
  if (!onDay(n, S.day)) toast(`已保存。「${n.title}」${daysText(n.days)}才做，所以${S.day === today() ? "今天" : "这天"}不显示`, null);
}

/* ---------- 拖动只做一次的步骤 ---------- */
function bindDrag(li, s, timeEl, ax, dot) {
  li.classList.add("drag-ok");
  dot.tabIndex = 0; dot.setAttribute("role", "slider");
  dot.setAttribute("aria-label", `${s.title}，${fmt(s.t)}，按上下键调整`);
  let bubble = null, startY = 0, startM = 0, cur = s.t;
  const setTime = (m) => {
    cur = m; timeEl.firstChild.nodeValue = fmt(m);
    if (!bubble) { bubble = el("span", "bubble mono"); ax.append(bubble); }
    bubble.textContent = fmt(m);
  };
  const hide = () => { if (bubble) bubble.remove(); bubble = null; };
  const commit = async () => { if (cur === s.t) return; s.t = cur; S.steps = S.steps.map((x) => (x.id === s.id ? { ...x, t: cur } : x)); await saveSteps(); };
  dot.addEventListener("pointerdown", (e) => { e.preventDefault(); dot.setPointerCapture(e.pointerId); S.dragging = true; startY = e.clientY; startM = s.t; dot.classList.add("drag"); setTime(startM); });
  dot.addEventListener("pointermove", (e) => { if (dot.classList.contains("drag")) setTime(clampMin(startM + (e.clientY - startY) / PX_PER_MIN)); });
  const end = async () => { if (!dot.classList.contains("drag")) return; dot.classList.remove("drag"); hide(); S.dragging = false; await commit(); renderTimeline(); };
  dot.addEventListener("pointerup", end); dot.addEventListener("pointercancel", end);
  dot.addEventListener("keydown", (e) => {
    const d = e.key === "ArrowUp" ? -DRAG_STEP : e.key === "ArrowDown" ? DRAG_STEP : 0;
    if (!d) return;
    e.preventDefault(); setTime(clampMin(cur + d)); commit();
  });
  dot.addEventListener("blur", () => { if (!S.dragging && bubble) { hide(); renderTimeline(); } });
  dot.addEventListener("mouseenter", () => { if (!bubble) setTime(cur); });
  dot.addEventListener("mouseleave", () => { if (!dot.classList.contains("drag") && document.activeElement !== dot) hide(); });
}

/* ---------- 启动 + 实时同步 ---------- */
function render() { renderDays(); renderTop(); renderPool(); renderTimeline(); }
function setEditing(on) {
  S.editing = on;
  $("#editbar").hidden = !on; $("#hint").hidden = on;
  renderTimeline();
}
function flash(id) { const st = $(id); st.classList.remove("flash"); void st.offsetWidth; st.classList.add("flash"); setTimeout(() => st.classList.remove("flash"), 2200); }
async function start() {
  $("#open-sched").addEventListener("click", ACT.sched);
  $("#empty-go").addEventListener("click", ACT.sched);
  $("#open-pool").addEventListener("click", ACT.pool);
  $("#edit-on").addEventListener("click", () => setEditing(true));
  $("#edit-off").addEventListener("click", () => setEditing(false));
  $("#restore").addEventListener("click", async () => {
    const before = S.steps.slice();
    S.steps = DEFAULT_STEPS.map((s) => normStep(s));
    await saveSteps(); renderTimeline();
    toast("已恢复默认步骤", async () => { S.steps = before; await saveSteps(); renderTimeline(); });
  });
  $("#toast-undo").addEventListener("click", async () => { const f = undoFn; $("#toast").hidden = true; undoFn = null; if (f) await f(); });
  $("#notify").addEventListener("change", (e) => chrome.storage.local.set({ wfNotify: e.target.checked }));
  setupDlg();
  await load();
  $("#notify").checked = S.notify;
  render();
  chrome.runtime.sendMessage({ type: "pull" }).catch(() => {});      // 打开页面时让另外两个插件马上发一次最新数据
  let lastSched = JSON.stringify(H("schedule", {}));
  chrome.storage.onChanged.addListener(async (ch, area) => {
    if (area !== "local" || !["snap", "xsnap", "wfSteps", "wfDone", "wfRuns", "wfNotify"].some((k) => ch[k])) return;
    if (S.dragging) return;          // 正在拖动时不打断，松手后会重新画
    await load();
    if (!$("#dlg").hidden) { renderTop(); renderPool(); return; }   // 编辑面板开着时不重画时间线
    render();
    const now = JSON.stringify(H("schedule", {}));
    if (now !== lastSched) { lastSched = now; flash("#st-hg"); }
  });
  setInterval(() => {
    if (S.dragging || !$("#dlg").hidden) return;
    if (S.day < today()) S.day = today();    // 过了零点：跳到新的一天
    render();
  }, 30000);
}
start();
