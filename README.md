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

### Teacher and camera credentials

Set `TEACHER_PIN` and `AUTH_SECRET` in `web-dashboard/.env` or in the Render service environment. `TEACHER_PIN` is the instructor/admin sign-in and must be different from teacher PINs. Teacher names and PINs are stored in the database. The app creates a `teachers` table on startup and migrates the existing sample accounts once for compatibility. Teacher sessions last eight hours and are stored in the browser session only.

To add a teacher directly in Supabase, open `web-dashboard/supabase_teacher_codes.sql` and run its setup statements once in **Supabase Dashboard → SQL Editor**. Then insert each teacher in **Table Editor → teachers → Insert row**, entering `name`, `pin`, and optionally `department`; leave `id` and defaulted fields blank. Or run an insert like:

```sql
INSERT INTO public.teachers (name, pin, department)
VALUES ('Teacher Name', '5678', 'Department');
```

The new PIN can be used to sign in immediately. Do not reuse a PIN. Keep the `teachers` table unavailable to the public Supabase Data API; the setup SQL enables row-level security and revokes `anon` and `authenticated` access. The application connects using its private `DATABASE_URL`.

Set `EDGE_API_KEY` to the same private value in the web dashboard environment and `desktop-app/.env`. Camera event ingestion and the edge WebSocket reject connections without this key. The database creates the `app_settings` and `teachers` tables automatically on startup in SQLite or Supabase PostgreSQL.

The welcome screen offers Teacher Mode and Guest Mode. After starting a section's class session, the teacher can copy its **Guest viewing code** from the Live Class header and share it with that class or projector. Guests enter the code to see only that live session's participation queue. The code expires when the session ends or a different class starts; another section's ledger and seats remain inaccessible. Anyone who receives a valid code can view that session, so share it only with the intended class. Only Teacher Mode can change records, award points, or download reports.

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

---

## Run from VS Code (Source Project)

The `dist` folder contains packaged build output. It does not include the Python source and requirements needed to run or edit the project in VS Code. Download or clone the complete source repository first; the source project must contain `web-dashboard/`, `desktop-app/`, and `README.md`. Missing `.venv`, `__pycache__`, and other generated folders is normal: Python recreates the environment locally.

### 1. Open the source folder

1. Install Python 3.10 or 3.11 and VS Code. During Python setup on Windows, enable **Add Python to PATH**.
2. Clone/download the complete repository, then in VS Code choose **File → Open Folder** and open its root folder (the one containing `web-dashboard` and `desktop-app`).
3. Open a VS Code terminal with **Terminal → New Terminal**.

### 2. Start the web dashboard

In the first terminal:

```powershell
cd web-dashboard
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python run_server.py --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in a browser. The server creates its local database automatically. The default local admin PIN is `1234`; change `TEACHER_PIN` in `web-dashboard/.env` before using the dashboard outside your computer. First startup may take a little time while packages install.

### 3. Start the desktop camera app

Keep the dashboard running. Open a second VS Code terminal:

```powershell
cd desktop-app
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Open `desktop-app/.env` and set `EDGE_API_KEY` to the same private value used in `web-dashboard/.env`. Set `WEB_DASHBOARD_URL=http://127.0.0.1:8000`. Then run:

```powershell
python launch_desktop.py --url http://127.0.0.1:8000
```

Allow camera access if Windows asks. Press `Q` in the camera window to stop it. The first desktop install downloads large computer vision packages and can take several minutes. If you only need the web dashboard, skip this camera-app step.

### 4. Stop or start it again

Press `Ctrl+C` in each running terminal to stop that process. On later runs, open the same two folders in terminals, activate each `.venv`, then run `python run_server.py --host 127.0.0.1 --port 8000` and `python launch_desktop.py --url http://127.0.0.1:8000`. You only need to install packages once unless `requirements.txt` changes.

If PowerShell blocks activation, run the environment's Python directly instead: `..\.venv\Scripts\python.exe` from the desktop folder or `.\.venv\Scripts\python.exe` from the dashboard folder, followed by the same pip and run commands. `START_ALL.bat` also creates missing environments and installs dependencies automatically from the project root. If VS Code reports that `py -3.11` is unavailable but Python is installed, replace it with `python -m venv .venv`.
