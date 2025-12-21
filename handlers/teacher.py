from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select, func, or_

from db import AsyncSessionLocal
from models import Message as DbMessage, TeacherMessageLink, User
from utils.roles import is_teacher

router = Router()


def _extract_media_info(message: Message):
    """
    Допоміжна функція: дістаємо (text, has_media, media_file_id, media_kind)
    для повідомлення викладача.
    """
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
    media_kind = None

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

    return text, has_media, media_file_id, media_kind


async def _send_to_student_via_bot(message: Message, student_id: int):
    """
    Відправка повідомлення від вчителя учню з підтримкою медіа.
    Повертає sent_message.
    """
    text, has_media, media_file_id, media_kind = _extract_media_info(message)

    if has_media and media_file_id:
        if media_kind == "photo":
            sent = await message.bot.send_photo(
                chat_id=student_id,
                photo=media_file_id,
                caption=text or None,
            )
        elif media_kind == "document":
            sent = await message.bot.send_document(
                chat_id=student_id,
                document=media_file_id,
                caption=text or None,
            )
        elif media_kind == "voice":
            sent = await message.bot.send_voice(
                chat_id=student_id,
                voice=media_file_id,
                caption=text or None,
            )
        elif media_kind == "audio":
            sent = await message.bot.send_audio(
                chat_id=student_id,
                audio=media_file_id,
                caption=text or None,
            )
        elif media_kind == "video":
            sent = await message.bot.send_video(
                chat_id=student_id,
                video=media_file_id,
                caption=text or None,
            )
        else:
            sent = await message.bot.send_message(
                chat_id=student_id,
                text=text,
            )
    else:
        sent = await message.bot.send_message(
            chat_id=student_id,
            text=text,
        )

    return sent, text, has_media, media_file_id


@router.message(F.reply_to_message, ~F.text.startswith("/to"))
async def teacher_reply(message: Message):
    """
    Відповідь вчителя через стандартний Reply на повідомлення,
    яке бот надіслав від учня (з текстом або медіа).
    """
    if not await is_teacher(message.from_user.id):
        return

    replied = message.reply_to_message
    if not replied:
        return

    async with AsyncSessionLocal() as session:
        # шукаємо, якому студенту належало повідомлення, на яке відповіли
        res_link = await session.execute(
            select(TeacherMessageLink).where(
                TeacherMessageLink.teacher_tg_message_id == replied.message_id
            )
        )
        link = res_link.scalar_one_or_none()
        if not link:
            await message.answer("Не вдалося визначити учня для цієї відповіді.")
            return

        res_student = await session.execute(
            select(User).where(User.id == link.student_id)
        )
        student = res_student.scalar_one_or_none()
        if not student:
            await message.answer("Учня не знайдено в базі.")
            return

        # відправляємо учню з підтримкою медіа
        sent, text, has_media, media_file_id = await _send_to_student_via_bot(
            message, student.id
        )

        # лог в БД
        db_msg = DbMessage(
            from_user_id=message.from_user.id,
            to_user_id=student.id,
            direction="teacher_to_student",
            text=text,
            has_media=has_media,
            media_file_id=media_file_id,
            tg_message_id=sent.message_id,
        )
        session.add(db_msg)
        await session.commit()

    await message.answer(
        f"Повідомлення надіслано учню: {student.display_name or student.id}"
    )


@router.message(F.text.startswith("/to"))
async def teacher_to_command(message: Message):
    """
    /to <ім'я_або_id_учня> <текст>
    Важливо: тут підтримуємо текстові повідомлення.
    Для медіа краще використовувати Reply.
    """
    if not await is_teacher(message.from_user.id):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: /to <ім'я_або_id_учня> <текст>")
        return

    _, raw_name, text = parts

    async with AsyncSessionLocal() as session:
        # пошук учня по id або імені (як було раніше)
        # спочатку пробуємо як id
        student = None
        try:
            sid = int(raw_name)
        except ValueError:
            sid = None

        if sid is not None:
            res = await session.execute(
                select(User).where(User.id == sid, User.role == "student")
            )
            student = res.scalar_one_or_none()

        # якщо не знайдено по id — шукаємо по імені
        if not student:
            pattern = f"%{raw_name}%"
            res = await session.execute(
                select(User).where(
                    User.role == "student",
                    or_(
                        func.lower(User.display_name) == raw_name.lower(),
                        User.display_name.ilike(pattern),
                    ),
                )
            )
            students = res.scalars().all()

            if not students:
                await message.answer("Учня з таким ім'ям не знайдено.")
                return
            if len(students) > 1:
                lines = ["Знайдено кілька учнів:"]
                for s in students:
                    lines.append(f"{s.display_name or '—'} — {s.id}")
                lines.append(
                    "Уточніть, будь ласка, за ID або повним ім'ям."
                )
                await message.answer("\n".join(lines))
                return

            student = students[0]

        # тут для простоти в /to працюємо тільки з текстом
        sent = await message.bot.send_message(
            chat_id=student.id,
            text=text,
        )

        db_msg = DbMessage(
            from_user_id=message.from_user.id,
            to_user_id=student.id,
            direction="teacher_to_student",
            text=text,
            has_media=False,
            media_file_id=None,
            tg_message_id=sent.message_id,
        )
        session.add(db_msg)
        await session.commit()

    await message.answer(
        f"Повідомлення надіслано учню: {student.display_name or student.id}"
    )
