/* 回关助手 · 页面数据读取（运行在 X 页面自己的环境里）
 * X 的每条帖子背后带着作者资料，其中包括「你是否关注了他」。
 * 这里只读不改：读到后写在帖子元素的 data-xfb-rel 属性上（1 = 已关注，0 = 未关注），
 * 由插件主程序去用。读不到就不写，主程序会退回到已记住的关注列表。
 *
 * 另外负责「一键关注」：你点一次按钮，这里就用你自己的登录状态向 X 发一次关注请求，
 * 和你在对方主页点「关注」发出去的是同一个请求。只在你点击时发，一次一个人。
 */
(() => {
  "use strict";
  if (window.__xfbMainLoaded) return;
  window.__xfbMainLoaded = true;

  const HANDLE_RE = /^\/([A-Za-z0-9_]{1,15})$/;
  const MAX_UP = 25;        // 往上找多少层组件
  const MAX_DEPTH = 9;      // 每份数据往里找多深
  const MAX_NODES = 6000;   // 每条帖子最多看多少个对象，防止卡顿

  function fiberOf(el) {
    for (const k in el) if (k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")) return el[k];
    return null;
  }

  function handleOf(art) {
    const un = art.querySelector('[data-testid="User-Name"]');
    if (!un) return null;
    for (const a of un.querySelectorAll("a[href]")) {
      const m = a.getAttribute("href").match(HANDLE_RE);
      if (m) return m[1].toLowerCase();
    }
    return null;
  }

  const flagOn = (o) => {
    if (!o || typeof o !== "object") return null;
    if (typeof o.following === "boolean") return o.following;
    if (o.relationship_perspectives && typeof o.relationship_perspectives.following === "boolean") return o.relationship_perspectives.following;
    if (o.legacy && typeof o.legacy.following === "boolean") return o.legacy.following;
    return null;
  };

  const num = (v) => (typeof v === "number" && v >= 0 ? v : null);
  const countsOn = (o) => {
    if (!o || typeof o !== "object") return null;
    const src = [o, o.legacy, o.relationship_counts, o.core].filter((x) => x && typeof x === "object");
    for (const c of src) {
      const fo = num(c.followers_count), fr = num(c.friends_count);
      if (fo !== null && fr !== null) return { fo, fr };
    }
    return null;
  };

  /* 在一份数据里找 screen_name 等于 handle 的用户，再在它附近找 following 标记和粉丝数/关注数 */
  function search(root, handle, budget, out) {
    const seen = new WeakSet();
    const stack = [];
    let result = null;
    (function walk(o, depth) {
      if (result !== null || budget.n <= 0 || !o || typeof o !== "object" || depth > MAX_DEPTH || seen.has(o)) return;
      if (o instanceof Node || o === window) return;
      seen.add(o);
      budget.n--;
      if (typeof o.screen_name === "string" && o.screen_name.toLowerCase() === handle) {
        const near = [o, ...stack.slice(-4).reverse()];
        if (out && !out.counts) {
          for (const c of near) { const k = countsOn(c); if (k) { out.counts = k; break; } }
        }
        for (const c of near) {
          const f = flagOn(c);
          if (f !== null) { result = f; return; }
        }
        result = "found";
        return;
      }
      stack.push(o);
      for (const k in o) {
        if (k === "children" || k === "_owner" || k.startsWith("__react")) continue;
        let v;
        try { v = o[k]; } catch (e) { continue; }
        if (v && typeof v === "object") walk(v, depth + 1);
        if (result !== null) break;
      }
      stack.pop();
    })(root, 0);
    return result;
  }

  /* 返回 { rel: true/false/null, counts: {fo, fr} 或 null } */
  function relationOf(art, handle) {
    let f = fiberOf(art);
    const budget = { n: MAX_NODES };
    const out = { rel: null, counts: null };
    for (let i = 0; f && i < MAX_UP && budget.n > 0; i++, f = f.return) {
      const props = f.memoizedProps;
      if (props && typeof props === "object") {
        const r = search(props, handle, budget, out);
        if (r === true || r === false) { out.rel = r; return out; }
      }
    }
    return out;
  }

  function tag() {
    for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
      const h = handleOf(art);
      if (!h) continue;
      const same = art.getAttribute("data-xfb-rel-for") === h;
      if (same && art.hasAttribute("data-xfb-rel")) continue;
      const tries = same ? Number(art.getAttribute("data-xfb-try") || 0) : 0;
      if (tries >= 3) continue;                     // 读了 3 次都没读到，就不再浪费时间
      let res = { rel: null, counts: null };
      try { res = relationOf(art, h); } catch (e) { /* 读不到就算了 */ }
      const rel = res.rel;
      art.setAttribute("data-xfb-try", String(tries + 1));
      if (res.counts) {
        art.setAttribute("data-xfb-fo", String(res.counts.fo));
        art.setAttribute("data-xfb-fr", String(res.counts.fr));
      } else if (!same) {
        art.removeAttribute("data-xfb-fo");
        art.removeAttribute("data-xfb-fr");
      }
      art.setAttribute("data-xfb-rel-for", h);
      if (rel === true) art.setAttribute("data-xfb-rel", "1");
      else if (rel === false) art.setAttribute("data-xfb-rel", "0");
      else art.removeAttribute("data-xfb-rel");
    }
  }

  /* ---------- 一键关注 ---------- */
  const WEB_BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
  const cookie = (n) => {
    const m = document.cookie.match(new RegExp("(?:^|; )" + n + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  };
  window.addEventListener("message", async (e) => {
    if (e.source !== window || !e.data || e.data.__xfb !== "follow-req") return;
    const { id, handle } = e.data;
    const out = { __xfb: "follow-res", id, ok: false, status: 0, code: 0 };
    if (/^[A-Za-z0-9_]{1,15}$/.test(String(handle))) {
      try {
        const res = await fetch("/i/api/1.1/friendships/create.json", {
          method: "POST",
          credentials: "include",
          headers: {
            authorization: "Bearer " + WEB_BEARER,
            "x-csrf-token": cookie("ct0"),
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "content-type": "application/x-www-form-urlencoded",
          },
          body: "include_profile_interstitial_type=1&skip_status=1&screen_name=" + encodeURIComponent(handle),
        });
        out.status = res.status;
        const j = await res.json().catch(() => null);
        out.code = (j && j.errors && j.errors[0] && j.errors[0].code) || 0;
        out.ok = res.ok && !!(j && (j.id_str || j.screen_name));
      } catch (err) { /* 网络问题，ok 保持 false */ }
    }
    window.postMessage(out, location.origin);
  });

  /* ---------- 取关池：先查一次关系，对方已经回关就不取关；你一次点击只取关一个人 ---------- */
  const apiHeaders = () => ({
    authorization: "Bearer " + WEB_BEARER,
    "x-csrf-token": cookie("ct0"),
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes",
  });
  window.addEventListener("message", async (e) => {
    if (e.source !== window || !e.data || e.data.__xfb !== "unfollow-req") return;
    const { id, handle, me } = e.data;
    const out = { __xfb: "unfollow-res", id, ok: false, status: 0, code: 0, followsYou: false, notFollowing: false, checked: false };
    const okName = (h) => /^[A-Za-z0-9_]{1,15}$/.test(String(h));
    if (okName(handle) && okName(me)) {
      try {
        const r1 = await fetch(`/i/api/1.1/friendships/show.json?source_screen_name=${encodeURIComponent(me)}&target_screen_name=${encodeURIComponent(handle)}`,
          { credentials: "include", headers: apiHeaders() });
        out.status = r1.status;
        const j1 = await r1.json().catch(() => null);
        const rel = j1 && j1.relationship;
        if (rel && rel.source && rel.target) {
          out.checked = true;
          out.followsYou = !!rel.source.followed_by;
          out.notFollowing = !rel.source.following;
        } else {
          out.code = (j1 && j1.errors && j1.errors[0] && j1.errors[0].code) || 0;
        }
        if (out.checked && !out.followsYou && !out.notFollowing) {
          const r2 = await fetch("/i/api/1.1/friendships/destroy.json", {
            method: "POST", credentials: "include",
            headers: { ...apiHeaders(), "content-type": "application/x-www-form-urlencoded" },
            body: "include_profile_interstitial_type=1&skip_status=1&screen_name=" + encodeURIComponent(handle),
          });
          out.status = r2.status;
          const j2 = await r2.json().catch(() => null);
          out.code = (j2 && j2.errors && j2.errors[0] && j2.errors[0].code) || 0;
          out.ok = r2.ok && !!(j2 && (j2.id_str || j2.screen_name));
        }
      } catch (err) { /* 网络问题，ok 保持 false */ }
    }
    window.postMessage(out, location.origin);
  });

  /* ---------- 定时窗口的下拉框：直接调用 X 自己的「选择变化」处理函数 ---------- */
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || e.data.__xfb !== "select-set") return;
    const { id, value, index } = e.data;
    let ok = false;
    try {
      const sel = document.querySelector(`select[data-xfb-sel="${id}"]`);
      if (sel) {
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
        setter.call(sel, value);
        sel.selectedIndex = index;
        const pk = Object.keys(sel).find((k) => k.startsWith("__reactProps$"));
        const props = pk ? sel[pk] : null;
        const fake = {
          target: sel, currentTarget: sel, type: "change", nativeEvent: new Event("change"),
          bubbles: true, isTrusted: true, defaultPrevented: false,
          preventDefault() {}, stopPropagation() {}, persist() {}, isDefaultPrevented: () => false, isPropagationStopped: () => false,
        };
        if (props && typeof props.onChange === "function") { props.onChange(fake); ok = true; }
        if (props && typeof props.onValueChange === "function") { props.onValueChange(value, index); ok = true; }
        if (!ok) {
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          ok = true;
        }
      }
    } catch (err) { ok = false; }
    window.postMessage({ __xfb: "select-set-res", id, ok }, location.origin);
  });

  let timer = null;
  new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "childList" && m.addedNodes.length) { clearTimeout(timer); timer = setTimeout(tag, 250); return; }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(tag, 500);
})();
