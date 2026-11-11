// 默认设置与读写封装（全部存 chrome.storage.local：关键词文件可能超过 sync 的 8KB 单项限制）

export const DEFAULT_API_URL = "https://newsnow.busiyi.world/api/s";

// 与 config/config.yaml 中 platforms.sources 保持一致
export const DEFAULT_PLATFORMS = [
  { id: "toutiao", name: "今日头条", expectedDomain: "toutiao.com", enabled: true },
  { id: "baidu", name: "百度热搜", expectedDomain: "baidu.com", enabled: true },
  { id: "wallstreetcn-hot", name: "华尔街见闻", expectedDomain: "wallstreetcn.com", enabled: true },
  { id: "thepaper", name: "澎湃新闻", expectedDomain: "thepaper.cn", enabled: true },
  { id: "bilibili-hot-search", name: "bilibili 热搜", expectedDomain: "bilibili.com", enabled: true },
  { id: "cls-hot", name: "财联社热门", expectedDomain: "cls.cn", enabled: true },
  { id: "ifeng", name: "凤凰网", expectedDomain: "ifeng.com", enabled: true },
  { id: "tieba", name: "贴吧", expectedDomain: "baidu.com", enabled: true },
  { id: "weibo", name: "微博", expectedDomain: "weibo.com", enabled: true },
  { id: "douyin", name: "抖音", expectedDomain: "douyin.com", enabled: true },
  { id: "zhihu", name: "知乎", expectedDomain: "zhihu.com", enabled: true },
];

export const DEFAULT_KEYWORDS = `# 语法与 TrendRadar 的 config/frequency_words.txt 相同，可直接粘贴整个文件
# 词组之间用空行分隔；普通词任一命中即可，+必须词，!过滤词，@最多条数，/正则/
# 留空 = 显示全部新闻

[人工智能]
/AI|人工智能|大模型|ChatGPT|Claude|DeepSeek/
@10

[航空]
/C919|航空|航班|机场/
!广告
@10
`;

export const DEFAULT_SETTINGS = {
  apiUrl: DEFAULT_API_URL,
  platforms: DEFAULT_PLATFORMS,
  keywordsText: DEFAULT_KEYWORDS,
  refreshMinutes: 30,
  notifyEnabled: true,
  domainCheck: true,
};

export async function getSettings() {
  const stored = await chrome.storage.local.get(DEFAULT_SETTINGS);
  return { ...DEFAULT_SETTINGS, ...stored };
}

export async function saveSettings(patch) {
  await chrome.storage.local.set(patch);
}

export async function getCache() {
  const { cache } = await chrome.storage.local.get({ cache: null });
  return cache;
}

export async function setCache(cache) {
  await chrome.storage.local.set({ cache });
}
