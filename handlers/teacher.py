from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select, func, or_
from db import AsyncSessionLocal
from models import Message as DbMessage, TeacherMessageLink, User
from utils.roles import is_teacher
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

router = Router()
PENDING_TO_STUDENT: dict[int, int] = {}
TO_STUDENTS_PER_PAGE = 10


class ToState(StatesGroup):
    waiting_message = State()


async def _render_to_students_page(target_message: Message, teacher_id: int, page: int = 0) -> None:
    """
    Список учнів для команди /to з пагінацією.
    Зараз показуємо всіх студентів; за бажанням можна відфільтрувати тільки
    "своїх" (по групах/assigned_teacher).
    """
    async with AsyncSessionLocal() as session:
        count_res = await session.execute(
            select(func.count(User.id)).where(User.role == "student")
        )
        total_students = count_res.scalar() or 0

        if total_students == 0:
            await target_message.edit_text("Учнів поки немає.", reply_markup=None)
            return

        total_pages = (total_students + TO_STUDENTS_PER_PAGE - 1) // TO_STUDENTS_PER_PAGE
        if page < 0:
            page = 0
        if page >= total_pages:
            page = total_pages - 1

        offset = page * TO_STUDENTS_PER_PAGE

        res = await session.execute(
            select(User)
            .where(User.role == "student")
            .order_by(User.display_name)
            .offset(offset)
            .limit(TO_STUDENTS_PER_PAGE)
        )
        students = res.scalars().all()

    kb = InlineKeyboardBuilder()
    for u in students:
        name = u.display_name or f"ID {u.id}"
        kb.button(
            text=name,
            callback_data=f"to_sel:{u.id}:{page}",
        )
    kb.adjust(1)

    # пагінація
    if page > 0:
        kb.button(
            text="⬅️ Попередня сторінка",
            callback_data=f"to_page:{page - 1}",
        )
    if page < total_pages - 1:
        kb.button(
            text="➡️ Наступна сторінка",
            callback_data=f"to_page:{page + 1}",
        )

    kb.adjust(1)

    header = f"Оберіть учня для відповіді (сторінка {page + 1}/{total_pages}):"
    await target_message.edit_text(header, reply_markup=kb.as_markup())


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
    Варіанти:
    1) /to <ім'я_або_id_учня> <текст>  — як раніше, одразу надсилаємо.
    2) /to                            — показати список учнів інлайн-кнопками,
                                        обрати учня, а потім надіслати йому текст.
    """
    if not await is_teacher(message.from_user.id):
        return

    parts = message.text.split(maxsplit=2)

    # Випадок 2: лише "/to" без аргументів – запускаємо режим вибору учня
    if len(parts) == 1:
        sent = await message.answer("Завантаження списку учнів...")
        await _render_to_students_page(sent, teacher_id=message.from_user.id, page=0)
        return

    # Випадок 1: старий формат /to <name_or_id> <text>
    if len(parts) < 3:
        await message.answer("Формат: /to <ім'я_або_id_учня> <текст>")
        return

    _, raw_name, text = parts

    async with AsyncSessionLocal() as session:
        # спроба як id
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
                lines.append("Уточніть, будь ласка, за ID або повним ім'ям.")
                await message.answer("\n".join(lines))
                return

            student = students[0]

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


@router.callback_query(F.data.startswith("to_page:"))
async def teacher_to_page_callback(callback: CallbackQuery):
    if not await is_teacher(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = callback.data or ""
    try:
        _, raw_page = data.split(":", 1)
        page = int(raw_page)
    except Exception:
        await callback.answer("Помилка пагінації.", show_alert=True)
        return

    await _render_to_students_page(callback.message, teacher_id=callback.from_user.id, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("to_sel:"))
async def teacher_to_select_student(callback: CallbackQuery, state: FSMContext):
    if not await is_teacher(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = callback.data or ""
    try:
        _, raw_student_id, raw_page = data.split(":", 2)
        student_id = int(raw_student_id)
    except Exception:
        await callback.answer("Некоректні дані кнопки.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(User).where(User.id == student_id, User.role == "student")
        )
        student = res.scalar_one_or_none()

    if not student:
        await callback.answer("Учня не знайдено.", show_alert=True)
        return

    # Зберігаємо в FSM і вмикаємо режим очікування
    await state.update_data(to_student_id=student_id)
    await state.set_state(ToState.waiting_message)

    await callback.message.edit_text(
        f"Учень обраний: {student.display_name or student.id}.\n"
        f"Надішліть наступним повідомленням текст/файл/голосове — я перешлю його цьому учню.\n"
        f"Скасувати: /to_cancel",
        reply_markup=None,
    )
    await callback.answer("Учень обраний.")


@router.message(ToState.waiting_message, F.chat.type == "private")
async def teacher_free_text_after_to(message: Message, state: FSMContext):
    if not await is_teacher(message.from_user.id):
        return

    # Якщо це команда або reply — нехай обробляють інші хендлери
    if message.text and message.text.startswith("/"):
        return
    if message.reply_to_message:
        return

    data = await state.get_data()
    student_id = data.get("to_student_id")
    if not student_id:
        # на всякий випадок
        await state.clear()
        return

    async with AsyncSessionLocal() as session:
        res_student = await session.execute(select(User).where(User.id == student_id))
        student = res_student.scalar_one_or_none()
        if not student:
            await message.answer("Учня не знайдено в базі.")
            await state.clear()
            return

        sent, text, has_media, media_file_id = await _send_to_student_via_bot(message, student.id)

        session.add(DbMessage(
            from_user_id=message.from_user.id,
            to_user_id=student.id,
            direction="teacher_to_student",
            text=text,
            has_media=has_media,
            media_file_id=media_file_id,
            tg_message_id=sent.message_id,
        ))
        await session.commit()

    await message.answer(f"Повідомлення надіслано учню: {student.display_name or student.id}")

    # очищаємо стан
    await state.clear()


@router.message(F.text == "/to_cancel")
async def teacher_to_cancel(message: Message, state: FSMContext):
    if not await is_teacher(message.from_user.id):
        return
    await state.clear()
    await message.answer("Режим /to скасовано.")
