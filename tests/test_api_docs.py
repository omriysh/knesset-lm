"""
tests/test_api_docs.py

/docs, /redoc and /openapi.json stay public on both apps, render from self-hosted pinned
Swagger UI / ReDoc bundles (no third-party script on the origin that holds the visitor's
Gemini key), and document every route: tags on every operation, and /v1 tool routes carry
the tool registry's description and per-parameter docs.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from api.routes import TOOL_ENDPOINTS

THIRD_PARTY_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost)[^\s\"')]+")
TOOL_SPECS = {spec.name: spec for spec in RESEARCH_TOOL_REGISTRY}


@pytest.fixture(params=["web", "api"])
def docs_client(request):
    if request.param == "web":
        import web.app as app_module
    else:
        import api.app as app_module
    app_module.app.openapi_schema = None
    return TestClient(app_module.app)


def openapi_operations(client):
    spec = client.get("/openapi.json").json()
    return spec, [(path, method, op) for path, ops in spec["paths"].items() for method, op in ops.items()]


class TestSelfHostedDocs:
    @pytest.mark.parametrize("page", ["/docs", "/redoc"])
    def test_docs_pages_load_only_local_assets(self, docs_client, page):
        response = docs_client.get(page)
        assert response.status_code == 200
        asset_urls = re.findall(r'(?:src|href)="([^"]+)"', response.text) + re.findall(r"url: '([^']+)'", response.text)
        assert asset_urls
        assert all(url.startswith("/") for url in asset_urls), asset_urls
        assert not THIRD_PARTY_URL.search(response.text.replace("https://fonts.googleapis.com", "")), \
            THIRD_PARTY_URL.findall(response.text)

    @pytest.mark.parametrize("asset", ["swagger-ui-bundle.js", "swagger-ui.css", "redoc.standalone.js", "favicon.svg"])
    def test_pinned_assets_are_served(self, docs_client, asset):
        response = docs_client.get(f"/docs-assets/{asset}")
        assert response.status_code == 200
        assert len(response.content) > 100

    def test_swagger_validator_badge_is_off(self, docs_client):
        assert "validatorUrl" in docs_client.get("/docs").text

    def test_docs_csp_allows_no_third_party_origin(self):
        assert "http" not in config.WEB_DOCS_CONTENT_SECURITY_POLICY.replace("https://fonts.googleapis.com", "") \
                                                                    .replace("https://fonts.gstatic.com", "")

    def test_docs_assets_are_not_rate_limited(self):
        from api.rate_limit import route_bucket
        assert route_bucket("/docs-assets/swagger-ui-bundle.js") is None


class TestOpenApiContent:
    def test_every_operation_is_tagged_and_described(self, docs_client):
        _, operations = openapi_operations(docs_client)
        untagged = [(method, path) for path, method, op in operations if not op.get("tags")]
        undescribed = [(method, path) for path, method, op in operations if not op.get("description")]
        assert untagged == [] and undescribed == []

    def test_site_description_points_agents_to_instructions(self, docs_client):
        spec, _ = openapi_operations(docs_client)
        assert "/llms.txt" in spec["info"]["description"]
        assert spec["info"]["version"] != "0.1.0"

    @pytest.mark.parametrize("tool", sorted(TOOL_ENDPOINTS))
    def test_tool_routes_carry_the_registry_description(self, docs_client, tool):
        spec, _ = openapi_operations(docs_client)
        operation = spec["paths"][TOOL_ENDPOINTS[tool]]["get"]
        first_sentence = TOOL_SPECS[tool].schema["description"].split(".")[0]
        assert first_sentence in operation["description"]
        assert operation["summary"] and operation["summary"] != tool.replace("_", " ").title()

    @pytest.mark.parametrize("tool", sorted(TOOL_ENDPOINTS))
    def test_every_tool_route_parameter_is_documented(self, docs_client, tool):
        spec, _ = openapi_operations(docs_client)
        parameters = spec["paths"][TOOL_ENDPOINTS[tool]]["get"].get("parameters", [])
        undocumented = [p["name"] for p in parameters if not p.get("description")]
        assert undocumented == []

    def test_html_index_page_is_not_in_the_schema(self):
        import web.app as webapp
        webapp.app.openapi_schema = None
        spec = TestClient(webapp.app).get("/openapi.json").json()
        assert "/" not in spec["paths"]
