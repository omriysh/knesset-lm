"""
Public /docs, /redoc and /openapi.json for the web app and the standalone API.

Swagger UI and ReDoc are served from pinned local copies under /docs-assets (swagger-ui-dist 5.33.0,
redoc 2.5.4 standalone bundle, downloaded from cdn.jsdelivr.net/npm), so no third-party
script runs on the origin that keeps the visitor's Gemini key. The OpenAPI schema is enriched
after generation: every operation gets a tag, and /v1 tool routes take their description and
parameter docs from the public view of the research tool registry (routes.public_tool_schema).
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from api.routes import TOOL_ENDPOINTS, public_tool_schema

DOCS_ASSETS_DIR = Path(__file__).parent / "docs_assets"
DOCS_ASSETS_URL = "/docs-assets"
PUBLIC_API_PATH_PREFIX = "/v1/"

API_VERSION = "1.0"
SITE_DESCRIPTION = (
    "Read-only access to Israeli Knesset data: committee protocols (AI summaries, verbatim opinions, "
    "full transcripts), MKs, committees, parties, bills and plenum votes.\n\n"
    "**AI agents:** start with [/llms.txt](/llms.txt) for usage rules, search tips and recipes; "
    "[/v1/tools](/v1/tools) lists the tools with their JSON schemas. The same tools are served over MCP "
    f"(Streamable HTTP, stateless) at `{config.MCP_PATH}` and at "
    f"{', '.join(f'`https://{host}`' for host in config.MCP_SUBDOMAIN_HOSTS)}.\n\n"
    "All `/v1` routes are GET, need no key, and are rate-limited per IP "
    f"({config.API_RATE_LIMIT_UPSTREAM_PER_MINUTE}/min for routes that call the Knesset APIs, "
    f"{config.API_RATE_LIMIT_DB_PER_MINUTE}/min for protocol search). Add `format=md` for compact markdown."
)

TAG_PUBLIC_TOOLS = "Public API: tools"
TAG_PUBLIC_META = "Public API: discovery"
TAG_DESCRIPTIONS = {
    TAG_PUBLIC_TOOLS: "One route per research tool; the same tools the site's research agent uses.",
    TAG_PUBLIC_META: "Tool listing, data coverage and health.",
}

API_PARAM_TO_TOOL_ARG = {"q": "query", "committee": "committees", "meeting_id": "meeting_ids"}
API_PARAM_DESCRIPTIONS = {
    "format": "`json` (default) or `md`: markdown, more compact for an LLM context.",
    "knesset_num": "Knesset number; see the tool's `knesset_num` argument in /v1/tools for its default.",
    "offset": "Paging position: characters for protocols and bill text, rows for bills and votes; "
              "copy it from the response's `next`.",
    "search_in": "Scopes to search: `topics`, `opinions`, `speeches` (repeat or comma-separate; "
                 f"default: {', '.join(config.API_PROTOCOLS_DEFAULT_SCOPES)}).",
    "meeting_id": "Meeting id(s), as returned in protocol rows (repeat or comma-separate).",
    "sort": "Ranking order, e.g. `date` for newest first (default: relevance, or newest first when q is empty).",
}


def tool_specs_by_name() -> dict:
    return {spec.name: spec for spec in RESEARCH_TOOL_REGISTRY}


def first_sentence(text: str, max_chars: int = 90) -> str:
    sentence = text.replace("\n", " ").split(". ")[0].split(":")[0].strip().rstrip(".")
    return sentence if len(sentence) <= max_chars else sentence[:max_chars - 1].rstrip() + "…"


def tag_for_path(path: str) -> str:
    if path in TOOL_ENDPOINTS.values():
        return TAG_PUBLIC_TOOLS
    return TAG_PUBLIC_META


def tool_parameter_description(tool_name: str, api_param: str, tool_properties: dict) -> str:
    tool_arg = api_param if api_param in tool_properties else API_PARAM_TO_TOOL_ARG.get(api_param, api_param)
    registry_description = (tool_properties.get(tool_arg) or {}).get("description", "")
    description = registry_description or API_PARAM_DESCRIPTIONS.get(api_param, "")
    if not description:
        description = f"See the `{tool_arg}` argument of the `{tool_name}` tool in /v1/tools."
    return description


def document_tool_operation(operation: dict, tool_name: str, tool_spec) -> None:
    public_schema = public_tool_schema(tool_spec)
    tool_description = public_schema["description"]
    operation["summary"] = first_sentence(tool_description)
    operation["description"] = (f"{tool_description}\n\n"
                                f"Research-agent tool: `{tool_name}`. Errors return "
                                "`{error_code, message, tool, args, hint}` with a 4xx/5xx status.")
    tool_properties = public_schema.get("properties", {})
    for parameter in operation.get("parameters", []):
        if not parameter.get("description"):
            parameter["description"] = tool_parameter_description(tool_name, parameter["name"], tool_properties)


def enrich_openapi_schema(schema: dict) -> dict:
    specs = tool_specs_by_name()
    tool_name_by_path = {path: tool for tool, path in TOOL_ENDPOINTS.items()}
    used_tags = set()
    for path, operations in schema.get("paths", {}).items():
        for operation in operations.values():
            tag = tag_for_path(path)
            operation["tags"] = [tag]
            used_tags.add(tag)
            tool_name = tool_name_by_path.get(path)
            if tool_name in specs:
                document_tool_operation(operation, tool_name, specs[tool_name])
            for parameter in operation.get("parameters", []):
                if not parameter.get("description") and parameter["name"] in API_PARAM_DESCRIPTIONS:
                    parameter["description"] = API_PARAM_DESCRIPTIONS[parameter["name"]]
    schema["tags"] = [{"name": tag, "description": text} for tag, text in TAG_DESCRIPTIONS.items() if tag in used_tags]
    return schema


def install_public_docs(app: FastAPI, title: str) -> None:
    """The app must be created with docs_url=None, redoc_url=None."""

    def cached_enriched_openapi() -> dict:
        if app.openapi_schema is None:
            app.openapi_schema = enrich_openapi_schema(get_openapi(
                title=title, version=API_VERSION, description=SITE_DESCRIPTION,
                routes=[route for route in app.routes if getattr(route, "path", "").startswith(PUBLIC_API_PATH_PREFIX)]))
        return app.openapi_schema

    app.openapi = cached_enriched_openapi
    app.mount(DOCS_ASSETS_URL, StaticFiles(directory=str(DOCS_ASSETS_DIR)), name="docs-assets")

    @app.get("/docs", include_in_schema=False)
    def swagger_ui():
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title=f"{title} — Swagger UI",
            swagger_js_url=f"{DOCS_ASSETS_URL}/swagger-ui-bundle.js",
            swagger_css_url=f"{DOCS_ASSETS_URL}/swagger-ui.css",
            swagger_favicon_url=f"{DOCS_ASSETS_URL}/favicon.svg",
            oauth2_redirect_url="/docs/oauth2-redirect",
            swagger_ui_parameters={"validatorUrl": None})

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    def swagger_ui_oauth2_redirect():
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    def redoc():
        return get_redoc_html(
            openapi_url="/openapi.json", title=f"{title} — ReDoc",
            redoc_js_url=f"{DOCS_ASSETS_URL}/redoc.standalone.js",
            redoc_favicon_url=f"{DOCS_ASSETS_URL}/favicon.svg", with_google_fonts=False)
