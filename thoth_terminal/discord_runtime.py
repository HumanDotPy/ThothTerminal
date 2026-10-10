from __future__ import annotations

import logging

from thoth_terminal.config import ensure_data_dirs, settings
from thoth_terminal.migrations import init_db
from thoth_terminal.runtime_env import configure_logging
from thoth_terminal.runtime_info import runtime_fingerprint


logger = logging.getLogger("thoth.discord_runtime")


def main() -> None:
    """Run the persistent Discord gateway without importing the monolithic CLI."""

    ensure_data_dirs()
    configure_logging(settings.log_level)
    init_db(allow_locked=True)
    from thoth_terminal.discord.bot import run_bot

    logger.info("discord dedicated runtime startup: %s", runtime_fingerprint())
    run_bot()


if __name__ == "__main__":
    main()
