#!/usr/bin/env bash
# 一条本地启动命令：重置数据库、载入部署/缺件/提前解除样例、启动监控与 Web 服务。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
SPEED="${SPEED:-2}"          # 天气时间轴倍速：约 10 秒升级、45 秒后减弱解除
ELAPSED="${ELAPSED:-20}"     # 从“雨势增强”阶段开始，样例预警不会被立即冲掉

exec python3 run.py --reset --host 0.0.0.0 --port "$PORT" --speed "$SPEED" --elapsed "$ELAPSED"
