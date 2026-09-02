"""LM Studio 共享端点与凭据配置。"""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sona.config.validators import validate_service_url
from sona.lm_studio import DEFAULT_LM_STUDIO_API_KEY

DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"
DEFAULT_LLM_MODEL = "local/kat-coder-2.5"


class LMStudioSettings(BaseSettings):
    """Shared LM Studio endpoint and credential ownership for every workload."""

    model_config = SettingsConfigDict(
        env_prefix="SONA_LM_STUDIO_", env_file=".env", extra="ignore", populate_by_name=True
    )

    base_url: str = Field(
        default=DEFAULT_LM_STUDIO_URL,
        validation_alias=AliasChoices(
            "base_url", "SONA_LM_STUDIO_BASE_URL", "SONA_INTERACTION_LLM_BASE_URL"
        ),
    )
    api_key: str = Field(
        default=DEFAULT_LM_STUDIO_API_KEY,
        min_length=1,
        validation_alias=AliasChoices(
            "api_key", "SONA_LM_STUDIO_API_KEY", "SONA_INTERACTION_LLM_API_KEY"
        ),
    )

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        return validate_service_url(value)

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value
