// X 自动回复流程：运行在 x.com 页面里的脚本
// 流程：首页找下一条帖子 → 打开 → AI 回复助手「换一批」→ 等生成完 → 「填入回复框」
//      → 点黑色「回复」→ 等发送成功 → 返回首页 → 往下滑 → 下一条
(() => {
  if (window.__xrfLoaded) return;
  window.__xrfLoaded = true;

  // ---------- X 页面上的固定元素 ----------
  const SEL = {
    article: '[data-testid="primaryColumn"] article[data-testid="tweet"]',
    anyArticle: 'article[data-testid="tweet"]',
    replyBox: '[data-testid="tweetTextarea_0"]',
    replyBtn: '[data-testid="tweetButtonInline"]',
    toast: '[data-testid="toast"]',
    homeLink: 'a[data-testid="AppTabBar_Home_Link"]',
    profileLink: 'a[data-testid="AppTabBar_Profile_Link"]',
  };

  let cfg = { ...XRF_DEFAULTS };
  const run = { active: false, paused: false, stopReq: false, done: 0, failed: 0, step: "空闲", tabIdx: 0 };

  class Stopped extends Error {}
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);
  const norm = (s) => (s || "").replace(/[\s\u200b-\u200d\ufeff]+/g, "");

  // ---------- 插件更新/重新加载后，旧页面里的脚本会和插件断开 ----------
  let dead = false;
  const alive = () => {
    try {
      return !dead && !!chrome.runtime?.id && !!chrome.storage?.local;
    } catch {
      return false;
    }
  };
  function markDead() {
    if (dead) return;
    dead = true;
    run.stopReq = true;
    run.paused = false;
    run.step = "插件已更新或重新加载，请刷新 X 页面后再开始";
    overlay.update();
  }
  /** 安全地调用 chrome.storage：断开时不报错，直接停止任务 */
  async function store(fn) {
    if (!alive()) {
      markDead();
      return {};
    }
    try {
      return (await fn(chrome.storage.local)) || {};
    } catch (e) {
      if (!alive() || /context invalidated/i.test(String(e?.message))) {
        markDead();
        return {};
      }
      throw e;
    }
  }

  // ---------- 状态与日志（弹窗通过 storage 实时显示） ----------
  function publish() {
    store((st) =>
      st.set({
        xrfStatus: { active: run.active, paused: run.paused, step: run.step, done: run.done, failed: run.failed, max: cfg.maxPosts, at: Date.now() },
      })
    ).catch(() => {});
    overlay.update();
  }
  function setStep(s) {
    if (dead) return;
    run.step = s;
    publish();
  }
  async function log(text, level = "info") {
    const { xrfLog = [] } = await store((st) => st.get("xrfLog"));
    xrfLog.push({ t: Date.now(), text, level });
    await store((st) => st.set({ xrfLog: xrfLog.slice(-200) }));
  }

  // ---------- 等待工具（支持暂停/停止） ----------
  async function checkpoint() {
    if (!alive()) markDead();
    if (run.stopReq) throw new Stopped();
    while (run.paused) {
      await sleep(300);
      if (run.stopReq) throw new Stopped();
    }
  }
  async function wait(ms) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      await checkpoint();
      await sleep(Math.min(250, Math.max(0, end - Date.now())));
    }
  }
  async function waitFor(fn, timeout, label) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      await checkpoint();
      const v = fn();
      if (v) return v;
      await sleep(250);
    }
    throw new Error(`等待超时：${label}`);
  }

  // ---------- 按文字找按钮（会进入开放的 Shadow DOM） ----------
  /** 所有可搜索的根：document + 开放的 Shadow DOM（跳过本插件自己的浮窗） */
  function searchRoots(root = document, out = []) {
    out.push(root);
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot && !el.hasAttribute("data-xrf")) searchRoots(el.shadowRoot, out);
    }
    return out;
  }
  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
  }
  function isDisabled(el) {
    return !!(el.disabled || el.getAttribute("aria-disabled") === "true");
  }
  /** 找到所有文字为 text 的可点击元素（按文字节点查找，再向上找 button/role=button/a） */
  function findAll(text) {
    const t = norm(text);
    if (!t) return [];
    const out = new Set();
    for (const root of searchRoots()) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const s = norm(n.data);
        if (!s) continue;
        let el = n.parentElement;
        if (!el || el.closest("[data-xrf]")) continue;
        if (s.includes(t)) {
          if (s.length > t.length + 8) continue; // 排除长段落（如帖子正文里出现这几个字）
        } else if (t.includes(s)) {
          // 文字被拆成几段（如「AI<br>回复」）：向上找到整体文字刚好包含目标的元素
          while (el && norm(el.textContent).length < t.length) el = el.parentElement;
          const whole = el ? norm(el.textContent) : "";
          if (!whole.includes(t) || whole.length > t.length + 8) continue;
        } else continue;
        const target = el.closest('button,[role="button"],a') || el;
        if (isVisible(target)) out.add(target);
      }
    }
    return [...out];
  }
  const findOne = (text) => findAll(text)[0] || null;
  /** 「生成回复 / 换一批」按钮：第一次叫「生成回复」，生成过之后叫「换一批」，都认 */
  const regenWords = () => [...new Set([...cfg.txtRegen.split(/[,，/]/), ...XRF_DEFAULTS.txtRegen.split(",")].map((w) => w.trim()).filter(Boolean))];
  const findRegen = () => regenWords().map(findOne).find(Boolean) || null;
  const findAllRegen = () => regenWords().flatMap(findAll);

  function click(el) {
    el.scrollIntoView({ block: "center", inline: "nearest" });
    const r = el.getBoundingClientRect();
    const opts = { bubbles: true, cancelable: true, composed: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
    el.dispatchEvent(new PointerEvent("pointerdown", { ...opts, pointerType: "mouse" }));
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.dispatchEvent(new PointerEvent("pointerup", { ...opts, pointerType: "mouse" }));
    el.dispatchEvent(new MouseEvent("mouseup", opts));
    el.dispatchEvent(new MouseEvent("click", opts));
  }

  // ---------- X 页面辅助 ----------
  const statusIdFromPath = () => location.pathname.match(/^\/[^/]+\/status\/(\d+)/)?.[1] || null;
  const isHome = () => location.pathname === "/home";
  /** 当前登录账号（从左侧「个人资料」链接读取），用于跳过自己的帖子和回复 */
  const myHandle = () => (document.querySelector(SEL.profileLink)?.getAttribute("href") || "").replace(/^\//, "").toLowerCase();
  const authorFromPath = (path) => (path || "").match(/^\/([^/]+)\/status\//)?.[1]?.toLowerCase() || "";
  const isMine = (path) => !!myHandle() && authorFromPath(path) === myHandle();

  function articleInfo(art) {
    const time = art.querySelector('a[href*="/status/"] time');
    const link = time?.closest("a");
    const href = link?.getAttribute("href") || "";
    const id = href.match(/\/status\/(\d+)/)?.[1];
    return id ? { id, link, mine: isMine(href) } : null; // 广告没有时间链接，会被自然跳过
  }
  const replyBoxText = () => norm(document.querySelector(SEL.replyBox)?.textContent);

  // ---------- 首页标签：为你推荐 → 正在关注 → 新闻 … 轮流 ----------
  const TAB_ALIASES = { 为你推荐: ["ForYou"], 正在关注: ["关注", "Following"] };
  const feedTabs = () => {
    const list = (cfg.feedTabs || "").split(/[,，]/).map((w) => w.trim()).filter(Boolean);
    return list.length ? list : ["为你推荐"];
  };
  const currentTabName = () => feedTabs()[run.tabIdx % feedTabs().length];
  function findTab(name) {
    const names = [name, ...(TAB_ALIASES[name] || [])].map((n) => norm(n).toLowerCase());
    return [...document.querySelectorAll('[role="tab"]')].find((el) => names.includes(norm(el.textContent).toLowerCase())) || null;
  }

  async function goHome() {
    if (!(isHome() && document.querySelector(SEL.article))) {
      const link = document.querySelector(SEL.homeLink);
      if (!link) throw new Error("找不到左侧「主页」入口");
      click(link);
      await waitFor(() => isHome() && document.querySelector(SEL.article), 20000, "首页加载");
      await wait(1500);
    }
    // 确保在当前要处理的标签（默认「为你推荐」）
    const name = currentTabName();
    const tab = findTab(name);
    if (!tab) {
      await log(`⚠️ 首页上找不到「${name}」标签，留在当前标签`, "warn");
      return;
    }
    if (tab.getAttribute("aria-selected") !== "true") {
      setStep(`切换到「${name}」`);
      click(tab);
      await waitFor(() => findTab(name)?.getAttribute("aria-selected") === "true" && document.querySelector(SEL.article), 15000, `切换到「${name}」`);
      window.scrollTo({ top: 0 });
      await wait(1500);
    }
  }

  /** 列表顶部「显示 N 帖子」：有就点一下加载新帖 */
  async function loadNewPosts() {
    const btn = [...document.querySelectorAll('[data-testid="primaryColumn"] [role="button"], [data-testid="primaryColumn"] button')].find((b) =>
      /^(显示\d+(个|条)?帖子|Show\d+posts?)$/i.test(norm(b.textContent))
    );
    if (btn && isVisible(btn)) {
      click(btn);
      await wait(2000);
      return true;
    }
    return false;
  }

  /** 在当前标签里找下一条可回复的帖子（跳过自己的、处理过的、广告），找不到就往下滑 */
  async function scanCurrentTab(processed) {
    for (let tries = 0; tries < 12; tries++) {
      await checkpoint();
      for (const art of document.querySelectorAll(SEL.article)) {
        if (art.getBoundingClientRect().bottom <= 80) continue; // 已经滑过去的不要
        const info = articleInfo(art);
        if (info && !info.mine && !processed.has(info.id)) return { art, ...info };
      }
      setStep(`「${currentTabName()}」向下滑动寻找下一条帖子…`);
      window.scrollBy({ top: window.innerHeight * 0.8, behavior: "smooth" });
      await wait(rand(1200, 2000));
    }
    return null;
  }

  /** 当前标签没有新帖子了，就换下一个标签（为你推荐 → 正在关注 → 新闻 …），全部找过都没有才报错 */
  async function findNextArticle(processed) {
    const tabs = feedTabs();
    for (let n = 0; n <= tabs.length; n++) {
      const found = await scanCurrentTab(processed);
      if (found) return found;
      if (n === tabs.length) break;
      run.tabIdx = (run.tabIdx + 1) % tabs.length;
      await log(`「${tabs[(run.tabIdx + tabs.length - 1) % tabs.length]}」没有可回复的新帖子，切换到「${currentTabName()}」`);
      await goHome();
      window.scrollTo({ top: 0 });
      await wait(1000);
      await loadNewPosts();
    }
    throw new Error("所有标签里都没找到新的帖子");
  }

  // ---------- AI 回复助手 ----------
  /** 候选回复区域的“指纹”：用来判断换一批之后内容有没有变 */
  /** 读出某个「填入回复框」按钮对应的候选回复文字（优先读卡片里的输入框） */
  function candidateText(btn) {
    const fillText = norm(cfg.txtFill);
    let c = btn;
    for (let i = 0; i < 6 && c.parentElement; i++) {
      c = c.parentElement;
      const fillsInside = [...c.querySelectorAll('button,[role="button"]')].filter((b) => norm(b.textContent) === fillText).length;
      if (fillsInside > 1) break; // 已经越过这一张卡片
      const ta = [...c.querySelectorAll('textarea,[contenteditable="true"]')].find((el) => !el.closest(SEL.replyBox) && !el.matches(SEL.replyBox));
      if (ta) return (ta.value ?? ta.textContent ?? "").trim();
    }
    // 没有输入框：取按钮附近的文字
    c = btn;
    const own = norm(btn.textContent).length;
    for (let i = 0; i < 5 && c.parentElement; i++) {
      c = c.parentElement;
      if (norm(c.textContent).length > own + 4) break;
    }
    return (c.textContent || "").trim();
  }

  /** 候选回复区域的“指纹”：用来判断换一批之后内容有没有变 */
  const fillSignature = (fills) => fills.map(candidateText).join("|");

  /** 判断候选回复是不是乱码 / 不通顺；正常返回 null，否则返回原因 */
  function garbageReason(text) {
    const t = (text || "").trim();
    const n = norm(t);
    if (n.length < 2) return "内容为空";
    if (n.length > (Number(cfg.maxReplyLen) || 140)) return `太长（${n.length} 字）`;
    const bad = (cfg.badWords || "").split(/[,，]/).map((w) => w.trim()).filter(Boolean).find((w) => t.toLowerCase().includes(w.toLowerCase()));
    if (bad) return `包含「${bad}」`;
    if ((t.match(/[{}\[\]"\\<>`|=_#]/g) || []).length >= 4) return "包含代码符号";
    if (/\ufffd|Ã|â€|锟斤拷|烫烫/.test(t)) return "乱码字符";
    if (/(.)\1{9,}/u.test(n)) return "重复字符";
    return null;
  }
  const isLoading = () =>
    cfg.loadingWords.split(/[,，]/).map((w) => w.trim()).filter(Boolean).some((w) => findAll(w).length);

  /** 页面上是否出现了「AI 没返回能用的回复」之类的失败提示 */
  function hasFailText() {
    const words = cfg.failWords.split(/[,，]/).map(norm).filter(Boolean);
    if (!words.length) return false;
    for (const root of searchRoots()) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const s = norm(n.data);
        if (s && words.some((w) => s.includes(w)) && n.parentElement && isVisible(n.parentElement)) return true;
      }
    }
    return false;
  }

  /**
   * 点一次「生成回复 / 换一批」并等待结果。
   * 返回「填入回复框」按钮；生成失败（出现失败提示 / 长时间没反应）返回 null。
   */
  async function generateOnce(regen) {
    const beforeSig = fillSignature(findAll(cfg.txtFill)) || null;
    const failBefore = hasFailText();
    click(regen);
    const clickedAt = Date.now();
    let sawLoading = false;
    let lastSig = null;
    let stableSince = 0;
    await wait(800);
    const res = await waitFor(
      () => {
        const btn = findRegen();
        const loading = isLoading() || (btn && isDisabled(btn));
        if (loading) sawLoading = true;
        const fills = findAll(cfg.txtFill).filter((b) => !isDisabled(b));
        const fresh = fills.length && !(beforeSig && fillSignature(fills) === beforeSig);

        if (!loading && !fresh) {
          // 失败提示：生成过程结束后出现，或者原来没有、点完后新出现
          const fail = hasFailText();
          if (fail && (sawLoading || !failBefore || Date.now() - clickedAt > 6000)) return { fail: "AI 没返回能用的回复" };
          if (Date.now() - clickedAt > 30000) return { fail: "30 秒没有生成结果" };
        }
        if (!fresh || loading) {
          lastSig = null;
          return null;
        }
        const sig = fillSignature(fills);
        if (sig !== lastSig) {
          lastSig = sig;
          stableSince = Date.now();
          return null;
        }
        if (Date.now() - stableSince < 1500) return null; // 内容稳定 1.5 秒才算生成完（兼容逐字输出）
        // 检查每条候选：乱码 / 不通顺的不要
        const cands = fills.map((b, i) => ({ b, i, reason: garbageReason(candidateText(b)) }));
        const good = cands.filter((c) => !c.reason);
        if (!good.length) return { fail: `候选回复都不通顺（${cands[0].reason}）`, garbage: true };
        const chosen = cfg.pick === "random" ? good[Math.floor(Math.random() * good.length)] : good[0];
        const skipped = cands.filter((c) => c.reason && c.i < chosen.i);
        return { fill: chosen.b, note: skipped.length ? skipped.map((c) => `第 ${c.i + 1} 条${c.reason}`).join("，") + `，改用第 ${chosen.i + 1} 条` : "" };
      },
      90000,
      "AI 回复生成完成"
    ).catch((e) => {
      if (e instanceof Stopped) throw e;
      return { fail: e.message };
    });
    return res;
  }

  /**
   * 生成失败就等 4 秒再点一次：
   * - AI 没返回 / 超时：最多重试 cfg.maxRetries 次
   * - 候选回复都是乱码：一直点「换一批」直到通顺，最多 cfg.maxGarbageRetries 次
   */
  async function generateWithRetry(regen) {
    const maxFail = Math.max(0, Number(cfg.maxRetries) || 0);
    const maxGarbage = Math.max(0, Number(cfg.maxGarbageRetries) || 0);
    let fails = 0;
    let garbage = 0;
    for (let attempt = 1; ; attempt++) {
      setStep(attempt === 1 ? "等待 AI 生成回复…" : `等待 AI 生成回复…（第 ${attempt} 次）`);
      const res = await generateOnce(regen);
      if (res.fill) {
        if (res.note) await log(`ℹ️ ${res.note}`);
        return res.fill;
      }
      if (res.garbage) garbage++;
      else fails++;
      if (fails > maxFail) throw new Error(`重试 ${maxFail} 次仍未生成回复（${res.fail}）`);
      if (garbage > maxGarbage) throw new Error(`换了 ${maxGarbage} 批仍然没有通顺的回复`);
      await log(`↻ ${res.fail}，4 秒后${res.garbage ? "换一批" : "重新点击"}`, "warn");
      setStep(`${res.fail}，4 秒后${res.garbage ? "换一批" : "再点一次"}…`);
      await wait(4000);
      regen = await waitFor(() => {
        const b = findRegen();
        return b && !isDisabled(b) && b;
      }, 30000, "「生成回复 / 换一批」可点击");
    }
  }

  async function replyOnCurrentPost() {
    await waitFor(() => document.querySelector(SEL.anyArticle), 15000, "帖子加载");
    await wait(rand(1000, 1800));

    // 1. 找「换一批」，面板没开就先打开
    setStep("点击「生成回复 / 换一批」");
    let regen = findRegen();
    if (!regen) {
      const opener = findOne(cfg.txtOpen);
      if (opener) {
        click(opener);
        await wait(1000);
      }
      regen = await waitFor(findRegen, 10000, "找到「生成回复 / 换一批」按钮");
    }
    await waitFor(() => !isDisabled(regen), 30000, "「生成回复 / 换一批」可点击");

    // 2. 等生成完成（失败会等 4 秒自动再点）
    const fill = await generateWithRetry(regen);
    await wait(rand(600, 1200));

    // 3. 填入回复框
    setStep(`点击「${cfg.txtFill}」`);
    click(fill);
    await waitFor(() => replyBoxText().length > 0, 8000, "回复框出现内容");
    await wait(rand(600, 1200));

    // 4. 点黑色「回复」
    if (cfg.confirmBeforeSend) {
      setStep("等你确认：浮窗里点「发送」或「跳过」");
      const ok = await overlay.ask();
      if (!ok) throw new Error("你选择了跳过");
    }
    setStep("点击「回复」");
    const btn = await waitFor(() => {
      const b = document.querySelector(SEL.replyBtn);
      return b && !isDisabled(b) && b;
    }, 8000, "「回复」按钮可点击");
    click(btn);
    await waitFor(() => document.querySelector(SEL.toast) || replyBoxText() === "", 15000, "回复发送成功");
  }

  // ---------- 主循环 ----------
  async function start() {
    if (run.active) return;
    if (!alive()) return markDead();
    const { xrfCfg = {}, xrfProcessed = [] } = await store((st) => st.get(["xrfCfg", "xrfProcessed"]));
    cfg = { ...XRF_DEFAULTS, ...xrfCfg };
    const processed = new Set(xrfProcessed);
    const saveProcessed = () => store((st) => st.set({ xrfProcessed: [...processed].slice(-5000) })).catch(() => {});
    Object.assign(run, { active: true, paused: false, stopReq: false, done: 0, failed: 0 });
    overlay.show();
    await log(`开始：最多 ${cfg.maxPosts} 条，间隔 ${cfg.minGap}–${cfg.maxGap} 秒`);

    try {
      // 起点：在帖子页且没处理过 → 先处理当前帖子；否则回到首页
      run.tabIdx = 0;
      const sid = statusIdFromPath();
      if (!(sid && !processed.has(sid) && !isMine(location.pathname))) await goHome();

      while (run.done < cfg.maxPosts) {
        await checkpoint();
        let id = statusIdFromPath();
        if (!id) {
          const next = await findNextArticle(processed);
          id = next.id;
          setStep(`打开帖子 ${id}`);
          next.art.scrollIntoView({ block: "center", behavior: "smooth" });
          await wait(rand(600, 1200));
          click(next.link);
          await waitFor(() => statusIdFromPath() === id, 15000, "打开帖子");
        }
        processed.add(id);
        saveProcessed();

        try {
          await replyOnCurrentPost();
          run.done++;
          await log(`✅ 第 ${run.done} 条已回复：${location.href}`, "ok");
        } catch (e) {
          if (e instanceof Stopped) throw e;
          run.failed++;
          await log(`⚠️ 跳过 ${location.href}：${e.message}`, "warn");
        }
        publish();
        if (run.done >= cfg.maxPosts) break;

        const gap = rand(cfg.minGap, cfg.maxGap);
        setStep(`等待 ${gap.toFixed(1)} 秒…`);
        await wait(gap * 1000);

        setStep("返回上一页");
        history.back();
        try {
          await waitFor(() => isHome() && document.querySelector(SEL.article), 10000, "返回首页");
        } catch {
          // 返回不到首页时直接点「主页」
        }
        await goHome();
        await wait(rand(800, 1500));
      }
      setStep(`完成：成功 ${run.done} 条，跳过 ${run.failed} 条`);
      await log(`🎉 完成：成功 ${run.done} 条，跳过 ${run.failed} 条`, "ok");
    } catch (e) {
      if (e instanceof Stopped && dead) {
        // 插件已断开：浮窗里已经提示刷新页面
      } else if (e instanceof Stopped) {
        setStep(`已停止：成功 ${run.done} 条`);
        await log(`⏹ 已停止：成功 ${run.done} 条`);
      } else {
        setStep(`出错停止：${e.message}`);
        await log(`❌ 出错停止：${e.message}`, "error");
      }
    } finally {
      run.active = false;
      run.paused = false;
      publish();
    }
  }

  // ---------- 检测页面：看看各个按钮能不能找到 ----------
  function check() {
    const frames = [...document.querySelectorAll("iframe")].filter(isVisible);
    return {
      page: statusIdFromPath() ? "帖子页" : isHome() ? "首页" : location.pathname,
      me: myHandle(),
      tabs: feedTabs().map((n) => `${n}${findTab(n) ? "✅" : "❌"}`).join(" "),
      opener: findAll(cfg.txtOpen).length,
      regen: findAllRegen().map((b) => norm(b.textContent)).join("、"),
      fill: findAll(cfg.txtFill).length,
      replyBox: !!document.querySelector(SEL.replyBox),
      replyBtn: !!document.querySelector(SEL.replyBtn),
      extFrames: frames.filter((f) => (f.src || "").startsWith("chrome-extension://")).length,
    };
  }

  // ---------- 页面左下角的浮窗 ----------
  const overlay = (() => {
    let host, root, resolveAsk;
    function ensure() {
      if (host) return;
      host = document.createElement("div");
      host.setAttribute("data-xrf", "");
      host.style.cssText = "position:fixed;left:16px;bottom:16px;z-index:2147483647;";
      root = host.attachShadow({ mode: "open" });
      root.innerHTML = `
        <style>
          .box{font:13px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1419;color:#fff;border-radius:14px;padding:10px 12px;width:260px;box-shadow:0 6px 24px rgba(0,0,0,.35)}
          .hd{display:flex;justify-content:space-between;font-weight:700;margin-bottom:4px}
          .step{color:#c9ced3;word-break:break-all}
          .row{display:flex;gap:6px;margin-top:8px}
          button{flex:1;border:0;border-radius:999px;padding:5px 0;font:inherit;font-weight:600;cursor:pointer;background:#2f3336;color:#fff}
          button.pri{background:#1d9bf0} button.ok{background:#00ba7c}
          [hidden]{display:none!important}
        </style>
        <div class="box">
          <div class="hd"><span>X 自动回复流程</span><span id="cnt"></span></div>
          <div class="step" id="step"></div>
          <div class="row" id="ask" hidden><button class="ok" id="send">发送</button><button id="skip">跳过</button></div>
          <div class="row" id="ctl"><button class="pri" id="pause">暂停</button><button id="stop">停止</button></div>
        </div>`;
      root.getElementById("pause").onclick = () => {
        run.paused = !run.paused;
        publish();
      };
      root.getElementById("stop").onclick = () => {
        run.stopReq = true;
        run.paused = false;
        if (resolveAsk) resolveAsk(false);
      };
      root.getElementById("send").onclick = () => resolveAsk?.(true);
      root.getElementById("skip").onclick = () => resolveAsk?.(false);
      document.documentElement.append(host);
    }
    return {
      show() {
        ensure();
        host.hidden = false;
        this.update();
      },
      update() {
        if (!root) return;
        root.getElementById("cnt").textContent = `${run.done}/${cfg.maxPosts}`;
        root.getElementById("step").textContent = (run.paused ? "⏸ 已暂停 · " : "") + run.step;
        root.getElementById("pause").textContent = run.paused ? "继续" : "暂停";
        root.getElementById("ctl").hidden = !run.active || dead;
        if (dead) root.getElementById("step").textContent = "⚠️ " + run.step;
      },
      ask() {
        ensure();
        root.getElementById("ask").hidden = false;
        return new Promise((resolve) => {
          resolveAsk = (v) => {
            resolveAsk = null;
            root.getElementById("ask").hidden = true;
            resolve(v);
          };
        });
      },
    };
  })();

  // ---------- 接收弹窗指令 ----------
  chrome.runtime.onMessage.addListener((msg, _s, reply) => {
    switch (msg?.type) {
      case "start":
        start();
        reply({ ok: true });
        break;
      case "pause":
        run.paused = true;
        publish();
        reply({ ok: true });
        break;
      case "resume":
        run.paused = false;
        publish();
        reply({ ok: true });
        break;
      case "stop":
        run.stopReq = true;
        run.paused = false;
        reply({ ok: true });
        break;
      case "check":
        store((st) => st.get("xrfCfg")).then(({ xrfCfg = {} }) => {
          cfg = run.active ? cfg : { ...XRF_DEFAULTS, ...xrfCfg };
          reply({ ok: true, result: check(), running: run.active });
        });
        return true;
      case "ping":
        reply({ ok: true, running: run.active });
        break;
    }
    return false;
  });

  // 页面刷新后上一次的运行状态作废
  store((st) => st.get("xrfStatus")).then(({ xrfStatus }) => {
    if (xrfStatus?.active) store((st) => st.set({ xrfStatus: { ...xrfStatus, active: false, paused: false, step: "页面已刷新，任务中断" } }));
  });
})();
