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

Teachers now sign in with their **Teacher ID and password** at `/login`. Administrator access is at `/admin` (also available from the welcome screen). There are no default accounts or shared PINs.

From `web-dashboard`, run `python manage_admin.py --database sqlite` to create or reset a local administrator, or `python manage_admin.py --database postgres` for Supabase. The command reads `.env` and prompts for an ID and password without echoing the password. Sign in at `/admin` to add teachers, edit IDs/names/departments, reset passwords, enable/disable accounts, or delete sign-in access. Deleting access ends the active session and retains class history. Passwords require 12–128 characters and are stored only as salted PBKDF2-SHA256 hashes (600,000 iterations). Password/credential changes revoke previous sign-ins.

**Upgrade:** the migration preserves existing account IDs, section ownership and student records, clears publicly exposed PINs, and disables the old key login endpoints. Existing teachers must receive a new ID/password through the administrator panel. Historical duplicate enrollments are preserved for review; new duplicate student IDs (ignoring case and surrounding whitespace) are rejected within each section, including concurrent submissions. Different students may share a name, and the same student ID may appear in different sections.

Set a long random `AUTH_SECRET` in the dashboard environment. Camera connections do not require an API key: roster/session reads, camera controls, calibration geometry and event ingestion are open to camera nodes. Teacher account management, classroom edits and participation awards require teacher authentication. Use HTTPS for hosted access. Password hashing protects credentials; student records and uploaded images are **not application-encrypted at rest**. Protect the database/files with your hosting or disk encryption and restrict backup access.

Each teacher can run one live session independently of other teachers. Each class has its own queue and guest viewing code; ending another teacher's session does not affect it. Guests see only the session for their code, which expires when that session ends. For simultaneous classrooms, run a camera node per classroom and pass `--section-id SECTION_ID` (or select the section in the desktop app). A camera follows its selected section; it never switches to another teacher's class automatically. An unbound camera follows the only running session, or auto-selects a section when only one exists. The earliest observed raiser gets a green box and `HAND RAISED`; other raisers get blue boxes and `Name | 2nd Hand Raised`, `Name | 3rd Hand Raised`, etc. Lowering a hand does not renumber or promote other students. The order resets automatically after everyone lowers their hands. Lowered hands show `Standby` with blue boxes. Exact same-frame ties use a stable seat ID rather than detection order.

Double-click **`START_ALL.bat`**. This starts:
1. **Web Dashboard** at `http://127.0.0.1:8000` (browser opens automatically).
2. **Desktop App** which connects to your webcam and streams hand raises to the dashboard.

Local launchers explicitly use **SQLite** at `web-dashboard/hand_tracking.db`, even if `.env` contains a Supabase `DATABASE_URL`. The deployed dashboard uses **Supabase/PostgreSQL**. The desktop app sends data to its selected dashboard; it never connects to either database directly. Local and cloud accounts/classes are separate; switching modes does not copy or delete records.

`START_ALL.bat` puts the localhost camera in dashboard-following mode. Signing in/selecting a class or starting a session assigns that class to the camera, whose section menu contains only that teacher's classes. Switching assignments clears the previous roster and session before loading the new roster; delayed responses cannot restore the old assignment. Reconnecting restores the current dashboard selection. For separate classroom cameras, use `--section-id SECTION_ID --no-follow-dashboard`; those explicit bindings remain independent. Pressing `C` selects a fixed classroom binding until the camera restarts.

Section creation and student enrollment use a persistent `Idempotency-Key`. The browser disables repeat submissions and retains the key through an uncertain response/reload. The server atomically saves the resource and replay response, scopes keys to the teacher or section, and rejects reuse with different data. Live notifications run after the save response, and the browser uses their roster snapshots directly.

---

## Running Systems Individually

### 1. Web Dashboard
```bash
cd web-dashboard
START_DASHBOARD.bat
# Or manually:
python run_server.py --host 127.0.0.1 --port 8000 --database sqlite
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
python run_server.py --host 127.0.0.1 --port 8000 --database sqlite
```

Open `http://127.0.0.1:8000/admin` in a browser. A fresh local SQLite database automatically creates administrator ID `admin1` with password `admin1234567`. Sign in there to create teacher accounts, then use a teacher account at `http://127.0.0.1:8000/` to manage classes. Existing administrator accounts are preserved. First startup may take a little time while packages install.

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

Open `desktop-app/.env` and set `WEB_DASHBOARD_URL=http://127.0.0.1:8000` for a local dashboard, or the hosted dashboard URL for Render. No camera key is needed. Then run:

```powershell
python launch_desktop.py --url http://127.0.0.1:8000
```

Allow camera access if Windows asks. Press `Q` in the camera window to stop it. The first desktop install downloads large computer vision packages and can take several minutes. If you only need the web dashboard, skip this camera-app step.

### 4. Stop or start it again

Press `Ctrl+C` in each running terminal to stop that process. On later runs, open the same two folders in terminals, activate each `.venv`, then run `python run_server.py --host 127.0.0.1 --port 8000 --database sqlite` and `python launch_desktop.py --url http://127.0.0.1:8000`. You only need to install packages once unless `requirements.txt` changes.

If PowerShell blocks activation, run the environment's Python directly instead: `..\.venv\Scripts\python.exe` from the desktop folder or `.\.venv\Scripts\python.exe` from the dashboard folder, followed by the same pip and run commands. `START_ALL.bat` also creates missing environments and installs dependencies automatically from the project root. If VS Code reports that `py -3.11` is unavailable but Python is installed, replace it with `python -m venv .venv`.

## Regression and browser checks

Install test dependencies with `python -m pip install -r web-dashboard/requirements-test.txt`, then `python -m playwright install chromium`. From the repository root run:

```sh
python -m pytest web-dashboard/tests desktop-app/tests -q
```

Tests use temporary SQLite databases and an isolated browser server. They cover credential administration, password storage and revocation, duplicate enrollment, validation, concurrent classrooms, guest/camera isolation, mobile enrollment, and admin add/edit/delete. Browser screenshots are saved in pytest's temporary `browser-school` directory. Vision-worker tests require the desktop computer-vision dependencies; camera hardware and the deployed PostgreSQL database require separate environment-specific checks.
