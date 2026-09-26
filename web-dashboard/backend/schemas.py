from typing import List, Optional, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", allow_inf_nan=False)

    @field_validator('*', mode='before')
    @classmethod
    def clean_text(cls, value):
        if isinstance(value, str):
            if any(ord(c) < 32 and c not in '\t\n\r' for c in value) or '<' in value or '>' in value:
                raise ValueError("Use plain text without markup or control characters")
            return value.strip()
        return value


class SectionCreateRequest(InputModel):
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(default="General", min_length=1, max_length=120)
    room: str = Field(default="Room 204", min_length=1, max_length=120)


class SectionResponse(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=120)
    subject: str
    room: str
    created_at: str
    student_count: int = 0
    session_count: int = 0
    teacher_id: Optional[str] = None


class StudentRegisterRequest(InputModel):
    student_name: str = Field(min_length=1, max_length=120)
    student_id_number: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    seat_id: Optional[str] = None
    label: Optional[str] = None


class SessionStartRequest(InputModel):
    title: str = Field(default="Classroom Recitation Session", min_length=1, max_length=160)
    section_id: Optional[str] = None


class SessionResponse(BaseModel):
    id: str
    title: str
    section_id: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None


class SeatSchema(InputModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    section_id: Optional[str] = None
    label: str = Field(min_length=1, max_length=80)
    student_name: str = Field(min_length=1, max_length=120)
    student_id_number: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9._ /-]*$")
    student_id: Optional[str] = None
    photo_path: Optional[str] = ""
    face_embedding: Optional[str] = None
    grid_row: Optional[int] = Field(default=0, ge=0, le=99)
    grid_col: Optional[int] = Field(default=0, ge=0, le=99)
    x_min: float = Field(ge=0, le=1)
    y_min: float = Field(ge=0, le=1)
    x_max: float = Field(ge=0, le=1)
    y_max: float = Field(ge=0, le=1)
    is_present: bool = True
    total_points: int = 0


    @model_validator(mode='after')
    def valid_rectangle(self):
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("Desk bounds must form a positive rectangle")
        return self


class SeatBulkUpdateRequest(InputModel):
    seats: List[SeatSchema] = Field(max_length=100)


class SeatAttendanceToggleResponse(BaseModel):
    id: str
    is_present: bool


class GestureEventSchema(BaseModel):
    id: str
    session_id: Optional[str] = None
    seat_id: str
    student_name: str = Field(min_length=1, max_length=120)
    status: str  # "VALID" | "INVALID"
    reason_code: str  # "VALID_HAND_RAISE" | "ERR_DOUBLE_HAND_RAISE" | "ERR_ELBOW_ACUTE_ANGLE" | "ERR_INSUFFICIENT_DURATION"
    arm_angle: float
    duration_sec: float
    queue_pos: Optional[int] = None
    timestamp_ms: int = Field(ge=0)
    earned_point: Optional[int] = None  # 1 = awarded, 0 = dismissed/false positive, None = pending


class EventActionResponse(BaseModel):
    id: str
    status: str
    earned_point: Optional[int]
    message: str


class GridConfigureRequest(InputModel):
    rows: int = Field(default=3, ge=1, le=10)
    cols: int = Field(default=4, ge=1, le=10)


class SeatAssignRequest(InputModel):
    student_id: Optional[str] = None


class SeatSwapRequest(InputModel):
    seat_id_1: str
    seat_id_2: str


class StudentEnrollRequest(InputModel):
    name: str = Field(min_length=1, max_length=120)
    student_id_number: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    photo_base64: Optional[str] = Field(default=None, max_length=7_000_000)
    assign_to_seat_id: Optional[str] = None
    auto_create_desk: Optional[bool] = False


# ---------- Edge Camera Node Ingest Schema ----------

class EdgeEventIngest(InputModel):
    """
    Schema for gesture events received from remote Camera Nodes.
    Posted to POST /api/events/ingest by the camera-node's api_client.
    """
    event_id: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    section_id: str
    seat_id: str
    student_id: Optional[str] = None
    student_name: str = Field(min_length=1, max_length=120)
    status: Literal["VALID", "INVALID", "IDLE"]              # "VALID" | "INVALID" | "IDLE"
    reason_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z0-9_]+$")         # "VALID_HAND_RAISE" | "HAND_LOWERED" | "ERR_*" etc.
    arm_angle_deg: float = Field(default=0.0, ge=0, le=180)
    duration_sec: float = Field(default=0.0, ge=0, le=3600)
    timestamp_ms: int = Field(ge=0)
    queue_pos: Optional[int] = None
    delta_ms: Optional[int] = None
    podium_rank: Optional[int] = None  # Alias for queue_pos (plan compat)
    session_id: Optional[str] = None  # If camera node knows the active session
    earned_point: None = None
