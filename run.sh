#!/usr/bin/env bash
# 一条本地命令：重置并生成离线 / 错位 / 调店样例，然后启动平台
set -e
cd "$(dirname "$0")"
exec python3 -m app.server --reset "$@"
