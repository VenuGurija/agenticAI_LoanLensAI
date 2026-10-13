import asyncio
from pathlib import Path

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.core.config import (
    ANALYZE_HTTP_TIMEOUT_SECONDS,
    VISION_REQUEST_TIMEOUT_SECONDS,
    Settings,
)
from app.main import AnalyzeHttpTimeoutMiddleware, create_app
from app.services.images.preprocess import VISION_LONGEST_EDGE

_SPEC_ENV_KEYS = (
    "LOANLENS_ENV",
    "LOANLENS_DATABASE_URL",
    "LOANLENS_TMP_DIR",
    "LOANLENS_MAX_IMAGES_PER_ANALYSIS",
    "LOANLENS_MAX_IMAGE_BYTES",
    "LOANLENS_ANALYSIS_TTL_HOURS",
    "VISION_PROVIDER",
    "VISION_API_KEY",
    "VISION_BASE_URL",
    "VISION_MODEL",
    "LOANLENS_LOG_LEVEL",
    "LOANLENS_CORS_ORIGINS",
    "LOANLENS_WORKER_ENABLED",
    "LOANLENS_VISION_TIMEOUT_SECONDS",
)


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_title_is_loan_lens_ai(client: TestClient) -> None:
    assert client.app.title == "Loan Lens AI"


def test_settings_use_spec_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _SPEC_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.loanlens_env == "development"
    assert settings.loanlens_database_url == "sqlite:///./data/loanlens.db"
    assert settings.loanlens_tmp_dir == "data/tmp/images"
    assert settings.loanlens_max_images_per_analysis == 12
    assert settings.loanlens_max_image_bytes == 10_485_760
    assert settings.loanlens_analysis_ttl_hours == 24
    assert settings.vision_provider == "fake"
    assert settings.vision_api_key == ""
    assert settings.vision_base_url == ""
    assert settings.vision_model == ""
    assert settings.loanlens_log_level == "INFO"
    assert settings.cors_origin_list == [
        "http://127.0.0.1:5175",
        "http://localhost:5175",
    ]
    assert "*" not in settings.cors_origin_list
    assert settings.loanlens_worker_enabled is True
    assert settings.loanlens_vision_timeout_seconds == VISION_REQUEST_TIMEOUT_SECONDS
    assert ANALYZE_HTTP_TIMEOUT_SECONDS == 120
    assert VISION_REQUEST_TIMEOUT_SECONDS == 60
    assert VISION_LONGEST_EDGE == 2048


def test_vision_timeout_env_can_be_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOANLENS_VISION_TIMEOUT_SECONDS", "300")
    settings = Settings(_env_file=None)
    assert settings.loanlens_vision_timeout_seconds == 300


def test_production_cors_default_is_an_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOANLENS_CORS_ORIGINS", raising=False)
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        _env_file=None,
        loanlens_env="production",
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
    )
    application = create_app(settings)
    try:
        cors = _cors_layer(application)
        assert cors.kwargs["allow_origins"] == [
            "http://127.0.0.1:5175",
            "http://localhost:5175",
        ]
        assert "*" not in cors.kwargs["allow_origins"]
        timeout = _timeout_layer(application)
        assert timeout.kwargs["timeout_seconds"] == float(ANALYZE_HTTP_TIMEOUT_SECONDS)
    finally:
        application.state.engine.dispose()


def test_cors_preflight_echoes_configured_origin(tmp_path: Path) -> None:
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        _env_file=None,
        loanlens_env="production",
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_cors_origins="https://loanlens.example",
    )
    application = create_app(settings)
    try:
        with TestClient(application) as client:
            allowed = client.options(
                "/api/v1/health",
                headers={
                    "Origin": "https://loanlens.example",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert allowed.status_code == 200
            assert allowed.headers["access-control-allow-origin"] == "https://loanlens.example"

            private_network = client.options(
                "/api/v1/books",
                headers={
                    "Origin": "https://loanlens.example",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                    "Access-Control-Request-Private-Network": "true",
                },
            )
            assert private_network.status_code == 200
            assert private_network.headers["access-control-allow-private-network"] == "true"
            assert private_network.headers["access-control-allow-origin"] == "https://loanlens.example"

            health = client.get("/api/v1/health", headers={"Origin": "https://loanlens.example"})
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert health.headers["access-control-allow-origin"] == "https://loanlens.example"

            denied = client.options(
                "/api/v1/health",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert denied.headers.get("access-control-allow-origin") != "*"
    finally:
        application.state.engine.dispose()


def test_wildcard_cors_origin_is_rejected(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        loanlens_env="production",
        loanlens_database_url=f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}",
        loanlens_cors_origins="*",
    )
    with pytest.raises(ValueError, match=r"not \*"):
        create_app(settings)


def test_analyze_http_timeout_returns_vision_timeout() -> None:
    async def slow(scope: dict, receive: object, send: object) -> None:
        await asyncio.sleep(5)

    middleware = AnalyzeHttpTimeoutMiddleware(slow, timeout_seconds=0.05)
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/analyses/anl_1/analyze",
        "headers": [],
    }
    asyncio.run(middleware(scope, receive, send))

    assert messages[0]["status"] == 504
    assert b"VISION_TIMEOUT" in messages[1]["body"]


def _cors_layer(application):
    return next(layer for layer in application.user_middleware if layer.cls is CORSMiddleware)


def _timeout_layer(application):
    return next(layer for layer in application.user_middleware if layer.cls is AnalyzeHttpTimeoutMiddleware)
