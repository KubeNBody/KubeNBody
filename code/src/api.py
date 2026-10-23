# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The FastAPI application: startup (migrations, default user, buckets, the
RabbitMQ publisher of publisher.py) and the routers of routes/ and
v2/api_v2.py."""

import asyncio
from contextlib import asynccontextmanager

import anyio.to_thread
import uvicorn
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from animation_cache import clear_expired_cache
from auth import check_production_secrets, create_default_user
from config import settings as app_settings
from file_service import ensure_buckets_exist, get_s3
from logging_config import setup_logging
from migrate import run_migrations
from publisher import close_publisher, connect_at_startup, publisher_keepalive
from request_size import RequestSizeLimitMiddleware
from routes.auth import router as auth_router
from routes.comparison import router as comparison_router
from routes.files import router as files_router
from routes.jobs import router as jobs_router
from routes.monitoring import router as monitoring_router
from routes.results import router as results_router
from services.comparison_cache import clear_expired_comparison_cache
from services.comparison_service import warmup_comparison_jit
from tracing import UNTRACED_URLS, setup_tracing
from v2.api_v2 import router as v2_router


logger = setup_logging('api')
setup_tracing('nbody-api')

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup_event()
    yield
    await shutdown_event()


app = FastAPI(
    title="KubeNBody API",
    description="REST API of KubeNBody: N-body simulations on Kubernetes",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# a span per HTTP request (no-op when tracing is off)
FastAPIInstrumentor.instrument_app(app, excluded_urls=UNTRACED_URLS)

app.include_router(v2_router, prefix="/api/v2", tags=["V2"])
app.include_router(auth_router)
app.include_router(files_router)
app.include_router(monitoring_router)
app.include_router(jobs_router)
app.include_router(results_router)
app.include_router(comparison_router)

_cors = app_settings.api.cors_origins
cors_origins = [origin.strip() for origin in _cors.split(",")] if _cors != "*" else ["*"]

# Body size cap, inside CORS (a 413 still carries its headers): before any
# route or form parsing reads the body, see request_size
app.add_middleware(RequestSizeLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    # with "*" Starlette would reflect any origin and allow credentials. The
    # API authenticates by bearer token, so credentials only for named origins
    allow_credentials=cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

INPUT_FILES_BUCKET = app_settings.s3.input_files_bucket

# Database connections: each handler thread holds its own pooled connection,
# so FastAPI's threadpool (anyio's 40 threads by default) is capped below the
# pool, leaving connections for the event-loop thread and the asyncio.to_thread
# workers. Raising the pool instead would let the API take most of pgBouncer's
# default_pool_size (configs/base/infra/postgres.yml: 60 server connections in
# session mode, shared with the workers' lease renewers).
DB_CONNECTIONS_RESERVED = 4


def _limit_threadpool() -> None:
    """Cap FastAPI's threadpool at the database pool size, less the reserve."""
    threads = max(1, app_settings.database.max_connections - DB_CONNECTIONS_RESERVED)
    anyio.to_thread.current_default_thread_limiter().total_tokens = threads
    logger.info(f"Handler threadpool limited to {threads} threads "
                f"(DB pool: {app_settings.database.max_connections} connections)")


async def periodic_cache_cleanup():
    """Periodically clean up expired cache entries every 5 minutes."""

    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            animation_deleted = clear_expired_cache()
            comparison_deleted = clear_expired_comparison_cache()
            total_deleted = animation_deleted + comparison_deleted
            if total_deleted > 0:
                logger.info(f"Cleared {total_deleted} expired cache entries (animation: {animation_deleted}, "
                            f"comparison: {comparison_deleted})")
        except Exception as e:
            logger.error(f"Error during cache cleanup: {e}")


async def startup_event():
    """Initialize connections and ensure resources exist."""
    logger.info("Starting the KubeNBody API...")

    # No development secrets in a cluster (raises, the pod restarts with a
    # clear log line instead of serving forgeable tokens)
    check_production_secrets()

    try:
        run_migrations()
        logger.info("Database migrations completed")
    except Exception as e:
        logger.error(f"Failed to run database migrations: {e}")
        raise

    try:
        create_default_user()
    except Exception as e:
        logger.error(f"Failed to create default user: {e}")

    ensure_buckets_exist()

    try:
        get_s3().meta.client.head_bucket(Bucket=INPUT_FILES_BUCKET)
        logger.info(f"Bucket '{INPUT_FILES_BUCKET}' already exists")
    except ClientError:
        try:
            get_s3().create_bucket(Bucket=INPUT_FILES_BUCKET)
            logger.info(f"Created bucket '{INPUT_FILES_BUCKET}'")
        except ClientError as e:
            logger.error(f"Failed to create bucket '{INPUT_FILES_BUCKET}': {e}")

    _limit_threadpool()

    await connect_at_startup()
    app.state.publisher_keepalive_task = asyncio.create_task(publisher_keepalive())

    # Start periodic cache cleanup task (referenced: the loop holds tasks weakly)
    app.state.cache_cleanup_task = asyncio.create_task(periodic_cache_cleanup())

    # Warmup comparison JIT functions (avoid first-request latency)
    try:
        warmup_comparison_jit()
    except Exception as e:
        logger.warning(f"Failed to warmup comparison JIT functions: {e}")

    logger.info("API startup completed")


async def shutdown_event():
    """Clean up connections."""
    logger.info("Shutting down the KubeNBody API...")
    keepalive = getattr(app.state, "publisher_keepalive_task", None)
    if keepalive:
        keepalive.cancel()

    await asyncio.to_thread(close_publisher)


if __name__ == "__main__":
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=True
    )
