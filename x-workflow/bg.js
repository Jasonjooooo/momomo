/* X 时间工作流 · 后台
 * 1. 接收另外两个插件发来的数据，存起来给页面用：
 *    「回关助手」：发帖排期表、今天回复数、待回复评论数、互关池……
 *    「X 自动回复流程」：运行状态、日志、今天回复了几条
 * 2. 页面上的操作转交给它们去做（打开排期表、交给 X 定时、开始/暂停/停止自动回复……）
 * 3. 每一步到点弹提醒；设了「到点自动开始」的步骤，到点自己做（开始自动回复、叫 TG二创 开始、打开互关池……）
 * 4. 每分钟问一次 TG二创（本机 127.0.0.1:38719）在不在、在做什么 */
"use strict";
importScripts("steps.js");

const PAGE = chrome.runtime.getURL("workflow.html");
const TICK = "wf-tick";

/* 已经开着就切过去，不重复开（getContexts 不需要额外的「读取浏览记录」权限） */
async function openPage() {
  let open = [];
  try { open = await chrome.runtime.getContexts({ contextTypes: ["TAB"], documentUrls: [PAGE] }); } catch (e) { /* 旧版 Chrome */ }
  if (open.length && open[0].tabId >= 0) {
    await chrome.tabs.update(open[0].tabId, { active: true });
    if (open[0].windowId >= 0) await chrome.windows.update(open[0].windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: PAGE });
  }
}
chrome.action.onClicked.addListener(openPage);

/* ---------- 另外两个插件 → 这里 ---------- */
chrome.runtime.onMessageExternal.addListener((msg, sender, send) => {
  if (!msg) return false;
  if (msg.app === "回关助手") {
    if (msg.type === "hg-snapshot" && msg.data) {
      chrome.storage.local.set({ snap: { ...msg.data, at: Date.now() }, hgId: sender.id }).then(() => send({ ok: true }));
      return true;
    }
    if (msg.type === "hg-open-workflow") {
      chrome.storage.local.set({ hgId: sender.id });
      openPage().then(() => send({ ok: true }));
      return true;
    }
  }
  if (msg.app === "自动回复流程") {
    if (msg.type === "xrf-snapshot" && msg.data) {
      onXrf(msg.data, sender.id).then(() => send({ ok: true }));
      return true;
    }
    if (msg.type === "xrf-open-workflow") {
      chrome.storage.local.set({ xrfId: sender.id });
      openPage().then(() => send({ ok: true }));
      return true;
    }
  }
  return false;
});

/* 自动回复流程的状态 → 记到对应步骤的「这一轮」上（开始时间、进度、结束时间） */
async function onXrf(d, id) {
  const v = await chrome.storage.local.get(["wfRuns"]);
  const runs = v.wfRuns || {};
  const tag = d.tag, st = d.status || {};
  if (tag && tag.date && tag.stepId) {
    const day = (runs[tag.date] ||= {});
    let r = day[tag.stepId];
    if (!r || r.startAt !== tag.at) r = day[tag.stepId] = { startAt: tag.at };
    const runningThis = st.active && (st.at || 0) >= tag.at - 5000;
    if (runningThis) Object.assign(r, { active: true, done: st.done || 0, failed: st.failed || 0, max: st.max || 0, error: "" });
    else if (r.active) Object.assign(r, { active: false, endAt: Date.now(), done: st.done || r.done || 0, failed: st.failed || r.failed || 0, endStep: st.step || "" });
    else if (!r.endAt && Date.now() - tag.at > 90000) r.error = st.step || "X 页面没响应";
  }
  const cut = dateKey(new Date(Date.now() - 14 * 864e5));
  for (const k of Object.keys(runs)) if (k < cut) delete runs[k];
  await chrome.storage.local.set({ xsnap: { ...d, at: Date.now() }, xrfId: id, wfRuns: runs });
}

/* ---------- 这里 → 另外两个插件 ---------- */
async function toHg(msg) {
  const { hgId } = await chrome.storage.local.get(["hgId"]);
  if (!hgId) return { ok: false, reason: "还没连上回关助手：先装好新版回关助手，再打开一次 X 网页。" };
  try { await chrome.runtime.sendMessage(hgId, msg); return { ok: true }; }
  catch (e) { return { ok: false, reason: "回关助手没有响应。去 chrome://extensions 看看它是不是被关掉了，或者点一下它的刷新按钮。" }; }
}
async function toXrf(msg) {
  const { xrfId } = await chrome.storage.local.get(["xrfId"]);
  if (!xrfId) return { ok: false, reason: "还没连上 X 自动回复流程：装好新版（1.1.0），再刷新一次 X 页面。" };
  try {
    const r = await chrome.runtime.sendMessage(xrfId, msg);
    return r && typeof r.ok === "boolean" ? r : { ok: true };
  } catch (e) {
    return { ok: false, reason: "X 自动回复流程没有响应。去 chrome://extensions 看看它是不是被关掉了，或者点一下它的刷新按钮。" };
  }
}
/* ---------- TG二创（Mac 程序，在本机开了一个只认这个插件的小接口） ---------- */
const TG_URL = `http://127.0.0.1:${TG_PORT}`;
async function tgFetch(path, body) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 4000);
  try {
    const r = await fetch(TG_URL + path, body ? { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" }, signal: ctl.signal } : { signal: ctl.signal });
    return await r.json();
  } finally { clearTimeout(timer); }
}
async function tgPoll() {
  try {
    const d = await tgFetch("/status");
    await chrome.storage.local.set({ tsnap: { ...d, at: Date.now() } });
    return true;
  } catch (e) {
    const { tsnap } = await chrome.storage.local.get(["tsnap"]);
    if (tsnap && tsnap.at) await chrome.storage.local.set({ tsnap: { ...tsnap, down: true } });
    return false;
  }
}
async function toTg(msg) {
  try {
    const r = await tgFetch(msg.type === "tg-stop" ? "/stop" : "/run", { mode: msg.mode || "all" });
    tgPoll();
    return r && typeof r.ok === "boolean" ? { ok: r.ok, reason: r.error } : { ok: false, reason: "TG二创 回复的内容看不懂，更新一下 TG二创。" };
  } catch (e) {
    return { ok: false, reason: "连不上 TG二创：要先打开 TG二创（1.9.0 以上），它开着才能叫它开始。" };
  }
}

chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (msg && msg.type === "hg") { toHg(msg.msg).then(send); return true; }
  if (msg && msg.type === "xrf") { toXrf(msg.msg).then(send); return true; }
  if (msg && msg.type === "tg") { toTg(msg.msg).then(send); return true; }
  if (msg && msg.type === "run-step") { runStepNow(msg.id, msg.m).then(send); return true; }
  if (msg && msg.type === "pull") { Promise.all([toHg({ type: "wf-pull" }), toXrf({ type: "xrf-pull" }), tgPoll()]).then(() => send({ ok: true })); return true; }
  return false;
});

/* ---------- 到点自动做 ---------- */
/* 自动回复每一轮的记录：只做一次的步骤按步骤记；重复做的按「步骤@第几分钟」分开记，每一次都是新的一轮 */
const runKey = (s, m) => (s.mode === "repeat" ? `${s.id}@${m}` : s.id);
async function runAction(s, day, m) {
  const a = s.action;
  if (a.type === "xrf") return toXrf({ type: "xrf-start", count: a.count, tab: a.tab, stepId: runKey(s, m), date: day, auto: true });
  if (a.type === "tg") return toTg({ type: "tg-run", mode: a.mode });
  if (a.type === "inbox") return toHg({ type: "wf-inbox-fetch" });
  if (a.type === "pool") return toHg({ type: "wf-open-pool" });
  if (a.type === "sched") return toHg({ type: "xfb-open-schedule" });
  if (a.type === "url" && a.url) {
    await chrome.tabs.create({ url: /^https?:\/\//.test(a.url) ? a.url : "https://" + a.url });
    return { ok: true };
  }
  return { ok: false, reason: "这一步没有可以自动做的事" };
}
/* 该自动做的：时间在 (现在 - AUTO_WINDOW, 现在] 里、今天还没做过的。重复做的只做最近的一次 */
function autoDue(steps, now, done) {
  const day = dateKey(now), n = now.getHours() * 60 + now.getMinutes();
  const out = [];
  for (const s of steps) {
    if (!s.auto || s.action.type === "none" || !onDay(s, day)) continue;
    const ms = occurrences(s).filter((m) => m <= n && m > n - AUTO_WINDOW);
    if (!ms.length) continue;
    const m = ms[ms.length - 1];
    const key = `${s.id}|${m}`;
    if (done[key]) continue;
    out.push({ s, m, key });
  }
  return out;
}
/* 页面上点格子：马上做这一次（没自动做成、错过了、或者自动关着的时候用） */
async function runStepNow(id, m) {
  const v = await chrome.storage.local.get(["wfSteps", "wfTimes", "wfV14", "wfFollowMigrated", "wfFollow", "wfAuto"]);
  const s = loadSteps(v).find((x) => x.id === id);
  if (!s || s.action.type === "none") return { ok: false, reason: "找不到这一步，刷新一下页面再试。" };
  const day = dateKey(new Date());
  const r = await runAction(s, day, m);
  const log = v.wfAuto || {};
  (log[day] ||= {})[`${id}|${m}`] = { at: Date.now(), m, ok: !!r.ok, manual: true, msg: r.ok ? AUTO_TEXT[s.action.type] || "已开始" : (r.reason || "没成功") };
  await chrome.storage.local.set({ wfAuto: log });
  return r;
}

async function autoTick(now) {
  const v = await chrome.storage.local.get(["wfSteps", "wfTimes", "wfV14", "wfFollowMigrated", "wfFollow", "wfAutoOn", "wfAutoPause", "wfAuto", "wfNotify", "wfRuns"]);
  const day = dateKey(now);
  if (v.wfAutoOn === false || v.wfAutoPause === day) return [];
  const log = v.wfAuto || {};
  const today = (log[day] ||= {});
  const list = autoDue(loadSteps(v), now, today);
  for (const x of list) {
    today[x.key] = { at: Date.now(), m: x.m, ok: false, msg: "正在开始…" };
    await chrome.storage.local.set({ wfAuto: log });          // 先记下，免得下一分钟又做一次
    let r;
    if (x.s.action.type === "xrf" && ((v.wfRuns || {})[day] || {})[runKey(x.s, x.m)]) r = { ok: false, reason: "这一轮已经跑过了" };
    else r = await runAction(x.s, day, x.m);
    today[x.key] = { at: Date.now(), m: x.m, ok: !!r.ok, msg: r.ok ? AUTO_TEXT[x.s.action.type] || "已开始" : (r.reason || "没成功") };
    await chrome.storage.local.set({ wfAuto: log });
    const late = Math.round((now.getHours() * 60 + now.getMinutes()) - x.m);
    if (!r.ok || v.wfNotify !== false) {
      chrome.notifications.create(`wf|auto|${day}|${x.key}`, {
        type: "basic", iconUrl: "icons/icon128.png", priority: 1,
        title: r.ok ? `已自动开始：${x.s.title}` : `没能自动开始：${x.s.title}`,
        message: (r.ok ? AUTO_TEXT[x.s.action.type] + (late > 0 ? `（晚了 ${late} 分钟，电脑刚醒）` : "") : r.reason || "") + "\n点这里打开时间工作流",
      });
    }
  }
  const cut = dateKey(new Date(Date.now() - 14 * 864e5));
  for (const k of Object.keys(log)) if (k < cut) delete log[k];
  if (list.length) await chrome.storage.local.set({ wfAuto: log });
  return list.map((x) => x.key);
}

/* ---------- 到点提醒 / 到点自动开始 ---------- */
function armTick() {
  chrome.alarms.get(TICK, (a) => { if (!a) chrome.alarms.create(TICK, { delayInMinutes: 0.1, periodInMinutes: 1 }); });
}
chrome.runtime.onInstalled.addListener(armTick);
chrome.runtime.onStartup.addListener(armTick);
armTick();

/* 这一分钟该做的事 */
function dueNow(steps, schedule, now) {
  const day = dateKey(now), nowMin = now.getHours() * 60 + now.getMinutes();
  const out = [];
  for (const s of steps) {
    if (!onDay(s, day)) continue;
    if (s.mode === "repeat") {
      if (s.notify && !(s.auto && s.action.type !== "none") && occurrences(s).includes(nowMin)) out.push({ key: `${day}|${s.id}|${nowMin}`, title: `该做了：${s.title}`, body: s.items.join("\n") || `每 ${everyText(s.every)}一次` });
      continue;
    }
    if (s.t !== nowMin) continue;
    if (s.auto && s.action.type !== "none") continue;          // 到点自动做的，做完会单独弹「已自动开始」
    if (s.notify) {
      out.push({ key: `${day}|${s.id}`, title: `该做了：${s.title}`, body: s.items.join("\n") || s.dur || "" });
    }
  }
  const items = (schedule && schedule[day]) || {};
  Object.keys(items).sort().forEach((hm) => {
    const it = items[hm];
    if (!hasContent(it) || toMin(hm) + FOLLOW_AFTER !== nowMin) return;
    if (it.status !== "xsched" && it.status !== "sent") return;     // 还没发出去的不提醒回评论
    const text = (it.text || "").trim().replace(/\s+/g, " ");
    out.push({ key: `${day}|post|${hm}`, title: `该回评论了：${hm} 那条帖子`, body: (text.length > 60 ? text.slice(0, 60) + "…" : text) || "（只有图片或视频）" });
  });
  return out;
}

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== TICK) return;
  tgPoll();
  await autoTick(new Date());
  const v = await chrome.storage.local.get(["wfSteps", "wfTimes", "wfV14", "wfFollowMigrated", "wfFollow", "wfNotify", "snap", "wfNotified", "wfRuns"]);
  const steps = loadSteps(v);
  const now = new Date(), day = dateKey(now);
  const done = v.wfNotified || {};
  const list = dueNow(steps, v.snap && v.snap.schedule, now).filter((x) => !done[x.key]);
  for (const x of list) {
    done[x.key] = Date.now();
    if (v.wfNotify === false) continue;
    chrome.notifications.create("wf|" + x.key, { type: "basic", iconUrl: "icons/icon128.png", title: x.title, message: (x.body ? x.body + "\n" : "") + "点这里打开时间工作流", priority: 1 });
  }
  if (list.length) {
    const cut = Date.now() - 3 * 864e5;
    for (const k of Object.keys(done)) if (done[k] < cut) delete done[k];
    await chrome.storage.local.set({ wfNotified: done });
  }
});
chrome.notifications.onClicked.addListener((id) => {
  if (!id.startsWith("wf|")) return;
  chrome.notifications.clear(id);
  openPage();
});

globalThis.__wf = { dueNow, openPage, onXrf, autoDue, autoTick, runAction, tgPoll, toTg };
