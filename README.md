# ClassTrack — Desktop App & Web Dashboard

Decoupled, dual-system architecture for real-time classroom hand-tracking and recitation monitoring.

---

## Architecture Overview

```text
HandTracking - Desktop and Web/
├── desktop-app/                      # Desktop Camera Node (Local Classroom PC)
│   ├── models/                       # YOLO pose model weights (yolo11s-pose.pt)
│   ├── vision_worker/                # Local vision pipeline & pose engine
│   ├── api_client.py                 # REST & WebSocket client to Dashboard
│   ├── config.py                     # Self-contained configuration
│   ├── launch_desktop.py             # Desktop app with live OpenCV feed & HUD
│   ├── requirements.txt              # PyTorch, OpenCV, Ultralytics dependencies
│   ├── .env.example                  # Environment settings template
│   └── START_DESKTOP.bat             # 1-Click desktop launcher
│
├── web-dashboard/                    # Web Dashboard (Cloud-Ready / Local Server)
│   ├── backend/                      # Decoupled FastAPI server (Zero PyTorch/OpenCV)
│   ├── static/                       # UI assets and student uploads
│   ├── web/                          # Tailwind web dashboard (Live Participation Queue)
│   ├── requirements.txt              # Lightweight dependencies (<50MB)
│   ├── run_server.py                 # Server entrypoint (uvicorn backend.app:app)
│   ├── .env.example                  # Environment settings template
│   ├── START_DASHBOARD.bat           # 1-Click dashboard launcher
│   ├── Dockerfile                    # Containerization config
│   └── render.yaml                   # Free-tier cloud deployment config
│
├── START_ALL.bat                     # 1-Click launcher to run both together locally
└── README.md
```

---

## Quick Start (Run Both Locally)

Double-click **`START_ALL.bat`**. This starts:
1. **Web Dashboard** at `http://127.0.0.1:8000` (browser opens automatically).
2. **Desktop App** which connects to your webcam and streams hand raises to the dashboard.

---

## Running Systems Individually

### 1. Web Dashboard
```bash
cd web-dashboard
START_DASHBOARD.bat
# Or manually:
python run_server.py --host 127.0.0.1 --port 8000
```
- Open `http://127.0.0.1:8000/` in your browser.
- Ultra-lean: Zero PyTorch or OpenCV dependencies, deployable directly to cloud platforms (Render, Railway, etc.).

### 2. Desktop App
```bash
cd desktop-app
START_DESKTOP.bat
# Or manually:
python launch_desktop.py --url http://127.0.0.1:8000
```
- Runs on the local classroom computer connected to the physical webcam.
- Displays live video feed with desk boundaries, participant names, and status HUD.
- Keyboard shortcuts:
  - `Q`: Quit
  - `C`: Cycle active section
  - `S`: Sync seats from dashboard
  - `R`: Reload all data from dashboard
