from typing import List, Optional
from pydantic import BaseModel, Field


class SectionCreateRequest(BaseModel):
    name: str
    subject: str = Field(default="General")
    room: str = Field(default="Room 204")


class SectionResponse(BaseModel):
    id: str
    name: str
    subject: str
    room: str
    created_at: str
    student_count: int = 0
    session_count: int = 0
    teacher_id: Optional[str] = None


class StudentRegisterRequest(BaseModel):
    student_name: str
    student_id_number: str = ""
    seat_id: Optional[str] = None
    label: Optional[str] = None


class SessionStartRequest(BaseModel):
    title: str = Field(default="Classroom Recitation Session")
    section_id: Optional[str] = None


class SessionResponse(BaseModel):
    id: str
    title: str
    section_id: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None


class SeatSchema(BaseModel):
    id: str
    section_id: Optional[str] = None
    label: str
    student_name: str
    student_id_number: str = ""
    student_id: Optional[str] = None
    photo_path: Optional[str] = ""
    face_embedding: Optional[str] = None
    grid_row: Optional[int] = 0
    grid_col: Optional[int] = 0
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    is_present: bool = True
    total_points: int = 0


class SeatBulkUpdateRequest(BaseModel):
    seats: List[SeatSchema]


class SeatAttendanceToggleResponse(BaseModel):
    id: str
    is_present: bool


class GestureEventSchema(BaseModel):
    id: str
    session_id: Optional[str] = None
    seat_id: str
    student_name: str
    status: str  # "VALID" | "INVALID"
    reason_code: str  # "VALID_HAND_RAISE" | "ERR_DOUBLE_HAND_RAISE" | "ERR_ELBOW_ACUTE_ANGLE" | "ERR_INSUFFICIENT_DURATION"
    arm_angle: float
    duration_sec: float
    queue_pos: Optional[int] = None
    timestamp_ms: int
    earned_point: Optional[int] = None  # 1 = awarded, 0 = dismissed/false positive, None = pending


class EventActionResponse(BaseModel):
    id: str
    status: str
    earned_point: Optional[int]
    message: str


class GridConfigureRequest(BaseModel):
    rows: int = Field(default=3, ge=1, le=10)
    cols: int = Field(default=4, ge=1, le=10)


class SeatAssignRequest(BaseModel):
    student_id: Optional[str] = None


class SeatSwapRequest(BaseModel):
    seat_id_1: str
    seat_id_2: str


class StudentEnrollRequest(BaseModel):
    name: str
    student_id_number: str = ""
    photo_base64: Optional[str] = None
    assign_to_seat_id: Optional[str] = None
    auto_create_desk: Optional[bool] = False


# ---------- Edge Camera Node Ingest Schema ----------

class EdgeEventIngest(BaseModel):
    """
    Schema for gesture events received from remote Camera Nodes.
    Posted to POST /api/events/ingest by the camera-node's api_client.
    """
    event_id: Optional[str] = None
    section_id: str
    seat_id: str
    student_id: Optional[str] = None
    student_name: str
    status: str              # "VALID" | "INVALID" | "IDLE"
    reason_code: str         # "VALID_HAND_RAISE" | "HAND_LOWERED" | "ERR_*" etc.
    arm_angle_deg: float = 0.0
    duration_sec: float = 0.0
    timestamp_ms: int
    queue_pos: Optional[int] = None
    delta_ms: Optional[int] = None
    podium_rank: Optional[int] = None  # Alias for queue_pos (plan compat)
    session_id: Optional[str] = None  # If camera node knows the active session
    earned_point: Optional[int] = None
