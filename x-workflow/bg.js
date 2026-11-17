/* X 时间工作流 · 后台
 * 1. 接收另外两个插件发来的数据，存起来给页面用：
 *    「回关助手」：发帖排期表、今天回复数、待回复评论数、互关池……
 *    「X 自动回复流程」：运行状态、日志、今天回复了几条
 * 2. 页面上的操作转交给它们去做（打开排期表、交给 X 定时、开始/暂停/停止自动回复……）
 * 3. 每一步到点弹提醒；设了「到点自动开始」的自动回复步骤，到点叫自动回复流程开始 */
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
chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (msg && msg.type === "hg") { toHg(msg.msg).then(send); return true; }
  if (msg && msg.type === "xrf") { toXrf(msg.msg).then(send); return true; }
  if (msg && msg.type === "pull") { Promise.all([toHg({ type: "wf-pull" }), toXrf({ type: "xrf-pull" })]).then(() => send({ ok: true })); return true; }
  return false;
});

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
      if (s.notify && occurrences(s).includes(nowMin)) out.push({ key: `${day}|${s.id}|${nowMin}`, title: `该做了：${s.title}`, body: s.items.join("\n") || `每 ${everyText(s.every)}一次` });
      continue;
    }
    if (s.t !== nowMin) continue;
    if (s.action.type === "xrf" && s.action.auto) {
      out.push({ key: `${day}|${s.id}|auto`, auto: s, title: `自动回复开始了：${s.title}`, body: `回复 ${s.action.count} 条，从「${s.action.tab}」开始` });
    } else if (s.notify) {
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
  const v = await chrome.storage.local.get(["wfSteps", "wfTimes", "wfNotify", "snap", "wfNotified", "wfRuns"]);
  const steps = loadSteps(v);
  const now = new Date(), day = dateKey(now);
  const done = v.wfNotified || {};
  const list = dueNow(steps, v.snap && v.snap.schedule, now).filter((x) => !done[x.key]);
  for (const x of list) {
    done[x.key] = Date.now();
    if (x.auto) {
      const ran = ((v.wfRuns || {})[day] || {})[x.auto.id];
      if (ran) continue;                                   // 今天这一步已经跑过了
      const r = await toXrf({ type: "xrf-start", count: x.auto.action.count, tab: x.auto.action.tab, stepId: x.auto.id, date: day, auto: true });
      if (!r.ok) { x.title = `自动回复没能开始：${x.auto.title}`; x.body = r.reason || "X 自动回复流程没有响应"; }
      if (v.wfNotify === false && r.ok) continue;
    } else if (v.wfNotify === false) continue;
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

globalThis.__wf = { dueNow, openPage, onXrf };
