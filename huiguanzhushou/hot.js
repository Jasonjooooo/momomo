/* 热点雷达：分析收集到的「每小时浏览 ≥ 5000」的新帖。收集在 content.js，抓取页由 background.js 打开 */
"use strict";

const $ = (id) => document.getElementById(id);
let HOT = {};
let PICK = null;
let BLOCK = [];             // 回关助手面板里设的屏蔽词：带这些词的热帖不算            // 点了哪个词：{ kind, value }

/* 中文、英文常见的虚词，统计热词时不算 */
const STOP = new Set(`的 了 是 我 你 他 她 它 们 我们 你们 他们 她们 这 那 这个 那个 这些 那些 这样 那样 这么 那么 一个 一些 一下 一样 一直 一定 一起
没有 不是 就是 还是 也是 都是 只是 但是 而是 可是 于是 或是 不会 不能 不要 不用 不过 不知道 知道 觉得 感觉 认为 以为 看到 看看 看着 说 说的 说了
什么 怎么 怎么样 为什么 哪里 哪个 多少 如何 谁 吗 呢 吧 啊 呀 哦 嘛 啦 哈 哈哈 哈哈哈 嗯 哎 唉 呃 喔 噢 欸
因为 所以 如果 虽然 然后 而且 并且 或者 还有 以及 及 和 与 跟 同 对 对于 关于 至于 由于 为了 通过 根据 按照 除了
在 有 也 都 就 要 会 能 可以 可能 应该 需要 必须 已经 正在 曾经 将 将要 还 又 再 更 最 很 太 非常 特别 比较 真的 真是 实在 确实 其实 当然 简直
自己 大家 人家 别人 有人 有的 所有 每个 各种 任何 一切 其他 其它 别的 什么样 这种 那种 某些
现在 今天 昨天 明天 今年 去年 时候 时间 以后 之后 以前 之前 刚才 马上 立刻 终于 一次 第一 一点 有点 一边
上 下 里 中 内 外 前 后 来 去 到 从 被 把 让 给 向 往 比 像 等 等等 啊啊 呗 罢了 而已 之 其 此 该 以 于 而 且 并 但 或 即 若 则 乃
个 只 条 种 次 位 年 月 日 号 点 分 秒 万 亿 多 少 好 好的 很多 不少 一般 东西 事情 问题 方面 情况 地方 样子 部分 结果 原因 意思
the a an and or but if of to in on at for with by from as is are was were be been being am do does did done have has had having
this that these those it its it's i me my we our you your he him his she her they them their what which who whom whose when where why how
not no yes so too very can could will would should may might must just also only than then there here about into over after before
all any some more most other such up out off down own same few each both again further once now ever even still like get got
https http www com co amp rt via new one two`.split(/\s+/));

const segmenter = typeof Intl !== "undefined" && Intl.Segmenter ? new Intl.Segmenter("zh", { granularity: "word" }) : null;
const URL_RE = /https?:\/\/\S+/g, TAG_RE = /[#＃]([\p{L}\p{N}_]{1,40})/gu, AT_RE = /@([A-Za-z0-9_]{1,15})/g;
const isCJK = (w) => /[㐀-鿿぀-ヿ가-힯]/.test(w);

/* 一条帖子里的词（同一条里重复的只算一次）。
 * 中文：浏览器自带的分词对新闻词很差（「美联储」会切成「美/联/储」），所以把连续的汉字切成 2～6 个字的片段，
 *       在所有热帖里数片段出现了几次，再去掉被更长的词包含的（见 keepLongest）。
 * 英文等：按单词，外加相邻两个词组成的词组。 */
const EDGE = new Set([..."的了是在和也都就吗呢吧啊呀哦嘛啦我你他她它这那有不个们一很被把让给对说要会能着过还又而与及其之于以从到为来去上下里中后前就才更最没别等再并或但如若即每各某该此"]);
const CJK_RUN = /[㐀-鿿]+/g;
function wordsOf(text) {
  const clean = String(text || "").replace(URL_RE, " ").replace(TAG_RE, " ").replace(AT_RE, " ");
  const words = new Set(), phrases = new Set();
  for (const run of clean.match(CJK_RUN) || []) {
    for (let n = 2; n <= 6; n++) {
      for (let i = 0; i + n <= run.length; i++) {
        const g = run.slice(i, i + n);
        if (EDGE.has(g[0]) || EDGE.has(g[g.length - 1]) || STOP.has(g)) continue;
        words.add(g);
      }
    }
  }
  const latin = clean.replace(CJK_RUN, " ").toLowerCase().split(/[^\p{L}\p{N}_'’-]+/u).filter(Boolean);
  const ok = (w) => w && !STOP.has(w) && !/^\d+([.,]\d+)?$/.test(w) && w.length >= 2 && !isCJK(w);
  for (const w of latin) if (ok(w)) words.add(w);
  for (let i = 0; i + 1 < latin.length; i++) if (ok(latin[i]) && ok(latin[i + 1])) phrases.add(latin[i] + " " + latin[i + 1]);
  return { words, phrases };
}
/* 去掉被更长的词「几乎总是一起出现」的短片段：「联储」总跟着「美联储」，就只留「美联储」 */
function keepLongest(list) {
  const top = list.filter((x) => x.n >= 2);
  const byLen = [...top].sort((a, b) => b.k.length - a.k.length);
  const kept = [];
  for (const x of byLen) {
    if (isCJK(x.k) && kept.some((y) => isCJK(y.k) && y.k.length > x.k.length && y.k.includes(x.k) && y.n >= x.n * 0.8)) continue;
    kept.push(x);
  }
  /* 长片段如果其实是两个热词拼起来、出现次数也不比它们多，也拆掉 */
  return kept.sort((a, b) => b.n - a.n || b.heat - a.heat || b.k.length - a.k.length);
}
const tagsOf = (t) => new Set([...String(t || "").matchAll(TAG_RE)].map((m) => "#" + m[1].toLowerCase()));
const atsOf = (t) => new Set([...String(t || "").matchAll(AT_RE)].map((m) => "@" + m[1]));

const fmtN = (n) => (n >= 1e8 ? (n / 1e8).toFixed(1) + "亿" : n >= 1e4 ? (n / 1e4).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "万" : String(Math.round(n)));
const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);
const median = (arr) => { if (!arr.length) return 0; const s = [...arr].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
function xLen(s) { let n = 0; for (const ch of String(s || "").replace(URL_RE, "")) n += ch.codePointAt(0) <= 0x10ff ? 1 : 2; return n; }
function ago(t) {
  const m = Math.max(1, Math.round((Date.now() - t) / 60000));
  return m < 60 ? `${m} 分钟前` : m < 48 * 60 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`;
}

function filtered() {
  const range = Number($("range").value) * 36e5, min = Number($("min").value), lang = $("lang").value;
  return Object.entries(HOT)
    .map(([id, h]) => ({ id, ...h }))
    .filter((h) => !BLOCK.some((w) => (String(h.text || "") + (h.name || "")).toLowerCase().replace(/\s+/g, "").includes(w)))
    .filter((h) => Date.now() - h.postedAt <= range && h.vph >= min && (!lang || String(h.lang || "").startsWith(lang)))
    .sort((a, b) => b.vph - a.vph);
}

/* 数一数：每个词出现在几条帖子里、这些帖子的热度加起来多少 */
function count(posts, getter) {
  const m = new Map();
  for (const p of posts) for (const k of getter(p)) {
    const e = m.get(k) || { n: 0, heat: 0 };
    e.n++; e.heat += p.vph;
    m.set(k, e);
  }
  return [...m.entries()].map(([k, e]) => ({ k, ...e })).sort((a, b) => b.n - a.n || b.heat - a.heat);
}

function chips(el, list, kind, minN, max) {
  el.textContent = "";
  const top = list.filter((x) => x.n >= minN).slice(0, max);
  if (!top.length) { const p = document.createElement("span"); p.className = "empty"; p.textContent = "还没有在 2 条以上热帖里都出现的。"; el.appendChild(p); return; }
  const hi = top[0].n;
  for (const x of top) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip " + (x.n >= hi * 0.6 && x.n >= 3 ? "l3" : x.n >= hi * 0.3 && x.n >= 2 ? "l2" : "l1");
    b.setAttribute("aria-pressed", String(!!(PICK && PICK.kind === kind && PICK.value === x.k)));
    b.title = `出现在 ${x.n} 条热帖里，这些帖子加起来每小时 ${fmtN(x.heat)} 浏览`;
    b.append(x.k);
    const s = document.createElement("small");
    s.textContent = x.n;
    b.appendChild(s);
    b.addEventListener("click", () => { PICK = PICK && PICK.kind === kind && PICK.value === x.k ? null : { kind, value: x.k }; render(); });
    el.appendChild(b);
  }
}

function stat(label, big, sub, parts) {
  const d = document.createElement("div");
  d.className = "stat";
  const b = document.createElement("b");
  b.textContent = big;
  const s = document.createElement("span");
  s.textContent = label + (sub ? " · " + sub : "");
  d.append(b, s);
  if (parts) {
    const m = document.createElement("div");
    m.className = "meter";
    for (const [v, c] of parts) { const i = document.createElement("i"); i.style.width = v + "%"; i.style.background = c; m.appendChild(i); }
    d.appendChild(m);
  }
  return d;
}

function render() {
  const posts = filtered();
  const n = posts.length;
  $("sum-n").textContent = n ? `${n} 条` : "";
  const st = $("stats");
  st.textContent = "";
  const parsed = posts.map((p) => ({ p, ...wordsOf(p.text), tags: tagsOf(p.text), ats: atsOf(p.text) }));

  if (!n) {
    $("sum").textContent = Object.keys(HOT).length
      ? "这个范围里还没有热帖。换个时间范围或门槛看看，或者点「抓取一次」。"
      : "还没收集到热帖。点上面的「抓取一次」，或者去 X 首页、搜索页刷一会儿，再回来看。";
  } else {
    const vid = posts.filter((p) => p.media === "vid").length, img = posts.filter((p) => p.media === "img").length, txt = n - vid - img;
    const lens = posts.map((p) => xLen(p.text));
    const medLen = Math.round(median(lens));
    const short = lens.filter((l) => l < 60).length, long = lens.filter((l) => l > 280).length;
    const ver = posts.filter((p) => p.verified).length;
    const withTag = parsed.filter((x) => x.tags.size).length;
    const withLink = posts.filter((p) => /https?:\/\//.test(p.text)).length;
    const ask = posts.filter((p) => /[?？]/.test(p.text)).length;
    const quote = posts.filter((p) => p.quote).length;
    const likeRate = median(posts.filter((p) => p.views).map((p) => p.likes / p.views)) * 100;
    const replyRate = median(posts.filter((p) => p.views).map((p) => p.replies / p.views)) * 100;
    const words = keepLongest(count(parsed, (x) => x.words));
    const topW = words.filter((w) => w.n >= 2).slice(0, 5).map((w) => `「${w.k}」`);
    const mediaWord = vid >= img && vid >= txt ? `视频最多（${pct(vid, n)}%）` : img >= txt ? `带图的最多（${pct(img, n)}%）` : `纯文字最多（${pct(txt, n)}%）`;
    const lenWord = medLen < 60 ? "多数是短帖" : medLen > 200 ? "多数是长文" : "长度中等";
    $("sum").textContent = `这 ${n} 条热帖里，${mediaWord}；${lenWord}（中间值 ${medLen} 字符）；${pct(ver, n)}% 是蓝 V 发的。`
      + (topW.length ? `反复出现的词：${topW.join("、")}。` : "热词还不集中，多收一些再看。")
      + (ask >= n * 0.3 ? `有 ${pct(ask, n)}% 带问句，提问容易引来回复。` : "");

    st.append(
      stat("中间值每小时浏览", fmtN(median(posts.map((p) => p.vph))), `最高 ${fmtN(posts[0].vph)}`),
      stat("视频 / 图片 / 纯文字", `${pct(vid, n)} / ${pct(img, n)} / ${pct(txt, n)}%`, "", [[pct(vid, n), "#f4212e"], [pct(img, n), "#1d9bf0"], [pct(txt, n), "#8b98a5"]]),
      stat("长度中间值", `${medLen} 字符`, `短帖 ${pct(short, n)}% · 超长 ${pct(long, n)}%`),
      stat("蓝 V 发的", `${pct(ver, n)}%`, "", [[pct(ver, n), "#1d9bf0"]]),
      stat("带话题标签", `${pct(withTag, n)}%`, "", [[pct(withTag, n), "#7856ff"]]),
      stat("带链接", `${pct(withLink, n)}%`, "", [[pct(withLink, n), "#00ba7c"]]),
      stat("带问句", `${pct(ask, n)}%`, "", [[pct(ask, n), "#ffad1f"]]),
      stat("引用别的帖子", `${pct(quote, n)}%`, "", [[pct(quote, n), "#536471"]]),
      stat("点赞 / 浏览", `${likeRate.toFixed(1)}%`, "中间值"),
      stat("回复 / 浏览", `${replyRate.toFixed(2)}%`, "中间值"),
    );
  }

  const allWords = keepLongest(count(parsed, (x) => x.words));
  /* 热词：2～3 个字的中文词和英文单词；词组：4 个字以上的中文片段和英文词组 */
  chips($("words"), allWords.filter((w) => !isCJK(w.k) || w.k.length <= 3), "word", 2, 60);
  chips($("phrases"), [...allWords.filter((w) => isCJK(w.k) && w.k.length >= 4), ...keepLongest(count(parsed, (x) => x.phrases))].sort((a, b) => b.n - a.n), "phrase", 2, 30);
  chips($("tags"), count(parsed, (x) => x.tags), "tag", 1, 30);
  const people = [
    ...count(parsed, (x) => x.ats).filter((x) => x.n >= 2),
    ...count(parsed, (x) => [x.p.name ? `${x.p.name}（@${x.p.handle}）` : "@" + x.p.handle]).filter((x) => x.n >= 2),
  ].sort((a, b) => b.n - a.n);
  chips($("people"), people, "person", 2, 30);

  /* 列表：点了某个词，就只看带这个词的 */
  const has = (x) => {
    if (!PICK) return true;
    const v = PICK.value;
    if (PICK.kind === "word") return x.words.has(v);
    if (PICK.kind === "phrase") return x.phrases.has(v) || x.words.has(v);
    if (PICK.kind === "tag") return x.tags.has(v);
    return x.ats.has(v) || v.endsWith(`（@${x.p.handle}）`) || v === "@" + x.p.handle;
  };
  const show = parsed.filter(has).slice(0, 150);
  $("list-title").textContent = PICK ? `带「${PICK.value}」的热帖（${parsed.filter(has).length} 条，点上面的词可以取消）` : `热帖（按每小时浏览排，前 ${Math.min(150, n)} 条）`;
  const list = $("list");
  list.textContent = "";
  if (!show.length) { const p = document.createElement("p"); p.className = "empty"; p.textContent = "没有。"; list.appendChild(p); return; }
  for (const { p } of show) {
    const row = document.createElement("article");
    row.className = "p";
    const av = document.createElement("div");
    av.className = "av";
    if (p.avatar) av.style.backgroundImage = `url("${String(p.avatar).replace(/"/g, "")}")`;
    const body = document.createElement("div");
    const hd = document.createElement("div");
    hd.className = "hd";
    const nm = document.createElement("b");
    nm.textContent = (p.name || p.handle) + (p.verified ? " ✓" : "");
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = `@${p.handle} · ${ago(p.postedAt)}发`;
    hd.append(nm, sub);
    const tx = document.createElement("div");
    tx.className = "tx";
    markText(tx, p.text || "（没有文字）", PICK && PICK.kind !== "person" ? PICK.value : "");
    const mt = document.createElement("div");
    mt.className = "mt";
    for (const s of [p.media === "vid" ? "🎬 视频" : p.media === "img" ? "🖼 图片" : "纯文字", `${fmtN(p.views)} 浏览`, `${fmtN(p.likes)} 赞`, `${fmtN(p.replies)} 回复`, `${fmtN(p.reposts)} 转发`]) {
      const sp = document.createElement("span"); sp.textContent = s; mt.appendChild(sp);
    }
    const a = document.createElement("a");
    a.href = "https://x.com" + p.url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "在 X 打开";
    mt.appendChild(a);
    body.append(hd, tx, mt);
    const heat = document.createElement("span");
    heat.className = "heat" + (p.vph < 1e4 ? " t3" : "");
    heat.textContent = fmtN(p.vph) + "/时";
    row.append(av, body, heat);
    list.appendChild(row);
  }
}
/* 把选中的词标亮（不用 innerHTML） */
function markText(el, text, word) {
  const w = String(word || "").replace(/^#/, "");
  if (!w) { el.textContent = text; return; }
  const parts = w.includes(" ") ? [w] : [w];
  const low = text.toLowerCase();
  let i = 0;
  while (i < text.length) {
    const j = low.indexOf(parts[0].toLowerCase(), i);
    if (j < 0) { el.append(text.slice(i)); break; }
    el.append(text.slice(i, j));
    const m = document.createElement("mark");
    m.textContent = text.slice(j, j + parts[0].length);
    el.appendChild(m);
    i = j + parts[0].length;
  }
}

const blockOf = (s) => String(s || "").split(/[,，、]/).map((x) => x.trim().toLowerCase().replace(/\s+/g, "")).filter(Boolean);
function setMsg(t, k) { $("msg").textContent = t || ""; $("msg").className = "msg" + (k ? " " + k : ""); }

async function load() {
  const v = await chrome.storage.local.get(["blockWords", "hot", "hotQuery", "hotAge", "hotMin", "hotFetchedAt", "hotView"]);
  HOT = v.hot || {};
  BLOCK = blockOf(v.blockWords);
  $("q").value = v.hotQuery || "lang:zh min_faves:20";
  $("age").value = Number(v.hotAge) || 6;
  $("hmin").value = Number(v.hotMin) || 5000;
  const view = v.hotView || {};
  if (view.range) $("range").value = view.range;
  if (view.min) $("min").value = view.min;
  if (view.lang !== undefined) $("lang").value = view.lang;
  if (v.hotFetchedAt) setMsg(`上次抓取：${new Date(v.hotFetchedAt).toLocaleString("zh-CN", { hour12: false })}。一共收了 ${Object.keys(HOT).length} 条热帖（3 天内的）。`);
  else setMsg(`一共收了 ${Object.keys(HOT).length} 条热帖（3 天内的）。`);
  render();
}
for (const id of ["range", "min", "lang"]) $(id).addEventListener("change", () => {
  PICK = null;
  chrome.storage.local.set({ hotView: { range: $("range").value, min: $("min").value, lang: $("lang").value } });
  render();
});
$("q").addEventListener("change", () => chrome.storage.local.set({ hotQuery: $("q").value.trim() || "lang:zh min_faves:20" }));
$("age").addEventListener("change", () => chrome.storage.local.set({ hotAge: Math.min(72, Math.max(1, Number($("age").value) || 6)) }));
$("hmin").addEventListener("change", () => chrome.storage.local.set({ hotMin: Math.max(500, Number($("hmin").value) || 5000) }));
let armClear = false;
$("clear").addEventListener("click", async () => {
  if (!armClear) { armClear = true; $("clear").textContent = "确定清空？再点一次"; setTimeout(() => { armClear = false; $("clear").textContent = "清空收集的帖子"; }, 4000); return; }
  armClear = false;
  $("clear").textContent = "清空收集的帖子";
  await chrome.storage.local.set({ hot: {} });
});
$("fetch").addEventListener("click", () => {
  $("fetch").disabled = true;
  setMsg("正在打开 X 搜索（最近几小时的热门帖），会自动往下翻 25 屏左右，收完自动关掉、回到这里。大约 1 分钟，期间别关那个页面。");
  chrome.runtime.sendMessage({ type: "xfb-hot-fetch" }, (r) => {
    if (!r || !r.ok) { $("fetch").disabled = false; setMsg("没能打开 X，刷新一下这个页面再试。", "err"); }
  });
});
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local") return;
  if (ch.hot) { HOT = ch.hot.newValue || {}; render(); }
  if (ch.blockWords) { BLOCK = blockOf(ch.blockWords.newValue); render(); }
  if (ch.hotFetchedAt && ch.hotFetchedAt.newValue) {
    $("fetch").disabled = false;
    setMsg(`抓取完成。现在一共收了 ${Object.keys(HOT).length} 条热帖（3 天内的）。`, "ok");
  }
});
setInterval(render, 5 * 60000);
load();
