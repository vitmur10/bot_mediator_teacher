from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select

from db import AsyncSessionLocal
from models import Message as DbMessage, TeacherMessageLink
from utils.roles import is_teacher, is_admin, get_or_create_user
from utils.teacher_selector import select_teacher_for_student
from config import get_settings
from aiogram.utils.keyboard import InlineKeyboardBuilder
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

    # ---- Формуємо заголовок для викладача ----
    display = student.display_name or f"ID {student.id}"
    header = f"👤 Учень: {display} (id: {student.id})"
    body = text or ""
    full_text = f"{header}\n\n{body}" if body else header

    # Кнопка "Історія з цим учнем"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📜 Історія з цим учнем",
        callback_data=f"hist:{student.id}:0",
    )
    kb.adjust(1)

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
                    caption=full_text if len(full_text) <= 1024 else full_text[:1020] + "…",
                    reply_markup=kb.as_markup(),
                )
            elif media_kind == "document":
                sent = await message.bot.send_document(
                    chat_id=teacher_id,
                    document=media_file_id,
                    caption=full_text if len(full_text) <= 1024 else full_text[:1020] + "…",
                    reply_markup=kb.as_markup(),
                )
            elif media_kind == "voice":
                sent = await message.bot.send_voice(
                    chat_id=teacher_id,
                    voice=media_file_id,
                    caption=full_text if len(full_text) <= 1024 else full_text[:1020] + "…",
                    reply_markup=kb.as_markup(),
                )
            elif media_kind == "audio":
                sent = await message.bot.send_audio(
                    chat_id=teacher_id,
                    audio=media_file_id,
                    caption=full_text if len(full_text) <= 1024 else full_text[:1020] + "…",
                    reply_markup=kb.as_markup(),
                )
            elif media_kind == "video":
                sent = await message.bot.send_video(
                    chat_id=teacher_id,
                    video=media_file_id,
                    caption=full_text if len(full_text) <= 1024 else full_text[:1020] + "…",
                    reply_markup=kb.as_markup(),
                )
            else:
                sent = await message.bot.send_message(
                    chat_id=teacher_id,
                    text=full_text,
                    reply_markup=kb.as_markup(),
                )
        else:
            # тільки текст
            sent = await message.bot.send_message(
                chat_id=teacher_id,
                text=full_text,
                reply_markup=kb.as_markup(),
            )

        db_msg.tg_message_id = sent.message_id

        link = TeacherMessageLink(
            teacher_tg_message_id=sent.message_id,
            student_id=student.id,
        )
        session.add(link)

        await session.commit()

