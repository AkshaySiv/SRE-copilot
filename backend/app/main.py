from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app import __version__
from app.api.routes import clusters, integrations, investigations, meta
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import Integration
from app.db.session import dispose_db, init_db, sessionmaker
from app.plugins import registry
from app.services.integrations import bootstrap_from_file, pool, prepare_config, plugin_class
from app.services.investigations import runner

log = logging.getLogger("sre_copilot")


async def seed_demo() -> None:
    async with sessionmaker()() as s:
        if await s.scalar(select(func.count()).select_from(Integration)):
            return
        for name, kind, type_, desc in (
            ("demo-cluster", "kubernetes", "demo", "Simulated RKE2 cluster with a live payments incident"),
            ("demo-monitoring", "monitoring", "demo", "Simulated Prometheus + Loki"),
        ):
            s.add(Integration(name=name, kind=kind, type=type_, description=desc, is_default=True,
                              config=prepare_config(plugin_class(kind, type_), {})))
        await s.commit()
        log.info("Seeded demo integrations")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    registry.load(settings.plugin_modules)
    await init_db()
    await runner.recover_interrupted()
    if settings.bootstrap_file:
        await bootstrap_from_file(settings.bootstrap_file)
    if settings.seed_demo:
        await seed_demo()
    log.info("SRE Copilot %s started (auth=%s, remediation=%s)", __version__, settings.auth_mode,
             settings.remediation_mode)
    yield
    await runner.shutdown()
    await pool.close_all()
    await dispose_db()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="SRE Copilot", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    for r in (meta.router, integrations.router, clusters.router, investigations.router):
        app.include_router(r, prefix="/api")

    if settings.static_dir and Path(settings.static_dir).is_dir():
        static = Path(settings.static_dir)
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):  # client-side routing fallback
            candidate = (static / path).resolve()
            if path and candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static / "index.html")

    return app


app = create_app()
