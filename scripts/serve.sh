#!/bin/bash
# MissionCrew 服务管理入口(pm2)。
# start/restart 用 env -i 构造最小环境,防止调用方终端的环境变量
# (VSCODE_*、CLAUDE_*、SSH_AUTH_SOCK 等)被服务及其 spawn 的 Agent CLI 继承。
set -eu
cd "$(dirname "$0")/.."

NODE_BIN="$(dirname "$(command -v node)")"
CLEAN_PATH="$NODE_BIN:$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"

clean_env_pm2() {
  env -i \
    HOME="$HOME" \
    USER="${USER:-$(id -un)}" \
    LOGNAME="${USER:-$(id -un)}" \
    SHELL=/bin/bash \
    LANG="${LANG:-en_US.UTF-8}" \
    PATH="$CLEAN_PATH" \
    pm2 "$@"
}

case "${1:-status}" in
  start)
    clean_env_pm2 start ecosystem.config.cjs --update-env
    ;;
  restart)
    # --update-env 用当前(已清洗的)环境刷新应用环境
    clean_env_pm2 restart ecosystem.config.cjs --update-env
    ;;
  stop)
    pm2 stop missioncrew
    ;;
  delete)
    pm2 delete missioncrew
    ;;
  status)
    pm2 status missioncrew
    ;;
  logs)
    pm2 logs missioncrew --lines "${2:-50}"
    ;;
  *)
    echo "用法: scripts/serve.sh {start|restart|stop|delete|status|logs [行数]}" >&2
    exit 1
    ;;
esac
