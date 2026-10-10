from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from thoth_terminal.migrations import init_db
from thoth_terminal.runtime_env import configure_logging
from thoth_terminal.config import settings
from thoth_terminal.web.local_agent_routes import register_local_agent_routes


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    if not bool(getattr(app.state, "database_prepared_by_desktop", False)):
        init_db(allow_locked=True)
    yield


app = FastAPI(title="Thoth Local Analyst Agent API", version="0.1.0", lifespan=_lifespan)
register_local_agent_routes(app)
