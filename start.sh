#!/usr/bin/env bash
# 启动 VideoTube Videogen(WSL2 / Linux)。
#
#   ./start.sh              # 装好依赖并启动，默认 8020
#   ./start.sh --check      # 只自检不启动
#   ./start.sh --port 8030
#
# 脚本是幂等的:虚拟环境、依赖和 config.yaml 都是缺什么补什么，
# 已经就位就直接启动。改过 pyproject.toml 后会自动重装依赖。
#
# Windows 那边的 start-wsl.ps1 会把这个文件去掉 CR 之后管道喂给 bash，
# 所以这里不能依赖 $0 指向真实路径:它在那种情况下就是 "bash"。

set -euo pipefail

# 被管道执行时 $0 是 bash，dirname 会得到 "."；调用方已经 cd 到仓库里了。
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
    cd "$(dirname "${BASH_SOURCE[0]}")"
fi

if [[ ! -f pyproject.toml || ! -d videogen_service ]]; then
    echo "✗ 当前目录不是 videogen 仓库: $PWD" >&2
    exit 1
fi

# 在 WSL 里默认用 .wsl-venv:仓库通常放在 /mnt/d 上，跟 Windows 那边共用同一个
# 目录，两边都写 .venv 会互相覆盖成对方平台的解释器。
if [[ -z "${VIDEOGEN_VENV:-}" ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    VIDEOGEN_VENV=".wsl-venv"
fi
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

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            # 3.11 是本项目的下限。
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if [[ ! -x "$PYTHON" ]]; then
    if ! BASE_PYTHON="$(find_python)"; then
        echo "✗ 没找到 Python 3.11+。在 Ubuntu/Debian 上装一个:" >&2
        echo "    sudo apt update && sudo apt install -y python3 python3-venv" >&2
        exit 1
    fi
    echo "==> 创建虚拟环境 $VENV($BASE_PYTHON)"
    if ! "$BASE_PYTHON" -m venv "$VENV"; then
        # Ubuntu 把 venv 拆成了单独的包，这是 WSL 上最常见的一次失败。
        echo "✗ 创建虚拟环境失败。多半是缺 venv 模块，装上再来一次:" >&2
        echo "    sudo apt update && sudo apt install -y python3-venv python3-pip" >&2
        exit 1
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
