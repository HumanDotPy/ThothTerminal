"""Read the existing local-agent bridge health without delaying ingestion HTML."""
from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp

from thoth_terminal.config import settings


async def intake_bridge_status() -> dict[str, object]:
    port = int(getattr(settings, "agent_api_port", 8765))
    enabled = bool(getattr(settings, "agent_api_enabled", False))
    token_present = bool(getattr(settings, "agent_api_token", None)) or any(
        path.is_file() and path.stat().st_size > 0 for path in (
            Path(settings.runtime_state_dir) / "local-agent-api.token.txt",
            Path(__file__).resolve().parents[2] / "tmp-local-agent-api.token.txt",
        )
    )
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=1.5)) as client:
            async with client.get(f"http://127.0.0.1:{port}/api/local-agent/health", allow_redirects=False) as response:
                if response.status != 200:
                    return {"state": "unavailable", "message": "Bridge health is unavailable. Check runtime health."}
                import json
                raw = await response.content.read(8193)
                if len(raw) > 8192:
                    raise ValueError("Oversized health response")
                health = json.loads(raw)
                if health.get("ok") is not True or "token_configured" not in health:
                    raise ValueError("Unrecognized health response")
                if health.get("local_ingestion_enabled") is False:
                    return {"state": "disabled", "message": "Local models for ingestion are disabled. Enable Use local models for ingestion in Settings > Desktop Shell to resume."}
                if not health.get("token_configured"):
                    return {"state": "not_configured", "message": "Bridge credentials are not configured. Enable the local-agent bridge in Desktop settings."}
                if not health.get("enabled"):
                    return {"state": "disabled", "message": "The local-agent bridge is disabled."}
                return {"state": "ready", "message": "Local-agent bridge is listening and configured. This confirms the bridge, not a worker connection."}
    except aiohttp.ClientConnectorError:
        if not token_present:
            return {"state": "not_configured", "message": "Local-agent bridge has not been set up. Enable it in Desktop settings."}
        if not enabled:
            return {"state": "disabled", "message": "Local-agent bridge is configured but disabled in this runtime."}
        return {"state": "closed", "message": "Local-agent bridge is configured but closed. Check Desktop settings and runtime health."}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        return {"state": "unresponsive", "message": "Local-agent bridge did not return valid health in time. Check runtime health."}
