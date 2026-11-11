# TrendRadar 热点雷达 · Chrome 插件

在浏览器工具栏里直接查看多平台热榜，并用 TrendRadar 的关键词规则筛选；命中新热点时显示角标并弹出桌面通知。

## 功能

- **多平台热榜**：今日头条、百度、微博、知乎、抖音、B 站、贴吧、澎湃、凤凰、财联社、华尔街见闻（与 `config/config.yaml` 默认平台一致），数据来自 [newsnow](https://github.com/ourongxing/newsnow) API，可在设置中增删平台或换成自己部署的实例。
- **关键词筛选**：完全兼容 `config/frequency_words.txt` 语法（`[组名]`、`+必须词`、`!过滤词`、`@条数`、`/正则/`、`=> 显示名`、`[GLOBAL_FILTER]`），可直接导入该文件。
- **「关键词命中」汇总页**：按词组分组、跨平台去重，遵守每组 `@` 条数上限。
- **后台定时刷新**：默认每 30 分钟一次（最少 5 分钟），新命中的热点会显示在图标角标上并发送桌面通知，点击通知打开原文。
- **域名安全校验**：与 Python 端一致，链接必须为 HTTPS 且域名与平台匹配，否则丢弃该平台数据。
- 标题搜索、只看命中、深色模式。

## 安装（开发者模式）

1. 打开 `chrome://extensions`
2. 打开右上角「开发者模式」
3. 点击「加载已解压的扩展程序」，选择本目录 `chrome-extension/`
4. 把 TrendRadar 图标固定到工具栏，点击即可查看

## 使用说明

- 点击弹窗右上角 ⚙ 进入设置：配置 API 地址、平台、关键词、刷新间隔和通知。
- 关键词留空时显示全部新闻，且不发送通知。
- 修改关键词后会重新建立「已读」基线，避免一次性推送大量旧新闻。
- 填写非默认的 API 地址时，Chrome 会弹出请求访问该站点的权限，允许后才会保存。

## 目录结构

```
chrome-extension/
├── manifest.json        # Manifest V3 配置
├── background.js        # 后台：定时抓取、匹配、角标、通知
├── popup.html/css/js    # 工具栏弹窗
├── options.html/css/js  # 设置页
├── lib/
│   ├── api.js           # NewsNow 抓取 + 域名校验（移植自 trendradar/crawler/fetcher.py）
│   ├── frequency.js     # 关键词规则解析与匹配（移植自 trendradar/core/frequency.py）
│   └── settings.js      # 默认设置与存储
└── icons/
```

## 注意

- 正则使用 JavaScript 语法，与 Python 绝大多数写法通用（如 `\b`、`|`、`(?:...)`），个别 Python 专有语法无法识别时会在控制台警告并按普通文本匹配。
- 所有设置与缓存只保存在本机浏览器（`chrome.storage.local`）。
