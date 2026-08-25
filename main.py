"""
lnkshrnk — URL shortener
FastAPI + SQLite + nanoid, single-file backend.

Routes:
  POST /api/shorten  -> create short code (auto nanoid or custom)
  GET  /api/stats/{code} -> stats for a code
  GET  /{code} -> 307 redirect + click counting (or static file fallback)
  GET  /health -> liveness probe (used by Render / local)
  GET  /api/recent -> last N mappings

DB: SQLite (shortener.db) with fresh connection per request for thread safety
under Uvicorn. Table `urls` holds code (PK), original, clicks, created_at.

Spec notes preserved:
  - Custom code regex ^[A-Za-z0-9_-]{3,20}$ and reserved set {api,admin,static,favicon.ico}
  - Dedup by original URL when no custom code supplied
  - nanoid(size=6) with 5 collision retries
  - StaticFiles mount AFTER redirect route (spec requirement) + dot-file fallback
"""

import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from nanoid import generate as nanoid_generate

# ---------------------------------------------------------------------------
# Logging & config
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lnkshrnk")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("DB_PATH", "shortener.db")
RESERVED_CODES = {"api", "admin", "static", "favicon.ico"}
CUSTOM_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
NANOID_SIZE = 6
NANOID_RETRIES = 5
NANOID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
DEFAULT_RECENT_LIMIT = 10
MAX_RECENT_LIMIT = 50

# ---------------------------------------------------------------------------
# DB helpers  — fresh connection per call to stay thread-safe under Uvicorn
# ---------------------------------------------------------------------------


def get_conn() -> sqlite3.Connection:
    """Return a fresh SQLite connection with Row factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL mode improves concurrent reads under Uvicorn workers (best-effort).
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except sqlite3.Error:
        pass
    return conn


def init_db() -> None:
    """Create tables / indexes if they do not exist."""
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
        # Index for dedup lookup by original URL (frequent path when custom is None).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_original ON urls(original)")
        # Index for recency ordering.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_created_at ON urls(created_at)")
        conn.commit()
        logger.debug("DB initialised at %s", DB_PATH)
    finally:
        conn.close()


def db_healthcheck() -> bool:
    """Simple SELECT 1 probe — returns True if DB is reachable."""
    try:
        conn = get_conn()
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Validation helpers — extracted for testability and to increase code clarity
# ---------------------------------------------------------------------------


def is_valid_url(url: str) -> bool:
    """Return True iff url starts with http:// or https:// and has host part."""
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    # Require at least host after scheme, e.g. http://a
    remainder = url.split("://", 1)[-1]
    return len(remainder) > 0 and "." in remainder or remainder == "localhost" or "/" in remainder or len(remainder) >= 3


def normalize_custom(custom: Optional[str]) -> Optional[str]:
    """Normalise empty string to None, strip whitespace if present."""
    if custom is None:
        return None
    custom = custom.strip()
    return custom if custom != "" else None


def validate_custom_code(code: str) -> Optional[str]:
    """
    Validate a custom code.
    Returns None if valid, else an error detail string for HTTPException.
    """
    if not CUSTOM_CODE_RE.match(code):
        return "Custom code must be 3-20 characters and contain only letters, numbers, hyphen or underscore"
    if code.lower() in RESERVED_CODES:
        return f"'{code}' is a reserved word and cannot be used as a custom code"
    return None


def build_short_url(request: Request, code: str) -> str:
    """Build absolute short URL from request base and code."""
    host = str(request.base_url).rstrip("/")
    return f"{host}/{code}"


def fetch_existing_code_for_url(conn: sqlite3.Connection, url: str) -> Optional[str]:
    """Dedup lookup — return existing code for original URL if any."""
    try:
        row = conn.execute("SELECT code FROM urls WHERE original = ? LIMIT 1", (url,)).fetchone()
    except sqlite3.Error as exc:
        logger.warning("dedup lookup failed: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")
    return row["code"] if row else None


def try_insert_code(conn: sqlite3.Connection, code: str, original: str) -> None:
    """Attempt to insert (code, original). Raise translated HTTPException on failure."""
    try:
        conn.execute("INSERT INTO urls (code, original) VALUES (?, ?)", (code, original))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Custom code already taken")
    except sqlite3.Error as exc:
        logger.error("insert failed for code=%s: %s", code, exc)
        raise HTTPException(status_code=500, detail="Database error")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="lnkshrnk — URL Shortener",
    description="Paste a long link, get a short code. Custom aliases, dedup, click counting.",
    version="1.1.0",
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    logger.info("lnkshrnk started — DB=%s reserved=%s", DB_PATH, sorted(RESERVED_CODES))


# Also ensure DB exists on import (covers edge cases where startup event
# may not fire in some test runners).
init_db()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ShortenRequest(BaseModel):
    url: str
    custom: Optional[str] = None


class ShortenResponse(BaseModel):
    short: str


class StatsResponse(BaseModel):
    code: str
    original: str
    clicks: int
    created_at: str


# ---------------------------------------------------------------------------
# Utility routes — health & recent (not part of original spec, additive)
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Liveness probe for Render / load balancers."""
    ok = db_healthcheck()
    return {"status": "ok" if ok else "degraded", "db": "up" if ok else "down", "ts": int(time.time())}


@app.get("/api/health")
def api_health():
    """Alias for /health under /api prefix."""
    return health()


@app.get("/api/recent")
def recent(limit: int = DEFAULT_RECENT_LIMIT):
    """Return most recent N mappings, newest first. Additive, does not break spec."""
    limit = max(1, min(limit, MAX_RECENT_LIMIT))
    conn = get_conn()
    try:
        try:
            rows = conn.execute(
                "SELECT code, original, clicks, created_at FROM urls ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            raise HTTPException(status_code=500, detail="Database error")
        return [
            {"code": r["code"], "original": r["original"], "clicks": r["clicks"], "created_at": r["created_at"]}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API routes  — must be defined BEFORE the catch-all redirect & static mount
# ---------------------------------------------------------------------------


@app.post("/api/shorten", response_model=ShortenResponse)
def shorten(req: ShortenRequest, request: Request):
    # 1. Validate URL scheme + basic host check
    if not is_valid_url(req.url):
        # Keep original error phrasing for backwards compat with tests/clients.
        if not (req.url.startswith("http://") or req.url.startswith("https://")):
            raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # 2. Validate & normalize custom code if provided
    req.custom = normalize_custom(req.custom)
    if req.custom is not None:
        err = validate_custom_code(req.custom)
        if err:
            status = 400
            raise HTTPException(status_code=status, detail=err)
    else:
        req.custom = None

    conn = get_conn()
    try:
        # 3. Dedup: if this exact original URL already exists, return existing code
        #    (but if a custom code was requested, we still honour that request
        #    instead of silently returning the old mapping)
        if req.custom is None:
            existing = fetch_existing_code_for_url(conn, req.url)
            if existing:
                return {"short": build_short_url(request, existing)}

        # 4. Custom code path
        if req.custom is not None:
            try_insert_code(conn, req.custom, req.url)
            return {"short": build_short_url(request, req.custom)}

        # 5. Auto-generated nanoid path with collision retry
        last_error: Optional[Exception] = None
        for _ in range(NANOID_RETRIES):
            code = nanoid_generate(size=NANOID_SIZE)
            try:
                conn.execute(
                    "INSERT INTO urls (code, original) VALUES (?, ?)",
                    (code, req.url),
                )
                conn.commit()
                return {"short": build_short_url(request, code)}
            except sqlite3.IntegrityError as exc:
                last_error = exc
                logger.debug("nanoid collision for %s, retrying", code)
                continue
            except sqlite3.Error as exc:
                logger.error("db error during nanoid insert: %s", exc)
                raise HTTPException(status_code=500, detail="Database error")

        logger.error("nanoid exhaustion after %s retries, last_error=%s", NANOID_RETRIES, last_error)
        raise HTTPException(status_code=500, detail="Failed to generate unique code, please try again")

    finally:
        conn.close()


@app.get("/api/stats/{code}", response_model=StatsResponse)
def stats(code: str):
    conn = get_conn()
    try:
        try:
            row = conn.execute(
                "SELECT code, original, clicks, created_at FROM urls WHERE code = ?",
                (code,),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.error("stats lookup failed for %s: %s", code, exc)
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
        # Security: prevent directory traversal — only allow direct files in public/.
        if "/" in code or "\\" in code or code.startswith("."):
            raise HTTPException(status_code=404, detail="Not Found")
        file_path = Path("public") / code
        # Resolve and ensure it stays inside public/
        try:
            resolved = file_path.resolve()
            public_resolved = Path("public").resolve()
            if not str(resolved).startswith(str(public_resolved)):
                raise HTTPException(status_code=404, detail="Not Found")
        except Exception:
            raise HTTPException(status_code=404, detail="Not Found")
        if file_path.is_file():
            return FileResponse(str(file_path))
        raise HTTPException(status_code=404, detail="Not Found")

    conn = get_conn()
    try:
        try:
            row = conn.execute(
                "SELECT original FROM urls WHERE code = ?", (code,)
            ).fetchone()
        except sqlite3.Error as exc:
            logger.error("redirect lookup failed for %s: %s", code, exc)
            raise HTTPException(status_code=500, detail="Database error")

        if not row:
            raise HTTPException(status_code=404, detail="Short code not found")

        # Increment clicks (best-effort, non-fatal if it fails).
        try:
            conn.execute("UPDATE urls SET clicks = clicks + 1 WHERE code = ?", (code,))
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("click increment failed for %s: %s", code, exc)
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


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
