import { getSettings, saveSettings, DEFAULT_API_URL, DEFAULT_PLATFORMS } from "./lib/settings.js";
import { parseFrequencyWords } from "./lib/frequency.js";

const $ = (s) => document.querySelector(s);

function platformRow(p = { id: "", name: "", expectedDomain: "", enabled: true }) {
  const tr = document.createElement("tr");
  const cell = (input) => { const td = document.createElement("td"); td.append(input); return td; };
  const mk = (type, cls, value, placeholder) => {
    const i = document.createElement("input");
    i.type = type; i.className = cls;
    if (type === "checkbox") i.checked = value; else { i.value = value; i.placeholder = placeholder; }
    return i;
  };
  const del = document.createElement("button");
  del.type = "button"; del.className = "del"; del.textContent = "✕"; del.title = "删除";
  del.addEventListener("click", () => tr.remove());
  tr.append(
    cell(mk("checkbox", "p-enabled", p.enabled)),
    cell(mk("text", "p-name", p.name, "显示名称")),
    cell(mk("text", "p-id", p.id, "newsnow 平台 ID")),
    cell(mk("text", "p-domain", p.expectedDomain || "", "如 zhihu.com")),
    cell(del)
  );
  return tr;
}

function renderPlatforms(list) {
  $("#platforms tbody").replaceChildren(...list.map(platformRow));
}

function readPlatforms() {
  return [...document.querySelectorAll("#platforms tbody tr")]
    .map((tr) => ({
      enabled: tr.querySelector(".p-enabled").checked,
      name: tr.querySelector(".p-name").value.trim(),
      id: tr.querySelector(".p-id").value.trim(),
      expectedDomain: tr.querySelector(".p-domain").value.trim().toLowerCase(),
    }))
    .filter((p) => p.id)
    .map((p) => ({ ...p, name: p.name || p.id }));
}

function updateParseInfo() {
  const r = parseFrequencyWords($("#keywords").value);
  $("#parseInfo").textContent = r.groups.length
    ? `已识别 ${r.groups.length} 个词组、${r.filterWords.length} 个过滤词、${r.globalFilters.length} 个全局过滤词`
    : "未配置词组：将显示全部新闻，不发送通知";
}

function setStatus(text, cls) {
  const s = $("#status");
  s.textContent = text;
  s.className = cls;
  if (cls === "ok") setTimeout(() => (s.textContent = ""), 2500);
}

async function ensureHostPermission(apiUrl) {
  const u = new URL(apiUrl);
  if (u.origin === new URL(DEFAULT_API_URL).origin) return true;
  return chrome.permissions.request({ origins: [`${u.origin}/*`] });
}

async function save() {
  let apiUrl = $("#apiUrl").value.trim() || DEFAULT_API_URL;
  try {
    const u = new URL(apiUrl);
    if (!/^https?:$/.test(u.protocol)) throw new Error();
  } catch {
    setStatus("API 地址格式不正确", "err");
    return;
  }
  // 注意：permissions.request 必须在用户手势中调用，这里位于点击事件的同步链路上
  const granted = await ensureHostPermission(apiUrl);
  if (!granted) {
    setStatus("未授予访问该 API 的权限，未保存", "err");
    return;
  }
  const platforms = readPlatforms();
  const ids = platforms.map((p) => p.id);
  if (new Set(ids).size !== ids.length) {
    setStatus("平台 ID 不能重复", "err");
    return;
  }
  await saveSettings({
    apiUrl,
    platforms,
    keywordsText: $("#keywords").value,
    refreshMinutes: Math.max(5, Math.min(1440, parseInt($("#refreshMinutes").value, 10) || 30)),
    notifyEnabled: $("#notifyEnabled").checked,
    domainCheck: $("#domainCheck").checked,
  });
  setStatus("已保存，正在刷新数据…", "ok");
  chrome.runtime.sendMessage({ type: "refresh" }).then((res) => {
    if (res?.ok) setStatus("已保存并刷新", "ok");
  });
}

async function init() {
  const s = await getSettings();
  $("#apiUrl").value = s.apiUrl;
  $("#domainCheck").checked = s.domainCheck;
  $("#keywords").value = s.keywordsText;
  $("#refreshMinutes").value = s.refreshMinutes;
  $("#notifyEnabled").checked = s.notifyEnabled;
  renderPlatforms(s.platforms);
  updateParseInfo();

  $("#keywords").addEventListener("input", updateParseInfo);
  $("#addPlatform").addEventListener("click", () => {
    const row = platformRow();
    $("#platforms tbody").append(row);
    row.querySelector(".p-name").focus();
  });
  $("#resetPlatforms").addEventListener("click", () => renderPlatforms(DEFAULT_PLATFORMS));
  $("#importFile").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    $("#keywords").value = await f.text();
    updateParseInfo();
    e.target.value = "";
  });
  $("#save").addEventListener("click", save);
}

init();
