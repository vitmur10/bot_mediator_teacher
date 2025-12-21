from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from utils.roles import is_teacher, is_admin, get_or_create_user

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    if await is_teacher(message.from_user.id):
        role = "teacher"
    elif await is_admin(message.from_user.id):
        role = "admin"
    else:
        role = "student"

    await get_or_create_user(message.from_user, role=role)
    await message.answer("Бот-посередник готовий до роботи.")
