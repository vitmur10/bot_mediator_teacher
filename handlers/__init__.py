from aiogram import Router

from . import common, student, teacher, admin


def setup_routers() -> Router:
    router = Router()
    router.include_router(common.router)
    router.include_router(teacher.router)
    router.include_router(admin.router)
    router.include_router(student.router)
    return router
