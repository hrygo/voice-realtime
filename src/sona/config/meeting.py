"""会议助手 PostgreSQL、纪要和恢复 journal 配置。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sona.config.lm_studio import DEFAULT_LLM_MODEL
from sona.config.validators import validate_listen_host, validate_service_url


class MeetingSettings(BaseSettings):
    """会议助手 PostgreSQL、纪要和恢复 journal 配置。"""

    model_config = SettingsConfigDict(
        env_prefix="SONA_MEETING_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql:///knowledge",
        description="本机 PostgreSQL DSN；不得把完整 DSN 写入日志",
    )
    schema_name: str = Field(
        default="sona",
        validation_alias=AliasChoices("schema", "SONA_MEETING_SCHEMA"),
        serialization_alias="schema",
        description="会议表所在独立 schema",
    )
    summary_model: str = Field(default=DEFAULT_LLM_MODEL, description="会后纪要模型 ID")
    summary_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    summary_reasoning: str = Field(default="off", description="纪要推理开关，首版固定 off")
    inner_os_enabled: bool = Field(default=False, description="是否启用会议内心 OS")
    inner_os_analysis_enabled: bool = Field(
        default=False, description="是否启用 Inner OS analysis/mixed 意图"
    )
    inner_os_cache_ttl_secs: int = Field(default=1800, ge=60, le=86_400)
    inner_os_max_cache_entries: int = Field(default=128, ge=1, le=10_000)
    inner_os_max_cache_bytes: int = Field(
        default=4 * 1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024
    )
    inner_os_cancel_timeout_secs: float = Field(default=2.0, ge=0.1, le=10.0)
    inner_os_fact_timeout_secs: float = Field(default=15.0, ge=1.0, le=120.0)
    inner_os_analysis_timeout_secs: float = Field(default=35.0, ge=1.0, le=300.0)
    inner_os_max_output_chars: int = Field(default=65_536, ge=2_048, le=262_144)
    inner_os_max_context_chars: int = Field(default=48_000, ge=4_000, le=192_000)
    inner_os_recent_context_chars: int = Field(default=16_000, ge=1_000, le=96_000)
    summary_timeout_secs: float = Field(
        default=60.0,
        ge=5.0,
        le=600.0,
        description="纪要 LM Studio 流式读取空闲超时（秒，兼容旧配置）",
    )
    summary_request_timeout_secs: float = Field(
        default=180.0, ge=5.0, le=600.0, description="单次纪要模型调用总时限（秒）"
    )
    summary_job_timeout_secs: float = Field(
        default=600.0, ge=30.0, le=1800.0, description="整条纪要任务总时限（秒）"
    )
    summary_map_max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    summary_reduce_max_output_tokens: int = Field(default=10240, ge=256, le=16384)
    summary_title_max_output_tokens: int = Field(default=128, ge=32, le=512)
    summary_max_output_chars: int = Field(default=65_536, ge=2_048, le=262_144)
    summary_max_input_chars: int = Field(default=20_000, ge=4_000, le=96_000)
    summary_chunk_max_duration_ms: int = Field(
        default=1_200_000, ge=60_000, le=7_200_000, description="单个 map chunk 最大时长"
    )
    summary_chunk_overlap_segments: int = Field(default=1, ge=0, le=10)
    finalization_timeout_secs: float = Field(default=8.0, ge=1.0, le=300.0)
    recovery_dir: Path = Field(default=Path("runtime/meetings/recovery"))
    summary_concurrency: int = Field(default=1, ge=1, le=8)
    diarization_smoothing_enabled: bool = Field(
        default=True,
        description="是否启用会议说话人时序平滑与短片段杂音滤波",
    )
    diarization_min_duration_ms: int = Field(
        default=350,
        ge=50,
        le=2000,
        description="短片段过滤最小有效时长（毫秒）",
    )
    diarization_hangover_gap_ms: int = Field(
        default=800,
        ge=100,
        le=5000,
        description="同一说话人相邻段落合并最大时间间隙（毫秒）",
    )
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8100",
            "http://localhost:8100",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"postgresql", "postgres"}:
            raise ValueError("会议数据库必须使用 PostgreSQL URL")
        if parsed.hostname is not None:
            validate_listen_host(parsed.hostname)
        elif not parsed.path.lstrip("/"):
            raise ValueError("PostgreSQL URL 必须指定数据库")
        return value

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        if re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value) is None:
            raise ValueError("schema 必须是安全的 PostgreSQL 标识符")
        return value

    @field_validator("summary_reasoning")
    @classmethod
    def _validate_reasoning(cls, value: str) -> str:
        if value != "off":
            raise ValueError("会议纪要首版只允许 summary_reasoning=off")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, values: list[str]) -> list[str]:
        return [validate_service_url(value) for value in values]
