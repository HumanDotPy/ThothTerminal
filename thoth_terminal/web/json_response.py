"""orjson-backed response exposed under the historical JSONResponse name.

FastAPI's ORJSONResponse renders with OPT_NON_STR_KEYS | OPT_SERIALIZE_NUMPY,
so integer dictionary keys and numpy values serialize exactly as the stock
ORJSONResponse allows, while unsupported object types raise instead of being
silently stringified. Aliasing keeps every existing JSONResponse import site
and call site unchanged.
"""

from __future__ import annotations

from fastapi.responses import ORJSONResponse


JSONResponse = ORJSONResponse
