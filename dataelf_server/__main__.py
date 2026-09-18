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
    parser.add_argument("--check-environment", action="store_true", help="Check local runtime before starting")
    parser.add_argument("--max-concurrent-jobs", type=int, choices=range(1, 6),
                        help="Concurrent task limit (1-5; default 5, configurable)")
    parser.add_argument("--max-pending-jobs", type=int,
                        help="Maximum queued jobs, excluding running jobs (positive; default 50)")
    args = parser.parse_args()
    if args.max_pending_jobs is not None and args.max_pending_jobs < 1:
        parser.error("--max-pending-jobs must be positive")
    if args.config:
        os.environ["DATAELF_CONFIG_FILE"] = args.config
    for key in ("host", "port", "state_dir", "max_concurrent_jobs", "max_pending_jobs"):
        value = getattr(args, key)
        if value is not None:
            os.environ[f"DATAELF_SERVER_{key.upper()}"] = str(value)
    if args.check_environment:
        from dataelf_server.deployment.preflight import check_python, report
        try:
            check_python()
        except Exception as exc:
            report(f"FAILED: {exc}")
            raise SystemExit(1) from None
    try:
        import uvicorn
        from dataelf_server.app import create_app
    except ModuleNotFoundError as exc:
        if exc.name in {"uvicorn", "fastapi"}:
            parser.error('Install project dependencies with: uv sync (or python -m pip install -e .)')
        raise
    from dataelf_server.settings import Settings
    if args.check_environment:
        from dataelf_server.deployment.preflight import check_runtime
        try:
            settings = Settings.from_env()
        except Exception as exc:
            report(f"FAILED: configuration could not be loaded ({type(exc).__name__}); check YAML and environment overrides")
            raise SystemExit(1) from None
        try:
            check_runtime(settings)
        except Exception as exc:
            from dataelf.discovery.redaction import redact_text, secret_values
            secrets = secret_values(settings.core.model_dump()) | secret_values(os.environ)
            report(f"FAILED: {redact_text(str(exc), secrets)}")
            raise SystemExit(1) from None
    else:
        settings = Settings.from_env()
    uvicorn.run(create_app(settings=settings), host=settings.server.host, port=settings.server.port, workers=1)


if __name__ == "__main__":
    main()
