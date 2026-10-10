from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the ThothTerminal desktop shell.")
    parser.add_argument("--runtime-child", choices=("web", "worker-main", "worker-cloud", "local-agent-api"), help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    parser.add_argument("--hidden", action="store_true", help="Start minimized to the system tray.")
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="Launch the web shell without the background worker loop.",
    )
    args = parser.parse_args()

    if args.runtime_child:
        from thoth_terminal.runtime_child import main as runtime_child_main

        runtime_child_main([str(args.runtime_child), "--host", str(args.host), "--port", str(args.port)])
        return

    from thoth_terminal.desktop.app import launch_desktop_app

    launch_desktop_app(start_hidden=bool(args.hidden), start_worker=not bool(args.no_worker))
