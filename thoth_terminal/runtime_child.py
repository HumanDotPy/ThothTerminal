from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from thoth_terminal.config import ensure_data_dirs, settings
from thoth_terminal.runtime_env import configure_logging
from thoth_terminal.runtime_info import write_runtime_state


def _configure_child_logging(service: str) -> None:
    ensure_data_dirs()
    configure_logging(settings.log_level)
    logs_dir = Path(settings.runtime_state_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / f"{service}.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(settings.log_level or "INFO").upper(), logging.INFO))


def _run_worker_main() -> None:
    from thoth_terminal.worker import run_worker

    run_worker()


def _run_worker_cloud() -> None:
    from thoth_terminal.worker import run_cloud_worker

    run_cloud_worker()


def _run_local_agent_api(*, host: str, port: int) -> None:
    import uvicorn

    settings.agent_api_enabled = True
    from thoth_terminal.web.local_agent_app import app

    app.state.database_prepared_by_desktop = True
    uvicorn.run(
        app,
        host=str(host),
        port=int(port),
        log_level=str(settings.log_level or "info").lower(),
        log_config=None,
        access_log=False,
        lifespan="on",
        loop="asyncio",
        http="h11",
    )


def _run_web(*, host: str, port: int) -> None:
    import uvicorn

    from thoth_terminal.web.app import app

    app.state.database_prepared_by_desktop = True
    uvicorn.run(
        app,
        host=str(host),
        port=int(port),
        log_level=str(settings.log_level or "info").lower(),
        log_config=None,
        access_log=False,
        lifespan="on",
        loop="asyncio",
        http="h11",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Internal isolated Thoth runtime child.")
    parser.add_argument("service", choices=("web", "worker-main", "worker-cloud", "local-agent-api"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    _configure_child_logging(str(args.service))
    state_service = {
        "worker-main": "worker",
        "worker-cloud": "cloud-worker",
        "local-agent-api": "local-agent-api",
        "web": "web",
    }[str(args.service)]
    write_runtime_state(
        service=state_service,
        data={"supervised_child": True, "runtime_child_service": str(args.service)},
    )
    if args.service == "web":
        _run_web(host=str(args.host), port=int(args.port))
        return
    if args.service == "worker-main":
        _run_worker_main()
        return
    if args.service == "worker-cloud":
        _run_worker_cloud()
        return
    _run_local_agent_api(host=str(args.host), port=int(args.port))


if __name__ == "__main__":
    main()
