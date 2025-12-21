from typing import Optional

from aiogram.types import User as TgUser
from sqlalchemy import select

from db import AsyncSessionLocal
from models import User


async def get_user_role(user_id: int) -> Optional[str]:
    """
    Повертає роль користувача з БД: 'student' / 'teacher' / 'admin' або None,
    якщо користувача ще немає в таблиці users.
    """
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(User.role).where(User.id == user_id)
        )
        row = res.first()
        if row:
            return row[0]
    return None


async def is_admin(user_id: int) -> bool:
    role = await get_user_role(user_id)
    return role == "admin"


async def is_teacher(user_id: int) -> bool:
    role = await get_user_role(user_id)
    return role == "teacher"


async def is_student(user_id: int) -> bool:
    role = await get_user_role(user_id)
    return role == "student"


async def get_or_create_user(
    tg_user: TgUser,
    role: str = "student",
) -> User:
    """
    Дістає користувача з БД або створює нового.
    ВАЖЛИВО: якщо користувач уже є, ми НЕ міняємо йому роль тут.
    Роль змінюється тільки адмінськими командами (/add_teacher, /add_admin тощо).
    """
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(User).where(User.id == tg_user.id)
        )
        user = res.scalar_one_or_none()

        if user:
            return user

        user = User(
            id=tg_user.id,
            role=role,
            display_name=tg_user.full_name,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user
