/* X 自动回复流程 · 后台：连接「X 时间工作流」插件
 *
 * 时间工作流是另一个插件，读不到这里的数据，所以由这里把运行状态、最近的日志、今天回复了几条发过去：
 * 数据一变就发，另外每分钟发一次，让它知道这个插件还在。没装时间工作流就什么都不做。
 * 它能叫这里做的事：开始一轮（回复几条、从哪个标签开始）、暂停、继续、停止、切到正在跑的 X 页面。
 * 回复的过程和弹窗里点「开始」完全一样，间隔、重试、乱码过滤都用弹窗里的设置。 */
"use strict";

const WF_ID = "fpfdleplbfpeaajiplfdbokjcmefdmak";   // 时间工作流的 ID（它的 manifest 里有固定 key，每台电脑都一样）
const KEYS = ["xrfStatus", "xrfLog", "xrfRunTag"];
const X_URLS = ["https://x.com/*", "https://twitter.com/*"];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pad = (n) => String(n).padStart(2, "0");
const dateKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

/* ---------- 发数据给时间工作流 ---------- */
async function push() {
  try {
    const v = await chrome.storage.local.get(KEYS);
    const dayStart = new Date(new Date().setHours(0, 0, 0, 0)).getTime();
    const log = v.xrfLog || [];
    const todayDone = log.filter((l) => l.t >= dayStart && l.level === "ok" && /^✅/.test(l.text)).length;
    await chrome.runtime.sendMessage(WF_ID, { app: "自动回复流程", type: "xrf-snapshot", data: {
      status: v.xrfStatus || {}, tag: v.xrfRunTag || null, todayDone, logs: log.slice(-80),
    } });
    return true;
  } catch (e) {
    return false;      // 没装时间工作流
  }
}
let timer = 0;
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local" || !KEYS.some((k) => ch[k])) return;
  /* 弹窗里自己点「开始」的那一轮，不算时间工作流里的步骤：把上一次的标记清掉 */
  if (ch.xrfStatus) {
    const was = ch.xrfStatus.oldValue || {}, now = ch.xrfStatus.newValue || {};
    if (now.active && !was.active) {
      chrome.storage.local.get(["xrfRunTag"]).then(({ xrfRunTag: t }) => {
        if (t && Date.now() - t.at > 60000) chrome.storage.local.set({ xrfRunTag: null });
      });
    }
  }
  clearTimeout(timer); timer = setTimeout(push, 400);
});
function armTick() { chrome.alarms.get("xrf-tick", (a) => { if (!a) chrome.alarms.create("xrf-tick", { delayInMinutes: 0.2, periodInMinutes: 1 }); }); }
chrome.runtime.onInstalled.addListener(() => { armTick(); push(); });
chrome.runtime.onStartup.addListener(() => { armTick(); push(); });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "xrf-tick") push(); });
armTick();

/* ---------- 找 X 页面 ---------- */
async function ping(tabId) {
  try { return await chrome.tabs.sendMessage(tabId, { type: "ping" }); } catch (e) { return null; }
}
/* 正在跑的那个 X 页面（暂停、停止、切过去用） */
async function runningTab() {
  const { xrfRunTag: t } = await chrome.storage.local.get(["xrfRunTag"]);
  if (t && t.tabId) { const r = await ping(t.tabId); if (r && r.running) return t.tabId; }
  for (const tab of await chrome.tabs.query({ url: X_URLS })) { const r = await ping(tab.id); if (r && r.running) return tab.id; }
  return null;
}
/* 自动回复专用的 X 页面：第一次新开一个，以后一直用它，不碰你自己正在看的 X 页面 */
async function ownTab() {
  const { xrfOwnTab } = await chrome.storage.local.get(["xrfOwnTab"]);
  if (xrfOwnTab) {
    try {
      const t = await chrome.tabs.get(xrfOwnTab);
      if (/^https:\/\/(x|twitter)\.com\//.test(t.url || t.pendingUrl || "")) {
        await chrome.tabs.update(t.id, { active: true });           // 切到前面：在后台的页面 Chrome 会把它放慢
        if (!(await ping(t.id))) await chrome.tabs.reload(t.id);    // 插件更新过 / 页面卡住：刷新一下
        return t;
      }
    } catch (e) { /* 已经被关掉了 */ }
  }
  const w = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => null);
  const t = await chrome.tabs.create({ url: "https://x.com/home", active: true, ...(w ? { windowId: w.id } : {}) });
  await chrome.storage.local.set({ xrfOwnTab: t.id });
  return t;
}

/* 开始一轮：在专用的 X 页面上，从首页的标签（默认「为你推荐」）开始回复 */
async function startRun(m) {
  if (await runningTab()) return { ok: false, reason: "自动回复已经在跑了，等这一轮跑完，或者先点「停止」。" };
  const tab = await ownTab();
  let ok = false;
  for (let i = 0; i < 60 && !ok; i++) { ok = !!(await ping(tab.id)); if (!ok) await sleep(500); }
  if (!ok) return { ok: false, reason: "X 页面 30 秒还没加载好，刷新一下 X 页面再试。" };
  await chrome.storage.local.set({ xrfRunTag: { stepId: m.stepId || "", date: m.date || dateKey(new Date()), at: Date.now(), tabId: tab.id } });
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "start", opts: { maxPosts: m.count, tab: m.tab, fromHome: true } });
  } catch (e) {
    return { ok: false, reason: "X 页面没响应，刷新一下 X 页面再试。" };
  }
  push();
  return { ok: true };
}
async function control(type) {
  const id = await runningTab();
  if (!id) return { ok: false, reason: "现在没有在跑的自动回复。" };
  if (type === "focus") {
    const tab = await chrome.tabs.update(id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
    return { ok: true };
  }
  try { await chrome.tabs.sendMessage(id, { type }); } catch (e) { return { ok: false, reason: "X 页面没响应。" }; }
  return { ok: true };
}

/* ---------- 时间工作流发来的请求 ---------- */
chrome.runtime.onMessageExternal.addListener((msg, sender, send) => {
  if (sender.id !== WF_ID || !msg) return false;
  const reply = (p) => { p.then(send, (e) => send({ ok: false, reason: String(e && e.message || e) })); return true; };
  switch (msg.type) {
    case "xrf-pull": return reply(push().then((ok) => ({ ok })));
    case "xrf-start": return reply(startRun(msg));
    case "xrf-pause": return reply(control("pause"));
    case "xrf-resume": return reply(control("resume"));
    case "xrf-stop": return reply(control("stop"));
    case "xrf-focus": return reply(control("focus"));
  }
  return false;
});

globalThis.__xrf = { push, startRun, control };
