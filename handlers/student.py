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
    # Якщо це пише вчитель або адмін – нехай обробляють інші хендлери
    if await is_teacher(message.from_user.id) or await is_admin(message.from_user.id):
        return

    student = await get_or_create_user(message.from_user, role="student")
    teacher_id = await select_teacher_for_student(student)

    # ---- ВИЗНАЧАЄМО ТЕКСТ І МЕДІА ----
    text = message.text or message.caption or ""

    if not text:
        if message.voice:
            text = "🎤 Голосове"
        elif message.audio:
            text = "🎵 Аудіо"
        elif message.video:
            text = "🎬 Відео"
        elif message.document:
            text = "📄 Документ"
        elif message.photo:
            text = "📷 Фото"

    has_media = bool(
        message.photo
        or message.document
        or message.voice
        or message.audio
        or message.video
    )

    media_file_id = None
    media_kind = None  # просто для відправки, у БД окремо не зберігаємо

    if message.photo:
        media_file_id = message.photo[-1].file_id
        media_kind = "photo"
    elif message.document:
        media_file_id = message.document.file_id
        media_kind = "document"
    elif message.voice:
        media_file_id = message.voice.file_id
        media_kind = "voice"
    elif message.audio:
        media_file_id = message.audio.file_id
        media_kind = "audio"
    elif message.video:
        media_file_id = message.video.file_id
        media_kind = "video"

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

        # ---- ВІДПРАВЛЯЄМО ВЧИТЕЛЮ ЗБЕРІГАЮЧИ МЕДІА ----
        if has_media and media_file_id:
            if media_kind == "photo":
                sent = await message.bot.send_photo(
                    chat_id=teacher_id,
                    photo=media_file_id,
                    caption=text or None,
                )
            elif media_kind == "document":
                sent = await message.bot.send_document(
                    chat_id=teacher_id,
                    document=media_file_id,
                    caption=text or None,
                )
            elif media_kind == "voice":
                sent = await message.bot.send_voice(
                    chat_id=teacher_id,
                    voice=media_file_id,
                    caption=text or None,
                )
            elif media_kind == "audio":
                sent = await message.bot.send_audio(
                    chat_id=teacher_id,
                    audio=media_file_id,
                    caption=text or None,
                )
            elif media_kind == "video":
                sent = await message.bot.send_video(
                    chat_id=teacher_id,
                    video=media_file_id,
                    caption=text or None,
                )
            else:
                # на всякий випадок fallback — як текст
                sent = await message.bot.send_message(
                    chat_id=teacher_id,
                    text=text,
                )
        else:
            # тільки текст
            sent = await message.bot.send_message(
                chat_id=teacher_id,
                text=text,
            )

        db_msg.tg_message_id = sent.message_id

        link = TeacherMessageLink(
            teacher_tg_message_id=sent.message_id,
            student_id=student.id,
        )
        session.add(link)

        await session.commit()

