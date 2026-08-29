from fastapi import APIRouter

from app.api.routes import control_plane, health


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(control_plane.router)

# Legacy Skills bridge routes are intentionally not mounted. The native control
# plane exposes the supported public workflow surface.

# Legacy project/worker routes are intentionally not mounted in the native
# Website release. They remain in the source tree for historical migration
# reference, but the production app exposes only native control-plane APIs.
