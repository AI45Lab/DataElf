"""Repository and console-script entry point for the HTTP service."""
from __future__ import annotations
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="DataElf Server: asynchronous Insight API")
    parser.add_argument("--config", help="Current DataElf YAML/JSON configuration file")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--state-dir", help="Override server state directory")
    parser.add_argument("--max-concurrent-jobs", type=int, choices=range(1, 6),
                        help="Concurrent task limit (1-5; default 5, configurable)")
    args = parser.parse_args()
    if args.config:
        os.environ["DATAELF_CONFIG_FILE"] = args.config
    for key in ("host", "port", "state_dir", "max_concurrent_jobs"):
        value = getattr(args, key)
        if value is not None:
            os.environ[f"DATAELF_SERVER_{key.upper()}"] = str(value)
    try:
        import uvicorn
        from dataelf_server.app import create_app
    except ModuleNotFoundError as exc:
        if exc.name in {"uvicorn", "fastapi"}:
            parser.error('Install project dependencies with: uv sync (or python -m pip install -e .)')
        raise
    from dataelf_server.settings import Settings
    settings = Settings.from_env()
    uvicorn.run(create_app(settings=settings), host=settings.server.host, port=settings.server.port, workers=1)


if __name__ == "__main__":
    main()
