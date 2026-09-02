// pm2 部署配置。请通过 scripts/serve.sh 调用(restart 带 --update-env,新装的
// Agent CLI 才会被检测到)。服务按常规继承调用方环境;宿主终端注入的变量
// (VSCODE_*/SSH_AUTH_SOCK 等)由派发层 runtime/base.py 在启动 Agent 子进程前剥离。
// 默认只监听 127.0.0.1,局域网访问在启动时设置 MISSIONCREW_HOST=0.0.0.0。
const path = require("path");

module.exports = {
  apps: [
    {
      name: "missioncrew",
      cwd: __dirname,
      env: {
        // 平台默认数据目录是 ~/.missioncrew;pm2 部署沿用仓库内的 .missioncrew
        // (既有实例的库、文档库与 Agent 工作区都在这里),已导出的变量优先。
        MISSIONCREW_HOME:
          process.env.MISSIONCREW_HOME || path.join(__dirname, ".missioncrew"),
      },
      script: ".venv/bin/python",
      args: "-m missioncrew.cli serve",
      interpreter: "none",
      autorestart: true,
      watch: false,
      // 优雅退出:lifespan 关停会逐个终止常驻 CLI 及其后台任务,给足时间
      kill_timeout: 30000,
      // 端口被旧实例占用时的崩溃重试间隔
      restart_delay: 2000,
      out_file: ".missioncrew/server.log",
      error_file: ".missioncrew/server.log",
    },
  ],
};
