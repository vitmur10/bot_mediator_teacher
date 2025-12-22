import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import get_settings
from db import init_db
from handlers import setup_routers


async def main():
    settings = get_settings()
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(setup_routers())

    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
