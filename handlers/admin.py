from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, or_, delete
from config import get_settings
from db import AsyncSessionLocal
from models import Message as DbMessage, User, Group, TeacherMessageLink
from utils.roles import is_admin, is_teacher
from aiogram.exceptions import TelegramBadRequest

router = Router()
settings = get_settings()
STUDENTS_PAGE_SIZE = 10
STUDENTS_PER_PAGE = 10
MSGS_PER_PAGE = 20
UNASSIGNED_PER_PAGE = 10
TEACHERS_PER_PAGE = 10
ADMINS_PER_PAGE = 10
DEL_STUDENTS_PER_PAGE = 10
TLINK_TEACHERS_PER_PAGE = 10
TLINK_STUDENTS_PER_PAGE = 10
TEACHER_STUDENTS_PER_PAGE = 10


def _norm(s: str) -> str:
    return (s or "").strip().lower()


async def _find_students_by_query(session, q: str):
    """
    q: id або частина імені
    повертає список студентів (може бути 0/1/багато)
    """
    q = q.strip()
    # пробуємо як id
    try:
        sid = int(q)
    except ValueError:
        sid = None

    if sid is not None:
        res = await session.execute(
            select(User).where(User.id == sid, User.role == "student")
        )
        st = res.scalar_one_or_none()
        return [st] if st else []

    # інакше пошук по імені
    pattern = f"%{q}%"
    res = await session.execute(
        select(User).where(
            User.role == "student",
            User.display_name.is_not(None),
            or_(
                func.lower(User.display_name) == q.lower(),
                User.display_name.ilike(pattern),
            ),
        ).order_by(User.display_name)
    )
    return res.scalars().all()


async def safe_edit_message(msg: Message, text: str, reply_markup=None):
    """
    Якщо msg текстове -> edit_text
    Якщо msg медіа (має caption або content_type != 'text') -> edit_caption
    Якщо редагування неможливе -> надсилаємо нове повідомлення
    """
    try:
        # Якщо це звичайне текстове повідомлення
        if msg.content_type == "text":
            return await msg.edit_text(text, reply_markup=reply_markup)

        # Якщо це медіа (photo/video/document/voice/audio...)
        return await msg.edit_caption(text, reply_markup=reply_markup)

    except TelegramBadRequest:
        # fallback: надіслати нове повідомлення (щоб не падало)
        return await msg.answer(text, reply_markup=reply_markup)


async def _render_students_toggle_page(target_message: Message, page: int = 0):
    """
    Список студентів з кнопками Activate/Deactivate.
    """
    async with AsyncSessionLocal() as session:
        count_res = await session.execute(
            select(func.count(User.id)).where(User.role == "student")
        )
        total = count_res.scalar() or 0

        if total == 0:
            await target_message.edit_text("Студентів поки немає.", reply_markup=None)
            return

        total_pages = (total + STUDENTS_PAGE_SIZE - 1) // STUDENTS_PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        offset = page * STUDENTS_PAGE_SIZE

        res = await session.execute(
            select(User)
            .where(User.role == "student")
            .order_by(User.display_name)
            .offset(offset)
            .limit(STUDENTS_PAGE_SIZE)
        )
        students = res.scalars().all()

    kb = InlineKeyboardBuilder()

    for s in students:
        name = s.display_name or f"ID {s.id}"
        status = "✅ active" if getattr(s, "is_active", True) else "⛔ inactive"
        # Заголовок-рядок (не кнопка)
        kb.button(text=f"👤 {name} — {status}", callback_data="noop")

        # Кнопки дії
        if getattr(s, "is_active", True):
            kb.button(text="🛑 Deactivate", callback_data=f"stu_deact:{s.id}:{page}")
        else:
            kb.button(text="✅ Activate", callback_data=f"stu_act:{s.id}:{page}")

        kb.adjust(1, 1)  # заголовок, потім кнопка

    # пагінація
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"stu_page:{page - 1}")
    nav.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
    if page < total_pages - 1:
        nav.button(text="➡️", callback_data=f"stu_page:{page + 1}")
    nav.adjust(3)

    # додаємо рядок навігації в кінець
    for row in nav.export():
        kb.row(*row)

    await target_message.edit_text(
        "Оберіть студента для активації/деактивації:",
        reply_markup=kb.as_markup(),
    )


def _sname(u: User) -> str:
    return (u.display_name or f"ID {u.id}").strip()


def _status_emoji(active: bool) -> str:
    return "🟢" if active else "🔴"


def _format_dt(dt: datetime | None) -> str:
    """
    Форматування дати/часу для виводу в історії:
    19.12 22:31
    """
    if not dt:
        return ""
    return dt.strftime("%d.%m %H:%M")


@router.callback_query(F.data == "noop")
async def _noop(callback: CallbackQuery):
    await callback.answer()


async def _set_student_active(student_id: int, is_active: bool) -> tuple[bool, str]:
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(User).where(User.id == student_id, User.role == "student")
        )
        st = res.scalar_one_or_none()
        if not st:
            return False, "Студента не знайдено."

        if not hasattr(st, "is_active"):
            return False, "У моделі User немає поля is_active."

        st.is_active = is_active
        await session.commit()
        return True, f"{st.display_name or st.id}"


async def _render_teacher_students_page(target_message: Message, teacher_id: int, page: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        teacher = (await session.execute(
            select(User).where(User.id == teacher_id, User.role == "teacher")
        )).scalar_one_or_none()

        if not teacher:
            await target_message.edit_text("Викладача не знайдено.", reply_markup=None)
            return

        # Учні викладача:
        # - assigned_teacher_id = teacher_id
        # - або student.group_id in groups where teacher_id=teacher_id
        group_ids = (await session.execute(
            select(Group.id).where(Group.teacher_id == teacher_id)
        )).scalars().all()

        base_q = select(User).where(
            User.role == "student",
            (
                    (User.assigned_teacher_id == teacher_id)
                    | (User.group_id.in_(group_ids) if group_ids else False)
            )
        )

        count_res = await session.execute(select(func.count()).select_from(base_q.subquery()))
        total = count_res.scalar() or 0

        if total == 0:
            kb = InlineKeyboardBuilder()
            kb.button(text="⬅️ Назад до викладачів", callback_data="tch_page:0")
            await target_message.edit_text(
                f"{_sname(teacher)}\nУчнів поки немає.",
                reply_markup=kb.as_markup(),
            )
            return

        total_pages = (total + TEACHER_STUDENTS_PER_PAGE - 1) // TEACHER_STUDENTS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        offset = page * TEACHER_STUDENTS_PER_PAGE

        students = (await session.execute(
            base_q.order_by(User.display_name).offset(offset).limit(TEACHER_STUDENTS_PER_PAGE)
        )).scalars().all()

    kb = InlineKeyboardBuilder()
    for s in students:
        kb.button(
            text=f"{_status_emoji(s.is_active)} {_sname(s)}",
            callback_data=f"stu:{teacher_id}:{s.id}:{page}",
        )
    kb.adjust(1)

    # pagination
    if page > 0:
        kb.button(text="⬅️ Назад", callback_data=f"tch:{teacher_id}:{page - 1}")
    if page < total_pages - 1:
        kb.button(text="➡️ Далі", callback_data=f"tch:{teacher_id}:{page + 1}")

    kb.button(text="⬅️ До списку викладачів", callback_data="tch_page:0")
    kb.adjust(1)

    await target_message.edit_text(
        f"👨‍🏫 {_sname(teacher)}\nУчні (сторінка {page + 1}/{total_pages}):",
        reply_markup=kb.as_markup(),
    )


async def _find_teachers_by_identifier(session, identifier: str) -> list[User]:
    """
    Пошук вчителя по:
    - точному telegram id
    - точному display_name (без регістру)
    - display_name, що містить підрядок
    """
    candidates: list[User] = []

    # спроба як ID
    try:
        tid = int(identifier)
    except ValueError:
        tid = None

    if tid is not None:
        res = await session.execute(
            select(User).where(User.id == tid, User.role == "teacher")
        )
        teacher = res.scalar_one_or_none()
        if teacher:
            candidates.append(teacher)

    if candidates:
        return candidates

    # точний збіг по імені
    res = await session.execute(
        select(User).where(
            User.role == "teacher",
            func.lower(User.display_name) == identifier.lower(),
        )
    )
    exact = res.scalars().all()
    if exact:
        return exact

    # "як містить"
    pattern = f"%{identifier}%"
    res = await session.execute(
        select(User).where(
            User.role == "teacher",
            User.display_name.ilike(pattern),
        )
    )
    return res.scalars().all()


async def _find_students_by_identifier(session, identifier: str) -> list[User]:
    """
    Пошук учня по:
    - telegram id
    - точному display_name
    - display_name, що містить підрядок
    """
    candidates: list[User] = []

    try:
        sid = int(identifier)
    except ValueError:
        sid = None

    if sid is not None:
        res = await session.execute(
            select(User).where(User.id == sid, User.role == "student")
        )
        student = res.scalar_one_or_none()
        if student:
            candidates.append(student)

    if candidates:
        return candidates

    res = await session.execute(
        select(User).where(
            User.role == "student",
            func.lower(User.display_name) == identifier.lower(),
        )
    )
    exact = res.scalars().all()
    if exact:
        return exact

    pattern = f"%{identifier}%"
    res = await session.execute(
        select(User).where(
            User.role == "student",
            User.display_name.ilike(pattern),
        )
    )
    return res.scalars().all()


async def _find_any_users_by_identifier(session, identifier: str) -> list[User]:
    """
    Пошук користувача по:
    - telegram id
    - точному display_name
    - display_name, що містить підрядок
    """
    candidates: list[User] = []

    # пробуємо як telegram id
    try:
        uid = int(identifier)
    except ValueError:
        uid = None

    if uid is not None:
        res = await session.execute(
            select(User).where(User.id == uid)
        )
        user = res.scalar_one_or_none()
        if user:
            candidates.append(user)

    if candidates:
        return candidates

    # точний збіг по імені (без регістру)
    res = await session.execute(
        select(User).where(func.lower(User.display_name) == identifier.lower())
    )
    exact = res.scalars().all()
    if exact:
        return exact

    # "як містить"
    pattern = f"%{identifier}%"
    res = await session.execute(
        select(User).where(User.display_name.ilike(pattern))
    )
    return res.scalars().all()


async def _render_students_page(target_message: Message, page: int = 0, teacher_id: int | None = None):
    async with AsyncSessionLocal() as session:
        q_count = select(func.count(User.id)).where(User.role == "student")
        if teacher_id is not None:
            q_count = q_count.where(User.assigned_teacher_id == teacher_id)

        total_students = (await session.execute(q_count)).scalar() or 0
        if total_students == 0:
            await target_message.edit_text("Учнів поки немає.", reply_markup=None)
            return

        total_pages = (total_students + STUDENTS_PER_PAGE - 1) // STUDENTS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        offset = page * STUDENTS_PER_PAGE

        q_list = (
            select(User)
            .where(User.role == "student")
            .order_by(User.display_name)
            .offset(offset)
            .limit(STUDENTS_PER_PAGE)
        )
        if teacher_id is not None:
            q_list = q_list.where(User.assigned_teacher_id == teacher_id)

        students = (await session.execute(q_list)).scalars().all()

    kb = InlineKeyboardBuilder()

    for s in students:
        name = s.display_name or f"ID {s.id}"
        status_icon = "✅" if getattr(s, "is_active", True) else "⛔️"
        kb.button(
            text=f"{name} {status_icon}",
            callback_data=f"hist:{s.id}:0",  # <-- ОЦЕ ГОЛОВНЕ
        )

    # пагінація списку учнів
    if page > 0:
        kb.button(text="⬅️ Попередня", callback_data=f"stud_page:{page - 1}")
    if page < total_pages - 1:
        kb.button(text="➡️ Наступна", callback_data=f"stud_page:{page + 1}")

    kb.adjust(1)

    title = f"Список учнів (сторінка {page + 1}/{total_pages}):"
    await target_message.edit_text(title, reply_markup=kb.as_markup())


MEDIA_EMOJI = {
    "photo": "📷 Фото",
    "document": "📄 Документ",
    "voice": "🎤 Голосове",
    "audio": "🎵 Аудіо",
    "video": "🎬 Відео",
}


async def _render_history_page(target_message: Message, student_id: int, page: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        total_msgs = (await session.execute(
            select(func.count(DbMessage.id)).where(
                (DbMessage.from_user_id == student_id) | (DbMessage.to_user_id == student_id)
            )
        )).scalar() or 0

        if total_msgs == 0:
            await target_message.edit_text("Історія для цього користувача порожня.", reply_markup=None)
            return

        total_pages = (total_msgs + MSGS_PER_PAGE - 1) // MSGS_PER_PAGE
        page = max(0, min(page, total_pages - 1))

        # ВАЖЛИВО: беремо блок з кінця
        start_from_end = max(total_msgs - (page + 1) * MSGS_PER_PAGE, 0)

        msgs = (await session.execute(
            select(DbMessage)
            .where((DbMessage.from_user_id == student_id) | (DbMessage.to_user_id == student_id))
            .order_by(DbMessage.created_at)
            .offset(start_from_end)
            .limit(MSGS_PER_PAGE)
        )).scalars().all()

        student = (await session.execute(select(User).where(User.id == student_id))).scalar_one_or_none()

    header_name = student.display_name if student and student.display_name else str(student_id)

    lines = [
        f"Історія діалогу з учнем: {header_name}",
        f"Сторінка {page + 1}/{total_pages}",
        "",
    ]

    for m in msgs:
        ts = _format_dt(m.created_at)
        direction = "Учень → Вчитель" if m.direction == "student_to_teacher" else "Вчитель → Учень"
        prefix = f"[{ts}] {direction}"

        body = m.text or ""
        if m.has_media and m.media_file_id:
            kind = getattr(m, "media_kind", None)
            body_media = MEDIA_EMOJI.get(kind, "📎 Медіа")
            body = f"{body}\n{body_media}".strip() if body else body_media

        lines.append(f"{prefix}\n{body}\n")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]

    kb = InlineKeyboardBuilder()

    # Новіші/старіші (page=0 найновіші)
    if page > 0:
        kb.button(text="➡️ Новіші", callback_data=f"hist:{student_id}:{page - 1}")
    if page < total_pages - 1:
        kb.button(text="⬅️ Старіші", callback_data=f"hist:{student_id}:{page + 1}")

    kb.button(text="⬅️ Назад до списку учнів", callback_data="stud_page:0")
    kb.button(text="📎 Показати медіа зі сторінки", callback_data=f"hist_media:{student_id}:{page}")
    kb.adjust(1)

    await target_message.edit_text(text or "Немає повідомлень.", reply_markup=kb.as_markup())


async def _render_assign_group_page(
        target_message: Message,
        group_id: int,
        page: int = 0,
) -> None:
    """
    Малює список студентів без групи для прив'язки до group_id.
    Викликається з /assign_group та з callback'ів asgpage:...
    """
    async with AsyncSessionLocal() as session:
        # рахуємо скільки студентів без групи
        count_res = await session.execute(
            select(func.count(User.id)).where(
                User.role == "student",
                User.group_id.is_(None),
            )
        )
        total_students = count_res.scalar() or 0

        if total_students == 0:
            await target_message.edit_text(
                f"Немає учнів без групи.\nГрупа id={group_id}.",
                reply_markup=None,
            )
            return

        total_pages = (total_students + UNASSIGNED_PER_PAGE - 1) // UNASSIGNED_PER_PAGE
        if page < 0:
            page = 0
        if page >= total_pages:
            page = total_pages - 1

        offset = page * UNASSIGNED_PER_PAGE

        res = await session.execute(
            select(User)
            .where(User.role == "student", User.group_id.is_(None))
            .order_by(User.display_name)
            .offset(offset)
            .limit(UNASSIGNED_PER_PAGE)
        )
        students = res.scalars().all()

    kb = InlineKeyboardBuilder()

    for u in students:
        name = u.display_name or f"ID {u.id}"
        # при кліку – додати учня до групи
        kb.button(
            text=name,
            callback_data=f"asg:{group_id}:{u.id}",
        )

    kb.adjust(1)

    # пагінація
    if page > 0:
        kb.button(
            text="⬅️ Попередня сторінка",
            callback_data=f"asgpage:{group_id}:{page - 1}",
        )
    if page < total_pages - 1:
        kb.button(
            text="➡️ Наступна сторінка",
            callback_data=f"asgpage:{group_id}:{page + 1}",
        )

    # “закінчити”
    kb.button(
        text="✅ Закінчити",
        callback_data=f"asgdone:{group_id}",
    )

    kb.adjust(1)

    header = (
        f"Група id={group_id}\n"
        f"Оберіть учнів для цієї групи (сторінка {page + 1}/{total_pages}):"
    )

    await target_message.edit_text(
        header,
        reply_markup=kb.as_markup(),
    )


@router.message(F.text.startswith("/students"))
async def admin_students(message: Message):
    """
    /students – список учнів інлайн-кнопками з пагінацією.
    Адмін: всі учні
    Викладач: тільки свої (assigned_teacher_id == teacher_id)
    """
    user_id = message.from_user.id

    is_adm = await is_admin(user_id)
    is_tch = await is_teacher(user_id)

    if not (is_adm or is_tch):
        return

    sent = await message.answer("Завантаження списку учнів...")

    # якщо викладач — передаємо teacher_id, щоб фільтрувало "своїх"
    teacher_id = user_id if is_tch and not is_adm else None

    await _render_students_page(sent, page=0, teacher_id=teacher_id)


@router.callback_query(F.data.startswith("stud_page:"))
async def students_page_callback(callback: CallbackQuery):
    user_id = callback.from_user.id

    is_adm = await is_admin(user_id)
    is_tch = await is_teacher(user_id)
    if not (is_adm or is_tch):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_page = (callback.data or "").split(":", 1)
        page = int(raw_page)
    except Exception:
        await callback.answer("Помилка пагінації.", show_alert=True)
        return

    teacher_id = user_id if is_tch and not is_adm else None
    await _render_students_page(callback.message, page=page, teacher_id=teacher_id)
    await callback.answer()



@router.callback_query(F.data.startswith("hist:"))
async def admin_history_callback(callback: CallbackQuery):
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    is_adm = await is_admin(user_id)
    is_tch = await is_teacher(user_id)
    if not (is_adm or is_tch):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_student_id, raw_page = (callback.data or "").split(":", 2)
        student_id = int(raw_student_id)
        page = int(raw_page)
    except Exception:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    # якщо це викладач (і не адмін) — перевіряємо прив'язку
    if is_tch and not is_adm:
        async with AsyncSessionLocal() as session:
            s = (await session.execute(select(User).where(User.id == student_id))).scalar_one_or_none()
            if not s or s.assigned_teacher_id != user_id:
                await callback.answer("Цей учень не закріплений за вами.", show_alert=True)
                return

    target = await callback.message.answer("Завантажую історію...")
    await _render_history_page(target, student_id=student_id, page=page)
    await callback.answer()


@router.message(F.text.startswith("/groups"))
async def admin_groups(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        # робимо JOIN щоб отримати також User (teacher)
        res = await session.execute(
            select(Group, User)
            .join(User, Group.teacher_id == User.id, isouter=True)
        )
        rows = res.all()

    if not rows:
        await message.answer("Груп поки немає.")
        return

    lines: list[str] = ["Список груп:"]

    for group, teacher in rows:
        if teacher:
            teacher_info = f"{teacher.display_name or teacher.id} (id {teacher.id})"
        else:
            teacher_info = "не призначено"

        lines.append(f"{group.id}: {group.name} — вчитель: {teacher_info}")

    await message.answer("\n".join(lines))


@router.message(F.text.startswith("/add_group"))
async def admin_add_group(message: Message):
    """
    /add_group <group_name> <teacher_nickname або id>

    Приклади:
    /add_group Math Іван Петров
    /add_group Math 123456789

    Якщо передати лише /add_group <group_name> – бот покаже список доступних вчителів.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Формат: /add_group <group_name> <teacher_nickname або id>")
        return

    group_name = parts[1]

    # якщо вчителя не вказали – показуємо список вчителів, щоб можна було скопіювати
    if len(parts) == 2:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(User).where(User.role == "teacher").order_by(User.display_name)
            )
            teachers = res.scalars().all()

        if not teachers:
            await message.answer("Вчителів поки немає. Спочатку додайте вчителя в систему.")
            return

        lines = ["Доступні вчителі (можете скопіювати ім'я або id):", ""]
        for t in teachers:
            lines.append(f"{t.display_name or '—'} — {t.id}")

        lines.append("")
        lines.append(f"Після вибору, використайте:\n/add_group {group_name} <teacher>")

        await message.answer("\n".join(lines))
        return

    teacher_identifier = parts[2]

    async with AsyncSessionLocal() as session:
        teachers = await _find_teachers_by_identifier(session, teacher_identifier)

        if not teachers:
            await message.answer(
                "Вчителя з таким іменем або id не знайдено. "
                "Перевірте написання або оберіть зі списку через /add_group <group_name>."
            )
            return

        if len(teachers) > 1:
            lines = [
                "Знайдено кілька вчителів, команда не може обрати одного:",
                "",
            ]
            for t in teachers:
                lines.append(f"{t.display_name or '—'} — {t.id}")
            lines.append("")
            lines.append(
                "Уточніть ім'я або використайте id вчителя в команді /add_group."
            )
            await message.answer("\n".join(lines))
            return

        teacher = teachers[0]

        group = Group(name=group_name, teacher_id=teacher.id)
        session.add(group)
        await session.commit()
        await session.refresh(group)

    await message.answer(
        f"Групу створено: id={group.id}, name={group.name}, teacher={teacher.display_name or teacher.id}"
    )


@router.message(F.text.startswith("/set_group_teacher"))
async def admin_set_group_teacher(message: Message):
    """
    /set_group_teacher <group_id> <teacher_nickname або id>

    Якщо вказати тільки /set_group_teacher <group_id> – бот покаже список вчителів.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Формат: /set_group_teacher <group_id> <teacher_nickname або id>")
        return

    try:
        group_id = int(parts[1])
    except ValueError:
        await message.answer("group_id має бути числом.")
        return

    # перевіряємо групу
    async with AsyncSessionLocal() as session:
        res_group = await session.execute(select(Group).where(Group.id == group_id))
        group = res_group.scalar_one_or_none()

        if not group:
            await message.answer("Групу з таким id не знайдено.")
            return

        # якщо вчителя не вказали – показуємо список вчителів
        if len(parts) == 2:
            res = await session.execute(
                select(User).where(User.role == "teacher").order_by(User.display_name)
            )
            teachers = res.scalars().all()

            if not teachers:
                await message.answer("Вчителів поки немає.")
                return

            lines = [
                f"Група {group.id}: {group.name}",
                "",
                "Доступні вчителі:",
            ]
            for t in teachers:
                lines.append(f"{t.display_name or '—'} — {t.id}")
            lines.append("")
            lines.append(
                f"Після вибору, використайте:\n/set_group_teacher {group_id} <teacher>"
            )

            await message.answer("\n".join(lines))
            return

        teacher_identifier = parts[2]
        teachers = await _find_teachers_by_identifier(session, teacher_identifier)

        if not teachers:
            await message.answer(
                "Вчителя з таким іменем або id не знайдено. Перевірте написання."
            )
            return

        if len(teachers) > 1:
            lines = ["Знайдено кілька вчителів:"]
            for t in teachers:
                lines.append(f"{t.display_name or '—'} — {t.id}")
            lines.append("")
            lines.append(
                "Уточніть ім'я або використайте id вчителя в команді /set_group_teacher."
            )
            await message.answer("\n".join(lines))
            return

        teacher = teachers[0]
        group.teacher_id = teacher.id
        await session.commit()

    await message.answer(
        f"Для групи {group_id} встановлено вчителя {teacher.display_name or teacher.id}."
    )


@router.message(F.text.startswith("/assign_group"))
async def admin_assign_group(message: Message):
    """
    /assign_group <group_id>
    Якщо group_id не вказаний — показує список груп (id + назва),
    щоб можна було скопіювати.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    # якщо group_id не вказали – показуємо список груп
    if len(parts) == 1:
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Group))
            groups = res.scalars().all()

        if not groups:
            await message.answer("Груп поки немає. Створіть їх через /add_group.")
            return

        lines: list[str] = ["Список груп:", ""]
        for g in groups:
            lines.append(f"{g.id}: {g.name} (teacher_id={g.teacher_id or '—'})")

        lines.append("")
        lines.append("Після вибору, використайте:\n/assign_group <group_id>")

        await message.answer("\n".join(lines))
        return

    try:
        group_id = int(parts[1])
    except ValueError:
        await message.answer("group_id має бути числом.")
        return

    # перевіримо, що група існує
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Group).where(Group.id == group_id))
        group = res.scalar_one_or_none()

    if not group:
        await message.answer("Групу з таким id не знайдено. Спочатку створіть її через /add_group.")
        return

    sent = await message.answer(
        f"Завантаження списку учнів без групи для групи id={group_id}..."
    )
    await _render_assign_group_page(sent, group_id=group_id, page=0)


@router.callback_query(F.data.startswith("asg:"))
async def admin_assign_group_select_student(callback: CallbackQuery):
    """
    Клік по учню в режимі /assign_group.
    data: asg:<group_id>:<student_id>
    """
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = callback.data or ""
    try:
        _, raw_group_id, raw_student_id = data.split(":", 2)
        group_id = int(raw_group_id)
        student_id = int(raw_student_id)
    except Exception:
        await callback.answer("Некоректні дані кнопки.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        # перевіряємо групу
        res_group = await session.execute(select(Group).where(Group.id == group_id))
        group = res_group.scalar_one_or_none()
        if not group:
            await callback.answer("Групу не знайдено.", show_alert=True)
            return

        # перевіряємо студента
        res_student = await session.execute(
            select(User).where(User.id == student_id, User.role == "student")
        )
        student = res_student.scalar_one_or_none()
        if not student:
            await callback.answer("Учня не знайдено.", show_alert=True)
            return

        # якщо вже у групі – просто скажемо йому
        if student.group_id == group_id:
            await callback.answer("Учень уже в цій групі.", show_alert=True)
        else:
            student.group_id = group_id
            await session.commit()
            await callback.answer(
                f"Учня {student.display_name or student.id} додано до групи {group.name}.",
                show_alert=False,
            )

    # оновлюємо список учнів без групи (щоб цей зник зі списку)
    await _render_assign_group_page(callback.message, group_id=group_id, page=0)


@router.callback_query(F.data.startswith("asgpage:"))
async def admin_assign_group_page_callback(callback: CallbackQuery):
    """
    Перемикання сторінок списку учнів без групи в режимі /assign_group.
    data: asgpage:<group_id>:<page>
    """
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = callback.data or ""
    try:
        _, raw_group_id, raw_page = data.split(":", 2)
        group_id = int(raw_group_id)
        page = int(raw_page)
    except Exception:
        await callback.answer("Некоректні дані пагінації.", show_alert=True)
        return

    await _render_assign_group_page(callback.message, group_id=group_id, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("asgdone:"))
async def admin_assign_group_done(callback: CallbackQuery):
    """
    Завершення режиму прив'язки учнів до групи.
    data: asgdone:<group_id>
    """
    if not callback.from_user or not await (callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = callback.data or ""
    try:
        _, raw_group_id = data.split(":", 1)
        group_id = int(raw_group_id)
    except Exception:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    await callback.message.edit_text(
        f"Прив'язка учнів до групи id={group_id} завершена.",
        reply_markup=None,
    )
    await callback.answer("Готово.")


@router.message(F.text.startswith("/remove_from_group"))
async def admin_remove_from_group(message: Message):
    """
    /remove_from_group <student_nickname або id>
    Відв'язати учня від групи (видалити з групи).

    Якщо параметр не вказаний – показує список учнів, які зараз у групах.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    # якщо учня не вказали – список
    if len(parts) == 1:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(User).where(
                    User.role == "student",
                    User.group_id.is_not(None),
                )
            )
            students = res.scalars().all()

        if not students:
            await message.answer("Немає учнів, прив'язаних до груп.")
            return

        lines = ["Учні, які прив'язані до груп:", ""]
        for s in students:
            lines.append(f"{s.display_name or '—'} — {s.id} (group_id={s.group_id})")

        lines.append("")
        lines.append(
            "Після вибору, використайте:\n/remove_from_group <ім'я_учня або id>"
        )

        await message.answer("\n".join(lines))
        return

    identifier = parts[1]

    async with AsyncSessionLocal() as session:
        students = await _find_students_by_identifier(session, identifier)

        if not students:
            await message.answer(
                "Учня з таким ім'ям або id не знайдено. Перевірте написання."
            )
            return

        if len(students) > 1:
            lines = ["Знайдено кілька учнів:"]
            for s in students:
                lines.append(f"{s.display_name or '—'} — {s.id} (group_id={s.group_id})")
            lines.append("")
            lines.append(
                "Уточніть ім'я або використайте id у /remove_from_group."
            )
            await message.answer("\n".join(lines))
            return

        student = students[0]

        if student.group_id is None:
            await message.answer("Учень не прив'язаний до жодної групи.")
            return

        old_group_id = student.group_id
        student.group_id = None
        await session.commit()

    await message.answer(
        f"Учня {student.display_name or student.id} видалено з групи id={old_group_id}."
    )


@router.message(F.text.startswith("/add_teacher"))
async def admin_add_teacher(message: Message):
    """
    /add_teacher <нік або id>

    Якщо параметр не вказаний – показує список користувачів, які НЕ є вчителями.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    # якщо параметр не вказали – список кандидатів
    if len(parts) == 1:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(User).where(User.role != "teacher")
            )
            users = res.scalars().all()

        if not users:
            await message.answer("Немає користувачів, яких можна зробити вчителями.")
            return

        lines = ["Користувачі, які зараз не є вчителями:", ""]
        for u in users:
            lines.append(f"{u.display_name or '—'} — {u.id} (role={u.role})")

        lines.append("")
        lines.append("Після вибору використайте:\n/add_teacher <нік або id>")

        await message.answer("\n".join(lines))
        return

    identifier = parts[1]

    async with AsyncSessionLocal() as session:
        users = await _find_any_users_by_identifier(session, identifier)

        if not users:
            await message.answer("Користувача з таким ім'ям або id не знайдено.")
            return

        if len(users) > 1:
            lines = ["Знайдено кілька користувачів:"]
            for u in users:
                lines.append(f"{u.display_name or '—'} — {u.id} (role={u.role})")
            lines.append("")
            lines.append(
                "Уточніть ім'я або використайте конкретний id у /add_teacher."
            )
            await message.answer("\n".join(lines))
            return

        user = users[0]

        if user.role == "teacher":
            await message.answer("Цей користувач уже має роль 'teacher'.")
            return

        user.role = "teacher"
        await session.commit()

    await message.answer(
        f"Користувачу {user.display_name or user.id} призначено роль 'teacher'."
    )


@router.message(F.text.startswith("/remove_teacher"))
async def admin_remove_teacher(message: Message):
    """
    /remove_teacher <нік або id>
    Знімає роль 'teacher' (ставить 'student') і відв'язує від груп як вчителя.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    # якщо параметр не вказали – список вчителів
    if len(parts) == 1:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(User).where(User.role == "teacher")
            )
            teachers = res.scalars().all()

        if not teachers:
            await message.answer("Немає користувачів з роллю 'teacher'.")
            return

        lines = ["Поточні вчителі:", ""]
        for t in teachers:
            lines.append(f"{t.display_name or '—'} — {t.id}")
        lines.append("")
        lines.append("Після вибору використайте:\n/remove_teacher <нік або id>")

        await message.answer("\n".join(lines))
        return

    identifier = parts[1]

    async with AsyncSessionLocal() as session:
        # шукаємо тільки серед teacher
        res = await session.execute(
            select(User).where(User.role == "teacher")
        )
        all_teachers = res.scalars().all()

        # фільтруємо їх локально через helper
        # (щоб не ускладнювати запит)
        candidates = []
        for t in all_teachers:
            if (
                    str(t.id) == identifier
                    or (t.display_name and t.display_name.lower() == identifier.lower())
                    or (t.display_name and identifier.lower() in t.display_name.lower())
            ):
                candidates.append(t)

        if not candidates:
            await message.answer("Вчителя з таким ім'ям або id не знайдено.")
            return

        if len(candidates) > 1:
            lines = ["Знайдено кілька вчителів:"]
            for t in candidates:
                lines.append(f"{t.display_name or '—'} — {t.id}")
            lines.append("")
            lines.append(
                "Уточніть ім'я або використайте точний id у /remove_teacher."
            )
            await message.answer("\n".join(lines))
            return

        teacher = candidates[0]

        # відв'язуємо всі групи, де він стоїть teacher_id
        res_groups = await session.execute(
            select(Group).where(Group.teacher_id == teacher.id)
        )
        groups = res_groups.scalars().all()
        for g in groups:
            g.teacher_id = None

        teacher.role = "student"
        await session.commit()

    await message.answer(
        f"Користувача {teacher.display_name or teacher.id} більше не вважаємо 'teacher'. "
        f"Його прибрано з {len(groups)} груп(и)."
    )


@router.message(F.text.startswith("/add_admin"))
async def admin_add_admin(message: Message):
    """
    /add_admin <нік або id>

    Якщо параметр не вказаний – показує список користувачів, які НЕ є admin.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) == 1:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(User).where(User.role != "admin")
            )
            users = res.scalars().all()

        if not users:
            await message.answer("Немає користувачів, яких можна зробити адмінами.")
            return

        lines = ["Користувачі, які зараз не є 'admin':", ""]
        for u in users:
            lines.append(f"{u.display_name or '—'} — {u.id} (role={u.role})")
        lines.append("")
        lines.append("Після вибору використайте:\n/add_admin <нік або id>")

        await message.answer("\n".join(lines))
        return

    identifier = parts[1]

    async with AsyncSessionLocal() as session:
        users = await _find_any_users_by_identifier(session, identifier)

        if not users:
            await message.answer("Користувача з таким ім'ям або id не знайдено.")
            return

        if len(users) > 1:
            lines = ["Знайдено кілька користувачів:"]
            for u in users:
                lines.append(f"{u.display_name or '—'} — {u.id} (role={u.role})")
            lines.append("")
            lines.append(
                "Уточніть ім'я або використайте конкретний id у /add_admin."
            )
            await message.answer("\n".join(lines))
            return

        user = users[0]

        if user.role == "admin":
            await message.answer("Цей користувач уже має роль 'admin'.")
            return

        user.role = "admin"
        await session.commit()

    await message.answer(
        f"Користувачу {user.display_name or user.id} призначено роль 'admin'."
    )


@router.message(F.text.startswith("/remove_admin"))
async def admin_remove_admin(message: Message):
    """
    /remove_admin <нік або id>
    Знімає роль 'admin' (ставить 'student').

    Не дозволяє видалити останнього адміна.
    """
    if not await  is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    async with AsyncSessionLocal() as session:
        # рахуємо адмінів
        res_all = await session.execute(
            select(User).where(User.role == "admin")
        )
        all_admins = res_all.scalars().all()
        admins_count = len(all_admins)

        if admins_count == 0:
            await message.answer("У системі немає жодного 'admin'.")
            return

        # якщо параметр не вказаний – список адмінів
        if len(parts) == 1:
            lines = ["Поточні адміни:", ""]
            for u in all_admins:
                lines.append(f"{u.display_name or '—'} — {u.id}")
            lines.append("")
            lines.append("Після вибору використайте:\n/remove_admin <нік або id>")

            await message.answer("\n".join(lines))
            return

        identifier = parts[1]

        candidates = []
        for u in all_admins:
            if (
                    str(u.id) == identifier
                    or (u.display_name and u.display_name.lower() == identifier.lower())
                    or (u.display_name and identifier.lower() in u.display_name.lower())
            ):
                candidates.append(u)

        if not candidates:
            await message.answer("Адміна з таким ім'ям або id не знайдено.")
            return

        if len(candidates) > 1:
            lines = ["Знайдено кілька адмінів:"]
            for u in candidates:
                lines.append(f"{u.display_name or '—'} — {u.id}")
            lines.append("")
            lines.append(
                "Уточніть ім'я або використайте точний id у /remove_admin."
            )
            await message.answer("\n".join(lines))
            return

        admin_user = candidates[0]

        # захист від видалення останнього адміна
        if admins_count == 1 and admin_user.id == all_admins[0].id:
            await message.answer(
                "Неможливо видалити останнього адміна. Спочатку додайте іншого через /add_admin."
            )
            return

        admin_user.role = "student"
        await session.commit()

    await message.answer(
        f"Користувача {admin_user.display_name or admin_user.id} більше не вважаємо 'admin'."
    )


@router.message(F.text.startswith("/assign_teacher"))
async def admin_assign_teacher_1to1(message: Message):
    """
    /assign_teacher <учень> <викладач>

    Прив'язка студента до викладача 1-на-1, поверх груп.
    Обидва параметри можна вказати як id або частину імені.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Формат: /assign_teacher <учень> <викладач>\n"
            "Можна вказувати id або імʼя (display name)."
        )
        return

    _, student_ident, teacher_ident = parts

    async with AsyncSessionLocal() as session:
        # ---- шукаємо студента ----
        students_q = select(User).where(User.role == "student")

        # спроба як id
        try:
            sid = int(student_ident)
        except ValueError:
            sid = None

        student = None
        if sid is not None:
            res = await session.execute(students_q.where(User.id == sid))
            student = res.scalar_one_or_none()

        if not student:
            pattern = f"%{student_ident}%"
            res = await session.execute(
                students_q.where(
                    or_(
                        func.lower(User.display_name) == student_ident.lower(),
                        User.display_name.ilike(pattern),
                    )
                )
            )
            students = res.scalars().all()
            if not students:
                await message.answer("Учня з таким іменем або id не знайдено.")
                return
            if len(students) > 1:
                lines = ["Знайдено кілька учнів:"]
                for s in students:
                    lines.append(f"{s.display_name or '—'} — {s.id}")
                lines.append(
                    "Уточніть, будь ласка, за ID або повним імʼям у /assign_teacher."
                )
                await message.answer("\n".join(lines))
                return
            student = students[0]

        # ---- шукаємо викладача ----
        teachers_q = select(User).where(User.role == "teacher")

        try:
            tid = int(teacher_ident)
        except ValueError:
            tid = None

        teacher = None
        if tid is not None:
            res = await session.execute(teachers_q.where(User.id == tid))
            teacher = res.scalar_one_or_none()

        if not teacher:
            pattern = f"%{teacher_ident}%"
            res = await session.execute(
                teachers_q.where(
                    or_(
                        func.lower(User.display_name) == teacher_ident.lower(),
                        User.display_name.ilike(pattern),
                    )
                )
            )
            teachers = res.scalars().all()
            if not teachers:
                await message.answer("Викладача з таким іменем або id не знайдено.")
                return
            if len(teachers) > 1:
                lines = ["Знайдено кілька викладачів:"]
                for t in teachers:
                    lines.append(f"{t.display_name or '—'} — {t.id}")
                lines.append(
                    "Уточніть, будь ласка, за ID або повним імʼям у /assign_teacher."
                )
                await message.answer("\n".join(lines))
                return
            teacher = teachers[0]

        # ---- записуємо прив'язку 1-на-1 ----
        student.assigned_teacher_id = teacher.id
        await session.commit()

        await message.answer(
            f"Учня {student.display_name or student.id} привʼязано до викладача "
            f"{teacher.display_name or teacher.id} в режимі 1-на-1."
        )


@router.message(F.text.startswith("/unassign_teacher"))
async def admin_unassign_teacher_1to1(message: Message):
    """
    /unassign_teacher <учень>

    Забирає прямого викладача 1-на-1 (assigned_teacher_id = NULL).
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Формат: /unassign_teacher <учень>\n"
            "Можна вказати id або імʼя (display name)."
        )
        return

    student_ident = parts[1]

    async with AsyncSessionLocal() as session:
        students_q = select(User).where(User.role == "student")

        # пробуємо як id
        try:
            sid = int(student_ident)
        except ValueError:
            sid = None

        student = None
        if sid is not None:
            res = await session.execute(students_q.where(User.id == sid))
            student = res.scalar_one_or_none()

        if not student:
            pattern = f"%{student_ident}%"
            res = await session.execute(
                students_q.where(
                    or_(
                        func.lower(User.display_name) == student_ident.lower(),
                        User.display_name.ilike(pattern),
                    )
                )
            )
            students = res.scalars().all()
            if not students:
                await message.answer("Учня з таким іменем або id не знайдено.")
                return
            if len(students) > 1:
                lines = ["Знайдено кілька учнів:"]
                for s in students:
                    lines.append(f"{s.display_name or '—'} — {s.id}")
                lines.append(
                    "Уточніть, будь ласка, за ID або повним імʼям у /unassign_teacher."
                )
                await message.answer("\n".join(lines))
                return
            student = students[0]

        if student.assigned_teacher_id is None:
            await message.answer(
                "У цього учня немає прямого викладача 1-на-1 (assigned_teacher_id порожнє)."
            )
            return

        student.assigned_teacher_id = None
        await session.commit()

        await message.answer(
            f"Для учня {student.display_name or student.id} скасовано привʼязку 1-на-1."
        )


async def _render_teachers_page(target_message: Message, page: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        count_res = await session.execute(
            select(func.count(User.id)).where(User.role == "teacher")
        )
        total = count_res.scalar() or 0

        if total == 0:
            await target_message.edit_text("Викладачів поки немає.", reply_markup=None)
            return

        total_pages = (total + TEACHERS_PER_PAGE - 1) // TEACHERS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        offset = page * TEACHERS_PER_PAGE

        # Беремо викладачів + рахуємо їх учнів (1-на-1 та групи)
        teachers_res = await session.execute(
            select(User)
            .where(User.role == "teacher")
            .order_by(User.display_name)
            .offset(offset)
            .limit(TEACHERS_PER_PAGE)
        )
        teachers = teachers_res.scalars().all()

        # Підрахунок учнів на кожного (активні+неактивні)
        # 1) assigned_teacher_id
        assigned_counts = dict(
            (await session.execute(
                select(User.assigned_teacher_id, func.count(User.id))
                .where(User.role == "student", User.assigned_teacher_id.is_not(None))
                .group_by(User.assigned_teacher_id)
            )).all()
        )

        # 2) групи (Group.teacher_id -> User.group_id)
        group_counts = dict(
            (await session.execute(
                select(Group.teacher_id, func.count(User.id))
                .join(User, User.group_id == Group.id)
                .where(User.role == "student", Group.teacher_id.is_not(None))
                .group_by(Group.teacher_id)
            )).all()
        )

    kb = InlineKeyboardBuilder()
    for t in teachers:
        cnt = (assigned_counts.get(t.id, 0) or 0) + (group_counts.get(t.id, 0) or 0)
        kb.button(
            text=f"{_sname(t)} — {cnt} учні",
            callback_data=f"tch:{t.id}:0",  # teacher students page 0
        )
    kb.adjust(1)

    # pagination
    if page > 0:
        kb.button(text="⬅️ Назад", callback_data=f"tch_page:{page - 1}")
    if page < total_pages - 1:
        kb.button(text="➡️ Далі", callback_data=f"tch_page:{page + 1}")
    kb.adjust(1)

    await target_message.edit_text(
        f"Викладачі (сторінка {page + 1}/{total_pages}):",
        reply_markup=kb.as_markup(),
    )


@router.message(F.text == "/teachers")
async def admin_teachers(message: Message):
    if not await is_admin(message.from_user.id):
        return

    sent = await message.answer("Завантаження списку викладачів...")
    await _render_teachers_page(sent, page=0)


@router.callback_query(F.data.startswith("tch:"))
async def admin_teacher_students(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    try:
        _, raw_teacher_id, raw_page = callback.data.split(":", 2)
        teacher_id = int(raw_teacher_id)
        page = int(raw_page)
    except Exception:
        await callback.answer("Помилка.", show_alert=True)
        return

    await _render_teacher_students_page(callback.message, teacher_id=teacher_id, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("t_page:"))
async def admin_teachers_page(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    page = int(callback.data.split(":")[1])
    await _render_teachers_page(callback.message, page)
    await callback.answer()


@router.callback_query(F.data.startswith("tch_page:"))
async def admin_teachers_page(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    try:
        _, raw = callback.data.split(":", 1)
        page = int(raw)
    except Exception:
        await callback.answer("Помилка.", show_alert=True)
        return

    await _render_teachers_page(callback.message, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("t_students:"))
async def admin_teacher_students(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    # t_students:<teacher_id>:<students_page>:<teachers_page>
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    _, teacher_id, page, back_page = parts
    teacher_id = int(teacher_id)
    page = int(page)
    back_page = int(back_page)

    async with AsyncSessionLocal() as session:
        teacher = await session.get(User, teacher_id)
        if not teacher:
            await callback.answer("Викладача не знайдено", show_alert=True)
            return

        total = await session.scalar(
            select(func.count(User.id)).where(User.assigned_teacher_id == teacher_id)
        ) or 0

        pages = max(1, (total + STUDENTS_PER_PAGE - 1) // STUDENTS_PER_PAGE)
        page = max(0, min(page, pages - 1))

        res = await session.execute(
            select(User)
            .where(User.assigned_teacher_id == teacher_id)
            .order_by(User.display_name)
            .offset(page * STUDENTS_PER_PAGE)
            .limit(STUDENTS_PER_PAGE)
        )
        students = res.scalars().all()

    kb = InlineKeyboardBuilder()

    title = teacher.display_name or str(teacher.id)
    lines = [
        f"👨‍🏫 {title}",
        f"Учні ({page + 1}/{pages}):",
        "",
    ]

    for s in students:
        name = s.display_name or "—"
        lines.append(f"• {name} (ID {s.id})")

        # кнопка видалення цього студента
        kb.button(
            text=f"🗑 Видалити: {name}",
            callback_data=f"stud_del:{s.id}:{teacher_id}:{page}:{back_page}",
        )

    # Пагінація учнів
    if page > 0:
        kb.button(text="⬅️", callback_data=f"t_students:{teacher_id}:{page - 1}:{back_page}")
    if page < pages - 1:
        kb.button(text="➡️", callback_data=f"t_students:{teacher_id}:{page + 1}:{back_page}")

    # Назад до списку викладачів на ту ж сторінку
    kb.button(text="⬅️ Назад", callback_data=f"t_back:{back_page}")

    kb.adjust(1)

    await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("stud_del:"))
async def admin_student_delete_confirm(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    # stud_del:<student_id>:<teacher_id>:<students_page>:<teachers_back_page>
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    _, student_id, teacher_id, page, back_page = parts
    student_id = int(student_id)
    teacher_id = int(teacher_id)
    page = int(page)
    back_page = int(back_page)

    async with AsyncSessionLocal() as session:
        student = await session.get(User, student_id)

    if not student:
        await callback.answer("Учня не знайдено.", show_alert=True)
        return

    name = student.display_name or str(student.id)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так, видалити", callback_data=f"stud_del_ok:{student_id}:{teacher_id}:{page}:{back_page}")
    kb.button(text="❌ Скасувати", callback_data=f"t_students:{teacher_id}:{page}:{back_page}")
    kb.adjust(1)

    await callback.message.edit_text(
        f"⚠️ Видалити учня **{name}** (ID {student.id}) з бази?\n"
        f"Це прибере його історію та привʼязки.",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stud_del_ok:"))
async def admin_student_delete_do(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    # stud_del_ok:<student_id>:<teacher_id>:<students_page>:<teachers_back_page>
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    _, student_id, teacher_id, page, back_page = parts
    student_id = int(student_id)
    teacher_id = int(teacher_id)
    page = int(page)
    back_page = int(back_page)

    async with AsyncSessionLocal() as session:
        # 1) прибираємо лінки reply->student
        await session.execute(
            delete(TeacherMessageLink).where(TeacherMessageLink.student_id == student_id)
        )
        # 2) прибираємо повідомлення
        await session.execute(
            delete(DbMessage).where(
                (DbMessage.from_user_id == student_id) | (DbMessage.to_user_id == student_id)
            )
        )
        # 3) видаляємо самого користувача
        student = await session.get(User, student_id)
        if student:
            await session.delete(student)

        await session.commit()

    await callback.answer("✅ Учня видалено.", show_alert=True)

    # Повертаємось до списку учнів викладача
    await admin_teacher_students(
        CallbackQuery(
            id=callback.id,
            from_user=callback.from_user,
            chat_instance=callback.chat_instance,
            message=callback.message,
            data=f"t_students:{teacher_id}:{page}:{back_page}",
        )
    )


@router.callback_query(F.data.startswith("t_back:"))
async def admin_teachers_back(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    back_page = int((callback.data or "").split(":")[1])
    await _render_teachers_page(callback.message, page=back_page)
    await callback.answer()


# =========================
# /admins (admin) + pagination
# =========================

async def _render_admins_page(target_message: Message, page: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        count_res = await session.execute(
            select(func.count(User.id)).where(User.role == "admin")
        )
        total = count_res.scalar() or 0

        if total == 0:
            await target_message.edit_text("Адмінів поки немає.", reply_markup=None)
            return

        total_pages = (total + ADMINS_PER_PAGE - 1) // ADMINS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        offset = page * ADMINS_PER_PAGE

        res = await session.execute(
            select(User)
            .where(User.role == "admin")
            .order_by(User.display_name)
            .offset(offset)
            .limit(ADMINS_PER_PAGE)
        )
        admins = res.scalars().all()

    lines = [f"🛡 Адміни (сторінка {page + 1}/{total_pages}):", ""]
    for a in admins:
        lines.append(f"• {a.display_name or '—'} — {a.id}")

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="⬅️", callback_data=f"adm_page:{page - 1}")
    if page < total_pages - 1:
        kb.button(text="➡️", callback_data=f"adm_page:{page + 1}")
    kb.adjust(2)

    await target_message.edit_text("\n".join(lines), reply_markup=kb.as_markup())


@router.message(F.text.startswith("/admins"))
async def admin_admins(message: Message):
    if not await is_admin(message.from_user.id):
        return
    sent = await message.answer("Завантаження адмінів...")
    await _render_admins_page(sent, page=0)


@router.callback_query(F.data.startswith("adm_page:"))
async def admin_admins_page_callback(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    try:
        _, raw_page = (callback.data or "").split(":", 1)
        page = int(raw_page)
    except Exception:
        await callback.answer("Помилка пагінації.", show_alert=True)
        return

    await _render_admins_page(callback.message, page=page)
    await callback.answer()


# =========================
# /delete_student (admin) + confirm + pagination
# =========================

async def _render_delete_students_page(target_message: Message, page: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        count_res = await session.execute(
            select(func.count(User.id)).where(User.role == "student")
        )
        total = count_res.scalar() or 0

        if total == 0:
            await target_message.edit_text("Студентів поки немає.", reply_markup=None)
            return

        total_pages = (total + DEL_STUDENTS_PER_PAGE - 1) // DEL_STUDENTS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        offset = page * DEL_STUDENTS_PER_PAGE

        res = await session.execute(
            select(User)
            .where(User.role == "student")
            .order_by(User.display_name)
            .offset(offset)
            .limit(DEL_STUDENTS_PER_PAGE)
        )
        students = res.scalars().all()

    kb = InlineKeyboardBuilder()
    for s in students:
        name = s.display_name or f"ID {s.id}"
        kb.button(text=f"🗑 {name}", callback_data=f"delstud:{s.id}:{page}")
    kb.adjust(1)

    if page > 0:
        kb.button(text="⬅️", callback_data=f"delstud_page:{page - 1}")
    if page < total_pages - 1:
        kb.button(text="➡️", callback_data=f"delstud_page:{page + 1}")
    kb.adjust(1, 2)

    header = f"🗑 Видалення студента (сторінка {page + 1}/{total_pages}).\nОбери кого видалити:"
    await target_message.edit_text(header, reply_markup=kb.as_markup())


@router.message(F.text.startswith("/delete_student"))
async def admin_delete_student(message: Message):
    """
    /delete_student
    /delete_student <ім'я або id>

    Якщо без параметрів — дає список з кнопками і підтвердженням.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    # режим зі списком
    if len(parts) == 1:
        sent = await message.answer("Завантаження студентів...")
        await _render_delete_students_page(sent, page=0)
        return

    identifier = parts[1]

    async with AsyncSessionLocal() as session:
        students = await _find_students_by_identifier(session, identifier)
        if not students:
            await message.answer("Студента з таким ім'ям або id не знайдено.")
            return
        if len(students) > 1:
            lines = ["Знайдено кілька студентів:"]
            for s in students:
                lines.append(f"{s.display_name or '—'} — {s.id}")
            lines.append("Уточни, будь ласка, за ID або повним ім'ям.")
            await message.answer("\n".join(lines))
            return
        student = students[0]

    # підтвердження
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так, видалити", callback_data=f"delstud_ok:{student.id}")
    kb.button(text="❌ Скасувати", callback_data="delstud_cancel")
    kb.adjust(1, 1)

    await message.answer(
        f"Підтверди видалення студента:\n"
        f"• {student.display_name or '—'} — {student.id}\n\n"
        f"⚠️ Будуть видалені також його повідомлення та зв'язки.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("delstud_page:"))
async def admin_delete_students_page_callback(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    try:
        _, raw_page = (callback.data or "").split(":", 1)
        page = int(raw_page)
    except Exception:
        await callback.answer("Помилка пагінації.", show_alert=True)
        return

    await _render_delete_students_page(callback.message, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("delstud:"))
async def admin_delete_student_pick(callback: CallbackQuery):
    """
    Клік по студенту зі списку на видалення.
    data: delstud:<student_id>:<page>
    """
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_sid, raw_page = (callback.data or "").split(":", 2)
        student_id = int(raw_sid)
        page = int(raw_page)
    except Exception:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.id == student_id, User.role == "student"))
        student = res.scalar_one_or_none()

    if not student:
        await callback.answer("Студента не знайдено.", show_alert=True)
        # оновимо список
        await _render_delete_students_page(callback.message, page=page)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так, видалити", callback_data=f"delstud_ok:{student.id}")
    kb.button(text="⬅️ Назад", callback_data=f"delstud_page:{page}")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        f"Підтверди видалення студента:\n"
        f"• {student.display_name or '—'} — {student.id}\n\n"
        f"⚠️ Будуть видалені також його повідомлення та зв'язки.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delstud_ok:"))
async def admin_delete_student_confirm(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_sid = (callback.data or "").split(":", 1)
        student_id = int(raw_sid)
    except Exception:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        # студент
        res = await session.execute(select(User).where(User.id == student_id, User.role == "student"))
        student = res.scalar_one_or_none()
        if not student:
            await callback.message.edit_text("Студента вже немає в базі.", reply_markup=None)
            await callback.answer()
            return

        # чистимо зв'язки
        await session.execute(
            delete(TeacherMessageLink).where(TeacherMessageLink.student_id == student_id)
        )
        await session.execute(
            delete(DbMessage).where(
                (DbMessage.from_user_id == student_id) | (DbMessage.to_user_id == student_id)
            )
        )

        # прибираємо прив'язки (на всяк випадок)
        student.group_id = None
        student.assigned_teacher_id = None

        # видаляємо user
        await session.delete(student)
        await session.commit()

    await callback.message.edit_text(f"✅ Студента {student_id} видалено.", reply_markup=None)
    await callback.answer("Готово.")


@router.callback_query(F.data == "delstud_cancel")
async def admin_delete_student_cancel(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    await callback.message.edit_text("Скасовано.", reply_markup=None)
    await callback.answer()


@router.callback_query(F.data.startswith("stu:"))
async def admin_student_card(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    try:
        _, raw_teacher_id, raw_student_id, raw_page = callback.data.split(":", 3)
        teacher_id = int(raw_teacher_id)
        student_id = int(raw_student_id)
        page = int(raw_page)
    except Exception:
        await callback.answer("Помилка.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        student = (await session.execute(
            select(User).where(User.id == student_id, User.role == "student")
        )).scalar_one_or_none()

    if not student:
        await callback.answer("Учня не знайдено.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()

    if student.is_active:
        kb.button(text="🔴 Деактивувати", callback_data=f"stu_toggle:{teacher_id}:{student_id}:{page}:0")
    else:
        kb.button(text="🟢 Активувати", callback_data=f"stu_toggle:{teacher_id}:{student_id}:{page}:1")

    kb.button(text="⬅️ Назад", callback_data=f"tch:{teacher_id}:{page}")
    kb.adjust(1)

    await callback.message.edit_text(
        f"👤 {_sname(student)}\n"
        f"ID: {student.id}\n"
        f"Статус: {'active' if student.is_active else 'inactive'}",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stu_toggle:"))
async def admin_student_toggle(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    try:
        _, raw_teacher_id, raw_student_id, raw_page, raw_set = callback.data.split(":", 4)
        teacher_id = int(raw_teacher_id)
        student_id = int(raw_student_id)
        page = int(raw_page)
        set_val = int(raw_set)  # 0/1
    except Exception:
        await callback.answer("Помилка.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        student = (await session.execute(
            select(User).where(User.id == student_id, User.role == "student")
        )).scalar_one_or_none()

        if not student:
            await callback.answer("Учня не знайдено.", show_alert=True)
            return

        student.is_active = bool(set_val)
        await session.commit()

    # Повертаємо назад у список учнів викладача (щоб одразу було видно 🟢/🔴)
    await _render_teacher_students_page(callback.message, teacher_id=teacher_id, page=page)
    await callback.answer("Ок")


# =========================
# /unassigned_students
# =========================
@router.message(F.text.startswith("/unassigned_students"))
async def admin_unassigned_students(message: Message):
    if not await is_admin(message.from_user.id):
        return
    sent = await message.answer("Завантаження...")
    await _render_unassigned_students_page(sent, page=0)


async def _render_unassigned_students_page(target_message: Message, page: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        # "без назначенного учителя":
        # немає assigned_teacher_id
        # і група або відсутня, або в групи нема teacher_id
        q = (
            select(User)
            .outerjoin(Group, User.group_id == Group.id)
            .where(
                User.role == "student",
                User.assigned_teacher_id.is_(None),
                (User.group_id.is_(None) | (Group.teacher_id.is_(None))),
            )
        )

        count_res = await session.execute(select(func.count()).select_from(q.subquery()))
        total = count_res.scalar() or 0

        if total == 0:
            await target_message.edit_text("Немає студентів без призначеного викладача.", reply_markup=None)
            return

        total_pages = (total + UNASSIGNED_PER_PAGE - 1) // UNASSIGNED_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        offset = page * UNASSIGNED_PER_PAGE

        students = (await session.execute(
            q.order_by(User.display_name).offset(offset).limit(UNASSIGNED_PER_PAGE)
        )).scalars().all()

    kb = InlineKeyboardBuilder()
    # просто список + щоб можна було копіювати ID (в тексті)
    lines = [f"Студенти без призначеного викладача (сторінка {page + 1}/{total_pages}):", ""]
    for s in students:
        lines.append(f"• {_status_emoji(s.is_active)} {_sname(s)} (ID {s.id})")

    if page > 0:
        kb.button(text="⬅️ Назад", callback_data=f"unass_page:{page - 1}")
    if page < total_pages - 1:
        kb.button(text="➡️ Далі", callback_data=f"unass_page:{page + 1}")
    kb.adjust(1)

    await target_message.edit_text("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("unass_page:"))
async def admin_unassigned_students_page(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    try:
        _, raw = callback.data.split(":", 1)
        page = int(raw)
    except Exception:
        await callback.answer("Помилка.", show_alert=True)
        return

    await _render_unassigned_students_page(callback.message, page=page)
    await callback.answer()


@router.message(F.text.startswith("/deactivate_student"))
async def admin_deactivate_student(message: Message):
    if not await is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)

    # без параметра → показуємо список
    if len(parts) == 1:
        sent = await message.answer("Завантаження списку студентів...")
        await _render_students_toggle_page(sent, page=0)
        return

    q = parts[1].strip()
    async with AsyncSessionLocal() as session:
        candidates = await _find_students_by_query(session, q)

    if not candidates:
        await message.answer("Студента не знайдено.")
        return

    if len(candidates) > 1:
        lines = ["Знайдено кілька студентів, уточніть (краще ID):"]
        for s in candidates[:20]:
            status = "active" if getattr(s, "is_active", True) else "inactive"
            lines.append(f"• {s.display_name or '—'} — {s.id} ({status})")
        await message.answer("\n".join(lines))
        return

    st = candidates[0]
    ok, name = await _set_student_active(st.id, False)
    await message.answer(("🛑 Деактивовано: " + name) if ok else name)


@router.message(F.text.startswith("/activate_student"))
async def admin_activate_student(message: Message):
    if not await is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)

    # без параметра → показуємо список
    if len(parts) == 1:
        sent = await message.answer("Завантаження списку студентів...")
        await _render_students_toggle_page(sent, page=0)
        return

    q = parts[1].strip()
    async with AsyncSessionLocal() as session:
        candidates = await _find_students_by_query(session, q)

    if not candidates:
        await message.answer("Студента не знайдено.")
        return

    if len(candidates) > 1:
        lines = ["Знайдено кілька студентів, уточніть (краще ID):"]
        for s in candidates[:20]:
            status = "active" if getattr(s, "is_active", True) else "inactive"
            lines.append(f"• {s.display_name or '—'} — {s.id} ({status})")
        await message.answer("\n".join(lines))
        return

    st = candidates[0]
    ok, name = await _set_student_active(st.id, True)
    await message.answer(("✅ Активовано: " + name) if ok else name)


@router.callback_query(F.data.startswith("stu_page:"))
async def admin_students_toggle_page(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_page = (callback.data or "").split(":", 1)
        page = int(raw_page)
    except Exception:
        await callback.answer("Помилка пагінації.", show_alert=True)
        return

    await _render_students_toggle_page(callback.message, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("stu_deact:"))
async def admin_deactivate_student_cb(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_id, raw_page = (callback.data or "").split(":", 2)
        student_id = int(raw_id)
        page = int(raw_page)
    except Exception:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    ok, name = await _set_student_active(student_id, False)
    await callback.answer(("🛑 Деактивовано: " + name) if ok else name, show_alert=not ok)
    await _render_students_toggle_page(callback.message, page=page)


@router.callback_query(F.data.startswith("stu_act:"))
async def admin_activate_student_cb(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_id, raw_page = (callback.data or "").split(":", 2)
        student_id = int(raw_id)
        page = int(raw_page)
    except Exception:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    ok, name = await _set_student_active(student_id, True)
    await callback.answer(("✅ Активовано: " + name) if ok else name, show_alert=not ok)
    await _render_students_toggle_page(callback.message, page=page)


def format_media_footer(m: DbMessage) -> str:
    dt = m.created_at.strftime("%d.%m %H:%M")
    direction = (
        "Учень → Вчитель"
        if m.direction == "student_to_teacher"
        else "Вчитель → Учень"
    )
    return f"[{dt}] {direction}"


@router.callback_query(F.data.startswith("hist_media:"))
async def admin_history_media_callback(callback: CallbackQuery):
    if not callback.from_user or not (
            await is_admin(callback.from_user.id) or await is_teacher(callback.from_user.id)
    ):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    try:
        _, raw_student_id, raw_page = (callback.data or "").split(":", 2)
        student_id = int(raw_student_id)
        page = int(raw_page)
    except Exception:
        await callback.answer("Некоректні дані.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        offset = page * MSGS_PER_PAGE
        res = await session.execute(
            select(DbMessage)
            .where(
                (DbMessage.from_user_id == student_id)
                | (DbMessage.to_user_id == student_id)
            )
            .order_by(DbMessage.created_at)
            .offset(offset)
            .limit(MSGS_PER_PAGE)
        )
        msgs = res.scalars().all()

    media_msgs = [m for m in msgs if m.has_media and m.media_file_id]
    if not media_msgs:
        await callback.answer("На цій сторінці немає медіа.", show_alert=True)
        return

    await callback.answer(f"Надсилаю медіа: {len(media_msgs)} шт.", show_alert=False)

    for m in media_msgs[:10]:
        kind = getattr(m, "media_kind", None)
        caption = (m.text or "")[:900] or MEDIA_EMOJI.get(kind, "📎 Медіа")

        if kind == "photo":
            await callback.bot.send_photo(callback.from_user.id, m.media_file_id, caption=caption)
        elif kind == "document":
            await callback.bot.send_document(callback.from_user.id, m.media_file_id, caption=caption)
        elif kind == "voice":
            await callback.bot.send_voice(callback.from_user.id, m.media_file_id)
        elif kind == "audio":
            await callback.bot.send_audio(callback.from_user.id, m.media_file_id, caption=caption)
        elif kind == "video":
            await callback.bot.send_video(callback.from_user.id, m.media_file_id, caption=caption)
        else:
            await callback.bot.send_message(
                callback.from_user.id,
                f"📎 Медіа: {caption}"
            )

        # ✅ ОЦЕ ГОЛОВНЕ — підпис ПІД медіа
        await callback.bot.send_message(
            callback.from_user.id,
            format_media_footer(m)
        )
