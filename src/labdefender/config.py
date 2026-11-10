from typing import Literal

from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    bot_token: SecretStr
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/labdefender.sqlite3", min_length=1
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("bot_token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        try:
            validate_token(value.get_secret_value())
        except TokenValidationError:
            raise ValueError("Нужен токен из BotFather") from None
        return value

    @field_validator("database_url")
    @classmethod
    def sqlite_only(cls, value: str) -> str:
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("Некорректный DATABASE_URL") from None
        if url.drivername != "sqlite+aiosqlite" or not url.database:
            raise ValueError("В MVP используется sqlite+aiosqlite с указанным путём к базе")
        return value
