import re
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from nanoid import generate as nanoid_generate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = "shortener.db"
RESERVED_CODES = {"api", "admin", "static", "favicon.ico"}
CUSTOM_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")

# ---------------------------------------------------------------------------
# DB helpers  — fresh connection per call to stay thread-safe under Uvicorn
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS urls (
                code       TEXT PRIMARY KEY,
                original   TEXT NOT NULL,
                clicks     INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="lnkshrnk — URL Shortener")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# Also ensure DB exists on import (covers edge cases where startup event
# may not fire in some test runners).
init_db()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class ShortenRequest(BaseModel):
    url: str
    custom: Optional[str] = None


# ---------------------------------------------------------------------------
# API routes  — must be defined BEFORE the catch-all redirect & static mount
# ---------------------------------------------------------------------------

@app.post("/api/shorten")
def shorten(req: ShortenRequest, request: Request):
    # 1. Validate URL scheme
    if not (req.url.startswith("http://") or req.url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # 2. Validate custom code if provided
    if req.custom is not None and req.custom != "":
        if not CUSTOM_CODE_RE.match(req.custom):
            raise HTTPException(
                status_code=400,
                detail="Custom code must be 3-20 characters and contain only letters, numbers, hyphen or underscore",
            )
        if req.custom.lower() in RESERVED_CODES:
            raise HTTPException(
                status_code=400,
                detail=f"'{req.custom}' is a reserved word and cannot be used as a custom code",
            )
    else:
        # Normalise empty string to None
        req.custom = None

    conn = get_conn()
    try:
        # 3. Dedup: if this exact original URL already exists, return existing code
        #    (but if a custom code was requested, we still honour that request
        #    instead of silently returning the old mapping)
        if req.custom is None:
            try:
                row = conn.execute(
                    "SELECT code FROM urls WHERE original = ?", (req.url,)
                ).fetchone()
            except sqlite3.Error:
                raise HTTPException(status_code=500, detail="Database error")
            if row:
                code = row["code"]
                host = str(request.base_url).rstrip("/")
                return {"short": f"{host}/{code}"}

        # 4. Custom code path
        if req.custom is not None:
            try:
                conn.execute(
                    "INSERT INTO urls (code, original) VALUES (?, ?)",
                    (req.custom, req.url),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="Custom code already taken")
            except sqlite3.Error:
                raise HTTPException(status_code=500, detail="Database error")
            host = str(request.base_url).rstrip("/")
            return {"short": f"{host}/{req.custom}"}

        # 5. Auto-generated nanoid path with collision retry
        last_error: Optional[Exception] = None
        for _ in range(5):
            code = nanoid_generate(size=6)
            try:
                conn.execute(
                    "INSERT INTO urls (code, original) VALUES (?, ?)",
                    (code, req.url),
                )
                conn.commit()
                host = str(request.base_url).rstrip("/")
                return {"short": f"{host}/{code}"}
            except sqlite3.IntegrityError as exc:
                last_error = exc
                continue
            except sqlite3.Error:
                raise HTTPException(status_code=500, detail="Database error")

        raise HTTPException(status_code=500, detail="Failed to generate unique code, please try again")

    finally:
        conn.close()


@app.get("/api/stats/{code}")
def stats(code: str):
    conn = get_conn()
    try:
        try:
            row = conn.execute(
                "SELECT code, original, clicks, created_at FROM urls WHERE code = ?",
                (code,),
            ).fetchone()
        except sqlite3.Error:
            raise HTTPException(status_code=500, detail="Database error")
        if not row:
            raise HTTPException(status_code=404, detail="Short code not found")
        return {
            "code": row["code"],
            "original": row["original"],
            "clicks": row["clicks"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Redirect route — registered AFTER all /api/* routes to avoid
# shadowing them. NOTE: the StaticFiles mount below is defined AFTER this
# route (per spec), so requests for real files like /style.css would
# otherwise hit this handler first. To avoid that, we detect file-like
# codes (containing a dot) and serve the static file directly via
# FileResponse, letting the mount handle "/" and fallback cases.
# ---------------------------------------------------------------------------

@app.get("/{code}")
def redirect_to_original(code: str):
    # If code looks like a file (contains a dot), try to serve it from
    # public/ directly so static assets aren't shadowed by the DB lookup.
    if "." in code:
        file_path = Path("public") / code
        if file_path.is_file():
            return FileResponse(str(file_path))
        raise HTTPException(status_code=404, detail="Not Found")

    conn = get_conn()
    try:
        try:
            row = conn.execute(
                "SELECT original FROM urls WHERE code = ?", (code,)
            ).fetchone()
        except sqlite3.Error:
            raise HTTPException(status_code=500, detail="Database error")

        if not row:
            raise HTTPException(status_code=404, detail="Short code not found")

        # Increment clicks
        try:
            conn.execute("UPDATE urls SET clicks = clicks + 1 WHERE code = ?", (code,))
            conn.commit()
        except sqlite3.Error:
            # Non-critical — still redirect even if counter update fails
            pass

        return RedirectResponse(url=row["original"], status_code=307)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Static files — mounted AFTER all API routes and the redirect route
# (per spec) using StaticFiles(directory="public", html=True).
# Serves index.html for "/" and any remaining static assets.
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="public", html=True), name="static")

# ---------------------------------------------------------------------------
# Deployment notes for Render (free tier)
# ---------------------------------------------------------------------------
# Runtime:       Python 3
# Build command: pip install -r requirements.txt
# Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
#
# Free tier caveats:
# - Filesystem is ephemeral and resets on redeploy — SQLite data will NOT
#   persist across deploys. Acceptable for now; migrate to Postgres/Redis
#   for persistence if needed.
# - Service sleeps after ~15 min of inactivity; first request after sleep
#   takes ~30-50 s to wake (cold start).
