# utils/teacher_selector.py

from sqlalchemy import select

from config import get_settings
from db import AsyncSessionLocal
from models import User, Group

settings = get_settings()


async def select_teacher_for_student(student: User) -> int:
    """
    Логіка:
    1. Якщо у студента є group_id і в групи є teacher_id — повертаємо його.
    2. Інакше – пробуємо знайти будь-якого користувача з role='teacher' у БД.
    """

    # 1) пробуємо взяти вчителя з групи
    if student.group_id:
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
