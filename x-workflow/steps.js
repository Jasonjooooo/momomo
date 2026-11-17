/* 时间工作流：页面和后台共用的步骤定义和小工具 */
"use strict";

const pad = (n) => String(n).padStart(2, "0");
const dateKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const toMin = (hm) => { const [h, m] = String(hm).split(":").map(Number); return (h || 0) * 60 + (m || 0); };
const fmt = (m) => pad(Math.floor(m / 60)) + ":" + pad(m % 60);
const hasContent = (it) => !!(it && ((it.text && it.text.trim()) || it.imgs > 0 || it.vid));
const weekdayOf = (key) => new Date(key + "T00:00:00").getDay();   // 0 = 周日

const DAY_END = 24 * 60;
const MISS_AFTER = 10;      // 跟回关助手一致：到点 10 分钟还没发就算错过
const FOLLOW_AFTER = 10;    // 帖子发出后多久开始回评论
const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6];

const KINDS = { prep: "准备素材", post: "发帖", talk: "互动", review: "复盘", other: "其他", follow: "关注" };
const ACTIONS_LIST = {
  none: "不要按钮",
  sched: "打开发帖排期表",
  inbox: "打开待回复评论",
  pool: "打开互关池",
  xrf: "启动 X 自动回复流程",
  tg: "启动 TG二创",
  url: "打开一个网址",
};
const XRF_TABS = ["为你推荐", "正在关注", "新闻"];
const TG_MODES = { all: "下载并处理", download: "只下载", process: "只处理" };
const TG_PORT = 38719;      // TG二创 在本机开的接口（只认时间工作流插件）
const AUTO_WINDOW = 10;     // 到点那一分钟 Chrome 没开 / 电脑在睡：10 分钟内醒来还会补做，再晚就不补了

/* 每种按钮「到点自动」做什么 */
const AUTO_TEXT = {
  xrf: "开始这一轮自动回复",
  tg: "叫 TG二创 开始",
  inbox: "打开待回复评论并抓取最新评论",
  pool: "打开互关池（关注还是你按 F）",
  sched: "打开发帖排期表",
  url: "打开这个网址",
};
const EVERY_CHOICES = [10, 15, 20, 30, 45, 60, 90, 120, 180];

/* 一个步骤长这样（存在 chrome.storage.local.wfSteps 里）：
 * { id, title, kind, mode: "once" | "repeat",
 *   t（只做一次：开始时间，分钟）, dur（大概多久，文字）,
 *   every / from / to（重复做：每隔几分钟、从几点到几点，分钟）,
 *   items（要做的事，一行一条）, tool（用的工具）,
 *   action: { type, url, count, tab, auto }（卡片上的按钮）,
 *   days（哪几天做，0 = 周日）, idle（挂机）, notify（到点提醒）, custom（你自己加的） } */
const DEFAULT_STEPS = [
  { id: "data", t: "08:30", kind: "review", title: "看昨天的数据", dur: "15 分钟", items: ["看昨天帖子的浏览、点赞、转发、涨粉", "记下哪类内容最好，今天多排这类"], tool: "X 数据分析" },
  { id: "pick", t: "09:00", kind: "prep", title: "挑素材", dur: "30 分钟", items: ["TG二创「浏览」按标签逛群，点开视频看前 8 秒", "挑出今天要用的，点下载"], tool: "TG二创 · 浏览" },
  { id: "remix", t: "09:30", kind: "prep", idle: true, title: "批量二创", dur: "20–40 分钟", items: ["TG二创 下载新素材并批量处理，这段时间可以做别的"], tool: "TG二创", action: { type: "tg", mode: "all" } },
  { id: "write", t: "10:15", kind: "prep", title: "写文案，填进发帖排期表", dur: "30 分钟", items: ["每条成品看一遍，写一句开头钩子", "在排期表里填好文字和视频，点「把这天还没发的都交给 X 定时」"], action: { type: "sched" } },
  { id: "reply1", t: "12:30", kind: "talk", idle: true, title: "自动回复 第 1 轮", dur: "约 40 分钟", items: [], action: { type: "xrf", count: 30, tab: "为你推荐" } },
  { id: "reply2", t: "19:00", kind: "talk", idle: true, title: "自动回复 第 2 轮", dur: "约 40 分钟", items: [], action: { type: "xrf", count: 30, tab: "正在关注" } },
  { id: "inbox", t: "22:00", kind: "talk", title: "亲手回评论和私信", dur: "30 分钟", items: ["先点「抓取最新评论」，再一条条回", "真人互动最涨粉，这步别用自动回复"], action: { type: "inbox" } },
  { id: "follow", follow: true, mode: "repeat", every: 20, from: toMin("10:00"), to: toMin("22:40"), kind: "follow", title: "关注新人", dur: "1 分钟", items: ["打开互关池，连按 F 关注，一轮 15 个"], action: { type: "pool" }, notify: false },
  { id: "wrap", t: "23:00", kind: "review", title: "收尾", dur: "10 分钟", items: ["看看明天的排期写满没有，没写的先挑好素材"], action: { type: "sched" } },
].map((s) => normStep({ ...s, t: s.t != null ? toMin(s.t) : undefined }));

/* 补全缺的字段，修正不合理的值（读存储、保存前都过一遍） */
function normStep(s) {
  const n = {
    id: s.id || "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    title: String(s.title || "").trim() || "未命名步骤",
    kind: KINDS[s.kind] ? s.kind : "other",
    mode: s.mode === "repeat" ? "repeat" : "once",
    t: clampMin(Number.isFinite(s.t) ? s.t : 9 * 60),
    dur: String(s.dur || "").trim(),
    every: Math.max(5, Math.min(720, Math.round(Number(s.every) || 30))),
    from: clampMin(Number.isFinite(s.from) ? s.from : 10 * 60),
    to: clampMin(Number.isFinite(s.to) ? s.to : 22 * 60),
    items: (Array.isArray(s.items) ? s.items : []).map((x) => String(x).trim()).filter(Boolean),
    tool: String(s.tool || "").trim(),
    action: { type: "none", ...(s.action || {}) },
    days: Array.isArray(s.days) && s.days.length ? [...new Set(s.days.map(Number).filter((d) => d >= 0 && d <= 6))].sort() : [...ALL_DAYS],
    idle: !!s.idle,
    notify: s.notify !== false,
    custom: !!s.custom,
    /* 到点自动开始：没设过的（旧版存的步骤），有按钮就默认开 */
    auto: s.auto != null ? !!s.auto : !!(s.action && s.action.type && s.action.type !== "none"),
    follow: !!s.follow,      // 关注新人：每一格的关注数由回关助手自动统计
  };
  if (n.follow) { n.mode = "repeat"; n.kind = "follow"; n.action = { type: "pool" }; }
  else if (n.kind === "follow") n.kind = "talk";
  if (!ACTIONS_LIST[n.action.type]) n.action.type = "none";
  if (n.action.type === "xrf") {
    n.action.count = Math.max(1, Math.min(500, Math.round(Number(n.action.count) || 30)));
    if (!XRF_TABS.includes(n.action.tab)) n.action.tab = XRF_TABS[0];
    n.action.auto = !!n.action.auto;
  }
  if (n.action.type === "url") n.action.url = String(n.action.url || "").trim();
  if (n.action.type === "tg" && !TG_MODES[n.action.mode]) n.action.mode = "all";
  delete n.action.auto;                   // 旧版只有自动回复有「到点自动开始」，现在是每一步都有的 auto
  if (n.action.type === "none") n.auto = false;
  if (n.to < n.from) n.to = n.from;
  return n;
}
function clampMin(m) { return Math.max(0, Math.min(DAY_END - 5, Math.round(m / 5) * 5)); }

/* 重复做：一天里的每一次（分钟） */
function occurrences(s) {
  if (s.mode !== "repeat") return [s.t];
  const out = [];
  for (let m = s.from; m <= s.to && out.length < 300; m += s.every) out.push(m);
  return out;
}
const onDay = (s, key) => s.days.includes(weekdayOf(key));

function daysText(days) {
  const d = [...days].sort();
  if (d.length === 7) return "每天";
  if (d.join() === "1,2,3,4,5") return "工作日";
  if (d.join() === "0,6") return "周末";
  const names = "日一二三四五六";
  const order = [1, 2, 3, 4, 5, 6, 0].filter((x) => d.includes(x));
  return "周" + order.map((x) => names[x]).join(" ");
}
function everyText(m) {
  if (m % 60 === 0) return `${m / 60} 小时`;
  if (m > 60 && m % 30 === 0) return `${m / 60} 小时`;
  return `${m} 分钟`;
}

/* 读存储里的步骤；第一次用就是默认步骤（旧版拖动过的时间会带过来）。
 * 1.3 之前「关注新人」是固定显示的，第一次读到时把它变成一个普通步骤（用旧的显示时段），之后删了就不再加回来 */
function loadSteps(v) {
  let steps;
  if (Array.isArray(v.wfSteps)) steps = v.wfSteps.map(normStep);
  else {
    const times = v.wfTimes || {};
    steps = DEFAULT_STEPS.filter((s) => !s.follow).map((s) => normStep({ ...s, t: Number.isFinite(times[s.id]) ? times[s.id] : s.t }));
  }
  /* 1.4：默认的「批量二创」接上 TG二创（只改没动过按钮的） */
  if (!v.wfV14) steps = steps.map((s) => (s.id === "remix" && !s.custom && s.action.type === "none" ? normStep({ ...s, action: { type: "tg", mode: "all" }, auto: true }) : s));
  if (!v.wfFollowMigrated && !steps.some((s) => s.follow)) {
    const old = { ...FOLLOW_DEF, ...(v.wfFollow || {}) };
    if (old.on !== false) {
      const f = DEFAULT_STEPS.find((s) => s.follow);
      steps.push(normStep({ ...f, from: old.start * 60, to: Math.max(old.start * 60, old.end * 60 - f.every) }));
    }
  }
  return steps;
}

/* 旧版（1.2 及以前）「关注新人」的显示时段，只用来升级 */
const FOLLOW_DEF = { on: true, start: 10, end: 23 };
