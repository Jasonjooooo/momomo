// 弹窗与页面脚本共用的默认设置
const XRF_DEFAULTS = {
  feedTabs: "为你推荐,正在关注,新闻", // 首页标签顺序：当前标签没有新帖子就换下一个
  maxPosts: 30,          // 本次最多回复多少条
  minGap: 5,             // 两条之间最少等待（秒）
  maxGap: 8,             // 两条之间最多等待（秒）
  confirmBeforeSend: false, // 发送前停下来等你确认
  pick: "first",         // 有多条候选回复时：first=第一条，random=随机
  txtOpen: "AI回复",      // 面板没打开时，点击这个文字打开「AI 回复助手」
  txtRegen: "生成回复,换一批", // 多个用逗号分隔，哪个出现点哪个
  txtFill: "填入回复框",
  failWords: "没返回能用的回复,再点一次试试", // 出现这些字 = 生成失败
  maxRetries: 3,         // 生成失败后自动重试次数（每次等 4 秒）
  loadingWords: "生成中,正在生成,加载中,思考中,Loading",
};
