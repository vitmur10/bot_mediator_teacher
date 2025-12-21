from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func

from config import get_settings
from db import AsyncSessionLocal
from models import Message as DbMessage, User, Group
from utils.roles import is_admin

router = Router()
settings = get_settings()

STUDENTS_PER_PAGE = 10
MSGS_PER_PAGE = 20
UNASSIGNED_PER_PAGE = 10


def _format_dt(dt: datetime | None) -> str:
    """
    Форматування дати/часу для виводу в історії:
    19.12 22:31
    """
    if not dt:
        return ""
    return dt.strftime("%d.%m %H:%M")


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


async def _render_students_page(target_message: Message, page: int = 0) -> None:
    """
    Малює список учнів з пагінацією в одному повідомленні.
    Використовується як для /students, так і для callback stud_page:<page>
    """
    async with AsyncSessionLocal() as session:
        # рахуємо кількість студентів
        count_res = await session.execute(
            select(func.count(User.id)).where(User.role == "student")
        )
        total_students = count_res.scalar() or 0

        if total_students == 0:
            # якщо немає студентів — просто показуємо текст
            if target_message.text != "Учнів поки немає.":
                await target_message.edit_text("Учнів поки немає.", reply_markup=None)
            return

        total_pages = (total_students + STUDENTS_PER_PAGE - 1) // STUDENTS_PER_PAGE
        if page < 0:
            page = 0
        if page >= total_pages:
            page = total_pages - 1

        offset = page * STUDENTS_PER_PAGE

        res = await session.execute(
            select(User)
            .where(User.role == "student")
            .order_by(User.display_name)
            .offset(offset)
            .limit(STUDENTS_PER_PAGE)
        )
        students = res.scalars().all()

    kb = InlineKeyboardBuilder()

    for u in students:
        name = u.display_name or f"ID {u.id}"
        # При кліку відкриваємо історію учня, перша сторінка (0)
        kb.button(
            text=name,
            callback_data=f"hist:{u.id}:0",
        )

    kb.adjust(1)

    # кнопки пагінації по учнях
    nav_row = []
    if page > 0:
        nav_row.append(
            dict(text="⬅️ Попередня сторінка", callback_data=f"stud_page:{page - 1}")
        )
    if page < total_pages - 1:
        nav_row.append(
            dict(text="➡️ Наступна сторінка", callback_data=f"stud_page:{page + 1}")
        )

    for btn in nav_row:
        kb.button(text=btn["text"], callback_data=btn["callback_data"])

    if nav_row:
        kb.adjust(1, 1)

    header = f"Список учнів (сторінка {page + 1}/{total_pages}):"

    await target_message.edit_text(
        header,
        reply_markup=kb.as_markup(),
    )


async def _render_history_page(
        target_message: Message,
        student_id: int,
        page: int = 0,
) -> None:
    """
    Малює сторінку історії з конкретним учнем + інлайн-пагінацію.
    """
    async with AsyncSessionLocal() as session:
        # рахуємо кількість повідомлень
        count_res = await session.execute(
            select(func.count(DbMessage.id)).where(
                (DbMessage.from_user_id == student_id)
                | (DbMessage.to_user_id == student_id)
            )
        )
        total_msgs = count_res.scalar() or 0

        if total_msgs == 0:
            await target_message.edit_text(
                "Історія для цього користувача порожня.",
                reply_markup=None,
            )
            return

        total_pages = (total_msgs + MSGS_PER_PAGE - 1) // MSGS_PER_PAGE
        if page < 0:
            page = 0
        if page >= total_pages:
            page = total_pages - 1

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

        user_res = await session.execute(
            select(User).where(User.id == student_id)
        )
        student = user_res.scalar_one_or_none()

    header_name = (
        student.display_name if student and student.display_name else str(student_id)
    )

    lines: list[str] = [
        f"Історія діалогу з учнем: {header_name}",
        f"Сторінка {page + 1}/{total_pages}",
        "",
    ]

    for m in msgs:
        ts = _format_dt(m.created_at)
        direction = (
            "Учень → Вчитель"
            if m.direction == "student_to_teacher"
            else "Вчитель → Учень"
        )
        prefix = f"[{ts}] {direction}"
        body = m.text or ""
        lines.append(f"{prefix}\n{body}\n")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]

    kb = InlineKeyboardBuilder()

    # Пагінація по історії
    if page > 0:
        kb.button(
            text="⬅️ Попередня сторінка",
            callback_data=f"hist:{student_id}:{page - 1}",
        )
    if page < total_pages - 1:
        kb.button(
            text="➡️ Наступна сторінка",
            callback_data=f"hist:{student_id}:{page + 1}",
        )

    # Кнопка назад до списку учнів (на першу сторінку)
    kb.button(
        text="⬅️ Назад до списку учнів",
        callback_data="stud_page:0",
    )

    kb.adjust(1)

    await target_message.edit_text(
        text or "Немає текстових повідомлень.",
        reply_markup=kb.as_markup(),
    )


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
    /students – показує список учнів інлайн-кнопками з пагінацією.
    По кліку на кнопку буде показана історія діалогу з цим учнем.
    """
    if not await  is_admin(message.from_user.id):
        return

    # спочатку надсилаємо порожнє повідомлення, потім редагуємо його в _render_students_page
    sent = await message.answer("Завантаження списку учнів...")
    await _render_students_page(sent, page=0)


@router.callback_query(F.data.startswith("stud_page:"))
async def admin_students_page_callback(callback: CallbackQuery):
    """
    Перемикання сторінок списку учнів.
    data: stud_page:<page>
    """
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = callback.data or ""
    try:
        _, raw_page = data.split(":", 1)
        page = int(raw_page)
    except Exception:
        await callback.answer("Некоректні дані пагінації.", show_alert=True)
        return

    await _render_students_page(callback.message, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("hist:"))
async def admin_history_callback(callback: CallbackQuery):
    """
    Обробка кліку по інлайн-кнопці з /students та пагінації історії.
    data: hist:<student_id>:<page> або hist:<student_id> (тоді сторінка 0)
    """
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = callback.data or ""
    try:
        parts = data.split(":")
        # варіанти:
        # ["hist", "<id>"]
        # ["hist", "<id>", "<page>"]
        student_id = int(parts[1])
        if len(parts) >= 3:
            page = int(parts[2])
        else:
            page = 0
    except Exception:
        await callback.answer("Некоректні дані кнопки.", show_alert=True)
        return

    await _render_history_page(callback.message, student_id=student_id, page=page)
    await callback.answer()


@router.message(F.text.startswith("/groups"))
async def admin_groups(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Group))
        groups = res.scalars().all()

    if not groups:
        await message.answer("Груп поки немає.")
        return

    lines: list[str] = ["Список груп:"]
    for g in groups:
        teacher_info = "не призначено"
        if g.teacher_id:
            teacher_info = f"teacher_id={g.teacher_id}"
        lines.append(f"{g.id}: {g.name} (вчитель: {teacher_info})")

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
