from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import Settings
from app.database import Database
from app.services.brain_provider import WebsiteBrainProvider
from app.services.native_site_analysis import NativeSiteAnalyzer
from app.services.production_runtime import ProductionRuntimeService
from app.services.site_scan_runtime import SiteScanRuntimeService
from app.middleware import InternalServiceAuthMiddleware


def create_app(settings: Settings) -> FastAPI:
    """Build an application from explicit, already-resolved settings."""

    database = Database(settings.database_path)
    brain = WebsiteBrainProvider()
    site_analyzer = NativeSiteAnalyzer(settings.output_root, brain)
    production_runtime = ProductionRuntimeService(database, settings.output_root)
    site_scan_runtime = SiteScanRuntimeService(database, settings.output_root, site_analyzer)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.create_schema()
        production_runtime.reconcile_all()
        site_scan_runtime.reconcile_all()
        try:
            yield
        finally:
            site_scan_runtime.shutdown()
            database.dispose()

    app = FastAPI(
        title="Furniture Workflow API",
        version="0.16.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.website_brain = brain
    app.state.site_analyzer = site_analyzer
    app.state.production_runtime = production_runtime
    app.state.site_scan_runtime = site_scan_runtime

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.web_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )
    internal_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    if internal_token:
        if len(internal_token) < 32:
            raise ValueError("INTERNAL_SERVICE_TOKEN must contain at least 32 characters")
        app.add_middleware(InternalServiceAuthMiddleware, token=internal_token)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


def create_runtime_app() -> FastAPI:
    """Build the real server app after loading the repository-root env file."""

    return create_app(Settings.from_environment(load_env_file=True))
