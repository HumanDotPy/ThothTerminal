"""Privacy contracts at the serving boundary; never use the live database."""
from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from thoth_terminal.config import settings
from thoth_terminal.db import get_session
from thoth_terminal.models import Base, Document, Source, ResearchReport, ResearchReportArtifact, ResearchReportVersion
from thoth_terminal.web.app import app
from thoth_terminal.web.app_sections import ops_routes
from thoth_terminal.web import deepseek_harness_proxy


@pytest.fixture
def isolated_client(monkeypatch, tmp_path):
    # Private-route side effects are never allowed to contact runtime services,
    # even when the pre-fix boundary incorrectly admits the request.
    monkeypatch.setattr(ops_routes, "_ops_health_payload", lambda: {"private_diagnostics": True})

    async def synthetic_native_socket(socket):
        await socket.accept()
        await socket.close(code=1000)

    monkeypatch.setattr(deepseek_harness_proxy, "native_socket", synthetic_native_socket)
    for key, value in {
        "mode": "local_desktop", "public_build": False,
        "public_allow_unauthenticated": False, "require_auth": True,
        "public_base_url": "https://public.example.test",
        "public_base_url_restrict_to_public_routes": True,
        "app_auth_token": "synthetic-owner-token", "mcp_auth_token": "synthetic-mcp-token",
        "chatgpt_app_token": None, "chatgpt_app_read_only_token": None,
        "objects_dir": tmp_path / "objects",
    }.items():
        monkeypatch.setattr(settings, key, value)
    settings.objects_dir.mkdir()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def session_dependency():
        with factory() as session:
            yield session

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session_dependency
    # No context manager: app lifespan would initialize the real runtime.
    client = TestClient(app, base_url="https://public.example.test")
    try:
        yield client, factory
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        engine.dispose()


@pytest.mark.parametrize("host", ["public.example.test", "public.example.test.", "alternate.example.test", "localhost", "127.0.0.1"])
def test_nonlocal_requests_cannot_select_private_routes_by_host(isolated_client, host):
    client, _ = isolated_client
    response = client.get("/openapi.json", headers={"host": host})
    assert response.status_code == 404
    assert "paths" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/ops/health", "/ops/version", "/openapi.json", "/docs", "/api/search", "/objects/private.pdf", "/omori-local/private.png"])
def test_public_deployment_never_serves_private_diagnostics_or_data(isolated_client, monkeypatch, path):
    client, _ = isolated_client
    monkeypatch.setattr(settings, "mode", "public")
    monkeypatch.setattr(settings, "public_build", True)
    monkeypatch.setattr(settings, "public_allow_unauthenticated", True)
    # Even owner credentials must not enable a private plane in a public build.
    response = client.get(path, headers={"authorization": "Bearer synthetic-owner-token"})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_private_owner_access_is_not_publicly_cacheable(isolated_client):
    client, _ = isolated_client
    response = client.get("/openapi.json", headers={"x-thoth-auth": "synthetic-owner-token"})
    assert response.status_code == 200
    assert "paths" in response.json()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("token,status", [(None, 404), ("wrong", 404), ("synthetic-mcp-token", 200)])
def test_public_host_retains_authenticated_mcp_transport(isolated_client, token, status):
    client, _ = isolated_client
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    response = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "synthetic-test-client", "version": "1"}},
    })
    assert response.status_code == status
    if status == 200:
        payload = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
        assert payload["result"]["serverInfo"]["name"]
    assert response.headers["cache-control"] == "no-store"


def test_public_index_does_not_enrich_published_metadata_from_working_report(isolated_client):
    client, factory = isolated_client
    _publication(factory)
    with factory() as session:
        report = session.query(ResearchReport).first()
        report.title = "SYNTHETIC PRIVATE DRAFT TITLE"
        report.extra = {"summary": "SYNTHETIC PRIVATE DRAFT SUMMARY", "topic": "PRIVATE DRAFT TOPIC"}
        session.commit()
    response = client.get("/public")
    assert response.status_code == 200
    assert "Published text" in response.text
    assert "SYNTHETIC PRIVATE" not in response.text
    assert "PRIVATE DRAFT TOPIC" not in response.text
    assert response.headers["cache-control"] == "no-cache"


def test_public_database_failure_does_not_disclose_private_diagnostics(isolated_client):
    from thoth_terminal.db import DatabaseBusyError

    client, _ = isolated_client

    def failed_database():
        raise DatabaseBusyError("SYNTHETIC PRIVATE DATABASE LOCATION")

    app.dependency_overrides[get_session] = failed_database
    response = client.get("/public", headers={"Accept": "application/json"})
    assert response.status_code == 503
    assert "SYNTHETIC PRIVATE" not in response.text
    assert "temporarily unavailable" in response.text


def test_public_mode_denies_private_websocket_before_native_connection(isolated_client, monkeypatch):
    client, _ = isolated_client
    monkeypatch.setattr(settings, "mode", "public")
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/agents/harness/native/api/remote.mux", headers={"authorization": "Bearer synthetic-owner-token"}):
            pass
    assert denied.value.code == 1008


def _publication(factory, *, object_path=None):
    now = datetime(2026, 10, 10)
    with factory() as session:
        source = Source(name="Synthetic private source", category="research", config={}, created_at=now)
        session.add(source)
        session.flush()
        document = Document(id=37, kind="article", source_id=source.id, title="SYNTHETIC PRIVATE DOCUMENT TITLE", ingested_at=now,
                            raw_text="Synthetic private body", normalized_text="Synthetic private body", content_hash="synthetic", extra={})
        report = ResearchReport(slug="public-example", subject_key="test:public-example", title="Public example",
                                status="active", created_at=now, updated_at=now, extra={})
        session.add_all([document, report])
        session.flush()
        version = ResearchReportVersion(report_id=report.id, version=1, status="published", content_json={},
                                        content_md="# Published text\n\nReference [[doc:37]].", coverage_json={},
                                        as_of_at=now, created_at=now, updated_at=now, published_at=now)
        session.add(version)
        session.flush()
        report.latest_published_version_id = version.id
        artifact_id = None
        if object_path is not None:
            artifact = ResearchReportArtifact(report_version_id=version.id, artifact_type="markdown", variant="default",
                                              object_path=object_path, content_type="text/markdown", payload={}, created_at=now)
            session.add(artifact)
            session.flush()
            artifact_id = artifact.id
        session.commit()
        return artifact_id


def test_public_report_renders_only_published_text_without_private_reference_enrichment(isolated_client):
    client, factory = isolated_client
    _publication(factory)
    response = client.get("/public/reports/public-example/reader")
    assert response.status_code == 200
    assert "Published text" in response.text
    assert "SYNTHETIC PRIVATE DOCUMENT TITLE" not in response.text
    assert '/docs/37' not in response.text


def test_public_artifact_cannot_read_outside_object_store(isolated_client, tmp_path):
    client, factory = isolated_client
    private_file = tmp_path / "private-settings.md"
    private_file.write_text("SYNTHETIC PRIVATE FILE BYTES", encoding="utf-8")
    artifact_id = _publication(factory, object_path=str(private_file))
    response = client.get(f"/public/reports/public-example/artifacts/{artifact_id}/raw")
    assert response.status_code == 404
    assert "SYNTHETIC PRIVATE FILE BYTES" not in response.text
