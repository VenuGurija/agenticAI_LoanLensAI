"""Loan Lens AI HTTP application."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.analyses import router as analyses_router
from app.api.health import router as health_router
from app.models.database import init_database
from app.core.config import ANALYZE_HTTP_TIMEOUT_SECONDS, Settings, get_settings
from app.core.errors import AppError, error_envelope
from app.core.logging import configure_logging, log_event, redact_payload
from app.services.images.upload import resolve_tmp_dir
from app.services.retention import commit_expired_draft_sweep
from app.services.vision.factory import get_vision_provider
from app.services.worker import worker_loop

_CORS_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
_CORS_HEADERS = ["Accept", "Content-Type"]


def _error_response(request: Request, status_code: int, content: dict[str, object]) -> JSONResponse:
    """Error JSON never includes `VISION_API_KEY`."""
    secret = request.app.state.settings.vision_api_key
    body = redact_payload(content, (secret,))
    if not isinstance(body, dict):
        body = content
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        log_event(
            "app_error",
            level=logging.ERROR if exc.status_code >= 500 else logging.WARNING,
            error_code=exc.code,
        )
        return _error_response(request, exc.status_code, exc.envelope())

    @application.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, _exc: RequestValidationError) -> JSONResponse:
        log_event("request_invalid", error_code="REQUEST_INVALID")
        return _error_response(
            request,
            422,
            error_envelope(
                "REQUEST_INVALID",
                "The request could not be processed.",
                False,
            ),
        )

    @application.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code, message = "NOT_FOUND", "Not found."
        elif exc.status_code == 405:
            code, message = "METHOD_NOT_ALLOWED", "Method not allowed."
        else:
            code, message = "HTTP_ERROR", "Request failed."
        retryable = exc.status_code >= 500
        log_event(
            "http_error",
            level=logging.ERROR if retryable else logging.INFO,
            error_code=code,
        )
        return _error_response(request, exc.status_code, error_envelope(code, message, retryable))

    @application.exception_handler(Exception)
    async def handle_unexpected(request: Request, _exc: Exception) -> JSONResponse:
        log_event("internal_error", level=logging.ERROR, error_code="INTERNAL_ERROR")
        err = AppError("INTERNAL_ERROR")
        return _error_response(request, err.status_code, err.envelope())


def _cors_allow_origins(settings: Settings) -> list[str]:
    """Origins from config. Production uses the same allowlist and never `*`."""
    origins = settings.cors_origin_list
    if any(origin == "*" for origin in origins):
        raise ValueError("LOANLENS_CORS_ORIGINS must be an explicit allowlist, not *")
    return origins


class AnalyzeHttpTimeoutMiddleware:
    """Stop a stuck analyze request at the SPEC §22 server timeout (120s)."""

    def __init__(self, app: ASGIApp, timeout_seconds: float) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_analyze_post(scope):
            await self.app(scope, receive, send)
            return

        started = False

        async def send_tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await asyncio.wait_for(
                self.app(scope, receive, send_tracked),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            if started:
                return
            log_event("analyze_timeout", level=logging.ERROR, error_code="VISION_TIMEOUT")
            err = AppError("VISION_TIMEOUT")
            response = JSONResponse(status_code=err.status_code, content=err.envelope())
            await response(scope, receive, send)


def _is_analyze_post(scope: Scope) -> bool:
    return scope.get("method") == "POST" and str(scope.get("path", "")).endswith("/analyze")


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()
    origins = _cors_allow_origins(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """Start one draft worker when `LOANLENS_WORKER_ENABLED` is true."""

        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        if application.state.settings.loanlens_worker_enabled:
            if getattr(application.state, "vision_provider", None) is None:
                application.state.vision_provider = get_vision_provider(
                    application.state.settings
                )
            task = asyncio.create_task(
                worker_loop(
                    application.state.session_factory,
                    settings=application.state.settings,
                    provider=application.state.vision_provider,
                    stop=stop,
                )
            )
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task

    configure_logging(settings.loanlens_log_level, secrets=(settings.vision_api_key,))
    application = FastAPI(title="Loan Lens AI", lifespan=lifespan)
    application.state.settings = settings
    init_database(application, settings)
    commit_expired_draft_sweep(
        application.state.session_factory,
        resolve_tmp_dir(settings.loanlens_tmp_dir),
    )
    register_exception_handlers(application)
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(analyses_router, prefix="/api/v1")
    application.add_middleware(
        AnalyzeHttpTimeoutMiddleware,
        timeout_seconds=float(ANALYZE_HTTP_TIMEOUT_SECONDS),
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_private_network=True,
        allow_methods=_CORS_METHODS,
        allow_headers=_CORS_HEADERS,
    )
    return application


app = create_app()
