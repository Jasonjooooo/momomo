// 弹窗与页面脚本共用的默认设置
const XRF_DEFAULTS = {
  maxPosts: 30,          // 本次最多回复多少条
  minGap: 5,             // 两条之间最少等待（秒）
  maxGap: 8,             // 两条之间最多等待（秒）
  confirmBeforeSend: false, // 发送前停下来等你确认
  pick: "first",         // 有多条候选回复时：first=第一条，random=随机
  txtOpen: "AI回复",      // 面板没打开时，点击这个文字打开「AI 回复助手」
  txtRegen: "换一批",
  txtFill: "填入回复框",
  loadingWords: "生成中,正在生成,加载中,思考中,Loading",
};
