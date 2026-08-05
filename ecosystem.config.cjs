// pm2 部署配置。请通过 scripts/serve.sh 调用,以保证服务在最小环境变量下启动,
// 避免把终端环境(VSCODE_*/SSH_AUTH_SOCK 等)传染给 Agent 子进程。
module.exports = {
  apps: [
    {
      name: "missioncrew",
      cwd: __dirname,
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
