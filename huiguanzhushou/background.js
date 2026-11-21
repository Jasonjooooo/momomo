/* 回关助手 · 后台：定时提醒去找互关帖（X 页面顶上提示条 + 图标倒计时）、发帖排期。不会替你关注任何人。 */
"use strict";
try { importScripts("media-db.js"); } catch (e) { /* 忽略 */ }

const DEF = { remindOn: true, remindMin: 15, minReplies: 150, keywords: "蓝V互关" };
const ALARM = "xfb-remind";

async function cfg() {
  const v = await chrome.storage.local.get(Object.keys(DEF));
  return { ...DEF, ...v };
}

function searchUrl(c) {
  const first = String(c.keywords || DEF.keywords).split(/[,，]/).map((s) => s.trim()).filter(Boolean)[0] || DEF.keywords;
  const tag = /^[#＃]/.test(first) ? first : "#" + first;
  const q = c.minReplies > 0 ? `${tag} min_replies:${c.minReplies}` : tag;
  return "https://x.com/search?q=" + encodeURIComponent(q) + "&src=typed_query&f=live";
}

/* 倒计时只跑一次：到点提醒后就停住；你开始关注后按人数算一轮，关满（默认 15 个）才重新开始倒数 */
const WORK_MAX_MIN = 60;     // 进行中超过 1 小时还没关满，自动开始下一轮
async function arm(delayMin) {
  const c = await cfg();
  await chrome.alarms.clear(ALARM);
  if (c.remindOn) {
    const m = Math.max(1, Number(c.remindMin) || 15);
    const d = delayMin || m;
    await chrome.alarms.create(ALARM, { delayInMinutes: d });
    await chrome.storage.local.set({ remindNextAt: Date.now() + d * 60000 });
  } else {
    await chrome.storage.local.set({ remindDue: 0, remindWorking: 0, remindNextAt: 0 });
  }
  updateBadge();
}
/* 一轮结束（关满了 / 点「这轮够了」/ 跳过）：从现在开始重新倒数 */
async function roundDone() {
  await chrome.storage.local.set({ remindDue: 0, remindWorking: 0, roundCount: 0 });
  await arm();
}
/* 开始一轮（点了「打开下一条」）：倒计时暂停，图标显示 ▶ 和这一轮已关注几个 */
async function roundStart() {
  const v = await chrome.storage.local.get(["remindWorking"]);
  await chrome.alarms.clear(ALARM);
  if (v.remindWorking) { await chrome.storage.local.set({ remindDue: 0 }); updateBadge(); return; }   // 已经在一轮里了，接着算
  await chrome.storage.local.set({ remindDue: 0, remindWorking: Date.now(), roundCount: 0, remindNextAt: 0 });
  updateBadge();
}
/* 关注了一个人 */
async function roundFollow() {
  const v = await chrome.storage.local.get(["remindWorking", "remindDue", "roundCount", "roundTarget", "remindOn"]);
  if (v.remindOn === false) return;
  if (!v.remindWorking) {
    if (!v.remindDue) return;                     // 还在倒数中，不算
    await roundStart();                           // 到点后直接开始关注，也算开始了一轮
  }
  const n = (Number(v.remindWorking ? v.roundCount : 0) || 0) + 1;
  const target = Math.max(1, Number(v.roundTarget) || 15);
  if (n >= target) { await roundDone(); return; }
  await chrome.storage.local.set({ roundCount: n, remindWorking: Date.now() });   // 每关注一个，「超时」重新算
  updateBadge();
}

/* ---------- 工具栏图标：一直显示离下次提醒还有几分钟；到点变红色「!」；排期表到点发帖时显示蓝色「发」 ---------- */
async function updateBadge() {
  try {
    const v = await chrome.storage.local.get(["remindDue", "schedBadge", "remindOn", "remindWorking", "roundCount", "roundTarget"]);
    if (v.schedBadge) {
      await chrome.action.setBadgeBackgroundColor({ color: "#1d9bf0" });
      await chrome.action.setBadgeText({ text: "发" });
      return;
    }
    if (v.remindDue) {
      await chrome.action.setBadgeBackgroundColor({ color: "#f4212e" });
      await chrome.action.setBadgeText({ text: "!" });
      await chrome.action.setTitle({ title: "回关助手：该找互关帖了，点这里打开下一条" });
      return;
    }
    if (v.remindWorking && v.remindOn !== false) {
      await chrome.action.setBadgeBackgroundColor({ color: "#1d9bf0" });
      const n = Number(v.roundCount) || 0, target = Number(v.roundTarget) || 15;
      await chrome.action.setBadgeText({ text: "▶" + n });
      await chrome.action.setTitle({ title: `回关助手：这一轮已关注 ${n} / ${target} 个，关满就开始下一轮倒计时` });
      return;
    }
    const a = v.remindOn === false ? null : await chrome.alarms.get(ALARM);
    if (!a) { await chrome.action.setBadgeText({ text: "" }); await chrome.action.setTitle({ title: "回关助手：打开最新的互关帖" }); return; }
    const left = Math.max(1, Math.round((a.scheduledTime - Date.now()) / 60000));
    await chrome.action.setBadgeBackgroundColor({ color: "#5c6b78" });
    await chrome.action.setBadgeText({ text: String(left) });
    await chrome.action.setTitle({ title: `回关助手：${left} 分钟后提醒你找互关帖。点这里现在就打开下一条` });
  } catch (e) { /* 忽略 */ }
}

chrome.runtime.onInstalled.addListener(() => arm());
chrome.runtime.onStartup.addListener(() => arm());

/* ================= 发帖排期表 ================= */
const SCHED_ALARM = "xfb-sched";
const SCHED_WINDOW = 10;     // 到点后 10 分钟内提醒；过了就算错过，不再弹
const pad = (n) => String(n).padStart(2, "0");
const dateKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const toMin = (hm) => { const [h, m] = String(hm).split(":").map(Number); return h * 60 + m; };
const hasContent = (it) => !!(it && ((it.text && it.text.trim()) || it.imgs > 0));

function armSched() {
  chrome.alarms.get(SCHED_ALARM, (a) => {
    if (!a) chrome.alarms.create(SCHED_ALARM, { delayInMinutes: 0.1, periodInMinutes: 1 });
  });
}
chrome.runtime.onInstalled.addListener(armSched);
chrome.runtime.onStartup.addListener(armSched);
armSched();

async function checkSchedule() {
  const v = await chrome.storage.local.get(["schedule", "schedSettings"]);
  const set = v.schedSettings || {};
  if (set.on === false) return;
  const sched = v.schedule || {};
  const now = new Date();
  const day = dateKey(now);
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const items = sched[day] || {};
  const due = Object.keys(items).sort().filter((hm) => {
    const it = items[hm];
    const diff = nowMin - toMin(hm);
    return hasContent(it) && !it.status && diff >= 0 && diff < SCHED_WINDOW;
  });
  if (!due.length) return;
  const hm = due[0];
  items[hm].status = "reminded";
  sched[day] = items;
  await chrome.storage.local.set({ schedule: sched });
  const text = (items[hm].text || "").trim().replace(/\s+/g, " ") || "（只有图片）";
  const pics = items[hm].vid ? "［视频］" : items[hm].imgs ? `［${items[hm].imgs} 张图］` : "";
  await chrome.storage.local.set({ schedBadge: true });
  updateBadge();
  chrome.notifications.create(`xfb-sched|${day}|${hm}`, {
    type: "basic",
    iconUrl: "icon128.png",
    title: `该发帖了 · ${hm}`,
    message: pics + (text.length > 90 ? text.slice(0, 90) + "…" : text) + "\n点这里打开发帖框，内容" + (items[hm].vid ? "和视频" : pics ? "和图片" : "") + "会自动填好。",
    priority: 2,
    requireInteraction: true,
  });
}

/* 打开 X 的发帖框，内容填好。用户自己点「发帖」 */
async function openSlot(day, hm) {
  const v = await chrome.storage.local.get(["schedule"]);
  const sched = v.schedule || {};
  const it = sched[day] && sched[day][hm];
  if (!hasContent(it)) return;
  if (it.status !== "sent") it.status = "opened";
  await chrome.storage.local.set({ schedule: sched, schedOpen: { date: day, time: hm, text: it.text || "", imgs: it.imgs || 0, vid: !!it.vid, at: Date.now() } });
  await chrome.storage.local.set({ schedBadge: false });
  updateBadge();
  const url = "https://x.com/intent/post?text=" + encodeURIComponent(it.text || "");
  const w = await chrome.windows.getLastFocused().catch(() => null);
  await chrome.tabs.create({ url, ...(w ? { windowId: w.id } : {}) });
  if (w) chrome.windows.update(w.id, { focused: true });
}

/* 交给 X 自带的定时发帖：打开发帖框，内容填好，由 content.js 帮你把日期时间选好，你点「安排」 */
async function scheduleOnX(day, hm, batch) {
  const v = await chrome.storage.local.get(["schedule"]);
  const sched = v.schedule || {};
  const it = sched[day] && sched[day][hm];
  if (!hasContent(it)) return false;
  const st = (await chrome.storage.local.get(["schedSettings"])).schedSettings || {};
  await chrome.storage.local.set({ schedJob: { date: day, time: hm, text: it.text || "", imgs: it.imgs || 0, vid: !!it.vid, at: Date.now(), batch: !!batch, auto: st.autoArrange !== false } });
  /* 用 X 普通的发帖框（不是带文字的分享链接）：分享链接打开的框，去「安排表」再回来会被重置，定时和图片都会丢。
   * 文字由 content.js 填进去 */
  const url = "https://x.com/compose/post";
  const tabs = await chrome.tabs.query({ url: ["https://x.com/intent/*", "https://x.com/compose/*"] });
  if (batch && tabs.length) {
    await chrome.tabs.update(tabs[0].id, { url, active: true });
  } else {
    const w = await chrome.windows.getLastFocused().catch(() => null);
    await chrome.tabs.create({ url, ...(w ? { windowId: w.id } : {}) });
    if (w) chrome.windows.update(w.id, { focused: true });
  }
  return true;
}

/* 下一条还没交给 X 的（时间要在 3 分钟以后，X 不让定时到过去） */
async function nextForX(day) {
  const v = await chrome.storage.local.get(["schedule"]);
  const items = (v.schedule || {})[day] || {};
  const now = new Date();
  const today = dateKey(now);
  const nowMin = now.getHours() * 60 + now.getMinutes();
  return Object.keys(items).sort().find((hm) => {
    const it = items[hm];
    if (!hasContent(it) || it.status) return false;
    return day > today || (day === today && toMin(hm) > nowMin + 2);
  }) || null;
}

/* 用户在 X 上点了「安排」 */
async function markSchedOnX(ok) {
  const v = await chrome.storage.local.get(["schedule", "schedJob"]);
  const job = v.schedJob;
  if (!job) return;
  const sched = v.schedule || {};
  const it = sched[job.date] && sched[job.date][job.time];
  if (ok && it) { it.status = "xsched"; it.xAt = Date.now(); }
  await chrome.storage.local.set({ schedule: sched, schedJob: null });
  if (ok && job.batch) {
    const next = await nextForX(job.date);
    if (next) setTimeout(() => scheduleOnX(job.date, next, true), 1500);
  }
}

/* 用户在发帖框点了「发帖」：把刚打开的那一格标成已发 */
async function markSchedSent(text) {
  const v = await chrome.storage.local.get(["schedule", "schedOpen"]);
  const o = v.schedOpen;
  if (!o || Date.now() - o.at > 60 * 60 * 1000) return;
  const norm = (x) => String(x || "").replace(/\s+/g, "").slice(0, 30);
  if (typeof text === "string" && norm(text) !== norm(o.text)) return;      // 发的不是排期表里这一条
  const sched = v.schedule || {};
  const it = sched[o.date] && sched[o.date][o.time];
  if (it) { it.status = "sent"; it.sentAt = Date.now(); }
  await chrome.storage.local.set({ schedule: sched, schedOpen: null });
}

/* ================= 待回复评论 =================
 * 抓取：开一个标签页 → 你的主页（找最近的主帖）→ 一条条打开主帖读评论 → 关掉
 * 回复：开 X 的「回复这条」页面（内容已填好）→ content.js 点发送 → 关掉、标成已回复 */
async function inboxFetch(fromTabId) {
  const v = await chrome.storage.local.get(["me"]);
  const me = v.me && v.me.handle;
  if (!me) return { ok: false, reason: "还不知道你的 X 账号。先打开一次 X 网页，再回来点抓取。" };
  const tab = await chrome.tabs.create({ url: `https://x.com/${me}`, active: true });
  await chrome.storage.local.set({
    inboxJob: { tabId: tab.id, back: fromTabId || null, me, phase: "profile", posts: [], idx: 0, added: 0, at: Date.now() },
    inboxProgress: { step: "profile" },
  });
  return { ok: true };
}
async function inboxGo(job) {
  const p = job.posts[job.idx];
  await chrome.storage.local.set({ inboxJob: job, inboxProgress: { step: "post", idx: job.idx + 1, total: job.posts.length, skipped: job.skipped || 0, text: p.text } });
  try { await chrome.tabs.update(job.tabId, { url: `https://x.com/${job.me}/status/${p.id}` }); } catch (e) { await inboxFinish(job); }
}
async function inboxFinish(job) {
  await chrome.storage.local.set({ inboxJob: null, inboxProgress: null, inboxFetchedAt: Date.now(), inboxLastAdded: job.added || 0, inboxLastPosts: job.posts.length, inboxLastSkipped: job.skipped || 0, inboxLastFound: job.found || 0 });
  try { await chrome.tabs.remove(job.tabId); } catch (e) { /* 已经关了 */ }
  if (job.back) { try { await chrome.tabs.update(job.back, { active: true }); } catch (e) { /* 忽略 */ } }
}
async function inboxPosts(tabId, posts) {
  const job = (await chrome.storage.local.get(["inboxJob"])).inboxJob;
  if (!job || job.tabId !== tabId || job.phase !== "profile") return;
  /* 扫过的做标记：回复数跟上次一样、而且上次把评论都看全了，才不再打开。
   * 上次没看全（看到的评论比回复数少，可能被 X 折叠了）就每次都重新打开，宁可慢一点也不漏 */
  const sc = (await chrome.storage.local.get(["inboxScanned"])).inboxScanned || {};
  const all = (posts || []).slice(0, 50);
  job.found = all.length;
  job.posts = all.filter((p) => (p.replies || 0) > 0 && !(sc[p.id] && sc[p.id].full && (p.replies || 0) <= sc[p.id].replies));
  job.skipped = all.length - job.posts.length;
  job.phase = "post"; job.idx = 0; job.at = Date.now();
  if (!job.posts.length) return inboxFinish(job);
  return inboxGo(job);
}
async function inboxNext(tabId, added, seenN, full) {
  const job = (await chrome.storage.local.get(["inboxJob"])).inboxJob;
  if (!job || job.tabId !== tabId || job.phase !== "post") return;
  job.added = (job.added || 0) + (added || 0);
  /* 这条扫完了：记下回复数，下次没涨就跳过 */
  const done = job.posts[job.idx];
  const sc = (await chrome.storage.local.get(["inboxScanned"])).inboxScanned || {};
  sc[done.id] = { replies: done.replies || 0, at: done.at, scannedAt: Date.now(), full: !!full, seen: seenN || 0 };
  const cut = Date.now() - 7 * 864e5;
  for (const k of Object.keys(sc)) if ((sc[k].at || 0) < cut) delete sc[k];
  await chrome.storage.local.set({ inboxScanned: sc });
  job.idx++; job.at = Date.now();
  if (job.idx >= job.posts.length) return inboxFinish(job);
  return inboxGo(job);
}
async function inboxReply(fromTabId, id, text) {
  if (!id || !String(text || "").trim()) return { ok: false, reason: "回复内容是空的。" };
  const url = "https://x.com/intent/post?in_reply_to=" + encodeURIComponent(id) + "&text=" + encodeURIComponent(text);
  const tab = await chrome.tabs.create({ url, active: true });
  await chrome.storage.local.set({ inboxSend: { tabId: tab.id, id, text, back: fromTabId || null, at: Date.now(), clicked: false } });
  return { ok: true };
}
async function inboxSent(tabId, ok, why) {
  const v = await chrome.storage.local.get(["inboxSend", "inbox", "replied"]);
  const job = v.inboxSend;
  if (!job || job.tabId !== tabId) return;
  if (ok) {
    const inbox = v.inbox || {}, replied = v.replied || {};
    if (inbox[job.id]) { inbox[job.id].state = "done"; inbox[job.id].doneAt = Date.now(); inbox[job.id].myReply = job.text; }
    replied[job.id] = Date.now();
    /* 你自己这条回复也会让帖子回复数 +1，记上，免得下次为它白打开一次 */
    const sc = (await chrome.storage.local.get(["inboxScanned"])).inboxScanned || {};
    const pid = inbox[job.id] && inbox[job.id].post;
    if (pid && sc[pid]) { sc[pid].replies = (sc[pid].replies || 0) + 1; await chrome.storage.local.set({ inboxScanned: sc }); }
    await chrome.storage.local.set({ inbox, replied, inboxSend: null, inboxSendResult: { id: job.id, ok: true, at: Date.now() } });
    try { await chrome.tabs.remove(tabId); } catch (e) { /* 忽略 */ }
    if (job.back) { try { await chrome.tabs.update(job.back, { active: true }); } catch (e) { /* 忽略 */ } }
  } else {
    /* 没发出去：留着那个页面让你自己看、自己发 */
    await chrome.storage.local.set({ inboxSend: null, inboxSendResult: { id: job.id, ok: false, why: why || "", at: Date.now() } });
  }
}
function openInboxPage() {
  chrome.tabs.create({ url: chrome.runtime.getURL("inbox.html") });
}

/* ================= 热点雷达 ================= */
function openHotPage() {
  chrome.tabs.create({ url: chrome.runtime.getURL("hot.html") });
}
/* 抓取一次：开一个 X 搜索页（热门，最近 N 小时），content.js 往下翻着收，收完关掉 */
async function hotFetch(fromTabId) {
  const v = await chrome.storage.local.get(["hotQuery", "hotAge"]);
  const age = Number(v.hotAge) || 6;
  const q = `${(v.hotQuery || "lang:zh min_faves:20").trim()} since_time:${Math.floor((Date.now() - age * 36e5) / 1000)}`;
  const tab = await chrome.tabs.create({ url: "https://x.com/search?q=" + encodeURIComponent(q) + "&src=typed_query&f=top", active: true });
  await chrome.storage.local.set({ hotJob: { tabId: tab.id, back: fromTabId || null, at: Date.now(), rounds: 25 } });
  return { ok: true };
}
async function hotDone(tabId) {
  const job = (await chrome.storage.local.get(["hotJob"])).hotJob;
  if (!job || job.tabId !== tabId) return;
  await chrome.storage.local.set({ hotJob: null, hotFetchedAt: Date.now() });
  try { await chrome.tabs.remove(tabId); } catch (e) { /* 已关 */ }
  if (job.back) { try { await chrome.tabs.update(job.back, { active: true }); } catch (e) { /* 忽略 */ } }
}

function openSchedulePage() {
  chrome.tabs.create({ url: chrome.runtime.getURL("schedule.html") });
}
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === "local" && (ch.remindOn || ch.remindMin)) arm();
});

/* 到点：不再弹系统通知，改成在 X 页面顶上出提示条（content.js 看到 remindDue 就显示），图标变红 */
async function remindNow() {
  await chrome.storage.local.set({ remindDue: Date.now(), remindNextAt: 0 });
  updateBadge();
  return { ok: true };
}

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name === SCHED_ALARM) {
    checkSchedule();
    /* 保险：抓评论的标签页 3 分钟还没关，就关掉 */
    const ij = (await chrome.storage.local.get(["inboxJob"])).inboxJob;
    if (ij && Date.now() - ij.at > 2 * 60000) inboxFinish(ij);          // 某一步卡住两分钟：收工
    const hj = (await chrome.storage.local.get(["hotJob"])).hotJob;
    if (hj && Date.now() - hj.at > 3 * 60000) hotDone(hj.tabId);
    const w = (await chrome.storage.local.get(["remindWorking"])).remindWorking;
    if (w && Date.now() - w > WORK_MAX_MIN * 60000) await roundDone();
    else {
      /* 保险：既不在进行中、也没到点、也没有倒计时（比如浏览器重启丢了），就重新开始倒数 */
      const v = await chrome.storage.local.get(["remindDue", "remindOn"]);
      if (!w && !v.remindDue && v.remindOn !== false && !(await chrome.alarms.get(ALARM))) await arm();
      updateBadge();
    }
    return;
  }
  if (a.name !== ALARM) return;
  const c = await cfg();
  if (!c.remindOn) return;
  await remindNow();
});

chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (msg && msg.type === "xfb-sched-open") { openSlot(msg.date, msg.time); return false; }
  if (msg && msg.type === "xfb-sched-sent") { markSchedSent(msg.text); return false; }
  if (msg && msg.type === "xfb-sched-x") { scheduleOnX(msg.date, msg.time, false); return false; }
  if (msg && msg.type === "xfb-sched-x-all") {
    nextForX(msg.date).then((hm) => {
      if (hm) scheduleOnX(msg.date, hm, true);
      send({ ok: !!hm });
    });
    return true;
  }
  if (msg && msg.type === "xfb-sched-x-done") { markSchedOnX(!!msg.ok); return false; }
  if (msg && msg.type === "xfb-media-chunk") { mediaChunk(msg.id, msg.offset, msg.len).then(send, () => send({ ok: false })); return true; }
  if (msg && msg.type === "xfb-open-schedule") { openSchedulePage(); return false; }
  if (msg && msg.type === "xfb-open-hot") { openHotPage(); return false; }
  if (msg && msg.type === "xfb-hot-fetch") { hotFetch(_s && _s.tab ? _s.tab.id : null).then(send); return true; }
  if (msg && msg.type === "xfb-hot-job") {
    chrome.storage.local.get(["hotJob"]).then((v) => {
      const j = v.hotJob;
      send({ job: j && _s.tab && j.tabId === _s.tab.id && Date.now() - j.at < 5 * 60000 ? j : null });
    });
    return true;
  }
  if (msg && msg.type === "xfb-hot-done") { hotDone(_s.tab && _s.tab.id); return false; }
  if (msg && msg.type === "xfb-open-inbox") { openInboxPage(); return false; }
  if (msg && msg.type === "xfb-inbox-fetch") { inboxFetch(_s && _s.tab ? _s.tab.id : null).then(send); return true; }
  if (msg && msg.type === "xfb-inbox-job") {
    chrome.storage.local.get(["inboxJob"]).then((v) => {
      const j = v.inboxJob;
      send({ job: j && _s.tab && j.tabId === _s.tab.id && Date.now() - j.at < 5 * 60000 ? j : null });
    });
    return true;
  }
  if (msg && msg.type === "xfb-inbox-posts") { inboxPosts(_s.tab && _s.tab.id, msg.posts); return false; }
  if (msg && msg.type === "xfb-inbox-next") { inboxNext(_s.tab && _s.tab.id, msg.added, msg.seen, msg.full); return false; }
  if (msg && msg.type === "xfb-inbox-reply") { inboxReply(_s && _s.tab ? _s.tab.id : null, msg.id, msg.text).then(send); return true; }
  if (msg && msg.type === "xfb-inbox-send-job") {
    chrome.storage.local.get(["inboxSend"]).then((v) => {
      const j = v.inboxSend;
      send({ job: j && _s.tab && j.tabId === _s.tab.id && Date.now() - j.at < 3 * 60000 ? j : null });
    });
    return true;
  }
  if (msg && msg.type === "xfb-inbox-clicked") {
    chrome.storage.local.get(["inboxSend"]).then((v) => { if (v.inboxSend && _s.tab && v.inboxSend.tabId === _s.tab.id) chrome.storage.local.set({ inboxSend: { ...v.inboxSend, clicked: true } }); });
    return false;
  }
  if (msg && msg.type === "xfb-inbox-sent") { inboxSent(_s.tab && _s.tab.id, !!msg.ok, msg.why); return false; }
  if (msg && msg.type === "xfb-test-notify") {
    remindNow().then((r) => send(r));
    return true;
  }
  if (msg && msg.type === "xfb-remind-snooze") {          // 15 分钟后再提醒
    chrome.storage.local.set({ remindDue: 0 }).then(() => arm(15));
    return false;
  }
  if (msg && msg.type === "xfb-remind-done") { roundDone(); return false; }      // 点 × 跳过：从现在开始倒数
  if (msg && msg.type === "xfb-round-start") { roundStart(); return false; }     // 开始处理
  if (msg && msg.type === "xfb-round-done") { roundDone(); return false; }       // 点「这轮够了」
  if (msg && msg.type === "xfb-round-follow") { roundFollow(); return false; }   // 关注了一个人
  return false;
});

async function openSearch() {
  await roundStart();
  const url = searchUrl(await cfg()) + "#xfb-next";
  const tabs = await chrome.tabs.query({ url: ["https://x.com/search*", "https://twitter.com/search*"] });
  if (tabs.length) {
    const t = tabs[0];
    await chrome.tabs.update(t.id, { url, active: true });
    await chrome.windows.update(t.windowId, { focused: true });
  } else {
    const w = await chrome.windows.getLastFocused().catch(() => null);
    await chrome.tabs.create({ url, ...(w ? { windowId: w.id } : {}) });
    if (w) chrome.windows.update(w.id, { focused: true });
  }
}
chrome.action.onClicked.addListener(openSearch);

/* 点提醒：已经开着 X 搜索页就复用那个标签页，没有就新开一个 */
chrome.notifications.onClicked.addListener(async (id) => {
  if (id.startsWith("xfb-sched|")) {
    chrome.notifications.clear(id);
    const [, day, hm] = id.split("|");
    openSlot(day, hm);
    return;
  }
  if (id !== ALARM) return;
  chrome.notifications.clear(id);
  openSearch();
});

/* 右键工具栏图标的菜单：随时清零重新计时 */
function setupMenus() {
  try {
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({ id: "xfb-reset", title: "清零，重新计时（从现在开始倒数）", contexts: ["action"] });
      chrome.contextMenus.create({ id: "xfb-start", title: "现在开始一轮（暂停倒计时，开始数关注人数）", contexts: ["action"] });
      chrome.contextMenus.create({ id: "xfb-inbox", title: "待回复评论", contexts: ["action"] });
      chrome.contextMenus.create({ id: "xfb-sched", title: "发帖排期表", contexts: ["action"] });
      chrome.contextMenus.create({ id: "xfb-hot", title: "热点雷达", contexts: ["action"] });
    });
  } catch (e) { /* 忽略 */ }
}
chrome.runtime.onInstalled.addListener(setupMenus);
chrome.runtime.onStartup.addListener(setupMenus);
try {
  chrome.contextMenus.onClicked.addListener((info) => {
    if (info.menuItemId === "xfb-reset") roundDone();
    if (info.menuItemId === "xfb-start") roundStart();
    if (info.menuItemId === "xfb-inbox") openInboxPage();
    if (info.menuItemId === "xfb-sched") openSchedulePage();
    if (info.menuItemId === "xfb-hot") openHotPage();
  });
} catch (e) { /* 忽略 */ }

chrome.runtime.onStartup.addListener(updateBadge);
updateBadge();

/* 【时间工作流】连接另一个插件「X 时间工作流」，代码都在 workflow-bridge.js。更新时保留这一行 */
try { importScripts("workflow-bridge.js"); } catch (e) { /* 没有这个文件也不影响别的功能 */ }

globalThis.__xfb = { searchUrl, arm, remindNow, updateBadge, roundStart, roundDone, roundFollow, checkSchedule, openSlot, markSchedSent, scheduleOnX, nextForX, markSchedOnX };
