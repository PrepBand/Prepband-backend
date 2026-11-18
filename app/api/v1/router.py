from fastapi import APIRouter

from . import admin, health, me

router = APIRouter()
router.include_router(health.router, tags=["health"])
router.include_router(me.router, tags=["me"])
router.include_router(admin.router, tags=["admin"])
