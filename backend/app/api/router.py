from fastapi import APIRouter

from app.api.routes import admin, auth, channels, health, me, uploads, videos

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(uploads.router)
api_router.include_router(videos.router)
api_router.include_router(channels.router)
api_router.include_router(me.router)
