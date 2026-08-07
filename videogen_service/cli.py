import argparse
from pathlib import Path
from typing import Sequence

import uvicorn

from videogen_service.api import create_app
from videogen_service.config import load_config


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the VideoTube render service")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args(argv)
    config = load_config(arguments.config)
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=arguments.port or config.port,
    )


if __name__ == "__main__":
    main()
