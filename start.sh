#!/usr/bin/env bash
# 启动 VideoTube Videogen(WSL2 / Linux)。
#
#   ./start.sh              # 装好依赖并启动，默认 8020
#   ./start.sh --check      # 只自检不启动
#   ./start.sh --port 8030
#
# 脚本是幂等的:虚拟环境、依赖和 config.yaml 都是缺什么补什么，
# 已经就位就直接启动。改过 pyproject.toml 后会自动重装依赖。

set -euo pipefail
cd "$(dirname "$0")"

VENV="${VIDEOGEN_VENV:-.venv}"
PYTHON="$VENV/bin/python"
STAMP="$VENV/.deps-stamp"
CONFIG="config.yaml"
FORWARD=()
REINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG="$2"; shift 2 ;;
        --reinstall) REINSTALL=1; shift ;;
        *) FORWARD+=("$1"); shift ;;
    esac
done

if [[ ! -x "$PYTHON" ]]; then
    echo "==> 创建虚拟环境 $VENV"
    if command -v python3.11 >/dev/null 2>&1; then
        python3.11 -m venv "$VENV"
    else
        python3 -m venv "$VENV"
    fi
fi

# pyproject 比上次安装新，就说明依赖表变了(比如新增的 yt-dlp)。
if [[ "$REINSTALL" == 1 || ! -f "$STAMP" || pyproject.toml -nt "$STAMP" ]]; then
    echo "==> 安装依赖"
    "$PYTHON" -m pip install --upgrade pip --quiet
    "$PYTHON" -m pip install -e ".[dev]"
    touch "$STAMP"
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "==> 没有 $CONFIG，从 config.example.yaml 复制一份"
    cp config.example.yaml "$CONFIG"
fi

exec "$PYTHON" -m videogen_service.cli --config "$CONFIG" ${FORWARD[@]+"${FORWARD[@]}"}
