"""会议 PostgreSQL schema 的版本化、可重复 migration runner。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import cast

from psycopg import AsyncConnection

MIGRATION_LOCK_ID = 7_294_610_381
_SCHEMA_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")


class MigrationError(RuntimeError):
    """migration 无法安全应用或检测到内容漂移。"""


def validate_schema_name(schema: str) -> str:
    """验证供 SQL 标识符插值使用的 schema 名称。"""
    if _SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError("schema 必须是安全的 PostgreSQL 标识符")
    return schema


def _migration_dir() -> Path:
    return Path(__file__).with_name("migrations")


def _migration_files() -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    for path in _migration_dir().glob("[0-9][0-9][0-9][0-9]_*.sql"):
        version_text = path.name.split("_", 1)[0]
        files.append((int(version_text), path))
    return sorted(files)


async def apply_migrations(connection: AsyncConnection[tuple[object, ...]], *, schema: str) -> None:
    """在一条连接上应用所有未执行 migration。

    调用方负责事先创建 schema；运行时应用角色只需拥有该 schema 的
    CREATE/USAGE 权限，不需要数据库级 CREATE 或超级用户权限。
    """
    schema = validate_schema_name(schema)
    qualified_schema = f'"{schema}"'
    files = _migration_files()
    if not files:
        raise MigrationError("未找到会议 migration 文件")

    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        await connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {qualified_schema}.schema_migrations (
                version integer PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cursor = await connection.execute(
            f"SELECT version, checksum FROM {qualified_schema}.schema_migrations"
        )
        applied = {
            int(cast(int, row[0])): str(row[1])
            for row in await cursor.fetchall()
        }

        for version, path in files:
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            existing = applied.get(version)
            if existing is not None:
                if existing != checksum:
                    raise MigrationError(f"migration {version:04d} checksum 不一致")
                continue

            rendered = sql.replace("__SCHEMA__", qualified_schema)
            await connection.execute(rendered)
            await connection.execute(
                f"""
                INSERT INTO {qualified_schema}.schema_migrations (version, checksum)
                VALUES (%s, %s)
                """,
                (version, checksum),
            )


async def run_migrations(database_url: str, *, schema: str) -> None:
    """打开短生命周期连接并应用会议 schema migration。"""
    async with await AsyncConnection.connect(database_url) as connection:
        await apply_migrations(connection, schema=schema)
