from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select, func, or_

from db import AsyncSessionLocal
from models import Message as DbMessage, TeacherMessageLink, User
from utils.roles import is_teacher

router = Router()


@router.message(F.reply_to_message, ~F.text.startswith("/to"))
async def teacher_reply(message: Message):
    """
    Відповідь вчителя через стандартний Reply на повідомлення,
    яке бот переслав від учня.
    """
    if not await is_teacher(message.from_user.id):
        return

    replied_id = message.reply_to_message.message_id

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(TeacherMessageLink).where(
                TeacherMessageLink.teacher_tg_message_id == replied_id
            )
        )
        link = res.scalar_one_or_none()
        if not link:
            await message.answer("Не вдалося визначити учня для цієї відповіді.")
            return

        student_id = link.student_id

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

        db_msg = DbMessage(
            from_user_id=message.from_user.id,
            to_user_id=student_id,
            direction="teacher_to_student",
            text=text,
            has_media=has_media,
            media_file_id=media_file_id,
            replied_to_tg_message_id=replied_id,
        )
        session.add(db_msg)
        await session.commit()

    await message.bot.send_message(chat_id=student_id, text=text)


@router.message(F.text.startswith("/to"))
async def teacher_to_command(message: Message):
    """
    Команда /to <name> <текст> для вчителя.

    Приклади:
    /to Анна Привіт, як справи?
    /to Петренко Завтра буде контрольна
    /to 123456789 Привіт по user_id
    """
    if not await is_teacher(message.from_user.id):
        return

    if not message.text:
        return

    parts = message.text.split(maxsplit=2)

    if len(parts) < 3:
        await message.answer("Формат команди: /to <ім'я_учня або user_id> <текст повідомлення>")
        return

    _, raw_name, text = parts
    text = text.strip()
    if not text:
        await message.answer("Порожнє повідомлення. Додайте текст після імені учня.")
        return

    # пробуємо інтерпретувати як user_id
    raw_id_int = None
    try:
        raw_id_int = int(raw_name)
    except ValueError:
        raw_id_int = None

    async with AsyncSessionLocal() as session:
        # базова умова: тільки студенти
        base_query = select(User).where(User.role == "student")

        # спочатку точний збіг по імені (без регістру)
        res_exact = await session.execute(
            base_query.where(func.lower(User.display_name) == raw_name.lower())
        )
        exact_matches = res_exact.scalars().all()

        candidate_students: list[User] = []

        if exact_matches:
            candidate_students = exact_matches
        else:
            # якщо точного збігу немає – пробуємо "як містить"
            like_pattern = f"%{raw_name}%"
            conditions = [User.display_name.ilike(like_pattern)]
            # якщо sirng був числом – додаємо варіант пошуку по id
            if raw_id_int is not None:
                conditions.append(User.id == raw_id_int)

            res_like = await session.execute(
                base_query.where(or_(*conditions))
            )
            candidate_students = res_like.scalars().all()

    if not candidate_students:
        await message.answer(
            "Учня з таким ім'ям або user_id не знайдено. Перевірте написання або використайте відповідь через Reply."
        )
        return

    if len(candidate_students) > 1:
        # кілька збігів – показуємо список, щоб вчитель розумів, чому команда не відпрацювала однозначно
        lines = ["Знайдено кілька учнів, команда /to не може вибрати одного:"]
        for u in candidate_students:
            name = u.display_name or "—"
            lines.append(f"{u.id} — {name}")
        lines.append(
            "Уточніть ім'я або використайте Reply на повідомленні потрібного учня."
        )
        await message.answer("\n".join(lines))
        return

    student = candidate_students[0]

    # надсилаємо повідомлення учню
    sent = await message.bot.send_message(chat_id=student.id, text=text)

    # лог у БД
    async with AsyncSessionLocal() as session:
        db_msg = DbMessage(
            from_user_id=message.from_user.id,
            to_user_id=student.id,
            direction="teacher_to_student",
            text=text,
            has_media=False,
            media_file_id=None,
        )
        session.add(db_msg)
        await session.commit()

    await message.answer(
        f"Повідомлення надіслано учню: {student.display_name or student.id}"
    )
