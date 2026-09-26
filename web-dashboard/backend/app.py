"""
Cloud-Deployable Web Dashboard Backend.

This FastAPI application is fully decoupled from the computer vision pipeline.
It receives gesture events from remote Camera Nodes via POST /api/events/ingest
or the /ws/edge WebSocket, and broadcasts them to browser clients via /ws/events.

ZERO dependencies on: torch, ultralytics, cv2, mediapipe, or any .pt model files.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ConfigDict, field_validator
from dotenv import load_dotenv

from backend.database import DatabaseManager, SafeCsvWriter
from backend.podium_queue import PodiumQueueManager
from backend.schemas import (
    EdgeEventIngest,
    EventActionResponse,
    GestureEventSchema,
    GridConfigureRequest,
    SeatAssignRequest,
    SeatAttendanceToggleResponse,
    SeatBulkUpdateRequest,
    SeatSchema,
    SeatSwapRequest,
    SectionCreateRequest,
    SectionResponse,
    SessionResponse,
    SessionStartRequest,
    StudentEnrollRequest,
    StudentRegisterRequest,
)

# ---------------------------------------------------------------------------
# Global managers
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
DATABASE_PATH = os.getenv("DATABASE_PATH", "hand_tracking.db")
db = DatabaseManager(DATABASE_PATH)
queues: Dict[str, PodiumQueueManager] = {}

def session_queue(session_id):
    return queues.setdefault(session_id, PodiumQueueManager())

# Server secrets. A generated auth secret invalidates tokens after a restart.
EDGE_API_KEY = os.getenv("EDGE_API_KEY", "")
AUTH_SECRET = (os.getenv("AUTH_SECRET") or secrets.token_urlsafe(32)).encode()
TOKEN_LIFETIME_SECONDS = 8 * 60 * 60
login_attempts: Dict[str, tuple[int, float]] = {}

event_subscribers: Dict[WebSocket, Optional[str]] = {}
event_teacher_ids: Dict[WebSocket, str] = {}
event_tokens: Dict[WebSocket, str] = {}
edge_sections: Dict[WebSocket, Optional[str]] = {}
edge_subscribers: Set[WebSocket] = set()
edge_selected_section_id: Optional[str] = None


def _teacher_id_from_token(token: Optional[str]) -> Optional[str]:
    payload = _decode_token(token)
    if not payload or payload.get("role") != "teacher":
        return None
    return payload.get("teacher_id", "teacher_master")


def _session_belongs_to_teacher(session: Optional[dict], teacher_id: Optional[str]) -> bool:
    if not session:
        return False
    return db.get_section_owner(session.get("section_id")) == teacher_id


def _event_visible_to_teacher(event_dict: dict, teacher_id: str) -> bool:
    event_type = event_dict.get('type')
    payload = event_dict.get('payload')
    if event_type == 'SECTIONS_UPDATED':
        event_dict['payload'] = db.get_sections(teacher_id=teacher_id)
        return True
    if isinstance(payload, list):
        filtered = [r for r in payload if db.get_section_owner(r.get('section_id')) == teacher_id]
        event_dict['payload'] = filtered
        for key in ('seats', 'students'):
            if key in event_dict:
                event_dict[key] = [r for r in event_dict[key] if db.get_section_owner(r.get('section_id')) == teacher_id]
        return bool(filtered) or db.get_section_owner(event_dict.get('section_id')) == teacher_id
    payload = payload or {}
    owner = event_dict.get('teacher_id') or payload.get('teacher_id')
    if owner:
        return owner == teacher_id
    section_id = event_dict.get('section_id') or payload.get('section_id')
    if section_id:
        return db.get_section_owner(section_id) == teacher_id
    session_id = payload.get('session_id') or (payload.get('id') if event_type.startswith('SESSION_') else None)
    return _session_belongs_to_teacher(db.get_session_details(session_id), teacher_id) if session_id else False


class ConnectionManager:
    @staticmethod
    async def broadcast_event(event_dict: dict):
        event_type = event_dict.get('type')
        if event_type in {'SEATS_UPDATED', 'STUDENTS_UPDATED', 'SECTIONS_UPDATED'}:
            _restore_live_queue()
            await ConnectionManager.broadcast_to_edge({'type': 'SECTIONS_UPDATED' if event_type == 'SECTIONS_UPDATED' else 'SEATS_UPDATED',
                                                       'section_id': event_dict.get('section_id')})
        for ws, guest_session_id in list(event_subscribers.items()):
            try:
                if guest_session_id is None:
                    if not _valid_teacher_token(event_tokens.get(ws)):
                        await ws.close(code=1008)
                        event_subscribers.pop(ws, None)
                        event_teacher_ids.pop(ws, None)
                        event_tokens.pop(ws, None)
                        continue
                    message = dict(event_dict)
                    if _event_visible_to_teacher(message, event_teacher_ids.get(ws)):
                        await ws.send_text(json.dumps(message))
                else:
                    active = _guest_session(event_tokens.get(ws))
                    if not active:
                        await ws.send_text(json.dumps({'type': 'SESSION_STOPPED', 'ledger': []}))
                        await ws.close(code=1008)
                        event_subscribers.pop(ws, None)
                        event_tokens.pop(ws, None)
                        continue
                    message = dict(event_dict)
                    if _event_visible_to_teacher(message, db.get_section_owner(active['section_id'])):
                        await ws.send_text(json.dumps({'type': event_type, 'ledger': _guest_safe(db.get_recitation_ledger(
                            session_id=active['id'], section_id=active['section_id'], active_session_required=True,
                            active_podium_map=session_queue(active['id']).get_podium_map()))}))
            except Exception:
                event_subscribers.pop(ws, None)
                event_teacher_ids.pop(ws, None)
                event_tokens.pop(ws, None)

    @staticmethod
    async def broadcast_to_edge(event_dict: dict):
        section_id = event_dict.get('section_id')
        for ws in list(edge_subscribers):
            selected = edge_sections.get(ws)
            if section_id and selected != section_id:
                continue
            try:
                await ws.send_text(json.dumps(event_dict))
            except Exception:
                edge_subscribers.discard(ws)
                edge_sections.pop(ws, None)


main_loop: Optional[asyncio.AbstractEventLoop] = None


def _restore_live_queue() -> None:
    queues.clear()
    for active in db.get_sessions():
        if active.get('ended_at'):
            continue
        queue = session_queue(active['id'])
        seats = {seat['id']: seat for seat in db.get_seats(section_id=active['section_id'])}
        for event in db.get_latest_gesture_events(active['id']):
            seat = seats.get(event['seat_id'])
            if (event['status'] == 'VALID' and event['reason_code'] == 'VALID_HAND_RAISE'
                    and seat and seat['is_present'] and seat.get('student_id')
                    and (seat['student_id'] == event['student_id'] if event.get('student_id') else seat['student_name'] == event['student_name'])):
                queue.register_raise(seat_id=event['seat_id'], student_name=event['student_name'],
                                     arm_angle=event['arm_angle'], timestamp_ms=event['timestamp_ms'])


def _prune_live_queue() -> None:
    for session_id, queue in list(queues.items()):
        session = db.get_session_details(session_id)
        if not session or session.get('ended_at'):
            queues.pop(session_id, None)
            continue
        seats = {s['id']: s for s in db.get_seats(section_id=session['section_id'])}
        for entry in queue.get_podium():
            seat = seats.get(entry.seat_id)
            if not seat or not seat['is_present'] or seat['student_name'] != entry.student_name:
                queue.release_raise(entry.seat_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    _restore_live_queue()
    print("[Web Dashboard] FastAPI initialized — Cloud-ready, zero CV dependencies.")
    yield
    print("[Web Dashboard] FastAPI shutdown complete.")


app = FastAPI(
    title="ClassTrack — Classroom Participation Monitoring System",
    description="Cloud-deployable web dashboard for real-time recitation and attendance monitoring.",
    lifespan=lifespan,
)

uploads_dir = Path(__file__).resolve().parent.parent / "static" / "uploads" / "students"
uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir.parent)), name="uploads")


# ---------------------------------------------------------------------------
# Authentication and response filtering
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    login_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9._/-]+$')
    password: str = Field(min_length=1, max_length=128)


class GuestCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class EdgeSectionSelectRequest(BaseModel):
    section_id: str = Field(min_length=1, max_length=128)


class TeacherCreateRequest(LoginRequest):
    name: str = Field(min_length=1, max_length=120)
    department: str = Field(default='', max_length=120)
    password: str = Field(min_length=12, max_length=128)

    @field_validator('name', 'department')
    @classmethod
    def valid_text(cls, value, info):
        value = ' '.join(value.split())
        if (info.field_name == 'name' and not value) or '<' in value or '>' in value:
            raise ValueError('Enter a valid plain-text name')
        return value

    @field_validator('password')
    @classmethod
    def valid_password(cls, value):
        if value is not None and (not value.strip() or len(set(value)) < 4):
            raise ValueError('Choose a password with at least 12 characters and varied characters')
        return value


class TeacherUpdateRequest(TeacherCreateRequest):
    password: Optional[str] = Field(default=None, min_length=12, max_length=128)
    is_active: bool = True


def _create_teacher_token(teacher_info: Optional[dict] = None) -> str:
    teacher_info = teacher_info or db.get_admin("teacher_master")
    if not teacher_info:
        raise ValueError("Create an administrator before signing in")
    payload = json.dumps({
        "role": "teacher",
        "teacher_id": teacher_info["id"],
        "teacher_name": teacher_info["name"],
        "is_admin": bool(teacher_info.get("is_admin")),
        "auth_version": teacher_info.get("auth_version", 1),
        "login_version": 2,
        "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
        "nonce": secrets.token_hex(12)
    }, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(AUTH_SECRET, encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _decode_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(AUTH_SECRET, encoded.encode(), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if isinstance(payload, dict) and hmac.compare_digest(expected, actual) and int(payload.get("exp", 0)) > time.time():
            return payload
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        pass
    return None


def _valid_teacher_token(token: Optional[str]) -> bool:
    payload = _decode_token(token)
    if not payload or payload.get("role") != "teacher":
        return False
    teacher_id = payload.get("teacher_id", "teacher_master")
    if payload.get("login_version") != 2:
        return False
    teacher = db.get_admin(teacher_id) if payload.get("is_admin") else db.get_teacher(teacher_id)
    return bool(teacher and teacher["is_active"] and teacher["auth_version"] == payload.get("auth_version", 1))


def _require_admin(token: Optional[str]) -> dict:
    if not _valid_teacher_token(token):
        raise HTTPException(status_code=401, detail="Teacher authentication required")
    payload = _decode_token(token) or {}
    if not payload.get("is_admin") or not db.get_admin(payload.get("teacher_id")):
        raise HTTPException(status_code=403, detail="Instructor account required")
    return payload


def _create_guest_token(session: dict) -> str:
    payload = json.dumps({"role": "guest", "session_id": session["id"],
                          "section_id": session["section_id"],
                          "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(AUTH_SECRET, encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _guest_session(token: Optional[str]) -> Optional[dict]:
    payload = _decode_token(token)
    if not payload or payload.get("role") != "guest":
        return None
    active = db.get_session_details(payload.get("session_id"))
    if active and not active.get("ended_at") and active.get("section_id") and payload.get("session_id") == active["id"] and payload.get("section_id") == active["section_id"]:
        return active
    return None


def _teacher_or_edge(request: Request) -> bool:
    return (_valid_teacher_token(request.headers.get("x-teacher-token"))
            or _validate_edge_key(request.headers.get("x-edge-key", "")))


def _guest_safe(value):
    """Remove private student fields from every public REST/WS payload."""
    hidden = {"student_id_number", "student_id", "photo_path", "face_embedding", "students", "guest_code",
              "latest_event_id", "face_similarity", "verification_status", "event_id"}
    if isinstance(value, dict):
        return {key: _guest_safe(item) for key, item in value.items() if key not in hidden}
    if isinstance(value, list):
        return [_guest_safe(item) for item in value]
    return value


@app.middleware("http")
async def enforce_teacher_access(request: Request, call_next):
    path = request.url.path
    method = request.method
    if path.startswith("/uploads/") and not _teacher_or_edge(request):
        return JSONResponse({"detail": "Teacher authentication required"}, status_code=401)
    if path.startswith('/uploads/') and not _validate_edge_key(request.headers.get('x-edge-key', '')):
        teacher_id = _teacher_id_from_token(request.headers.get('x-teacher-token'))
        with db._connect() as conn:
            photo = conn.execute("SELECT section_id FROM students WHERE photo_path = ?", (path,)).fetchone()
        if not photo or db.get_section_owner(photo['section_id']) != teacher_id:
            return JSONResponse({'detail': 'Photo not found'}, status_code=404)
    if path.startswith("/api/"):
        guest_get = {"/api/sections", "/api/sessions/active", "/api/recitation/ledger"}
        public_get = {"/api/edge/status", "/api/auth/guest-session"}
        public_post = {"/api/auth/login", "/api/auth/guest", "/api/events/ingest"}
        if method == "POST" and path == "/api/events/ingest":
            if not _validate_edge_key(request.headers.get("x-edge-key", "")):
                return JSONResponse({"detail": "Invalid or missing X-Edge-Key"}, status_code=401)
        if method == "GET" and path in public_get:
            pass
        elif path.startswith("/api/admin/teachers"):
            try:
                _require_admin(request.headers.get("x-teacher-token"))
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        elif method == "GET" and path in guest_get and (
            _valid_teacher_token(request.headers.get("x-teacher-token"))
            or _guest_session(request.headers.get("x-guest-token"))
            or _teacher_or_edge(request)
        ):
            pass
        elif method == "GET" and path == "/api/seats" and _teacher_or_edge(request):
            pass
        elif method == "GET" and path == "/api/auth/session":
            pass
        elif method == "POST" and path in public_post:
            pass
        elif method == "PUT" and path == "/api/seats" and _teacher_or_edge(request):
            pass
        elif not _valid_teacher_token(request.headers.get("x-teacher-token")):
            return JSONResponse({"detail": "Teacher authentication required"}, status_code=401)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.scheme == 'https':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.post('/api/auth/login')
async def login_teacher(body: LoginRequest, request: Request):
    client = f"login:{request.client.host if request.client else 'unknown'}:{body.login_id.upper()}"
    count, reset_at = login_attempts.get(client, (0, 0.0))
    if time.monotonic() >= reset_at:
        count, reset_at = 0, time.monotonic() + 300
    if count >= 5:
        raise HTTPException(status_code=429, detail='Too many attempts. Try again in five minutes.')
    login_attempts[client] = (count + 1, reset_at)
    teacher = await run_in_threadpool(db.authenticate_account, body.login_id, body.password)
    if not teacher:
        login_attempts[client] = (count + 1, reset_at)
        raise HTTPException(status_code=401, detail='Incorrect teacher ID or password')
    login_attempts.pop(client, None)
    return {'authenticated': True, 'token': _create_teacher_token(teacher),
            'teacher_id': teacher['id'], 'teacher_name': teacher['name'], 'is_admin': bool(teacher['is_admin'])}


@app.get("/api/auth/session")
async def get_auth_session(x_teacher_token: Optional[str] = Header(default=None)):
    if not _valid_teacher_token(x_teacher_token):
        raise HTTPException(status_code=401, detail="Teacher session expired")
    payload = _decode_token(x_teacher_token) or {}
    return {
        "authenticated": True,
        "role": "teacher",
        "teacher_id": payload.get("teacher_id", "teacher_master"),
        "teacher_name": payload.get("teacher_name", "Instructor"),
        "is_admin": payload.get("is_admin", False)
    }


@app.middleware("http")
async def restrict_teacher_sections(request: Request, call_next):
    """Prevent teachers from selecting another teacher's section by changing an ID."""
    token = request.headers.get("x-teacher-token")
    if _valid_teacher_token(token):
        payload = _decode_token(token) or {}
        teacher_id = payload.get("teacher_id", "teacher_master")
        if teacher_id != "teacher_master":
            parts = request.url.path.strip("/").split("/")
            section_id = None
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "sections":
                section_id = parts[2]
            if section_id and db.get_section_owner(section_id) != teacher_id:
                return JSONResponse({"detail": "Section not found"}, status_code=404)
            if request.url.path == "/api/seats" and request.method == "GET":
                requested_section = request.query_params.get("section_id")
                if requested_section and db.get_section_owner(requested_section) != teacher_id:
                    return JSONResponse({"detail": "Section not found"}, status_code=404)
    return await call_next(request)


@app.get("/api/admin/teachers")
async def list_teachers(x_teacher_token: Optional[str] = Header(default=None)):
    _require_admin(x_teacher_token)
    return db.list_teachers()


@app.post("/api/admin/teachers", status_code=status.HTTP_201_CREATED)
async def add_teacher(body: TeacherCreateRequest, x_teacher_token: Optional[str] = Header(default=None)):
    _require_admin(x_teacher_token)
    try:
        return db.create_teacher(body.name, body.department, body.login_id, body.password)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/api/admin/teachers/{teacher_id}")
async def deactivate_teacher(teacher_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    _require_admin(x_teacher_token)
    active = db.get_active_session(teacher_id=teacher_id)
    if teacher_id == "teacher_master" or not db.delete_teacher(teacher_id):
        raise HTTPException(status_code=404, detail="Teacher account not found")
    if active:
        queues.pop(active['id'], None)
        await ConnectionManager.broadcast_to_edge({'type': 'SESSION_STOPPED', 'section_id': active['section_id'], 'session_id': active['id']})
    await ConnectionManager.broadcast_event({'type': 'ACCOUNT_UPDATED', 'teacher_id': teacher_id})
    return {"deleted": True, "teacher_id": teacher_id}


@app.put('/api/admin/teachers/{teacher_id}')
async def edit_teacher(teacher_id: str, body: TeacherUpdateRequest, x_teacher_token: Optional[str] = Header(default=None)):
    _require_admin(x_teacher_token)
    try:
        result = db.update_teacher(teacher_id, **body.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail='Teacher account not found')
    if not body.is_active:
        ended = db.stop_session(teacher_id=teacher_id)
        if ended:
            queues.pop(ended['id'], None)
            await ConnectionManager.broadcast_to_edge({'type': 'SESSION_STOPPED', 'section_id': ended['section_id'], 'session_id': ended['id']})
    await ConnectionManager.broadcast_event({'type': 'ACCOUNT_UPDATED', 'teacher_id': teacher_id})
    return result


@app.exception_handler(ValueError)
async def invalid_value(request, error):
    return JSONResponse({'detail': str(error)}, status_code=409)


try:
    from psycopg2 import IntegrityError as PgIntegrityError
except ImportError:
    PgIntegrityError = sqlite3.IntegrityError


@app.exception_handler(PgIntegrityError)
@app.exception_handler(sqlite3.IntegrityError)
async def duplicate_record(request, error):
    return JSONResponse({'detail': 'This record already exists or references an unavailable record'}, status_code=409)


@app.post("/api/auth/guest")
async def verify_guest_code(body: GuestCodeRequest, request: Request):
    client = f"guest:{request.client.host if request.client else 'unknown'}"
    count, reset_at = login_attempts.get(client, (0, 0.0))
    if time.monotonic() >= reset_at:
        count, reset_at = 0, time.monotonic() + 300
    if count >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in five minutes.")
    active = db.get_session_by_guest_code(body.code.strip().upper())
    if not active:
        login_attempts[client] = (count + 1, reset_at)
        raise HTTPException(status_code=401, detail="Invalid viewing code or no live session")
    login_attempts.pop(client, None)
    return {"authenticated": True, "token": _create_guest_token(active), "section_id": active["section_id"]}


@app.get("/api/auth/guest-session")
async def get_guest_auth_session(x_guest_token: Optional[str] = Header(default=None)):
    active = _guest_session(x_guest_token)
    if not active:
        raise HTTPException(status_code=401, detail="Viewing code expired")
    return {"authenticated": True, "section_id": active["section_id"]}


def _validate_edge_key(provided_key: str) -> bool:
    if not EDGE_API_KEY:
        return False
    return secrets.compare_digest(provided_key or "", EDGE_API_KEY)


def _require_edge_auth(x_edge_key: Optional[str] = None) -> None:
    if not _validate_edge_key(x_edge_key or ""):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Edge-Key")


# ---------------------------------------------------------------------------
# REST API ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/api/sections", response_model=List[SectionResponse])
async def get_sections(x_teacher_token: Optional[str] = Header(default=None),
                       x_guest_token: Optional[str] = Header(default=None),
                       x_edge_key: Optional[str] = Header(default=None)):
    teacher_payload = _decode_token(x_teacher_token) if _valid_teacher_token(x_teacher_token) else None
    if teacher_payload and teacher_payload.get("role") == "teacher":
        return db.get_sections(teacher_id=teacher_payload.get("teacher_id"))
    if x_edge_key and _validate_edge_key(x_edge_key):
        return db.get_sections()
    active = _guest_session(x_guest_token)
    if active and active.get("section_id"):
        return [section for section in db.get_sections() if section["id"] == active["section_id"]]
    return []


@app.post("/api/edge/section")
async def select_camera_section(body: EdgeSectionSelectRequest,
                                x_teacher_token: Optional[str] = Header(default=None)):
    """Tell connected camera nodes which teacher-owned roster to track before class starts."""
    _enforce_owned_section(body.section_id, x_teacher_token)
    active = db.get_active_session(teacher_id=_teacher_id_from_token(x_teacher_token))
    if active and active.get("section_id") != body.section_id:
        raise HTTPException(status_code=409, detail="End the active session before changing the camera section")
    global edge_selected_section_id
    edge_selected_section_id = body.section_id
    # Unbound nodes may adopt a section only when the deployment is unambiguous.
    if len(db.get_sections()) == 1:
        for ws in edge_subscribers:
            if not edge_sections.get(ws):
                edge_sections[ws] = body.section_id
    await ConnectionManager.broadcast_to_edge({"type": "SECTION_SELECTED", "section_id": body.section_id})
    return {"status": "ok", "section_id": body.section_id}


@app.post("/api/sections", response_model=SectionResponse, status_code=status.HTTP_201_CREATED)
async def create_section(body: SectionCreateRequest, x_teacher_token: Optional[str] = Header(default=None)):
    teacher_payload = _decode_token(x_teacher_token) if _valid_teacher_token(x_teacher_token) else None
    teacher_id = teacher_payload.get("teacher_id") if teacher_payload and teacher_payload.get("role") == "teacher" else None
    sec = db.create_section(name=body.name, subject=body.subject, room=body.room, teacher_id=teacher_id)
    await ConnectionManager.broadcast_event({"type": "SECTIONS_UPDATED", "payload": db.get_sections()})
    return sec


@app.delete("/api/sections/{section_id}")
async def delete_section(section_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    if db.get_section_owner(section_id) != teacher_id:
        raise HTTPException(status_code=404, detail="Section not found")
    db.delete_section(section_id)
    await ConnectionManager.broadcast_event({"type": "SECTIONS_UPDATED", "payload": db.get_sections()})
    return {"message": "Section deleted successfully"}


def _teacher_owned_section_ids(teacher_id: Optional[str]) -> set:
    return {section["id"] for section in db.get_sections(teacher_id=teacher_id)} if teacher_id else set()


def _enforce_owned_section(section_id: str, token: Optional[str]) -> None:
    teacher_id = _teacher_id_from_token(token)
    if not teacher_id or db.get_section_owner(section_id) != teacher_id:
        raise HTTPException(status_code=404, detail="Section not found")


@app.get("/api/sections/{section_id}/students")
async def get_section_students(section_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    return db.get_students(section_id)


@app.post("/api/sections/{section_id}/students/enroll")
async def enroll_student(section_id: str, body: StudentEnrollRequest, x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    photo_rel_path = ""
    face_emb_str = ""

    if body.photo_base64:
        try:
            raw_b64 = body.photo_base64
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]
            img_bytes = base64.b64decode(raw_b64, validate=True)

            if len(img_bytes) > 5_000_000 or not (img_bytes.startswith(b'\xff\xd8\xff') or img_bytes.startswith(b'\x89PNG\r\n\x1a\n')):
                raise ValueError('Upload a JPEG or PNG image under 5 MB')
            stud_filename = f"stud_{uuid.uuid4().hex[:8]}.jpg"
            save_path = uploads_dir / stud_filename

            # Save photo directly — no face engine processing in cloud mode
            with open(save_path, "wb") as f:
                f.write(img_bytes)
            photo_rel_path = f"/uploads/students/{stud_filename}"
        except Exception as e:
            raise HTTPException(status_code=422, detail="Upload a valid JPEG or PNG image under 5 MB") from e

    try:
        stud = db.enroll_student(
            section_id=section_id,
            name=body.name,
            student_id_number=body.student_id_number,
            photo_path=photo_rel_path,
            face_embedding=face_emb_str,
            assign_to_seat_id=body.assign_to_seat_id,
            auto_create_desk=bool(body.auto_create_desk),
        )
    except Exception:
        if photo_rel_path:
            (uploads_dir / Path(photo_rel_path).name).unlink(missing_ok=True)
        raise

    seats = db.get_seats(section_id=section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "STUDENTS_UPDATED",
        "payload": students,
        "section_id": section_id,
        "seats": seats,
    })
    return stud


@app.delete("/api/students/{student_id}")
async def delete_student(student_id: str, section_id: Optional[str] = None,
                         x_teacher_token: Optional[str] = Header(default=None), x_edge_key: Optional[str] = Header(default=None)):
    if not section_id:
        raise HTTPException(status_code=400, detail="section_id is required")
    _enforce_owned_section(section_id, x_teacher_token)
    if not any(student["id"] == student_id for student in db.get_students(section_id)):
        raise HTTPException(status_code=404, detail="Student not found")
    db.delete_student(student_id)
    seats = db.get_seats(section_id=section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "STUDENTS_UPDATED",
        "payload": students,
        "section_id": section_id,
        "seats": seats,
    })
    return {"message": "Student deleted"}


@app.post("/api/sections/{section_id}/seats/auto-generate-enrolled")
async def auto_generate_desks_enrolled(section_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    updated_seats = db.auto_generate_desks_from_enrolled_students(section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": updated_seats,
        "students": students,
        "section_id": section_id,
    })
    return updated_seats


@app.post("/api/sections/{section_id}/seats/grid")
async def configure_seating_grid(section_id: str, body: GridConfigureRequest, x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    updated_seats = db.configure_seating_grid(section_id, body.rows, body.cols)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated_seats, "section_id": section_id})
    return updated_seats


@app.post("/api/sections/{section_id}/seats/auto-fill-alphabetical")
async def auto_fill_seats_alphabetical(section_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    updated_seats = db.auto_fill_seats_alphabetical(section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": updated_seats,
        "students": students,
        "section_id": section_id,
    })
    return updated_seats


@app.post("/api/sections/{section_id}/seats/clear-assignments")
async def clear_seat_assignments(section_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    updated_seats = db.clear_seat_assignments(section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": updated_seats,
        "students": students,
        "section_id": section_id,
    })
    return updated_seats


@app.post("/api/seats/{seat_id}/assign")
async def assign_student_seat(seat_id: str, body: SeatAssignRequest, section_id: Optional[str] = None,
                              x_teacher_token: Optional[str] = Header(default=None)):
    if not section_id:
        raise HTTPException(status_code=400, detail="section_id is required")
    _enforce_owned_section(section_id, x_teacher_token)
    if not any(seat["id"] == seat_id for seat in db.get_seats(section_id=section_id)):
        raise HTTPException(status_code=404, detail="Seat not found")
    res = db.assign_student_to_seat(seat_id, body.student_id)
    actual_sec_id = section_id or res.get("section_id")
    seats = db.get_seats(section_id=actual_sec_id)
    students = db.get_students(section_id=actual_sec_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": seats,
        "students": students,
        "section_id": actual_sec_id,
    })
    return res


@app.post("/api/seats/swap")
async def swap_seats(body: SeatSwapRequest, section_id: Optional[str] = None,
                     x_teacher_token: Optional[str] = Header(default=None)):
    if not section_id:
        raise HTTPException(status_code=400, detail="section_id is required")
    _enforce_owned_section(section_id, x_teacher_token)
    seat_ids = {seat["id"] for seat in db.get_seats(section_id=section_id)}
    if body.seat_id_1 not in seat_ids or body.seat_id_2 not in seat_ids:
        raise HTTPException(status_code=404, detail="Seat not found")
    ok = db.swap_seats(body.seat_id_1, body.seat_id_2)
    if not ok:
        raise HTTPException(status_code=400, detail="Could not swap seats")
    seats = db.get_seats(section_id=section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": seats,
        "students": students,
        "section_id": section_id,
    })
    return {"success": True, "seats": seats}


@app.get("/api/analytics/heatmap")
async def get_heatmap(section_id: Optional[str] = None,
                      x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    if not section_id or db.get_section_owner(section_id) != teacher_id:
        raise HTTPException(status_code=404, detail="Section not found")
    return db.get_participation_heatmap(section_id=section_id)


@app.get("/api/analytics/grades")
async def get_participation_grades(section_id: str, target: int = Query(default=5, ge=1, le=10000), weight: float = Query(default=100.0, gt=0, le=100),
                                   x_teacher_token: Optional[str] = Header(default=None)):
    if db.get_section_owner(section_id) != _teacher_id_from_token(x_teacher_token):
        raise HTTPException(status_code=404, detail="Section not found")
    return db.calculate_participation_grades(section_id=section_id, target_raises=target, weight_percent=weight)


@app.get("/api/exports/grades.csv")
async def export_grades_csv(section_id: str, target: int = Query(default=5, ge=1, le=10000), weight: float = Query(default=100.0, gt=0, le=100),
                            x_teacher_token: Optional[str] = Header(default=None)):
    if db.get_section_owner(section_id) != _teacher_id_from_token(x_teacher_token):
        raise HTTPException(status_code=404, detail="Section not found")
    grades = db.calculate_participation_grades(section_id=section_id, target_raises=target, weight_percent=weight)
    import io
    out = io.StringIO()
    w = SafeCsvWriter(out)
    w.writerow(["Class Recitation & Participation Grade Sheet"])
    w.writerow(["Section ID:", section_id, "Target Raises:", target, "Weight:", f"{weight}%"])
    w.writerow([])
    w.writerow([
        "Student ID",
        "Student Name",
        "Assigned Desk",
        "Total Raises",
        "Points Awarded",
        "Seat Mismatches Flagged",
        "Participation %",
        "Recitation Grade",
    ])
    for g in grades:
        w.writerow([
            g["student_id_number"],
            g["student_name"],
            g["assigned_seat_label"],
            g["total_raises"],
            g["total_points"],
            g["seat_mismatches"],
            f"{g['participation_percentage']}%",
            g["recitation_grade"],
        ])
    return PlainTextResponse(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="grades-{section_id}.csv"'},
    )


@app.post("/api/sections/{section_id}/students")
async def register_student(section_id: str, body: StudentRegisterRequest,
                           x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    if body.seat_id and not any(seat["id"] == body.seat_id for seat in db.get_seats(section_id=section_id)):
        raise HTTPException(status_code=404, detail="Seat not found")
    seat = db.register_student(
        section_id=section_id,
        student_name=body.student_name,
        student_id_number=body.student_id_number,
        seat_id=body.seat_id,
        label=body.label,
    )
    seats = db.get_seats(section_id=section_id)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": seats, "section_id": section_id})
    return seat


@app.get("/api/recitation/ledger")
async def get_recitation_ledger(session_id: Optional[str] = None, section_id: Optional[str] = None,
                                x_teacher_token: Optional[str] = Header(default=None),
                                x_guest_token: Optional[str] = Header(default=None)):
    # If session_id not explicitly provided, default to active session
    teacher = _valid_teacher_token(x_teacher_token)
    teacher_id = _teacher_id_from_token(x_teacher_token) if teacher else None
    active = db.get_active_session(teacher_id=teacher_id) if teacher else _guest_session(x_guest_token)
    if not teacher:
        if not active or (session_id and session_id != active["id"]) or (section_id and section_id != active["section_id"]):
            raise HTTPException(status_code=403, detail="This viewing code belongs to another class session")
        section_id = active["section_id"]
    elif session_id:
        session = db.get_session_details(session_id, teacher_id=teacher_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if section_id and section_id != session.get("section_id"):
            raise HTTPException(status_code=400, detail="Session belongs to another class section")
        section_id = session.get("section_id")
    elif active:
        section_id = section_id or active.get("section_id")
    if teacher and section_id and db.get_section_owner(section_id) != teacher_id:
        raise HTTPException(status_code=404, detail="Section not found")
    if not section_id:
        return []
    target_session_id = session_id if session_id is not None else (active["id"] if active and active["section_id"] == section_id else None)
    _prune_live_queue()
    current_session = active
    active_podium_map = (session_queue(target_session_id).get_podium_map()
                         if current_session and target_session_id == current_session["id"] else {})
    ledger = db.get_recitation_ledger(
        session_id=target_session_id,
        section_id=section_id,
        active_session_required=True,
        active_podium_map=active_podium_map,
    )
    return ledger if teacher else _guest_safe(ledger)


@app.get("/api/sessions")
async def get_sessions(section_id: Optional[str] = None, date: Optional[str] = None,
                       x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    return db.get_sessions(section_id=section_id, date_filter=date, teacher_id=teacher_id)


@app.get("/api/sessions/active", response_model=Optional[SessionResponse])
async def get_active_session(x_teacher_token: Optional[str] = Header(default=None),
                             x_guest_token: Optional[str] = Header(default=None), section_id: Optional[str] = None):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    if teacher_id:
        return db.get_active_session(teacher_id=teacher_id)
    if x_guest_token:
        return _guest_session(x_guest_token)
    if section_id:
        return db.get_active_session(section_id=section_id)
    active = [s for s in db.get_sessions() if not s.get("ended_at")]
    return active[0] if len(active) == 1 else None


@app.get("/api/sessions/active/guest-access")
async def get_active_guest_access(x_teacher_token: Optional[str] = Header(default=None)):
    active = db.get_active_session(teacher_id=_teacher_id_from_token(x_teacher_token))
    if not active or not active.get("section_id"):
        raise HTTPException(status_code=404, detail="Start a section class session to create a viewing code")
    return {"code": active["guest_code"], "section_id": active["section_id"]}


@app.get("/api/sessions/{session_id}")
async def get_session_details(session_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    details = db.get_session_details(session_id, teacher_id=_teacher_id_from_token(x_teacher_token))
    if not details:
        raise HTTPException(status_code=404, detail="Session not found")
    return details


@app.delete("/api/sessions")
async def clear_all_sessions(section_id: Optional[str] = None,
                             x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    if section_id and db.get_section_owner(section_id) != teacher_id:
        raise HTTPException(status_code=404, detail="Section not found")
    deleted_count = db.clear_all_sessions(section_id=section_id, teacher_id=teacher_id)
    await ConnectionManager.broadcast_event({
        "type": "SESSIONS_CLEARED",
        "payload": {"section_id": section_id, "deleted_count": deleted_count, "teacher_id": teacher_id},
    })
    return {"status": "success", "deleted_count": deleted_count}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    session = db.get_session_details(session_id, teacher_id=teacher_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete_session(session_id, teacher_id=teacher_id)
    await ConnectionManager.broadcast_event({"type": "SESSION_DELETED", "payload": {"session_id": session_id, "teacher_id": teacher_id}})
    return {"status": "success", "deleted": session_id}


@app.post("/api/sessions/start", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def start_session(body: SessionStartRequest, x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    owned_section_ids = {section["id"] for section in db.get_sections(teacher_id=teacher_id)}
    if not body.section_id or body.section_id not in owned_section_ids:
        raise HTTPException(status_code=400, detail="Select a valid class section before starting")
    current_session = db.get_active_session(teacher_id=teacher_id)
    if current_session:
        raise HTTPException(status_code=409, detail="End your current class session before starting another")
    sess = db.start_session(title=body.title, section_id=body.section_id)
    # Clear podium queue for fresh session
    session_queue(sess["id"]).clear()
    ledger = db.get_recitation_ledger(session_id=sess["id"], section_id=body.section_id, active_session_required=True)
    await ConnectionManager.broadcast_event({"type": "SESSION_STARTED", "payload": sess, "ledger": ledger})
    # Notify connected Camera Nodes
    await ConnectionManager.broadcast_to_edge({
        "type": "SESSION_STARTED",
        "session_id": sess["id"],
        "section_id": body.section_id,
    })
    return sess


@app.post("/api/sessions/stop", response_model=Optional[SessionResponse])
async def stop_session(x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    sess = db.stop_session(teacher_id=teacher_id)
    if sess:
        queues.pop(sess["id"], None)
    empty_ledger = []
    if sess:
        await ConnectionManager.broadcast_event({"type": "SESSION_STOPPED", "payload": sess, "ledger": empty_ledger})
        await ConnectionManager.broadcast_to_edge({"type": "SESSION_STOPPED", "section_id": sess["section_id"], "session_id": sess["id"]})
    return sess


@app.get("/api/seats", response_model=List[SeatSchema])
async def get_seats(section_id: Optional[str] = None,
                    x_teacher_token: Optional[str] = Header(default=None),
                    x_edge_key: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token) if _valid_teacher_token(x_teacher_token) else None
    # Prefer teacher ownership scope when both credential types are supplied.
    if teacher_id:
        return db.get_seats(section_id=section_id, teacher_id=teacher_id)
    if _validate_edge_key(x_edge_key or ""):
        return db.get_seats(section_id=section_id) if section_id else []
    return []


@app.put("/api/seats", response_model=List[SeatSchema])
async def update_seats(body: SeatBulkUpdateRequest, section_id: Optional[str] = None,
                       x_teacher_token: Optional[str] = Header(default=None), x_edge_key: Optional[str] = Header(default=None)):
    if not section_id:
        raise HTTPException(status_code=400, detail="section_id is required")
    if not _validate_edge_key(x_edge_key or ''):
        _enforce_owned_section(section_id, x_teacher_token)
    existing = {seat['id']: seat for seat in db.get_seats(section_id=section_id)}
    roster = {student['id']: student for student in db.get_students(section_id)}
    all_ids = {seat['id'] for seat in db.get_seats()}
    ids = [seat.id for seat in body.seats]
    assigned = [seat.student_id for seat in body.seats if seat.student_id]
    if len(ids) != len(set(ids)) or len(assigned) != len(set(assigned)):
        raise HTTPException(status_code=422, detail='Each desk and student may appear only once')
    for seat in body.seats:
        if (seat.section_id and seat.section_id != section_id) or (seat.id in all_ids and seat.id not in existing):
            raise HTTPException(status_code=404, detail='Desk not found in this section')
        if seat.student_id and seat.student_id not in roster:
            raise HTTPException(status_code=404, detail='Student not found in this section')
        if _validate_edge_key(x_edge_key or ''):
            previous = existing.get(seat.id)
            if not previous or any(getattr(seat, key) != previous.get(key) for key in ('student_id', 'student_name', 'student_id_number', 'is_present')):
                raise HTTPException(status_code=403, detail='Camera calibration can only update existing desk geometry')
    seats_data = [s.model_dump() for s in body.seats]
    updated = db.bulk_update_seats(seats_data, section_id=section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": updated,
        "students": students,
        "section_id": section_id,
    })
    return updated


@app.post("/api/sections/{section_id}/attendance/bulk", response_model=List[SeatSchema])
async def set_section_attendance(section_id: str, is_present: bool = True, x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    updated = db.set_section_attendance(section_id, is_present=is_present)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated})
    return updated


@app.post("/api/seats/{seat_id}/toggle-attendance", response_model=SeatAttendanceToggleResponse)
async def toggle_seat_attendance(seat_id: str, section_id: Optional[str] = None,
                                 x_teacher_token: Optional[str] = Header(default=None)):
    if not section_id:
        raise HTTPException(status_code=400, detail="section_id is required")
    _enforce_owned_section(section_id, x_teacher_token)
    matching_seat = next((seat for seat in db.get_seats(section_id=section_id) if seat["id"] == seat_id), None)
    if not matching_seat:
        raise HTTPException(status_code=404, detail="Seat not found")
    try:
        res = db.toggle_attendance(seat_id)
        await ConnectionManager.broadcast_event({"type": "ATTENDANCE_TOGGLED", "payload": res})
        return res
    except KeyError:
        raise HTTPException(status_code=404, detail="Seat not found")


@app.post("/api/seats/preset/{preset_name}", response_model=List[SeatSchema])
async def load_seat_preset(preset_name: str, section_id: Optional[str] = None,
                           x_teacher_token: Optional[str] = Header(default=None)):
    if not section_id:
        raise HTTPException(status_code=400, detail="section_id is required")
    _enforce_owned_section(section_id, x_teacher_token)
    try:
        updated = db.load_preset_seats(preset_name, section_id=section_id)
        await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated})
        return updated
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/seats/{seat_id}")
async def delete_seat(seat_id: str, section_id: Optional[str] = None,
                      x_teacher_token: Optional[str] = Header(default=None)):
    if not section_id:
        raise HTTPException(status_code=400, detail="section_id is required")
    _enforce_owned_section(section_id, x_teacher_token)
    db.delete_seat(seat_id)
    updated = db.get_seats(section_id=section_id)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated})
    return {"message": "Seat deleted", "seats": updated}


@app.get("/api/events")
async def get_events(limit: int = Query(default=50, ge=1, le=500), x_teacher_token: Optional[str] = Header(default=None)):
    active = db.get_active_session(teacher_id=_teacher_id_from_token(x_teacher_token))
    return db.get_events(session_id=active["id"], limit=limit) if active else []


@app.post("/api/seats/{seat_id}/award")
async def award_manual_point(seat_id: str, section_id: str,
                             x_teacher_token: Optional[str] = Header(default=None)):
    _enforce_owned_section(section_id, x_teacher_token)
    active = db.get_active_session(teacher_id=_teacher_id_from_token(x_teacher_token))
    if not active or active["section_id"] != section_id:
        raise HTTPException(status_code=409, detail="Start this class session before awarding a point")
    seat = next((item for item in db.get_seats(section_id=section_id) if item["id"] == seat_id), None)
    if not seat or not seat.get("student_id"):
        raise HTTPException(status_code=404, detail="Assigned student desk not found")
    event_id = f"manual_{uuid.uuid4().hex}"
    db.insert_event({
        "event_id": event_id, "session_id": active["id"], "seat_id": seat_id,
        "student_id": seat["student_id"],
        "student_name": seat["student_name"], "status": "MANUAL",
        "reason_code": "MANUAL_POINT", "arm_angle_deg": 0,
        "duration_sec": 0, "queue_pos": None,
        "timestamp_ms": time.time_ns() // 1_000_000, "earned_point": 1,
    })
    await ConnectionManager.broadcast_event({
        "type": "POINT_AWARDED",
        "payload": {"id": event_id, "session_id": active["id"],
                    "teacher_id": _teacher_id_from_token(x_teacher_token)},
    })
    return {"id": event_id, "status": "AWARDED", "earned_point": 1}


@app.post("/api/events/{event_id}/award", response_model=EventActionResponse)
async def award_event_point(event_id: str, force: bool = False,
                            x_teacher_token: Optional[str] = Header(default=None)):
    try:
        teacher_id = _teacher_id_from_token(x_teacher_token)
        event_context = db.get_event_context(event_id)
        if not event_context or event_context["teacher_id"] != teacher_id:
            raise HTTPException(status_code=404, detail="Event not found")
        res = db.award_point(event_id, force_override=force)
        active = db.get_active_session(teacher_id=teacher_id)
        active_id = active["id"] if active else None
        active_podium_map = session_queue(active_id).get_podium_map()
        ledger = db.get_recitation_ledger(session_id=active_id, section_id=event_context["section_id"], active_session_required=True, active_podium_map=active_podium_map)
        # Notify clients with instant ledger push
        await ConnectionManager.broadcast_event({
            "type": "POINT_AWARDED",
            "payload": {**res, "teacher_id": teacher_id},
            "ledger": ledger,
            "seats": db.get_seats(section_id=event_context["section_id"]),
        })
        return res
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except KeyError:
        raise HTTPException(status_code=404, detail="Event not found")


@app.post("/api/events/{event_id}/dismiss", response_model=EventActionResponse)
async def dismiss_event(event_id: str, x_teacher_token: Optional[str] = Header(default=None)):
    try:
        teacher_id = _teacher_id_from_token(x_teacher_token)
        event_context = db.get_event_context(event_id)
        if not event_context or event_context["teacher_id"] != teacher_id:
            raise HTTPException(status_code=404, detail="Event not found")
        res = db.dismiss_event(event_id)
        active = db.get_active_session(teacher_id=teacher_id)
        active_id = active["id"] if active else None
        active_podium_map = session_queue(active_id).get_podium_map()
        ledger = db.get_recitation_ledger(session_id=active_id, section_id=event_context["section_id"], active_session_required=True, active_podium_map=active_podium_map)
        await ConnectionManager.broadcast_event({
            "type": "EVENT_DISMISSED",
            "payload": {**res, "teacher_id": teacher_id},
            "ledger": ledger,
        })
        return res
    except KeyError:
        raise HTTPException(status_code=404, detail="Event not found")


@app.get("/api/exports/session-report.csv")
async def export_csv(session_id: Optional[str] = None, section_id: Optional[str] = None,
                     x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    target_session_id = session_id
    if not target_session_id:
        active = db.get_active_session(teacher_id=teacher_id)
        target_session_id = active["id"] if active else None
    if target_session_id and not db.get_session_details(target_session_id, teacher_id=teacher_id):
        raise HTTPException(status_code=404, detail="Session not found")
    if target_session_id and not section_id:
        session = db.get_session_details(target_session_id, teacher_id=teacher_id)
        section_id = session.get("section_id")
    if section_id and db.get_section_owner(section_id) != teacher_id:
        raise HTTPException(status_code=404, detail="Section not found")
    if not target_session_id and not section_id:
        raise HTTPException(status_code=404, detail="No session selected")
    csv_text = db.export_session_report_csv(session_id=target_session_id, section_id=section_id)
    filename = f"session-report-{target_session_id or 'all'}.csv"
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/exports/class-report.csv")
async def export_class_csv(section_id: Optional[str] = None,
                           x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    if not section_id or db.get_section_owner(section_id) != teacher_id:
        raise HTTPException(status_code=404, detail="Section not found")
    csv_text = db.export_class_report_csv(section_id=section_id)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="class-report.csv"'},
    )


# ---------------------------------------------------------------------------
# EDGE CAMERA NODE INGEST ENDPOINT
# ---------------------------------------------------------------------------

@app.post("/api/events/ingest")
async def ingest_edge_event(body: EdgeEventIngest, x_edge_key: Optional[str] = Header(default=None)):
    """
    Receive gesture events from remote Camera Nodes.
    This is the primary integration point between camera-node and web-dashboard.
    Requires X-Edge-Key header when EDGE_API_KEY is configured.
    """
    _require_edge_auth(x_edge_key)

    active = db.get_active_session(section_id=body.section_id)
    if not active:
        raise HTTPException(status_code=409, detail="No active session — start a session first")
    if body.section_id != active["section_id"]:
        raise HTTPException(status_code=409, detail="Camera event belongs to another class section")

    session_id = active["id"]
    queue = session_queue(session_id)
    if body.session_id and body.session_id != session_id:
        raise HTTPException(status_code=409, detail="Camera event belongs to an ended class session")
    seat = next((seat for seat in db.get_seats(section_id=body.section_id) if seat["id"] == body.seat_id), None)
    if not seat or not seat["is_present"] or not seat.get("student_id"):
        raise HTTPException(status_code=409, detail="Desk is not assigned to a present student")
    if body.student_id and body.student_id != seat["student_id"]:
        raise HTTPException(status_code=409, detail="Desk assignment changed; sync camera seats")
    if body.student_name != seat["student_name"]:
        raise HTTPException(status_code=409, detail="Student assignment changed; sync camera seats")

    event_id = body.event_id or f"evt_{body.timestamp_ms}_{uuid.uuid4().hex[:6]}"

    # Build event dict for DB persistence
    fallback_queue_pos = body.queue_pos if body.queue_pos is not None else body.podium_rank
    event_dict = {
        "event_id": event_id,
        "session_id": session_id,
        "seat_id": body.seat_id,
        "student_id": seat["student_id"],
        "student_name": seat["student_name"],
        "status": body.status,
        "reason_code": body.reason_code,
        "arm_angle_deg": body.arm_angle_deg,
        "duration_sec": body.duration_sec,
        "queue_pos": fallback_queue_pos,
        "delta_ms": body.delta_ms,
        "timestamp_ms": body.timestamp_ms,
        "earned_point": body.earned_point,
    }

    try:
        inserted = db.insert_event(event_dict)
    except Exception as e:
        print(f"[Edge Ingest] Error persisting event to DB: {e}")
        raise HTTPException(status_code=503, detail="Could not save camera event") from e
    if not inserted:
        return {"status": "duplicate", "event_id": event_id}

    # Change the live queue only after the event has been saved.
    if body.status == "VALID" and body.reason_code == "VALID_HAND_RAISE":
        existing = queue.get_entry(body.seat_id)
        if existing and existing.student_name != seat["student_name"]:
            queue.release_raise(body.seat_id)
        podium_entry = queue.register_raise(
            seat_id=body.seat_id,
            student_name=seat["student_name"],
            arm_angle=body.arm_angle_deg,
            timestamp_ms=body.timestamp_ms,
        )
        event_dict["queue_pos"] = podium_entry.queue_position
        event_dict["delta_ms"] = podium_entry.delta_ms
    elif body.reason_code == "HAND_LOWERED" or body.status == "INVALID":
        queue.release_raise(body.seat_id)

    # Broadcast to all browser WebSocket clients with optimized ledger
    active_podium_map = queue.get_podium_map()
    ledger = db.get_recitation_ledger(
        session_id=session_id,
        active_session_required=True,
        active_podium_map=active_podium_map,
    )

    await ConnectionManager.broadcast_event({
        "type": "GESTURE_EVENT",
        "payload": event_dict,
        "ledger": ledger,
    })

    return {"status": "ok", "event_id": event_id}


# In-memory Camera Node Settings
teacher_camera_settings = {}
camera_settings = {
    "show_skeleton": False,
    "model_name": "yolo11s-pose.pt",
    "confidence": 0.50,
}

AVAILABLE_MODELS = [
    {"id": "yolo11s-pose.pt", "name": "YOLO11 Small (Pose)", "desc": "Recommended — high precision hand-raise detection", "recommended": True},
    {"id": "yolov8n-pose.pt", "name": "YOLOv8 Nano (Pose)", "desc": "Ultra-fast & lightweight (ideal for low-spec PCs)", "recommended": False},
    {"id": "yolov8s-pose.pt", "name": "YOLOv8 Small (Pose)", "desc": "Balanced speed and accuracy", "recommended": False},
    {"id": "yolov8m-pose.pt", "name": "YOLOv8 Medium (Pose)", "desc": "Maximum accuracy (requires dedicated GPU)", "recommended": False},
]


@app.get("/api/camera/settings")
async def get_camera_settings(x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    return {'settings': teacher_camera_settings.get(teacher_id, camera_settings), 'available_models': AVAILABLE_MODELS,
            'connected_nodes': sum(db.get_section_owner(sec) == teacher_id for sec in edge_sections.values())}


class CameraSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, protected_namespaces=())
    show_skeleton: Optional[bool] = None
    model_name: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.1, le=0.95)

    @field_validator('model_name')
    @classmethod
    def valid_model(cls, value):
        if value not in {m['id'] for m in AVAILABLE_MODELS}:
            raise ValueError('Select an available camera model')
        return value


@app.post('/api/camera/settings')
async def update_camera_settings(payload: CameraSettingsRequest, x_teacher_token: Optional[str] = Header(default=None)):
    teacher_id = _teacher_id_from_token(x_teacher_token)
    settings = {**teacher_camera_settings.get(teacher_id, camera_settings), **payload.model_dump(exclude_none=True)}
    teacher_camera_settings[teacher_id] = settings
    for section in db.get_sections(teacher_id=teacher_id):
        await ConnectionManager.broadcast_to_edge({'type': 'CAMERA_SETTINGS_UPDATED', 'section_id': section['id'], 'settings': settings})
    await ConnectionManager.broadcast_event({'type': 'CAMERA_SETTINGS_CHANGED', 'teacher_id': teacher_id, 'settings': settings})
    return {'status': 'ok', 'settings': settings}


@app.get("/api/edge/status")
async def get_edge_status(x_teacher_token: Optional[str] = Header(default=None)):
    """Show the signed-in teacher only the nodes assigned to their sections."""
    _prune_live_queue()
    teacher_id = _teacher_id_from_token(x_teacher_token) if _valid_teacher_token(x_teacher_token) else None
    active = db.get_active_session(teacher_id=teacher_id) if teacher_id else None
    return {
        "connected_nodes": sum(db.get_section_owner(sec) == teacher_id for sec in edge_sections.values()) if teacher_id else len(edge_subscribers),
        "podium_active": len(session_queue(active['id']).get_podium()) if active else (0 if teacher_id else sum(len(q.get_podium()) for q in queues.values())),
        "camera_settings": camera_settings,
    }


# ---------------------------------------------------------------------------
# WEBSOCKET CHANNELS
# ---------------------------------------------------------------------------

@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await websocket.accept()
    try:
        auth_message = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=5))
        teacher = _valid_teacher_token(auth_message.get("teacher_token"))
        guest_session = None if teacher else _guest_session(auth_message.get("guest_token"))
        if not teacher and not guest_session:
            await websocket.close(code=1008)
            return
        event_tokens[websocket] = auth_message.get("teacher_token") if teacher else auth_message.get("guest_token")
        event_subscribers[websocket] = None if teacher else guest_session["id"]
        teacher_id = _teacher_id_from_token(auth_message.get("teacher_token")) if teacher else None
        if teacher_id:
            event_teacher_ids[websocket] = teacher_id
        # Send initial snapshot of state
        active_session = db.get_active_session(teacher_id=teacher_id) if teacher else guest_session
        sections = db.get_sections(teacher_id=teacher_id) if teacher else [
            section for section in db.get_sections() if section["id"] == guest_session["section_id"]
        ]
        owned_section_ids = {section["id"] for section in sections}
        seats = [seat for seat in db.get_seats() if seat.get("section_id") in owned_section_ids] if teacher else []
        await websocket.send_text(
            json.dumps(
                {
                    "type": "INITIAL_STATE",
                    "payload": {
                        "active_session": _guest_safe(active_session),
                        "sections": sections,
                        "seats": _guest_safe(seats),
                        "edge_nodes": len(edge_subscribers),
                    },
                }
            )
        )
        while True:
            data = await websocket.receive_text()
            if not (_valid_teacher_token(event_tokens.get(websocket)) if teacher else _guest_session(event_tokens.get(websocket))):
                await websocket.close(code=1008)
                break
            # Client heartbeats or commands
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        event_subscribers.pop(websocket, None)
        event_teacher_ids.pop(websocket, None)
    except Exception:
        event_subscribers.pop(websocket, None)
        event_teacher_ids.pop(websocket, None)
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
    finally:
        event_subscribers.pop(websocket, None)
        event_teacher_ids.pop(websocket, None)
        event_tokens.pop(websocket, None)


@app.websocket("/ws/edge")
async def ws_edge(websocket: WebSocket):
    """
    Bidirectional WebSocket for Camera Node ↔ Web Dashboard communication.
    Camera Nodes connect here to:
    - Receive session start/stop commands
    - Stream gesture events in real-time
    - Get seat configuration updates
    """
    if not _validate_edge_key(websocket.headers.get("x-edge-key", "")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    edge_subscribers.add(websocket)
    selected = websocket.query_params.get("section_id")
    sections = db.get_sections()
    if not selected and len(sections) == 1:
        selected = sections[0]["id"]
    edge_sections[websocket] = selected
    print(f"[Edge] Camera Node connected. Total nodes: {len(edge_subscribers)}")
    try:
        # Send current state to newly connected node
        active_session = db.get_active_session(section_id=selected) if selected else None
        await websocket.send_text(json.dumps({
            "type": "EDGE_INIT",
            "active_session": active_session,
            "selected_section_id": active_session.get("section_id") if active_session else selected,
            "sections": db.get_sections(),
            "camera_settings": teacher_camera_settings.get(db.get_section_owner(selected), camera_settings),
        }))

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
                continue

            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "")

                if msg_type == 'SELECT_SECTION':
                    selected = msg.get('section_id')
                    if any(s['id'] == selected for s in db.get_sections()):
                        edge_sections[websocket] = selected
                        await websocket.send_text(json.dumps({'type': 'EDGE_INIT', 'active_session': db.get_active_session(section_id=selected),
                            'selected_section_id': selected, 'sections': db.get_sections(), 'camera_settings': teacher_camera_settings.get(db.get_section_owner(selected), camera_settings)}))
                if msg_type == "GESTURE_EVENT":
                    body = EdgeEventIngest(**msg.get("payload", {}))
                    try:
                        if edge_sections.get(websocket) != body.section_id:
                            raise HTTPException(status_code=403, detail="Select this section on the camera before sending events")
                        result = await ingest_edge_event(body, x_edge_key=websocket.headers.get("x-edge-key"))
                        await websocket.send_text(json.dumps({"type": "EVENT_ACK", **result}))
                    except HTTPException as error:
                        await websocket.send_text(json.dumps({
                            "type": "EVENT_REJECTED", "event_id": body.event_id,
                            "status": error.status_code, "detail": error.detail,
                        }))

            except Exception as e:
                print(f"[Edge WS] Error processing message: {e}")

    except WebSocketDisconnect:
        edge_subscribers.discard(websocket)
        print(f"[Edge] Camera Node disconnected. Remaining: {len(edge_subscribers)}")
    except Exception:
        edge_subscribers.discard(websocket)
    finally:
        edge_subscribers.discard(websocket)
        edge_sections.pop(websocket, None)


# ---------------------------------------------------------------------------
# WEB FRONTEND MOUNT
# ---------------------------------------------------------------------------
web_path = Path(__file__).resolve().parent.parent / "web"
if web_path.exists():
    app.mount("/client", StaticFiles(directory=str(web_path), html=True), name="web_client")

    @app.get("/admin")
    @app.get("/login")
    @app.get("/")
    async def serve_index():
        return FileResponse(str(web_path / "index.html"))
