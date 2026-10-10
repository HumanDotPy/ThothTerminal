"""ArtCraft tool and UI contracts, shared by Thoth's existing MCP transports."""
from pathlib import Path

ARTCRAFT_UI_URI = "ui://widget/thoth-artcraft-workbench-v1.html"
ARTCRAFT_TOOLS = frozenset({"artcraft_status", "artcraft_catalog", "artcraft_workflow", "get_artcraft_workbench"})
_APPS = ["photocraft", "vectorcraft", "filmcraft", "lightcraft", "pdfcraft", "effectcraft", "designcraft"]


def artcraft_tool_definitions():
    app = {"type": "string", "enum": _APPS}
    return [
        {"name": "get_artcraft_workbench", "title": "ArtCraft workspace",
         "description": "Get the creative workspace beside a conversation: available local apps, editable-document workflows and the latest preview returned in this conversation.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "artcraft_status", "title": "Available ArtCraft apps",
         "description": "Get selected local ArtCraft apps and versions without launching them. These tools work on the Thoth desktop host.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "artcraft_catalog", "title": "Discover ArtCraft commands",
         "description": "Search one app's native tool catalog or get a named tool's input schema. Inspect schemas before authoring or editing an ArtCraft document. Load only commands needed for the task.",
         "inputSchema": {"type": "object", "properties": {"app": app,
             "query": {"type": "string", "maxLength": 200}, "tool": {"type": "string", "maxLength": 100},
             "limit": {"type": "integer", "minimum": 1, "maximum": 40}},
             "required": ["app"], "additionalProperties": False}},
        {"name": "artcraft_workflow", "title": "Edit an ArtCraft document",
         "description": "Create, inspect, edit, undo, save or render through one local ArtCraft engine session. Inspect native schemas first. Each call starts a fresh headless session: reopen the saved native document for revisions and explicitly save before ending. Files belong to one named workspace; paths are relative to it. Stops on the first error, retains earlier edits/files and reports partial progress. Execution is not atomic or retry-idempotent. Text scripts and live UI control are unsupported.",
         "inputSchema": {"type": "object", "properties": {"app": app,
             "workspace": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
             "steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "object",
                 "properties": {"tool": {"type": "string", "maxLength": 100}, "arguments": {"type": "object"}},
                 "required": ["tool", "arguments"], "additionalProperties": False}}},
             "required": ["app", "workspace", "steps"], "additionalProperties": False}},
    ]


def artcraft_resource():
    return {"uri": ARTCRAFT_UI_URI, "name": "ArtCraft workspace",
            "description": "Local creative tools, editable-document workflows and visual results.",
            "mimeType": "text/html;profile=mcp-app"}


def artcraft_resource_contents():
    return {"contents": [{**artcraft_resource(),
        "text": (Path(__file__).parent / "artcraft_workbench.html").read_text(encoding="utf-8"),
        "_meta": {"ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}},
                  "openai/widgetDescription": "ArtCraft workspace with local app capabilities and the most recent creative preview."}}]}


def artcraft_call(name, arguments):
    from thoth_terminal.artcraft_service import artcraft_catalog, artcraft_status, artcraft_workflow
    if name in {"artcraft_status", "get_artcraft_workbench"}:
        return artcraft_status()
    if name == "artcraft_catalog":
        return artcraft_catalog(str(arguments["app"]), query=str(arguments.get("query") or ""),
                                tool=arguments.get("tool"), limit=int(arguments.get("limit") or 20))
    return artcraft_workflow(str(arguments["app"]), str(arguments["workspace"]), arguments["steps"])
