
"""FastAPI HTTP sidecar for flagsmith-contexts."""

from __future__ import annotations

import os
from typing import Protocol

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
)

from .cache import verify_webhook_signature
from .schemas import (
    CacheBackend,
    EvaluateRequest,
    EvaluateResponse,
    HealthResponse,
    TrackEventRequest,
    TrackEventResponse,
    WebhookResponse,
)


class ContextEngine(Protocol):
    """Interface for Partner 1's ContextFlagsmith client."""

    def get_context_flags(
        self, request: EvaluateRequest
    ) -> EvaluateResponse:
        ...

    def track_context_event(
        self, request: TrackEventRequest
    ) -> None:
        ...


def create_app(
    engine: ContextEngine | None = None,
    cache: CacheBackend | None = None,
    webhook_secret: str | None = None,
) -> FastAPI:
    """Create the HTTP sidecar with injectable dependencies."""

    app = FastAPI(
        title="Flagsmith Context Engine",
        description="Multi-Tenant Context Evaluation HTTP Sidecar",
        version="1.0.0",
    )

    app.state.context_engine = engine
    app.state.cache = cache
    app.state.webhook_secret = (
        webhook_secret or os.getenv("FLAGSMITH_WEBHOOK_SECRET")
    )

    def get_engine(request: Request) -> ContextEngine:
        context_engine = request.app.state.context_engine

        if context_engine is None:
            raise HTTPException(
                status_code=503,
                detail="Context engine is not configured",
            )

        return context_engine

    def get_cache(request: Request) -> CacheBackend:
        cache_backend = request.app.state.cache

        if cache_backend is None:
            raise HTTPException(
                status_code=503,
                detail="Cache backend is not configured",
            )

        return cache_backend

    
    @app.get("/health", response_model=HealthResponse)
    def health(request: Request):
        cache_backend = request.app.state.cache

        cache_entries = getattr(
            cache_backend, "entry_count", 0
        )

        return HealthResponse(
            status="ok",
            flagsmith_connected=(
                request.app.state.context_engine is not None
            ),
            cache_entries=cache_entries,
        )

    @app.post(
        "/v1/evaluate",
        response_model=EvaluateResponse,
    )
    def evaluate(
        payload: EvaluateRequest,
        context_engine: ContextEngine = Depends(get_engine),
    ):
        try:
            return context_engine.get_context_flags(payload)

        except HTTPException:
            raise

        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="Feature flag evaluation failed",
            ) from exc

    @app.post(
        "/v1/events",
        response_model=TrackEventResponse,
        status_code=202,
    )
    def track_event(
        payload: TrackEventRequest,
        context_engine: ContextEngine = Depends(get_engine),
    ):
        try:
            return context_engine.track_context_event(payload)

            
        except HTTPException:
            raise

        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="Event tracking failed",
            ) from exc

    @app.post(
        "/v1/webhooks/flagsmith",
        response_model=WebhookResponse,
    )
    async def flagsmith_webhook(
        request: Request,
        signature: str | None = Header(
            default=None,
            alias="X-Flagsmith-Signature",
        ),
        cache_backend: CacheBackend = Depends(get_cache),
    ):
        secret = request.app.state.webhook_secret

        if not secret:
            raise HTTPException(
                status_code=503,
                detail="Webhook secret is not configured",
            )

        if not signature:
            raise HTTPException(
                status_code=401,
                detail="Missing webhook signature",
            )

        body = await request.body()

        if not verify_webhook_signature(
            payload=body,
            signature=signature,
            secret=secret,
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid webhook signature",
            )

        # Clear cached evaluations after a valid webhook.
        purged_keys = getattr(cache_backend, "entry_count", 0)
        cache_backend.clear()

        return WebhookResponse(
            status="invalidated",
            purged_keys=purged_keys,
        )

    return app


def create_app_from_env() -> FastAPI:
    """Build a fully wired sidecar from environment variables (and `.env`).

    Reads FLAGSMITH_ENV_KEY (required), FLAGSMITH_API_URL and
    FLAGSMITH_WEBHOOK_SECRET. Intended for:
        uvicorn --factory flagsmith_contexts.server:create_app_from_env
    """

    from dotenv import load_dotenv

    from .client import ContextFlagsmith

    load_dotenv()
    env_key = os.getenv("FLAGSMITH_ENV_KEY")
    if not env_key:
        raise RuntimeError("FLAGSMITH_ENV_KEY must be set to start the sidecar")

    client = ContextFlagsmith(
        env_key,
        api_url=os.getenv("FLAGSMITH_API_URL") or "http://localhost:8000/api/v1/",
    )
    return create_app(
        engine=client,
        cache=client.cache,
        webhook_secret=os.getenv("FLAGSMITH_WEBHOOK_SECRET"),
    )


app = create_app()
