================================================================================
           CLASSTRACK — HOW TO RUN (QUICK START FOR EVERYONE)
================================================================================

ClassTrack is an automated AI classroom recitation and participation system.
You do NOT need to know programming or install Python to use this!
This package includes Camera Node version 1.0.1. No camera key is required.
Close an older running camera app before installing this update.

LOCAL SOURCE PACKAGE LOGIN:
START_ALL.bat and START_DASHBOARD.bat use the local SQLite database.
The installed camera connected to the deployed dashboard uses Supabase through
that dashboard. Local and cloud accounts/classes are separate; records are not
automatically transferred between them.
The localhost camera follows the class selected on the teacher dashboard.
Starting a session loads that session's students and replaces the old roster.
For multiple classroom cameras, assign each camera explicitly to its own section.
Run Source_Code\START_ALL.bat, then open http://127.0.0.1:8000/admin
Administrator ID: admin1
Password: admin1234567
Fresh local databases create this administrator automatically on startup.
Use the administrator page to create teacher accounts. Then sign in with a
teacher account at http://127.0.0.1:8000/ to manage classes and start sessions.

--------------------------------------------------------------------------------
OPTION 1: FOR TEACHERS & EVALUATORS (Zero Installation)
--------------------------------------------------------------------------------
1. Open this website in any browser (Chrome, Safari, Edge, phone, or laptop):
   https://classtrack-mm41.onrender.com

   (Or double-click "Open_ClassTrack_Dashboard.url" in this folder)

2. Sign in with your teacher account and select your section (e.g. BSIT-3).
3. Click "Start Class Session".
4. When students raise hands, they appear in order (1st, 2nd, 3rd) on the podium!
5. Tap "+1 Point" to credit participation or "End Session" to export CSV grades.

--------------------------------------------------------------------------------
OPTION 2: FOR THE CLASSROOM CAMERA PC (1-Click Windows Setup)
--------------------------------------------------------------------------------
1. Locate and run:
   ClassTrack_Setup_v1.0.exe

2. Follow the setup wizard (Click "Next" -> "Install").
   * If Windows displays a blue "Windows protected your PC" alert,
     click "More info", then click "Run anyway".

3. Double-click the new "ClassTrack Camera Node" shortcut on your Desktop.

4. Sign in to the teacher dashboard and select your class. In the camera,
   press C until its section matches your dashboard, then press R to sync.
   Check the seating layout so the tracked desks match your students.

5. Click "Start Class Session" on the dashboard to begin recording hand raises.
   An unselected camera automatically joins when exactly one class is running.
   The first student gets a green box and "HAND RAISED". Other raisers get
   blue boxes and "Name | 2nd Hand Raised", "Name | 3rd Hand Raised", etc.
   Lowered hands show "Standby". Nobody is promoted when another hand lowers;
   the order resets after everyone lowers their hands.

Keyboard Shortcuts inside Camera Window:
- [Q] : Exit / Close Camera
- [C] : Switch / Cycle to Next Section
- [S] : Sync Desk Layouts
- [R] : Refresh Connection
- [F] or [K] : Restart / Re-scan the Camera

--------------------------------------------------------------------------------
FREQUENTLY ASKED QUESTIONS (FAQ)
--------------------------------------------------------------------------------
Q: Does this require Python to be installed?
A: NO! The installer (ClassTrack_Setup_v1.0.exe) is completely self-contained.

Q: Can multiple people access the web dashboard at the same time?
A: YES! Teachers, professors, and assistants can all open the cloud URL at
   https://classtrack-mm41.onrender.com simultaneously.

Q: How do I test it by myself?
A: 1. Sign in to the Web Dashboard, select a section and start a session.
   2. Open the ClassTrack desktop app on your PC.
   3. Select the same section with C and refresh with R. Match your desk layout.
   4. Raise your hand in front of the webcam and hold it until confirmed.
   5. Your box turns green and shows "HAND RAISED" if you are first. Later raisers
      stay blue and show "2nd Hand Raised", "3rd Hand Raised", etc. The order resets
      after everyone lowers. Assigned students join the queue after confirmation.
================================================================================
