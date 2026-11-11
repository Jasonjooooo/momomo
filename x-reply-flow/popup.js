const $ = (s) => document.querySelector(s);
const FIELDS = ["maxPosts", "minGap", "maxGap", "confirmBeforeSend", "txtOpen", "txtRegen", "txtFill", "loadingWords", "failWords", "maxRetries", "feedTabs", "pick"];
let tabId = null;

async function send(type) {
  if (tabId == null) return null;
  try {
    return await chrome.tabs.sendMessage(tabId, { type });
  } catch {
    $("#notX").hidden = false;
    return null;
  }
}

function readCfg() {
  const c = {};
  for (const k of FIELDS) {
    const el = $("#" + k);
    c[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value.trim();
  }
  c.maxRetries = Math.max(0, Math.min(10, Math.round(c.maxRetries)));
  if (Number.isNaN(c.maxRetries)) c.maxRetries = XRF_DEFAULTS.maxRetries;
  c.maxPosts = Math.max(1, Math.round(c.maxPosts) || XRF_DEFAULTS.maxPosts);
  c.minGap = Math.max(1, c.minGap || XRF_DEFAULTS.minGap);
  c.maxGap = Math.max(c.minGap, c.maxGap || XRF_DEFAULTS.maxGap);
  for (const k of ["txtOpen", "txtRegen", "txtFill", "loadingWords", "failWords", "feedTabs"]) if (!c[k]) c[k] = XRF_DEFAULTS[k];
  return c;
}

async function saveCfg() {
  await chrome.storage.local.set({ xrfCfg: readCfg() });
}

function renderStatus(st) {
  const active = !!st?.active;
  $("#dot").className = "dot" + (active ? (st.paused ? " pause" : " run") : "");
  $("#step").textContent = st ? (st.paused ? "已暂停 · " : "") + st.step : "空闲";
  $("#count").textContent = st ? `成功 ${st.done} / ${st.max} 条，跳过 ${st.failed || 0} 条` : "";
  $("#start").disabled = active;
  $("#pause").disabled = !active;
  $("#stop").disabled = !active;
  $("#pause").textContent = st?.paused ? "继续" : "暂停";
  for (const k of FIELDS) $("#" + k).disabled = active;
}

function renderLog(list = []) {
  const ol = $("#log");
  ol.replaceChildren(
    ...list.slice(-80).reverse().map((e) => {
      const li = document.createElement("li");
      li.className = e.level;
      const t = document.createElement("time");
      t.textContent = new Date(e.t).toLocaleTimeString();
      li.append(t, e.text);
      return li;
    })
  );
}

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  tabId = tab?.id ?? null;

  const { xrfCfg = {}, xrfStatus, xrfLog = [], xrfProcessed = [] } = await chrome.storage.local.get(["xrfCfg", "xrfStatus", "xrfLog", "xrfProcessed"]);
  const cfg = { ...XRF_DEFAULTS, ...xrfCfg };
  for (const k of FIELDS) {
    const el = $("#" + k);
    if (el.type === "checkbox") el.checked = !!cfg[k];
    else el.value = cfg[k];
    el.addEventListener("change", saveCfg);
  }
  $("#processedCount").textContent = `(${xrfProcessed.length})`;
  renderLog(xrfLog);

  const ping = await send("ping");
  renderStatus(ping?.running ? xrfStatus : xrfStatus && { ...xrfStatus, active: false, paused: false });

  chrome.storage.onChanged.addListener((ch, area) => {
    if (area !== "local") return;
    if (ch.xrfStatus) renderStatus(ch.xrfStatus.newValue);
    if (ch.xrfLog) renderLog(ch.xrfLog.newValue);
    if (ch.xrfProcessed) $("#processedCount").textContent = `(${(ch.xrfProcessed.newValue || []).length})`;
  });

  $("#start").addEventListener("click", async () => {
    await saveCfg();
    await send("start");
  });
  $("#pause").addEventListener("click", () => send($("#pause").textContent === "继续" ? "resume" : "pause"));
  $("#stop").addEventListener("click", () => send("stop"));
  $("#clearLog").addEventListener("click", () => chrome.storage.local.set({ xrfLog: [] }));
  $("#clearProcessed").addEventListener("click", () => {
    if (confirm("清空后，之前回复过的帖子可能会被再次回复。确定清空？")) chrome.storage.local.set({ xrfProcessed: [] });
  });
  $("#check").addEventListener("click", async () => {
    await saveCfg();
    const res = await send("check");
    if (!res?.ok) return;
    const r = res.result;
    const yn = (v, tip = "") => (v ? "✅ 找到" : "❌ 没找到" + tip);
    $("#checkResult").textContent = [
      `当前页面：${r.page}`,
      `当前账号：${r.me ? "@" + r.me + "（自己的帖子和回复会跳过）" : "❌ 没识别到（可能会回复到自己的帖子）"}`,
      r.page === "首页" ? `首页标签：${r.tabs}` : "",
      `生成按钮：${r.regen ? "✅ 找到「" + r.regen + "」" : "❌ 没找到（面板没打开时属正常，开始后会先点「" + $("#txtOpen").value + "」打开）"}`,
      `「${$("#txtFill").value}」：${r.fill ? `✅ 找到 ${r.fill} 个` : "— 还没出现（生成后才出现，属正常）"}`,
      `打开面板「${$("#txtOpen").value}」：${yn(r.opener)}`,
      r.page === "帖子页" ? `回复框：${yn(r.replyBox)}　黑色回复按钮：${yn(r.replyBtn)}` : "",
      r.extFrames ? `⚠️ 页面里有 ${r.extFrames} 个其他插件的独立窗口（iframe），里面的按钮本插件点不到` : "",
    ].filter(Boolean).join("\n");
  });
}

init();
