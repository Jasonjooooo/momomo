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
  };

  let cfg = { ...XRF_DEFAULTS };
  const run = { active: false, paused: false, stopReq: false, done: 0, failed: 0, step: "空闲" };

  class Stopped extends Error {}
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);
  const norm = (s) => (s || "").replace(/[\s​-‍﻿]+/g, "");

  // ---------- 状态与日志（弹窗通过 storage 实时显示） ----------
  function publish() {
    chrome.storage.local.set({
      xrfStatus: { active: run.active, paused: run.paused, step: run.step, done: run.done, failed: run.failed, max: cfg.maxPosts, at: Date.now() },
    });
    overlay.update();
  }
  function setStep(s) {
    run.step = s;
    publish();
  }
  async function log(text, level = "info") {
    const { xrfLog = [] } = await chrome.storage.local.get("xrfLog");
    xrfLog.push({ t: Date.now(), text, level });
    await chrome.storage.local.set({ xrfLog: xrfLog.slice(-200) });
  }

  // ---------- 等待工具（支持暂停/停止） ----------
  async function checkpoint() {
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
  function articleInfo(art) {
    const time = art.querySelector('a[href*="/status/"] time');
    const link = time?.closest("a");
    const id = link?.getAttribute("href")?.match(/\/status\/(\d+)/)?.[1];
    return id ? { id, link } : null; // 广告没有时间链接，会被自然跳过
  }
  const replyBoxText = () => norm(document.querySelector(SEL.replyBox)?.textContent);

  async function goHome() {
    if (!(isHome() && document.querySelector(SEL.article))) {
      const link = document.querySelector(SEL.homeLink);
      if (!link) throw new Error("找不到左侧「主页」入口");
      click(link);
      await waitFor(() => isHome() && document.querySelector(SEL.article), 20000, "首页加载");
      await wait(1500);
    }
    // 确保在「为你推荐」标签
    const forYouTab = () => [...document.querySelectorAll('[role="tab"]')].find((el) => /^(为你推荐|For you)$/i.test(norm(el.textContent)));
    const tab = forYouTab();
    if (tab && tab.getAttribute("aria-selected") !== "true") {
      setStep("切换到「为你推荐」");
      click(tab);
      await waitFor(() => forYouTab()?.getAttribute("aria-selected") === "true" && document.querySelector(SEL.article), 15000, "切换到为你推荐");
      window.scrollTo({ top: 0 });
      await wait(1500);
    }
  }

  /** 首页里找下一条没处理过的帖子，找不到就往下滑 */
  async function findNextArticle(processed) {
    for (let tries = 0; tries < 25; tries++) {
      await checkpoint();
      for (const art of document.querySelectorAll(SEL.article)) {
        if (art.getBoundingClientRect().bottom <= 80) continue; // 已经滑过去的不要
        const info = articleInfo(art);
        if (info && !processed.has(info.id)) return { art, ...info };
      }
      setStep("向下滑动寻找下一条帖子…");
      window.scrollBy({ top: window.innerHeight * 0.8, behavior: "smooth" });
      await wait(rand(1200, 2000));
    }
    throw new Error("连续下滑也没找到新的帖子");
  }

  // ---------- AI 回复助手 ----------
  /** 候选回复区域的“指纹”：用来判断换一批之后内容有没有变 */
  function fillSignature(fills) {
    return fills
      .map((b) => {
        let c = b;
        const own = norm(b.textContent).length;
        for (let i = 0; i < 5 && c.parentElement; i++) {
          c = c.parentElement;
          if (norm(c.textContent).length > own + 4) break;
        }
        return norm(c.textContent);
      })
      .join("|");
  }
  const isLoading = () =>
    cfg.loadingWords.split(/[,，]/).map((w) => w.trim()).filter(Boolean).some((w) => findAll(w).length);

  async function waitForGenerated(beforeSig) {
    let lastSig = null;
    let stableSince = 0;
    return waitFor(
      () => {
        const fills = findAll(cfg.txtFill).filter((b) => !isDisabled(b));
        if (!fills.length || isLoading()) {
          lastSig = null;
          return null;
        }
        const sig = fillSignature(fills);
        if (beforeSig && sig === beforeSig) return null; // 还是旧内容
        if (sig !== lastSig) {
          lastSig = sig;
          stableSince = Date.now();
          return null;
        }
        if (Date.now() - stableSince < 1500) return null; // 内容稳定 1.5 秒才算生成完（兼容逐字输出）
        return cfg.pick === "random" ? fills[Math.floor(Math.random() * fills.length)] : fills[0];
      },
      90000,
      "AI 回复生成完成"
    );
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
    const beforeSig = fillSignature(findAll(cfg.txtFill)) || null;
    click(regen);

    // 2. 等生成完成
    setStep("等待 AI 生成回复…");
    await wait(800);
    const fill = await waitForGenerated(beforeSig);
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
    const { xrfCfg = {}, xrfProcessed = [] } = await chrome.storage.local.get(["xrfCfg", "xrfProcessed"]);
    cfg = { ...XRF_DEFAULTS, ...xrfCfg };
    const processed = new Set(xrfProcessed);
    const saveProcessed = () => chrome.storage.local.set({ xrfProcessed: [...processed].slice(-5000) });
    Object.assign(run, { active: true, paused: false, stopReq: false, done: 0, failed: 0 });
    overlay.show();
    await log(`开始：最多 ${cfg.maxPosts} 条，间隔 ${cfg.minGap}–${cfg.maxGap} 秒`);

    try {
      // 起点：在帖子页且没处理过 → 先处理当前帖子；否则回到首页
      const sid = statusIdFromPath();
      if (!(sid && !processed.has(sid))) await goHome();

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
      if (e instanceof Stopped) {
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
        root.getElementById("ctl").hidden = !run.active;
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
        chrome.storage.local.get("xrfCfg").then(({ xrfCfg = {} }) => {
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
  chrome.storage.local.get("xrfStatus").then(({ xrfStatus }) => {
    if (xrfStatus?.active) chrome.storage.local.set({ xrfStatus: { ...xrfStatus, active: false, paused: false, step: "页面已刷新，任务中断" } });
  });
})();
