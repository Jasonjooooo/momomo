/* 回关助手 · 连接「X 时间工作流」插件（单独一个文件，background.js 末尾一行 importScripts 引入）
 *
 * 时间工作流是另一个插件，读不到这里的数据，所以由这里把数据发过去：
 *   发帖排期表（前 1 天到后 8 天）、今天回复数和目标、待回复评论数、找互关帖提醒的倒计时。
 * 数据一变就发，另外每分钟发一次，让它知道回关助手还在运行。没装时间工作流就什么都不做。
 * 它只能叫这里：打开排期表、打开待回复评论（可以顺便点「抓取最新评论」）、把某一格交给 X 定时、打开某一格的发帖框、打开待关注池。
 * 还能接收 TG二创「发帖」页挑好的视频和文案，放进发帖排期表（视频存进 media-db.js 的 IndexedDB，和你手动加的一样）。
 *
 * 用到 background.js 里的：dateKey、SCHED_ALARM、openSchedulePage、openInboxPage、scheduleOnX、openSlot、roundStart、inboxFetch，
 * media-db.js 里的：mediaPut、mediaDel */
"use strict";

const WF_ID = "fpfdleplbfpeaajiplfdbokjcmefdmak";      // 时间工作流插件的 ID（它的 manifest 里有固定 key，所以每台电脑都一样）
const WF_KEYS = ["schedule", "replyLog", "replyGoal", "inbox", "replied", "remindNextAt", "remindOn", "remindDue", "remindMin",
  "pool", "following", "done", "followLog", "followedAt", "roundCount", "roundTarget", "remindWorking"];

/* 待关注池：去掉已经关注的，按「关注数和粉丝数最接近」排序（和面板里一样），只发前 5 个的名字 */
function wfPool(v) {
  const following = v.following || {}, done = v.done || {};
  const score = (p) => Math.abs(1 - (p.fr || 0) / Math.max(p.fo || 1, 1));
  const list = Object.entries(v.pool || {}).filter(([k, p]) => p && !following[k] && !done[k]).map(([, p]) => p).sort((a, b) => score(a) - score(b) || b.at - a.at);
  return { count: list.length, top: list.slice(0, 5).map((p) => ({ handle: p.handle, name: p.name || "", fo: p.fo || 0, fr: p.fr || 0, verified: !!p.verified })) };
}

async function wfPush() {
  try {
    const v = await chrome.storage.local.get(WF_KEYS);
    const today = dateKey(new Date());
    const from = dateKey(new Date(Date.now() - 864e5)), to = dateKey(new Date(Date.now() + 8 * 864e5));
    const schedule = {};
    for (const [d, items] of Object.entries(v.schedule || {})) {
      if (d < from || d > to) continue;
      schedule[d] = {};
      for (const [hm, it] of Object.entries(items || {})) {
        schedule[d][hm] = { text: (it && it.text) || "", imgs: (it && it.imgs) || 0, vid: !!(it && it.vid), status: (it && it.status) || "" };
      }
    }
    const inbox = v.inbox || {}, replied = v.replied || {};
    const inboxTodo = Object.keys(inbox).filter((k) => inbox[k].post && !inbox[k].hidden && !inbox[k].state && !replied[k]).length;
    await chrome.runtime.sendMessage(WF_ID, { app: "回关助手", type: "hg-snapshot", data: {
      schedule, replyToday: (v.replyLog || {})[today] || 0, replyGoal: v.replyGoal || 500, inboxTodo,
      remindNextAt: v.remindNextAt || 0, remindOn: v.remindOn !== false, remindDue: v.remindDue || 0, remindMin: Number(v.remindMin) || 15,
      pool: wfPool(v), followToday: (v.followLog || {})[today] || 0,
      followTimes: Object.values(v.followedAt || {}).filter((t) => t >= new Date(new Date().setHours(0, 0, 0, 0)).getTime()).sort(),
      roundCount: Number(v.roundCount) || 0, roundTarget: Number(v.roundTarget) || 15, roundWorking: v.remindWorking || 0,
    } });
    return true;
  } catch (e) {
    return false;          // 没装时间工作流：不用管
  }
}

/* 打开时间工作流（面板按钮、右键菜单） */
async function wfOpen() {
  try {
    await chrome.runtime.sendMessage(WF_ID, { app: "回关助手", type: "hg-open-workflow" });
    wfPush();
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: "还没装「X 时间工作流」插件，装好后再点。" };
  }
}

/* 打开 X 首页并展开待关注池（content.js 看到 #xfb-pool 就打开池子），同时开始一轮计数。关注还是你自己按 F */
async function wfOpenPool() {
  try { await roundStart(); } catch (e) { /* 忽略 */ }
  const url = "https://x.com/home#xfb-pool";
  const tabs = await chrome.tabs.query({ url: ["https://x.com/home*", "https://twitter.com/home*"] });
  if (tabs.length) {
    await chrome.tabs.update(tabs[0].id, { url, active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
  } else {
    const w = await chrome.windows.getLastFocused().catch(() => null);
    await chrome.tabs.create({ url, ...(w ? { windowId: w.id } : {}) });
    if (w) chrome.windows.update(w.id, { focused: true });
  }
}

/* 打开「待回复评论」并开始抓取最新评论（和在那个页面点「抓取最新评论」一样），抓完回到这个页面 */
async function wfInboxFetch() {
  const tab = await chrome.tabs.create({ url: chrome.runtime.getURL("inbox.html") });
  const { inboxJob } = await chrome.storage.local.get(["inboxJob"]);
  if (inboxJob && Date.now() - (inboxJob.at || 0) < 3 * 60000) return { ok: true };     // 已经在抓了
  const r = await inboxFetch(tab.id);
  return r && r.ok ? { ok: true } : { ok: false, reason: (r && r.reason) || "抓取没开始" };
}

/* ---------- 接收 TG二创 的视频，放进发帖排期表 ----------
 * 时间工作流分段送来（每段 4MB，转成了文字），收齐了拼成视频，找一个空格子放进去：
 * 想要的时间已经有内容 / 已经过了，就往后挪到下一个空格子（按排期表设置的间隔，当天排不下就排到第二天） */
const wfIncoming = new Map();
function wfB64(s) {
  const bin = atob(s), u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
}
async function wfFreeSlot(day, hm) {
  const v = await chrome.storage.local.get(["schedule", "schedSettings"]);
  const sched = v.schedule || {};
  const set = { start: "08:00", end: "23:00", step: 20, ...(v.schedSettings || {}) };
  const step = Math.max(5, Number(set.step) || 20);
  const toM = (t) => { const [h, m] = String(t).split(":").map(Number); return h * 60 + m; };
  const fmtM = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const used = (d, t) => { const it = (sched[d] || {})[t]; return !!(it && ((it.text && it.text.trim()) || it.imgs > 0)); };
  const now = new Date(), today = dateKey(now), nowM = now.getHours() * 60 + now.getMinutes() + 5;   // 至少 5 分钟以后（X 定时要 3 分钟以上）
  const ok = (d, m) => !used(d, fmtM(m)) && (d > today || (d === today && m >= nowM));
  let d = day < today ? today : day;
  let m = toM(hm);
  if (ok(d, m)) return { day: d, hm: fmtM(m) };
  for (let i = 0; i < 15; i++) {
    const s0 = toM(set.start), e0 = toM(set.end);
    const grid = [];
    for (let t = s0; t <= e0; t += step) grid.push(t);
    const next = grid.find((t) => t > m && ok(d, t));
    if (next != null) return { day: d, hm: fmtM(next) };
    const nd = new Date(d + "T00:00:00"); nd.setDate(nd.getDate() + 1);
    d = dateKey(nd); m = -1;
  }
  return null;
}
async function wfPost(msg) {
  const qid = String(msg.qid || "");
  if (msg.type === "wf-post-begin") {
    if (!msg.size || msg.size > 1024 * 1024 * 1024) return { ok: false, reason: "视频大小不对" };
    wfIncoming.set(qid, { ...msg, parts: [], got: 0, at: Date.now() });
    for (const [k, p] of wfIncoming) if (Date.now() - p.at > 30 * 60000) wfIncoming.delete(k);   // 半路断掉的清掉
    return { ok: true };
  }
  const p = wfIncoming.get(qid);
  if (msg.type === "wf-post-abort") { wfIncoming.delete(qid); return { ok: true }; }
  if (!p) return { ok: false, reason: "回关助手重启过，这一条要重新送" };
  if (msg.type === "wf-post-chunk") {
    if (msg.off !== p.got) return { ok: false, reason: "视频分段顺序不对" };
    const u = wfB64(msg.data || "");
    p.parts.push(u); p.got += u.length; p.at = Date.now();
    return { ok: true };
  }
  /* wf-post-end：拼成视频，放进格子 */
  wfIncoming.delete(qid);
  if (p.got !== p.size) return { ok: false, reason: `视频没收全（${p.got} / ${p.size}）` };
  const slot = await wfFreeSlot(p.day, p.hm);
  if (!slot) return { ok: false, reason: "未来两周的格子都满了" };
  const key = `img:${slot.day}|${slot.hm}`;
  const id = "v" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const type = p.mime || "video/mp4";
  const blob = new Blob(p.parts, { type });
  /* 先记格子再存视频：排期表页面会清理「没有格子认领」的视频 */
  await chrome.storage.local.set({ [key]: [{ type, idb: id, size: blob.size, name: p.name || "video.mp4" }] });
  try {
    await mediaPut(id, { blob, type, name: p.name || "video.mp4", owner: key });
  } catch (e) {
    await chrome.storage.local.remove(key);
    return { ok: false, reason: "视频没存下来（电脑硬盘空间可能不够）" };
  }
  const v = await chrome.storage.local.get(["schedule"]);
  const sched = v.schedule || {};
  (sched[slot.day] ||= {})[slot.hm] = { text: String(p.text || ""), status: "", imgs: 1, vid: true, from: "TG二创" };
  await chrome.storage.local.set({ schedule: sched });
  return { ok: true, day: slot.day, hm: slot.hm };
}

let wfTimer = 0;
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local" || !WF_KEYS.some((k) => ch[k])) return;
  clearTimeout(wfTimer);
  wfTimer = setTimeout(wfPush, 300);
});
chrome.alarms.onAlarm.addListener((a) => { if (a.name === SCHED_ALARM) wfPush(); });   // 每分钟一次
chrome.runtime.onStartup.addListener(wfPush);
chrome.runtime.onInstalled.addListener(wfPush);

/* 时间工作流发来的请求 */
chrome.runtime.onMessageExternal.addListener((msg, sender, send) => {
  if (sender.id !== WF_ID || !msg) return false;
  if (msg.type === "wf-pull") { wfPush().then((ok) => send({ ok })); return true; }
  if (msg.type === "xfb-open-schedule") openSchedulePage();
  else if (msg.type === "xfb-open-inbox") openInboxPage();
  else if (msg.type === "xfb-sched-x" && msg.date && msg.time) scheduleOnX(msg.date, msg.time, false);
  else if (msg.type === "xfb-sched-open" && msg.date && msg.time) openSlot(msg.date, msg.time);
  else if (msg.type === "wf-open-pool") wfOpenPool();
  else if (msg.type === "wf-inbox-fetch") { wfInboxFetch().then(send); return true; }
  else if (msg.type === "wf-sched-x-all" && msg.day) {
    /* 把这一天还没交给 X 的一条条交给 X 定时（和排期表里点「把这天还没发的都交给 X 定时」一样） */
    nextForX(msg.day).then((hm) => { if (hm) scheduleOnX(msg.day, hm, true); send({ ok: !!hm }); });
    return true;
  }
  else if (msg.type === "wf-post-begin" || msg.type === "wf-post-chunk" || msg.type === "wf-post-end" || msg.type === "wf-post-abort") {
    wfPost(msg).then(send, (e) => send({ ok: false, reason: String((e && e.message) || e) }));
    return true;
  }
  else return false;
  send({ ok: true });
  return false;
});

/* 面板上的「时间工作流 ↗」按钮 */
chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (msg && msg.type === "xfb-open-workflow") { wfOpen().then(send); return true; }
  return false;
});

/* 右键图标菜单：background.js 的 setupMenus 会先 removeAll，所以晚一点再加 */
function wfMenu() {
  setTimeout(() => {
    try {
      chrome.contextMenus.create({ id: "xfb-workflow", title: "时间工作流（另一个插件）", contexts: ["action"] }, () => void chrome.runtime.lastError);
    } catch (e) { /* 忽略 */ }
  }, 1500);
}
chrome.runtime.onInstalled.addListener(wfMenu);
chrome.runtime.onStartup.addListener(wfMenu);
try { chrome.contextMenus.onClicked.addListener((info) => { if (info.menuItemId === "xfb-workflow") wfOpen(); }); } catch (e) { /* 忽略 */ }
