/* 回关助手 2.0 · X 涨粉小工具
 * 四种页面：
 *  - 你自己的「正在关注」页：标出没回关的人，手动取关；顺便记下你关注了谁
 *  - 首页 / 搜索 / 话题页：高亮带关键词（默认 #蓝V互关）的帖子
 *  - 帖子详情页：下滑到底，列出回复里你还没关注的人
 *  - 个人主页：你点了关注，插件记下来，并从待关注名单里划掉
 * 关注、取关主要由你亲手点；待关注池打开后可自动连点关注（3.5 秒一个、满 15 个停），取关池打开后可自动连点取关（3 秒一个、满 50 个停），随时可停。
 */
(() => {
  "use strict";
  if (window.__xfbLoaded) return;
  window.__xfbLoaded = true;

  const UNFOLLOW_LIMIT = 150;
  const FOLLOW_LIMIT = Infinity;       // 关注不设上限（X 自己的限制仍然有效，碰到会提示）
  const UNFOLLOW_COOLDOWN_MS = 1500;
  const DEFAULT_KEYWORDS = "蓝V互关";
  const SEL = {
    column: '[data-testid="primaryColumn"]',
    cell: '[data-testid="UserCell"]',
    followsYou: '[data-testid="userFollowIndicator"]',
    unfollowBtn: '[data-testid$="-unfollow"]',
    followBtn: '[data-testid$="-follow"]',
    confirm: '[data-testid="confirmationSheetConfirm"]',
    profileLink: 'a[data-testid="AppTabBar_Profile_Link"]',
    tweet: 'article[data-testid="tweet"]',
    tweetText: '[data-testid="tweetText"]',
    userName: '[data-testid="User-Name"]',
    verified: '[data-testid="icon-verified"]',
  };
  const HANDLE_RE = /^\/([A-Za-z0-9_]{1,15})$/;
  const RESERVED = new Set(["home", "explore", "notifications", "messages", "search", "i", "settings",
    "compose", "hashtag", "login", "logout", "signup", "tos", "privacy", "jobs", "premium", "lists",
    "bookmarks", "communities", "verified-choose", "account", "intent", "share"]);
  const MORE_REPLIES_RE = /^(显示更多回复|显示更多帖子|显示其他回复|Show more replies|Show additional replies.*)$/;

  /* ---------- 小工具 ---------- */
  const today = () => {
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const setText = (el, text) => { if (el && el.textContent !== String(text)) el.textContent = text; };
  const waitFor = async (fn, timeout = 3000, step = 100) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const v = fn();
      if (v) return v;
      await sleep(step);
    }
    return null;
  };
  const norm = (s) => String(s || "").toLowerCase().replace(/[#＃\s]/g, "");
  const keyOf = (h) => String(h).toLowerCase();

  /* ---------- 存储 ---------- */
  const KEYS = ["kept", "log", "done", "following", "followingSyncedAt", "followLog", "pending", "keywords", "onlyVerified", "minReplies", "remindOn", "remindMin", "heatAll", "replied", "replyLog", "replyGoal", "handled", "followedAt", "graceDays", "autoJump", "pool", "unpool", "blockWords"];
  const store = {
    async get() {
      try { return await chrome.storage.local.get(KEYS); } catch (e) { return {}; }
    },
    async set(patch) {
      try { await chrome.storage.local.set(patch); } catch (e) { /* 存不了就算了 */ }
    },
  };

  /* ---------- 状态 ---------- */
  const S = {
    kept: {}, log: {}, done: {},
    following: {},            // key -> handle，你关注的人
    followingSyncedAt: 0,
    followLog: {},            // 日期 -> 当天关注数
    pending: {},              // key -> 时间，从互关帖里列出的待关注
    keywords: DEFAULT_KEYWORDS,
    onlyVerified: true,
    heatAll: true,            // 所有帖子都显示热度，不只互关帖
    replied: {},              // 帖子 ID -> 回复时间，你回复过的帖子
    replyLog: {},             // 日期 -> 当天回复数
    replyGoal: 500,
    handled: {},              // 帖子 ID -> 时间，处理完的互关帖
    followedAt: {},           // 账号 -> 你关注他的时间（从这一版开始记）
    graceDays: 3,
    autoJump: true,           // 取关后自动跳到下一个未回关             // 关注后等几天，对方还没回关才算「未回关」
    reachedEnd: null,         // 当前帖子页已经下滑到底
    minReplies: 150,          // 互关帖至少要有多少条回复
    remindOn: true,           // 定时提醒去找互关帖
    remindMin: 15,
    seen: new Map(),          // 当前页面扫描到的账号
    fadeOthers: false,
    autoScroll: false,
    busy: false,
    armed: null,
    lastUrl: "",
    mode: null,
    profileState: null,       // 个人主页上一次看到的关注状态
    pool: {},                 // 待关注池：账号 -> { handle, name, fo, fr, from, at }，只收「容易回关」的
    unpool: {},               // 取关池：账号 -> { handle, name, at }，「正在关注」页上扫到的、可以取关的人
    noteKind: null,
  };
  const unfollowedToday = () => S.log[today()] || 0;
  const followedToday = () => S.followLog[today()] || 0;

  const saveLater = (() => {
    const timers = {};
    return (key, ms = 800) => {
      clearTimeout(timers[key]);
      timers[key] = setTimeout(() => store.set({ [key]: S[key] }), ms);
    };
  })();

  /* ---------- 页面判断 ---------- */
  function myHandle() {
    const a = document.querySelector(SEL.profileLink);
    const m = a && a.getAttribute("href") && a.getAttribute("href").match(HANDLE_RE);
    return m ? keyOf(m[1]) : null;
  }
  function pageInfo() {
    const path = location.pathname;
    let m = path.match(/^\/([A-Za-z0-9_]{1,15})\/following\/?$/);
    if (m) {
      const me = myHandle();
      return { mode: "following", own: me ? keyOf(m[1]) === me : null };
    }
    if (/^\/[A-Za-z0-9_]{1,15}\/status\/\d+/.test(path)) return { mode: "status" };
    if (/^\/(home|search|hashtag\/[^/]+)\/?$/.test(path)) return { mode: "feed" };
    m = path.match(/^\/([A-Za-z0-9_]{1,15})\/?$/);
    if (m && !RESERVED.has(keyOf(m[1]))) {
      const key = keyOf(m[1]);
      return { mode: "profile", key, handle: m[1], own: key === myHandle() };
    }
    return { mode: null };
  }

  /* ---------- 读取页面元素 ---------- */
  function readCell(cell) {
    let handle = null;
    for (const a of cell.querySelectorAll("a[href]")) {
      const m = a.getAttribute("href").match(HANDLE_RE);
      if (m) { handle = m[1]; break; }
    }
    if (!handle) return null;
    const nameEl = cell.querySelector('a[href="/' + handle + '"] span');
    return {
      handle,
      key: keyOf(handle),
      name: nameEl ? nameEl.textContent.trim() : "",
      followsYou: !!cell.querySelector(SEL.followsYou),
      unfollowBtn: cell.querySelector(SEL.unfollowBtn),
      followBtn: cell.querySelector(SEL.followBtn),
    };
  }

  /* "9"、"1,234"、"1.2K"、"1.2万" → 数字 */
  function parseCount(str) {
    const m = String(str || "").replace(/[,，\s]/g, "").match(/(\d+(?:\.\d+)?)(万|[KkMm])?/);
    if (!m) return 0;
    let n = parseFloat(m[1]);
    if (m[2] === "万") n *= 1e4;
    else if (m[2] && /k/i.test(m[2])) n *= 1e3;
    else if (m[2] && /m/i.test(m[2])) n *= 1e6;
    return Math.round(n);
  }
  /* 读帖子的热度：回复、转发、点赞、书签、浏览量、发布多久了。读不到的项是 null */
  function readMetrics(art) {
    const m = { replies: null, reposts: null, likes: null, bookmarks: null, views: null, ageH: null };
    const labels = [];
    const grp = art.querySelector('[role="group"][aria-label]');
    if (grp) labels.push(...grp.getAttribute("aria-label").split(/[,，、;；]\s*/));
    for (const el of art.querySelectorAll('[data-testid="reply"],[data-testid="retweet"],[data-testid="unretweet"],[data-testid="like"],[data-testid="unlike"],[data-testid="bookmark"],[data-testid="removeBookmark"],a[href$="/analytics"]')) {
      const l = el.getAttribute("aria-label");
      if (l) labels.push(...l.split(/[,，、;；。]\s*/));
    }
    const put = (k, txt) => { if (m[k] === null) m[k] = parseCount(txt); };
    for (const l of labels) {
      if (/回复|repl/i.test(l) && /\d/.test(l)) put("replies", l);
      else if (/转帖|转发|转推|repost|retweet/i.test(l) && /\d/.test(l)) put("reposts", l);
      else if (/喜欢|点赞|like/i.test(l) && /\d/.test(l)) put("likes", l);
      else if (/书签|收藏|bookmark/i.test(l) && /\d/.test(l)) put("bookmarks", l);
      else if (/查看|浏览|view/i.test(l) && /\d/.test(l)) put("views", l);
    }
    if (m.replies === null) m.replies = replyCount(art);
    const tm = art.querySelector("a[href*='/status/'] time[datetime]");
    if (tm) {
      const t = Date.parse(tm.getAttribute("datetime"));
      if (t) m.ageH = Math.max(0, (Date.now() - t) / 36e5);
    }
    return m;
  }
  const fmtN = (n) => (n >= 1e4 ? (n / 1e4).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "万" : String(n));
  function fmtAge(h) {
    if (h === null) return "";
    if (h < 1) return Math.max(1, Math.round(h * 60)) + " 分钟前";
    if (h < 48) return Math.round(h) + " 小时前";
    return Math.round(h / 24) + " 天前";
  }
  /* 热度 = 每小时浏览量（浏览总数 ÷ 发了多久，不足 15 分钟按 15 分钟算）。分 4 档，用颜色区分 */
  const HEAT_CUTS = [500, 2000, 10000];
  function heatInfo(m) {
    if (!m || m.ageH === null || !m.views) return null;
    const per = m.views / Math.max(m.ageH, 0.25);
    const tier = per >= HEAT_CUTS[2] ? 4 : per >= HEAT_CUTS[1] ? 3 : per >= HEAT_CUTS[0] ? 2 : 1;
    return { text: `${fmtN(Math.round(per))}/时`, tier };
  }
  function setHeat(el, m) {
    const h = heatInfo(m);
    setText(el, h ? h.text : "");
    el.dataset.tier = h ? String(h.tier) : "";
    el.hidden = !h;
    el.title = h ? "每小时浏览量。灰 <500，黄 500–1999，橙 2000–9999，红 ≥1万" : "";
    return !!h;
  }
  function replyCount(art) {
    const b = art.querySelector('[data-testid="reply"]');
    if (!b) return 0;
    return parseCount(b.getAttribute("aria-label")) || parseCount(b.textContent);
  }

  function readTweet(art) {
    const un = art.querySelector(SEL.userName);
    if (!un) return null;
    let handle = null;
    for (const a of un.querySelectorAll("a[href]")) {
      const m = a.getAttribute("href").match(HANDLE_RE);
      if (m) { handle = m[1]; break; }
    }
    if (!handle) return null;
    const nameEl = un.querySelector("a span");
    const timeLink = art.querySelector("a[href*='/status/'] time");
    const metrics = readMetrics(art);
    return {
      handle,
      key: keyOf(handle),
      name: nameEl ? nameEl.textContent.trim() : "",
      verified: !!un.querySelector(SEL.verified),
      text: (art.querySelector(SEL.tweetText) || {}).textContent || "",
      url: timeLink ? timeLink.closest("a").getAttribute("href") : null,
      replies: metrics.replies || 0,
      metrics,
    };
  }

  /* ---------- 面板 ---------- */
  let panel;
  const TITLES = { following: "回关助手 · 清理未回关", status: "回关助手 · 找互关", feed: "回关助手 · 互关帖", profile: "回关助手" };

  function buildPanel() {
    panel = document.createElement("div");
    panel.id = "xfb-panel";
    panel.innerHTML = `
      <div class="xfb-head">
        <b id="xfb-title">回关助手</b>
        <span class="xfb-head-btns">
          <button class="xfb-poolbtn" id="xfb-poolbtn" type="button" aria-pressed="false" title="待关注池：看过的互关帖里「容易回关」的人">池子 <b id="xfb-pool-n">0</b></button>
          <button class="xfb-poolbtn xfb-unpoolbtn" id="xfb-unpoolbtn" type="button" aria-pressed="false" title="取关池：「正在关注」页上扫到的、没回关又过了等待期的人">取关池 <b id="xfb-unpool-n">0</b></button>
          <button class="xfb-min" title="收起" aria-label="收起面板">–</button>
        </span>
      </div>
      <div class="xfb-body">
        <div class="xfb-note" id="xfb-note" hidden></div>

        <section class="xfb-sec" data-sec="pool">
          <div class="xfb-tip">你打开过的互关帖里，标着绿色「容易回关」、你还没关注的人，会自动放进这里。按 <b>F</b> 关注池子里的下一个。打开池子会自动连点（3.5 秒一个，满 15 个停）。</div>
          <div class="xfb-autof" id="xfb-autof" hidden><span id="xfb-autof-txt"></span></div>
          <div class="xfb-row"><button type="button" id="xfb-autof-start">开始连点</button><button type="button" id="xfb-autof-stop">停止连点</button></div>
          <div class="xfb-round xfb-roundbox"><span class="xfb-round-txt"></span><button type="button" class="xfb-round-reset">清零，重新计时</button></div>
          <div class="xfb-list" id="xfb-pool-list"></div>
          <div class="xfb-row">
            <button type="button" id="xfb-pool-back">← 返回</button>
            <button type="button" id="xfb-pool-clear">清空池子</button>
          </div>
          <button id="xfb-autof-toggle" class="xfb-wide" type="button" aria-pressed="true">打开池子自动连点</button>
        </section>

        <section class="xfb-sec" data-sec="unpool">
          <div class="xfb-tip">在你的「正在关注」页上滑过的人里，没回关、也过了等待期、没点「保留」的，会自动放进这里。以后不用再打开那个页面一点点刷，直接在这里取关。按 <b>D</b> 取关池子里的下一个。打开取关池会自动连点（3 秒一个，满 50 个停）。</div>
          <div class="xfb-autof" id="xfb-autod" hidden><span id="xfb-autod-txt"></span></div>
          <div class="xfb-row"><button type="button" id="xfb-autod-start">开始连点</button><button type="button" id="xfb-autod-stop">停止连点</button></div>
          <div class="xfb-tip">取关前插件会先问一下 X：对方这几天回关了你，就不取关、直接移出池子。</div>
          <div class="xfb-today xfb-un-today-line2">今天已取关 <b class="xfb-un-today2">0</b> / ${UNFOLLOW_LIMIT}</div>
          <div class="xfb-list" id="xfb-unpool-list"></div>
          <div class="xfb-row">
            <button type="button" id="xfb-unpool-back">← 返回</button>
            <button type="button" id="xfb-unpool-clear">清空取关池</button>
          </div>
          <button id="xfb-autod-toggle" class="xfb-wide" type="button" aria-pressed="true">打开取关池自动连点</button>
        </section>

        <section class="xfb-sec" data-sec="following">
          <div class="xfb-stats">
            <div><span id="xfb-scanned">0</span><small>已扫描</small></div>
            <div class="nf"><span id="xfb-nf">0</span><small>未回关</small></div>
            <div><span id="xfb-mutual">0</span><small>互关</small></div>
            <div><span id="xfb-kept">0</span><small>保留</small></div>
          </div>
          <div class="xfb-today" id="xfb-un-today-line">今天已取关 <b id="xfb-un-today">0</b> / ${UNFOLLOW_LIMIT}</div>
          <div class="xfb-row">
            <button id="xfb-filter" aria-pressed="false">淡化已互关</button>
            <button class="xfb-scroll" aria-pressed="false">自动下滑加载</button>
          </div>
          <button id="xfb-jump" class="xfb-wide xfb-primary-btn" type="button">下一个未回关 ↓</button>
          <div class="xfb-row">
            <button id="xfb-autojump" type="button" aria-pressed="true">取关后自动跳下一个</button>
          </div>
          <div class="xfb-tip"><b>快捷键 D</b>：跳到下一个红色「未回关」的人。</div>
          <div class="xfb-tip" id="xfb-wait-line"></div>
          <label class="xfb-field">关注后等几天，对方还没回关才算「未回关」
            <input id="xfb-grace" type="number" min="0" max="30" step="1" inputmode="numeric">
          </label>
          <button id="xfb-export" class="xfb-wide">导出未回关名单（CSV）</button>
          <div class="xfb-tip" id="xfb-sync-tip"></div>
        </section>

        <section class="xfb-sec" data-sec="feed">
          <div class="xfb-stats two">
            <div class="nf"><span id="xfb-feed-hit">0</span><small>互关帖</small></div>
            <div><span id="xfb-feed-all">0</span><small>已扫描帖子</small></div>
          </div>
          <div class="xfb-round xfb-roundbox"><span class="xfb-round-txt"></span><button type="button" class="xfb-round-reset">清零，重新计时</button></div>
          <div class="xfb-today xfb-reply-line">今天已回复 <b class="xfb-r-today">0</b> / <span class="xfb-r-goal">500</span></div>
          <a class="xfb-btn xfb-wide" id="xfb-search" target="_self">搜索最新的互关帖</a>
          <button id="xfb-next-feed" class="xfb-wide" type="button">打开下一条没处理的互关帖 →</button>
          <div class="xfb-row">
            <button id="xfb-open-inbox" type="button">待回复评论 <b id="xfb-inbox-n"></b> ↗</button>
            <button id="xfb-open-sched" type="button">发帖排期 ↗</button>
          </div>
          <button id="xfb-open-workflow" class="xfb-wide" type="button">时间工作流 ↗</button><!-- 【时间工作流】 -->
          <button id="xfb-open-hot" class="xfb-wide" type="button">热点雷达 <b id="xfb-hot-n"></b> ↗</button>

          <details class="xfb-fold" data-fold="feed-set">
            <summary>设置</summary>
            <div class="xfb-fold-body">
              <label class="xfb-field">关键词（多个用逗号隔开）
                <input id="xfb-kw" type="text" autocomplete="off">
              </label>
              <label class="xfb-field">屏蔽词（多个用逗号隔开，带这些词的帖子折叠起来）
                <input id="xfb-block" type="text" autocomplete="off" placeholder="比如：约炮,裸聊,代开发票">
              </label>
              <label class="xfb-field">回复数至少（只看回复多的帖子）
                <input id="xfb-minrep" type="number" min="0" step="10" inputmode="numeric">
              </label>
              <label class="xfb-field">每轮关注几个人（关满才开始下一轮倒计时）
                <input id="xfb-round-target" type="number" min="1" max="100" step="1" inputmode="numeric">
              </label>
              <label class="xfb-field">定时提醒我去找互关帖
                <select id="xfb-remind">
                  <option value="0">不提醒</option>
                  <option value="10">每 10 分钟</option>
                  <option value="15">每 15 分钟</option>
                  <option value="20">每 20 分钟</option>
                  <option value="30">每 30 分钟</option>
                  <option value="60">每 1 小时</option>
                </select>
              </label>
              <label class="xfb-field">每天回复目标
                <input id="xfb-rgoal" type="number" min="1" step="10" inputmode="numeric">
              </label>
            </div>
          </details>

          <details class="xfb-fold" data-fold="feed-view">
            <summary>显示与提醒</summary>
            <div class="xfb-fold-body">
              <button id="xfb-heat" class="xfb-wide" type="button" aria-pressed="true">所有帖子都显示热度</button>
              <div class="xfb-tip">热度 = 每小时浏览量：灰 &lt;500，黄 500–1999，橙 2000–9999，红 ≥1万</div>
              <button id="xfb-fade" class="xfb-wide" aria-pressed="false">淡化其他帖子</button>
              <button id="xfb-test-notify" class="xfb-wide" type="button">马上提醒一次（试试看）</button>
            </div>
          </details>
        </section>

        <section class="xfb-sec" data-sec="status">
          <div class="xfb-stats">
            <div><span id="xfb-rep">0</span><small>回复者</small></div>
            <div class="nf"><span id="xfb-todo">0</span><small>未关注</small></div>
            <div><span id="xfb-have">0</span><small>已关注</small></div>
            <div><span id="xfb-skip">0</span><small>非蓝V</small></div>
          </div>
          <div class="xfb-today" id="xfb-f-today-line">今天已关注 <b id="xfb-f-today">0</b></div>
          <div class="xfb-round xfb-roundbox"><span class="xfb-round-txt"></span><button type="button" class="xfb-round-reset">清零，重新计时</button></div>
          <div class="xfb-today xfb-reply-line">今天已回复 <b class="xfb-r-today">0</b> / <span class="xfb-r-goal">500</span></div>
          <div class="xfb-row">
            <button class="xfb-scroll" aria-pressed="false">下滑到最后一条</button>
            <button id="xfb-verified" aria-pressed="true">只看蓝V</button>
          </div>
          <div class="xfb-list" id="xfb-list"></div>
          <div class="xfb-row">
            <button id="xfb-handled" type="button" aria-pressed="false">这条处理完了</button>
            <button id="xfb-next-status" type="button">下一条互关帖 →</button>
          </div>
          <details class="xfb-fold" data-fold="status-help">
            <summary>怎么用</summary>
            <div class="xfb-fold-body">
              <div class="xfb-tip"><b>快捷键 F</b>：关注名单里下一个绿色「容易回关」的人。按一下关注一个，两次之间至少隔 3 秒。</div>
              <div class="xfb-tip">点「关注」直接关注，不用打开主页，每次只关注一个人。想先看看这个人，点「主页」。</div>
            </div>
          </details>
        </section>

        <section class="xfb-sec" data-sec="profile">
          <div class="xfb-today" id="xfb-p-today-line">今天已关注 <b id="xfb-p-today">0</b></div>
          <div class="xfb-tip">这是从互关帖名单打开的账号。关注后关掉这个标签页，回到帖子继续。</div>
        </section>
      </div>`;
    document.body.appendChild(panel);

    panel.querySelector(".xfb-min").addEventListener("click", () => panel.classList.toggle("xfb-collapsed"));
    /* 折叠块：记住你上次是展开还是收起 */
    panel.querySelectorAll("details.xfb-fold").forEach((d) => {
      const k = "xfb-fold-" + d.dataset.fold;
      try { d.open = localStorage.getItem(k) === "1"; } catch (e) { /* 忽略 */ }
      d.addEventListener("toggle", () => { try { localStorage.setItem(k, d.open ? "1" : "0"); } catch (e) { /* 忽略 */ } });
    });
    panel.querySelector("#xfb-poolbtn").addEventListener("click", () => togglePool());
    setTimeout(updatePoolCount, 0);
    setTimeout(updateInboxCount, 0);
    panel.querySelector("#xfb-pool-back").addEventListener("click", () => togglePool(false));
    panel.querySelector("#xfb-unpoolbtn").addEventListener("click", () => toggleUnpool());
    panel.querySelector("#xfb-unpool-back").addEventListener("click", () => toggleUnpool(false));
    setTimeout(updateUnpoolCount, 0);
    panel.querySelector("#xfb-unpool-clear").addEventListener("click", () => {
      if (!Object.keys(S.unpool).length) return;
      if (S.armed !== "unpool-clear") { S.armed = "unpool-clear"; setNote("确定清空取关池？再点一次「清空取关池」。（不会取关任何人，下次滑「正在关注」页还会重新收进来）", true); return; }
      S.armed = null;
      S.unpool = {};
      store.set({ unpool: S.unpool });
      renderUnpool();
      setNote("取关池已清空。");
    });
    panel.querySelector("#xfb-autod-stop").addEventListener("click", () => stopAutoD());
    panel.querySelector("#xfb-autod-start").addEventListener("click", () => startAutoD(true));
    const autodTgl = panel.querySelector("#xfb-autod-toggle");
    autodTgl.addEventListener("click", () => {
      const on = autodTgl.getAttribute("aria-pressed") !== "true";
      autodTgl.setAttribute("aria-pressed", String(on));
      autoDEnabled = on;
      store.set({ autoDOn: on });
      if (!on) stopAutoD();
    });
    try {
      chrome.storage.local.get(["autoDOn"]).then((v) => {
        if (v.autoDOn === false) { autoDEnabled = false; autodTgl.setAttribute("aria-pressed", "false"); }
      });
    } catch (e) { /* 读不到就默认开 */ }
    panel.querySelector("#xfb-pool-clear").addEventListener("click", () => {
      if (!Object.keys(S.pool).length) return;
      if (S.armed !== "pool-clear") { S.armed = "pool-clear"; setNote("确定清空池子？再点一次「清空池子」。", true); return; }
      S.armed = null;
      S.pool = {};
      store.set({ pool: S.pool });
      renderPool();
      setNote("池子已清空。");
    });
    panel.querySelector("#xfb-autof-stop").addEventListener("click", () => stopAutoF());
    panel.querySelector("#xfb-autof-start").addEventListener("click", () => startAutoF(true));
    const autofTgl = panel.querySelector("#xfb-autof-toggle");
    autofTgl.addEventListener("click", () => {
      const on = autofTgl.getAttribute("aria-pressed") !== "true";
      autofTgl.setAttribute("aria-pressed", String(on));
      autoFEnabled = on;
      store.set({ autoFOn: on });
      if (!on) stopAutoF();
    });
    try {
      chrome.storage.local.get(["autoFOn"]).then((v) => {
        if (v.autoFOn === false) { autoFEnabled = false; autofTgl.setAttribute("aria-pressed", "false"); }
      });
    } catch (e) { /* 读不到就默认开 */ }

    panel.querySelector("#xfb-filter").addEventListener("click", (e) => {
      const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
      e.currentTarget.setAttribute("aria-pressed", String(on));
      document.documentElement.classList.toggle("xfb-only-nf", on);
    });
    panel.querySelectorAll(".xfb-scroll").forEach((b) => b.addEventListener("click", () => {
      S.autoScroll = !S.autoScroll;
      syncScrollButtons();
      if (S.autoScroll) runAutoScroll();
    }));
    panel.querySelector("#xfb-export").addEventListener("click", exportCsv);

    const kw = panel.querySelector("#xfb-kw");
    kw.value = S.keywords;
    kw.addEventListener("change", () => {
      S.keywords = kw.value.trim() || DEFAULT_KEYWORDS;
      kw.value = S.keywords;
      store.set({ keywords: S.keywords });
      updateSearchLink();
      scan();
    });
    const bw = panel.querySelector("#xfb-block");
    bw.value = S.blockWords;
    bw.addEventListener("change", () => {
      S.blockWords = bw.value.split(/[,，、\n]/).map((x) => x.trim()).filter(Boolean).join(",");
      bw.value = S.blockWords;
      store.set({ blockWords: S.blockWords });
      blockArticles(true);
    });
    const mr = panel.querySelector("#xfb-minrep");
    mr.value = String(S.minReplies);
    mr.addEventListener("change", () => {
      const n = Math.max(0, Math.round(Number(mr.value) || 0));
      S.minReplies = n;
      mr.value = String(n);
      store.set({ minReplies: n });
      updateSearchLink();
      scan();
    });
    panel.querySelector("#xfb-handled").addEventListener("click", () => {
      const id = statusIdOf(location.pathname);
      if (!id) return;
      markHandled(id, !S.handled[id]);
      setNote(S.handled[id] ? "已标记为处理完。回到首页或搜索页，这条帖子会变灰，标着「已处理」。" : "已取消「处理完」标记。");
    });
    panel.querySelector("#xfb-open-sched").addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-open-schedule" }); } catch (e) { setNote("插件刚更新过，刷新一下这个页面再试。", true); }
    });
    /* 【时间工作流】打开另一个插件「X 时间工作流」 */
    panel.querySelector("#xfb-open-workflow").addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-open-workflow" }, (r) => { if (r && !r.ok) setNote(r.reason, true); }); }
      catch (e) { setNote("插件刚更新过，刷新一下这个页面再试。", true); }
    });
    panel.querySelector("#xfb-open-hot").addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-open-hot" }); } catch (e) { setNote("插件刚更新过，刷新一下这个页面再试。", true); }
    });
    panel.querySelector("#xfb-open-inbox").addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-open-inbox" }); } catch (e) { setNote("插件刚更新过，刷新一下这个页面再试。", true); }
    });
    panel.querySelector("#xfb-jump").addEventListener("click", () => jumpNextNF());
    const aj = panel.querySelector("#xfb-autojump");
    aj.setAttribute("aria-pressed", String(S.autoJump));
    aj.addEventListener("click", () => {
      S.autoJump = !S.autoJump;
      aj.setAttribute("aria-pressed", String(S.autoJump));
      store.set({ autoJump: S.autoJump });
    });
    const gr = panel.querySelector("#xfb-grace");
    gr.value = String(S.graceDays);
    gr.addEventListener("change", () => {
      const n = Math.min(30, Math.max(0, Math.round(Number(gr.value))));
      S.graceDays = Number.isFinite(n) ? n : 3;
      gr.value = String(S.graceDays);
      store.set({ graceDays: S.graceDays });
      scan();
    });
    panel.querySelector("#xfb-next-status").addEventListener("click", () => goNextPost());
    panel.querySelector("#xfb-next-feed").addEventListener("click", () => {
      if (!openNextHit()) setNote("这一页没有还没处理的互关帖。往下滑加载更多，或者点上面的搜索按钮刷新。", true);
    });
    const rg = panel.querySelector("#xfb-rgoal");
    rg.value = String(S.replyGoal);
    rg.addEventListener("change", () => {
      const n = Math.max(1, Math.round(Number(rg.value) || 500));
      S.replyGoal = n;
      rg.value = String(n);
      store.set({ replyGoal: n });
      updateReplyLines();
    });
    const rt = panel.querySelector("#xfb-round-target");
    chrome.storage.local.get(["roundTarget"]).then((v) => { rt.value = String(v.roundTarget || 15); });
    rt.addEventListener("change", () => {
      const n = Math.min(100, Math.max(1, Math.round(Number(rt.value) || 15)));
      rt.value = String(n);
      chrome.storage.local.set({ roundTarget: n });
    });
    /* 计时状态 + 「清零，重新计时」：任何时候都能手动清掉这一轮的计数，从现在开始倒数 */
    panel.querySelectorAll(".xfb-round-reset").forEach((b) => b.addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-round-done" }); } catch (e) { /* 忽略 */ }
      setNote(`已清零，从现在开始倒数 ${S.remindMin} 分钟。`);
    }));
    const syncRound = async () => {
      let v;
      try { v = await chrome.storage.local.get(["remindWorking", "roundCount", "roundTarget", "remindDue", "remindNextAt", "remindOn"]); } catch (e) { return; }
      const target = Number(v.roundTarget) || 15;
      let txt;
      if (v.remindOn === false) txt = "定时提醒已关闭";
      else if (v.remindWorking) txt = `这一轮已关注 ${Number(v.roundCount) || 0} / ${target}`;
      else if (v.remindDue) txt = "到点了，该找互关帖了";
      else if (v.remindNextAt) txt = `下次提醒还有 ${Math.max(1, Math.round((v.remindNextAt - Date.now()) / 60000))} 分钟`;
      else txt = "倒计时中";
      panel.querySelectorAll(".xfb-roundbox").forEach((box) => {
        box.classList.toggle("working", !!v.remindWorking);
        setText(box.querySelector(".xfb-round-txt"), txt);
        box.querySelector(".xfb-round-reset").hidden = v.remindOn === false;
      });
    };
    syncRound();
    setInterval(() => { if (alive()) syncRound(); }, 30000);
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && (ch.remindWorking || ch.roundCount || ch.roundTarget || ch.remindDue || ch.remindNextAt || ch.remindOn)) syncRound();
    });
    const rm = panel.querySelector("#xfb-remind");
    const remindVal = () => (S.remindOn ? String(S.remindMin) : "0");
    rm.value = remindVal();
    if (rm.value !== remindVal()) { const o = document.createElement("option"); o.value = remindVal(); o.textContent = `每 ${S.remindMin} 分钟`; rm.appendChild(o); rm.value = remindVal(); }
    rm.addEventListener("change", () => {
      const n = Number(rm.value);
      S.remindOn = n > 0;
      if (n > 0) S.remindMin = n;
      store.set({ remindOn: S.remindOn, remindMin: S.remindMin });
      setNote(n > 0 ? `好的，每 ${n} 分钟提醒你一次。点提醒就会打开最新的互关帖。` : "已关闭定时提醒。");
    });
    const hb = panel.querySelector("#xfb-heat");
    hb.setAttribute("aria-pressed", String(S.heatAll));
    hb.addEventListener("click", () => {
      S.heatAll = !S.heatAll;
      hb.setAttribute("aria-pressed", String(S.heatAll));
      store.set({ heatAll: S.heatAll });
      scan();
    });
    panel.querySelector("#xfb-test-notify").addEventListener("click", () => {
      try {
        chrome.runtime.sendMessage({ type: "xfb-test-notify" }, (r) => {
          if (chrome.runtime.lastError || !r || !r.ok) {
            setNote("提醒没发出去：" + ((r && r.error) || (chrome.runtime.lastError && chrome.runtime.lastError.message) || "插件后台没响应，去 chrome://extensions 点一下刷新。"), true);
          } else {
            setNote("页面顶上应该出现了蓝色提示条，浏览器右上角回关助手图标也变成了红色「!」。", false);
          }
        });
      } catch (e) { setNote("插件刚更新过，刷新一下这个 X 页面再试。", true); }
    });
    updateSearchLink();
    panel.querySelector("#xfb-fade").addEventListener("click", (e) => {
      S.fadeOthers = !S.fadeOthers;
      e.currentTarget.setAttribute("aria-pressed", String(S.fadeOthers));
      document.documentElement.classList.toggle("xfb-fade-others", S.fadeOthers);
    });

    const vb = panel.querySelector("#xfb-verified");
    vb.setAttribute("aria-pressed", String(S.onlyVerified));
    vb.addEventListener("click", () => {
      S.onlyVerified = !S.onlyVerified;
      vb.setAttribute("aria-pressed", String(S.onlyVerified));
      store.set({ onlyVerified: S.onlyVerified });
      scan();
    });
  }

  function syncScrollButtons() {
    if (!panel) return;
    panel.querySelectorAll(".xfb-scroll").forEach((b) => {
      b.setAttribute("aria-pressed", String(S.autoScroll));
      const idle = b.closest('[data-sec="status"]') ? "下滑到最后一条" : "自动下滑加载";
      setText(b, S.autoScroll ? "停止下滑" : idle);
    });
  }

  function updateSearchLink() {
    if (!panel) return;
    const first = S.keywords.split(/[,，]/).map((s) => s.trim()).filter(Boolean)[0] || DEFAULT_KEYWORDS;
    const q = first.startsWith("#") || first.startsWith("＃") ? first : "#" + first;
    const full = S.minReplies > 0 ? `${q} min_replies:${S.minReplies}` : q;
    panel.querySelector("#xfb-search").href = "/search?q=" + encodeURIComponent(full) + "&src=typed_query&f=live";
    setText(panel.querySelector("#xfb-search"), S.minReplies > 0 ? `搜索最新的 ${q}（回复 ≥ ${S.minReplies}）` : `搜索最新的 ${q} 帖子`);
  }

  function setNote(text, warn, kind = "msg") {
    if (!panel) return;
    S.noteKind = text ? kind : null;
    const n = panel.querySelector("#xfb-note");
    setText(n, text || "");
    n.classList.toggle("warn", !!warn);
    n.hidden = !text;
  }

  function showMode(mode) {
    if (!mode) { if (panel) panel.hidden = true; return false; }
    if (!panel) buildPanel();
    panel.hidden = false;
    panel.dataset.mode = mode;
    setText(panel.querySelector("#xfb-title"), panel.dataset.pool === "1" ? "回关助手 · 待关注池" : panel.dataset.pool === "un" ? "回关助手 · 取关池" : TITLES[mode]);
    return true;
  }

  /* ========== 模式一：自己的「正在关注」页 ========== */
  function decorateFollowingCell(cell, info) {
    cell.classList.add("xfb-cell");
    cell.classList.toggle("xfb-mutual", info.followsYou);
    const isKept = !!S.kept[info.key];
    const left = !info.followsYou && !isKept ? waitLeftH(info.key) : 0;
    cell.classList.toggle("xfb-nf", !info.followsYou && !isKept && !left);
    cell.classList.toggle("xfb-wait", !!left);
    cell.classList.toggle("xfb-kept", !info.followsYou && isKept);

    let bar = cell.querySelector(":scope > .xfb-bar");
    if (info.followsYou) { if (bar) bar.remove(); return; }
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "xfb-bar";
      bar.innerHTML = `<span class="xfb-tag"></span>
        <button class="xfb-keep" type="button"></button>
        <button class="xfb-un" type="button"></button>`;
      cell.appendChild(bar);
      /* 点击时再读格子里是谁：X 会把格子复用给别的账号 */
      bar.querySelector(".xfb-keep").addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        const now = readCell(cell);
        if (now) toggleKeep(now.key);
      });
      bar.querySelector(".xfb-un").addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        const now = readCell(cell);
        if (now) unfollow(cell, now.key);
      });
    }
    const done = !!S.done[info.key] && !info.unfollowBtn;
    cell.classList.toggle("xfb-done", done);
    if (done) cell.classList.remove("xfb-nf");
    setText(bar.querySelector(".xfb-tag"), done ? "已取关" : isKept ? "保留" : left ? `刚关注 · ${fmtLeft(left)}` : "未回关");
    bar.querySelector(".xfb-tag").title = left ? `你关注他还不到 ${S.graceDays} 天，先等等他回关。到期还没回关，这里会变成红色「未回关」。` : "";
    setText(bar.querySelector(".xfb-keep"), isKept ? "取消保留" : "保留");
    bar.querySelector(".xfb-keep").hidden = done;
    const un = bar.querySelector(".xfb-un");
    un.hidden = isKept || done || !info.unfollowBtn || !!left;
    un.disabled = S.busy;
    setText(un, S.armed === info.key ? "已超上限，再点一次确认" : "取关");
    un.classList.toggle("armed", S.armed === info.key);
  }

  function scanFollowing(info) {
    if (info.own === false) {
      setNote("这是别人的关注列表。请打开你自己主页的「正在关注」页面。", true, "page");
      document.querySelectorAll(".xfb-bar").forEach((b) => b.remove());
      renderFollowingStats();
      return;
    }
    if (info.own === null) setNote("没认出你的账号，确认这是你自己的「正在关注」页面再操作。", true, "page");
    else if (S.noteKind === "page") setNote("");

    let changed = false;
    const col = document.querySelector(SEL.column) || document;
    for (const cell of col.querySelectorAll(SEL.cell)) {
      const c = readCell(cell);
      if (!c) continue;
      S.seen.set(c.key, { name: c.name, handle: c.handle, followsYou: c.followsYou });
      if (info.own && c.unfollowBtn && !S.following[c.key]) { S.following[c.key] = c.handle; changed = true; }
      if (info.own) unpoolConsider(c);
      decorateFollowingCell(cell, c);
    }
    if (changed) saveLater("following");
    renderFollowingStats();
  }

  function renderFollowingStats() {
    let nf = 0, mutual = 0, wait = 0;
    for (const [key, v] of S.seen) {
      if (S.done[key]) continue;
      if (v.followsYou) mutual++;
      else if (S.kept[key]) continue;
      else if (waitLeftH(key)) wait++;
      else nf++;
    }
    setText(panel.querySelector("#xfb-wait-line"), wait
      ? `另有 ${wait} 个是 ${S.graceDays} 天内刚关注的，先等他们回关，不算进「未回关」。`
      : S.graceDays ? `刚关注不到 ${S.graceDays} 天的人会标黄色「刚关注」，不会让你取关。` : "等待期是 0 天：关注后没回关的人马上算「未回关」。");
    setText(panel.querySelector("#xfb-scanned"), S.seen.size);
    setText(panel.querySelector("#xfb-nf"), nf);
    setText(panel.querySelector("#xfb-mutual"), mutual);
    setText(panel.querySelector("#xfb-kept"), Object.keys(S.kept).length);
    setText(panel.querySelector("#xfb-un-today"), unfollowedToday());
    panel.querySelector("#xfb-un-today-line").classList.toggle("over", unfollowedToday() >= UNFOLLOW_LIMIT);
    const n = Object.keys(S.following).length;
    setText(panel.querySelector("#xfb-sync-tip"), S.followingSyncedAt
      ? `已记住你关注的 ${n} 个人（${new Date(S.followingSyncedAt).toLocaleDateString()} 完整读取过一次）。`
      : `已记住你关注的 ${n} 个人。找互关主要靠读帖子里的关注状态，这里不用特意滑到底。`);
  }

  async function toggleKeep(key) {
    if (S.kept[key]) delete S.kept[key]; else S.kept[key] = today();
    await store.set({ kept: S.kept });
    scan();
  }

  async function unfollow(cell, key) {
    if (S.busy) return;
    if (unfollowedToday() >= UNFOLLOW_LIMIT && S.armed !== key) {
      S.armed = key;
      setNote(`今天已经取关 ${unfollowedToday()} 个，超过 ${UNFOLLOW_LIMIT} 的建议上限，继续容易被 X 限流。确定要继续就再点一次。`, true);
      scan();
      return;
    }
    S.armed = null;
    const current = readCell(cell);
    if (!current || current.key !== key) { scan(); return; }
    const btn = current.unfollowBtn;
    if (!btn) { setNote("没找到这个账号的「正在关注」按钮，滚动一下页面再试。", true); return; }

    S.busy = true; scan();
    try {
      btn.click();
      const ok = await waitFor(() => document.querySelector(SEL.confirm), 3000);
      if (!ok) throw new Error("没弹出取关确认框");
      ok.click();
      const changed = await waitFor(() => !cell.querySelector(SEL.unfollowBtn) && cell.querySelector(SEL.followBtn), 4000);
      if (!changed) throw new Error("取关后按钮没有变化");
      S.done[key] = today();
      S.log[today()] = unfollowedToday() + 1;
      delete S.following[key];
      delete S.followedAt[key];
      await store.set({ done: S.done, log: S.log, following: S.following, followedAt: S.followedAt });
      setNote(`已取关 @${S.seen.get(key)?.handle || key}`);
      S.jumpAfter = true;
    } catch (err) {
      setNote(`这次没取关成功：${err.message}。可能是 X 改了页面，或者操作太快被限制，等一会再试。`, true);
    } finally {
      await sleep(UNFOLLOW_COOLDOWN_MS);
      S.busy = false;
      scan();
      if (S.jumpAfter && S.autoJump) { S.jumpAfter = false; await sleep(150); jumpNextNF(); }
      S.jumpAfter = false;
    }
  }

  function exportCsv() {
    const rows = [["用户名", "昵称", "主页链接"]];
    for (const [key, v] of S.seen) {
      if (v.followsYou || S.kept[key] || S.done[key] || waitLeftH(key)) continue;
      rows.push(["@" + v.handle, v.name, "https://x.com/" + v.handle]);
    }
    if (rows.length === 1) { setNote("还没有扫描到未回关的人，先往下滑加载列表。", true); return; }
    const csv = "﻿" + rows.map((r) => r.map((c) => {
      const s = String(c);
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `未回关名单-${today()}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    setNote(`已导出 ${rows.length - 1} 个账号。`);
  }

  /* ========== 模式二：首页 / 搜索，找互关帖 ========== */
  function keywordList() {
    return S.keywords.split(/[,，]/).map(norm).filter(Boolean);
  }

  function updateReplyLines() {
    if (!panel) return;
    const n = repliedToday();
    panel.querySelectorAll(".xfb-r-today").forEach((el) => setText(el, n));
    panel.querySelectorAll(".xfb-r-goal").forEach((el) => setText(el, S.replyGoal));
    panel.querySelectorAll(".xfb-reply-line").forEach((el) => el.classList.toggle("done", n >= S.replyGoal));
  }

  /* ================= 交给 X 定时：帮你在 X 自己的「定时」窗口里选好日期和时间 ================= */
  let xJob = null, xJobBusy = false, xJobReady = false;
  const setSelect = (sel, opt) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
    try { sel.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
    setter.call(sel, opt.value);
    sel.selectedIndex = opt.index;
    sel.dispatchEvent(new Event("input", { bubbles: true }));
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  };
  /* 备用办法：让页面里的 main.js 直接调用 X 自己的「选择变化」处理函数 */
  let mainSeq = 0;
  function setSelectMain(sel, opt) {
    return new Promise((resolve) => {
      const id = "s" + (++mainSeq);
      sel.setAttribute("data-xfb-sel", id);
      const done = (e) => {
        if (e.source !== window || !e.data || e.data.__xfb !== "select-set-res" || e.data.id !== id) return;
        window.removeEventListener("message", done);
        resolve(!!e.data.ok);
      };
      window.addEventListener("message", done);
      window.postMessage({ __xfb: "select-set", id, value: opt.value, index: opt.index }, location.origin);
      setTimeout(() => { window.removeEventListener("message", done); resolve(false); }, 1500);
    });
  }
  /* X 定时窗口顶上「将于 2026年11月7日周六 下午5:24 发布」：读出它现在显示的时间 */
  function headerTime(root) {
    const t = (root.textContent || "").replace(/\s+/g, " ");
    const m = t.match(/(?:将于|將於|Will send on|Will send)([^。]{0,60}?)(?:发布|發佈|发送|$)/i);
    if (!m) return null;
    const seg = m[1];
    const tm = seg.match(/(上午|下午|凌晨|中午|晚上|AM|PM)?\s*(\d{1,2}):(\d{2})\s*(AM|PM)?/i);
    if (!tm) return null;
    let h = Number(tm[2]);
    const ap = (tm[1] || tm[4] || "").toUpperCase();
    if (/下午|晚上|PM/.test(ap) && h < 12) h += 12;
    if (/上午|凌晨|AM/.test(ap) && h === 12) h = 0;
    const dm = seg.match(/(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日/);
    return { h, mi: Number(tm[3]), date: dm ? `${dm[1]}-${String(dm[2]).padStart(2, "0")}-${String(dm[3]).padStart(2, "0")}` : null };
  }
  /* 失败时收集定时窗口的结构，方便发给开发的人看 */
  function scheduleDiag(root, job, note) {
    const sels = [...(root || document).querySelectorAll("select")];
    const lines = [`回关助手诊断 v${chrome.runtime.getManifest().version}`, `目标 ${job.date} ${job.time}`, `说明 ${note}`, `网址 ${location.pathname}`,
      `顶部文字 ${((root || document).textContent || "").replace(/\s+/g, " ").slice(0, 160)}`, `下拉框 ${sels.length} 个`];
    sels.forEach((sel, i) => {
      const r = sel.getBoundingClientRect();
      const op = [...sel.options];
      lines.push(`#${i} 标签「${labelOf(sel).slice(0, 30)}」 选项${op.length}个 [${op.slice(0, 3).map((o) => o.value + "/" + o.textContent.trim()).join(", ")} … ${op.slice(-2).map((o) => o.value + "/" + o.textContent.trim()).join(", ")}] 当前=${sel.value}/${(op[sel.selectedIndex] || {}).textContent || ""} 尺寸${Math.round(r.width)}x${Math.round(r.height)} 透明度${getComputedStyle(sel).opacity} id=${sel.id} labelledby=${sel.getAttribute("aria-labelledby") || ""}`);
    });
    const c = document.querySelector('[data-testid="scheduledConfirmationPrimaryAction"]');
    lines.push(`确认按钮 ${c ? (c.getAttribute("aria-disabled") || c.disabled ? "灰的" : "可点") : "没找到"}`);
    return lines.join("\n");
  }
  const optNum = (o) => { const m = String(o.textContent || o.value).match(/\d+/); return m ? Number(m[0]) : NaN; };
  /* 下拉框旁边写的字（月/日/年/时/分）。X 只列出还没过去的时间，所以不能靠选项个数判断 */
  function labelOf(sel) {
    const parts = [sel.getAttribute("aria-label") || ""];
    const lb = sel.getAttribute("aria-labelledby");
    if (lb) for (const id of lb.split(/\s+/)) { const el = document.getElementById(id); if (el) parts.push(el.textContent); }
    if (sel.id) { const l = document.querySelector(`label[for="${CSS.escape(sel.id)}"]`); if (l) parts.push(l.textContent); }
    let box = sel.parentElement;
    for (let i = 0; box && i < 3; i++, box = box.parentElement) {
      if (box.querySelectorAll("select").length > 1) break;
      const c = box.cloneNode(true);
      c.querySelectorAll("select").forEach((x) => x.remove());
      parts.push(c.textContent);
    }
    return parts.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }
  function classifySelects(sels) {
    const r = {};
    const tests = [
      ["ampm", /\b(am|pm)\b|上午|下午|am\/pm/],
      ["minute", /minute|分/],
      ["hour", /hour|小时|时/],
      ["year", /year|年/],
      ["day", /\bday\b|日/],
      ["month", /month|月/],
    ];
    for (const sel of sels) {
      const lb = labelOf(sel);
      const hit = tests.find(([k, re]) => !r[k] && re.test(lb));
      if (hit) r[hit[0]] = sel;
    }
    if (!(r.month && r.day && r.year && r.hour && r.minute)) {
      /* 认不出来就按 X 的顺序：月、日、年、时、分（12 小时制时最后还有上午/下午） */
      const order = ["month", "day", "year", "hour", "minute", "ampm"];
      sels.slice(0, 6).forEach((sel, i) => { if (!Object.values(r).includes(sel) && !r[order[i]]) r[order[i]] = sel; });
    }
    return r;
  }

  /* 插件升级后，旧页面里的这段程序就失效了；失效后不再做任何事，免得在「错误」里刷屏 */
  const alive = () => { try { return !!(chrome.runtime && chrome.runtime.id); } catch (e) { return false; } };

  /* ---------- 找到「内容和这条帖子一致」的那个发帖框 ----------
   * 首页顶上「有什么新鲜事」也是一个发帖框，弹出的发帖框打开时它还在背景里，
   * 所有操作都要限定在内容对得上的那个框里，不能在整个页面里随便找 */
  const normText = (x) => String(x || "").replace(/\s+/g, "").slice(0, 30);
  function composerRootOf(ed) {
    let el = ed;
    for (let i = 0; el && i < 25; i++, el = el.parentElement) {
      if (el.querySelector('[data-testid="toolBar"]') && el.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]')) return el;
    }
    return null;
  }
  function findComposer(text) {
    const inModal = (ed) => !!ed.closest('[aria-modal="true"], [role="dialog"]');
    /* 从排期表打开的页面（/intent/、/compose/）只认弹出的发帖框，绝不碰首页顶上那个 */
    const modalOnly = /^\/(intent|compose)\//.test(location.pathname);
    const cands = [...document.querySelectorAll('[data-testid="tweetTextarea_0"]')]
      .filter((ed) => (!modalOnly || inModal(ed)) && normText(ed.innerText) === normText(text))
      .sort((a, b) => inModal(b) - inModal(a));            // 弹出的发帖框优先
    for (const ed of cands) {
      const root = composerRootOf(ed);
      if (root) return { ed, root };
    }
    return null;
  }
  const isScheduled = (root) => {
    const btn = root.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
    return (btn && /安排|schedule/i.test(btn.textContent)) || /将于|will send|scheduled for/i.test(root.textContent);
  };

  /* ---------- 大视频：从插件后台一块一块（每块 8MB）拿过来，拼回文件 ---------- */
  async function videoBlob(im) {
    const parts = [];
    const CH = 8 * 1024 * 1024;
    let off = 0, size = im.size || Infinity, type = im.type;
    while (off < size) {
      const r = await new Promise((res) => {
        try { chrome.runtime.sendMessage({ type: "xfb-media-chunk", id: im.idb, offset: off, len: CH }, (x) => res(chrome.runtime.lastError ? null : x)); }
        catch (e) { res(null); }
      });
      if (!r || !r.ok) return null;
      size = r.size; type = r.type || type;
      const bin = atob(r.data);
      const u8 = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
      parts.push(u8);
      off += CH;
      if (!bin.length) break;
      if (size > CH * 2) xJobBanner(`正在准备视频… ${Math.min(100, Math.round((off / size) * 100))}%`);
    }
    return new Blob(parts, { type });
  }
  /* 视频越大，X 上传越久：按大小多等一会（每 100MB 多等 2 分钟） */
  let mediaBytes = 0;
  const uploadWait = (base) => base + Math.ceil(mediaBytes / 1e8) * 120000;

  /* ---------- 把排期表里这一格的图片放进 X 的发帖框 ---------- */
  async function attachImages(date, time, want, comp) {
    if (!want) return true;
    let v;
    try { v = await chrome.storage.local.get([`img:${date}|${time}`]); } catch (e) { return false; }
    const list = v[`img:${date}|${time}`] || [];
    if (!list.length) return true;
    mediaBytes = list.reduce((n, x) => n + (x.size || 0), 0);
    const files = [];
    for (let i = 0; i < list.length; i++) {
      const blob = list[i].idb ? await videoBlob(list[i]) : await (await fetch(list[i].data)).blob();
      if (!blob) return false;
      const ext = (list[i].type.split("/")[1] || "png").replace("jpeg", "jpg").replace("quicktime", "mov");
      files.push(new File([blob], `${/^video/.test(list[i].type) ? "video" : "image"}${i + 1}.${ext}`, { type: list[i].type }));
    }
    const dt = new DataTransfer();
    files.forEach((f) => dt.items.add(f));
    if (!comp) return false;
    const scope = comp.root;
    const input = scope.querySelector('input[data-testid="fileInput"]');     // 只用这个发帖框自己的上传按钮
    const count = () => scope.querySelectorAll('[data-testid="attachments"] img, [data-testid="attachments"] video').length;
    const before = count();
    if (input) {
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      const ed = comp.ed;
      ed.focus();
      ed.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    }
    /* 等图片/视频出现在发帖框里（上传要一点时间，视频更久） */
    const video = files.some((f) => /^video/.test(f.type));
    return !!(await waitFor(() => count() >= before + files.length, video ? uploadWait(180000) : 20000, 500));
  }

  /* 从排期表点「现在发」/ 点提醒打开的发帖框：内容是网址带过来的，图片在这里补上 */
  let openMediaDone = false;
  async function checkOpenMedia() {
    if (openMediaDone || !/^\/(intent|compose)\//.test(location.pathname)) return;
    let v;
    try { v = await chrome.storage.local.get(["schedOpen"]); } catch (e) { return; }
    const o = v.schedOpen;
    if (!o || !o.imgs || o.mediaDone || Date.now() - o.at > 3 * 60 * 1000) { openMediaDone = !!o; return; }
    /* 这个页面是不是为这一条打开的：网址里带的文字要对得上 */
    if (normText(new URLSearchParams(location.search).get("text") || "") !== normText(o.text)) { openMediaDone = true; return; }
    const comp = findComposer(o.text);
    if (!comp) return;
    openMediaDone = true;
    try { await chrome.storage.local.set({ schedOpen: { ...o, mediaDone: true } }); } catch (e) { /* 忽略 */ }
    xJobBanner(o.vid ? "正在把视频放进发帖框，视频上传要等一会儿…" : "正在把图片放进发帖框…");
    const ok = await attachImages(o.date, o.time, o.imgs, comp);
    xJobBanner(ok ? `内容和${o.vid ? "视频" : "图片"}都放好了。${o.vid ? "等视频上传完，" : ""}看一眼没问题就点「发帖」。`
      : `${o.vid ? "视频" : "图片"}没能自动放进去，请点发帖框下面的图片图标手动添加。`, ok ? "ok" : "warn");
  }
  setInterval(() => { if (alive()) checkOpenMedia(); }, 900);

  /* 记下你的 X 名字和头像，排期表的预览用 */
  async function rememberMe() {
    const sw = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
    if (!sw) return;
    const img = sw.querySelector("img");
    const spans = [...sw.querySelectorAll("span")].map((x) => x.textContent.trim()).filter(Boolean);
    const handleTxt = spans.find((x) => x.startsWith("@"));
    const name = spans.find((x) => !x.startsWith("@"));
    const me = { name: name || "", handle: handleTxt ? handleTxt.slice(1) : (myHandle() || ""), avatar: img ? img.src : "", verified: !!sw.querySelector('[data-testid="icon-verified"]') };
    if (!me.name && !me.handle) return;
    let v;
    try { v = await chrome.storage.local.get(["me"]); } catch (e) { return; }
    if (JSON.stringify(v.me || {}) !== JSON.stringify(me)) chrome.storage.local.set({ me });
  }
  setTimeout(() => { if (alive()) rememberMe(); }, 3000);
  setInterval(() => { if (alive()) rememberMe(); }, 60000);

  /* 在 X 的定时窗口里把下拉框设成 day hm。逐个设，设完再核对一遍 */
  async function fillScheduleSelects(root, day, hm) {
    const [Y, M, D] = day.split("-").map(Number);
    const [h, mi] = hm.split(":").map(Number);
    const get = () => classifySelects([...root.querySelectorAll("select")]);
    let r = get();
    if (!r.month || !r.day || !r.year || !r.hour || !r.minute) return false;
    const twelve = !!r.ampm || Math.max(...[...r.hour.options].map(optNum).filter((n) => !isNaN(n))) <= 12;
    const want = {
      year: (o) => optNum(o) === Y,
      month: (o) => optNum(o) === M || (/^[一二三四五六七八九十]+月$/.test(o.textContent.trim()) && o.index === [...o.parentElement.options].findIndex((x) => /一月/.test(x.textContent)) + M - 1),
      day: (o) => optNum(o) === D,
      ampm: (o) => (h < 12 ? /am|上午/i : /pm|下午/i).test(o.textContent),
      hour: (o) => optNum(o) === (twelve ? h % 12 || 12 : h),
      minute: (o) => optNum(o) === mi,
    };
    const headerOk = () => {
      const ht = headerTime(root);
      if (!ht) return null;                                   // 读不到顶上那行字，就只看下拉框
      return ht.h === h && ht.mi === mi && (!ht.date || ht.date === day);
    };
    for (let round = 0; round < 3; round++) {
      for (const k of ["year", "month", "day", "ampm", "hour", "minute"]) {
        r = get();                       // 每设一个，X 可能会重画后面的选项
        const sel = r[k];
        if (!sel) continue;
        const o = [...sel.options].find(want[k]);
        if (!o) continue;
        if (round === 0) { if (sel.value !== o.value) setSelect(sel, o); }
        else await setSelectMain(sel, o);                        // 第二轮起用备用办法，不管看起来对不对都再设一次
        await sleep(250);
      }
      await sleep(300);
      r = get();
      const selOk = ["year", "month", "day", "hour", "minute"].every((k) => r[k] && want[k](r[k].options[r[k].selectedIndex] || {})) &&
        (!r.ampm || want.ampm(r.ampm.options[r.ampm.selectedIndex] || { textContent: "" }));
      const hOk = headerOk();
      if (selOk && hOk !== false) return true;
    }
    return false;
  }

  /* 把文字填进 X 的发帖框：先用「粘贴」（X 的输入框会自己接管），不行再模拟打字。
   * X 有时会在这时把发帖框重画一遍，所以每一步都重新找框，出错也不中断 */
  async function typeInto(getEd, text) {
    const landed = () => { const ed = getEd(); return !!ed && normText(ed.innerText) === normText(text); };
    const selectAll = (ed) => {
      try {
        const sel = window.getSelection();
        const r = document.createRange();
        r.selectNodeContents(ed);
        sel.removeAllRanges();
        sel.addRange(r);
      } catch (e) { /* 框刚被重画，下面重试 */ }
    };
    for (let attempt = 0; attempt < 3; attempt++) {
      let ed = getEd();
      if (!ed || !ed.isConnected) { await sleep(400); continue; }
      try { ed.focus(); } catch (e) { /* 忽略 */ }
      selectAll(ed);
      await sleep(120);
      ed = getEd();
      if (!ed) continue;
      try {
        const dt = new DataTransfer();
        dt.setData("text/plain", text);
        ed.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
      } catch (e) { /* 忽略 */ }
      if (await waitFor(landed, 1500, 100)) return true;
      ed = getEd();
      if (!ed) continue;
      try { ed.focus(); } catch (e) { /* 忽略 */ }
      selectAll(ed);
      try { document.execCommand("insertText", false, text); } catch (e) { /* 忽略 */ }
      if (await waitFor(landed, 1500, 100)) return true;
      await sleep(300);
    }
    return false;
  }

  async function runXJob() {
    if (xJobBusy || !xJob) return;
    /* 普通发帖框（/compose/post）打开时是空的：先把文字填进去 */
    if (!xJob.textDone && xJob.text && !findComposer(xJob.text)) {
      const empty = findComposer("");
      if (!empty) return;                                    // 发帖框还没出来
      xJobBusy = true;
      xJobBanner("正在把文字填进发帖框…");
      /* 每次都重新找：弹出框里内容为空的，或者已经填上这条文字的那个框 */
      const getEd = () => { const c = findComposer(xJob.text) || findComposer(""); return c ? c.ed : null; };
      const ok = await typeInto(getEd, xJob.text);
      xJobBusy = false;
      if (!ok) {
        xJobReady = true;
        const diag = scheduleDiag(document, xJob, "文字没能填进发帖框");
        xJobBanner("文字没能自动填进去。请从排期表复制这条的文字，粘贴到发帖框里，再手动设定时。", "warn", diag);
        return;
      }
      xJob.textDone = true;
      await sleep(400);
    }
    /* 只处理内容对得上的发帖框（不是首页顶上那个，也不是上一条还没刷新的页面） */
    if (!findComposer(xJob.text)) return;
    xJobBusy = true;
    const job = xJob;
    let root = null;
    try {
      xJobBanner(`正在把这条交给 X 定时：${job.date} ${job.time}…`);
      await sleep(600);
      /* 先设定时，再放图片：图片上传到一半去开定时窗口，回来图片会丢 */
      if (!job.schedDone) {
        const comp = findComposer(job.text);
        const opt = comp && await waitFor(() => comp.root.querySelector('[data-testid="scheduleOption"]'), 6000);
        if (!opt) throw new Error("没找到发帖框里的「定时」（日历）图标");
        opt.click();
        const sels = await waitFor(() => { const d = document.querySelectorAll("select"); return d.length >= 5 ? d : null; }, 6000);
        if (!sels) throw new Error("X 的定时窗口没打开");
        root = sels[0].closest('[aria-modal="true"], [role="dialog"]') || document;
        await sleep(400);
        xJobBanner(`正在选时间：${job.date} ${job.time}…`);
        if (!(await fillScheduleSelects(root, job.date, job.time))) throw new Error("没能自动选好日期时间");
        await sleep(300);
        const confirm = root.querySelector('[data-testid="scheduledConfirmationPrimaryAction"]') || document.querySelector('[data-testid="scheduledConfirmationPrimaryAction"]');
        if (!confirm) throw new Error("没找到定时窗口里的「确认」按钮");
        if (confirm.getAttribute("aria-disabled") === "true" || confirm.disabled) throw new Error("X 的「确认」按钮是灰的，时间没被 X 接受");
        confirm.click();
        /* 确认真的设上了：发帖框的按钮会变成「安排」，上面出现「将于……发布」 */
        const after = await waitFor(() => { const c = findComposer(job.text); return c && isScheduled(c.root) ? c : null; }, 6000, 200);
        if (!after) throw new Error("定时好像没设上（发帖按钮没变成「安排」）");
        job.schedDone = true;
        root = null;
      }
      if (job.imgs && !job.imgDone) {
        xJobBanner(job.vid ? "定时已设好，正在放视频，视频上传要等一会儿…" : "定时已设好，正在放图片…");
        job.imgDone = true;
        if (!(await attachImages(job.date, job.time, job.imgs, findComposer(job.text)))) {
          xJobReady = true;
          xJobBanner(`定时已设好，但${job.vid ? "视频" : "图片"}没能自动放进去。请点发帖框下面的图片图标手动加，再点「安排」。`, "warn");
          return;
        }
      }
      xJobReady = true;
      const c = findComposer(job.text);
      const btn = c && c.root.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
      if (btn) btn.classList.add("xfb-xjob-btn");
      if (job.auto) { await autoArrange(job); return; }
      xJobBanner(`已经选好 ${job.date} ${job.time}` + (job.imgs ? `，${job.vid ? "视频" : "图片"}也放好了` : "") + `。看一眼，没问题就点 X 的「安排」按钮。` + (job.batch ? "点完会自动打开下一条。" : ""), "ok");
    } catch (err) {
      xJobReady = true;
      const diag = scheduleDiag(root || document, job, err.message);
      try { chrome.storage.local.set({ xfbDiag: diag }); } catch (e) { /* 忽略 */ }
      xJobBanner(`${err.message}。请手动点发帖框下面的日历图标，选 ${job.date} ${job.time}，确认后点「安排」。` + (job.imgs ? `${job.vid ? "视频" : "图片"}也要手动加。` : ""), "warn", diag);
    } finally {
      xJobBusy = false;
    }
  }
  /* 自动点「安排」：只在按钮确实是「安排」（定时已设上）时点，绝不点「发帖」；点之前倒数 3 秒，可以取消 */
  async function autoArrange(job) {
    const what = job.imgs ? `，${job.vid ? "视频" : "图片"}也放好了` : "";
    const ready = () => {
      const c = findComposer(job.text);
      const b = c && c.root.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
      if (!b || !isScheduled(c.root) || !/安排|schedule/i.test(b.textContent)) return null;
      return b.getAttribute("aria-disabled") === "true" || b.disabled ? null : b;
    };
    if (!ready()) xJobBanner(job.vid ? "定时已设好，等视频上传完再点「安排」…" : "定时已设好，等图片上传完再点「安排」…");
    const btn0 = await waitFor(ready, job.vid ? uploadWait(300000) : 60000, 300);
    if (!btn0) { xJobBanner(`已经选好 ${job.date} ${job.time}${what}，但「安排」按钮一直是灰的（可能还在上传）。好了以后请你自己点「安排」。`, "warn"); return; }
    let cancelled = false;
    for (let n = 3; n > 0; n--) {
      xJobBanner(`已经选好 ${job.date} ${job.time}${what}。${n} 秒后自动点「安排」…`, "ok");
      const b = document.getElementById("xfb-xjob");
      const stop = document.createElement("button");
      stop.type = "button";
      stop.textContent = "取消，我自己看看";
      stop.addEventListener("click", () => { cancelled = true; });
      b.append(" ", stop);
      for (let t = 0; t < 10 && !cancelled; t++) await sleep(100);
      if (cancelled) break;
    }
    if (cancelled || xJob !== job) {
      if (xJob === job) xJobBanner(`已取消自动点。看好了自己点 X 的「安排」按钮。` + (job.batch ? "点完会自动打开下一条。" : ""), "ok");
      return;
    }
    const btn = ready();                                     // 倒数期间再确认一次
    if (!btn) { xJobBanner("「安排」按钮变了，没有自动点。请看一眼，自己点「安排」。", "warn"); return; }
    btn.click();                                             // 下面的点击监听会把这一格标成「X 已定时」并打开下一条
    const gone = await waitFor(() => !findComposer(job.text), 10000, 300);
    xJobBanner(gone ? `已安排 ✓ ${job.date} ${job.time}，到点 X 会自己发。` + (job.batch ? "这天还有没交的，会自动打开下一条。" : "")
      : "点了「安排」，但发帖框还在。请看一眼是不是有提示，需要的话自己再点一次。", gone ? "ok" : "warn");
  }

  function xJobBanner(text, kind, diag) {
    let b = document.getElementById("xfb-xjob");
    if (!b) {
      b = document.createElement("div");
      b.id = "xfb-xjob";
      b.setAttribute("role", "status");
      document.body.appendChild(b);
    }
    b.className = kind || "";
    b.textContent = text;
    if (diag) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "复制诊断信息";
      btn.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(diag); btn.textContent = "已复制，粘贴发给 Claude"; }
        catch (e) {
          const ta = document.createElement("textarea");
          ta.value = diag; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
          btn.textContent = "已复制，粘贴发给 Claude";
        }
      });
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "关闭";
      x.addEventListener("click", () => b.remove());
      b.append(" ", btn, " ", x);
    }
  }

  async function checkXJob() {
    if (xJob || !/^\/(intent|compose)\//.test(location.pathname)) return;
    let v;
    try { v = await chrome.storage.local.get(["schedJob"]); } catch (e) { return; }
    const job = v.schedJob;
    if (!job || Date.now() - job.at > 10 * 60 * 1000) return;
    xJob = job;
    xJobReady = false;
    runXJob();
  }
  setInterval(() => { if (!alive()) return; if (!xJob) checkXJob(); else if (!xJobReady) runXJob().catch(() => { xJobBusy = false; }); }, 800);

  /* 你点 X 的「回复」按钮时，记下回复的是哪条帖子、今天回复了几条。发新帖不算 */
  function replyTargetId(btn) {
    const dlg = btn.closest('[role="dialog"]');
    if (dlg) {
      const art = dlg.querySelector(SEL.tweet);
      if (!art) return null;                                   // 弹窗里没有原帖 = 发新帖
      const a = art.querySelector("a[href*='/status/'] time");
      return a ? statusIdOf(a.closest("a").getAttribute("href")) : null;
    }
    return statusIdOf(location.pathname);                      // 帖子页下面的回复框
  }
  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest && e.target.closest('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
    if (!btn || btn.disabled || btn.getAttribute("aria-disabled") === "true") return;
    const id = replyTargetId(btn);
    if (!id) {                                                 // 发的是新帖
      try {
        if (xJob && xJobReady) {                               // 交给 X 定时的这一条：你点了「安排」
          xJob = null;
          xJobBanner("已交给 X 定时 ✓", "ok");
          chrome.runtime.sendMessage({ type: "xfb-sched-x-done", ok: true });
        } else {
          const root = composerRootOf(btn) || btn.closest('[aria-modal="true"], [role="dialog"]') || document;
          const ed = root.querySelector('[data-testid="tweetTextarea_0"]');
          chrome.runtime.sendMessage({ type: "xfb-sched-sent", text: ed ? ed.innerText : "" });   // 从排期表打开的：内容对得上才标成已发
        }
      } catch (err) { /* 插件刚更新过，忽略 */ }
      return;
    }
    const first = !S.replied[id];
    S.replied[id] = Date.now();
    const cut = Date.now() - 14 * 864e5;                       // 只留两周内的记录
    for (const k in S.replied) if (S.replied[k] < cut) delete S.replied[k];
    if (first) S.replyLog[today()] = repliedToday() + 1;
    store.set({ replied: S.replied, replyLog: S.replyLog });
    updateReplyLines();
  }, true);

  /* ---------- 关注时间：关注后先给对方几天时间回关 ---------- */
  function recordFollowTime(key) {
    S.followedAt[key] = Date.now();
    const cut = Date.now() - 30 * 864e5;                       // 只留 30 天内的记录
    for (const k in S.followedAt) if (S.followedAt[k] < cut) delete S.followedAt[k];
    store.set({ followedAt: S.followedAt });
  }
  /* 还在等对方回关：返回还剩多少小时，不在等待期返回 0 */
  function waitLeftH(key) {
    const at = S.followedAt[key];
    if (!at) return 0;
    const left = S.graceDays * 24 - (Date.now() - at) / 36e5;
    return left > 0 ? left : 0;
  }
  const fmtLeft = (h) => (h >= 24 ? `还剩 ${Math.ceil(h / 24)} 天` : `还剩 ${Math.max(1, Math.ceil(h))} 小时`);

  /* ---------- 找互关帖提醒：X 页面顶上的提示条 ---------- */
  function searchHref() {
    const a = panel && panel.querySelector("#xfb-search");
    if (a && a.getAttribute("href")) return a.getAttribute("href");
    const first = String(S.keywords || "蓝V互关").split(/[,，]/).map((x) => x.trim()).filter(Boolean)[0] || "蓝V互关";
    const tag = /^[#＃]/.test(first) ? first : "#" + first;
    const q = S.minReplies > 0 ? `${tag} min_replies:${S.minReplies}` : tag;
    return "/search?q=" + encodeURIComponent(q) + "&src=typed_query&f=live";
  }
  function showRemindBar(on) {
    let bar = document.getElementById("xfb-remind-bar");
    if (!on || /^\/(intent|compose)\//.test(location.pathname)) { if (bar) bar.remove(); return; }
    if (bar) return;
    bar = document.createElement("div");
    bar.id = "xfb-remind-bar";
    bar.setAttribute("role", "status");
    const txt = document.createElement("span");
    txt.textContent = `该找互关帖了` + (S.minReplies > 0 ? `（回复 ≥ ${S.minReplies}）` : "");
    const go = document.createElement("button");
    go.type = "button";
    go.className = "go";
    go.textContent = "打开下一条 →";
    go.addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-round-start" }); } catch (e) { /* 忽略 */ }
      bar.remove();
      location.assign(searchHref() + "#xfb-next");
    });
    const later = document.createElement("button");
    later.type = "button";
    later.textContent = "15 分钟后再提醒";
    later.addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-remind-snooze" }); } catch (e) { /* 忽略 */ }
      bar.remove();
    });
    const x = document.createElement("button");
    x.type = "button";
    x.className = "x";
    x.textContent = "×";
    x.setAttribute("aria-label", "关掉这次提醒");
    x.addEventListener("click", () => {
      try { chrome.runtime.sendMessage({ type: "xfb-remind-done" }); } catch (e) { /* 忽略 */ }
      bar.remove();
    });
    bar.append(txt, go, later, x);
    document.body.appendChild(bar);
  }
  try {
    chrome.storage.local.get(["remindDue"]).then((v) => showRemindBar(!!v.remindDue));
    chrome.storage.onChanged.addListener((ch, area) => { if (area === "local" && ch.remindDue) showRemindBar(!!ch.remindDue.newValue); });
  } catch (e) { /* 忽略 */ }

  /* ---------- 快速流程：F 键关注下一个绿色的人；一键打开下一条没处理的互关帖 ---------- */
  function followNextGreen() {
    const rows = S.rows || [];
    const next = rows.find((r) => !S.following[keyOf(r.handle)] && (quality(r.fo, r.fr) || {}).cls === "good");
    if (!next) {
      setNote(rows.length ? "名单里没有绿色「容易回关」的人了。剩下的自己看着关，或者点「下一条互关帖」。" : "名单是空的。先点「下滑到最后一条」。", true);
      return;
    }
    const btn = panel.querySelector(`#xfb-list .xfb-follow[data-key="${keyOf(next.handle)}"]`);
    quickFollow(next.handle, btn);
  }
  document.addEventListener("keydown", (e) => {
    const k = (e.key || "").toLowerCase();
    if (k !== "f" && k !== "d") return;
    if (e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;
    const el = e.target;
    if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;   // 正在打字时不算
    if (!panel || panel.hidden) return;
    if (k === "d" && panel.dataset.pool === "un") {
      e.preventDefault(); e.stopPropagation();
      if (autoD.running) { stopAutoD(); setNote("自动连点已停止。再按 D 手动取关下一个。"); }
      else unfollowNextUnpool();
    }
    else if (k === "f" && panel.dataset.pool === "1") {
      e.preventDefault(); e.stopPropagation();
      if (autoF.running) { stopAutoF(); setNote("自动连点已停止。再按 F 手动关注下一个。"); }
      else followNextPool();
    }
    else if (k === "f" && S.mode === "status") { e.preventDefault(); e.stopPropagation(); followNextGreen(); }
    else if (k === "d" && S.mode === "following") { e.preventDefault(); e.stopPropagation(); jumpNextNF(); }
  }, true);

  /* ---------- 清理未回关：跳到下一个红色「未回关」 ---------- */
  let jumping = false;
  async function jumpNextNF() {
    if (jumping) return;
    jumping = true;
    try {
      const cur = document.querySelector(".xfb-cell.xfb-focus");
      const minTop = cur && cur.isConnected ? cur.getBoundingClientRect().top + 5 : 70;
      for (let tries = 0; tries < 8; tries++) {
        const next = [...document.querySelectorAll(`${SEL.column} .xfb-cell.xfb-nf`)]
          .find((c) => c !== cur && c.getBoundingClientRect().top > minTop);
        if (next) {
          document.querySelectorAll(".xfb-cell.xfb-focus").forEach((c) => c.classList.remove("xfb-focus"));
          next.classList.add("xfb-focus");
          const y = window.scrollY + next.getBoundingClientRect().top - Math.round(window.innerHeight * 0.3);
          window.scrollTo({ top: Math.max(0, y), behavior: "smooth" });
          if (S.noteKind !== "page") setNote("");
          return;
        }
        /* 已加载的里面没有了：往下滑一屏，让 X 加载更多 */
        if (tries === 0) setNote("正在往下找下一个未回关的人…");
        const before = document.documentElement.scrollHeight;
        window.scrollBy({ top: Math.round(window.innerHeight * 0.85), behavior: "auto" });
        await sleep(900);
        scan();
        await sleep(100);
        if (document.documentElement.scrollHeight === before && window.innerHeight + window.scrollY >= before - 4 && tries >= 2) break;
      }
      setNote("往下没找到未回关的人了，可能已经到列表最底下。", true);
    } finally {
      jumping = false;
    }
  }

  /* 首页/搜索页：打开这一页上第一条还没处理、回复也够多的互关帖（就在当前标签页） */
  function openNextHit() {
    for (const art of document.querySelectorAll(`${SEL.column} ${SEL.tweet}.xfb-hit`)) {
      const t = readTweet(art);
      const sid = t && statusIdOf(t.url);
      if (sid && !S.handled[sid]) { location.assign(t.url); return true; }
    }
    return false;
  }
  /* 帖子页：回到搜索页，加载出来后自动打开下一条 */
  function goNextPost() {
    try { sessionStorage.setItem("xfb-auto-next", String(Date.now())); } catch (e) { /* 忽略 */ }
    const a = panel && panel.querySelector("#xfb-search");
    location.assign(a && a.href ? a.href : "/search?q=" + encodeURIComponent("#蓝V互关") + "&f=live");
  }
  function maybeAutoNext() {
    if (location.hash === "#xfb-next") {        // 从提醒点进来的
      try { sessionStorage.setItem("xfb-auto-next", String(Date.now())); } catch (e) { /* 忽略 */ }
      history.replaceState(history.state, "", location.pathname + location.search);
    }
    let at = 0;
    try { at = Number(sessionStorage.getItem("xfb-auto-next") || 0); } catch (e) { /* 忽略 */ }
    if (!at) return;
    if (Date.now() - at > 20000) {            // 等了 20 秒还没找到
      try { sessionStorage.removeItem("xfb-auto-next"); } catch (e) { /* 忽略 */ }
      setNote("这一页没有还没处理的互关帖。往下滑一点，或者过一会儿再刷新。", true);
      return;
    }
    if (openNextHit()) { try { sessionStorage.removeItem("xfb-auto-next"); } catch (e) { /* 忽略 */ } }
  }

  function markHandled(id, on) {
    if (on) S.handled[id] = Date.now(); else delete S.handled[id];

    const cut = Date.now() - 14 * 864e5;                       // 只留两周内的记录
    for (const k in S.handled) if (S.handled[k] < cut) delete S.handled[k];
    store.set({ handled: S.handled });
    syncHandledButton();
  }
  function syncHandledButton() {
    if (!panel) return;
    const b = panel.querySelector("#xfb-handled");
    const id = statusIdOf(location.pathname);
    const on = !!(id && S.handled[id]);
    b.setAttribute("aria-pressed", String(on));
    setText(b, on ? "已处理 ✓（点一下取消）" : "这条处理完了");
  }

  const statusIdOf = (url) => { const m = String(url || "").match(/\/status\/(\d+)/); return m ? m[1] : null; };
  const repliedToday = () => S.replyLog[today()] || 0;

  /* 抢前排：发出来不到 1 小时、每小时浏览 ≥ 2000、回复还不到 30 条，现在回复最容易排在前面 */
  function isGrab(m) {
    if (!m || m.ageH === null || m.ageH > 1 || !m.views) return false;
    return m.views / Math.max(m.ageH, 0.25) >= 2000 && (m.replies || 0) < 30;
  }

  /* 右上角一小块：[互关 312] [打开] [热度]。悬浮在帖子上，不占位置、不让帖子跳动。返回是否显示了 */
  function applyCorner(art, t, match, enough) {
    const oldBar = art.querySelector(":scope > .xfb-bar");
    if (oldBar && !oldBar.classList.contains("xfb-bar-todo")) oldBar.remove();
    const showHeat = (S.heatAll || match) && !!heatInfo(t.metrics);
    const sid = statusIdOf(t.url);
    const done = !!(sid && S.replied[sid]);
    const handled = !!(sid && S.handled[sid]);
    const grab = !done && !handled && isGrab(t.metrics);
    art.classList.toggle("xfb-handled", handled);
    if (handled) art.classList.remove("xfb-hit");
    let corner = art.querySelector(":scope > .xfb-corner");
    if (!match && !showHeat && !done && !grab && !handled) { if (corner) corner.remove(); return false; }
    if (!corner) {
      corner = document.createElement("div");
      corner.className = "xfb-corner";
      corner.innerHTML = `<span class="xfb-chandled" title="这条互关帖你已经处理完了">已处理</span><span class="xfb-cdone" title="你回复过这条">已回</span><span class="xfb-ctag"></span><a class="xfb-copen" target="_blank" rel="noopener">打开</a><span class="xfb-grab" title="刚发不到 1 小时、浏览涨得快、回复还少。现在回复最容易排在前面">抢</span><span class="xfb-heat"></span>`;
      corner.addEventListener("click", (e) => e.stopPropagation());
      art.appendChild(corner);
    }
    corner.querySelector(".xfb-cdone").hidden = !done;
    corner.querySelector(".xfb-chandled").hidden = !handled;
    corner.querySelector(".xfb-grab").hidden = !grab;
    art.classList.toggle("xfb-replied", done);
    const tag = corner.querySelector(".xfb-ctag");
    const open = corner.querySelector(".xfb-copen");
    tag.hidden = !match || handled;
    open.hidden = !match || !t.url;
    if (match) {
      tag.classList.toggle("low", !enough);
      setText(tag, enough ? `互关 ${fmtN(t.replies)}` : `互关 ${t.replies}<${S.minReplies}`);
      tag.title = enough ? `互关帖，${t.replies} 条回复` : `互关帖，但回复只有 ${t.replies} 条，不到 ${S.minReplies}`;
      if (t.url) open.href = t.url;
    }
    const heat = corner.querySelector(".xfb-heat");
    if (showHeat) setHeat(heat, t.metrics); else heat.hidden = true;
    return !handled;
  }

  /* 个人主页（自己的、别人的都行）：每条帖子右上角显示热度 */
  function heatPosts() {
    for (const art of document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)) {
      const t = readTweet(art);
      if (!t) continue;
      hotConsider(art, t);
      art.classList.add("xfb-art");
      applyCorner(art, t, false, false);
    }
  }

  function scanFeed() {
    if (S.noteKind === "page") setNote("");
    const kws = keywordList();
    let hit = 0, all = 0;
    for (const art of document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)) {
      const t = readTweet(art);
      if (!t) continue;
      hotConsider(art, t);
      all++;
      const text = norm(t.text);
      const match = kws.some((k) => text.includes(k));
      const enough = t.replies >= S.minReplies;
      art.classList.add("xfb-art");
      art.classList.toggle("xfb-hit", match && enough);
      if (applyCorner(art, t, match, enough) && match && enough) hit++;
    }
    setText(panel.querySelector("#xfb-feed-hit"), hit);
    setText(panel.querySelector("#xfb-feed-all"), all);
    updateReplyLines();
    maybeAutoNext();
    wfPoolHash();   // 【时间工作流】
  }
  /* ================= 热点雷达：收集「新发的、每小时浏览 ≥ 5000」的帖子 =================
   * 你刷首页、搜索页、个人主页时顺手收；也可以在热点雷达页点「抓取一次」，开一个搜索页自动往下翻着收。
   * 只读帖子，不点赞、不回复、不关注。分析在 hot.html 里做。 */
  let HOT_MIN = 5000, HOT_AGE = 6, hotBuf = {}, hotTimer = 0;
  chrome.storage.local.get(["hotMin", "hotAge"]).then((v) => { HOT_MIN = Number(v.hotMin) || 5000; HOT_AGE = Number(v.hotAge) || 6; }).catch(() => {});
  chrome.storage.onChanged.addListener((ch, area) => {
    if (area !== "local") return;
    if (ch.hotMin) HOT_MIN = Number(ch.hotMin.newValue) || 5000;
    if (ch.hotAge) HOT_AGE = Number(ch.hotAge.newValue) || 6;
    if (ch.hot) updateHotCount(ch.hot.newValue);
  });
  function hotConsider(art, t) {
    const m = t.metrics;
    if (!m || m.ageH === null || !m.views || !t.url || m.ageH > HOT_AGE) return;
    if (blockHit(art)) return;          // 带屏蔽词的不收
    const vph = m.views / Math.max(m.ageH, 0.25);
    if (vph < HOT_MIN) return;
    const id = statusIdOf(t.url);
    if (!id) return;
    const tx = art.querySelector(SEL.tweetText);
    const av = art.querySelector('[data-testid="Tweet-User-Avatar"] img');
    hotBuf[id] = {
      handle: t.handle, name: t.name, verified: t.verified, avatar: av ? av.src : "",
      text: (t.text || "").slice(0, 1000), lang: (tx && tx.getAttribute("lang")) || "", url: t.url,
      vph: Math.round(vph), views: m.views, likes: m.likes || 0, replies: m.replies || 0, reposts: m.reposts || 0, bookmarks: m.bookmarks || 0,
      postedAt: Math.round(Date.now() - m.ageH * 36e5), seenAt: Date.now(),
      media: art.querySelector('[data-testid="videoPlayer"], [data-testid="videoComponent"]') ? "vid" : art.querySelector('[data-testid="tweetPhoto"]') ? "img" : "",
      quote: art.querySelectorAll(SEL.tweetText).length > 1,
    };
    clearTimeout(hotTimer);
    hotTimer = setTimeout(hotFlush, 1500);
  }
  async function hotFlush() {
    const add = hotBuf;
    hotBuf = {};
    if (!Object.keys(add).length || !alive()) return;
    try {
      const hot = (await chrome.storage.local.get(["hot"])).hot || {};
      for (const [id, it] of Object.entries(add)) hot[id] = { ...(hot[id] || {}), ...it, firstAt: (hot[id] && hot[id].firstAt) || Date.now() };
      /* 只留 3 天内发的，最多 3000 条 */
      const cut = Date.now() - 3 * 864e5;
      let ids = Object.keys(hot).filter((k) => (hot[k].postedAt || 0) >= cut);
      if (ids.length > 3000) ids = ids.sort((a, b) => hot[b].postedAt - hot[a].postedAt).slice(0, 3000);
      const kept = {};
      for (const k of ids) kept[k] = hot[k];
      await store.set({ hot: kept });
    } catch (e) { /* 忽略 */ }
  }
  function updateHotCount(hot) {
    if (!panel) return;
    const cut = Date.now() - HOT_AGE * 36e5;
    const n = Object.values(hot || {}).filter((h) => h.postedAt >= cut).length;
    setText(panel.querySelector("#xfb-hot-n"), n ? String(n) : "");
  }
  setTimeout(() => chrome.storage.local.get(["hot"]).then((v) => updateHotCount(v.hot)).catch(() => {}), 3000);
  /* 「抓取一次」打开的搜索页：自动往下翻，翻完告诉后台关掉 */
  let hotJobDone = false;
  async function hotJobTick() {
    if (hotJobDone || location.pathname !== "/search") return;
    const r = await new Promise((res) => {
      try { chrome.runtime.sendMessage({ type: "xfb-hot-job" }, (x) => res(chrome.runtime.lastError ? null : x)); } catch (e) { res(null); }
    });
    if (!r || !r.job) return;
    hotJobDone = true;
    await waitFor(() => document.querySelector(SEL.tweet) || /没有结果|No results/i.test(document.body.innerText), 15000, 300);
    const grab = () => {
      for (const art of document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)) { const t = readTweet(art); if (t) hotConsider(art, t); }
    };
    let idle = 0;
    for (let i = 0; i < (r.job.rounds || 25) && idle < 3; i++) {
      grab();
      const before = document.documentElement.scrollHeight;
      window.scrollBy(0, Math.round(window.innerHeight * 0.9));
      await sleep(1500);
      idle = document.documentElement.scrollHeight > before ? 0 : idle + 1;
    }
    grab();
    clearTimeout(hotTimer);
    await hotFlush();
    try { chrome.runtime.sendMessage({ type: "xfb-hot-done" }); } catch (e) { /* 忽略 */ }
  }
  setTimeout(() => { if (alive()) hotJobTick(); }, 2500);
  window.addEventListener("popstate", () => setTimeout(() => { if (alive()) hotJobTick(); }, 2500));

  /* 【时间工作流】从时间工作流点「打开待关注池」：地址带 #xfb-pool，就把池子打开 */
  function wfPoolHash() {
    if (location.hash !== "#xfb-pool" || !panel) return;
    history.replaceState(history.state, "", location.pathname + location.search);
    togglePool(true);
  }
  window.addEventListener("hashchange", wfPoolHash);

  /* ---------- 一键关注：不用打开主页。每次点击只关注一个人 ---------- */
  const FOLLOW_GAP = 3000;              // 两次关注之间至少隔 3 秒
  const followWaiters = new Map();
  let followSeq = 0, lastFollowAt = 0;
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || e.data.__xfb !== "follow-res") return;
    const done = followWaiters.get(e.data.id);
    if (done) { followWaiters.delete(e.data.id); done(e.data); }
  });
  function apiFollow(handle) {
    return new Promise((resolve) => {
      const id = ++followSeq;
      followWaiters.set(id, resolve);
      window.postMessage({ __xfb: "follow-req", id, handle }, location.origin);
      setTimeout(() => {
        if (followWaiters.has(id)) { followWaiters.delete(id); resolve({ ok: false, status: 0, code: 0 }); }
      }, 15000);
    });
  }

  /* ================= 待回复评论：只抓你主帖下面、你还没回的评论 =================
   * 「抓取」时后台开一个标签页：先到你的主页「帖子」找最近两天的主帖（最多 50 条），
   * 扫过的帖子会记下当时的回复数；下次回复数没涨就不再打开，只打开有新评论的。
   * 一条评论下面紧跟着就是你的回复，或者插件记得你回过，就算回过了，不抓。 */
  const INBOX_DAYS = 30, INBOX_MAX = 1000, INBOX_POSTS = 50, INBOX_POST_DAYS = 2;
  const authorOf = (art) => { const t = readTweet(art); return t ? t.key : null; };
  const isRepost = (art) => /转帖|转推|转发了|repost/i.test((art.querySelector('[data-testid="socialContext"]') || {}).textContent || "");
  async function inboxJob() {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage({ type: "xfb-inbox-job" }, (r) => resolve(chrome.runtime.lastError ? null : (r && r.job) || null)); }
      catch (e) { resolve(null); }
    });
  }
  async function scrollCollect(rounds, fn, patience = 2) {
    await waitFor(() => document.querySelector(SEL.tweet), 15000, 300);
    await sleep(800);
    fn();
    let idle = 0;
    for (let i = 0; i < rounds && idle < patience; i++) {
      const before = document.documentElement.scrollHeight;
      window.scrollBy(0, Math.round(window.innerHeight * 0.9));
      await sleep(1400);
      const grew = fn();
      idle = grew || document.documentElement.scrollHeight > before ? 0 : idle + 1;
    }
  }
  /* 第一步：主页「帖子」里你最近的主帖（不含转帖） */
  async function inboxProfileStep(job) {
    const me = myHandle();
    const cut = Date.now() - INBOX_POST_DAYS * 864e5;
    const posts = new Map();
    let old = 0;
    await scrollCollect(25, () => {
      const n0 = posts.size;
      for (const art of document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)) {
        const t = readTweet(art);
        if (!t || t.key !== me || isRepost(art)) continue;
        const id = statusIdOf(t.url);
        if (!id || posts.has(id)) continue;
        const tm = art.querySelector("time");
        const at = (tm && Date.parse(tm.getAttribute("datetime"))) || Date.now();
        if (at < cut) { old++; continue; }
        posts.set(id, { id, text: (t.text || "").slice(0, 80), at, replies: t.replies || 0 });
      }
      return posts.size > n0 && posts.size < INBOX_POSTS && old < 3;
    });
    /* 哪些要打开、哪些跳过，后台根据「上次扫到的回复数」决定 */
    const list = [...posts.values()].sort((a, b) => b.at - a.at).slice(0, INBOX_POSTS);
    chrome.runtime.sendMessage({ type: "xfb-inbox-posts", posts: list });
  }
  /* 第二步：打开一条主帖，读下面别人的回复，判断你回没回 */
  async function inboxPostStep(job, postId) {
    const me = myHandle();
    let v;
    try { v = await chrome.storage.local.get(["inbox", "replied"]); } catch (e) { return; }
    const inbox = v.inbox || {}, replied = v.replied || {};
    const post = (job.posts || []).find((p) => p.id === postId) || { id: postId, text: "" };
    const seen = new Map();   // 评论 ID -> { data, repliedByMe }
    const mineIds = new Set();
    /* X 先出帖子本身，评论要晚几秒才加载：有回复数的帖子，先等评论出来再开始读 */
    const want = post.replies || 0;
    const belowFocal = () => {
      const arts = [...document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)];
      const fi = arts.findIndex((a) => { const t = readTweet(a); return t && statusIdOf(t.url) === postId; });
      return fi >= 0 ? arts.length - fi - 1 : 0;
    };
    if (want > 0) await waitFor(() => belowFocal() > 0, 15000, 300);
    /* 翻到底，遇到「显示更多回复」就点开：X 会把新评论、小号评论折叠或排到后面 */
    const collect = () => {
      const n0 = seen.size, m0 = mineIds.size;
      const opened = clickMoreReplies();
      const arts = [...document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)];
      const fi = arts.findIndex((a) => { const t = readTweet(a); return t && statusIdOf(t.url) === postId; });
      for (let i = fi + 1; i < arts.length; i++) {
        const art = arts[i];
        const t = readTweet(art);
        if (!t || !t.url) continue;
        const id = statusIdOf(t.url);
        if (!id || id === postId) continue;
        if (t.key === me) {
          mineIds.add(id);
          /* 你的回复：紧挨着的上一条别人的评论，算你回过了 */
          const prev = arts[i - 1] && readTweet(arts[i - 1]);
          const pid = prev && prev.key !== me && statusIdOf(prev.url);
          if (pid && seen.has(pid)) seen.get(pid).mine = true;
          continue;
        }
        if (seen.has(id)) continue;
        const tm = art.querySelector("time");
        const av = art.querySelector('[data-testid="Tweet-User-Avatar"] img');
        seen.set(id, {
          mine: false,
          data: {
            handle: t.handle, name: t.name, avatar: av ? av.src : "", text: (t.text || "").slice(0, 600), url: t.url,
            at: (tm && Date.parse(tm.getAttribute("datetime"))) || Date.now(), got: Date.now(), state: "",
            media: !!art.querySelector('[data-testid="tweetPhoto"], [data-testid="videoPlayer"]'),
            post: postId, postText: post.text || "",
          },
        });
      }
      return opened || seen.size > n0 || mineIds.size > m0;
    };
    await scrollCollect(15, collect, 3);
    /* 看到的比回复数少：回到顶上，等一下再读一遍 */
    for (let k = 0; k < 1 && seen.size + mineIds.size < want; k++) {
      window.scrollTo(0, 0);
      await sleep(2500);
      await scrollCollect(15, collect, 3);
    }
    let added = 0;
    for (const [id, x] of seen) {
      const done = x.mine || !!replied[id];
      if (inbox[id]) {
        if (done && !inbox[id].state) { inbox[id].state = "done"; inbox[id].doneAt = Date.now(); }
        inbox[id].post = postId; inbox[id].postText = post.text || inbox[id].postText || "";
        continue;
      }
      if (done) continue;                       // 回过的不收
      inbox[id] = x.data;
      added++;
    }
    const cut = Date.now() - INBOX_DAYS * 864e5;
    let ids = Object.keys(inbox).filter((k) => inbox[k].at >= cut);
    if (ids.length > INBOX_MAX) ids = ids.sort((a, b) => inbox[b].at - inbox[a].at).slice(0, INBOX_MAX);
    const kept = {};
    for (const k of ids) kept[k] = inbox[k];
    try { await chrome.storage.local.set({ inbox: kept }); } catch (e) { /* 忽略 */ }
    /* 看全了：数量够，或者评论确实加载出来了、又重读过一遍（回复数里可能有删掉/隐藏的，凑不齐） */
    const got = seen.size + mineIds.size;
    chrome.runtime.sendMessage({ type: "xfb-inbox-next", added, seen: got, full: got >= want || got > 0 });
  }
  let inboxRunning = false, inboxLastPath = "";
  async function inboxTick() {
    if (inboxRunning || location.pathname + location.search === inboxLastPath) return;
    const me = myHandle();
    if (!me) return;
    const path = location.pathname;
    const onProfile = keyOf((path.match(/^\/([A-Za-z0-9_]{1,15})\/?$/) || [])[1] || "") === me;
    const sm = path.match(/^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)\/?$/);
    if (!onProfile && !(sm && keyOf(sm[1]) === me)) return;
    inboxRunning = true;
    try {
      const job = await inboxJob();
      if (!job) return;
      inboxLastPath = location.pathname + location.search;
      if (job.phase === "profile" && onProfile) await inboxProfileStep(job);
      else if (job.phase === "post" && sm && job.posts[job.idx] && job.posts[job.idx].id === sm[2]) await inboxPostStep(job, sm[2]);
      else inboxLastPath = "";
    } finally {
      inboxRunning = false;
    }
  }
  setInterval(() => { if (alive()) inboxTick(); }, 1500);

  /* 待回复评论页面里点「回复」：后台打开 X 的「回复这条」页面（内容已填好），这里替你点发送，发完告诉后台关掉 */
  let sendBusy = false;
  async function inboxSendTick() {
    if (sendBusy || !/^\/(intent|compose)\//.test(location.pathname)) return;
    sendBusy = true;
    try {
      const job = await new Promise((resolve) => {
        try { chrome.runtime.sendMessage({ type: "xfb-inbox-send-job" }, (r) => resolve(chrome.runtime.lastError ? null : (r && r.job) || null)); }
        catch (e) { resolve(null); }
      });
      if (!job || job.clicked) return;
      const comp = await waitFor(() => findComposer(job.text), 15000, 300);
      if (!comp) { chrome.runtime.sendMessage({ type: "xfb-inbox-sent", ok: false, why: "发帖框里的内容和你写的对不上" }); return; }
      const btn = await waitFor(() => {
        const b = comp.root.querySelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
        return b && b.getAttribute("aria-disabled") !== "true" && !b.disabled ? b : null;
      }, 10000, 300);
      if (!btn) { chrome.runtime.sendMessage({ type: "xfb-inbox-sent", ok: false, why: "X 的「回复」按钮是灰的" }); return; }
      chrome.runtime.sendMessage({ type: "xfb-inbox-clicked" });
      btn.click();
      const gone = await waitFor(() => !findComposer(job.text), 12000, 300);
      chrome.runtime.sendMessage({ type: "xfb-inbox-sent", ok: !!gone, why: gone ? "" : "点了回复，但发帖框还在，可能有提示" });
    } finally {
      sendBusy = false;
    }
  }
  setInterval(() => { if (alive()) inboxSendTick(); }, 1200);

  function updateInboxCount() {
    if (!panel) return;
    chrome.storage.local.get(["inbox", "replied"]).then((v) => {
      const inbox = v.inbox || {}, replied = v.replied || {};
      const n = Object.keys(inbox).filter((k) => inbox[k].post && !inbox[k].hidden && !inbox[k].state && !replied[k]).length;
      setText(panel.querySelector("#xfb-inbox-n"), n ? String(n) : "");
    }).catch(() => {});
  }
  try { chrome.storage.onChanged.addListener((ch, area) => { if (area === "local" && (ch.inbox || ch.replied)) updateInboxCount(); }); } catch (e) { /* 忽略 */ }

  /* ================= 待关注池 ================= */
  const POOL_MAX = 500, POOL_DAYS = 7;
  function poolPrune() {
    const cut = Date.now() - POOL_DAYS * 864e5;
    let changed = false;
    for (const [k, v] of Object.entries(S.pool)) {
      if (S.following[k] || S.done[k] || !v || v.at < cut) { delete S.pool[k]; changed = true; }
    }
    const keys = Object.keys(S.pool);
    if (keys.length > POOL_MAX) {
      keys.sort((a, b) => S.pool[a].at - S.pool[b].at).slice(0, keys.length - POOL_MAX).forEach((k) => delete S.pool[k]);
      changed = true;
    }
    return changed;
  }
  /* 帖子页扫描时调用：只收「容易回关」、没关注过、没被你取关过的人 */
  function poolConsider(key, v, rel) {
    if (S.following[key] || S.done[key] || rel === "1") { if (S.pool[key]) { delete S.pool[key]; saveLater("pool"); } return; }
    if (S.onlyVerified && !v.verified) return;
    const q = quality(v.fo, v.fr);
    if (!q || q.cls !== "good") return;
    const old = S.pool[key];
    const from = statusIdOf(location.pathname);
    if (old && old.fo === v.fo && old.fr === v.fr && old.name === v.name) return;
    S.pool[key] = { handle: v.handle, name: v.name || "", fo: v.fo, fr: v.fr, verified: !!v.verified, from: (old && old.from) || from, at: (old && old.at) || Date.now() };
    poolPrune();
    saveLater("pool");
    updatePoolCount();
  }
  function poolList() {
    poolPrune();
    /* 关注的人和粉丝最接近的排前面（最可能回关），一样的话新进池的在前 */
    const score = (v) => Math.abs(1 - (v.fr || 0) / Math.max(v.fo || 1, 1));
    return Object.entries(S.pool).map(([k, v]) => ({ key: k, ...v })).sort((a, b) => score(a) - score(b) || b.at - a.at);
  }
  function updatePoolCount() {
    if (!panel) return;
    setText(panel.querySelector("#xfb-pool-n"), Object.keys(S.pool).filter((k) => !S.following[k]).length);
  }
  function togglePool(on) {
    if (!panel) return;
    const want = on === undefined ? panel.dataset.pool !== "1" : on;
    panel.dataset.pool = want ? "1" : "";
    panel.querySelector("#xfb-poolbtn").setAttribute("aria-pressed", String(want));
    panel.querySelector("#xfb-unpoolbtn").setAttribute("aria-pressed", "false");
    panel.classList.remove("xfb-collapsed");
    setText(panel.querySelector("#xfb-title"), want ? "回关助手 · 待关注池" : (TITLES[panel.dataset.mode] || "回关助手"));
    if (want) { renderPool(); autoFRender(); startAutoF(); stopAutoD(); }
    else stopAutoF();
  }
  function renderPool() {
    updatePoolCount();
    if (!panel || panel.dataset.pool !== "1") return;
    const list = panel.querySelector("#xfb-pool-list");
    const rows = poolList();
    const sig = rows.map((r) => r.key + (S.following[r.key] ? "1" : "0")).join(",");
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.textContent = "";
    if (!rows.length) {
      const p = document.createElement("div");
      p.className = "xfb-empty";
      p.textContent = "池子是空的。打开几条互关帖、点「下滑到最后一条」，容易回关的人就会自动进来。";
      list.appendChild(p);
      return;
    }
    for (const r of rows) {
      const row = document.createElement("div");
      row.className = "xfb-item";
      const who = document.createElement("span");
      who.className = "xfb-who";
      const nm = document.createElement("b");
      nm.textContent = r.name || r.handle;
      const hd = document.createElement("small");
      hd.textContent = "@" + r.handle;
      who.append(nm, hd);
      const q = quality(r.fo, r.fr);
      if (q) {
        const qs = document.createElement("small");
        fillQuality(qs, q);
        setText(qs, q.text);
        who.appendChild(qs);
      }
      const btns = document.createElement("span");
      btns.className = "xfb-item-btns";
      const a = document.createElement("a");
      a.className = "xfb-open";
      a.href = "/" + r.handle;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "主页";
      const x = document.createElement("button");
      x.type = "button";
      x.className = "xfb-pool-x";
      x.textContent = "×";
      x.title = "移出池子（以后再遇到还会进来）";
      x.setAttribute("aria-label", `把 @${r.handle} 移出池子`);
      x.addEventListener("click", () => { delete S.pool[r.key]; store.set({ pool: S.pool }); renderPool(); });
      btns.append(followButton(r.handle), a, x);
      row.append(who, btns);
      list.appendChild(row);
    }
  }
  function followNextPool() {
    const next = poolList().find((r) => !S.following[r.key]);
    if (!next) { setNote("池子里没人了。打开几条互关帖，容易回关的人会自动进来。", true); return "empty"; }
    const btn = panel.querySelector(`#xfb-pool-list .xfb-follow[data-key="${next.key}"]`);
    return quickFollow(next.handle, btn);
  }

  /* ================= 取关池 =================
   * 规则和「正在关注」页一样：你关注了他、他没回关你、过了等待期、没点「保留」、没取关过。
   * 在「正在关注」页上滑过的人，符合的自动收进来；回关了、点了保留、取关了，自动移出。
   * 取关前先问一下 X 对方现在有没有回关你，回关了就不取关。每点一次只取关一个人。 */
  const UNPOOL_MAX = 3000;
  function unpoolConsider(c) {
    const key = c.key;
    const ok = !c.followsYou && !!c.unfollowBtn && !S.kept[key] && !S.done[key] && !waitLeftH(key);
    if (ok) {
      const old = S.unpool[key];
      if (old && old.name === c.name) return;
      S.unpool[key] = { handle: c.handle, name: c.name || "", at: (old && old.at) || Date.now() };
      const keys = Object.keys(S.unpool);
      if (keys.length > UNPOOL_MAX) keys.sort((a, b) => S.unpool[a].at - S.unpool[b].at).slice(0, keys.length - UNPOOL_MAX).forEach((k) => delete S.unpool[k]);
    } else if (S.unpool[key]) {
      delete S.unpool[key];
    } else return;
    saveLater("unpool");
    updateUnpoolCount();
  }
  function unpoolList() {
    for (const k of Object.keys(S.unpool)) if (S.kept[k] || S.done[k] || waitLeftH(k)) delete S.unpool[k];
    /* 关注得最久的排前面（没有关注时间的按进池先后） */
    const t = (r) => S.followedAt[r.key] || r.at || 0;
    return Object.entries(S.unpool).map(([k, v]) => ({ key: k, ...v })).sort((a, b) => t(a) - t(b));
  }
  function updateUnpoolCount() {
    if (!panel) return;
    setText(panel.querySelector("#xfb-unpool-n"), unpoolList().length);
    setText(panel.querySelector(".xfb-un-today2"), unfollowedToday());
    panel.querySelector(".xfb-un-today-line2").classList.toggle("over", unfollowedToday() >= UNFOLLOW_LIMIT);
  }
  function toggleUnpool(on) {
    if (!panel) return;
    const want = on === undefined ? panel.dataset.pool !== "un" : on;
    panel.dataset.pool = want ? "un" : "";
    panel.querySelector("#xfb-unpoolbtn").setAttribute("aria-pressed", String(want));
    panel.querySelector("#xfb-poolbtn").setAttribute("aria-pressed", "false");
    panel.classList.remove("xfb-collapsed");
    setText(panel.querySelector("#xfb-title"), want ? "回关助手 · 取关池" : (TITLES[panel.dataset.mode] || "回关助手"));
    if (want) { renderUnpool(true); autoDRender(); startAutoD(); stopAutoF(); }
    else stopAutoD();
  }
  function renderUnpool(force) {
    updateUnpoolCount();
    if (!panel || panel.dataset.pool !== "un") return;
    const list = panel.querySelector("#xfb-unpool-list");
    const rows = unpoolList();
    const sig = rows.map((r) => r.key).join(",") + "|" + (S.armed || "") + "|" + (unBusy ? 1 : 0);
    if (!force && list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.textContent = "";
    if (!rows.length) {
      const p = document.createElement("div");
      p.className = "xfb-empty";
      p.textContent = "取关池是空的。打开你的「正在关注」页往下滑（可以点「自动下滑加载」），没回关的人会自动进来。";
      list.appendChild(p);
      return;
    }
    for (const r of rows.slice(0, 300)) {
      const row = document.createElement("div");
      row.className = "xfb-item";
      const who = document.createElement("span");
      who.className = "xfb-who";
      const nm = document.createElement("b");
      nm.textContent = r.name || r.handle;
      const hd = document.createElement("small");
      hd.textContent = "@" + r.handle;
      who.append(nm, hd);
      const fa = S.followedAt[r.key];
      if (fa) { const d = document.createElement("small"); d.textContent = `关注了 ${Math.max(1, Math.floor((Date.now() - fa) / 864e5))} 天`; who.appendChild(d); }
      const btns = document.createElement("span");
      btns.className = "xfb-item-btns";
      const un = document.createElement("button");
      un.type = "button";
      un.className = "xfb-unf";
      un.dataset.key = r.key;
      un.textContent = S.armed === "u:" + r.key ? "超上限，再点确认" : "取关";
      un.disabled = unBusy;
      un.addEventListener("click", () => { stopAutoD(); unpoolUnfollow(r.handle); });
      const a = document.createElement("a");
      a.className = "xfb-open";
      a.href = "/" + r.handle;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "主页";
      const keep = document.createElement("button");
      keep.type = "button";
      keep.textContent = "保留";
      keep.title = "以后不取关他（和「正在关注」页的「保留」一样）";
      keep.addEventListener("click", async () => {
        S.kept[r.key] = today();
        delete S.unpool[r.key];
        await store.set({ kept: S.kept, unpool: S.unpool });
        renderUnpool(true);
        setNote(`已保留 @${r.handle}，以后不会再进取关池。`);
      });
      btns.append(un, a, keep);
      row.append(who, btns);
      list.appendChild(row);
    }
    if (rows.length > 300) {
      const p = document.createElement("div");
      p.className = "xfb-empty";
      p.textContent = `还有 ${rows.length - 300} 个，取关掉前面的会接着显示。`;
      list.appendChild(p);
    }
  }
  function unpoolNextHandle() {
    const r = unpoolList()[0];
    return r ? r.handle : null;
  }
  /* 返回取关结果给自动连点用：ok / removed（对方已回关、已没关注、账号没了——人已移出池子，继续下一个）/
   * busy / limit（到了今天 150 的建议上限，不自动确认）/ limited（X 限流）/ failed。手动按 D 不看返回值。 */
  async function unfollowNextUnpool() {
    const h = unpoolNextHandle();
    if (!h) { setNote("取关池里没人了。打开「正在关注」页往下滑，没回关的人会自动进来。", true); return "empty"; }
    return unpoolUnfollow(h);
  }
  const unWaiters = new Map();
  let unSeq = 0, unBusy = false;
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || e.data.__xfb !== "unfollow-res") return;
    const done = unWaiters.get(e.data.id);
    if (done) { unWaiters.delete(e.data.id); done(e.data); }
  });
  function apiUnfollow(handle) {
    return new Promise((resolve) => {
      const id = ++unSeq;
      unWaiters.set(id, resolve);
      window.postMessage({ __xfb: "unfollow-req", id, handle, me: myHandle() }, location.origin);
      setTimeout(() => { if (unWaiters.has(id)) { unWaiters.delete(id); resolve({ ok: false, status: 0, code: 0 }); } }, 15000);
    });
  }
  async function unpoolUnfollow(handle) {
    const key = keyOf(handle);
    if (unBusy || !S.unpool[key]) return "busy";
    if (!myHandle()) { setNote("没认出你的账号，刷新一下 X 页面再试。", true); return "failed"; }
    if (unfollowedToday() >= UNFOLLOW_LIMIT && S.armed !== "u:" + key) {
      S.armed = "u:" + key;
      setNote(`今天已经取关 ${unfollowedToday()} 个，超过 ${UNFOLLOW_LIMIT} 的建议上限，继续容易被 X 限流。确定要取关 @${handle} 就再点一次。`, true);
      renderUnpool(true);
      return "limit";
    }
    S.armed = null;
    unBusy = true;
    renderUnpool(true);
    panel.querySelectorAll(`.xfb-unf[data-key="${key}"]`).forEach((b) => setText(b, "取关中…"));
    try {
      const r = await apiUnfollow(handle);
      if (r.ok) {
        S.done[key] = today();
        S.log[today()] = unfollowedToday() + 1;
        delete S.following[key];
        delete S.followedAt[key];
        delete S.unpool[key];
        await store.set({ done: S.done, log: S.log, following: S.following, followedAt: S.followedAt, unpool: S.unpool });
        setNote(`已取关 @${handle}。`);
        return "ok";
      } else if (r.checked && r.followsYou) {
        delete S.unpool[key];
        await store.set({ unpool: S.unpool });
        setNote(`@${handle} 已经回关你了，没取关，已移出取关池。`);
        return "removed";
      } else if (r.checked && r.notFollowing) {
        delete S.unpool[key];
        delete S.following[key];
        await store.set({ unpool: S.unpool, following: S.following });
        setNote(`你已经没在关注 @${handle} 了，已移出取关池。`);
        return "removed";
      } else if (r.status === 429 || r.code === 88) {
        setNote("操作太频繁，X 暂时不让取关。歇几分钟再试。", true);
        return "limited";
      } else if (r.code === 50 || r.code === 63) {
        delete S.unpool[key];
        await store.set({ unpool: S.unpool });
        setNote(`@${handle} 这个账号已经不存在或被封了，已移出取关池。`, true);
        return "removed";
      } else {
        setNote(`取关 @${handle} 没成功${r.status ? `（${r.status}）` : ""}。可以点「主页」到他主页手动取关，或者等一会再试。`, true);
        return "failed";
      }
    } finally {
      await sleep(UNFOLLOW_COOLDOWN_MS);
      unBusy = false;
      renderUnpool(true);
    }
  }

  /* 返回关注结果给自动连点用：ok / gap（不到 3 秒）/ busy / limit（到了建议上限）/ limited（X 限流）/ failed。
   * 手动点「关注」和按 F 的地方都不看返回值，行为和原来一样。 */
  async function quickFollow(handle, btn) {
    const key = keyOf(handle);
    if (S.following[key] || (btn && btn.disabled)) return "busy";
    if (followedToday() >= FOLLOW_LIMIT && S.armed !== "f:" + key) {
      S.armed = "f:" + key;
      setNote(`今天已经关注 ${followedToday()} 个，超过 ${FOLLOW_LIMIT} 的建议上限，继续容易被 X 限制。确定要关注 @${handle} 就再点一次。`, true);
      return "limit";
    }
    const wait = FOLLOW_GAP - (Date.now() - lastFollowAt);
    if (wait > 0) { setNote(`点得有点快，等 ${Math.ceil(wait / 1000)} 秒再关注下一个。`, true); return "gap"; }
    S.armed = null;
    lastFollowAt = Date.now();
    const btns = document.querySelectorAll(`.xfb-follow[data-key="${key}"]`);
    btns.forEach((b) => { b.disabled = true; setText(b, "关注中…"); });

    const r = await apiFollow(handle);
    if (r.ok) {
      S.following[key] = handle;
      delete S.pending[key];
      S.followLog[today()] = followedToday() + 1;
      recordFollowTime(key);
      try { chrome.runtime.sendMessage({ type: "xfb-round-follow" }); } catch (e) { /* 忽略 */ }
      delete S.pool[key];
      await store.set({ following: S.following, pending: S.pending, followLog: S.followLog, pool: S.pool });
      renderPool();
      setNote(`已关注 @${handle}。`);
      if (panel) panel.querySelector("#xfb-list").dataset.sig = "-";
      scan();
      return "ok";
    }
    btns.forEach((b) => { b.disabled = false; setText(b, "关注"); });
    if (r.code === 161) {
      setNote("X 提示你暂时关注太多了，现在不能再关注。今天先停下，明天再继续。", true);
      return "limited";
    } else if (r.status === 429 || r.code === 88) {
      setNote("操作太频繁，X 暂时不让关注。歇几分钟再试。", true);
      return "limited";
    } else if (r.code === 162 || r.code === 160) {
      setNote(`关注 @${handle} 没成功：对方拉黑了你，或者已经在等对方通过关注申请。`, true);
      return "failed";
    } else {
      window.open("/" + handle, "_blank", "noopener");
      setNote(`直接关注没成功${r.status ? `（${r.status}）` : ""}，已经帮你打开 @${handle} 的主页，在那里手动点「关注」。`, true);
      return "failed";
    }
  }

  function followButton(handle) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "xfb-follow";
    b.dataset.key = keyOf(handle);
    b.textContent = "关注";
    b.title = "直接关注，不用打开主页";
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); stopAutoF(); quickFollow(handle, b); });
    return b;
  }


  /* ---------- 待关注池自动连点：打开池子后自动按 F，每 3.5 秒关注一个，满 15 个自动停 ----------
   * 走同一条 quickFollow：3 秒间隔、X 限流提示、这一轮计数都和手动按 F 一样。
   * 停下的情况：满 15 个、池子空了、X 限流、某次关注失败（已打开对方主页）、关掉池子、
   * 点「停止连点」、按 F、手动点某个「关注」按钮。点「开始连点」可手动开始（开关关掉也能手动开始）。 */
  const AUTOF_GAP = 3500;    // 自动连点间隔（毫秒），比 3 秒的关注间隔多留半秒
  const AUTOF_MAX = 15;      // 满这么多个自动停
  const AUTOF_FIRST = 1200;  // 打开池子后，第一次点击前的等待
  let autoFEnabled = true;   // 「打开池子自动连点」开关，存在 autoFOn 里
  const autoF = { timer: null, count: 0, running: false };

  function autoFRender() {
    const box = panel && panel.querySelector("#xfb-autof");
    if (box) {
      box.hidden = !autoF.running;
      if (autoF.running) setText(panel.querySelector("#xfb-autof-txt"), `自动连点：已关注 ${autoF.count} / ${AUTOF_MAX}`);
    }
    const st = panel && panel.querySelector("#xfb-autof-start");
    if (st) st.disabled = autoF.running;
    const sp = panel && panel.querySelector("#xfb-autof-stop");
    if (sp) sp.disabled = !autoF.running;
  }
  function startAutoF(manual) {
    if (autoF.running || (!manual && !autoFEnabled)) return;   // 手动点「开始连点」时，开关关掉也能跑；开关只管打开池子时自动跑不跑
    autoF.running = true;
    autoF.count = 0;
    autoFRender();
    autoF.timer = setTimeout(autoFTick, AUTOF_FIRST);
  }
  function stopAutoF() {
    if (!autoF.running && !autoF.timer) return;
    autoF.running = false;
    if (autoF.timer) { clearTimeout(autoF.timer); autoF.timer = null; }
    autoFRender();
  }
  async function autoFTick() {
    autoF.timer = null;
    if (!autoF.running) return;
    if (!panel || panel.dataset.pool !== "1") { stopAutoF(); return; }
    let res;
    try { res = await followNextPool(); } catch (e) { res = "failed"; }
    if (!autoF.running) return;   // 等关注结果时被停下了（比如关了池子）
    if (res === "ok") {
      autoF.count++;
      autoFRender();
      if (autoF.count >= AUTOF_MAX) { stopAutoF(); setNote("自动连点：已关注满 15 个，停下了。"); return; }
    } else if (res === "gap" || res === "busy") {
      /* 这次没点成（离上次关注还不到 3 秒、或者按钮正忙）：不计数，下一轮再试 */
    } else {
      /* empty（池子空了）、limited（X 限流）、failed（关注失败）、limit（到了建议上限）：
       * 原因 quickFollow 已经写在提示里了，这里直接停下 */
      stopAutoF();
      return;
    }
    autoF.timer = setTimeout(autoFTick, AUTOF_GAP);
  }

  /* ---------- 取关池自动连点：打开取关池后自动按 D，每 3 秒取关一个，满 50 个自动停 ----------
   * 走同一条 unpoolUnfollow：取关前先查对方有没有回关、1.5 秒冷却、今天 150 的建议上限都和手动按 D 一样。
   * 每次取关本身要先查关系再调接口，加上 1.5 秒冷却，所以实际两次取关之间会比 3 秒稍长。
   * 停下的情况：满 50 个、池子空了、到了今天的建议上限、X 限流、某次取关失败、关掉取关池、
   * 点「停止连点」、按 D、手动点某个「取关」按钮。点「开始连点」可手动开始（开关关掉也能手动开始）。 */
  const AUTOD_GAP = 3000;    // 自动连点间隔（毫秒）；取关函数里还有 1.5 秒冷却，实际间隔会稍长
  const AUTOD_MAX = 50;      // 满这么多个自动停
  const AUTOD_FIRST = 1200;  // 打开取关池后，第一次点击前的等待
  let autoDEnabled = true;   // 「打开取关池自动连点」开关，存在 autoDOn 里
  const autoD = { timer: null, count: 0, running: false };

  function autoDRender() {
    const box = panel && panel.querySelector("#xfb-autod");
    if (box) {
      box.hidden = !autoD.running;
      if (autoD.running) setText(panel.querySelector("#xfb-autod-txt"), `自动连点：已取关 ${autoD.count} / ${AUTOD_MAX}`);
    }
    const st = panel && panel.querySelector("#xfb-autod-start");
    if (st) st.disabled = autoD.running;
    const sp = panel && panel.querySelector("#xfb-autod-stop");
    if (sp) sp.disabled = !autoD.running;
  }
  function startAutoD(manual) {
    if (autoD.running || (!manual && !autoDEnabled)) return;   // 手动点「开始连点」时，开关关掉也能跑；开关只管打开池子时自动跑不跑
    autoD.running = true;
    autoD.count = 0;
    autoDRender();
    autoD.timer = setTimeout(autoDTick, AUTOD_FIRST);
  }
  function stopAutoD() {
    if (!autoD.running && !autoD.timer) return;
    autoD.running = false;
    if (autoD.timer) { clearTimeout(autoD.timer); autoD.timer = null; }
    autoDRender();
  }
  async function autoDTick() {
    autoD.timer = null;
    if (!autoD.running) return;
    if (!panel || panel.dataset.pool !== "un") { stopAutoD(); return; }
    let res;
    try { res = await unfollowNextUnpool(); } catch (e) { res = "failed"; }
    if (!autoD.running) return;   // 等取关结果时被停下了（比如关了池子）
    if (res === "ok") {
      autoD.count++;
      autoDRender();
      if (autoD.count >= AUTOD_MAX) { stopAutoD(); setNote("自动连点：已取关满 50 个，停下了。"); return; }
    } else if (res === "removed") {
      /* 对方已经回关 / 已经没关注 / 账号没了：人已移出池子，不计数，继续下一个 */
    } else if (res === "busy") {
      /* 这次没点成（正在取关中、或者人已不在池子）：不计数，下一轮再试 */
    } else {
      /* empty（池子空了）、limit（到了今天的建议上限）、limited（X 限流）、failed（取关失败）：
       * 原因 unpoolUnfollow 已经写在提示里了，这里直接停下 */
      stopAutoD();
      return;
    }
    autoD.timer = setTimeout(autoDTick, AUTOD_GAP);
  }

  /* 账号质量：粉丝数 / 关注数。关注的人和粉丝差不多多的，多半会回关；粉丝很多却几乎不关注人的大号，很少回关 */
  function quality(fo, fr) {
    if (fo == null || fr == null) return null;
    const ratio = fr / Math.max(fo, 1);
    let cls = "", label = "";
    if (fo >= 10000 && ratio < 0.2) { cls = "big"; label = "大号难回"; }
    else if (ratio >= 0.8 && fr >= 50) { cls = "good"; label = "容易回关"; }
    else if (ratio < 0.3) { cls = "low"; label = "回关率低"; }
    return { cls, label, text: `${fmtN(fo)}粉/${fmtN(fr)}关` };
  }
  function countsOf(art, key) {
    if (art.getAttribute("data-xfb-rel-for") !== key) return [null, null];
    const fo = art.getAttribute("data-xfb-fo"), fr = art.getAttribute("data-xfb-fr");
    return fo === null || fr === null ? [null, null] : [Number(fo), Number(fr)];
  }
  function fillQuality(el, q) {
    el.hidden = !q;
    if (!q) return;
    el.className = "xfb-q" + (q.cls ? " " + q.cls : "");
    setText(el, q.label ? `${q.text} · ${q.label}` : q.text);
    el.title = "粉丝数 / 关注数。关注的人和粉丝差不多多的，多半会回关；粉丝很多却几乎不关注人的大号，很少回关。";
  }

  /* ========== 模式三：帖子详情，找没关注的回复者 ========== */
  function scanStatus() {
    const me = myHandle();
    let pendingChanged = false, followingChanged = false, unknown = 0, total = 0;
    for (const art of document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)) {
      const t = readTweet(art);
      if (!t || t.key === me) continue;
      const prev = S.seen.get(t.key);
      const [fo, fr] = countsOf(art, t.key);
      S.seen.set(t.key, {
        handle: t.handle, name: t.name || (prev && prev.name) || "", verified: t.verified || !!(prev && prev.verified),
        fo: fo ?? (prev ? prev.fo : null), fr: fr ?? (prev ? prev.fr : null),
      });
      /* 优先用 X 页面数据里的关注状态，读不到再查插件记住的关注列表 */
      const rel = art.getAttribute("data-xfb-rel-for") === t.key ? art.getAttribute("data-xfb-rel") : null;
      total++;
      if (rel === "1" && !S.following[t.key]) { S.following[t.key] = t.handle; followingChanged = true; }
      poolConsider(t.key, S.seen.get(t.key), rel);
      if (rel === null && !S.following[t.key]) unknown++;
      const following = !!S.following[t.key] || rel === "1";
      const eligible = !following && (!S.onlyVerified || t.verified);
      art.classList.add("xfb-art");
      art.classList.toggle("xfb-todo", eligible);
      art.classList.toggle("xfb-have", following);
      let bar = art.querySelector(":scope > .xfb-bar");
      if (!eligible) {
        if (bar) bar.remove();
        if (following && S.pending[t.key]) { delete S.pending[t.key]; pendingChanged = true; }
        continue;
      }
      if (!S.pending[t.key]) { S.pending[t.key] = Date.now(); pendingChanged = true; }
      if (!bar) {
        bar = document.createElement("div");
        bar.className = "xfb-bar xfb-bar-todo";
        bar.innerHTML = `<span class="xfb-tag"></span><a class="xfb-open" target="_blank" rel="noopener">打开主页</a>`;
        bar.insertBefore(followButton(t.handle), bar.querySelector(".xfb-open"));
        const qs = document.createElement("span");
        qs.className = "xfb-q";
        bar.insertBefore(qs, bar.querySelector(".xfb-follow"));
        art.appendChild(bar);
        bar.addEventListener("click", (e) => e.stopPropagation());
      }
      setText(bar.querySelector(".xfb-tag"), rel === "0" ? "未关注" : "可能未关注");
      bar.querySelector(".xfb-open").href = "/" + t.handle;
      const sv = S.seen.get(t.key);
      const qEl = bar.querySelector(".xfb-q");
      if (qEl) fillQuality(qEl, quality(sv.fo, sv.fr));
      const fb = bar.querySelector(".xfb-follow");
      if (fb && fb.dataset.key !== t.key) fb.replaceWith(followButton(t.handle));
    }
    if (pendingChanged) saveLater("pending");
    if (followingChanged) saveLater("following");
    if (total >= 3 && unknown === total && !Object.keys(S.following).length) {
      setNote("这个页面读不到关注状态，名单里可能混有你已经关注的人，打开主页时会看到。", true, "page");
    } else if (S.noteKind === "page") setNote("");
    renderStatus();
  }

  function renderStatus() {
    let todo = 0, have = 0, skip = 0;
    const rows = [];
    for (const [key, v] of S.seen) {
      if (S.following[key]) { have++; continue; }
      if (S.onlyVerified && !v.verified) { skip++; continue; }
      todo++;
      rows.push(v);
    }
    /* 容易回关的排最前，大号难回的排最后 */
    const rank = (v) => { const q = quality(v.fo, v.fr); return !q ? 1 : q.cls === "good" ? 0 : q.cls === "low" ? 2 : q.cls === "big" ? 3 : 1; };
    rows.sort((a, b) => rank(a) - rank(b));
    S.rows = rows;
    setText(panel.querySelector("#xfb-rep"), S.seen.size);
    setText(panel.querySelector("#xfb-todo"), todo);
    setText(panel.querySelector("#xfb-have"), have);
    setText(panel.querySelector("#xfb-skip"), S.onlyVerified ? skip : "—");
    updateReplyLines();
    const curId = statusIdOf(location.pathname);
    if (curId && S.reachedEnd === curId && todo === 0 && S.seen.size && !S.handled[curId]) {
      markHandled(curId, true);
      setNote("这条帖子下面的人你都关注了，已自动标记为处理完。");
    }
    syncHandledButton();
    setText(panel.querySelector("#xfb-f-today"), followedToday());
    panel.querySelector("#xfb-f-today-line").classList.toggle("over", followedToday() >= FOLLOW_LIMIT);

    const list = panel.querySelector("#xfb-list");
    const sig = rows.map((r) => r.handle + ":" + (r.fo ?? "") + "/" + (r.fr ?? "")).join(",");
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.textContent = "";
    if (!rows.length) {
      const p = document.createElement("div");
      p.className = "xfb-empty";
      p.textContent = S.seen.size ? "这条帖子下面的人你都关注了。" : "还没读到回复，点「下滑到最后一条」。";
      list.appendChild(p);
      return;
    }
    for (const r of rows) {
      const row = document.createElement("div");
      row.className = "xfb-item";
      const who = document.createElement("span");
      who.className = "xfb-who";
      const nm = document.createElement("b");
      nm.textContent = r.name || r.handle;
      const hd = document.createElement("small");
      hd.textContent = "@" + r.handle;
      who.append(nm, hd);
      const q = quality(r.fo, r.fr);
      if (q) {
        const qs = document.createElement("small");
        fillQuality(qs, q);
        setText(qs, q.label || q.text);          /* 面板窄，只写结论，数字放在鼠标悬停提示里 */
        qs.title = `${q.text}。` + qs.title;
        who.appendChild(qs);
      }
      const a = document.createElement("a");
      a.className = "xfb-open";
      a.href = "/" + r.handle;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "主页";
      const btns = document.createElement("span");
      btns.className = "xfb-item-btns";
      btns.append(followButton(r.handle), a);
      row.append(who, btns);
      list.appendChild(row);
    }
  }

  function clickMoreReplies() {
    for (const el of document.querySelectorAll(`${SEL.column} [role="button"], ${SEL.column} button`)) {
      if (el.closest("#xfb-panel, .xfb-bar")) continue;
      const txt = (el.textContent || "").trim();
      if (MORE_REPLIES_RE.test(txt)) { el.click(); return true; }
    }
    return false;
  }

  /* ========== 模式四：个人主页，记录手动关注 ========== */
  function scanProfile(info) {
    const col = document.querySelector(SEL.column);
    if (!col || info.own) { panel.hidden = true; return; }
    const unBtn = col.querySelector(SEL.unfollowBtn);
    const fBtn = col.querySelector(SEL.followBtn);
    const state = unBtn ? "following" : fBtn ? "not" : null;
    if (!state) { panel.hidden = !S.pending[info.key]; return; }

    let changed = false;
    if (state === "following" && !S.following[info.key]) {
      S.following[info.key] = info.handle;
      changed = true;
      if (S.profileState === "not") {           // 就在这个页面上点了关注
        S.followLog[today()] = followedToday() + 1;
        store.set({ followLog: S.followLog });
        recordFollowTime(info.key);
        try { chrome.runtime.sendMessage({ type: "xfb-round-follow" }); } catch (e) { /* 忽略 */ }
        if (S.pool[info.key]) { delete S.pool[info.key]; store.set({ pool: S.pool }); }
        setNote(`已关注 @${info.handle}，回到帖子页它会从名单里划掉。`);
      }
    }
    if (state === "not" && S.following[info.key]) { delete S.following[info.key]; changed = true; }
    if (state === "following" && S.pending[info.key]) { delete S.pending[info.key]; saveLater("pending", 200); }
    if (changed) saveLater("following", 200);
    S.profileState = state;

    panel.hidden = !S.pending[info.key] && !(state === "following" && S.noteKind === "msg");
    setText(panel.querySelector("#xfb-p-today"), followedToday());
    panel.querySelector("#xfb-p-today-line").classList.toggle("over", followedToday() >= FOLLOW_LIMIT);
    if (state === "not" && followedToday() >= FOLLOW_LIMIT) {
      setNote(`今天已经关注 ${followedToday()} 个人了。短时间关注太多容易被 X 限制，建议明天再继续。`, true, "page");
    }
  }

  /* ---------- 自动下滑 ---------- */
  async function runAutoScroll() {
    let idle = 0, last = S.seen.size;
    while (S.autoScroll && idle < 5) {
      if (S.mode === "status" && clickMoreReplies()) { await sleep(2200); scan(); idle = 0; continue; }
      window.scrollBy({ top: Math.round(window.innerHeight * 0.8), behavior: "smooth" });
      await sleep(1800);
      scan();
      if (S.seen.size === last) idle++; else { idle = 0; last = S.seen.size; }
    }
    if (S.autoScroll) {
      S.autoScroll = false;
      syncScrollButtons();
      if (S.mode === "following" && pageInfo().own) {
        S.followingSyncedAt = Date.now();
        store.set({ followingSyncedAt: S.followingSyncedAt, following: S.following });
        setNote(`已经到底了，共扫描 ${S.seen.size} 个账号，并记住了你关注的人。`);
        renderFollowingStats();
      } else if (S.mode === "status") {
        S.reachedEnd = statusIdOf(location.pathname);
        setNote("已经到最后一条回复了。把名单里的人关注完，这条会自动标记为处理完；也可以直接点「这条处理完了」。");
        if (panel) panel.querySelector("#xfb-list").dataset.sig = "-";
        scan();
      } else {
        setNote("已经到底了。");
      }
    }
  }

  /* ---------- 总调度 ---------- */
  const PROFILE_PAGE_RE = /^\/([A-Za-z0-9_]{1,15})(\/(with_replies|highlights|media|articles|superfollows))?\/?$/;
  /* ================= 屏蔽词：带这些词的帖子折叠成一行，点「显示」可以展开 =================
   * X 自带的「静音字词」中文常常匹配不上（按空格分词），这里直接在帖子文字里找。
   * 看的是帖子文字、引用的帖子文字、发帖人名字。你点开的那条帖子本身不折叠。 */
  const blockList = () => String(S.blockWords || "").split(/[,，、]/).map((x) => x.trim().toLowerCase().replace(/\s+/g, "")).filter(Boolean);
  function blockHit(art) {
    const words = blockList();
    if (!words.length) return "";
    let txt = "";
    for (const el of art.querySelectorAll(`${SEL.tweetText}, ${SEL.userName}`)) txt += " " + el.textContent;
    txt = txt.toLowerCase().replace(/\s+/g, "");
    return words.find((w) => txt.includes(w)) || "";
  }
  function blockArticles(force) {
    const focal = (location.pathname.match(/\/status\/(\d+)/) || [])[1];
    for (const art of document.querySelectorAll(`${SEL.column} ${SEL.tweet}`)) {
      /* X 会复用帖子元素换成别的帖子，所以按「哪条帖子」记，不是按元素 */
      const tl = art.querySelector("a[href*='/status/'] time");
      const sig = (tl && tl.closest("a").getAttribute("href")) || (art.textContent || "").slice(0, 60);
      if (!force && art.dataset.xfbBlockChecked === sig) continue;
      if (art.dataset.xfbBlockChecked !== sig) art.dataset.xfbShow = "";
      art.dataset.xfbBlockChecked = sig;
      let hit = blockHit(art);
      if (hit && focal && statusIdOf(sig) === focal) hit = "";
      const bar = art.previousElementSibling && art.previousElementSibling.classList.contains("xfb-blockbar") ? art.previousElementSibling : null;
      if (!hit || art.dataset.xfbShow === "1") {
        art.classList.remove("xfb-blocked");
        if (bar && !hit) bar.remove();
        continue;
      }
      art.classList.add("xfb-blocked");
      if (!bar) {
        const b = document.createElement("div");
        b.className = "xfb-blockbar";
        const sp = document.createElement("span");
        sp.textContent = `已隐藏一条帖子（含屏蔽词「${hit}」）`;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = "显示";
        btn.addEventListener("click", () => {
          const show = art.dataset.xfbShow !== "1";
          art.dataset.xfbShow = show ? "1" : "";
          art.classList.toggle("xfb-blocked", !show);
          btn.textContent = show ? "收起" : "显示";
        });
        b.append(sp, btn);
        art.parentNode.insertBefore(b, art);
      } else {
        setText(bar.querySelector("span"), `已隐藏一条帖子（含屏蔽词「${hit}」）`);
        setText(bar.querySelector("button"), "显示");
      }
    }
  }

  function scan() {
    blockArticles(false);
    const info = pageInfo();
    const pm = location.pathname.match(PROFILE_PAGE_RE);
    if (pm && !RESERVED.has(keyOf(pm[1]))) heatPosts();
    if (info.mode !== S.mode) {
      S.mode = info.mode;
      S.profileState = null;
      if (panel) setNote("");
    }
    if (!showMode(info.mode)) return;
    if (info.mode === "following") scanFollowing(info);
    else if (info.mode === "feed") scanFeed();
    else if (info.mode === "status") scanStatus();
    else if (info.mode === "profile") scanProfile(info);
  }

  let timer = null;
  const schedule = () => { clearTimeout(timer); timer = setTimeout(scan, 300); };

  (async () => {
    const saved = await store.get();
    S.kept = saved.kept || {};
    S.log = saved.log || {};
    S.done = saved.done || {};
    S.following = saved.following || {};
    S.followingSyncedAt = saved.followingSyncedAt || 0;
    S.followLog = saved.followLog || {};
    S.pending = saved.pending || {};
    S.keywords = saved.keywords || DEFAULT_KEYWORDS;
    S.onlyVerified = saved.onlyVerified !== false;
    if (typeof saved.minReplies === "number") S.minReplies = saved.minReplies;
    if (typeof saved.remindOn === "boolean") S.remindOn = saved.remindOn;
    if (saved.remindMin) S.remindMin = saved.remindMin;
    if (typeof saved.heatAll === "boolean") S.heatAll = saved.heatAll;
    S.replied = saved.replied || {};
    S.replyLog = saved.replyLog || {};
    S.handled = saved.handled || {};
    S.pool = saved.pool || {};
    S.unpool = saved.unpool || {};
    S.followedAt = saved.followedAt || {};
    if (typeof saved.graceDays === "number") S.graceDays = saved.graceDays;
    if (typeof saved.autoJump === "boolean") S.autoJump = saved.autoJump;
    if (saved.replyGoal) S.replyGoal = saved.replyGoal;
    S.blockWords = saved.blockWords || "";

    /* 别的标签页（比如个人主页）关注了人，这里马上跟着更新 */
    try {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== "local") return;
        let touched = false;
        for (const k of ["following", "followLog", "pending", "kept", "done", "log", "replied", "replyLog", "handled", "followedAt", "pool", "unpool"]) {
          if (changes[k]) { S[k] = changes[k].newValue || {}; touched = true; }
        }
        if (changes.blockWords) { S.blockWords = changes.blockWords.newValue || ""; blockArticles(true); }
        if (changes.pool) renderPool();
        if (changes.unpool || changes.log || changes.kept) renderUnpool();
        if (touched) schedule();
      });
    } catch (e) { /* 没有扩展环境时忽略 */ }

    /* 只对 X 自己的页面变化重新扫描，插件自己改的地方不算 */
    const ours = (n) => {
      const el = n && (n.nodeType === 1 ? n : n.parentElement);
      return !!(el && el.closest && el.closest("#xfb-panel, .xfb-bar, .xfb-corner, .xfb-blockbar"));
    };
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (ours(m.target)) continue;
        const nodes = [...m.addedNodes, ...m.removedNodes];
        if (nodes.length && nodes.every((n) => n.nodeType === 1 && (n.id === "xfb-panel" || (n.classList?.contains("xfb-bar") || n.classList?.contains("xfb-corner"))))) continue;
        schedule();
        return;
      }
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-xfb-rel", "data-xfb-rel-for"] });

    setInterval(() => {
      if (location.href !== S.lastUrl) {
        S.lastUrl = location.href;
        S.seen.clear();
        S.reachedEnd = null;
        S.autoScroll = false;
        syncScrollButtons();
        if (panel) panel.querySelector("#xfb-list").dataset.sig = "-";
        schedule();
      }
    }, 500);
    scan();
  })();
})();
