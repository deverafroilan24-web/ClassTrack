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

For production, manage administrators directly in **Supabase Table Editor → Admin**; see [Supabase admin setup](web-dashboard/SUPABASE_ADMIN.md). For local development, from `web-dashboard`, run `python manage_admin.py` to create or reset the administrator. The command reads the same `.env` as the server and prompts for an ID, email address and password without echoing the password. Sign in at `/admin` to add teachers, edit IDs/names/departments, reset passwords, enable/disable accounts, or delete sign-in access. Deleting access ends the active session and retains class history. Teacher passwords require 12–128 characters and use salted PBKDF2-SHA256 hashes (600,000 iterations). Admin passwords entered through Supabase use salted bcrypt hashes; see the admin setup guide for length limits. Password/credential changes revoke previous sign-ins.

**Upgrade:** the migration preserves existing account IDs, section ownership and student records, clears publicly exposed PINs, and disables the old key login endpoints. Existing teachers must receive a new ID/password through the administrator panel. Historical duplicate enrollments are preserved for review; new duplicate student IDs (ignoring case and surrounding whitespace) are rejected within each section, including concurrent submissions. Different students may share a name, and the same student ID may appear in different sections.

Set a long random `AUTH_SECRET` in the dashboard environment. Set `EDGE_API_KEY` to the same private value in the dashboard and `desktop-app/.env`; camera access is rejected when this key is unset. Use HTTPS for hosted access. Password hashing protects credentials; student records and uploaded images are **not application-encrypted at rest**. Protect the database/files with your hosting or disk encryption and restrict backup access.

Each teacher can run one live session independently of other teachers. Each class has its own queue and guest viewing code; ending another teacher's session does not affect it. Guests see only the session for their code, which expires when that session ends. For simultaneous classrooms, run a camera node per classroom and pass `--section-id SECTION_ID` (or select the section in the desktop app). A camera follows its selected section; it never switches to another teacher's class automatically. An unbound camera may auto-select only when there is one section.

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

Open `http://127.0.0.1:8000` in a browser. The server creates its local database automatically. Run `python manage_admin.py` from `web-dashboard` to create your administrator, then open `/admin` to add teacher accounts. First startup may take a little time while packages install.

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

## Regression and browser checks

Install test dependencies with `python -m pip install -r web-dashboard/requirements-test.txt`, then `python -m playwright install chromium`. From the repository root run:

```sh
python -m pytest web-dashboard/tests desktop-app/tests -q
```

Tests use temporary SQLite databases and an isolated browser server. They cover credential administration, password storage and revocation, duplicate enrollment, validation, concurrent classrooms, guest/camera isolation, mobile enrollment, and admin add/edit/delete. Browser screenshots are saved in pytest's temporary `browser-school` directory. Vision-worker tests require the desktop computer-vision dependencies; camera hardware and the deployed PostgreSQL database require separate environment-specific checks.
