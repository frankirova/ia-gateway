"""FastAPI application factory for the shared inference gateway.

The gateway is task-agnostic: it authenticates an application, validates an
OpenAI-shaped request, translates the public model alias to the configured
backend model and returns one non-streaming completion. It never inspects what
the messages mean.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import upstream as upstream_adapter
from .auth import authenticate
from .config import GatewayConfig, load_config
from .errors import (
    GatewayError,
    capacity_exhausted,
    internal_error,
    invalid_request,
    payload_too_large,
    rate_limit_exceeded,
    unsupported_media_type,
    upstream_deadline,
    upstream_failure,
)
from .limits import InferenceSlot, ReadinessCache, TokenBucket
from .logging_setup import configure_logging, log_chat_request
from .schema import (
    PUBLIC_ALIASES,
    enforce_context_budget,
    parse_chat_request,
    resolve_upstream_model,
)

MAX_REQUEST_BODY_BYTES = 64 * 1024
READINESS_CACHE_TTL_SECONDS = 30.0
REQUEST_ID_SCOPE_KEY = "gateway.request_id"
MODELS_OWNER = "homelab"


class RequestIDMiddleware:
    """Attach a gateway-generated request identifier to every response."""

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = f"req_{uuid.uuid4().hex}"
        scope[REQUEST_ID_SCOPE_KEY] = request_id

        async def send_with_request_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


@dataclass
class GatewayRuntime:
    """Process-local state built once from configuration."""

    config: GatewayConfig
    alias_map: dict[str, str]
    slot: InferenceSlot
    readiness: ReadinessCache
    buckets: dict[str, TokenBucket]

    @classmethod
    def from_config(cls, config: GatewayConfig) -> "GatewayRuntime":
        return cls(
            config=config,
            alias_map={alias: config.upstream.model for alias in PUBLIC_ALIASES},
            slot=InferenceSlot(),
            readiness=ReadinessCache(READINESS_CACHE_TTL_SECONDS),
            buckets={
                application.id: TokenBucket(
                    application.rate_capacity, application.rate_per_minute
                )
                for application in config.applications
            },
        )


def request_id_of(request: Request) -> str:
    return str(request.scope.get(REQUEST_ID_SCOPE_KEY, ""))


def error_response(request: Request, error: GatewayError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error.envelope(),
        headers=error.headers(),
    )


async def read_request_body(request: Request) -> bytes:
    """Read the request body, stopping as soon as the byte limit is exceeded."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise invalid_request("Invalid Content-Length header.") from exc
        if declared_size > MAX_REQUEST_BODY_BYTES:
            raise payload_too_large()

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BODY_BYTES:
            raise payload_too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def require_json_content_type(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise unsupported_media_type()


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"unsupported JSON constant: {value}")


def parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        raise invalid_request("Request body must be a valid JSON object.") from exc
    if not isinstance(payload, dict):
        raise invalid_request("Request body must be a JSON object.")
    return payload


def build_completion_response(
    alias: str, completion: upstream_adapter.Completion
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": alias,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion.content},
                "finish_reason": completion.finish_reason,
            }
        ],
    }
    if completion.usage is not None:
        body["usage"] = completion.usage
    return body


def model_list(aliases: tuple[str, ...]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": alias,
                "object": "model",
                "created": 0,
                "owned_by": MODELS_OWNER,
            }
            for alias in aliases
        ],
    }


def create_app() -> FastAPI:
    """Build the application, failing closed on invalid configuration."""
    config = load_config()
    runtime = GatewayRuntime.from_config(config)
    logger = configure_logging()

    application = FastAPI(
        title="Inference Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.runtime = runtime
    application.add_middleware(RequestIDMiddleware)

    @application.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return error_response(request, exc)

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(request, invalid_request("Request validation failed."))

    @application.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            error = GatewayError(
                404, "not_found", "Unknown endpoint.", "invalid_request"
            )
        elif 400 <= exc.status_code < 500:
            error = invalid_request("Request could not be processed.")
        else:
            error = internal_error()
        return error_response(request, error)

    @application.post("/v1/chat/completions")
    async def create_chat_completion(request: Request) -> JSONResponse:
        started = time.perf_counter()
        context: dict[str, Any] = {
            "request_id": request_id_of(request),
            "application_id": None,
            "model": None,
            "status": 500,
            "code": "internal_error",
            "usage": None,
        }
        try:
            body = await read_request_body(request)
            require_json_content_type(request)
            app_config = authenticate(
                request.headers.get("authorization"), runtime.config
            )
            context["application_id"] = app_config.id

            payload = parse_json_body(body)
            chat_request = parse_chat_request(payload, app_config)

            upstream_model = resolve_upstream_model(
                chat_request.model, app_config, runtime.alias_map
            )
            # Only a validated alias may reach the request log.
            context["model"] = chat_request.model
            enforce_context_budget(
                chat_request.messages, runtime.config.limits.context_char_budget
            )

            if not runtime.buckets[app_config.id].consume():
                raise rate_limit_exceeded()
            if not runtime.slot.try_acquire():
                raise capacity_exhausted()
            try:
                completion = await run_in_threadpool(
                    upstream_adapter.chat_completion,
                    runtime.config.upstream,
                    upstream_model,
                    chat_request.messages,
                    chat_request.max_tokens,
                    temperature=chat_request.temperature,
                    reasoning=chat_request.reasoning,
                )
            finally:
                runtime.slot.release()

            context["status"] = 200
            context["code"] = None
            context["usage"] = completion.usage
            return JSONResponse(
                status_code=200,
                content=build_completion_response(chat_request.model, completion),
            )
        except GatewayError as error:
            context["status"] = error.status_code
            context["code"] = error.code
            return error_response(request, error)
        except upstream_adapter.UpstreamTimeout:
            context["status"] = 504
            context["code"] = "upstream_timeout"
            return error_response(request, upstream_deadline())
        except upstream_adapter.UpstreamError:
            context["status"] = 502
            context["code"] = "upstream_error"
            return error_response(request, upstream_failure())
        except Exception as exc:
            # Never log exception text: it could contain request data.
            logger.error(
                json.dumps(
                    {
                        "event": "gateway_internal_error",
                        "request_id": context["request_id"],
                        "exception": type(exc).__name__,
                    },
                    sort_keys=True,
                )
            )
            context["status"] = 500
            context["code"] = "internal_error"
            return error_response(request, internal_error())
        finally:
            log_chat_request(
                logger,
                request_id=context["request_id"],
                application_id=context["application_id"],
                model=context["model"],
                status=context["status"],
                code=context["code"],
                duration_ms=(time.perf_counter() - started) * 1000.0,
                usage=context["usage"],
            )

    @application.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        app_config = authenticate(request.headers.get("authorization"), runtime.config)
        return JSONResponse(status_code=200, content=model_list(app_config.models))

    @application.get("/health/live")
    async def health_live() -> JSONResponse:
        return JSONResponse(status_code=200, content={"status": "ok"})

    @application.get("/health/ready")
    async def health_ready() -> JSONResponse:
        healthy = await run_in_threadpool(
            runtime.readiness.result,
            lambda: upstream_adapter.check_health(runtime.config.upstream),
        )
        if healthy:
            return JSONResponse(status_code=200, content={"status": "ok"})
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    return application
