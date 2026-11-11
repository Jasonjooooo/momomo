// 后台 Service Worker：定时抓取、关键词匹配、角标与通知
import { getSettings, getCache, setCache } from "./lib/settings.js";
import { fetchPlatform } from "./lib/api.js";
import { parseFrequencyWords, matchTitle } from "./lib/frequency.js";

const ALARM = "trendradar-refresh";
const MAX_SEEN = 3000;

let running = null;

async function scheduleAlarm() {
  const { refreshMinutes } = await getSettings();
  const period = Math.max(5, Number(refreshMinutes) || 30);
  await chrome.alarms.clear(ALARM);
  await chrome.alarms.create(ALARM, { delayInMinutes: 1, periodInMinutes: period });
}

async function doRefresh() {
  const settings = await getSettings();
  const enabled = settings.platforms.filter((p) => p.enabled);
  const prev = (await getCache()) || { platforms: {} };
  const now = Date.now();

  const results = await Promise.all(
    enabled.map(async (p, i) => {
      await new Promise((r) => setTimeout(r, i * 100)); // 错开请求，与 Python 端 request_interval 类似
      try {
        const { items, cached } = await fetchPlatform(settings.apiUrl, p, {
          domainCheck: settings.domainCheck,
        });
        return [p.id, { name: p.name, items, cached, error: null, fetchedAt: now }];
      } catch (e) {
        // 失败时保留上次成功的数据，标记错误
        const old = prev.platforms?.[p.id];
        return [p.id, { name: p.name, items: old?.items || [], cached: true, error: e.message, fetchedAt: old?.fetchedAt || null }];
      }
    })
  );

  const cache = { fetchedAt: now, platforms: Object.fromEntries(results) };
  await setCache(cache);
  await notifyNewMatches(cache, settings);
  return cache;
}

async function notifyNewMatches(cache, settings) {
  const rules = parseFrequencyWords(settings.keywordsText);
  if (!rules.groups.length) return; // 未配置关键词时不打扰

  const { seen = null, unread = 0 } = await chrome.storage.local.get(["seen", "unread"]);
  const firstRun = seen === null;
  const seenSet = new Set(seen || []);
  const fresh = [];

  for (const [pid, pdata] of Object.entries(cache.platforms)) {
    if (pdata.error) continue;
    for (const item of pdata.items) {
      const g = matchTitle(item.title, rules);
      if (!g) continue;
      const key = `${pid}|${item.title}`;
      if (seenSet.has(key)) continue;
      seenSet.add(key);
      fresh.push({ ...item, platform: pdata.name, group: g.name });
    }
  }

  const seenArr = [...seenSet].slice(-MAX_SEEN);
  if (firstRun || !fresh.length) {
    await chrome.storage.local.set({ seen: seenArr });
    return;
  }

  const newUnread = unread + fresh.length;
  await chrome.storage.local.set({ seen: seenArr, unread: newUnread });
  await updateBadge(newUnread);

  if (settings.notifyEnabled) {
    const id = `tr-${Date.now()}`;
    const list = fresh.slice(0, 5).map((f) => ({ title: f.title, message: `${f.platform} · ${f.group}` }));
    await chrome.storage.session.set({ [id]: fresh[0].url || fresh[0].mobileUrl });
    chrome.notifications.create(id, {
      type: "list",
      iconUrl: "icons/icon128.png",
      title: `TrendRadar：${fresh.length} 条新命中热点`,
      message: fresh[0].title,
      items: list,
      priority: 1,
    });
  }
}

async function updateBadge(n) {
  await chrome.action.setBadgeBackgroundColor({ color: "#e5484d" });
  await chrome.action.setBadgeText({ text: n > 0 ? (n > 99 ? "99+" : String(n)) : "" });
}

function refresh() {
  if (!running) running = doRefresh().finally(() => (running = null));
  return running;
}

chrome.runtime.onInstalled.addListener(async () => {
  await scheduleAlarm();
  refresh();
});

chrome.runtime.onStartup.addListener(scheduleAlarm);

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) refresh();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.refreshMinutes) scheduleAlarm();
  if (changes.keywordsText) chrome.storage.local.remove("seen"); // 规则变了，重新建立基线，避免一次性刷屏
});

chrome.notifications.onClicked.addListener(async (id) => {
  const data = await chrome.storage.session.get(id);
  if (data[id]) chrome.tabs.create({ url: data[id] });
  chrome.notifications.clear(id);
  chrome.storage.session.remove(id);
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "refresh") {
    refresh()
      .then((cache) => sendResponse({ ok: true, cache }))
      .catch((e) => sendResponse({ ok: false, error: e.message }));
    return true;
  }
  if (msg?.type === "clearBadge") {
    chrome.storage.local.set({ unread: 0 }).then(() => updateBadge(0));
  }
  return false;
});
