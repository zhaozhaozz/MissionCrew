#!/bin/bash
# MissionCrew 服务管理入口(pm2)。
# 服务按常规继承调用方环境,尤其是完整 PATH——它要靠 PATH 探测本机装了
# 哪些 Agent CLI,曾经在这里重建最小 PATH 会让装在非标准目录的工具(如
# ~/.kimi-code/bin/kimi)被误判为未安装。宿主终端注入的变量改由派发层
# base.host_isolated_environ() 剥离,不再依赖启动方式。
set -eu
cd "$(dirname "$0")/.."

case "${1:-status}" in
  start)
    pm2 start ecosystem.config.cjs --update-env
    ;;
  restart)
    # --update-env 让服务用当前环境重启,PATH 变化(新装 CLI)才会被看见
    pm2 restart ecosystem.config.cjs --update-env
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
