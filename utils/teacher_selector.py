# utils/teacher_selector.py

from sqlalchemy import select

from config import get_settings
from db import AsyncSessionLocal
from models import User, Group

settings = get_settings()


async def select_teacher_for_student(student: User) -> int:
    """
    Логіка:
    0. Якщо у студента є прямий викладач (assigned_teacher_id) — повертаємо його.
    1. Якщо у студента є group_id і в групи є teacher_id — повертаємо його.
    2. Якщо ні — шукаємо будь-якого teacher у БД.
    """

    # 0) прямий викладач 1-на-1
    if getattr(student, "assigned_teacher_id", None):
        return student.assigned_teacher_id

    # 1) вчитель з групи
    if getattr(student, "group_id", None):
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Group).where(Group.id == student.group_id)
            )
            group = res.scalar_one_or_none()
            if group and group.teacher_id:
                return group.teacher_id

    # 2) fallback – перший teacher з БД
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(User.id).where(User.role == "teacher")
        )
        row = res.first()
        if row:
            return row[0]

    raise RuntimeError("Teacher is not configured")
