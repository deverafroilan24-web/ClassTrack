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
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from backend.database import DatabaseManager, verify_pin_hash
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
podium_queue = PodiumQueueManager()
PIN_VERSION = hashlib.sha256((os.getenv("TEACHER_PIN") or db.get_setting("teacher_pin_hash") or "").encode()).hexdigest()[:16]

# Server secrets. A generated auth secret invalidates tokens after a restart.
EDGE_API_KEY = os.getenv("EDGE_API_KEY", "")
AUTH_SECRET = (os.getenv("AUTH_SECRET") or secrets.token_urlsafe(32)).encode()
TOKEN_LIFETIME_SECONDS = 8 * 60 * 60
pin_attempts: Dict[str, tuple[int, float]] = {}

event_subscribers: Dict[WebSocket, Optional[str]] = {}
edge_subscribers: Set[WebSocket] = set()


class ConnectionManager:
    @staticmethod
    async def broadcast_event(event_dict: dict):
        if not event_subscribers:
            return
        public_event = _guest_safe(event_dict)
        if public_event.get("type") == "STUDENTS_UPDATED":
            public_event.pop("payload", None)
        msg = json.dumps(public_event)
        active = db.get_active_session()
        guest_message = None
        if active and any(session_id is not None for session_id in event_subscribers.values()) and event_dict.get("type") in {
            "GESTURE_EVENT", "POINT_AWARDED", "EVENT_DISMISSED", "SEATS_UPDATED",
            "STUDENTS_UPDATED", "SECTIONS_UPDATED",
        }:
            guest_message = json.dumps({
                "type": event_dict["type"],
                "ledger": _guest_safe(db.get_recitation_ledger(
                    session_id=active["id"], section_id=active["section_id"],
                    active_session_required=True, active_podium_map=podium_queue.get_podium_map(),
                )),
            })
        dead = []
        for ws, guest_session_id in list(event_subscribers.items()):
            try:
                if guest_session_id is None:
                    await ws.send_text(msg)
                elif event_dict.get("type") == "SESSION_STARTED" and active and guest_session_id != active["id"]:
                    await ws.send_text(json.dumps({"type": "SESSION_STOPPED", "ledger": []}))
                elif active and guest_session_id == active["id"] and guest_message:
                    await ws.send_text(guest_message)
                elif event_dict.get("type") == "SESSION_STOPPED" and event_dict.get("payload", {}).get("id") == guest_session_id:
                    await ws.send_text(json.dumps({"type": "SESSION_STOPPED", "ledger": []}))
            except Exception:
                dead.append(ws)
        for ws in dead:
            event_subscribers.pop(ws, None)

    @staticmethod
    async def broadcast_to_edge(event_dict: dict):
        """Broadcast commands/state to connected Camera Nodes."""
        if not edge_subscribers:
            return
        msg = json.dumps(event_dict)
        dead = []
        for ws in list(edge_subscribers):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            edge_subscribers.discard(ws)


main_loop: Optional[asyncio.AbstractEventLoop] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
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
class PinRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=128)


class GuestCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


def _teacher_pin_matches(pin: str) -> bool:
    return verify_pin_hash(pin, db.get_setting("teacher_pin_hash") or "")


def _create_teacher_token() -> str:
    payload = json.dumps({"role": "teacher", "pin_version": PIN_VERSION,
                          "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
                          "nonce": secrets.token_hex(12)}, separators=(",", ":")).encode()
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
    return bool(payload and payload.get("role") == "teacher" and payload.get("pin_version") == PIN_VERSION)


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
    active = db.get_active_session()
    if active and active.get("section_id") and payload.get("session_id") == active["id"] and payload.get("section_id") == active["section_id"]:
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
    if path.startswith("/api/"):
        guest_get = {"/api/sections", "/api/sessions/active", "/api/recitation/ledger"}
        public_get = {"/api/edge/status", "/api/auth/guest-session"}
        public_post = {"/api/auth/verify-pin", "/api/auth/guest", "/api/events/ingest"}
        if method == "POST" and path == "/api/events/ingest":
            if not EDGE_API_KEY:
                return JSONResponse({"detail": "EDGE_API_KEY is not configured"}, status_code=503)
            if not _validate_edge_key(request.headers.get("x-edge-key", "")):
                return JSONResponse({"detail": "Invalid or missing X-Edge-Key"}, status_code=401)
        if method == "GET" and path in public_get:
            pass
        elif method == "GET" and path in guest_get and (
            _valid_teacher_token(request.headers.get("x-teacher-token"))
            or _guest_session(request.headers.get("x-guest-token"))
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
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/auth/verify-pin")
async def verify_teacher_pin(body: PinRequest, request: Request):
    client = request.client.host if request.client else "unknown"
    count, reset_at = pin_attempts.get(client, (0, 0.0))
    if time.monotonic() >= reset_at:
        count, reset_at = 0, time.monotonic() + 300
    if count >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in five minutes.")
    if not _teacher_pin_matches(body.pin):
        pin_attempts[client] = (count + 1, reset_at)
        raise HTTPException(status_code=401, detail="Incorrect teacher PIN")
    pin_attempts.pop(client, None)
    return {"authenticated": True, "token": _create_teacher_token()}


@app.get("/api/auth/session")
async def get_auth_session(x_teacher_token: Optional[str] = Header(default=None)):
    if not _valid_teacher_token(x_teacher_token):
        raise HTTPException(status_code=401, detail="Teacher session expired")
    return {"authenticated": True, "role": "teacher"}


@app.post("/api/auth/guest")
async def verify_guest_code(body: GuestCodeRequest, request: Request):
    client = f"guest:{request.client.host if request.client else 'unknown'}"
    count, reset_at = pin_attempts.get(client, (0, 0.0))
    if time.monotonic() >= reset_at:
        count, reset_at = 0, time.monotonic() + 300
    if count >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in five minutes.")
    active = db.get_active_session()
    if not active or not active.get("section_id") or not active.get("guest_code") or not secrets.compare_digest(body.code.strip().upper(), active["guest_code"]):
        pin_attempts[client] = (count + 1, reset_at)
        raise HTTPException(status_code=401, detail="Invalid viewing code or no live session")
    pin_attempts.pop(client, None)
    return {"authenticated": True, "token": _create_guest_token(active), "section_id": active["section_id"]}


@app.get("/api/auth/guest-session")
async def get_guest_auth_session(x_guest_token: Optional[str] = Header(default=None)):
    active = _guest_session(x_guest_token)
    if not active:
        raise HTTPException(status_code=401, detail="Viewing code expired")
    return {"authenticated": True, "section_id": active["section_id"]}


def _validate_edge_key(provided_key: str) -> bool:
    return bool(EDGE_API_KEY) and secrets.compare_digest(provided_key or "", EDGE_API_KEY)


def _require_edge_auth(x_edge_key: Optional[str] = None) -> None:
    if not EDGE_API_KEY:
        raise HTTPException(status_code=503, detail="EDGE_API_KEY is not configured")
    if not _validate_edge_key(x_edge_key or ""):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Edge-Key")


# ---------------------------------------------------------------------------
# REST API ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/api/sections", response_model=List[SectionResponse])
async def get_sections(x_teacher_token: Optional[str] = Header(default=None),
                       x_guest_token: Optional[str] = Header(default=None)):
    sections = db.get_sections()
    if _valid_teacher_token(x_teacher_token):
        return sections
    active = _guest_session(x_guest_token)
    return [section for section in sections if section["id"] == active["section_id"]]


@app.post("/api/sections", response_model=SectionResponse, status_code=status.HTTP_201_CREATED)
async def create_section(body: SectionCreateRequest):
    sec = db.create_section(name=body.name, subject=body.subject, room=body.room)
    await ConnectionManager.broadcast_event({"type": "SECTIONS_UPDATED", "payload": db.get_sections()})
    return sec


@app.delete("/api/sections/{section_id}")
async def delete_section(section_id: str):
    db.delete_section(section_id)
    await ConnectionManager.broadcast_event({"type": "SECTIONS_UPDATED", "payload": db.get_sections()})
    return {"message": "Section deleted successfully"}


@app.get("/api/sections/{section_id}/students")
async def get_section_students(section_id: str):
    return db.get_students(section_id)


@app.post("/api/sections/{section_id}/students/enroll")
async def enroll_student(section_id: str, body: StudentEnrollRequest):
    photo_rel_path = ""
    face_emb_str = ""

    if body.photo_base64:
        try:
            raw_b64 = body.photo_base64
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]
            img_bytes = base64.b64decode(raw_b64)

            stud_filename = f"stud_{uuid.uuid4().hex[:8]}.jpg"
            save_path = uploads_dir / stud_filename

            # Save photo directly — no face engine processing in cloud mode
            with open(save_path, "wb") as f:
                f.write(img_bytes)
            photo_rel_path = f"/uploads/students/{stud_filename}"
        except Exception as e:
            print(f"Error processing enrolled photo: {e}")

    stud = db.enroll_student(
        section_id=section_id,
        name=body.name,
        student_id_number=body.student_id_number,
        photo_path=photo_rel_path,
        face_embedding=face_emb_str,
        assign_to_seat_id=body.assign_to_seat_id,
        auto_create_desk=bool(body.auto_create_desk),
    )

    seats = db.get_seats(section_id=section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "STUDENTS_UPDATED",
        "payload": students,
        "seats": seats,
    })
    return stud


@app.delete("/api/students/{student_id}")
async def delete_student(student_id: str, section_id: Optional[str] = None):
    db.delete_student(student_id)
    seats = db.get_seats(section_id=section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "STUDENTS_UPDATED",
        "payload": students,
        "seats": seats,
    })
    return {"message": "Student deleted"}


@app.post("/api/sections/{section_id}/seats/auto-generate-enrolled")
async def auto_generate_desks_enrolled(section_id: str):
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
async def configure_seating_grid(section_id: str, body: GridConfigureRequest):
    updated_seats = db.configure_seating_grid(section_id, body.rows, body.cols)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated_seats})
    return updated_seats


@app.post("/api/sections/{section_id}/seats/auto-fill-alphabetical")
async def auto_fill_seats_alphabetical(section_id: str):
    updated_seats = db.auto_fill_seats_alphabetical(section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": updated_seats,
        "students": students,
    })
    return updated_seats


@app.post("/api/sections/{section_id}/seats/clear-assignments")
async def clear_seat_assignments(section_id: str):
    updated_seats = db.clear_seat_assignments(section_id)
    students = db.get_students(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SEATS_UPDATED",
        "payload": updated_seats,
        "students": students,
    })
    return updated_seats


@app.post("/api/seats/{seat_id}/assign")
async def assign_student_seat(seat_id: str, body: SeatAssignRequest, section_id: Optional[str] = None):
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
async def swap_seats(body: SeatSwapRequest, section_id: Optional[str] = None):
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
async def get_heatmap(section_id: Optional[str] = None):
    return db.get_participation_heatmap(section_id=section_id)


@app.get("/api/analytics/grades")
async def get_participation_grades(section_id: str, target: int = 5, weight: float = 100.0):
    return db.calculate_participation_grades(section_id=section_id, target_raises=target, weight_percent=weight)


@app.get("/api/exports/grades.csv")
async def export_grades_csv(section_id: str, target: int = 5, weight: float = 100.0):
    grades = db.calculate_participation_grades(section_id=section_id, target_raises=target, weight_percent=weight)
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
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
async def register_student(section_id: str, body: StudentRegisterRequest):
    seat = db.register_student(
        section_id=section_id,
        student_name=body.student_name,
        student_id_number=body.student_id_number,
        seat_id=body.seat_id,
        label=body.label,
    )
    seats = db.get_seats(section_id=section_id)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": seats})
    return seat


@app.get("/api/recitation/ledger")
async def get_recitation_ledger(session_id: Optional[str] = None, section_id: Optional[str] = None,
                                x_teacher_token: Optional[str] = Header(default=None),
                                x_guest_token: Optional[str] = Header(default=None)):
    # If session_id not explicitly provided, default to active session
    teacher = _valid_teacher_token(x_teacher_token)
    active = db.get_active_session() if teacher else _guest_session(x_guest_token)
    if not teacher:
        if (session_id and session_id != active["id"]) or (section_id and section_id != active["section_id"]):
            raise HTTPException(status_code=403, detail="This viewing code belongs to another class session")
        section_id = active["section_id"]
    target_session_id = session_id if session_id is not None else (active["id"] if active else None)
    active_podium_map = podium_queue.get_podium_map()
    ledger = db.get_recitation_ledger(
        session_id=target_session_id,
        section_id=section_id,
        active_session_required=True,
        active_podium_map=active_podium_map,
    )
    return ledger if teacher else _guest_safe(ledger)


@app.get("/api/sessions")
async def get_sessions(section_id: Optional[str] = None, date: Optional[str] = None):
    return db.get_sessions(section_id=section_id, date_filter=date)


@app.get("/api/sessions/active", response_model=Optional[SessionResponse])
async def get_active_session():
    return db.get_active_session()


@app.get("/api/sessions/active/guest-access")
async def get_active_guest_access():
    active = db.get_active_session()
    if not active or not active.get("section_id"):
        raise HTTPException(status_code=404, detail="Start a section class session to create a viewing code")
    return {"code": active["guest_code"], "section_id": active["section_id"]}


@app.get("/api/sessions/{session_id}")
async def get_session_details(session_id: str):
    details = db.get_session_details(session_id)
    if not details:
        raise HTTPException(status_code=404, detail="Session not found")
    return details


@app.delete("/api/sessions")
async def clear_all_sessions(section_id: Optional[str] = None):
    deleted_count = db.clear_all_sessions(section_id=section_id)
    await ConnectionManager.broadcast_event({
        "type": "SESSIONS_CLEARED",
        "payload": {"section_id": section_id, "deleted_count": deleted_count},
    })
    return {"status": "success", "deleted_count": deleted_count}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    db.delete_session(session_id)
    await ConnectionManager.broadcast_event({"type": "SESSION_DELETED", "payload": {"session_id": session_id}})
    return {"status": "success", "deleted": session_id}


@app.post("/api/sessions/start", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def start_session(body: SessionStartRequest):
    if not body.section_id or body.section_id not in {section["id"] for section in db.get_sections()}:
        raise HTTPException(status_code=400, detail="Select a valid class section before starting")
    sess = db.start_session(title=body.title, section_id=body.section_id)
    # Clear podium queue for fresh session
    podium_queue.clear()
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
async def stop_session():
    sess = db.stop_session()
    podium_queue.clear()
    empty_ledger = db.get_recitation_ledger(session_id=None, active_session_required=True)
    if sess:
        await ConnectionManager.broadcast_event({"type": "SESSION_STOPPED", "payload": sess, "ledger": empty_ledger})
        await ConnectionManager.broadcast_to_edge({"type": "SESSION_STOPPED"})
    return sess


@app.get("/api/seats", response_model=List[SeatSchema])
async def get_seats(section_id: Optional[str] = None,
                    x_teacher_token: Optional[str] = Header(default=None),
                    x_edge_key: Optional[str] = Header(default=None)):
    return db.get_seats(section_id=section_id)


@app.put("/api/seats", response_model=List[SeatSchema])
async def update_seats(body: SeatBulkUpdateRequest, section_id: Optional[str] = None):
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
async def set_section_attendance(section_id: str, is_present: bool = True):
    updated = db.set_section_attendance(section_id, is_present=is_present)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated})
    return updated


@app.post("/api/seats/{seat_id}/toggle-attendance", response_model=SeatAttendanceToggleResponse)
async def toggle_seat_attendance(seat_id: str):
    try:
        res = db.toggle_attendance(seat_id)
        await ConnectionManager.broadcast_event({"type": "ATTENDANCE_TOGGLED", "payload": res})
        return res
    except KeyError:
        raise HTTPException(status_code=404, detail="Seat not found")


@app.post("/api/seats/preset/{preset_name}", response_model=List[SeatSchema])
async def load_seat_preset(preset_name: str, section_id: Optional[str] = None):
    try:
        updated = db.load_preset_seats(preset_name, section_id=section_id)
        await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated})
        return updated
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/seats/{seat_id}")
async def delete_seat(seat_id: str, section_id: Optional[str] = None):
    db.delete_seat(seat_id)
    updated = db.get_seats(section_id=section_id)
    await ConnectionManager.broadcast_event({"type": "SEATS_UPDATED", "payload": updated})
    return {"message": "Seat deleted", "seats": updated}


@app.get("/api/events")
async def get_events(limit: int = 50):
    active = db.get_active_session()
    session_id = active["id"] if active else None
    return db.get_events(session_id=session_id, limit=limit)


@app.post("/api/events/{event_id}/award", response_model=EventActionResponse)
async def award_event_point(event_id: str, force: bool = False):
    try:
        res = db.award_point(event_id, force_override=force)
        active = db.get_active_session()
        active_id = active["id"] if active else None
        active_podium_map = podium_queue.get_podium_map()
        ledger = db.get_recitation_ledger(session_id=active_id, active_session_required=True, active_podium_map=active_podium_map)
        # Notify clients with instant ledger push
        await ConnectionManager.broadcast_event({
            "type": "POINT_AWARDED",
            "payload": res,
            "ledger": ledger,
            "seats": db.get_seats(),
        })
        return res
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except KeyError:
        raise HTTPException(status_code=404, detail="Event not found")


@app.post("/api/events/{event_id}/dismiss", response_model=EventActionResponse)
async def dismiss_event(event_id: str):
    try:
        res = db.dismiss_event(event_id)
        active = db.get_active_session()
        active_id = active["id"] if active else None
        active_podium_map = podium_queue.get_podium_map()
        ledger = db.get_recitation_ledger(session_id=active_id, active_session_required=True, active_podium_map=active_podium_map)
        await ConnectionManager.broadcast_event({
            "type": "EVENT_DISMISSED",
            "payload": res,
            "ledger": ledger,
        })
        return res
    except KeyError:
        raise HTTPException(status_code=404, detail="Event not found")


@app.get("/api/exports/session-report.csv")
async def export_csv(session_id: Optional[str] = None, section_id: Optional[str] = None):
    target_session_id = session_id
    if not target_session_id:
        active = db.get_active_session()
        target_session_id = active["id"] if active else None
    csv_text = db.export_session_report_csv(session_id=target_session_id, section_id=section_id)
    filename = f"session-report-{target_session_id or 'all'}.csv"
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/exports/class-report.csv")
async def export_class_csv(section_id: Optional[str] = None):
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

    active = db.get_active_session()
    if not active:
        raise HTTPException(status_code=409, detail="No active session — start a session first")
    if body.section_id != active["section_id"]:
        raise HTTPException(status_code=409, detail="Camera event belongs to another class section")

    session_id = active["id"]

    # Update server-side podium queue
    if body.status == "VALID" and body.reason_code == "VALID_HAND_RAISE":
        podium_queue.register_raise(
            seat_id=body.seat_id,
            student_name=body.student_name,
            arm_angle=body.arm_angle_deg,
            timestamp_ms=body.timestamp_ms,
        )
    elif body.reason_code == "HAND_LOWERED":
        podium_queue.release_raise(body.seat_id)

    # Generate event ID
    event_id = body.session_id or f"evt_{body.timestamp_ms}_{uuid.uuid4().hex[:6]}"

    # Build event dict for DB persistence
    podium_entry = podium_queue.get_entry(body.seat_id)
    fallback_queue_pos = body.queue_pos if body.queue_pos is not None else body.podium_rank
    event_dict = {
        "event_id": event_id,
        "session_id": session_id,
        "seat_id": body.seat_id,
        "student_name": body.student_name,
        "status": body.status,
        "reason_code": body.reason_code,
        "arm_angle_deg": body.arm_angle_deg,
        "duration_sec": body.duration_sec,
        "queue_pos": podium_entry.queue_position if podium_entry else fallback_queue_pos,
        "delta_ms": podium_entry.delta_ms if podium_entry else body.delta_ms,
        "timestamp_ms": body.timestamp_ms,
        "earned_point": body.earned_point,
    }

    try:
        db.insert_event(event_dict)
    except Exception as e:
        print(f"[Edge Ingest] Error persisting event to DB: {e}")

    # Broadcast to all browser WebSocket clients with optimized ledger
    active_podium_map = podium_queue.get_podium_map()
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
async def get_camera_settings():
    """Returns camera settings and available AI models."""
    return {
        "settings": camera_settings,
        "available_models": AVAILABLE_MODELS,
        "connected_nodes": len(edge_subscribers),
    }


@app.post("/api/camera/settings")
async def update_camera_settings(payload: dict):
    """
    Update camera settings and broadcast to connected Camera Nodes via WebSocket.
    """
    global camera_settings
    if "show_skeleton" in payload:
        camera_settings["show_skeleton"] = bool(payload["show_skeleton"])
    if "model_name" in payload and payload["model_name"]:
        camera_settings["model_name"] = str(payload["model_name"])
    if "confidence" in payload:
        try:
            camera_settings["confidence"] = max(0.1, min(0.95, float(payload["confidence"])))
        except ValueError:
            pass

    # Broadcast updated settings to all connected Camera Nodes
    await ConnectionManager.broadcast_to_edge({
        "type": "CAMERA_SETTINGS_UPDATED",
        "settings": camera_settings,
    })

    # Also broadcast to browser clients
    await ConnectionManager.broadcast_event({
        "type": "CAMERA_SETTINGS_CHANGED",
        "settings": camera_settings,
    })

    return {"status": "ok", "settings": camera_settings}


@app.get("/api/edge/status")
async def get_edge_status():
    """Returns status of connected Camera Nodes."""
    return {
        "connected_nodes": len(edge_subscribers),
        "podium_active": len(podium_queue.get_podium()),
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
        event_subscribers[websocket] = None if teacher else guest_session["id"]
        # Send initial snapshot of state
        active_session = db.get_active_session() if teacher else guest_session
        sections = db.get_sections() if teacher else [
            section for section in db.get_sections() if section["id"] == guest_session["section_id"]
        ]
        seats = db.get_seats() if teacher else []
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
            # Client heartbeats or commands
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        event_subscribers.pop(websocket, None)
    except Exception:
        event_subscribers.pop(websocket, None)
        try:
            await websocket.close(code=1008)
        except Exception:
            pass


@app.websocket("/ws/edge")
async def ws_edge(websocket: WebSocket):
    """
    Bidirectional WebSocket for Camera Node ↔ Web Dashboard communication.
    Camera Nodes connect here to:
    - Receive session start/stop commands
    - Stream gesture events in real-time
    - Get seat configuration updates
    """
    if not EDGE_API_KEY or not _validate_edge_key(websocket.headers.get("x-edge-key", "")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    edge_subscribers.add(websocket)
    print(f"[Edge] Camera Node connected. Total nodes: {len(edge_subscribers)}")
    try:
        # Send current state to newly connected node
        active_session = db.get_active_session()
        await websocket.send_text(json.dumps({
            "type": "EDGE_INIT",
            "active_session": active_session,
            "sections": db.get_sections(),
            "camera_settings": camera_settings,
        }))

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
                continue

            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "")

                if msg_type == "GESTURE_EVENT":
                    # Process inline gesture event from edge WebSocket
                    body = EdgeEventIngest(**msg.get("payload", {}))
                    # Reuse the ingest logic
                    active = db.get_active_session()
                    if active:
                        if body.section_id != active["section_id"]:
                            continue
                        session_id = active["id"]
                        if body.status == "VALID" and body.reason_code == "VALID_HAND_RAISE":
                            podium_queue.register_raise(
                                seat_id=body.seat_id,
                                student_name=body.student_name,
                                arm_angle=body.arm_angle_deg,
                                timestamp_ms=body.timestamp_ms,
                            )
                        elif body.reason_code == "HAND_LOWERED":
                            podium_queue.release_raise(body.seat_id)

                        event_id = f"evt_{body.timestamp_ms}_{uuid.uuid4().hex[:6]}"
                        podium_entry = podium_queue.get_entry(body.seat_id)
                        fallback_qp = body.queue_pos if body.queue_pos is not None else body.podium_rank
                        event_dict = {
                            "event_id": event_id,
                            "session_id": session_id,
                            "seat_id": body.seat_id,
                            "student_name": body.student_name,
                            "status": body.status,
                            "reason_code": body.reason_code,
                            "arm_angle_deg": body.arm_angle_deg,
                            "duration_sec": body.duration_sec,
                            "queue_pos": podium_entry.queue_position if podium_entry else fallback_qp,
                            "delta_ms": podium_entry.delta_ms if podium_entry else body.delta_ms,
                            "timestamp_ms": body.timestamp_ms,
                            "earned_point": body.earned_point,
                        }
                        try:
                            db.insert_event(event_dict)
                        except Exception as e:
                            print(f"[Edge WS] DB error: {e}")

                        active_podium_map = podium_queue.get_podium_map()
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

            except Exception as e:
                print(f"[Edge WS] Error processing message: {e}")

    except WebSocketDisconnect:
        edge_subscribers.discard(websocket)
        print(f"[Edge] Camera Node disconnected. Remaining: {len(edge_subscribers)}")
    except Exception:
        edge_subscribers.discard(websocket)


# ---------------------------------------------------------------------------
# WEB FRONTEND MOUNT
# ---------------------------------------------------------------------------
web_path = Path(__file__).resolve().parent.parent / "web"
if web_path.exists():
    app.mount("/client", StaticFiles(directory=str(web_path), html=True), name="web_client")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(web_path / "index.html"))
