// NewsNow API 抓取（移植自 trendradar/crawler/fetcher.py）

/** 校验链接为 HTTPS 且域名匹配（含子域名）。返回 null 表示安全，否则返回异常描述。 */
export function checkDomainSafety(items, expectedDomain) {
  const expected = (expectedDomain || "").trim().toLowerCase();
  if (!expected) return null;
  for (const item of items) {
    for (const field of ["url", "mobileUrl"]) {
      const raw = item[field];
      if (!raw) continue;
      let u;
      try {
        u = new URL(raw);
      } catch {
        return `${raw}（格式异常）`;
      }
      if (u.protocol !== "https:") return `${raw}（非 HTTPS）`;
      const host = u.hostname.toLowerCase();
      if (host !== expected && !host.endsWith("." + expected)) return `${host}（来自 ${raw}）`;
    }
  }
  return null;
}

async function fetchJson(url, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: ctrl.signal,
      cache: "no-store",
      headers: { Accept: "application/json, text/plain, */*" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 抓取单个平台，失败自动重试。返回 { items, cached } 或抛出错误。 */
export async function fetchPlatform(apiUrl, platform, { domainCheck = true, maxRetries = 2 } = {}) {
  const url = `${apiUrl}?id=${encodeURIComponent(platform.id)}&latest`;
  let lastErr;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const data = await fetchJson(url, 10000);
      if (!["success", "cache"].includes(data.status)) {
        throw new Error(`响应状态异常: ${data.status ?? "未知"}`);
      }
      const items = (data.items || [])
        .filter((it) => it && it.title != null && String(it.title).trim())
        .map((it, i) => ({
          title: String(it.title).trim(),
          url: it.url || "",
          mobileUrl: it.mobileUrl || "",
          rank: i + 1,
          extra: it.extra?.info || it.extra?.hover || "",
        }));
      if (domainCheck) {
        const bad = checkDomainSafety(items, platform.expectedDomain);
        if (bad) throw new Error(`未通过域名安全校验：${bad}`);
      }
      return { items, cached: data.status === "cache" };
    } catch (e) {
      lastErr = e;
      if (String(e.message).startsWith("未通过域名安全校验")) break; // 不重试
      if (attempt < maxRetries) await sleep(1000 + attempt * 1000);
    }
  }
  throw lastErr;
}
