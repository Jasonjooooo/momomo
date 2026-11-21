/* 发帖排期表：编辑、加图、预览。到点提醒、打开发帖框在 background.js 里 */
"use strict";

const $ = (id) => document.getElementById(id);
const DEF_SET = { start: "08:00", end: "23:00", step: 20, on: true, autoArrange: true };
const MISS_MIN = 10;          // 过了时间 10 分钟还没发，算「错过」
const MAX_IMG = 4;            // X 一条最多 4 张图
const IMG_MAX_BYTES = 4.5e6;  // 超过就压缩（X 图片上限 5MB）
const GIF_MAX_BYTES = 15e6;   // X 的 GIF 上限 15MB
const isVid = (t) => /^video\//.test(t || "");

let SET = { ...DEF_SET };
let SCHED = {};               // { "2026-11-07": { "08:20": { text, status, imgs } } }
let IMGS = {};                // { "img:2026-11-07|08:20": [{ type, data }] }
let ME = null;                // 你的 X 账号：{ name, handle, avatar, verified }
let SEL = null;               // 当前编辑的格子 "08:20"
let saveTimer = null;
let pvDark = false;

const pad = (n) => String(n).padStart(2, "0");
const dateKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const toMin = (hm) => { const [h, m] = String(hm).split(":").map(Number); return h * 60 + m; };
const toHM = (min) => `${pad(Math.floor(min / 60))}:${pad(min % 60)}`;
const imgKey = (day, hm) => `img:${day}|${hm}`;
const hasContent = (it) => !!(it && ((it.text && it.text.trim()) || it.imgs > 0));
async function loadMedia(key) {
  if (key in IMGS) return IMGS[key];
  const v = await chrome.storage.local.get([key]);
  IMGS[key] = v[key] || [];
  /* 视频存在 IndexedDB 里，做一个本地地址给预览用 */
  for (const im of IMGS[key]) {
    if (im.idb && !im.url) {
      try { const rec = await mediaGet(im.idb); if (rec && rec.blob) im.url = URL.createObjectURL(rec.blob); } catch (e) { /* 读不到就不预览 */ }
    }
  }
  return IMGS[key];
}
const srcOf = (im) => im.data || im.url || "";
const toStore = (imgs) => imgs.map(({ url, ...rest }) => rest);     // 本地预览地址不存
const fmtMB = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : Math.round(b / 1e6) + " MB");
/* 清掉没有格子再用的视频 */
async function sweepVideos() {
  try {
    const all = await mediaAll();
    if (!all.length) return;
    const owners = [...new Set(all.map(([, r]) => r && r.owner).filter(Boolean))];
    const v = owners.length ? await chrome.storage.local.get(owners) : {};
    for (const [id, r] of all) {
      const list = (r && v[r.owner]) || [];
      if (!list.some((im) => im.idb === id)) await mediaDel(id);
    }
  } catch (e) { /* 忽略 */ }
}
const day = () => $("date").value;

/* X 的字数：中文、日文、表情算 2，其他算 1，上限 280 */
function xLen(s) {
  let n = 0;
  for (const ch of String(s)) {
    const c = ch.codePointAt(0);
    n += c <= 0x10ff || (c >= 0x2000 && c <= 0x200d) || (c >= 0x2010 && c <= 0x201f) || (c >= 0x2032 && c <= 0x2037) ? 1 : 2;
  }
  /* 链接不管多长都算 23 */
  for (const u of String(s).match(/https?:\/\/\S+/g) || []) n += 23 - xLenRaw(u);
  return n;
}
function xLenRaw(s) { let n = 0; for (const ch of s) n += ch.codePointAt(0) <= 0x10ff ? 1 : 2; return n; }

/* 全天 24 小时：从 00:00 到 23:xx，按间隔一格一格 */
function slotTimes() {
  const step = Number(SET.step) || 20;
  const out = [];
  for (let m = 0; m < 24 * 60; m += step) out.push(toHM(m));
  return out;
}
function allTimes(d) {
  const items = SCHED[d] || {};
  const times = slotTimes();
  const extra = Object.keys(items).filter((t) => !times.includes(t) && hasContent(items[t]));
  return [...new Set([...times, ...extra])].sort();
}

function isPast(d, hm) {
  const now = new Date();
  const today = dateKey(now);
  return d < today || (d === today && toMin(hm) < now.getHours() * 60 + now.getMinutes());
}

function statusOf(d, hm, item) {
  if (!hasContent(item)) return { cls: "", text: "" };
  if (item.status === "sent") return { cls: "sent", text: "已发 ✓" };
  if (item.status === "xsched") return { cls: "sent", text: "X 已定时 ✓" };
  const now = new Date();
  const today = dateKey(now);
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const past = d < today || (d === today && toMin(hm) <= nowMin);
  const late = d < today || (d === today && nowMin - toMin(hm) >= MISS_MIN);
  if (item.status === "opened") return { cls: "opened", text: "已打开，未确认发出" };
  if (late) return { cls: "missed", text: "错过了" };
  if (item.status === "reminded" || past) return { cls: "reminded", text: "该发了" };
  return { cls: "wait", text: "待发" };
}

/* ---------------- 左边：格子列表 ---------------- */
/* 左边：24 小时表格。一行一个小时，每行按间隔分格 */
function renderList() {
  const d = day();
  const box = $("slots");
  const items = SCHED[d] || {};
  const times = allTimes(d);
  const step = Number(SET.step) || 20;
  const perRow = Math.max(1, Math.round(60 / step));
  const sc = box.closest(".list") || box;
  const keepTop = sc.scrollTop;
  box.textContent = "";
  box.style.setProperty("--cols", String(perRow));
  const byHour = new Map();
  for (const hm of times) {
    const h = hm.slice(0, 2);
    if (!byHour.has(h)) byHour.set(h, []);
    byHour.get(h).push(hm);
  }
  const nowH = pad(new Date().getHours());
  const today = d === dateKey(new Date());
  for (let hh = 0; hh < 24; hh++) {
    const h = pad(hh);
    const row = document.createElement("div");
    row.className = "hrow" + (today && h === nowH ? " now" : "");
    row.dataset.h = h;
    const lab = document.createElement("div");
    lab.className = "hlab";
    lab.textContent = h + " 时";
    row.appendChild(lab);
    const cells = document.createElement("div");
    cells.className = "cells";
    for (const hm of byHour.get(h) || []) {
      const it = items[hm];
      const st = statusOf(d, hm, it);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cell" + (hm === SEL ? " sel" : "") + (isPast(d, hm) ? " past" : "") + (hasContent(it) ? " has" : "") + (st.cls ? " st-" + st.cls : "");
      b.dataset.hm = hm;
      const t = document.createElement("span");
      t.className = "cm";
      t.textContent = ":" + hm.slice(3);
      const sn = document.createElement("span");
      sn.className = "cs";
      const first = it && it.text ? it.text.trim().split("\n")[0] : "";
      sn.textContent = first || (it && it.imgs ? (it.vid ? "🎬 视频" : `🖼 ${it.imgs} 张图`) : "");
      if (first && it.imgs) sn.textContent = (it.vid ? "🎬 " : "🖼 ") + first;
      const ic = document.createElement("span");
      ic.className = "ci";
      ic.textContent = st.cls === "sent" ? "✓" : st.cls === "missed" ? "!" : "";
      b.title = `${hm} ${st.text || (isPast(d, hm) ? "已过" : "空")}${first ? "：" + first : ""}`;
      b.setAttribute("aria-label", b.title);
      b.append(t, ic, sn);
      b.addEventListener("click", () => select(hm));
      cells.appendChild(b);
    }
    row.appendChild(cells);
    box.appendChild(row);
  }
  sc.scrollTop = keepTop;
  renderSummary(d, times, items);
}

/* 打开时滚到现在这个小时 */
function scrollToNow() {
  const h = pad(new Date().getHours());
  const row = document.querySelector(`.hrow[data-h="${h}"]`);
  const sc = $("slots").closest(".list");
  if (row && sc) sc.scrollTop = Math.max(0, row.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - 40);
}

function renderSummary(d, times, items) {
  let filled = 0, sent = 0, missed = 0, next = null;
  for (const hm of times) {
    const it = items[hm];
    if (!hasContent(it)) continue;
    filled++;
    const st = statusOf(d, hm, it);
    if (st.cls === "sent") sent++;
    else if (st.cls === "missed") missed++;
    else if (!next && !isPast(d, hm)) next = hm;
  }
  const box = $("summary");
  box.textContent = "";
  const add = (label, val) => {
    const span = document.createElement("span");
    span.append(label + " ");
    const b = document.createElement("b");
    b.textContent = val;
    span.appendChild(b);
    box.appendChild(span);
  };
  add("这天一共", `${times.length} 格`);
  add("已填", `${filled} 条`);
  add("已发 / X 已定时", `${sent} 条`);
  if (missed) add("错过", `${missed} 条`);
  add("下一条", next || "—");
  if (!SET.on) add("到点提醒", "已关闭");
}

/* ---------------- 右边：编辑 ---------------- */
async function select(hm) {
  SEL = hm;
  $("ed-empty").hidden = !!hm;
  $("ed").hidden = !hm;
  if (!hm) return renderList();
  await loadMedia(imgKey(day(), hm));
  if (SEL !== hm) return;
  const it = (SCHED[day()] || {})[hm] || {};
  $("text").value = it.text || "";
  $("ed-msg").textContent = "";
  renderEditor();
  renderList();
  const cur = document.querySelector(`.cell[data-hm="${hm}"]`);
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

function curItem() { return (SCHED[day()] || {})[SEL] || { text: "", status: "" }; }
function curImgs() { return IMGS[imgKey(day(), SEL)] || []; }

function renderEditor() {
  if (!SEL) return;
  const d = day();
  const it = curItem();
  const imgs = curImgs();
  const st = statusOf(d, SEL, { ...it, text: $("text").value, imgs: imgs.length, vid: imgs.some((x) => isVid(x.type)) });
  $("ed-title").textContent = `${d.slice(5).replace("-", "月")}日 ${SEL}`;
  $("ed-pill").className = "pill" + (st.cls ? " " + st.cls : "");
  $("ed-pill").textContent = st.text;
  $("ed-pill").hidden = !st.text;

  const n = xLen($("text").value);
  $("count").textContent = `${n} / 280`;
  $("count").className = n > 280 ? "over" : "";
  $("count-tip").textContent = n > 280 ? "超过 280：Premium 能发，时间线上会折叠成「显示更多」" : "";

  /* 缩略图 */
  const th = $("thumbs");
  th.textContent = "";
  imgs.forEach((im, i) => {
    const w = document.createElement("div");
    w.className = "thumb";
    let img;
    if (isVid(im.type)) {
      img = document.createElement("video");
      img.src = srcOf(im);
      img.muted = true;
      img.preload = "metadata";
      img.setAttribute("aria-label", "视频");
      const tag = document.createElement("span");
      tag.className = "vtag";
      tag.textContent = "▶ 视频";
      w.appendChild(tag);
    } else {
      img = document.createElement("img");
      img.src = srcOf(im);
      img.alt = `第 ${i + 1} 张图`;
    }
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.setAttribute("aria-label", isVid(im.type) ? "删掉视频" : `删掉第 ${i + 1} 张图`);
    x.addEventListener("click", () => removeImg(i));
    w.prepend(img);
    w.append(x);
    if (i > 0) {
      const mv = document.createElement("button");
      mv.type = "button";
      mv.className = "mv";
      mv.textContent = "←";
      mv.setAttribute("aria-label", `把第 ${i + 1} 张图往前挪`);
      mv.addEventListener("click", () => moveImg(i));
      w.appendChild(mv);
    }
    th.appendChild(w);
  });
  const isGif = imgs.some((im) => im.type === "image/gif");
  const hasVid = imgs.some((im) => isVid(im.type));
  $("add-img").disabled = imgs.length >= MAX_IMG || isGif || hasVid;
  $("img-hint").textContent = hasVid ? "视频只能单独发，不能再加图片或别的视频。"
    : isGif ? "GIF 只能单独发，不能再加别的图。"
    : imgs.length >= MAX_IMG ? "已经 4 张了，X 一条最多 4 张。"
    : "图片最多 4 张，或者 1 张 GIF，或者 1 个视频（MP4/MOV，不限大小）。可以拖进来，也可以在文字框里直接粘贴。";

  /* 按钮 */
  const has = !!($("text").value.trim() || imgs.length);
  const done = ["sent", "xsched"].includes(it.status);
  $("to-x").disabled = !has || isPast(d, SEL) || done;
  $("to-x").title = isPast(d, SEL) ? "时间已经过了，X 不能定时到过去。可以点「现在发」。" : "打开 X 的发帖框，插件帮你选好这个时间，你点「安排」。到点 X 自己发，电脑关了也会发。";
  $("send-now").disabled = !has;
  $("send-now").textContent = it.status === "sent" ? "再发一次" : "现在发";
  $("clear").disabled = !has;
  const times = allTimes(d);
  const i = times.indexOf(SEL);
  $("prev").disabled = i <= 0;
  $("next").disabled = i < 0 || i >= times.length - 1;

  renderPreview();
}

/* ---------------- 预览：仿 X 帖子 ---------------- */
function linkify(text) {
  const frag = document.createDocumentFragment();
  const re = /(https?:\/\/[^\s]+)|([#＃][^\s#＃.,，。!！?？:：;；()（）\[\]【】"“”'‘’]+)|(@[A-Za-z0-9_]{1,15})/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    const a = document.createElement("a");
    if (m[1]) {
      /* X 显示链接：去掉 https://，太长截断 */
      const shown = m[1].replace(/^https?:\/\/(www\.)?/, "");
      a.textContent = shown.length > 26 ? shown.slice(0, 25) + "…" : shown;
    } else a.textContent = m[0];
    frag.append(a);
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

/* 超过 280 时，X 只显示前面一段，后面是「显示更多」 */
function cutAt280(text) {
  let n = 0, i = 0;
  const chars = [...text];
  for (; i < chars.length; i++) {
    n += chars[i].codePointAt(0) <= 0x10ff ? 1 : 2;
    if (n > 280) break;
  }
  return i < chars.length ? chars.slice(0, i).join("") : null;
}

function renderPreview() {
  const text = $("text").value;
  const imgs = curImgs();
  const me = ME || {};
  $("pv").classList.toggle("dark", pvDark);
  $("pv-theme").textContent = pvDark ? "切换浅色" : "切换深色";
  $("pv-av").style.backgroundImage = me.avatar ? `url("${String(me.avatar).replace(/"/g, "")}")` : "";
  $("pv-nm").textContent = me.name || "你的名字";
  $("pv-ck").style.display = me.verified === false ? "none" : "";
  $("pv-sub").textContent = `@${me.handle || "你的账号"} · ${SEL ? SEL : "刚刚"}`;
  $("pv-note").textContent = ME
    ? "名字和头像来自你上次打开 X 时的账号。实际效果以 X 为准。"
    : "打开一次 X 网页，这里就会显示你自己的名字和头像。实际效果以 X 为准。";

  const tx = $("pv-tx");
  tx.textContent = "";
  tx.classList.toggle("ph", !text.trim() && !imgs.length);
  if (!text.trim() && !imgs.length) tx.textContent = "在左边写点什么，这里会实时显示。";
  else {
    const cut = cutAt280(text);
    tx.append(linkify(cut !== null ? cut.replace(/\s+$/, "") + "…" : text));
    if (cut !== null) {
      tx.append(" ");
      const more = document.createElement("span");
      more.className = "more";
      more.textContent = "显示更多";
      tx.append(more);
    }
  }
  const media = $("pv-media");
  media.textContent = "";
  media.hidden = !imgs.length;
  media.className = "media n" + Math.min(imgs.length, 4);
  for (const im of imgs.slice(0, 4)) {
    if (isVid(im.type)) {
      const v = document.createElement("video");
      v.src = srcOf(im);
      v.controls = true;
      v.muted = true;
      v.playsInline = true;
      v.preload = "metadata";
      media.appendChild(v);
    } else {
      const img = document.createElement("img");
      img.src = srcOf(im);
      img.alt = "";
      media.appendChild(img);
    }
  }
}

/* ---------------- 保存 ---------------- */
function setItem(d, hm, item, debounce) {
  if (!SCHED[d]) SCHED[d] = {};
  if (!hasContent(item)) delete SCHED[d][hm]; else SCHED[d][hm] = item;
  if (debounce) { clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 400); }
  else saveNow();
}

async function saveNow() {
  clearTimeout(saveTimer);
  saveTimer = null;
  /* 只留 14 天内的排期，图片一起清 */
  const cut = dateKey(new Date(Date.now() - 14 * 864e5));
  const dropKeys = [];
  for (const d of Object.keys(SCHED)) if (d < cut || !Object.keys(SCHED[d]).length) delete SCHED[d];
  let keys = Object.keys(IMGS);
  try { if (chrome.storage.local.getKeys) keys = [...new Set([...keys, ...(await chrome.storage.local.getKeys())])]; } catch (e) { /* 旧版 Chrome */ }
  for (const k of keys) {
    if (!k.startsWith("img:")) continue;
    const [d, hm] = k.slice(4).split("|");
    if (d < cut || !(SCHED[d] && SCHED[d][hm])) { delete IMGS[k]; dropKeys.push(k); }
  }
  if (dropKeys.length) { await chrome.storage.local.remove(dropKeys); sweepVideos(); }
  return chrome.storage.local.set({ schedule: SCHED });
}

function onTextInput() {
  const d = day(), hm = SEL;
  const cur = curItem();
  const text = $("text").value;
  /* 改了已发的内容，就当成一条新的待发 */
  const status = ["sent", "xsched"].includes(cur.status) && cur.text !== text ? "" : cur.status || "";
  setItem(d, hm, { ...cur, text, status, imgs: curImgs().length, vid: curImgs().some((x) => isVid(x.type)) }, true);
  renderEditor();
  const row = document.querySelector(`.cell[data-hm="${hm}"] .cs`);
  if (row) renderListSoon();
}
let listTimer = null;
function renderListSoon() { clearTimeout(listTimer); listTimer = setTimeout(renderList, 300); }

/* ---------------- 图片 ---------------- */
function readAsDataURL(blob) {
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(blob); });
}
async function prepareImage(file) {
  if (file.type === "image/gif") {
    if (file.size > GIF_MAX_BYTES) throw new Error(`GIF「${file.name}」超过 15MB，X 发不了`);
    return { type: file.type, data: await readAsDataURL(file) };
  }
  const bmp = await createImageBitmap(file);
  const big = Math.max(bmp.width, bmp.height);
  if (file.size <= IMG_MAX_BYTES && big <= 4096) { bmp.close(); return { type: file.type, data: await readAsDataURL(file) }; }
  /* 太大：缩到长边 2048，转成 JPEG */
  const k = Math.min(1, 2048 / big);
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * k);
  c.height = Math.round(bmp.height * k);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  bmp.close();
  return { type: "image/jpeg", data: c.toDataURL("image/jpeg", 0.9) };
}

async function addFiles(files) {
  if (!SEL) return;
  const list = [...files].filter((f) => /^image\/(png|jpe?g|webp|gif)$/.test(f.type) || /^video\/(mp4|quicktime)$/.test(f.type));
  if (!list.length) { $("ed-msg").textContent = "只能加 PNG、JPG、WEBP、GIF 图片，或者 MP4、MOV 视频。"; $("ed-msg").className = "msg err"; return; }
  const d = day(), hm = SEL, key = imgKey(d, hm);
  let imgs = [...curImgs()];
  const msg = [];
  for (const f of list) {
    const gif = f.type === "image/gif";
    const vid = isVid(f.type);
    if (imgs.some((x) => isVid(x.type)) || (vid && imgs.length)) { msg.push("视频只能单独发，一条帖子只能放 1 个视频，不能和图片放一起"); break; }
    if (imgs.some((x) => x.type === "image/gif") || (gif && imgs.length)) { msg.push("GIF 只能单独发，不能和别的图放一起"); break; }
    if (vid) {
      /* 视频不限大小：存进插件的 IndexedDB，不转成文字 */
      $("ed-msg").textContent = `正在保存视频（${fmtMB(f.size)}）…`;
      const id = "v" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
      try { await mediaPut(id, { blob: f, type: f.type, name: f.name, owner: key }); }
      catch (e) { msg.push(`视频「${f.name}」没存下来：${(e && e.message) || "电脑硬盘空间可能不够"}`); break; }
      imgs.push({ type: f.type, idb: id, size: f.size, name: f.name, url: URL.createObjectURL(f) });
      continue;
    }
    if (imgs.length >= MAX_IMG) { msg.push("X 一条最多 4 张图，多的没加"); break; }
    try { imgs.push(await prepareImage(f)); } catch (e) { msg.push(e.message || `「${f.name}」读不出来`); }
  }
  IMGS[key] = imgs;
  await chrome.storage.local.set({ [key]: toStore(imgs) });
  const cur = curItem();
  setItem(d, hm, { ...cur, text: $("text").value, status: ["sent", "xsched"].includes(cur.status) ? "" : cur.status || "", imgs: imgs.length, vid: imgs.some((x) => isVid(x.type)) });
  $("ed-msg").textContent = msg.join("；");
  $("ed-msg").className = "msg" + (msg.length ? " err" : "");
  renderEditor();
  renderList();
}

async function storeImgs(imgs) {
  const d = day(), hm = SEL, key = imgKey(d, hm);
  IMGS[key] = imgs;
  if (imgs.length) await chrome.storage.local.set({ [key]: toStore(imgs) });
  else { delete IMGS[key]; await chrome.storage.local.remove(key); }
  sweepVideos();
  const cur = curItem();
  setItem(d, hm, { ...cur, text: $("text").value, status: ["sent", "xsched"].includes(cur.status) ? "" : cur.status || "", imgs: imgs.length, vid: imgs.some((x) => isVid(x.type)) });
  renderEditor();
  renderList();
}
function removeImg(i) { const a = [...curImgs()]; a.splice(i, 1); storeImgs(a); }
function moveImg(i) { const a = [...curImgs()]; [a[i - 1], a[i]] = [a[i], a[i - 1]]; storeImgs(a); }

/* ---------------- 其他 ---------------- */
function saveSettings() {
  SET = {
    step: Number($("step").value) || 20,
    on: $("on").checked,
    autoArrange: $("auto").checked,
  };
  chrome.storage.local.set({ schedSettings: SET });
  /* 改了时间段，原来选中的格子不在里面了：换到下一个还没到时间的格子 */
  const t = allTimes(day());
  if (!t.includes(SEL)) select(t.find((hm) => !isPast(day(), hm)) || t[0] || null);
  else { renderList(); renderEditor(); }
}

function bulkFill() {
  const parts = $("bulk").value.split(/\n\s*\n/).map((x) => x.trim()).filter(Boolean);
  const msg = $("bulk-msg");
  if (!parts.length) { msg.textContent = "上面还没有内容。"; msg.className = "msg err"; return; }
  const d = day();
  const items = SCHED[d] || {};
  const free = slotTimes().filter((hm) => !isPast(d, hm) && !hasContent(items[hm]));
  let i = 0;
  for (; i < parts.length && i < free.length; i++) setItem(d, free[i], { text: parts[i], status: "" }, true);
  saveNow();
  const left = parts.length - i;
  $("bulk").value = left ? parts.slice(i).join("\n\n") : "";
  msg.textContent = left
    ? `填进了 ${i} 条，这天的空格子不够，还剩 ${left} 条留在框里。可以换到明天再填。`
    : `填进了 ${i} 条。`;
  msg.className = "msg " + (left ? "err" : "ok");
  if (i && !SEL) select(free[0]); else { renderList(); if (SEL) { $("text").value = curItem().text || ""; renderEditor(); } }
}

async function load() {
  const all = await chrome.storage.local.get(["schedule", "schedSettings", "me"]);
  SCHED = all.schedule || {};
  SET = { ...DEF_SET, ...(all.schedSettings || {}) };
  ME = all.me || null;
  IMGS = {};
  try { pvDark = localStorage.getItem("xfb-pv-dark") === "1"; } catch (e) { pvDark = matchMedia("(prefers-color-scheme: dark)").matches; }
  $("date").value = dateKey(new Date());
  $("step").value = String(SET.step);
  $("on").checked = SET.on !== false;
  $("auto").checked = SET.autoArrange !== false;
  /* 默认选中下一个还没到时间的格子 */
  const times = allTimes(day());
  await select(times.find((hm) => !isPast(day(), hm)) || times[0] || null);
  scrollToNow();
}

["step", "on", "auto"].forEach((id) => $(id).addEventListener("change", saveSettings));
$("date").addEventListener("change", () => { const t = allTimes(day()); select(t.find((hm) => !isPast(day(), hm)) || t[0] || null); });
$("bulk-go").addEventListener("click", bulkFill);
$("text").addEventListener("input", onTextInput);
$("text").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData ? e.clipboardData.files : [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});
$("add-img").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", () => { addFiles($("file").files); $("file").value = ""; });
const drop = $("drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => { e.preventDefault(); addFiles(e.dataTransfer.files); });
$("prev").addEventListener("click", () => { const t = allTimes(day()); const i = t.indexOf(SEL); if (i > 0) select(t[i - 1]); });
$("next").addEventListener("click", () => { const t = allTimes(day()); const i = t.indexOf(SEL); if (i >= 0 && i < t.length - 1) select(t[i + 1]); });
$("clear").addEventListener("click", async () => {
  $("text").value = "";
  const key = imgKey(day(), SEL);
  delete IMGS[key];
  await chrome.storage.local.remove(key);
  sweepVideos();
  setItem(day(), SEL, { text: "", status: "" });
  renderEditor();
  renderList();
});
$("to-x").addEventListener("click", async () => {
  await saveNow();
  chrome.runtime.sendMessage({ type: "xfb-sched-x", date: day(), time: SEL });
  $("ed-msg").textContent = "已打开 X 的发帖框。确认内容和时间后，在那边点「安排」。";
  $("ed-msg").className = "msg ok";
});
$("send-now").addEventListener("click", async () => {
  await saveNow();
  chrome.runtime.sendMessage({ type: "xfb-sched-open", date: day(), time: SEL });
});
$("pv-theme").addEventListener("click", () => {
  pvDark = !pvDark;
  try { localStorage.setItem("xfb-pv-dark", pvDark ? "1" : "0"); } catch (e) { /* 忽略 */ }
  renderPreview();
});
$("x-all").addEventListener("click", async () => {
  await saveNow();
  const msg = $("x-msg");
  chrome.runtime.sendMessage({ type: "xfb-sched-x-all", date: day() }, (r) => {
    msg.textContent = r && r.ok
      ? (SET.autoArrange !== false ? "已打开第一条。插件会一条接一条设好、自动点「安排」，你看着就行，想停随时点提示条上的「取消」。" : "已打开第一条。每条确认后点 X 的「安排」，会自动打开下一条，直到这天都交完。")
      : "这天没有可以交给 X 的帖子（已经过了时间的、已发的、已定时的会跳过）。";
    msg.className = "msg " + (r && r.ok ? "ok" : "err");
  });
});

/* 后台改了状态（到点提醒、发出去了、交给 X 了），这里跟着刷新；正在打字时不打断 */
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local") return;
  if (ch.me) { ME = ch.me.newValue || null; renderPreview(); }
  if (!ch.schedule || saveTimer) return;
  SCHED = ch.schedule.newValue || {};
  renderList();
  if (SEL && document.activeElement !== $("text")) renderEditor();
});
setInterval(() => { renderList(); if (SEL && document.activeElement !== $("text")) renderEditor(); }, 60000);

load();
