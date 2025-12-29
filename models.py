from sqlalchemy import (
    Column,
    BigInteger,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from db import Base


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    teacher_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)

    # Вчитель групи (один) – явно кажемо, що FK = teacher_id
    teacher = relationship(
        "User",
        foreign_keys=[teacher_id],
        backref="teaching_groups",  # teacher.teaching_groups
    )

    # Учні групи (багато) – явно кажемо, що FK на нас це User.group_id
    students = relationship(
        "User",
        back_populates="group",
        foreign_keys="User.group_id",
    )



class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)  # telegram user_id
    role = Column(String(20), nullable=False, default="student")
    display_name = Column(String(255), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)

    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    group = relationship("Group", back_populates="students", foreign_keys=[group_id])

    # ✅ 1-на-1: учень -> викладач
    assigned_teacher_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)

    assigned_teacher = relationship(
        "User",
        foreign_keys=[assigned_teacher_id],
        remote_side=[id],                 # ✅ ключовий фікс
        back_populates="assigned_students",
    )

    # ✅ викладач -> список учнів
    assigned_students = relationship(
        "User",
        foreign_keys=[assigned_teacher_id],
        back_populates="assigned_teacher",
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    to_user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    direction = Column(String(32), nullable=False)  # student_to_teacher / teacher_to_student
    text = Column(Text, nullable=True)
    tg_message_id = Column(BigInteger, nullable=True)
    replied_to_tg_message_id = Column(BigInteger, nullable=True)
    has_media = Column(Boolean, default=False)
    media_file_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    from_user = relationship("User", foreign_keys=[from_user_id])
    to_user = relationship("User", foreign_keys=[to_user_id])
    media_kind = Column(String(16), nullable=True)


class TeacherMessageLink(Base):
    """
    Мапа: message_id у чаті вчителя -> студент.
    Щоб по Reply зрозуміти, кому відповідати.
    """
    __tablename__ = "teacher_message_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_tg_message_id = Column(BigInteger, index=True, unique=True)
    student_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
