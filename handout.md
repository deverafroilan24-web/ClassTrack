# ClassTrack — Project Handout & System Brief

## 1. Executive Summary & Purpose
**ClassTrack** is an automated, AI-powered **Classroom Participation and Recitation Monitoring System**. 

The system solves common classroom challenges:
- **Biased or subjective recitation tracking**: Teachers no longer guess who raised their hand first.
- **Manual scorekeeping overhead**: Points and raises are automatically queued, recorded, and exported into gradebooks.
- **Proxy attendance / Seat swapping**: Identifies students at registered desks and validates participation.
- **Physical limitations**: Allows the teacher to roam the room with a smartphone or tablet, viewing live hand-raise queues and awarding participation points with one tap while the classroom camera runs unattended.

---

## 2. High-Level Architecture & Decoupled Design

ClassTrack uses a **decoupled edge-to-cloud architecture**:

```
+------------------------------------+              +------------------------------------------+
|      LOCAL CLASSROOM COMPUTER      |              |          ONLINE CLOUD SERVICES           |
|                                    |              |                                          |
|  [ Physical Webcam / USB Cam ]     |              |     [ Render Web Service ]               |
|                |                   |              |     - FastAPI Dashboard Backend          |
|                v                   |              |     - Serves Web GUI (HTML/Tailwind/JS)  |
|  [ Desktop Camera Node ]           |              |     - WebSocket Live Broadcasting        |
|  - YOLOv11 Pose Estimation         |              |     - REST API for Desk & Student Admin  |
|  - Arm Angle & Kinematics Engine   |              |                        ^                 |
|  - Anti-Proxy Face Verification    |              |                        |                 |
|  - HUD Video Overlay               |              |                        v                 |
|                |                   |              |     [ Supabase Cloud Database ]          |
|                +==== HTTPS / WSS ==+============> |     - PostgreSQL Tables:                 |
|               (Ingest API Stream)  |              |       sections, students, sessions,      |
|                                    |              |       seats, events                      |
+------------------------------------+              +------------------------------------------+
                                                                     ^
                                                                     | (Realtime Browsing)
                                                                     v
                                                    +------------------------------------------+
                                                    |      ANY DEVICE (TEACHER / PROCTOR)      |
                                                    |  Phone, iPad, Laptop, Smart Board        |
                                                    |  https://classtrack-mm41.onrender.com    |
                                                    +------------------------------------------+
```

### Key Architectural Strengths:
1. **Zero Heavy ML Dependencies in the Cloud**:
   - The cloud web server does **not** install PyTorch, CUDA, Torchvision, or Ultralytics (which often exceed 3 GB and fail on free cloud tiers).
   - The web server is lightweight (<50 MB) and deploys on standard free-tier hosting (Render).
2. **Local Edge Processing**:
   - The computer with the physical webcam runs the machine learning pose estimation locally.
   - It only sends compact JSON event payloads over the network (e.g. `{ "seat_id": "Desk A1", "status": "VALID", "arm_angle": 125.4 }`).
3. **Decoupled Operations**:
   - The teacher can manage sections, enroll students, view attendance, configure seating grids, and export reports even when the camera is turned off.

---

## 3. What Has Happened So Far (Project Evolution)

1. **System Decoupling & Modularization**:
   - Separated the monolithic repository into two independent modules:
     - `desktop-app/`: Dedicated camera node with computer vision, state machines, and HUD.
     - `web-dashboard/`: Pure FastAPI web application with interactive seating charts and recitation podiums.
2. **Supabase Cloud Database Integration**:
   - Shifted from a local file-based SQLite database (`hand_tracking.db`) to **Supabase Hosted PostgreSQL**.
   - Created PostgreSQL schemas for `sections`, `students`, `sessions`, `seats`, and `events`.
   - Built a dual-mode database manager (`backend/database.py`) that uses Supabase PostgreSQL in production while remaining compatible with local SQLite for offline use.
   - Migrated existing classroom records and rosters into Supabase.
3. **Cloud Deployment on Render**:
   - Provisioned and configured Render web service linked directly to the GitHub repository:
     - **GitHub Repo**: [https://github.com/deverafroilan24-web/ClassTrack](https://github.com/deverafroilan24-web/ClassTrack)
     - **Live Cloud Dashboard**: [https://classtrack-mm41.onrender.com](https://classtrack-mm41.onrender.com)
4. **Camera Edge Node Configuration**:
   - Pointed `desktop-app/.env` directly to the live Render cloud URL (`https://classtrack-mm41.onrender.com`).
   - Integrated live connection detection: The web dashboard monitors if the camera node is online and displays a real-time status badge ("Camera Node Connected" vs "No Camera Nodes Connected").
5. **PostgreSQL Compatibility Refinements**:
   - Resolved SQL dialect differences (e.g., PostgreSQL `GROUP BY` strictness, `INSERT ... ON CONFLICT (id) DO UPDATE`).
   - Fixed timestamp datetime serialization to support seamless session starts and stops across devices.

---

## 4. Current State & Operating Procedures

### Where Things Live:
- **Cloud Dashboard URL**: [https://classtrack-mm41.onrender.com](https://classtrack-mm41.onrender.com)
- **Supabase Project**: Singapore Pooler PostgreSQL database storing all classroom data.
- **Git Repository**: [deverafroilan24-web/ClassTrack](https://github.com/deverafroilan24-web/ClassTrack) on `main` branch.

### How to Run in Everyday Practice:

#### A. The Teacher / Instructor (Any Device):
1. Open [https://classtrack-mm41.onrender.com](https://classtrack-mm41.onrender.com) on a phone, tablet, or browser.
2. Select the Section (e.g. `BSIT-3`).
3. Click **"Start Class Session"**.
4. Watch students raise their hands; the podium queue lists them in first-come, first-served order (1st, 2nd, 3rd).
5. Tap **"+1 Point"** to credit participation, or tap **"Dismiss"** for false positives.
6. At the end of class, click **"End Session"** and **"Export CSV"** for grading records.

#### B. The Classroom Camera PC (Local Machine):
1. Ensure the webcam is plugged in.
2. Launch `desktop-app/START_DESKTOP.bat`.
3. The OpenCV camera window will open, track student arm angles, and automatically transmit verified gestures to the live cloud dashboard.
4. When finished, press `Q` on the camera window.

---

## 5. Summary Matrix

| Component | Technology | Role | Location |
|---|---|---|---|
| **Vision Engine** | Python, YOLOv11 Pose, OpenCV | Hand raise kinematics & student detection | Local PC (`desktop-app/`) |
| **Web Server** | FastAPI, Uvicorn, WebSockets | API, event routing, real-time broadcasts | Render Cloud (`web-dashboard/`) |
| **Frontend** | Vanilla JS, HTML5, Tailwind CSS | Interactive seating charts, live queues, reports | Browser (`web/`) |
| **Database** | Supabase (PostgreSQL 15) | Persistent storage for rosters, sessions, and logs | Cloud (AWS Singapore) |
| **Codebase** | Git, GitHub | Version control and auto-deploy pipeline | `deverafroilan24-web/ClassTrack` |
