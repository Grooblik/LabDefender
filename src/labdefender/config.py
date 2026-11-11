from typing import Literal
from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/labdefender.sqlite3", min_length=1
    )

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


class Settings(DatabaseSettings):
    bot_token: SecretStr
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    teacher_registration_code: SecretStr = SecretStr("")
    admin_ids: frozenset[int] = frozenset()
    # Mini App: public HTTPS base address; empty disables the web server and the menu button.
    webapp_url: str = ""
    web_host: str = Field(default="127.0.0.1", min_length=1)
    web_port: int = Field(default=8080, ge=1, le=65535)
    # Maximum age of Telegram initData accepted by the API, in seconds.
    webapp_auth_max_age: int = Field(default=86400, ge=60, le=7 * 86400)
    # Token of a Cloudflare tunnel with a permanent address (WEBAPP_URL); used with --tunnel.
    tunnel_token: SecretStr = SecretStr("")

    @field_validator("webapp_url")
    @classmethod
    def https_webapp_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("WEBAPP_URL должен быть адресом https:// без параметров")
        return value

    @field_validator("tunnel_token")
    @classmethod
    def plain_tunnel_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if any(character.isspace() for character in token) or len(token) > 4096:
            raise ValueError("TUNNEL_TOKEN — одна строка из панели Cloudflare, без пробелов")
        return SecretStr(token)

    @field_validator("teacher_registration_code")
    @classmethod
    def normalize_registration_code(cls, value: SecretStr) -> SecretStr:
        code = value.get_secret_value().strip()
        if code.startswith("/") or len(code) > 4096:
            raise ValueError("Код должен помещаться в сообщение и не начинаться с /")
        return SecretStr(code)

    @field_validator("admin_ids")
    @classmethod
    def valid_admin_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(telegram_id <= 0 or telegram_id >= 2**63 for telegram_id in value):
            raise ValueError("ADMIN_IDS должен содержать положительные Telegram ID")
        return value

    @field_validator("bot_token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        try:
            validate_token(value.get_secret_value())
        except TokenValidationError:
            raise ValueError("Нужен токен из BotFather") from None
        return value
