/* 发帖排期表的视频：存在插件自己的 IndexedDB 里（不放 chrome.storage，那里放不下大文件），没有大小限制。
 * 排期表页面（schedule.js）存、删；后台（background.js）按块读出来，交给 X 页面里的 content.js 拼回视频文件。 */
"use strict";

const MEDIA_DB = "xfb-media", MEDIA_STORE = "files";
function mediaDb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(MEDIA_DB, 1);
    r.onupgradeneeded = () => { if (!r.result.objectStoreNames.contains(MEDIA_STORE)) r.result.createObjectStore(MEDIA_STORE); };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
async function mediaTx(mode, fn) {
  const db = await mediaDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(MEDIA_STORE, mode);
    const st = tx.objectStore(MEDIA_STORE);
    let out;
    Promise.resolve(fn(st)).then((v) => { out = v; });
    tx.oncomplete = () => { db.close(); resolve(out); };
    tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); };
  });
}
const reqP = (r) => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });

/* rec = { blob, type, name, owner }，owner 是排期表格子的 key（img:日期|时间） */
function mediaPut(id, rec) { return mediaTx("readwrite", (st) => { st.put(rec, id); }); }
function mediaGet(id) { return mediaTx("readonly", (st) => reqP(st.get(id))); }
function mediaDel(id) { return mediaTx("readwrite", (st) => { st.delete(id); }); }
function mediaAll() {
  return mediaTx("readonly", async (st) => {
    const keys = await reqP(st.getAllKeys());
    const vals = await reqP(st.getAll());
    return keys.map((k, i) => [k, vals[i]]);
  });
}

/* 一块转成 base64 发出去（插件消息只能传文字） */
async function mediaChunk(id, offset, len) {
  const rec = await mediaGet(id);
  if (!rec || !rec.blob) return { ok: false };
  const part = new Uint8Array(await rec.blob.slice(offset, offset + len).arrayBuffer());
  let bin = "";
  for (let i = 0; i < part.length; i += 0x8000) bin += String.fromCharCode.apply(null, part.subarray(i, i + 0x8000));
  return { ok: true, size: rec.blob.size, type: rec.type, data: btoa(bin) };
}
