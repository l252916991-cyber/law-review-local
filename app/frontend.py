"""Serve only the public Next.js export; never expose source or runtime data."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re

from fastapi import HTTPException
from fastapi.responses import FileResponse

DEFAULT_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend" / "out"


def frontend_dir() -> Path:
    return Path(os.getenv("LAW_REVIEW_FRONTEND_DIR") or DEFAULT_FRONTEND_DIR).resolve()


def frontend_shell() -> FileResponse:
    root = frontend_dir()
    path = root / "index.html"
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise HTTPException(503, "前端尚未构建，请在 frontend 执行 pnpm install --frozen-lockfile 和 pnpm build:static")
    # Next embeds hydration data in inline scripts. Bind the exact compiled
    # scripts by hash instead of allowing arbitrary inline JavaScript.
    html = path.read_bytes().decode("utf-8")
    hashes = {
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii") + "'"
        for attributes, body in re.findall(r"<script\b([^>]*)>(.*?)</script\s*>", html, re.DOTALL | re.IGNORECASE)
        if not re.search(r"\bsrc\s*=", attributes, re.IGNORECASE)
    }
    policy = (
        "default-src 'self'; script-src 'self' " + " ".join(sorted(hashes)) + "; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    return FileResponse(path, media_type="text/html", headers={"Content-Security-Policy": policy, "Cache-Control": "no-cache"})


def frontend_asset(asset_path: str) -> FileResponse:
    public = frontend_dir()
    root = (public / "_next").resolve()
    if not root.is_relative_to(public):
        raise HTTPException(404, "前端资源不存在")
    candidate = (root / asset_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(404, "前端资源不存在")
    if candidate.suffix.lower() not in {".js", ".css", ".woff", ".woff2", ".svg", ".png", ".jpg", ".webp", ".ico"}:
        raise HTTPException(404, "前端资源不存在")
    return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
