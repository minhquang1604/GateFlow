"""Application settings using Pydantic Settings."""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database configuration
    database_url: str = Field(
        default="postgresql+psycopg://postgres@localhost:5432/mlops_framework",
        description="Database connection URL",
    )

    # Database pool settings
    database_pool_size: int = Field(
        default=5,
        description="Database connection pool size",
    )
    database_max_overflow: int = Field(
        default=10,
        description="Maximum number of connections that can be created beyond pool_size",
    )
    database_pool_timeout: int = Field(
        default=30,
        description="Seconds to wait for a connection from the pool",
    )
    database_echo: bool = Field(
        default=False,
        description="Echo SQL queries to stdout",
    )

    # Application settings
    app_name: str = Field(
        default="mlops-framework",
        description="Application name",
    )
    app_version: str = Field(
        default="0.1.0",
        description="Application version",
    )

    # Development settings
    debug: bool = Field(
        default=False,
        description="Enable debug mode",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Returns:
        Settings: Application settings instance
    """
    return Settings()
