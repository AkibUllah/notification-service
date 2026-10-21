from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from notifications import __version__
from notifications.api import admin, inbox_api, tenant_api
from notifications.api.deps import get_ctx, get_db, require_admin
from notifications.clock import Clock
from notifications.config import Settings, get_settings
from notifications.core.channels import Channel
from notifications.database import init_db, make_engine, make_session_factory
from notifications.logging_config import configure_logging
from notifications.providers.chain import ProviderChain
from notifications.schemas import ErrorResponse, HealthOut
from notifications.services import digests, worker
from notifications.services.context import AppContext
from notifications.services.errors import DomainError, RateLimited

log = logging.getLogger(__name__)


async def worker_loop(session_factory, ctx: AppContext, stop: asyncio.Event) -> None:
    """In-process worker: digests then deliveries, every WORKER_POLL_SECONDS."""
    while not stop.is_set():
        session = session_factory()
        try:
            now = ctx.clock.now()
            flushed = digests.flush_due(session, ctx, now)
            session.commit()
            stats = await worker.process_once(session, ctx, now)
            if stats.processed or flushed:
                log.info("worker: %s digests_flushed=%d", stats.as_dict(), len(flushed))
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("worker iteration failed")
            session.rollback()
        finally:
            session.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=ctx.settings.worker_poll_seconds)
        except asyncio.TimeoutError:
            pass


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    chains: dict[Channel, ProviderChain] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    ctx = AppContext.from_settings(settings, clock=clock, transport=transport, chains=chains)
    stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        task = (
            asyncio.create_task(worker_loop(session_factory, ctx, stop))
            if settings.worker_enabled
            else None
        )
        app.state.worker_task = task
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await asyncio.wait([task], timeout=5)
            await ctx.http.aclose()
            engine.dispose()

    app = FastAPI(
        title="Notification Service",
        version=__version__,
        description=(
            "Email, SMS, push, in-app and webhook notifications with templates, preferences, "
            "quiet hours, digests and retries."
        ),
        lifespan=lifespan,
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
        },
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.ctx = ctx

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimited) else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    app.include_router(admin.router, dependencies=[Depends(require_admin)])
    app.include_router(tenant_api.router)
    app.include_router(inbox_api.router)
    app.include_router(inbox_api.public)

    @app.get("/health", response_model=HealthOut, tags=["ops"])
    def health(db=Depends(get_db), ctx: AppContext = Depends(get_ctx)):
        db.execute(text("SELECT 1"))
        task = getattr(app.state, "worker_task", None)
        worker_state = "disabled" if task is None else ("running" if not task.done() else "stopped")
        return HealthOut(
            status="ok",
            database="ok",
            worker=worker_state,
            providers=[c.snapshot() for c in ctx.chains.values()],
            inbox_subscribers=ctx.bus.subscriber_count(),
            clock=ctx.clock.now(),
            version=__version__,
        )

    return app


app = create_app()
