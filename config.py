import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    bot_token: str
    db_url: str


def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def get_settings() -> Settings:
    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        db_url=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///bot.db"),
    )
