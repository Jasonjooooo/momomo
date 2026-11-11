// TrendRadar 频率词规则解析与匹配（移植自 trendradar/core/frequency.py）
//
// 支持：[GLOBAL_FILTER] / [WORD_GROUPS] 区域、[组别名]、普通词、+必须词、
// !过滤词、@最大条数、/正则/、"词 => 显示名"。

function parseWord(raw) {
  let config = raw.trim();
  let displayName = null;
  if (config.includes("=>")) {
    const idx = config.indexOf("=>");
    const right = config.slice(idx + 2).trim();
    config = config.slice(0, idx).trim();
    if (right) displayName = right;
  }
  const m = config.match(/^\/(.+)\/[a-z]*$/);
  if (m) {
    try {
      return { word: m[1], pattern: new RegExp(m[1], "i"), displayName };
    } catch (e) {
      console.warn(`无效的正则 /${m[1]}/: ${e.message}`);
    }
  }
  return { word: config, pattern: null, displayName };
}

function wordMatches(w, titleLower) {
  if (w.pattern) return w.pattern.test(titleLower);
  return titleLower.includes(w.word.toLowerCase());
}

export function parseFrequencyWords(text) {
  const groups = [];
  const filterWords = [];
  const globalFilters = [];
  let section = "WORD_GROUPS";

  const blocks = (text || "")
    .replace(/\r\n/g, "\n")
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean);

  for (const block of blocks) {
    let lines = block
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("#"));
    if (!lines.length) continue;

    const isBracket = (l) => l.startsWith("[") && l.endsWith("]");
    if (isBracket(lines[0])) {
      const name = lines[0].slice(1, -1).toUpperCase();
      if (name === "GLOBAL_FILTER" || name === "WORD_GROUPS") {
        section = name;
        lines = lines.slice(1);
      }
    }

    if (section === "GLOBAL_FILTER") {
      for (const line of lines) {
        if (/^[!+@]/.test(line)) continue;
        globalFilters.push(parseWord(line));
      }
      continue;
    }

    let alias = null;
    if (lines.length && isBracket(lines[0])) {
      const a = lines[0].slice(1, -1).trim();
      if (!["GLOBAL_FILTER", "WORD_GROUPS"].includes(a.toUpperCase())) {
        alias = a;
        lines = lines.slice(1);
      }
    }

    const required = [];
    const normal = [];
    let maxCount = 0;
    for (const line of lines) {
      if (line.startsWith("@")) {
        const n = parseInt(line.slice(1), 10);
        if (n > 0) maxCount = n;
      } else if (line.startsWith("!")) {
        filterWords.push(parseWord(line.slice(1)));
      } else if (line.startsWith("+")) {
        required.push(parseWord(line.slice(1)));
      } else {
        normal.push(parseWord(line));
      }
    }

    if (required.length || normal.length) {
      const name =
        alias ||
        [...normal, ...required].map((w) => w.displayName || w.word).join(" / ");
      groups.push({ name, required, normal, maxCount });
    }
  }

  return { groups, filterWords, globalFilters };
}

/**
 * 返回标题命中的词组；未命中返回 null。
 * 没有配置任何词组时，未被过滤的标题返回 { name: null }（表示"全部"）。
 */
export function matchTitle(title, rules) {
  if (typeof title !== "string" || !title.trim()) return null;
  const t = title.toLowerCase();
  if (rules.globalFilters.some((w) => wordMatches(w, t))) return null;
  if (!rules.groups.length) return { name: null };
  if (rules.filterWords.some((w) => wordMatches(w, t))) return null;
  for (const g of rules.groups) {
    if (g.required.length && !g.required.every((w) => wordMatches(w, t))) continue;
    if (g.normal.length && !g.normal.some((w) => wordMatches(w, t))) continue;
    return g;
  }
  return null;
}
