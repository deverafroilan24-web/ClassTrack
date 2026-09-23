"""
Camera Node Configuration.

Loads settings from environment variables or .env file.
The camera-node communicates with a remote Web Dashboard
to send detected gesture events and fetch seat configurations.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Remote Web Dashboard URL (where gesture events are sent)
WEB_DASHBOARD_URL = os.getenv("WEB_DASHBOARD_URL", "https://classtrack-mm41.onrender.com")

# Edge authentication key (must match EDGE_API_KEY on the web-dashboard)
EDGE_API_KEY = os.getenv("EDGE_API_KEY", "")

# Active section ID to load seat zones for
SECTION_ID = os.getenv("SECTION_ID", "")

# Camera source (0 = default webcam, 1 = external USB, or file path)
VIDEO_SOURCE = os.getenv("VIDEO_SOURCE", "0")

# YOLO model to use for pose estimation
YOLO_MODEL = os.getenv("YOLO_MODEL", "yolo11s-pose.pt")

# Detection confidence threshold
DETECTION_CONFIDENCE = float(os.getenv("DETECTION_CONFIDENCE", "0.50"))

# Target FPS for the vision loop
TARGET_FPS = int(os.getenv("TARGET_FPS", "30"))


def resolve_model_path(model_name: str = "") -> str:
    """
    Resolve a YOLO .pt path within desktop-app/models or desktop-app/.
    Supports both source execution and frozen PyInstaller bundles.
    """
    from pathlib import Path
    import sys

    name = (model_name or YOLO_MODEL or "").strip() or "yolo11s-pose.pt"
    # Absolute or CWD-relative hit
    direct = Path(name)
    if direct.is_file():
        return str(direct)

    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent

    candidates = [
        base / "models" / Path(name).name,
        base / Path(name).name,
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return name
