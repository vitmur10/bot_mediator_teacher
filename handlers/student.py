from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select

from db import AsyncSessionLocal
from models import Message as DbMessage, TeacherMessageLink
from utils.roles import is_teacher, is_admin, get_or_create_user
from utils.teacher_selector import select_teacher_for_student
from config import get_settings

router = Router()
settings = get_settings()


@router.message(F.chat.type == "private")
async def student_message(message: Message):
    print(3)
    # Якщо це пише вчитель або адмін – нехай обробляють інші хендлери
    if await is_teacher(message.from_user.id) or await is_admin(message.from_user.id):
        return

    student = await get_or_create_user(message.from_user, role="student")
    teacher_id = await select_teacher_for_student(student)

    text = message.text or message.caption or ""
    has_media = bool(
        message.photo
        or message.document
        or message.voice
        or message.audio
        or message.video
    )
    media_file_id = None
    if message.photo:
        media_file_id = message.photo[-1].file_id
    elif message.document:
        media_file_id = message.document.file_id

    async with AsyncSessionLocal() as session:
        db_msg = DbMessage(
            from_user_id=student.id,
            to_user_id=teacher_id,
            direction="student_to_teacher",
            text=text,
            has_media=has_media,
            media_file_id=media_file_id,
        )
        session.add(db_msg)
        await session.flush()

        caption = f"{text}"
        sent = await message.bot.send_message(
            chat_id=teacher_id,
            text=caption,
        )

        db_msg.tg_message_id = sent.message_id

        link = TeacherMessageLink(
            teacher_tg_message_id=sent.message_id,
            student_id=student.id,
        )
        session.add(link)

        await session.commit()

