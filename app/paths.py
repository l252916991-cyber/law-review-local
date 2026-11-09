"""Filesystem anchors for code that reads files shipped with the repository.

Modules live one or two packages deep, so ``Path(__file__).parents[n]`` depends on
where a module sits. Use these constants instead.
"""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
