/* 待回复评论：查看、去回复、标记。收集评论在 content.js（X 页面里），抓取由 background.js 打开搜索页 */
"use strict";

const $ = (id) => document.getElementById(id);
let INBOX = {};       // 评论 ID -> { handle, name, avatar, text, url, at, got, state, media }
let REPLIED = {};     // 你回复过的帖子 ID（回关助手在你点 X 的「回复」时记下）
let view = "todo";
const DRAFTS = {};    // 正在写的回复，刷新列表时不丢
let SENDING = null;   // 正在发送的评论 ID
let pendingRender = false;
document.addEventListener("focusout", () => setTimeout(() => { if (pendingRender) render(); }, 0));

/* X 的字数：中文、表情算 2，其他算 1；链接算 23 */
function xLen(s) {
  let n = 0;
  for (const ch of String(s)) { const c = ch.codePointAt(0); n += c <= 0x10ff || (c >= 0x2000 && c <= 0x200d) || (c >= 0x2010 && c <= 0x201f) || (c >= 0x2032 && c <= 0x2037) ? 1 : 2; }
  for (const u of String(s).match(/https?:\/\/\S+/g) || []) { let r = 0; for (const ch of u) r += ch.codePointAt(0) <= 0x10ff ? 1 : 2; n += 23 - r; }
  return n;
}

const isDone = (id) => !!(INBOX[id] && (INBOX[id].state || REPLIED[id]));

function ago(t) {
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (m < 1) return "刚刚";
  if (m < 60) return `${m} 分钟前`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.round(h / 24)} 天前`;
}

async function setState(id, state) {
  const v = await chrome.storage.local.get(["inbox"]);
  const inbox = v.inbox || {};
  if (!inbox[id]) return;
  inbox[id].state = state;
  inbox[id].doneAt = state ? Date.now() : 0;
  /* 撤销「已回复」时，也把「你回复过」的记录去掉，不然它还是算已处理 */
  if (!state && REPLIED[id]) {
    const r = (await chrome.storage.local.get(["replied"])).replied || {};
    delete r[id];
    await chrome.storage.local.set({ replied: r });
  }
  await chrome.storage.local.set({ inbox });
}

/* 你在 X 上回复过的，存成「已回复」（「你回复过」的记录两周后会清掉，这里要自己记住） */
async function persistReplied() {
  const ids = Object.keys(INBOX).filter((id) => REPLIED[id] && !INBOX[id].state);
  if (!ids.length) return;
  const v = await chrome.storage.local.get(["inbox"]);
  const inbox = v.inbox || {};
  for (const id of ids) if (inbox[id]) { inbox[id].state = "done"; inbox[id].doneAt = Date.now(); }
  await chrome.storage.local.set({ inbox });
}

function render(force) {
  const a = document.activeElement;
  if (!force && a && a.tagName === "TEXTAREA" && a.closest(".rb")) { pendingRender = true; return; }
  pendingRender = false;
  /* 只显示你主帖下面的评论（旧版本从搜索里收的不算） */
  const ids = Object.keys(INBOX).filter((id) => INBOX[id].post && !INBOX[id].hidden).sort((a, b) => INBOX[b].at - INBOX[a].at);
  const todo = ids.filter((id) => !isDone(id));
  const done = ids.filter((id) => isDone(id));
  $("n-todo").textContent = todo.length ? `(${todo.length})` : "";
  $("n-done").textContent = done.length ? `(${done.length})` : "";
  $("tab-todo").setAttribute("aria-pressed", String(view === "todo"));
  $("tab-done").setAttribute("aria-pressed", String(view === "done"));
  $("clear-done").hidden = view !== "done" || !done.length;
  document.title = todo.length ? `(${todo.length}) 待回复评论` : "待回复评论 · 回关助手";

  const list = $("list");
  list.textContent = "";
  const show = view === "todo" ? todo : done;
  if (!show.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = view === "todo"
      ? (ids.length ? "都回完了 👍" : "还没有评论。点上面的「抓取最新评论」。")
      : "还没有处理过的评论。";
    list.appendChild(p);
    return;
  }
  for (const id of show) {
    const c = INBOX[id];
    const el = document.createElement("article");
    el.className = "c" + (isDone(id) ? " done" : "");
    const av = document.createElement("div");
    av.className = "av";
    if (c.avatar) av.style.backgroundImage = `url("${String(c.avatar).replace(/"/g, "")}")`;
    const body = document.createElement("div");
    const hd = document.createElement("div");
    hd.className = "hd";
    const nm = document.createElement("b");
    nm.textContent = c.name || c.handle;
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = `@${c.handle} · ${ago(c.at)}`;
    hd.append(nm, sub);
    if (c.media) { const t = document.createElement("span"); t.className = "tag"; t.textContent = "带图/视频"; hd.appendChild(t); }
    const tx = document.createElement("div");
    tx.className = "tx";
    tx.textContent = c.text || "（没有文字）";
    const pt = document.createElement("div");
    pt.className = "pt";
    pt.textContent = "回复你的帖子：" + (c.postText || "（图片/视频帖）");
    const acts = document.createElement("div");
    acts.className = "acts";
    let box = null;
    if (!isDone(id)) {
      /* 直接在这里写回复 */
      box = document.createElement("div");
      box.className = "rb";
      const ta = document.createElement("textarea");
      ta.placeholder = `回复 @${c.handle}…`;
      ta.setAttribute("aria-label", `回复 @${c.handle}`);
      ta.value = DRAFTS[id] || "";
      const cnt = document.createElement("span");
      cnt.className = "cnt";
      const send = document.createElement("button");
      send.type = "button";
      send.className = "primary";
      send.textContent = SENDING === id ? "发送中…" : "回复";
      const upd = () => {
        DRAFTS[id] = ta.value;
        const n = xLen(ta.value);
        cnt.textContent = ta.value ? `${n}/280` : "";
        cnt.classList.toggle("over", n > 280);
        send.disabled = !ta.value.trim() || !!SENDING;
      };
      ta.addEventListener("input", upd);
      ta.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !send.disabled) send.click(); });
      send.addEventListener("click", () => {
        SENDING = id;
        send.disabled = true;
        send.textContent = "发送中…";
        setMsg("正在通过 X 发送回复，会闪出一个 X 页面，发完自动关掉、回到这里。");
        chrome.runtime.sendMessage({ type: "xfb-inbox-reply", id, text: ta.value }, (r) => {
          if (!r || !r.ok) { SENDING = null; upd(); send.textContent = "回复"; setMsg((r && r.reason) || "没能打开 X，刷新一下这个页面再试。", "err"); }
        });
      });
      upd();
      const row = document.createElement("div");
      row.className = "rb-row";
      row.append(cnt, send);
      box.append(ta, row);
      const ok = document.createElement("button");
      ok.type = "button";
      ok.textContent = "已回复 ✓";
      ok.addEventListener("click", () => setState(id, "done"));
      const ig = document.createElement("button");
      ig.type = "button";
      ig.textContent = "忽略";
      ig.title = "不用回的评论（广告、机器人、表情等）";
      ig.addEventListener("click", () => setState(id, "ignored"));
      const go = document.createElement("button");
      go.type = "button";
      go.textContent = "在 X 打开";
      go.title = "在新标签页打开这条评论（想看上下文、或者用 AI 回复助手时）";
      go.addEventListener("click", () => chrome.tabs.create({ url: "https://x.com" + c.url }));
      acts.append(ok, ig, go);
    } else {
      const st = document.createElement("span");
      st.className = "state";
      st.textContent = c.state === "ignored" ? "已忽略" : "已回复 ✓";
      if (c.myReply) { const mr = document.createElement("div"); mr.className = "mr"; mr.textContent = "你回复：" + c.myReply; body.appendChild(mr); }
      const undo = document.createElement("button");
      undo.type = "button";
      undo.textContent = "撤销";
      undo.addEventListener("click", () => setState(id, ""));
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "打开";
      open.addEventListener("click", () => chrome.tabs.create({ url: "https://x.com" + c.url }));
      acts.append(st, open, undo);
    }
    body.prepend(hd, tx, pt);
    if (box) body.appendChild(box);
    body.appendChild(acts);
    el.append(av, body);
    list.appendChild(el);
  }
}

function setMsg(text, kind) { $("msg").textContent = text || ""; $("msg").className = "msg" + (kind ? " " + kind : ""); }

async function load() {
  const v = await chrome.storage.local.get(["inbox", "replied", "inboxFetchedAt", "inboxLastAdded"]);
  INBOX = v.inbox || {};
  REPLIED = v.replied || {};
  if (v.inboxFetchedAt) setMsg(`上次抓取：${new Date(v.inboxFetchedAt).toLocaleString("zh-CN", { hour12: false })}，新增 ${v.inboxLastAdded || 0} 条。`);
  render();
  persistReplied();
}

$("fetch").addEventListener("click", () => {
  $("fetch").disabled = true;
  setMsg("正在打开你的 X 主页，只会打开有新评论的帖子，收完自动关掉、回到这里。期间别关那个页面。");
  chrome.runtime.sendMessage({ type: "xfb-inbox-fetch" }, (r) => {
    if (!r || !r.ok) { $("fetch").disabled = false; setMsg((r && r.reason) || "没能打开 X，刷新一下这个页面再试。", "err"); }
  });
});
$("tab-todo").addEventListener("click", () => { view = "todo"; render(); });
$("tab-done").addEventListener("click", () => { view = "done"; render(); });
$("clear-done").addEventListener("click", async () => {
  const v = await chrome.storage.local.get(["inbox", "replied"]);
  const inbox = v.inbox || {}, replied = v.replied || {};
  /* 不删记录，只是藏起来：不然下次抓取又会当成新评论收进来 */
  for (const id of Object.keys(inbox)) if (inbox[id].state || replied[id]) inbox[id].hidden = true;
  await chrome.storage.local.set({ inbox });
});

chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local") return;
  let re = false;
  if (ch.inbox) { INBOX = ch.inbox.newValue || {}; re = true; }
  if (ch.replied) { REPLIED = ch.replied.newValue || {}; re = true; }
  if (ch.inboxFetchedAt && ch.inboxFetchedAt.newValue) {
    $("fetch").disabled = false;
    chrome.storage.local.get(["inboxLastAdded", "inboxLastPosts", "inboxLastSkipped", "inboxLastFound"]).then((v) => setMsg(`抓取完成：最近两天 ${v.inboxLastFound || 0} 条主帖，打开了 ${v.inboxLastPosts || 0} 条（有新评论或上次没看全），${v.inboxLastSkipped || 0} 条没变化跳过，新增 ${v.inboxLastAdded || 0} 条没回的评论。`, "ok"));
  }
  if (ch.inboxProgress && ch.inboxProgress.newValue) {
    const g = ch.inboxProgress.newValue;
    setMsg(g.step === "profile" ? "正在看你主页上最近两天的帖子…" : `正在看第 ${g.idx} / ${g.total} 条有新评论的帖子（${g.skipped} 条没变化已跳过）：${(g.text || "").slice(0, 30)}`);
  }
  if (ch.inboxSendResult && ch.inboxSendResult.newValue) {
    const r = ch.inboxSendResult.newValue;
    SENDING = null;
    if (r.ok) { delete DRAFTS[r.id]; setMsg("回复已发出 ✓", "ok"); }
    else setMsg(`没发出去：${r.why || "原因不明"}。那个 X 页面留着没关，你可以在那里看一眼、自己点「回复」。`, "err");
    re = true;
  }
  if (re) { render(); persistReplied(); }
});
setInterval(render, 60000);   // 刷新「几分钟前」
load();
