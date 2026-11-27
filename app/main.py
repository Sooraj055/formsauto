"""FastAPI backend for FormRush — Smart Google Forms Rapid Autofiller."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.automation import FormAutomationEngine
from app.config import FRONTEND_DIR, logger, redact_sensitive
from app.models import (
    HistoryEntry,
    ProfileUpdateRequest,
    ResumeRunRequest,
    RunStatus,
    RunSummary,
    StartRunRequest,
    UserProfile,
)
from app.storage import SecureStorage

TERMINAL_STATUSES = {
    RunStatus.SUBMITTED,
    RunStatus.BLOCKED,
    RunStatus.FAILED,
    RunStatus.UNKNOWN,
    RunStatus.UNCERTAIN_TIMEOUT,
    RunStatus.DUPLICATE_BLOCKED,
}

# Derive HMAC signing key from ephemeral secret or env so tokens don't leak PII
_TOKEN_SIGNING_KEY = os.getenv("FORMRUSH_SECRET_KEY", os.urandom(32).hex()).encode("utf-8")


def _issue_session_token(ttl_seconds: int = 86400 * 7) -> str:
    exp = int(time.time()) + ttl_seconds
    msg = f"formrush:{exp}".encode("utf-8")
    sig = hmac.new(_TOKEN_SIGNING_KEY, msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _verify_session_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    exp_str, sig = token.split(".", 1)
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    expected = hmac.new(_TOKEN_SIGNING_KEY, f"formrush:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


class UnlockRequest(BaseModel):
    pin: str = Field(..., min_length=1, max_length=64)


class PinConfigRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=64)


def create_app(storage: SecureStorage | None = None) -> FastAPI:
    store = storage or SecureStorage()
    engine = FormAutomationEngine(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.storage = store
        app.state.engine = engine
        await engine.startup()
        try:
            yield
        finally:
            await engine.shutdown()

    app = FastAPI(
        title="FormRush",
        description="Smart Google Forms Rapid Autofiller for first-come, first-served registrations.",
        version="1.0.0",
        lifespan=lifespan,
    )

    allowed_origins = [o.strip() for o in os.getenv("FORMRUSH_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-FormRush-Token"],
    )

    def require_auth(
        x_formrush_token: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ) -> None:
        if not store.has_pin():
            return
        supplied = x_formrush_token or token or ""
        if not _verify_session_token(supplied):
            raise HTTPException(status_code=401, detail="PIN unlock required.")

    @app.exception_handler(Exception)
    async def safe_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        profile_vals: list[str] = []
        try:
            p = store.get_profile()
            profile_vals = [p.full_name, p.mobile_number, p.email, p.address_locality, p.pin_code]
        except Exception:
            pass
        safe_msg = redact_sensitive(str(exc), profile_vals)
        logger.error("Unhandled error (%s): %s", type(exc).__name__, safe_msg)
        return JSONResponse(status_code=500, content={"detail": "Internal server error."})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "pin_required": store.has_pin(),
            "browser_ready": bool(engine._browser and engine._browser.is_connected()),
        }

    @app.get("/api/auth/status")
    async def auth_status(
        x_formrush_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        pin_req = store.has_pin()
        unlocked = True if not pin_req else _verify_session_token(x_formrush_token or "")
        return {"pin_required": pin_req, "unlocked": unlocked}

    @app.post("/api/auth/unlock")
    async def unlock(req: UnlockRequest) -> dict[str, Any]:
        if not store.has_pin():
            return {"unlocked": True, "token": _issue_session_token()}
        if not store.verify_pin(req.pin):
            raise HTTPException(status_code=401, detail="Incorrect PIN.")
        return {"unlocked": True, "token": _issue_session_token()}

    @app.post("/api/auth/pin", dependencies=[Depends(require_auth)])
    async def configure_pin(req: PinConfigRequest) -> dict[str, Any]:
        if os.getenv("FORMRUSH_ACCESS_PIN", "").strip():
            raise HTTPException(
                status_code=400,
                detail="PIN is managed by FORMRUSH_ACCESS_PIN environment variable.",
            )
        cleaned = req.pin.strip() if req.pin else None
        if cleaned is not None and len(cleaned) < 4:
            raise HTTPException(status_code=400, detail="PIN must be at least 4 characters.")
        store.set_pin(cleaned)
        token = _issue_session_token() if cleaned else ""
        return {"pin_required": store.has_pin(), "token": token}

    @app.get("/api/profile", response_model=UserProfile, dependencies=[Depends(require_auth)])
    async def get_profile() -> UserProfile:
        return store.get_profile()

    @app.put("/api/profile", response_model=UserProfile, dependencies=[Depends(require_auth)])
    async def update_profile(req: ProfileUpdateRequest) -> UserProfile:
        profile = UserProfile(**req.model_dump())
        return store.save_profile(profile)

    @app.get("/api/history", response_model=list[HistoryEntry], dependencies=[Depends(require_auth)])
    async def get_history(limit: int = Query(default=20, ge=1, le=100)) -> list[HistoryEntry]:
        return store.list_history(limit=limit)

    @app.post("/api/runs/start", response_model=RunSummary, dependencies=[Depends(require_auth)])
    async def start_run(req: StartRunRequest) -> RunSummary:
        try:
            return await engine.start_run(req.url, force_resubmit=req.force_resubmit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}", response_model=RunSummary, dependencies=[Depends(require_auth)])
    async def get_run_status(run_id: str) -> RunSummary:
        sess = engine.get_session(run_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Run session not found.")
        return sess.to_summary()

    @app.get("/api/runs/{run_id}/events", dependencies=[Depends(require_auth)])
    async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
        q = await engine.subscribe(run_id)
        if q is None:
            raise HTTPException(status_code=404, detail="Run session not found.")

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {json.dumps(payload)}\n\n"
                        if payload.get("status") in {s.value for s in TERMINAL_STATUSES}:
                            break
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                engine.unsubscribe(run_id, q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/resume", response_model=RunSummary, dependencies=[Depends(require_auth)])
    async def resume_run(run_id: str, req: ResumeRunRequest) -> RunSummary:
        try:
            return await engine.resume_run(run_id, req.answers)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(require_auth)])
    async def cancel_run(run_id: str) -> dict[str, str]:
        await engine.cancel_run(run_id)
        return {"status": "cancelled"}

    if FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

        @app.get("/")
        async def serve_index() -> FileResponse:
            return FileResponse(str(FRONTEND_DIR / "index.html"))

    return app


app = create_app()
