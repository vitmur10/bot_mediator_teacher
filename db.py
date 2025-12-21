from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import get_settings

settings = get_settings()

engine = create_async_engine(settings.db_url, echo=False, future=True)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

Base = declarative_base()


async def init_db() -> None:
    """
    Створення таблиць при старті бота.
    """
    async with engine.begin() as conn:
        from models import User, Message, TeacherMessageLink  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
