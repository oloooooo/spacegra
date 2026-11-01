"""本项目新增的**非密钥**配置：仓库根 `config.yaml`（逐键说明见 docs/configuration.md §3.2）。

密钥不进这个文件，仍然只走环境变量：JWT_SECRET / PASSWORD_PEPPER /
S3_ACCESS_KEY / S3_SECRET_KEY / OPENAI_API_KEY。aegra 原生配置走 `aegra.json` 与
`src/aegra_api/settings.py`，也不在这里。

优先级：同名环境变量 > config.yaml > 本文件的默认值。环境变量优先是为了容器里改一个键
不用重打镜像、单测能 `monkeypatch.setenv` 覆盖。

文件在 import 时读一次：改完必须重启进程（与 `aegra.json` 一致）。值类型写错会直接抛
`ValueError`，不会静默退回默认值——配置写错应该起不来，而不是悄悄按默认值跑。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "config.yaml"

# config.yaml 的键 -> (可覆盖它的环境变量, 默认值)。默认值 = 原本散在各模块里的常量。
_SPEC: dict[str, tuple[str, Any]] = {
    "quota.max_threads_per_user": ("MAX_THREADS_PER_USER", 50),
    "auth.service_identity": ("SERVICE_IDENTITY", "subagent_service"),
    "auth.access_token_ttl": ("ACCESS_TOKEN_TTL", 3600),
    "session_files.max_upload_mb": ("MAX_UPLOAD_MB", 10),
    "session_files.max_files_per_session": ("MAX_FILES_PER_SESSION", 50),
    "session_files.upload_ttl_minutes": ("UPLOAD_TTL_MINUTES", 4320),
    "xlsx.max_blob_mb": ("MAX_BLOB_MB", 10),
    "xlsx.max_cells": ("MAX_CELLS", 2_000_000),
    "xlsx.max_xlsx_files_per_session": ("MAX_XLSX_FILES_PER_SESSION", 20),
    "xlsx.max_md_files_per_session": ("MAX_MD_FILES_PER_SESSION", 10),
    "xlsx.max_blob_mb_per_user": ("MAX_BLOB_MB_PER_USER", 2048),
    "xlsx.cache_dir": ("XLSX_CACHE_DIR", ".cache/xlsx"),
    "xlsx.cache_ttl_days": ("XLSX_CACHE_TTL_DAYS", 7),
    "xlsx.query_budget_ms": ("XLSX_QUERY_BUDGET_MS", 5000),
    "s3.endpoint": ("S3_ENDPOINT", ""),
    "s3.bucket": ("S3_BUCKET", "aegra-blobs"),
    "s3.region": ("S3_REGION", "us-east-1"),
    "mirage.redis_url": ("MIRAGE_REDIS_URL", ""),
    "mirage.cache_limit": ("MIRAGE_CACHE_LIMIT", "256MB"),
    "mirage.index_ttl_seconds": ("MIRAGE_INDEX_TTL_SECONDS", 600),
    "okf.backend": ("OKF_BACKEND", "local"),
    "okf.dir": ("OKF_DIR", "knowledge"),
    "okf.mirror_dir": ("OKF_MIRROR_DIR", ".cache/okf"),
    "okf.mirror_ttl_seconds": ("OKF_MIRROR_TTL_SECONDS", 0.0),
    "agents.server_url": ("AEGRA_SERVER_URL", "http://127.0.0.1:2026"),
    "outbox.poll_interval_seconds": ("OUTBOX_POLL_INTERVAL_SECONDS", 1.0),
    "outbox.batch_size": ("OUTBOX_BATCH_SIZE", 50),
    "outbox.lease_seconds": ("OUTBOX_LEASE_SECONDS", 30.0),
    "outbox.busy_backoff_seconds": ("OUTBOX_BUSY_BACKOFF_SECONDS", 2.0),
    "outbox.max_attempts": ("OUTBOX_MAX_ATTEMPTS", 10),
    "outbox.startup_delay_seconds": ("OUTBOX_STARTUP_DELAY_SECONDS", 5.0),
    "outbox.summary_timeout_seconds": ("OUTBOX_SUMMARY_TIMEOUT_SECONDS", 5.0),
    "outbox.internal_url": ("AEGRA_INTERNAL_URL", None),  # None = 用服务端口拼 IPv4 回环
}


def _read_file() -> dict[str, Any]:
    if not PATH.is_file():
        return {}
    try:
        data = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:  # 配置语法错就起不来，别带着半份配置跑
        raise ValueError(f"{PATH} 不是合法 YAML：{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{PATH} 顶层必须是映射（key: value），实际是 {type(data).__name__}")
    return data


_FILE = _read_file()


def _from_file(key: str) -> Any:
    node: Any = _FILE
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _coerce(key: str, raw: Any, default: Any) -> Any:
    if default is None:
        return str(raw)
    if isinstance(default, bool):
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"配置 {key} 需要数字，实际是 {raw!r}") from e
    return str(raw)


def get(key: str) -> Any:
    """取一个配置值（键名见 `_SPEC`，如 `xlsx.max_cells`）。"""
    env_name, default = _SPEC[key]
    raw: Any = os.environ.get(env_name) or _from_file(key)
    if raw is None or raw == "":
        return default
    return _coerce(key, raw, default)
