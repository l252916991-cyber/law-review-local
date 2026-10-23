"""
配置管理模块

统一管理系统配置，支持环境变量覆盖
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

# Operator-editable in the workspace UI; every other field stays deployment-only.
MODEL_OVERRIDE_FILENAME = "model_settings.json"
MODEL_OVERRIDE_FIELDS = ("base_url", "model")


def model_override_path() -> Path:
    from .db import get_db_path

    return get_db_path().parent / MODEL_OVERRIDE_FILENAME


def load_model_override() -> dict[str, str]:
    """Return the persisted in-app model override, or {} when absent.

    An unreadable or malformed file falls back to environment defaults rather
    than taking the model service down.
    """
    try:
        raw = json.loads(model_override_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    override: dict[str, str] = {}
    for field in MODEL_OVERRIDE_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            override[field] = value.strip()
    return override


def save_model_override(values: dict[str, str]) -> None:
    path = model_override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".model_settings-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def clear_model_override() -> None:
    model_override_path().unlink(missing_ok=True)


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

    @classmethod
    def load(cls) -> "LLMConfig":
        """Effective config: the persisted in-app override over environment defaults.

        Resolved per call, so a saved change applies to the next model call in both
        the Web and Worker processes without a restart.
        """
        override = load_model_override()
        if not override:
            return cls.from_env()
        return cls(**{**asdict(cls.from_env()), **override})


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
