import argparse
from pathlib import Path
import sys
from typing import Sequence

import uvicorn

from videogen_service.api import create_app
from videogen_service.config import load_config
from videogen_service.preflight import format_checks, has_failure, run_checks


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the VideoTube render service")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查配置、workflow、ComfyUI 和 Ollama，然后退出",
    )
    arguments = parser.parse_args(argv)
    config = load_config(arguments.config)
    if arguments.port is not None:
        config = config.model_copy(update={"port": arguments.port})

    checks = run_checks(config)
    print(format_checks(checks))
    if has_failure(checks):
        # A broken workflow pointer or an unwritable work dir cannot be fixed by
        # starting anyway; a missing ComfyUI can, so only failures stop here.
        print("\n上面带 ✗ 的问题必须先解决，服务没有启动。", file=sys.stderr)
        raise SystemExit(1)
    if arguments.check:
        return

    print(f"\n生成台: http://{config.host}:{config.port}/  (Ctrl+C 停止)\n", flush=True)
    uvicorn.run(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
