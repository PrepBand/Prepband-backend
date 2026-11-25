from fastapi import APIRouter

from . import (
    admin,
    admin_content,
    admin_ops,
    admin_resources,
    attempts,
    certificates,
    client_events,
    examiner,
    health,
    me,
    mocks,
    resources,
    review,
    tests,
)

router = APIRouter()
router.include_router(health.router, tags=["health"])
router.include_router(me.router, tags=["me"])
router.include_router(admin.router, tags=["admin"])
router.include_router(admin_content.router, tags=["admin-content"])
router.include_router(admin_ops.router, tags=["admin-ops"])
router.include_router(tests.router, tags=["tests"])
router.include_router(attempts.router, tags=["attempts"])
router.include_router(mocks.router, tags=["mocks"])
router.include_router(certificates.router, tags=["certificates"])
router.include_router(examiner.router, tags=["examiner"])
router.include_router(client_events.router, tags=["client-events"])
router.include_router(review.router, tags=["review"])
router.include_router(resources.router, tags=["resources"])
router.include_router(admin_resources.router, tags=["admin-resources"])
