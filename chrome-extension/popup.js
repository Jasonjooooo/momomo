import { getSettings, getCache } from "./lib/settings.js";
import { parseFrequencyWords, matchTitle } from "./lib/frequency.js";

const $ = (s) => document.querySelector(s);
const MATCHED_TAB = "__matched__";

const state = {
  settings: null,
  rules: null,
  cache: null,
  tab: null,
  query: "",
  onlyMatched: false,
};

function store(key, val) {
  try { localStorage.setItem(key, JSON.stringify(val)); } catch {}
}
function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
}

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return "刚刚更新";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return new Date(ts).toLocaleString();
}

function el(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  Object.assign(n, props);
  for (const c of children) if (c != null) n.append(c);
  return n;
}

function highlight(text, q) {
  const frag = document.createDocumentFragment();
  if (!q) { frag.append(text); return frag; }
  const lower = text.toLowerCase();
  let i = 0;
  for (;;) {
    const j = lower.indexOf(q, i);
    if (j < 0) break;
    frag.append(text.slice(i, j), el("mark", { textContent: text.slice(j, j + q.length) }));
    i = j + q.length;
  }
  frag.append(text.slice(i));
  return frag;
}

function renderItem(item, { showPlatform } = {}) {
  const href = item.url || item.mobileUrl || "#";
  const meta = el("div", { className: "meta" });
  if (showPlatform) meta.append(el("span", { textContent: `${item.platform} #${item.rank}` }));
  if (item.group) meta.append(el("span", { className: "chip", textContent: item.group }));
  if (item.extra) meta.append(el("span", { textContent: item.extra }));

  return el(
    "a",
    { className: "item", href, target: "_blank", rel: "noopener noreferrer", title: item.title },
    showPlatform ? null : el("span", { className: "rank" + (item.rank <= 3 ? " top" : ""), textContent: item.rank }),
    el("div", { className: "body" },
      el("div", { className: "title" }, highlight(item.title, state.query)),
      meta.childNodes.length ? meta : null
    )
  );
}

function annotate(pid, pdata) {
  return pdata.items.map((it) => {
    const g = matchTitle(it.title, state.rules);
    return { ...it, pid, platform: pdata.name, matched: !!g, group: g?.name || null };
  });
}

function passesQuery(it) {
  return !state.query || it.title.toLowerCase().includes(state.query);
}

function renderTabs() {
  const tabs = $("#tabs");
  tabs.replaceChildren();
  const entries = [];
  if (state.rules.groups.length) entries.push([MATCHED_TAB, "关键词命中", false]);
  for (const p of state.settings.platforms.filter((p) => p.enabled)) {
    const pd = state.cache?.platforms?.[p.id];
    entries.push([p.id, p.name, !!pd?.error]);
  }
  if (!entries.some(([id]) => id === state.tab)) state.tab = entries[0]?.[0] ?? null;

  for (const [id, name, err] of entries) {
    const b = el("button", { textContent: name, role: "tab", className: err ? "err" : "" });
    b.setAttribute("aria-selected", String(id === state.tab));
    if (err) b.title = state.cache.platforms[id].error;
    b.addEventListener("click", () => {
      state.tab = id;
      store("tab", id);
      render();
    });
    tabs.append(b);
  }
  tabs.querySelector('[aria-selected="true"]')?.scrollIntoView({ inline: "nearest", block: "nearest" });
}

function renderMatched(list) {
  const byGroup = new Map(state.rules.groups.map((g) => [g.name, { g, items: [] }]));
  const seenTitles = new Set();
  for (const p of state.settings.platforms.filter((p) => p.enabled)) {
    const pd = state.cache?.platforms?.[p.id];
    if (!pd) continue;
    for (const it of annotate(p.id, pd)) {
      if (!it.matched || !passesQuery(it)) continue;
      if (seenTitles.has(it.title)) continue; // 跨平台去重
      seenTitles.add(it.title);
      byGroup.get(it.group)?.items.push(it);
    }
  }
  let total = 0;
  for (const { g, items } of byGroup.values()) {
    if (!items.length) continue;
    items.sort((a, b) => a.rank - b.rank);
    const shown = g.maxCount ? items.slice(0, g.maxCount) : items;
    total += shown.length;
    list.append(el("div", { className: "section-title", textContent: `${g.name} · ${items.length}` }));
    for (const it of shown) list.append(renderItem({ ...it, group: null }, { showPlatform: true }));
  }
  if (!total) list.append(el("div", { className: "empty", textContent: state.query ? "没有匹配搜索的命中新闻" : "暂无命中关键词的热点" }));
}

function renderPlatform(list, pid) {
  const pd = state.cache?.platforms?.[pid];
  if (!pd) {
    list.append(el("div", { className: "empty", textContent: "暂无数据，点击右上角刷新" }));
    return;
  }
  if (pd.error) list.append(el("div", { className: "error", textContent: `抓取失败：${pd.error}` + (pd.items.length ? "（显示上次数据）" : "") }));
  const items = annotate(pid, pd).filter((it) => passesQuery(it) && (!state.onlyMatched || it.matched));
  if (!items.length && !pd.error) {
    list.append(el("div", { className: "empty", textContent: "没有符合条件的新闻" }));
    return;
  }
  const hasGroups = state.rules.groups.length > 0;
  for (const it of items) list.append(renderItem({ ...it, group: hasGroups ? it.group : null }));
}

function render() {
  renderTabs();
  $("#updated").textContent = timeAgo(state.cache?.fetchedAt);
  $("#onlyMatched").parentElement.hidden = !state.rules.groups.length || state.tab === MATCHED_TAB;
  const list = $("#list");
  list.replaceChildren();
  if (!state.tab) {
    list.append(el("div", { className: "empty", textContent: "未启用任何平台，请在设置中勾选" }));
  } else if (state.tab === MATCHED_TAB) {
    renderMatched(list);
  } else {
    renderPlatform(list, state.tab);
  }
  list.scrollTop = 0;
}

async function refresh() {
  const btn = $("#refresh");
  btn.classList.add("spinning");
  btn.disabled = true;
  try {
    const res = await chrome.runtime.sendMessage({ type: "refresh" });
    if (res?.ok) state.cache = res.cache;
    else throw new Error(res?.error || "未知错误");
  } catch (e) {
    $("#list").replaceChildren(el("div", { className: "error", textContent: `刷新失败：${e.message}` }));
    return;
  } finally {
    btn.classList.remove("spinning");
    btn.disabled = false;
  }
  render();
}

async function init() {
  state.settings = await getSettings();
  state.rules = parseFrequencyWords(state.settings.keywordsText);
  state.cache = await getCache();
  state.tab = load("tab", null);
  state.onlyMatched = load("onlyMatched", false);
  $("#onlyMatched").checked = state.onlyMatched;

  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value.trim().toLowerCase();
    render();
  });
  $("#onlyMatched").addEventListener("change", (e) => {
    state.onlyMatched = e.target.checked;
    store("onlyMatched", state.onlyMatched);
    render();
  });
  $("#refresh").addEventListener("click", refresh);
  $("#settings").addEventListener("click", () => chrome.runtime.openOptionsPage());

  chrome.runtime.sendMessage({ type: "clearBadge" });
  render();

  // 没有缓存或缓存超过刷新间隔时自动刷新
  const stale = !state.cache || Date.now() - state.cache.fetchedAt > state.settings.refreshMinutes * 60000;
  if (stale) refresh();
}

init();
