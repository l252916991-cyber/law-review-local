"""
配置管理模块

统一管理系统配置，支持环境变量覆盖
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LLMConfig:
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    timeout: int

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=os.getenv("LAW_REVIEW_LLM_URL", "http://127.0.0.1:8000/v1").rstrip("/"),
            model=os.getenv("LAW_REVIEW_LLM_MODEL", "Qwythos-9B-v2-4bit-mlx"),
            temperature=float(os.getenv("LAW_REVIEW_LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.getenv("LAW_REVIEW_LLM_MAX_TOKENS", "900")),
            timeout=int(os.getenv("LAW_REVIEW_LLM_TIMEOUT", "180")),
        )


@dataclass
class RedisConfig:
    url: str
    queue_name: str
    max_jobs: int
    job_timeout: int

    @classmethod
    def from_env(cls) -> "RedisConfig":
        return cls(
            url=os.getenv("REDIS_URL", "redis://localhost:6379"),
            queue_name=os.getenv("ARQ_QUEUE_NAME", "law_review_tasks"),
            max_jobs=int(os.getenv("ARQ_MAX_JOBS", "10")),
            job_timeout=int(os.getenv("ARQ_JOB_TIMEOUT", "3600")),
        )


@dataclass
class AppConfig:
    max_upload_size: int
    max_files_per_upload: int
    batch_upload_limit: int
    max_upload_total_size: int
    indexing_concurrency: int
    llm: LLMConfig
    redis: RedisConfig

    @property
    def data_dir(self) -> Path:
        from .db import get_db_path
        return get_db_path().parent

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def db_path(self) -> Path:
        from .db import get_db_path
        return get_db_path()

    @classmethod
    def from_env(cls) -> "AppConfig":
        values = cls(
            max_upload_size=int(os.getenv("LAW_REVIEW_MAX_UPLOAD_SIZE", str(200 * 1024 * 1024))),
            max_files_per_upload=int(os.getenv("LAW_REVIEW_MAX_FILES_PER_UPLOAD", "30")),
            batch_upload_limit=int(os.getenv("LAW_REVIEW_BATCH_UPLOAD_LIMIT", "200")),
            max_upload_total_size=int(os.getenv("LAW_REVIEW_MAX_UPLOAD_TOTAL_SIZE", str(512 * 1024 * 1024))),
            indexing_concurrency=int(os.getenv("LAW_REVIEW_INDEXING_CONCURRENCY", "2")),
            llm=LLMConfig.from_env(),
            redis=RedisConfig.from_env(),
        )
        for name in ("max_upload_size", "max_files_per_upload", "batch_upload_limit", "max_upload_total_size", "indexing_concurrency"):
            if getattr(values, name) < 1:
                raise ValueError(f"{name} must be positive")
        return values


config = AppConfig.from_env()
