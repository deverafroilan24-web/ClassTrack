// ClassTrack Client Application — Cloud Dashboard Edition
// (Video feed removed — gesture events arrive from Camera Nodes via WebSocket)


// Header elements
const sectionSelect = document.getElementById("section-select");
const sessionStatusLabel = document.getElementById("session-status-label");
const btnStartSession = document.getElementById("btn-start-session");
const btnStopSession = document.getElementById("btn-stop-session");
const headerClock = document.getElementById("header-clock");
const currentSectionRoomLabel = document.getElementById("current-section-room-label");

// Tables & Grids
const recitationTbody = document.getElementById("recitation-tbody");
const sectionRosterTbody = document.getElementById("section-roster-tbody");
const classroomDesksGrid = document.getElementById("classroom-desks-grid");
const sectionsCardsGrid = document.getElementById("sections-cards-grid");
const reportsTbody = document.getElementById("reports-tbody");
const podiumBannerText = document.getElementById("podium-banner-text");

// Calibration
const calibSeatSelect = document.getElementById("calib-seat-select");
const calibXmin = document.getElementById("calib-xmin");
const calibYmin = document.getElementById("calib-ymin");
const calibXmax = document.getElementById("calib-xmax");
const calibYmax = document.getElementById("calib-ymax");
const btnSaveCalibration = document.getElementById("btn-save-calibration");

// App State
let sectionsList = [];
let currentSectionId = "";
let currentReportsSectionId = "";
let currentSeats = [];
let currentStudents = [];
let seatingViewMode = "seating"; // "seating" | "heatmap"
let currentHeatmapData = null;
let currentEnrollPhotoBase64 = null;
let enrollMediaStream = null;
let draggedStudentId = null;
let draggedFromSeatId = null;
let activeSession = null;
let currentLedger = [];
let currentPodiumList = [];
let teacherToken = sessionStorage.getItem("classtrack.teacherToken") || "";
let teacherName = sessionStorage.getItem("classtrack.teacherName") || "";
let teacherId = sessionStorage.getItem("classtrack.teacherId") || "";
let isAdmin = false;
let guestToken = sessionStorage.getItem("classtrack.guestToken") || "";
let guestAccessCode = "";
let guestAccessSessionId = "";
let role = "guest";
let authGeneration = 0;
let seatDataRequestId = 0;
let ledgerRequestId = 0;
let liveSessionRequestId = 0;
let seatingMutationInProgress = false;
let sessionMutationInProgress = false;
const manualAwardsInProgress = new Set();
let seatDataLoadingFor = "";
let eventsSocket = null;
let socketReconnectTimer = null;

const originalFetch = window.fetch.bind(window);
window.fetch = function (input, options = {}) {
  const url = typeof input === "string" ? input : input.url;
  if (!url.startsWith("/api/") || (!teacherToken && !guestToken)) return originalFetch(input, options);
  const headers = new Headers(options.headers || {});
  if (teacherToken) headers.set("X-Teacher-Token", teacherToken);
  else headers.set("X-Guest-Token", guestToken);
  const requestToken = teacherToken || guestToken;
  return originalFetch(input, { ...options, headers }).then((response) => {
    if (response.status === 401 && requestToken === (teacherToken || guestToken)) {
      enterGuestMode();
      showToast("Your access expired. Enter the viewing code or teacher PIN again.", "warning");
    }
    return response;
  });
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function applyRole(nextRole) {
  role = nextRole;
  document.body.classList.toggle("guest-mode", role === "guest");
  document.getElementById("role-controls").classList.remove("hidden");
  document.getElementById("role-badge").textContent = role === "teacher" ? (teacherName ? `Teacher: ${teacherName}` : "Teacher Mode") : "Guest View-Only Mode";
  document.getElementById("btn-switch-teacher").classList.toggle("hidden", role === "teacher");
  document.getElementById("btn-lock-teacher").classList.toggle("hidden", role !== "teacher");
  document.getElementById("nav-teachers")?.classList.toggle("hidden", role !== "teacher" || !isAdmin);
  if (role === "guest") showView("class");
  updateSessionUI();
  renderRecitationLedger();
  renderClassroomDesks();
  renderGuestQueue();
}

window.showWelcomeChoices = function () {
  document.getElementById("welcome-portal").classList.remove("hidden");
  document.getElementById("welcome-choices").classList.remove("hidden");
  document.getElementById("teacher-login-form").classList.add("hidden");
  document.getElementById("guest-login-form").classList.add("hidden");
  document.getElementById("teacher-pin-input").value = "";
  document.getElementById("guest-code-input").value = "";
  document.getElementById("teacher-login-error").classList.add("hidden");
  document.getElementById("guest-login-error").classList.add("hidden");
};

window.showTeacherLogin = function () {
  document.getElementById("welcome-portal").classList.remove("hidden");
  document.getElementById("welcome-choices").classList.add("hidden");
  document.getElementById("teacher-login-form").classList.remove("hidden");
  document.getElementById("guest-login-form").classList.add("hidden");
  document.getElementById("teacher-login-error").classList.add("hidden");
  document.getElementById("teacher-pin-input").focus();
  loadAvailableTeacherKeys();
};

async function loadAvailableTeacherKeys() {
  const list = document.getElementById("available-teacher-keys");
  if (!list) return;
  list.innerHTML = '<span class="col-span-2 text-slate-500">Loading teacher keys…</span>';
  try {
    const response = await originalFetch("/api/auth/teacher-keys", { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load teacher keys");
    const teachers = await response.json();
    list.innerHTML = teachers.length
      ? teachers.map((teacher) => `<span>• ${escapeHtml(teacher.pin)} : ${escapeHtml(teacher.name)}</span>`).join("")
      : '<span class="col-span-2 text-slate-500">No teacher keys have been added.</span>';
  } catch (error) {
    list.innerHTML = '<span class="col-span-2 text-rose-700">Could not load teacher keys. Check the server connection.</span>';
  }
}

window.showGuestLogin = function () {
  document.getElementById("welcome-portal").classList.remove("hidden");
  document.getElementById("welcome-choices").classList.add("hidden");
  document.getElementById("teacher-login-form").classList.add("hidden");
  document.getElementById("guest-login-form").classList.remove("hidden");
  document.getElementById("guest-login-error").classList.add("hidden");
  document.getElementById("guest-code-input").focus();
};

window.enterGuestMode = function () {
  authGeneration += 1;
  teacherToken = "";
  teacherId = "";
  isAdmin = false;
  guestToken = "";
  guestAccessCode = "";
  guestAccessSessionId = "";
  sessionStorage.removeItem("classtrack.teacherToken");
  sessionStorage.removeItem("classtrack.teacherId");
  sessionStorage.removeItem("classtrack.teacherName");
  sessionStorage.removeItem("classtrack.guestToken");
  if (socketReconnectTimer) clearTimeout(socketReconnectTimer);
  if (eventsSocket) { eventsSocket.onclose = null; eventsSocket.close(); eventsSocket = null; }
  sectionsList = [];
  currentSectionId = "";
  activeSession = null;
  currentSeats = [];
  currentStudents = [];
  currentLedger = [];
  currentHeatmapData = null;
  seatingViewMode = "seating";
  sessionsHistoryList = [];
  ["recitation-tbody", "section-roster-tbody", "sessions-history-tbody", "reports-tbody", "modal-session-tbody"]
    .forEach((id) => { const element = document.getElementById(id); if (element) element.innerHTML = ""; });
  ["modal-register-student", "modal-new-section", "modal-session-details", "modal-seating-settings"]
    .forEach((id) => document.getElementById(id)?.classList.add("hidden"));
  applyRole("guest");
  showWelcomeChoices();
};

window.lockTeacherMode = function () {
  enterGuestMode();
  showToast("Teacher controls locked", "info");
};

window.submitTeacherLogin = async function (event) {
  event.preventDefault();
  const pinInput = document.getElementById("teacher-pin-input");
  const error = document.getElementById("teacher-login-error");
  const submit = document.getElementById("teacher-login-submit");
  submit.disabled = true;
  error.classList.add("hidden");
  try {
    const response = await originalFetch("/api/auth/verify-pin", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin: pinInput.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Teacher login failed");
    teacherToken = data.token;
    teacherName = data.teacher_name || "";
    teacherId = data.teacher_id || "";
    isAdmin = !!data.is_admin;
    guestToken = "";
    authGeneration += 1;
    sessionStorage.setItem("classtrack.teacherToken", teacherToken);
    sessionStorage.setItem("classtrack.teacherName", teacherName);
    sessionStorage.setItem("classtrack.teacherId", teacherId);
    sessionStorage.removeItem("classtrack.guestToken");
    pinInput.value = "";
    document.getElementById("welcome-portal").classList.add("hidden");
    applyRole("teacher");
    await loadAuthorizedData();
    initEventsWebSocket();
  } catch (loginError) {
    error.textContent = loginError.message;
    error.classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
};

window.submitGuestLogin = async function (event) {
  event.preventDefault();
  const codeInput = document.getElementById("guest-code-input");
  const error = document.getElementById("guest-login-error");
  const submit = document.getElementById("guest-login-submit");
  submit.disabled = true;
  error.classList.add("hidden");
  try {
    const response = await originalFetch("/api/auth/guest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: codeInput.value.trim() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Unable to open live queue");
    guestToken = data.token;
    teacherToken = "";
    authGeneration += 1;
    sessionStorage.setItem("classtrack.guestToken", guestToken);
    sessionStorage.removeItem("classtrack.teacherToken");
    currentSectionId = data.section_id;
    codeInput.value = "";
    document.getElementById("welcome-portal").classList.add("hidden");
    applyRole("guest");
    await loadAuthorizedData();
    initEventsWebSocket();
  } catch (loginError) {
    error.textContent = loginError.message;
    error.classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
};

async function loadAuthorizedData() {
  const activeResponse = await fetch("/api/sessions/active");
  activeSession = activeResponse.ok ? await activeResponse.json() : null;
  if (activeSession?.section_id) currentSectionId = activeSession.section_id;
  await fetchSections();
  await Promise.all(role === "teacher" ? [fetchSeats(), fetchRecitationLedger()] : [fetchRecitationLedger()]);
  updateSessionUI();
  if (role === "teacher") fetchSessionsHistory();
}

async function restoreAuth() {
  if (teacherToken) {
    try {
      const response = await originalFetch("/api/auth/session", { headers: { "X-Teacher-Token": teacherToken } });
      if (response.ok) {
        const sessionData = await response.json();
        teacherName = sessionData.teacher_name || teacherName;
        teacherId = sessionData.teacher_id || teacherId;
        isAdmin = !!sessionData.is_admin;
        sessionStorage.setItem("classtrack.teacherName", teacherName);
        sessionStorage.setItem("classtrack.teacherId", teacherId);
        document.getElementById("welcome-portal").classList.add("hidden");
        applyRole("teacher");
        return true;
      }
    } catch (error) {
      console.warn("Teacher session validation failed", error);
    }
    teacherToken = "";
    teacherId = "";
    isAdmin = false;
    sessionStorage.removeItem("classtrack.teacherToken");
    sessionStorage.removeItem("classtrack.teacherId");
    sessionStorage.removeItem("classtrack.teacherName");
  }
  if (guestToken) {
    try {
      const response = await originalFetch("/api/auth/guest-session", { headers: { "X-Guest-Token": guestToken } });
      if (response.ok) {
        currentSectionId = (await response.json()).section_id;
        document.getElementById("welcome-portal").classList.add("hidden");
        applyRole("guest");
        return true;
      }
    } catch (error) {
      console.warn("Guest session validation failed", error);
    }
    guestToken = "";
    sessionStorage.removeItem("classtrack.guestToken");
  }
  applyRole("guest");
  showWelcomeChoices();
  return false;
}

// ============================================================================
// STUDENT INITIALS & AVATAR PALETTE
// ============================================================================
function getStudentInitials(name) {
  if (!name) return "?";
  const clean = name.replace(/\([^)]*\)/g, "").trim();
  const parts = clean.split(/[\s,]+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].substring(0, 2).toUpperCase();
  return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
}

function getStudentAvatarColor(name) {
  const palettes = [
    { bg: "bg-blue-100", text: "text-blue-800", border: "border-blue-300" },
    { bg: "bg-indigo-100", text: "text-indigo-800", border: "border-indigo-300" },
    { bg: "bg-violet-100", text: "text-violet-800", border: "border-violet-300" },
    { bg: "bg-emerald-100", text: "text-emerald-800", border: "border-emerald-300" },
    { bg: "bg-teal-100", text: "text-teal-800", border: "border-teal-300" },
    { bg: "bg-cyan-100", text: "text-cyan-800", border: "border-cyan-300" },
    { bg: "bg-amber-100", text: "text-amber-800", border: "border-amber-300" },
    { bg: "bg-rose-100", text: "text-rose-800", border: "border-rose-300" },
  ];
  let hash = 0;
  for (let i = 0; i < (name || "").length; i++) {
    hash = (hash << 5) - hash + name.charCodeAt(i);
    hash |= 0;
  }
  return palettes[Math.abs(hash) % palettes.length];
}

// ============================================================================
// 0. CUSTOM POPUPS & TOAST SYSTEM (Replaces browser alert/confirm)
// ============================================================================
window.showToast = function (message, type = "info", durationMs = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `pointer-events-auto flex items-center space-x-2.5 px-4 py-2.5 rounded-xl shadow-lg border text-xs font-semibold transform transition-all duration-300 translate-y-2 opacity-0 max-w-sm ${
    type === "success"
      ? "bg-emerald-800 text-white border-emerald-700 shadow-emerald-900/20"
      : type === "error"
      ? "bg-rose-800 text-white border-rose-700 shadow-rose-900/20"
      : type === "warning"
      ? "bg-amber-700 text-white border-amber-600 shadow-amber-900/20"
      : "bg-slate-900 text-white border-slate-800 shadow-slate-900/20"
  }`;

  let iconSvg = "";
  if (type === "success") {
    iconSvg = `<svg class="w-4 h-4 shrink-0 text-emerald-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>`;
  } else if (type === "error") {
    iconSvg = `<svg class="w-4 h-4 shrink-0 text-rose-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>`;
  } else if (type === "warning") {
    iconSvg = `<svg class="w-4 h-4 shrink-0 text-amber-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>`;
  } else {
    iconSvg = `<svg class="w-4 h-4 shrink-0 text-brand-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`;
  }

  toast.innerHTML = `
    ${iconSvg}
    <span class="flex-1">${message}</span>
    <button class="text-white/60 hover:text-white ml-2 text-base leading-none">&times;</button>
  `;

  const closeBtn = toast.querySelector("button");
  const dismiss = () => {
    toast.classList.add("opacity-0", "translate-y-2");
    setTimeout(() => toast.remove(), 300);
  };
  closeBtn.onclick = dismiss;

  container.appendChild(toast);
  requestAnimationFrame(() => {
    toast.classList.remove("opacity-0", "translate-y-2");
  });

  setTimeout(dismiss, durationMs);
};

window.showConfirmModal = function ({
  title = "Confirm Action",
  message = "Are you sure you want to proceed?",
  confirmText = "Confirm",
  cancelText = "Cancel",
  isDanger = true,
}) {
  return new Promise((resolve) => {
    const modal = document.getElementById("modal-confirm");
    if (!modal) {
      resolve(true);
      return;
    }
    const titleEl = document.getElementById("modal-confirm-title");
    const msgEl = document.getElementById("modal-confirm-message");
    const okBtn = document.getElementById("modal-confirm-ok-btn");
    const cancelBtn = document.getElementById("modal-confirm-cancel-btn");
    const iconBg = document.getElementById("modal-confirm-icon-bg");

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    if (okBtn) {
      okBtn.textContent = confirmText;
      okBtn.className = isDanger
        ? "text-xs font-semibold text-white bg-rose-600 hover:bg-rose-700 px-3.5 py-1.5 rounded-lg shadow-xs transition"
        : "text-xs font-semibold text-white bg-brand-700 hover:bg-brand-800 px-3.5 py-1.5 rounded-lg shadow-xs transition";
    }
    if (cancelBtn) cancelBtn.textContent = cancelText;
    if (iconBg) {
      iconBg.className = isDanger
        ? "w-10 h-10 rounded-full bg-rose-100 text-rose-600 flex items-center justify-center shrink-0"
        : "w-10 h-10 rounded-full bg-brand-100 text-brand-700 flex items-center justify-center shrink-0";
    }

    const cleanup = () => {
      modal.classList.add("hidden");
      okBtn.onclick = null;
      cancelBtn.onclick = null;
    };

    okBtn.onclick = () => {
      cleanup();
      resolve(true);
    };

    cancelBtn.onclick = () => {
      cleanup();
      resolve(false);
    };

    modal.classList.remove("hidden");
  });
};

// ============================================================================
// 1. NAVIGATION CONTROLLER
// ============================================================================
window.showView = function (viewName) {
  if (role === "guest" && viewName !== "class") viewName = "class";
  document.querySelectorAll(".nav-tab").forEach((tab) => {
    const isActive = tab.dataset.view === viewName;
    tab.setAttribute("aria-current", isActive ? "page" : "false");
    tab.classList.toggle("bg-brand-50", isActive);
    tab.classList.toggle("text-brand-800", isActive);
    tab.classList.toggle("font-semibold", isActive);
    tab.classList.toggle("text-slate-600", !isActive);
    tab.classList.toggle("font-medium", !isActive);
  });

  document.querySelectorAll(".view-panel").forEach((panel) => {
    panel.classList.add("hidden");
    panel.style.display = "none";
  });

  const target = document.getElementById(`view-${viewName}`);
  if (target) {
    target.classList.remove("hidden");
    target.style.display = "block";
  }

  if ((viewName === "class" || viewName === "recitation") && (teacherToken || guestToken)) {
    fetchRecitationLedger();
  } else if (viewName === "sections") {
    renderSectionsCards();
    renderSectionRoster();
  } else if (viewName === "seating") {
    renderClassroomDesks();
  } else if (viewName === "history") {
    fetchSessionsHistory();
  } else if (viewName === "reports") {
    renderClassReports();
  } else if (viewName === "camera") {
    fetchCameraSettings();
  } else if (viewName === "teachers" && isAdmin) {
    fetchTeachers();
  }
};

async function fetchTeachers() {
  const response = await fetch("/api/admin/teachers");
  if (!response.ok) throw new Error("Could not load teacher accounts");
  const teachers = await response.json();
  const tbody = document.getElementById("teachers-table-body");
  tbody.innerHTML = teachers.map((teacher) => `<tr>
    <td class="px-4 py-3 font-semibold text-slate-800">${escapeHtml(teacher.name)}</td>
    <td class="px-4 py-3 text-slate-600">${escapeHtml(teacher.department || "—")}</td>
    <td class="px-4 py-3"><span class="px-2 py-1 rounded-full text-xs ${teacher.is_active ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"}">${teacher.is_active ? "Active" : "Inactive"}</span></td>
    <td class="px-4 py-3 text-right">${teacher.is_active ? `<button onclick="deactivateTeacher('${escapeHtml(teacher.id)}')" class="text-rose-700 hover:underline font-semibold">Deactivate</button>` : ""}</td>
  </tr>`).join("") || '<tr><td colspan="4" class="px-4 py-6 text-center text-slate-500">No teacher accounts yet.</td></tr>';
}

async function reconcileLiveSession() {
  if (!teacherToken && !guestToken) return;
  const generation = authGeneration;
  const requestId = ++liveSessionRequestId;
  const previousSessionId = activeSession?.id || "";
  try {
    const response = await fetch("/api/sessions/active", { cache: "no-store" });
    if (!response.ok || generation !== authGeneration || requestId !== liveSessionRequestId) return;
    const session = await response.json();
    if (generation !== authGeneration || requestId !== liveSessionRequestId ||
        previousSessionId !== (activeSession?.id || "")) return;
    if (role === "guest" && !session) {
      enterGuestMode();
      showToast("Viewing code expired. Ask your teacher for the current code.", "warning");
      return;
    }
    const changed = (session?.id || "") !== (activeSession?.id || "");
    if (changed) {
      activeSession = session;
      if (session?.section_id && role === "teacher") {
        currentSectionId = session.section_id;
        populateSectionDropdown();
        updateSectionUI();
        await fetchSeats();
      }
      updateSessionUI();
    }
    await fetchRecitationLedger();
  } catch (error) {
    console.warn("Live session refresh failed", error);
  }
}

function showTeacherAdminMessage(message, isError = false) {
  const element = document.getElementById("teacher-admin-message");
  element.textContent = message;
  element.className = `text-sm rounded-lg px-3 py-2 ${isError ? "bg-rose-50 text-rose-700" : "bg-emerald-50 text-emerald-700"}`;
}

window.createTeacher = async function (event) {
  event.preventDefault();
  const button = document.getElementById("teacher-create-submit");
  button.disabled = true;
  try {
    const response = await fetch("/api/admin/teachers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: document.getElementById("new-teacher-name").value.trim(),
        department: document.getElementById("new-teacher-department").value.trim(),
        pin: document.getElementById("new-teacher-pin").value.trim(),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not add teacher");
    document.getElementById("teacher-create-form").reset();
    showTeacherAdminMessage("Teacher account added.");
    await fetchTeachers();
  } catch (error) {
    showTeacherAdminMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
};

window.deactivateTeacher = async function (teacherIdToDeactivate) {
  if (!window.confirm("Deactivate this teacher account? Their sections and session history will remain in the database.")) return;
  try {
    const response = await fetch(`/api/admin/teachers/${encodeURIComponent(teacherIdToDeactivate)}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not deactivate teacher");
    showTeacherAdminMessage("Teacher account deactivated. Their current sign-in is now invalid.");
    await fetchTeachers();
  } catch (error) {
    showTeacherAdminMessage(error.message, true);
  }
};

function labelResponsiveCells(row, labels) {
  row.classList.add("responsive-row");
  row.querySelectorAll("td").forEach((cell, index) => {
    cell.dataset.label = labels[index] || "";
  });
}

function renderGuestQueue() {
  const list = document.getElementById("guest-queue-list");
  if (!list) return;
  const raised = currentLedger
    .filter((student) => student.latest_status === "VALID" && student.latest_queue_pos)
    .sort((a, b) => a.latest_queue_pos - b.latest_queue_pos);
  list.innerHTML = raised.length
    ? raised.map((student) => `
      <div class="guest-queue-row">
        <span class="guest-queue-rank">${Number(student.latest_queue_pos)}</span>
        <span class="guest-queue-person"><strong>${escapeHtml(student.student_name)}</strong><small>${escapeHtml(student.label)}</small></span>
        <span class="guest-queue-time" data-raised-at="${Number(student.raised_at_ms) || 0}">0:00</span>
      </div>`).join("")
    : `<div class="guest-queue-empty"><span class="guest-empty-icon">✦</span><strong>Queue is clear</strong><p>Raised hands will appear here as the class progresses.</p></div>`;
  updateGuestTimers();
}

function updateGuestTimers() {
  document.querySelectorAll("#guest-queue-list [data-raised-at]").forEach((element) => {
    const raisedAt = Number(element.dataset.raisedAt);
    const seconds = raisedAt ? Math.max(0, Math.floor((Date.now() - raisedAt) / 1000)) : 0;
    element.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  });
}
setInterval(updateGuestTimers, 1000);

// Clock
function startClock() {
  function update() {
    const d = new Date();
    headerClock.textContent = d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }
  update();
  setInterval(update, 1000);
}

// ============================================================================
// 2. SECTIONS & STUDENT MANAGEMENT
// ============================================================================
async function fetchSections() {
  try {
    const res = await fetch("/api/sections");
    if (!res.ok) throw new Error(`Section request failed (${res.status})`);
    sectionsList = await res.json();
    if (!sectionsList.find((s) => s.id === currentSectionId)) {
      currentSectionId = sectionsList.length > 0 ? sectionsList[0].id : "";
      currentReportsSectionId = currentSectionId;
    }
    populateSectionDropdown();
    renderSectionsCards();
  } catch (err) {
    console.error("Failed to load sections:", err);
  }
}

function populateSectionDropdown() {
  if (sectionSelect) {
    sectionSelect.innerHTML = "";
    if (sectionsList.length === 0) {
      sectionSelect.innerHTML = `<option value="">No Sections Available</option>`;
    } else {
      sectionsList.forEach((sec) => {
        const opt = document.createElement("option");
        opt.value = sec.id;
        opt.textContent = `${sec.name} (${sec.subject})`;
        if (sec.id === currentSectionId) opt.selected = true;
        sectionSelect.appendChild(opt);
      });
    }
  }

  const seatingSectionSelect = document.getElementById("seating-section-select");
  if (seatingSectionSelect) {
    seatingSectionSelect.innerHTML = "";
    if (sectionsList.length === 0) {
      seatingSectionSelect.innerHTML = `<option value="">No Sections Available</option>`;
    } else {
      sectionsList.forEach((sec) => {
        const opt = document.createElement("option");
        opt.value = sec.id;
        opt.textContent = `${sec.name} (${sec.subject})`;
        if (sec.id === currentSectionId) opt.selected = true;
        seatingSectionSelect.appendChild(opt);
      });
    }
  }

  const historyFilter = document.getElementById("history-section-filter");
  if (historyFilter) {
    const prev = historyFilter.value;
    historyFilter.innerHTML = `<option value="">All Classes</option>`;
    sectionsList.forEach((sec) => {
      const opt = document.createElement("option");
      opt.value = sec.id;
      opt.textContent = `${sec.name} (${sec.subject})`;
      if (sec.id === prev) opt.selected = true;
      historyFilter.appendChild(opt);
    });
  }

  const reportsFilter = document.getElementById("reports-section-select");
  if (reportsFilter) {
    const prev = reportsFilter.value || currentReportsSectionId;
    reportsFilter.innerHTML = "";
    if (sectionsList.length === 0) {
      reportsFilter.innerHTML = `<option value="">No Sections Available</option>`;
    } else {
      sectionsList.forEach((sec) => {
        const opt = document.createElement("option");
        opt.value = sec.id;
        opt.textContent = `${sec.name} (${sec.subject})`;
        if (sec.id === prev) opt.selected = true;
        reportsFilter.appendChild(opt);
      });
    }
  }

  updateSectionUI();
}

if (sectionSelect) {
  sectionSelect.addEventListener("change", async (e) => {
    await selectClassSection(e.target.value);
  });
}

window.onSeatingSectionChange = async function (e) {
  await selectClassSection(e.target.value);
};

async function selectClassSection(sectionId) {
  if (sectionId === currentSectionId) {
    await Promise.all([fetchSeats(), fetchRecitationLedger()]);
    return;
  }
  currentSectionId = sectionId;
  currentReportsSectionId = sectionId;
  currentSeats = [];
  currentStudents = [];
  currentLedger = [];
  if (sectionSelect) sectionSelect.value = sectionId;
  const seatingSectionSelect = document.getElementById("seating-section-select");
  if (seatingSectionSelect) seatingSectionSelect.value = sectionId;
  const reportsFilter = document.getElementById("reports-section-select");
  if (reportsFilter) reportsFilter.value = sectionId;
  updateSectionUI();
  renderSectionsCards();
  renderClassroomDesks();
  renderSectionRoster();
  renderRecitationLedger();
  await Promise.all([fetchSeats(), fetchRecitationLedger()]);
}

window.onReportsSectionChange = function (e) {
  currentReportsSectionId = e.target.value;
  renderClassReports();
};

function updateSectionUI() {
  const current = sectionsList.find((s) => s.id === currentSectionId);
  const liveSection = sectionsList.find((s) => s.id === (activeSession?.section_id || currentSectionId));
  if (currentSectionRoomLabel) currentSectionRoomLabel.textContent = liveSection
    ? (liveSection.room ? `${liveSection.name} • ${liveSection.room}` : liveSection.name)
    : "No Section Selected";
  if (current) {
    const repTitle = document.getElementById("reports-class-title");
    if (repTitle) {
      repTitle.textContent = `${current.name} — Class Participation Report`;
    }
  } else {
    const repTitle = document.getElementById("reports-class-title");
    if (repTitle) {
      repTitle.textContent = "No Section Selected — Class Participation Report";
    }
  }
}

function renderSectionsCards() {
  if (!sectionsCardsGrid) return;
  sectionsCardsGrid.innerHTML = "";
  if (sectionsList.length === 0) {
    sectionsCardsGrid.innerHTML = `
      <div class="col-span-full p-8 text-center bg-slate-50 border-2 border-dashed border-slate-300 rounded-xl space-y-2">
        <h4 class="text-sm font-bold text-slate-700">No Class Sections Yet</h4>
        <p class="text-xs text-slate-500">Click "+ Create Section" to add your first course section.</p>
      </div>
    `;
    return;
  }
  sectionsList.forEach((sec) => {
    const isCurrent = sec.id === currentSectionId;
    const card = document.createElement("div");
    card.className = `p-3 rounded-xl border transition cursor-pointer relative group flex flex-col justify-between items-center text-center select-none shadow-xs ${
      isCurrent
        ? "border-brand-700 bg-brand-50/60 ring-2 ring-brand-700 shadow-sm"
        : "border-slate-200 bg-white hover:border-brand-300 hover:shadow-sm"
    }`;
    card.onclick = (e) => {
      if (e.target.closest(".btn-delete-section")) return;
      selectClassSection(sec.id);
    };
    card.innerHTML = `
      <!-- Top Row: Delete action on hover & Active badge -->
      <div class="w-full flex justify-between items-center mb-1">
        <span class="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
          isCurrent ? "bg-brand-700 text-white" : "bg-slate-100 text-slate-500"
        }">
          ${isCurrent ? "Active" : "Select"}
        </span>
        <button onclick="deleteSection('${sec.id}', '${sec.name}')" title="Delete section" class="btn-delete-section text-slate-300 hover:text-rose-600 hover:bg-rose-50 rounded p-1 transition opacity-60 group-hover:opacity-100">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
        </button>
      </div>

      <!-- Center: Section Title Prominent & Centered -->
      <div class="py-1">
        <h4 class="text-base font-extrabold text-slate-900 tracking-tight leading-snug">${sec.name}</h4>
        <p class="text-xs text-slate-500 font-medium truncate max-w-[140px] mx-auto mt-0.5">${sec.subject}</p>
      </div>

      <!-- Bottom: Compact Student Count -->
      <div class="w-full pt-2 mt-1 border-t border-slate-100 flex justify-center text-[11px] font-semibold text-slate-400">
        <span>${sec.student_count || 0} Students</span>
      </div>
    `;
    sectionsCardsGrid.appendChild(card);
  });
}

window.deleteSection = async function (sectionId, sectionName) {
  const confirmed = await showConfirmModal({
    title: "Delete Section",
    message: `Are you sure you want to delete "${sectionName}"? All desks and students registered in this section will be removed.`,
    confirmText: "Delete",
    cancelText: "Cancel",
    isDanger: true,
  });
  if (!confirmed) return;

  try {
    const res = await fetch(`/api/sections/${sectionId}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Failed to delete section");
    showToast(`Section "${sectionName}" deleted`, "info");
    await fetchSections();
    await fetchSeats();
    fetchRecitationLedger();
  } catch (err) {
    showToast(err.message, "error");
  }
};

function renderSectionRoster() {
  const container = document.getElementById("section-roster-container");
  const rosterHeader = document.getElementById("section-roster-header");
  const btnAddStudent = document.getElementById("btn-open-register-student");

  if (!sectionsList || sectionsList.length === 0 || !currentSectionId) {
    if (container) container.classList.add("hidden");
    return;
  }

  if (container) container.classList.remove("hidden");

  const activeSec = sectionsList.find((s) => s.id === currentSectionId);
  if (rosterHeader && activeSec) {
    rosterHeader.textContent = `${activeSec.name} — Enrolled Students (${activeSec.subject})`;
  }

  if (!sectionRosterTbody) return;
  sectionRosterTbody.innerHTML = "";
  const countEl = document.getElementById("section-roster-count");
  if (countEl) countEl.textContent = `${currentStudents.length} Students`;

  if (currentStudents.length === 0) {
    sectionRosterTbody.innerHTML = `
      <tr>
        <td colspan="6" class="px-4 py-8 text-center bg-slate-50/50">
          <p class="text-xs text-slate-400 font-medium">No students registered in ${activeSec ? activeSec.name : "this section"}. Use the "+ Add Student to Section" button above to add students.</p>
        </td>
      </tr>
    `;
    return;
  }

  currentStudents.forEach((stud) => {
    const assignedSeat = currentSeats.find((s) =>
      (s.student_id && s.student_id === stud.id) ||
      (s.student_name && !s.student_name.startsWith("Empty (") && s.student_name.trim().toLowerCase() === (stud.name || "").trim().toLowerCase())
    );
    const isPresent = assignedSeat ? Boolean(assignedSeat.is_present) : false;
    const seatLabel = assignedSeat ? assignedSeat.label : (stud.assigned_seat_label && stud.assigned_seat_label !== "Unassigned" ? stud.assigned_seat_label : null);
    const initials = getStudentInitials(stud.name);
    const color = getStudentAvatarColor(stud.name);

    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-50";
    tr.innerHTML = `
      <td class="px-4 py-3 font-mono text-xs text-slate-600">${stud.student_id_number || stud.id}</td>
      <td class="px-4 py-3 font-semibold text-slate-900">
        <div class="flex items-center space-x-2">
          <div class="w-6 h-6 rounded-full ${color.bg} ${color.text} font-bold text-[10px] flex items-center justify-center shrink-0 border ${color.border}">
            ${initials}
          </div>
          <span>${stud.name}</span>
        </div>
      </td>
      <td class="px-4 py-3 text-xs text-slate-600 font-medium">
        ${seatLabel ? `<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-blue-50 text-blue-800 border border-blue-200">${seatLabel}</span>` : '<span class="text-amber-600 italic font-normal">Unassigned (Pool)</span>'}
      </td>
      <td class="px-4 py-3">
        <span class="px-2 py-0.5 rounded text-xs font-semibold ${
          seatLabel
            ? isPresent ? "bg-emerald-100 text-emerald-800" : "bg-rose-100 text-rose-800"
            : "bg-slate-100 text-slate-500"
        }">
          ${seatLabel ? (isPresent ? "Present" : "Absent") : "N/A"}
        </span>
      </td>
      <td class="px-4 py-3 text-center font-bold text-slate-800">${stud.total_points || 0}</td>
      <td class="px-4 py-3 text-right">
        <div class="flex items-center justify-end space-x-1">
          ${assignedSeat ? `
            <button onclick="unassignSeat('${assignedSeat.id}')" title="Return to Student Pool" class="p-1 text-slate-400 hover:text-amber-600 hover:bg-amber-50 rounded transition">
              <svg class="w-3.5 h-3.5 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6"/></svg>
            </button>
          ` : ""}
          <button onclick="deleteStudent('${stud.id}', '${stud.name}')" title="Delete Student" class="p-1 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded transition">
            <svg class="w-3.5 h-3.5 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
          </button>
        </div>
      </td>
    `;
    labelResponsiveCells(tr, ["Student ID", "Student", "Assigned desk", "Attendance", "Points", "Actions"]);
    sectionRosterTbody.appendChild(tr);
  });
}

window.deleteStudent = async function (studentId, studentName) {
  const confirmed = await showConfirmModal({
    title: "Delete Student",
    message: `Are you sure you want to delete "${studentName}"? Their desk assignment, history, and face profile will be removed.`,
    confirmText: "Delete",
    cancelText: "Cancel",
    isDanger: true,
  });
  if (!confirmed) return;

  try {
    const res = await fetch(`/api/students/${studentId}?section_id=${currentSectionId}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Failed to delete student");
    showToast(`Student "${studentName}" deleted`, "info");
    await fetchSeats();
    fetchRecitationLedger();
  } catch (err) {
    showToast(err.message, "error");
  }
};

// Modal Controllers & Student Enrollment
window.openRegisterStudentModal = function () {
  if (!currentSectionId) {
    showToast("Please create and select a section before adding students.", "warning");
    openNewSectionModal();
    return;
  }

  const activeSec = sectionsList.find((s) => s.id === currentSectionId);
  const titleEl = document.getElementById("modal-enroll-section-name");
  if (titleEl) {
    titleEl.textContent = activeSec ? `${activeSec.name} (${activeSec.subject})` : "Active Section";
  }

  const select = document.getElementById("reg-seat-select");
  if (select) {
    select.innerHTML = '<option value="">-- Leave Unassigned (Add to Pool) --</option>';
    currentSeats.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.id;
      const occupant = s.student_name && !s.student_name.startsWith("Empty (") ? ` (Occupied: ${s.student_name})` : " (Empty)";
      opt.textContent = `${s.label}${occupant}`;
      select.appendChild(opt);
    });
  }

  const form = document.getElementById("form-register-student");
  if (form) form.reset();

  document.getElementById("modal-register-student").classList.remove("hidden");
};

window.closeRegisterStudentModal = function () {
  document.getElementById("modal-register-student").classList.add("hidden");
};

// Stub photo/webcam handlers safely in case referenced anywhere
window.switchEnrollPhotoTab = function () {};
window.onEnrollPhotoFileSelected = function () {};
window.startEnrollWebcam = async function () {};
window.captureEnrollSnapshot = function () {};
window.retakeEnrollSnapshot = function () {};
window.stopEnrollWebcam = function () {};

window.submitRegisterStudent = async function (e) {
  e.preventDefault();
  const student_name = document.getElementById("reg-student-name").value.trim();
  const student_id_number = document.getElementById("reg-student-id").value.trim();
  const seatSelect = document.getElementById("reg-seat-select");
  const seat_id = seatSelect ? seatSelect.value || null : null;

  if (!student_name) {
    showToast("Please enter student name", "warning");
    return;
  }

  const submitBtn = document.getElementById("btn-submit-enroll");
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = "Saving...";
  }

  try {
    const autoDeskCheckbox = document.getElementById("reg-auto-desk");
    const auto_create_desk = autoDeskCheckbox ? autoDeskCheckbox.checked : false;

    const res = await fetch(`/api/sections/${currentSectionId}/students/enroll`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: student_name,
        student_id_number: student_id_number,
        photo_base64: "",
        assign_to_seat_id: seat_id,
        auto_create_desk: auto_create_desk,
      }),
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || "Enrollment failed");
    }

    closeRegisterStudentModal();
    showToast(`Student "${student_name}" added successfully`, "success");
    await fetchSeats();
    fetchSections();
    fetchRecitationLedger();
  } catch (err) {
    showToast("Enrollment error: " + err.message, "error");
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Save Student";
    }
  }
};

window.openNewSectionModal = function () {
  document.getElementById("modal-new-section").classList.remove("hidden");
};

window.closeNewSectionModal = function () {
  document.getElementById("modal-new-section").classList.add("hidden");
};

window.submitNewSection = async function (e) {
  e.preventDefault();
  const name = document.getElementById("sec-name").value.trim();
  const subject = document.getElementById("sec-subject").value.trim();
  const roomEl = document.getElementById("sec-room");
  const room = roomEl ? roomEl.value.trim() : "";

  try {
    const res = await fetch("/api/sections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, subject, room }),
    });
    if (!res.ok) throw new Error("Failed to create section");
    const newSec = await res.json();
    currentSectionId = newSec.id;
    currentReportsSectionId = newSec.id;
    closeNewSectionModal();
    document.getElementById("form-new-section").reset();
    showToast(`Class section "${newSec.name}" created`, "success");
    await fetchSections();
    fetchSeats();
    fetchRecitationLedger();
  } catch (err) {
    showToast(err.message, "error");
  }
};

// ============================================================================
// 3. LIVE RECITATION LEDGER (Grouped by Student + Who Raised First)
// ============================================================================
async function fetchRecitationLedger() {
  const requestId = ++ledgerRequestId;
  const sectionId = activeSession?.section_id || currentSectionId;
  const sessionId = activeSession?.id || "";
  try {
    const generation = authGeneration;
    if (!teacherToken && !guestToken) return;
    const params = new URLSearchParams({ section_id: sectionId });
    if (sessionId) params.set("session_id", sessionId);
    const res = await fetch(`/api/recitation/ledger?${params}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`Queue request failed (${res.status})`);
    const ledger = await res.json();
    if (
      generation !== authGeneration ||
      requestId !== ledgerRequestId ||
      sectionId !== (activeSession?.section_id || currentSectionId) ||
      sessionId !== (activeSession?.id || "")
    ) return;
    currentLedger = ledger;
    renderRecitationLedger();
  } catch (err) {
    console.error("Failed to load recitation ledger:", err);
  }
}

function renderRecitationLedger() {
  renderGuestQueue();
  if (!recitationTbody) return;
  recitationTbody.innerHTML = "";

  // Active raisers sorted by arrival queue
  const activeRaisers = currentLedger
    .filter((s) => s.latest_status === "VALID" && s.latest_queue_pos)
    .sort((a, b) => a.latest_queue_pos - b.latest_queue_pos);

  if (currentLedger.length === 0) {
    recitationTbody.innerHTML = `
      <tr><td colspan="6" class="px-3 py-6 text-center text-slate-400 text-xs">No students registered in this section.</td></tr>
    `;
    return;
  }

  currentLedger.forEach((student, idx) => {
    const isDoubleHand = student.latest_reason_code === "ERR_DOUBLE_HAND_RAISE";
    const isSeatMismatch = student.verification_status === "SEAT_MISMATCH";
    const isRaised = student.latest_status === "VALID" && !isDoubleHand;
    const canAward = Boolean(activeSession) && isRaised && student.latest_earned_point === null && student.latest_event_id;

    // Queue position badge — clean, no ms timing
    let podiumBadge = `<span class="text-slate-400 font-mono text-[11px]">—</span>`;
    if (activeSession && isRaised && student.latest_queue_pos === 1) {
      podiumBadge = `<span class="inline-flex items-center space-x-1 px-1.5 py-0.5 rounded bg-amber-100 text-amber-900 font-bold text-[11px]"><span>🥇</span><span>1st</span></span>`;
    } else if (activeSession && isRaised && student.latest_queue_pos === 2) {
      podiumBadge = `<span class="inline-flex items-center space-x-1 px-1.5 py-0.5 rounded bg-slate-200 text-slate-800 font-semibold text-[11px]"><span>🥈</span><span>2nd</span></span>`;
    } else if (activeSession && isRaised && student.latest_queue_pos === 3) {
      podiumBadge = `<span class="inline-flex items-center space-x-1 px-1.5 py-0.5 rounded bg-orange-100 text-orange-900 font-semibold text-[11px]"><span>🥉</span><span>3rd</span></span>`;
    } else if (activeSession && isRaised && student.latest_queue_pos > 3) {
      podiumBadge = `<span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 font-semibold text-[11px]">#${student.latest_queue_pos}</span>`;
    }

    let actionBtn = "";
    if (role === "guest") {
      actionBtn = (student.latest_earned_point === 1)
        ? `<span class="text-emerald-700 font-bold text-[11px] bg-emerald-50 px-1.5 py-0.5 rounded">✓ +1</span>`
        : `<span class="text-[11px] text-slate-400 italic">Idle</span>`;
    } else if (!activeSession) {
      actionBtn = `<span class="text-[11px] text-slate-400 italic">Idle</span>`;
    } else if (student.latest_earned_point === 1) {
      actionBtn = `<span class="text-emerald-700 font-bold text-[11px] bg-emerald-50 px-1.5 py-0.5 rounded">✓ +1</span>`;
    } else if (isSeatMismatch && canAward) {
      actionBtn = `
        <div class="flex flex-col items-end space-y-0.5">
          <span class="text-[10px] font-bold text-rose-700 bg-rose-100 border border-rose-200 px-1.5 py-0.5 rounded">Blocked</span>
          <div class="flex items-center space-x-1">
            <button onclick="awardStudentPoint('${student.latest_event_id}', true)" title="Override" class="text-[10px] text-rose-700 hover:text-rose-900 underline font-semibold">Override</button>
            <button onclick="dismissStudentEvent('${student.latest_event_id}')" class="text-[10px] text-slate-400 hover:text-slate-600 underline">×</button>
          </div>
        </div>
      `;
    } else if (canAward) {
      actionBtn = `
        <div class="flex justify-end space-x-1">
          <button onclick="awardStudentPoint('${student.latest_event_id}', false)" class="bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-[11px] px-2 py-0.5 rounded shadow-xs transition">+1</button>
          <button onclick="dismissStudentEvent('${student.latest_event_id}')" class="bg-slate-200 hover:bg-slate-300 text-slate-600 font-medium text-[11px] px-1.5 py-0.5 rounded transition">×</button>
        </div>
      `;
    } else {
      actionBtn = `
        <button onclick="awardDirectStudent('${student.seat_id}')" title="Award a participation point manually" class="text-[11px] text-slate-500 hover:text-slate-800 font-medium px-1.5 py-0.5 bg-slate-100 hover:bg-slate-200 rounded border border-slate-200">+1 manual</button>
      `;
    }

    let statusBadge = `<span class="px-1.5 py-0.5 rounded text-[11px] font-semibold bg-slate-100 text-slate-500">Seated</span>`;
    if (!activeSession) {
      statusBadge = `<span class="px-1.5 py-0.5 rounded text-[11px] font-medium bg-slate-100 text-slate-400">Standby</span>`;
    } else if (isSeatMismatch) {
      statusBadge = `
        <span class="px-1.5 py-0.5 rounded text-[11px] font-bold bg-rose-100 text-rose-900 border border-rose-300 inline-flex items-center space-x-1">
          <span class="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse"></span>
          <span>Mismatch</span>
        </span>
      `;
    } else if (isDoubleHand) {
      statusBadge = `<span class="px-1.5 py-0.5 rounded text-[11px] font-bold bg-amber-100 text-amber-900 border border-amber-300">2-Hands</span>`;
    } else if (isRaised) {
      statusBadge = `
        <span class="px-1.5 py-0.5 rounded text-[11px] font-semibold bg-emerald-100 text-emerald-800 inline-flex items-center space-x-1">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
          <span>Raised</span>
        </span>
      `;
    }

    const initials = getStudentInitials(student.student_name);
    const color = getStudentAvatarColor(student.student_name);
    const avatarHtml = `<div class="w-6 h-6 rounded-full ${color.bg} ${color.text} font-bold text-[10px] flex items-center justify-center shrink-0 border ${color.border}">${initials}</div>`;

    const tr = document.createElement("tr");
    tr.className = `transition-all duration-300 ease-out ${
      !activeSession
        ? "hover:bg-slate-50"
        : isSeatMismatch
        ? "bg-rose-50/70 border-l-2 border-rose-500"
        : isDoubleHand
        ? "bg-amber-50/60"
        : isRaised
        ? "bg-emerald-50/50"
        : "hover:bg-slate-50"
    }`;
    // Smooth fade-in animation
    tr.style.opacity = "0";
    tr.style.transform = "translateY(4px)";
    tr.innerHTML = `
      <td class="px-2.5 py-2">${podiumBadge}</td>
      <td class="px-2.5 py-2">
        <div class="flex items-center space-x-2">
          ${avatarHtml}
          <div class="min-w-0">
            <span class="font-bold text-slate-900 text-xs leading-tight block truncate">${student.student_name}</span>
            <div class="text-[10px] text-slate-500 font-medium truncate">${student.label}</div>
          </div>
        </div>
      </td>
      <td class="px-2.5 py-2">${statusBadge}</td>
      <td class="px-2.5 py-2 text-center font-bold text-slate-800 text-xs">${student.total_raises || 0}</td>
      <td class="px-2.5 py-2 text-center font-bold text-slate-800 text-xs">${student.total_points || 0}</td>
      <td class="px-2.5 py-2 text-right">${actionBtn}</td>
    `;
    labelResponsiveCells(tr, ["Queue", "Student", "Status", "Raises", "Points", "Action"]);
    recitationTbody.appendChild(tr);

    // Staggered smooth fade-in
    requestAnimationFrame(() => {
      setTimeout(() => {
        tr.style.transition = "opacity 0.3s ease, transform 0.3s ease";
        tr.style.opacity = "1";
        tr.style.transform = "translateY(0)";
      }, idx * 30);
    });
  });
}

window.clearPodiumRound = function () {
  if (podiumBannerText) podiumBannerText.textContent = "Round reset. Ready for next question.";
  fetchRecitationLedger();
};

window.awardStudentPoint = async function (eventId, force = false) {
  try {
    const qs = force ? "?force=true" : "";
    const res = await fetch(`/api/events/${eventId}/award${qs}`, { method: "POST" });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || "Award failed");
    }
    showToast(force ? "Override applied: +1 Point awarded" : "+1 Recitation point awarded", "success");
    fetchRecitationLedger();
    fetchSeats();
  } catch (err) {
    showToast(err.message, "error");
  }
};

window.dismissStudentEvent = async function (eventId) {
  try {
    const res = await fetch(`/api/events/${eventId}/dismiss`, { method: "POST" });
    if (!res.ok) throw new Error("Dismiss failed");
    showToast("Raise event dismissed", "info");
    fetchRecitationLedger();
  } catch (err) {
    showToast(err.message, "error");
  }
};

window.awardDirectStudent = async function (seatId) {
  if (manualAwardsInProgress.has(seatId)) return;
  manualAwardsInProgress.add(seatId);
  try {
    const sectionId = activeSession?.section_id || currentSectionId;
    const res = await fetch(`/api/seats/${encodeURIComponent(seatId)}/award?section_id=${encodeURIComponent(sectionId)}`, { method: "POST" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Manual point award failed");
    }
    showToast("Manual participation point awarded", "success");
    fetchRecitationLedger();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    manualAwardsInProgress.delete(seatId);
  }
};

// ============================================================================
// 4. ATTENDANCE, SEATING GRID & HEATMAP
// ============================================================================
async function fetchSeats() {
  const requestId = ++seatDataRequestId;
  const sectionId = currentSectionId;
  seatDataLoadingFor = sectionId;
  if (!sectionId) {
    currentSeats = [];
    currentStudents = [];
    seatDataLoadingFor = "";
    renderClassroomDesks();
    renderSectionRoster();
    return;
  }
  try {
    const generation = authGeneration;
    const studentsRequest = role === "teacher"
      ? fetch(`/api/sections/${encodeURIComponent(sectionId)}/students`, { cache: "no-store" })
      : null;
    const seatsRes = await fetch(`/api/seats?section_id=${encodeURIComponent(sectionId)}`, { cache: "no-store" });
    if (!seatsRes.ok) throw new Error(`Seat request failed (${seatsRes.status})`);
    const seats = await seatsRes.json();
    if (
      generation !== authGeneration ||
      requestId !== seatDataRequestId ||
      sectionId !== currentSectionId
    ) {
      if (requestId === seatDataRequestId) seatDataLoadingFor = "";
      return;
    }
    currentSeats = seats;
    // Render the authoritative seat response immediately. A failure fetching
    // the separate student pool must not leave a successfully configured grid
    // hidden behind the empty-state panel.
    renderClassroomDesks();

    if (studentsRequest) {
      try {
        const studentsRes = await studentsRequest;
        if (!studentsRes.ok) throw new Error(`Student request failed (${studentsRes.status})`);
        const students = await studentsRes.json();
        if (
          generation !== authGeneration ||
          requestId !== seatDataRequestId ||
          sectionId !== currentSectionId
        ) {
          if (requestId === seatDataRequestId) seatDataLoadingFor = "";
          return;
        }
        currentStudents = students;
      } catch (err) {
        console.warn("Failed to load section student pool:", err);
      }
    }
    seatDataLoadingFor = "";

    if (seatingViewMode === "heatmap") {
      await fetchHeatmapData();
    }

    renderGuestQueue();
    renderUnassignedStudentTray();
    renderSectionRoster();
    populateCalibrationInputs();
  } catch (err) {
    if (requestId === seatDataRequestId) seatDataLoadingFor = "";
    console.error("Failed to load seats & students:", err);
  }
}

async function fetchHeatmapData() {
  try {
    const res = await fetch(`/api/analytics/heatmap?section_id=${currentSectionId}`);
    if (res.ok) {
      currentHeatmapData = await res.json();
    }
  } catch (err) {
    console.error("Failed to fetch heatmap data:", err);
  }
}

window.openSeatingSettingsModal = function () {
  const modal = document.getElementById("modal-seating-settings");
  if (modal) modal.classList.remove("hidden");
};

window.closeSeatingSettingsModal = function () {
  const modal = document.getElementById("modal-seating-settings");
  if (modal) modal.classList.add("hidden");
};

window.autoGenerateDesksFromEnrolled = async function () {
  if (seatingMutationInProgress) {
    showToast("A seating update is already in progress", "warning");
    return;
  }
  if (!currentSectionId) {
    showToast("Please select a class section first", "warning");
    return;
  }

  const sectionId = currentSectionId;
  if (seatDataLoadingFor === sectionId) await fetchSeats();
  if (sectionId !== currentSectionId) return;

  const count = currentStudents.length;
  if (count === 0) {
    showToast("No students enrolled in this section yet. Add students first.", "warning");
    return;
  }

  const confirmed = await showConfirmModal({
    title: "Auto-Generate Desks",
    message: `Generate exactly ${count} desks tailored to all enrolled students? Empty desks will be eliminated and existing layout updated.`,
    confirmText: "Generate Desks",
    cancelText: "Cancel",
    isDanger: false,
  });
  if (!confirmed) return;
  if (sectionId !== currentSectionId) {
    showToast("Section changed. Select Auto-Generate again for the current section.", "warning");
    return;
  }

  seatingMutationInProgress = true;
  try {
    const res = await fetch(`/api/sections/${encodeURIComponent(sectionId)}/seats/auto-generate-enrolled`, {
      method: "POST",
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(detail || `Failed to auto-generate desks (${res.status})`);
    }
    const updated = await res.json();
    if (sectionId !== currentSectionId) return;
    currentSeats = updated;
    await fetchSeats();
    fetchRecitationLedger();
    showToast(`Generated ${updated.length} desks for all enrolled students`, "success");
  } catch (err) {
    showToast("Error: " + err.message, "error");
  } finally {
    seatingMutationInProgress = false;
  }
};

window.autoFillDesksAlphabetical = async function () {
  if (seatingMutationInProgress) {
    showToast("A seating update is already in progress", "warning");
    return;
  }
  const sectionId = currentSectionId;
  if (seatDataLoadingFor === sectionId) await fetchSeats();
  if (!sectionId || sectionId !== currentSectionId) return;
  if (currentSeats.length === 0) {
    showToast("Please initialize a seating grid first", "warning");
    return;
  }
  if (currentStudents.length === 0) {
    showToast("No enrolled students found in this section", "warning");
    return;
  }
  const seatsForSection = [...currentSeats];
  const studentsForSection = [...currentStudents];

  // Sort students by last name
  function getStudentLastName(name) {
    if (!name) return "";
    const clean = name.trim();
    if (clean.includes(",")) {
      return clean.split(",")[0].trim().toLowerCase();
    }
    const parts = clean.split(/\s+/);
    return parts[parts.length - 1].toLowerCase();
  }

  const sortedStudents = studentsForSection.sort((a, b) => {
    const lastA = getStudentLastName(a.name);
    const lastB = getStudentLastName(b.name);
    const cmp = lastA.localeCompare(lastB);
    if (cmp !== 0) return cmp;
    return (a.name || "").localeCompare(b.name || "");
  });

  const confirmed = await showConfirmModal({
    title: "Auto-Fill Chairs Alphabetically",
    message: `Auto-assign all ${sortedStudents.length} enrolled students into chairs alphabetically by last name (A to Z)?`,
    confirmText: "Auto-Fill Chairs",
    cancelText: "Cancel",
    isDanger: false,
  });
  if (!confirmed) return;
  if (sectionId !== currentSectionId) {
    showToast("Section changed. Select Auto-Fill again for the current section.", "warning");
    return;
  }

  seatingMutationInProgress = true;
  try {
    // Attempt dedicated backend endpoint first
    let success = false;
    try {
      const res = await fetch(`/api/sections/${encodeURIComponent(sectionId)}/seats/auto-fill-alphabetical`, {
        method: "POST",
      });
      if (res.ok) {
        const updated = await res.json();
        if (sectionId !== currentSectionId) return;
        currentSeats = updated;
        success = true;
      } else if (res.status !== 404 && res.status !== 405) {
        throw new Error(`Auto-fill failed (${res.status})`);
      }
    } catch (error) {
      if (error instanceof Error && !/Failed to fetch|NetworkError/i.test(error.message)) throw error;
    }

    if (!success) {
      // Direct fallback via PUT /api/seats with sorted student assignments
      const sortedSeats = [...seatsForSection].sort((a, b) => {
        if (a.grid_row !== undefined && b.grid_row !== undefined && a.grid_row !== b.grid_row) {
          return (a.grid_row || 0) - (b.grid_row || 0);
        }
        if (a.grid_col !== undefined && b.grid_col !== undefined && a.grid_col !== b.grid_col) {
          return (a.grid_col || 0) - (b.grid_col || 0);
        }
        return (a.label || "").localeCompare(b.label || "", undefined, { numeric: true });
      });

      sortedSeats.forEach((seat, idx) => {
        if (idx < sortedStudents.length) {
          const st = sortedStudents[idx];
          seat.student_id = st.id;
          seat.student_name = st.name;
          seat.student_id_number = st.student_id_number || "";
          seat.photo_path = st.photo_path || null;
        } else {
          seat.student_id = null;
          seat.student_name = `Empty (${seat.label})`;
          seat.student_id_number = "";
          seat.photo_path = null;
        }
      });

      const putRes = await fetch(`/api/seats?section_id=${encodeURIComponent(sectionId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seats: sortedSeats }),
      });
      if (!putRes.ok) throw new Error("Failed to save seat assignments");
      const updated = await putRes.json();
      if (sectionId !== currentSectionId) return;
      currentSeats = updated;
    }

    if (sectionId !== currentSectionId) return;
    await fetchSeats();
    fetchRecitationLedger();
    showToast(`Chairs filled alphabetically by student last name (A-Z)`, "success");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    seatingMutationInProgress = false;
  }
};

window.clearAllSeatAssignments = async function () {
  const confirmed = await showConfirmModal({
    title: "Unassign All Chairs",
    message: "Return all assigned students to the Student Pool and mark all chairs empty?",
    confirmText: "Unassign All",
    cancelText: "Cancel",
    isDanger: true,
  });
  if (!confirmed) return;

  try {
    let success = false;
    try {
      const res = await fetch(`/api/sections/${currentSectionId}/seats/clear-assignments`, {
        method: "POST",
      });
      if (res.ok) {
        currentSeats = await res.json();
        success = true;
      }
    } catch (_) {}

    if (!success) {
      currentSeats.forEach((seat) => {
        seat.student_id = null;
        seat.student_name = `Empty (${seat.label})`;
        seat.student_id_number = "";
        seat.photo_path = null;
      });

      const putRes = await fetch(`/api/seats?section_id=${currentSectionId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seats: currentSeats }),
      });
      if (!putRes.ok) throw new Error("Failed to clear chair assignments");
      currentSeats = await putRes.json();
    }

    await fetchSeats();
    fetchRecitationLedger();
    showToast("All students returned to the Student Pool", "info");
  } catch (err) {
    showToast(err.message, "error");
  }
};

window.setSeatingViewMode = async function (mode) {
  seatingViewMode = mode;
  const btnSeating = document.getElementById("btn-mode-seating");
  const btnHeatmap = document.getElementById("btn-mode-heatmap");
  const heatmapLegend = document.getElementById("heatmap-legend");
  const unassignedPool = document.getElementById("unassigned-pool-container");

  if (mode === "heatmap") {
    if (btnHeatmap) btnHeatmap.className = "px-3 py-1.5 rounded-md font-bold transition bg-white text-brand-900 shadow-xs";
    if (btnSeating) btnSeating.className = "px-3 py-1.5 rounded-md font-semibold text-slate-600 hover:text-slate-900 transition";
    if (heatmapLegend) heatmapLegend.classList.remove("hidden");
    if (unassignedPool) unassignedPool.classList.add("hidden");
    await fetchHeatmapData();
  } else {
    if (btnSeating) btnSeating.className = "px-3 py-1.5 rounded-md font-bold transition bg-white text-brand-900 shadow-xs";
    if (btnHeatmap) btnHeatmap.className = "px-3 py-1.5 rounded-md font-semibold text-slate-600 hover:text-slate-900 transition";
    if (heatmapLegend) heatmapLegend.classList.add("hidden");
    if (unassignedPool) unassignedPool.classList.remove("hidden");
  }

  renderClassroomDesks();
};

window.applyGridDimensions = async function () {
  const rowsInput = document.getElementById("grid-rows-input");
  const colsInput = document.getElementById("grid-cols-input");
  const rows = parseInt(rowsInput ? rowsInput.value : "3", 10);
  const cols = parseInt(colsInput ? colsInput.value : "4", 10);

  if (rows < 1 || rows > 10 || cols < 1 || cols > 10) {
    showToast("Rows and columns must be between 1 and 10", "warning");
    return;
  }
  if (!currentSectionId) {
    showToast("Please select a class section first", "warning");
    return;
  }
  if (seatingMutationInProgress) {
    showToast("A seating update is already in progress", "warning");
    return;
  }
  const sectionId = currentSectionId;

  const confirmed = await showConfirmModal({
    title: "Reconfigure Seating Grid",
    message: `Generate a ${rows} × ${cols} seating grid (${rows * cols} total desks)? Current student assignments will be preserved where possible.`,
    confirmText: "Generate Grid",
    cancelText: "Cancel",
    isDanger: false,
  });
  if (!confirmed) return;
  if (sectionId !== currentSectionId) {
    showToast("Section changed. Initialize the grid again for the current section.", "warning");
    return;
  }

  seatingMutationInProgress = true;
  try {
    const res = await fetch(`/api/sections/${encodeURIComponent(sectionId)}/seats/grid`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows, cols }),
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(detail || `Failed to configure grid (${res.status})`);
    }
    const updated = await res.json();
    if (!Array.isArray(updated) || updated.length !== rows * cols) {
      throw new Error(`The server returned ${Array.isArray(updated) ? updated.length : 0} of ${rows * cols} desks. Refresh and try again.`);
    }
    if (sectionId !== currentSectionId) return;
    currentSeats = updated;
    renderClassroomDesks();
    renderUnassignedStudentTray();
    renderSectionRoster();
    await fetchSeats();
    fetchRecitationLedger();
    showToast(`Configured ${rows} × ${cols} seating grid`, "success");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    seatingMutationInProgress = false;
  }
};

let seatZoomLevel = 100;
try {
  const savedZoom = localStorage.getItem("classtrack_seat_zoom");
  if (savedZoom) seatZoomLevel = parseInt(savedZoom, 10) || 100;
} catch (_) {}

window.onSeatZoomChange = function (val) {
  seatZoomLevel = parseInt(val, 10) || 100;
  const zoomLabel = document.getElementById("seat-zoom-label");
  const slider = document.getElementById("seat-zoom-slider");
  if (zoomLabel) zoomLabel.textContent = `${seatZoomLevel}%`;
  if (slider && slider.value !== String(seatZoomLevel)) slider.value = seatZoomLevel;

  const grid = document.getElementById("classroom-desks-grid");
  if (grid) {
    grid.style.zoom = seatZoomLevel / 100;
  }
  try {
    localStorage.setItem("classtrack_seat_zoom", seatZoomLevel);
  } catch (_) {}
};

window.stepSeatZoom = function (delta) {
  const slider = document.getElementById("seat-zoom-slider");
  let cur = parseInt(slider ? slider.value : seatZoomLevel, 10) || 100;
  cur = Math.max(60, Math.min(150, cur + delta));
  window.onSeatZoomChange(cur);
};

window.resetSeatZoom = function () {
  window.onSeatZoomChange(100);
};

function renderClassroomDesks() {
  if (!classroomDesksGrid) return;
  classroomDesksGrid.innerHTML = "";

  if (currentSeats.length === 0) {
    classroomDesksGrid.innerHTML = `
      <div class="col-span-full p-8 text-center bg-slate-50 border-2 border-dashed border-slate-300 rounded-xl space-y-3">
        <div class="w-10 h-10 rounded-full bg-brand-50 text-brand-700 flex items-center justify-center mx-auto">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>
        </div>
        <h4 class="text-sm font-bold text-slate-800">No Seating Grid Configured for this Section</h4>
        <p class="text-xs text-slate-500 max-w-md mx-auto">Each section maintains an independent seating plan. Initialize a grid for this class:</p>
        <div class="flex flex-wrap justify-center gap-2 pt-1">
          <button onclick="applyPresetGrid(3, 3)" class="bg-brand-700 hover:bg-brand-800 text-white font-semibold text-xs px-3 py-1.5 rounded-lg shadow-xs transition">
            Initialize 3×3 Grid (9 Desks)
          </button>
          <button onclick="applyPresetGrid(3, 4)" class="bg-white hover:bg-slate-50 text-slate-700 border border-slate-300 font-semibold text-xs px-3 py-1.5 rounded-lg shadow-xs transition">
            Initialize 3×4 Grid (12 Desks)
          </button>
          <button onclick="loadSeatPreset('4-seats')" class="bg-white hover:bg-slate-50 text-slate-700 border border-slate-300 font-semibold text-xs px-3 py-1.5 rounded-lg shadow-xs transition">
            Load 4-Desk Layout
          </button>
        </div>
      </div>
    `;
    return;
  }

  // Determine grid dimensions
  let maxCol = 1;
  let maxRow = 1;
  currentSeats.forEach((s) => {
    if (s.grid_col && s.grid_col > maxCol) maxCol = s.grid_col;
    if (s.grid_row && s.grid_row > maxRow) maxRow = s.grid_row;
  });

  const colsInput = document.getElementById("grid-cols-input");
  const rowsInput = document.getElementById("grid-rows-input");
  const effectiveCols = colsInput ? Math.max(parseInt(colsInput.value, 10) || 1, maxCol + 1) : maxCol + 1;
  const effectiveRows = rowsInput ? Math.max(parseInt(rowsInput.value, 10) || 1, maxRow + 1) : maxRow + 1;

  // Responsive grid: cards are min 135px to comfortably fit student name & photo
  classroomDesksGrid.style.gridTemplateColumns = `repeat(${effectiveCols}, minmax(135px, 160px))`;
  classroomDesksGrid.style.justifyContent = "start";
  classroomDesksGrid.style.zoom = seatZoomLevel / 100;

  const zoomLabel = document.getElementById("seat-zoom-label");
  const slider = document.getElementById("seat-zoom-slider");
  if (zoomLabel) zoomLabel.textContent = `${seatZoomLevel}%`;
  if (slider) slider.value = seatZoomLevel;

  const dimBadge = document.getElementById("grid-dim-badge");
  if (dimBadge) {
    dimBadge.textContent = `${effectiveRows} × ${effectiveCols} Grid (${currentSeats.length} Desks)`;
  }

  // Map heatmap data for rapid lookup
  const heatmapMap = {};
  if (currentHeatmapData && currentHeatmapData.cells) {
    currentHeatmapData.cells.forEach((c) => {
      heatmapMap[c.seat_id] = c;
    });
  }

  currentSeats.forEach((seat) => {
    const isPresent = Boolean(seat.is_present);
    const hasStudent = Boolean(seat.student_id || (seat.student_name && !seat.student_name.startsWith("Empty (")));

    if (seatingViewMode === "heatmap") {
      // -------------------------------------------------------------
      // HEATMAP CELL RENDER (Clean, Responsive, Viewable Names - No Points)
      // -------------------------------------------------------------
      const stats = heatmapMap[seat.id] || { total_raises: 0, total_points: 0, mismatch_count: 0, intensity: 0 };
      const raises = stats.total_raises || 0;
      const mismatches = stats.mismatch_count || 0;

      let heatBg = "bg-slate-50 border-slate-200 text-slate-700";
      let activityBadge = `<span class="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-slate-200 text-slate-700">Quiet</span>`;

      if (raises >= 8) {
        heatBg = "bg-emerald-50/90 border-emerald-400 text-emerald-950 ring-1 ring-emerald-400/40 shadow-xs";
        activityBadge = `<span class="px-1.5 py-0.5 rounded text-[9px] font-bold bg-emerald-600 text-white flex items-center space-x-1"><svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg><span>High</span></span>`;
      } else if (raises >= 3) {
        heatBg = "bg-amber-50/90 border-amber-400 text-amber-950 ring-1 ring-amber-400/40 shadow-xs";
        activityBadge = `<span class="px-1.5 py-0.5 rounded text-[9px] font-bold bg-amber-500 text-white flex items-center space-x-1"><svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg><span>Active</span></span>`;
      }

      const card = document.createElement("div");
      card.className = `p-2.5 rounded-xl border transition flex flex-col justify-between aspect-square w-full min-w-[135px] max-w-[160px] min-h-[145px] shadow-2xs ${heatBg}`;
      card.innerHTML = `
        <div class="flex justify-between items-center pb-1 border-b border-slate-200/50">
          <span class="text-[11px] font-mono font-bold px-1.5 py-0.5 rounded bg-white/80 border border-slate-200 leading-none">${seat.label}</span>
          ${activityBadge}
        </div>
        <div class="my-auto text-center py-1">
          <h4 class="text-xs font-bold line-clamp-2 leading-tight break-words px-0.5" title="${seat.student_name}">
            ${hasStudent ? seat.student_name : '<span class="text-slate-400 italic">Empty Desk</span>'}
          </h4>
          <div class="text-[10px] font-semibold mt-1 flex items-center justify-center text-slate-600">
            <span>Raises: <strong>${raises}</strong></span>
          </div>
        </div>
        <div class="pt-1 border-t border-slate-200/50 text-[10px] text-center">
          ${
            mismatches > 0
              ? `<span class="text-rose-700 font-bold flex items-center justify-center space-x-1"><svg class="w-3 h-3 text-rose-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg><span>${mismatches} alert${mismatches > 1 ? "s" : ""}</span></span>`
              : `<span class="text-slate-400">No alerts</span>`
          }
        </div>
      `;
      classroomDesksGrid.appendChild(card);
    } else {
      // -------------------------------------------------------------
      // INTERACTIVE DRAG-AND-DROP SEATING DESK RENDER (Clearly Viewable Names - No Points)
      // -------------------------------------------------------------
      const card = document.createElement("div");
      card.id = `desk-card-${seat.id}`;
      card.setAttribute("ondragover", "allowDeskDrop(event)");
      card.setAttribute("ondragleave", "onDeskDragLeave(event)");
      card.setAttribute("ondrop", `onDeskDrop(event, '${seat.id}')`);

      if (hasStudent) {
        card.setAttribute("draggable", "true");
        card.setAttribute("ondragstart", `onDeskCardDragStart(event, '${seat.id}', '${seat.student_id || ""}')`);
        card.className = `group relative p-2.5 rounded-xl border transition-all cursor-grab active:cursor-grabbing select-none flex flex-col justify-between aspect-square w-full min-w-[135px] max-w-[160px] min-h-[145px] shadow-xs ${
          isPresent
            ? "border-slate-200 bg-white hover:border-brand-600 hover:shadow-md"
            : "border-rose-200 bg-rose-50/30 opacity-85 hover:opacity-100"
        }`;

        const initials = getStudentInitials(seat.student_name);
        const color = getStudentAvatarColor(seat.student_name);
        const avatarHtml = `<div class="w-10 h-10 rounded-full ${color.bg} ${color.text} font-extrabold text-sm flex items-center justify-center shrink-0 mx-auto border-2 ${color.border} shadow-2xs tracking-wider">${initials}</div>`;

        card.innerHTML = `
          <!-- Top Row: Desk label & unassign button, Attendance status pill -->
          <div class="flex justify-between items-center pb-1 border-b border-slate-100">
            <div class="flex items-center space-x-1">
              <span class="text-[11px] font-mono font-bold text-slate-700 bg-slate-100 px-1.5 py-0.5 rounded border border-slate-200 leading-none">${seat.label}</span>
              <button onclick="unassignSeat('${seat.id}')" title="Return student to pool" class="text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded p-0.5 text-xs font-bold leading-none transition">
                &times;
              </button>
            </div>
            <span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${
              isPresent ? "bg-emerald-100 text-emerald-800" : "bg-rose-100 text-rose-800"
            }">
              ${isPresent ? "Present" : "Absent"}
            </span>
          </div>

          <!-- Center: Avatar + FULL VIEWABLE STUDENT NAME (2-line wrap) + Student ID (No Points) -->
          <div class="my-auto py-1 flex flex-col items-center justify-center text-center">
            ${avatarHtml}
            <h4 class="text-xs font-bold text-slate-900 line-clamp-2 leading-tight break-words mt-1.5 px-1" title="${seat.student_name}">
              ${seat.student_name}
            </h4>
            ${seat.student_id_number ? `<div class="text-[10px] font-mono text-slate-400 mt-0.5 truncate max-w-full px-1">${seat.student_id_number}</div>` : ""}
          </div>

          <!-- Bottom Row: Mark Absent / Mark Present Action Button -->
          <div class="pt-1 border-t border-slate-100">
            <button onclick="toggleAttendance('${seat.id}')" title="Toggle student attendance" class="w-full py-1 px-2 rounded text-[10px] font-bold transition flex items-center justify-center space-x-1 shadow-2xs ${
              isPresent
                ? "bg-rose-50 hover:bg-rose-100 text-rose-700 border border-rose-200"
                : "bg-emerald-50 hover:bg-emerald-100 text-emerald-700 border border-emerald-200"
            }">
              ${
                isPresent
                  ? `<svg class="w-3 h-3 text-rose-600 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg><span>Mark Absent</span>`
                  : `<svg class="w-3 h-3 text-emerald-600 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg><span>Mark Present</span>`
              }
            </button>
          </div>
        `;
      } else {
        // Empty Desk Slot (Desk Icon 5310972 with Clear Desk Silhouette)
        card.className = "group relative p-2.5 rounded-xl border-2 border-dashed border-slate-200 bg-slate-50/70 hover:bg-brand-50/40 hover:border-brand-400 transition-all flex flex-col justify-between items-center aspect-square w-full min-w-[135px] max-w-[160px] min-h-[145px] text-center select-none shadow-2xs";
        card.innerHTML = `
          <div class="w-full flex justify-between items-center pb-1 border-b border-slate-100">
            <span class="text-[11px] font-mono font-bold text-slate-500 bg-white px-1.5 py-0.5 rounded border border-slate-200 leading-none">${seat.label}</span>
            <span class="text-[10px] font-medium text-slate-400">Empty</span>
          </div>
          <div class="my-auto flex flex-col items-center justify-center py-1">
            <img src="/client/desk_icon.png" alt="Desk" class="w-11 h-11 object-contain opacity-75 group-hover:opacity-100 transition-transform group-hover:scale-105">
          </div>
          <div class="text-[10px] text-slate-400 font-medium pt-1 border-t border-slate-100 w-full">Drop student here</div>
        `;
      }
      classroomDesksGrid.appendChild(card);
    }
  });
}

function renderUnassignedStudentTray() {
  const tray = document.getElementById("unassigned-students-tray");
  const countBadge = document.getElementById("unassigned-count-badge");
  const searchInput = document.getElementById("pool-search-input");
  if (!tray) return;
  tray.innerHTML = "";

  // Identify all students who are assigned to any desk
  const assignedStudentIds = new Set();
  const assignedStudentNames = new Set();

  currentSeats.forEach((s) => {
    const isOccupied = Boolean(
      s.student_id ||
      (s.student_name && !s.student_name.startsWith("Empty (") && s.student_name.trim() !== "")
    );
    if (isOccupied) {
      if (s.student_id) assignedStudentIds.add(s.student_id);
      if (s.student_name) assignedStudentNames.add(s.student_name.trim().toLowerCase());
    }
  });

  let unassigned = currentStudents.filter((st) => {
    if (assignedStudentIds.has(st.id)) return false;
    if (st.name && assignedStudentNames.has(st.name.trim().toLowerCase())) return false;
    return true;
  });

  const filterQuery = (searchInput?.value || "").trim().toLowerCase();
  if (filterQuery) {
    unassigned = unassigned.filter((st) => 
      (st.name && st.name.toLowerCase().includes(filterQuery)) ||
      (st.student_id_number && st.student_id_number.toLowerCase().includes(filterQuery))
    );
  }

  if (countBadge) {
    countBadge.textContent = `${unassigned.length} Unassigned (${currentStudents.length} Total)`;
  }

  if (unassigned.length === 0) {
    tray.innerHTML = filterQuery
      ? `
        <div class="p-4 text-center bg-slate-50 border border-dashed border-slate-200 rounded-lg text-xs text-slate-400">
          No students matching "${filterQuery}"
        </div>
      `
      : `
        <div class="p-4 text-center bg-slate-50 border border-dashed border-slate-200 rounded-lg text-xs text-slate-500 space-y-1.5">
          <svg class="w-5 h-5 text-emerald-500 mx-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          <div class="font-semibold text-slate-700">All Enrolled Students Seated</div>
          <p class="text-[11px] text-slate-400">All ${currentStudents.length} students in this section are assigned to desks.</p>
        </div>
      `;
    return;
  }

  unassigned.forEach((student) => {
    const card = document.createElement("div");
    card.setAttribute("draggable", "true");
    card.setAttribute("ondragstart", `onUnassignedCardDragStart(event, '${student.id}')`);
    card.className = "flex items-center justify-between p-2.5 rounded-lg border border-slate-200 bg-white hover:border-brand-500 hover:shadow-xs transition cursor-grab active:cursor-grabbing select-none group";

    const initials = getStudentInitials(student.name);
    const color = getStudentAvatarColor(student.name);
    const avatarHtml = `<div class="w-7 h-7 rounded-full ${color.bg} ${color.text} font-bold text-xs flex items-center justify-center shrink-0 border ${color.border}">${initials}</div>`;

    card.innerHTML = `
      <div class="flex items-center space-x-2.5 overflow-hidden">
        ${avatarHtml}
        <div class="overflow-hidden">
          <div class="text-xs font-bold text-slate-900 truncate">${student.name}</div>
          <div class="text-[10px] text-slate-400 font-mono truncate">${student.student_id_number || "No ID"}</div>
        </div>
      </div>
      <div class="flex items-center space-x-1 shrink-0">
        <button onclick="event.stopPropagation(); deleteStudent('${student.id}', '${student.name}')" title="Delete Student" class="p-1 text-slate-300 hover:text-rose-600 hover:bg-rose-50 rounded transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
        </button>
        <span class="text-slate-300 group-hover:text-brand-600 transition" title="Drag to place on desk">
          <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20"><path d="M7 2a2 2 0 10.001 4.001A2 2 0 007 2zm0 6a2 2 0 10.001 4.001A2 2 0 007 8zm0 6a2 2 0 10.001 4.001A2 2 0 007 14zm6-12a2 2 0 10.001 4.001A2 2 0 0013 2zm0 6a2 2 0 10.001 4.001A2 2 0 0013 8zm0 6a2 2 0 10.001 4.001A2 2 0 0013 14z"/></svg>
        </span>
      </div>
    `;
    tray.appendChild(card);
  });
}

window.filterUnassignedStudentTray = function () {
  renderUnassignedStudentTray();
};

window.applyPresetGrid = async function (rows, cols) {
  const rowsInput = document.getElementById("grid-rows-input");
  const colsInput = document.getElementById("grid-cols-input");
  if (rowsInput) rowsInput.value = rows;
  if (colsInput) colsInput.value = cols;
  await applyGridDimensions();
};

window.markAllAttendance = async function (isPresent) {
  try {
    const res = await fetch(`/api/sections/${currentSectionId}/attendance/bulk?is_present=${isPresent}`, {
      method: "POST",
    });
    if (!res.ok) throw new Error("Failed to update bulk attendance");
    currentSeats = await res.json();
    renderClassroomDesks();
    fetchRecitationLedger();
    showToast(isPresent ? "Marked all students Present" : "Marked all students Absent", "info");
  } catch (err) {
    showToast(err.message, "error");
  }
};

// ============================================================================
// HTML5 DRAG & DROP EVENT HANDLERS
// ============================================================================
window.allowDrop = function (e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
};

window.allowDeskDrop = function (e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  const target = e.currentTarget;
  if (target) {
    target.classList.add("ring-2", "ring-brand-700", "scale-[1.02]");
  }
};

window.onDeskDragLeave = function (e) {
  const target = e.currentTarget;
  if (target) {
    target.classList.remove("ring-2", "ring-brand-700", "scale-[1.02]");
  }
};

window.onUnassignedCardDragStart = function (e, studentId) {
  draggedStudentId = studentId;
  draggedFromSeatId = null;
  e.dataTransfer.setData("text/plain", studentId);
  e.dataTransfer.effectAllowed = "move";
};

window.onDeskCardDragStart = function (e, seatId, studentId) {
  draggedStudentId = studentId;
  draggedFromSeatId = seatId;
  e.dataTransfer.setData("text/plain", studentId);
  e.dataTransfer.effectAllowed = "move";
};

window.onDeskDrop = async function (e, targetSeatId) {
  e.preventDefault();
  const target = e.currentTarget;
  if (target) {
    target.classList.remove("ring-2", "ring-brand-700", "scale-[1.02]");
  }

  if (draggedFromSeatId) {
    if (draggedFromSeatId !== targetSeatId) {
      try {
        const res = await fetch(`/api/seats/swap?section_id=${encodeURIComponent(currentSectionId)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ seat_id_1: draggedFromSeatId, seat_id_2: targetSeatId }),
        });
        if (!res.ok) throw new Error("Seat swap failed");
        showToast("Swapped desk assignments", "success");
        await fetchSeats();
        fetchRecitationLedger();
      } catch (err) {
        showToast(err.message, "error");
      }
    }
  } else if (draggedStudentId) {
    try {
      const res = await fetch(`/api/seats/${targetSeatId}/assign?section_id=${currentSectionId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ student_id: draggedStudentId }),
      });
      if (!res.ok) throw new Error("Assignment failed");
      showToast("Student assigned to desk", "success");
      await fetchSeats();
      fetchRecitationLedger();
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  draggedStudentId = null;
  draggedFromSeatId = null;
};

window.handleUnassignDrop = async function (e) {
  e.preventDefault();
  if (draggedFromSeatId) {
    try {
      const res = await fetch(`/api/seats/${draggedFromSeatId}/assign?section_id=${currentSectionId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ student_id: null }),
      });
      if (!res.ok) throw new Error("Unassign failed");
      showToast("Student returned to pool", "info");
      await fetchSeats();
      fetchRecitationLedger();
    } catch (err) {
      showToast(err.message, "error");
    }
  }
  draggedStudentId = null;
  draggedFromSeatId = null;
};

window.unassignSeat = async function (seatId) {
  try {
    const res = await fetch(`/api/seats/${seatId}/assign?section_id=${currentSectionId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ student_id: null }),
    });
    if (!res.ok) throw new Error("Unassign failed");
    showToast("Student returned to pool", "info");
    await fetchSeats();
    fetchRecitationLedger();
  } catch (err) {
    showToast(err.message, "error");
  }
};

window.toggleAttendance = async function (seatId) {
  try {
    const res = await fetch(`/api/seats/${seatId}/toggle-attendance?section_id=${encodeURIComponent(currentSectionId)}`, { method: "POST" });
    if (!res.ok) throw new Error("Toggle attendance failed");
    fetchSeats();
    fetchRecitationLedger();
  } catch (err) {
    showToast(err.message, "error");
  }
};

window.loadSeatPreset = async function (presetName) {
  try {
    const res = await fetch(`/api/seats/preset/${presetName}?section_id=${currentSectionId}`, {
      method: "POST",
    });
    if (!res.ok) throw new Error("Failed to load preset layout");
    const updated = await res.json();
    currentSeats = updated;
    renderClassroomDesks();
    renderSectionRoster();
    populateCalibrationInputs();
    showToast(`Classroom layout "${presetName}" loaded`, "success");
  } catch (err) {
    showToast("Error loading preset: " + err.message, "error");
  }
};

// Calibration Inputs
function populateCalibrationInputs() {
  if (!calibSeatSelect) return;
  calibSeatSelect.innerHTML = "";
  currentSeats.forEach((seat) => {
    const opt = document.createElement("option");
    opt.value = seat.id;
    opt.textContent = `${seat.label} — ${seat.student_name}`;
    calibSeatSelect.appendChild(opt);
  });
  updateCalibFields();
}

function updateCalibFields() {
  if (!calibSeatSelect) return;
  const seatId = calibSeatSelect.value;
  const seat = currentSeats.find((s) => s.id === seatId);
  if (!seat) return;
  if (calibXmin) calibXmin.value = seat.x_min;
  if (calibYmin) calibYmin.value = seat.y_min;
  if (calibXmax) calibXmax.value = seat.x_max;
  if (calibYmax) calibYmax.value = seat.y_max;
}

if (calibSeatSelect) {
  calibSeatSelect.addEventListener("change", updateCalibFields);
}

if (btnSaveCalibration) {
  btnSaveCalibration.addEventListener("click", async () => {
    if (!calibSeatSelect) return;
    const seatId = calibSeatSelect.value;
    const seat = currentSeats.find((s) => s.id === seatId);
    if (!seat) return;

    if (calibXmin) seat.x_min = parseFloat(calibXmin.value);
    if (calibYmin) seat.y_min = parseFloat(calibYmin.value);
    if (calibXmax) seat.x_max = parseFloat(calibXmax.value);
    if (calibYmax) seat.y_max = parseFloat(calibYmax.value);

    try {
      const res = await fetch(`/api/seats?section_id=${currentSectionId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seats: currentSeats }),
      });
      if (!res.ok) throw new Error("Failed to save calibration coordinates");
      showToast("Seat calibration coordinates updated", "success");
      fetchSeats();
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}
// ============================================================================
// 5. SESSION HISTORY & CLASS REPORTS
// ============================================================================
let sessionsHistoryList = [];

window.switchReportsSubTab = function (tab) {
  const subHistory = document.getElementById("subview-sessions-history");
  const subClass = document.getElementById("subview-class-roster");
  const btnSessions = document.getElementById("tab-btn-sessions");
  const btnClass = document.getElementById("tab-btn-class");

  if (tab === "sessions") {
    if (subHistory) subHistory.classList.remove("hidden");
    if (subClass) subClass.classList.add("hidden");
    if (btnSessions) btnSessions.className = "text-sm font-bold text-brand-700 border-b-2 border-brand-700 pb-2 transition";
    if (btnClass) btnClass.className = "text-sm font-medium text-slate-500 hover:text-slate-800 pb-2 transition";
    fetchSessionsHistory();
  } else {
    if (subHistory) subHistory.classList.add("hidden");
    if (subClass) subClass.classList.remove("hidden");
    if (btnClass) btnClass.className = "text-sm font-bold text-brand-700 border-b-2 border-brand-700 pb-2 transition";
    if (btnSessions) btnSessions.className = "text-sm font-medium text-slate-500 hover:text-slate-800 pb-2 transition";
    renderClassReports();
  }
};

async function fetchSessionsHistory() {
  if (role !== "teacher") return;
  try {
    const generation = authGeneration;
    const secFilter = document.getElementById("history-section-filter");
    const dateFilter = document.getElementById("history-date-filter");
    const secVal = secFilter ? secFilter.value : "";
    const dateVal = dateFilter ? dateFilter.value : "";

    const params = new URLSearchParams();
    if (secVal) params.append("section_id", secVal);
    if (dateVal) params.append("date", dateVal);

    const qs = params.toString() ? `?${params.toString()}` : "";
    const res = await fetch(`/api/sessions${qs}`);
    if (!res.ok) return;
    const sessions = await res.json();
    if (generation !== authGeneration || role !== "teacher") return;
    sessionsHistoryList = sessions;
    renderSessionsHistory();

    const countBadge = document.getElementById("history-count-badge");
    if (countBadge) {
      countBadge.textContent = `${sessionsHistoryList.length} Session${sessionsHistoryList.length === 1 ? "" : "s"}`;
    }
  } catch (e) {
    console.error("Failed to fetch sessions history:", e);
  }
}

window.clearHistoryDateFilter = function () {
  const dateFilter = document.getElementById("history-date-filter");
  if (dateFilter) dateFilter.value = "";
  fetchSessionsHistory();
};

window.confirmClearAllSessions = async function () {
  const secFilter = document.getElementById("history-section-filter");
  const secVal = secFilter ? secFilter.value : "";
  const secName = secVal ? (sectionsList.find((s) => s.id === secVal)?.name || "this section") : "all sections";

  const confirmed = await showConfirmModal({
    title: "Clear Session History",
    message: `Are you sure you want to permanently clear past session logs and recitation records for ${secName}? This action cannot be undone.`,
    confirmText: "Clear All History",
    cancelText: "Cancel",
    isDanger: true,
  });
  if (!confirmed) return;

  try {
    const url = secVal ? `/api/sessions?section_id=${secVal}` : "/api/sessions";
    const res = await fetch(url, { method: "DELETE" });
    if (!res.ok) throw new Error("Failed to clear session history");
    const data = await res.json();
    showToast(`Successfully cleared ${data.deleted_count || 0} session records`, "success");
    fetchSessionsHistory();
    fetchSections();
  } catch (err) {
    showToast(err.message, "error");
  }
};

function renderSessionsHistory() {
  const tbody = document.getElementById("sessions-history-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  if (sessionsHistoryList.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="px-4 py-8 text-center text-slate-400 text-xs">No past recitation sessions recorded matching current filter.</td></tr>`;
    return;
  }

  sessionsHistoryList.forEach((sess) => {
    const startDate = new Date(sess.started_at);
    const dateStr = startDate.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    const timeStr = startDate.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    const isEnded = Boolean(sess.ended_at);

    let durationText = "--";
    if (sess.ended_at) {
      const durMs = new Date(sess.ended_at) - startDate;
      const durMins = Math.max(1, Math.round(durMs / 60000));
      durationText = `${durMins} min`;
    } else {
      durationText = "In Progress";
    }

    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-50 transition";
    tr.innerHTML = `
      <td class="px-4 py-3">
        <div class="font-bold text-slate-900">${dateStr}</div>
        <div class="text-xs text-slate-500">${timeStr}</div>
      </td>
      <td class="px-4 py-3">
        <div class="font-semibold text-slate-800">${sess.title}</div>
        <div class="text-xs text-slate-400 font-mono">${sess.id}</div>
      </td>
      <td class="px-4 py-3">
        <span class="px-2 py-0.5 rounded text-xs font-semibold bg-brand-50 text-brand-800 border border-brand-200">
          ${sess.section_name || sess.section_id || "All"}
        </span>
      </td>
      <td class="px-4 py-3 text-center font-bold text-slate-800">${sess.total_raises || 0}</td>
      <td class="px-4 py-3 text-center font-bold text-emerald-700">${sess.total_points || 0}</td>
      <td class="px-4 py-3 text-center">
        <span class="px-2 py-0.5 rounded text-xs font-semibold ${isEnded ? 'bg-slate-100 text-slate-600' : 'bg-emerald-100 text-emerald-800 animate-pulse'}">
          ${isEnded ? "Completed" : "Active"}
        </span>
      </td>
      <td class="px-4 py-3 text-right">
        <div class="flex justify-end space-x-1.5">
          <button onclick="openSessionDetailsModal('${sess.id}')" class="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium px-2.5 py-1 rounded transition border border-slate-200">
            View Breakdown
          </button>
          <button onclick="deleteClassSession('${sess.id}')" class="text-xs bg-rose-50 hover:bg-rose-100 text-rose-700 font-medium px-2.5 py-1 rounded transition border border-rose-200">
            Delete
          </button>
        </div>
      </td>
    `;
    labelResponsiveCells(tr, ["Date & time", "Session", "Section", "Raises", "Points", "Status", "Actions"]);
    tbody.appendChild(tr);
  });
}

window.deleteClassSession = async function (sessionId) {
  const confirmed = await showConfirmModal({
    title: "Delete Session Record",
    message: "Are you sure you want to permanently delete this class session record?",
    confirmText: "Delete",
    cancelText: "Cancel",
    isDanger: true,
  });
  if (!confirmed) return;

  try {
    const res = await fetch(`/api/sessions/${sessionId}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Failed to delete session");
    if (activeSession && activeSession.id === sessionId) {
      activeSession = null;
      updateSessionUI();
    }
    showToast("Session record deleted", "success");
    fetchSessionsHistory();
  } catch (err) {
    showToast("Error deleting session: " + err.message, "error");
  }
};

window.openSessionDetailsModal = async function (sessionId) {
  if (role !== "teacher") return;
  const generation = authGeneration;
  try {
    const res = await fetch(`/api/sessions/${sessionId}`);
    if (!res.ok) throw new Error("Failed to load session details");
    const data = await res.json();
    if (generation !== authGeneration || role !== "teacher") return;

    document.getElementById("modal-session-title").textContent = data.title;
    document.getElementById("modal-session-subtitle").textContent = `${data.section_name || data.section_id || ""} • Started: ${new Date(data.started_at).toLocaleString()}`;
    document.getElementById("modal-session-raises").textContent = data.total_raises || 0;
    document.getElementById("modal-session-points").textContent = data.total_points || 0;
    document.getElementById("modal-session-students").textContent = (data.ledger || []).filter((s) => s.total_raises > 0).length;

    const tbody = document.getElementById("modal-session-tbody");
    tbody.innerHTML = "";
    (data.ledger || []).forEach((st) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="px-3 py-2 font-mono text-xs text-slate-600">${st.label}</td>
        <td class="px-3 py-2 font-bold text-slate-900">${st.student_name}</td>
        <td class="px-3 py-2 text-center font-semibold text-slate-800">${st.total_raises}</td>
        <td class="px-3 py-2 text-center font-bold text-emerald-700">${st.total_points}</td>
      `;
      labelResponsiveCells(tr, ["Desk", "Student", "Raises", "Points"]);
      tbody.appendChild(tr);
    });

    document.getElementById("modal-session-download-btn").onclick = () => downloadSessionCSV(sessionId);
    document.getElementById("modal-session-details").classList.remove("hidden");
  } catch (err) {
    showToast(err.message, "error");
  }
};

window.closeSessionDetailsModal = function () {
  document.getElementById("modal-session-details").classList.add("hidden");
};

async function downloadTeacherCsv(url, filename) {
  if (role !== "teacher") return;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error("CSV download failed");
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  } catch (error) {
    showToast(error.message, "error");
  }
}

window.downloadSessionCSV = function (sessionId) {
  return downloadTeacherCsv(`/api/exports/session-report.csv?session_id=${sessionId}`, `session-report-${sessionId}.csv`);
};

async function renderClassReports() {
  await fetchClassGrades();
}

window.fetchClassGrades = async function () {
  if (!reportsTbody || role !== "teacher") return;
  const generation = authGeneration;
  reportsTbody.innerHTML = "";

  const secId = currentReportsSectionId || currentSectionId;
  const currentSec = sectionsList.find((s) => s.id === secId);
  const repTitle = document.getElementById("reports-class-title");
  if (repTitle && currentSec) {
    repTitle.textContent = `${currentSec.name} — Participation & Recitation Report (${currentSec.subject})`;
  }

  const targetInput = document.getElementById("grading-target-raises");
  const weightInput = document.getElementById("grading-weight");
  const targetVal = targetInput ? Math.max(1, parseInt(targetInput.value, 10) || 5) : 5;
  const weightVal = weightInput ? Math.max(1, parseFloat(weightInput.value) || 100) : 100;

  try {
    const res = await fetch(`/api/analytics/grades?section_id=${secId}&target=${targetVal}&weight=${weightVal}`);
    if (!res.ok) {
      const details = await res.json().catch(() => ({}));
      throw new Error(details.detail || `Failed to load grades (${res.status})`);
    }
    const grades = await res.json();
    if (generation !== authGeneration || role !== "teacher") return;

    const countEl = document.getElementById("reports-student-count");
    if (countEl) countEl.textContent = `${grades.length} Students Enrolled`;

    if (grades.length === 0) {
      reportsTbody.innerHTML = `
        <tr><td colspan="4" class="px-4 py-8 text-center text-slate-400 text-xs">No students registered in this class section.</td></tr>
      `;
      return;
    }

    grades.forEach((st) => {
      const initials = getStudentInitials(st.student_name);
      const color = getStudentAvatarColor(st.student_name);
      const avatarHtml = `<div class="w-8 h-8 rounded-full ${color.bg} ${color.text} font-bold text-xs flex items-center justify-center shrink-0 border ${color.border}">${initials}</div>`;

      const tr = document.createElement("tr");
      tr.className = "hover:bg-slate-50 transition";
      tr.innerHTML = `
        <td class="px-4 py-3">
          <div class="flex items-center space-x-2.5">
            ${avatarHtml}
            <div>
              <div class="font-bold text-slate-900">${st.student_name}</div>
              <div class="text-xs text-slate-400 font-mono">${st.student_id_number || "--"}</div>
            </div>
          </div>
        </td>
        <td class="px-4 py-3 font-semibold text-slate-700 text-xs">
          <span class="px-2 py-0.5 rounded bg-slate-100 border border-slate-200">${st.assigned_seat_label || "Unassigned"}</span>
        </td>
        <td class="px-4 py-3 text-center font-bold text-slate-800">${st.total_raises || 0}</td>
        <td class="px-4 py-3 text-center font-bold text-emerald-700">${st.total_points || 0}</td>
      `;
      labelResponsiveCells(tr, ["Student", "Assigned desk", "Total raises", "Points awarded"]);
      reportsTbody.appendChild(tr);
    });
  } catch (err) {
    console.error("Failed to load grades:", err);
    showToast("Error calculating grades: " + err.message, "error");
  }
};

window.downloadClassReportCSV = function () {
  const secId = currentReportsSectionId || currentSectionId;
  return downloadTeacherCsv(`/api/exports/class-report.csv?section_id=${secId}`, `class-report-${secId}.csv`);
};

window.downloadGradesCSV = function () {
  const secId = currentReportsSectionId || currentSectionId;
  const targetInput = document.getElementById("grading-target-raises");
  const weightInput = document.getElementById("grading-weight");
  const targetVal = targetInput ? Math.max(1, parseInt(targetInput.value, 10) || 5) : 5;
  const weightVal = weightInput ? Math.max(1, parseFloat(weightInput.value) || 100) : 100;
  return downloadTeacherCsv(`/api/exports/grades.csv?section_id=${secId}&target=${targetVal}&weight=${weightVal}`, `grades-${secId}.csv`);
};

// ============================================================================
// 6. CLASS SESSION LIFECYCLE
// ============================================================================
function updateSessionUI() {
  const badge = document.getElementById("live-session-status-badge");
  const desc = document.getElementById("live-session-desc");
  const bannerStart = document.getElementById("btn-banner-start");
  const bannerStop = document.getElementById("btn-banner-stop");
  const titleHeader = document.getElementById("live-class-banner-title");
  const accessPanel = document.getElementById("guest-access-panel");
  if (accessPanel) {
    const showAccess = role === "teacher" && Boolean(activeSession);
    accessPanel.classList.toggle("hidden", !showAccess);
    accessPanel.classList.toggle("flex", showAccess);
    if (showAccess && guestAccessSessionId !== activeSession.id) fetchGuestAccess();
  }

  if (activeSession) {
    if (sessionStatusLabel) sessionStatusLabel.textContent = `Active: ${activeSession.title}`;
    if (btnStartSession) btnStartSession.classList.add("hidden");
    if (btnStopSession) btnStopSession.classList.remove("hidden");

    if (badge) {
      badge.className = "px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 animate-pulse";
      badge.textContent = role === "guest" ? "Live session" : `Active Class Session: ${activeSession.title}`;
    }
    if (desc) {
      const section = sectionsList.find((item) => item.id === activeSession.section_id);
      desc.textContent = role === "guest"
        ? `${section?.name || "Class"}${section?.room ? ` • ${section.room}` : ""} · Live hand-raise order`
        : `Started: ${new Date(activeSession.started_at).toLocaleTimeString()} • Tracking student participation and hand raises in real-time.`;
    }
    if (bannerStart) bannerStart.classList.add("hidden");
    if (bannerStop) bannerStop.classList.remove("hidden");
    if (titleHeader) titleHeader.textContent = activeSession.title;
  } else {
    if (sessionStatusLabel) sessionStatusLabel.textContent = "No Active Class Session";
    if (btnStartSession) btnStartSession.classList.remove("hidden");
    if (btnStopSession) btnStopSession.classList.add("hidden");

    if (badge) {
      badge.className = "px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-100 text-slate-600";
      badge.textContent = "No Active Session (Standby)";
    }
    if (desc) desc.textContent = role === "teacher"
      ? 'Click "Start Class Session" to begin tracking student participation and hand raises.'
      : "Waiting for the teacher to start the class session.";
    if (bannerStart) bannerStart.classList.remove("hidden");
    if (bannerStop) bannerStop.classList.add("hidden");
    if (titleHeader) titleHeader.textContent = "Live Class Session";
  }
  updateSectionUI();
}

async function fetchGuestAccess() {
  if (role !== "teacher" || !activeSession) return;
  const sessionId = activeSession.id;
  guestAccessSessionId = sessionId;
  guestAccessCode = "";
  const codeElement = document.getElementById("guest-access-code");
  if (codeElement) codeElement.textContent = "Loading…";
  try {
    const response = await fetch("/api/sessions/active/guest-access");
    if (!response.ok) throw new Error("Unable to load guest viewing code");
    const data = await response.json();
    if (role === "teacher" && activeSession?.id === sessionId) {
      guestAccessCode = data.code;
      if (codeElement) codeElement.textContent = data.code;
    }
  } catch (error) {
    guestAccessSessionId = "";
    if (codeElement) codeElement.textContent = "Unavailable";
    console.error(error);
  }
}

window.copyGuestAccessCode = async function () {
  if (!guestAccessCode) return;
  try {
    await navigator.clipboard.writeText(guestAccessCode);
    showToast("Viewing code copied", "success");
  } catch (error) {
    showToast("Select the code to copy it", "warning");
  }
};

window.startClassSession = async function () {
  if (activeSession || sessionMutationInProgress) return;
  sessionMutationInProgress = true;
  const current = sectionsList.find((s) => s.id === currentSectionId);
  const className = current ? current.name : "Class";
  const defaultTitle = `${className} Session - ${new Date().toLocaleDateString()}`;

  try {
    const res = await fetch("/api/sessions/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: defaultTitle, section_id: currentSectionId }),
    });
    if (!res.ok) {
      const errJson = await res.json().catch(() => ({}));
      throw new Error(errJson.detail || "Failed to start class session");
    }
    const data = await res.json();
    activeSession = data;
    updateSessionUI();
    showToast(`Class session started: ${data.title}`, "success");
    fetchRecitationLedger();
    fetchSessionsHistory();
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    sessionMutationInProgress = false;
  }
};

window.stopClassSession = async function () {
  if (!activeSession || sessionMutationInProgress) return;
  const confirmed = await showConfirmModal({
    title: "End Class Session",
    message: "Are you sure you want to end the active class session? All student raises and participation points will be permanently saved in Session History.",
    confirmText: "End Class Session",
    cancelText: "Continue Session",
    isDanger: false,
  });
  if (!confirmed) return;

  sessionMutationInProgress = true;
  try {
    const res = await fetch("/api/sessions/stop", { method: "POST" });
    if (!res.ok) throw new Error("Failed to end session");
    activeSession = null;
    updateSessionUI();
    showToast("Class session concluded and saved", "info");
    fetchRecitationLedger();
    fetchSessionsHistory();
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    sessionMutationInProgress = false;
  }
};

// ============================================================================
// 7. WEBSOCKETS (Events Only — Video handled by Camera Node)
// ============================================================================

// Edge Node Status
window.fetchEdgeStatus = async function() {
  try {
    const res = await fetch("/api/edge/status");
    const data = await res.json();
    const dot = document.getElementById("edge-status-dot");
    const text = document.getElementById("edge-status-text");
    const desc = document.getElementById("edge-status-desc");
    const badge = document.getElementById("edge-connection-badge");
    if (data.connected_nodes > 0) {
      if (dot) { dot.className = "w-3 h-3 rounded-full bg-emerald-500 animate-pulse"; }
      if (text) { text.textContent = `${data.connected_nodes} Camera Node(s) Connected`; }
      if (desc) { desc.textContent = `Receiving live gesture events. Podium queue has ${data.podium_active} active entries.`; }
      if (badge) { badge.className = "px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-100 text-emerald-700 border border-emerald-200"; badge.textContent = `${data.connected_nodes} Node(s)`; }
    } else {
      if (dot) { dot.className = "w-3 h-3 rounded-full bg-slate-300 animate-pulse"; }
      if (text) { text.textContent = "No Camera Nodes Connected"; }
      if (desc) { desc.textContent = "Start the Camera Node desktop app on the classroom PC to begin receiving live gesture events."; }
      if (badge) { badge.className = "px-2 py-0.5 rounded-full text-[10px] font-bold bg-slate-100 text-slate-500 border border-slate-200"; badge.textContent = "No Camera Nodes"; }
    }
  } catch (e) {
    console.error("Edge status fetch failed:", e);
  }
};



function initEventsWebSocket() {
  if (!teacherToken && !guestToken) return;
  if (socketReconnectTimer) clearTimeout(socketReconnectTimer);
  if (eventsSocket) { eventsSocket.onclose = null; eventsSocket.close(); }
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProto}//${location.host}/ws/events`;
  const ws = new WebSocket(wsUrl);
  eventsSocket = ws;

  ws.onopen = () => ws.send(JSON.stringify(teacherToken
    ? { teacher_token: teacherToken }
    : { guest_token: guestToken }));

  ws.onmessage = (msg) => {
    try {
      const data = JSON.parse(msg.data);
      handleSocketMessage(data);
    } catch (e) {
      console.error(e);
    }
  };

  ws.onclose = (event) => {
    if (eventsSocket !== ws) return;
    eventsSocket = null;
    if (event.code === 1008 && role === "guest") {
      enterGuestMode();
      showToast("Viewing code expired. Ask your teacher for the current code.", "warning");
    } else if (teacherToken || guestToken) {
      socketReconnectTimer = setTimeout(initEventsWebSocket, 2000);
    }
  };
}

function handleSocketMessage(msg) {
  switch (msg.type) {
    case "INITIAL_STATE":
      if (msg.payload.sections) {
        sectionsList = msg.payload.sections;
        populateSectionDropdown();
        renderSectionsCards();
      }
      if (msg.payload.active_session) {
        activeSession = msg.payload.active_session;
      } else {
        activeSession = null;
      }
      updateSessionUI();
      if (role === "teacher") {
        // The initial socket snapshot contains seats for every section owned by
        // this teacher. Load only the selected section through the scoped API;
        // showing the unfiltered snapshot causes other desks to flash briefly.
        if (activeSession?.section_id && sectionsList.some((section) => section.id === activeSession.section_id)) {
          currentSectionId = activeSession.section_id;
          currentReportsSectionId = currentSectionId;
          currentLedger = [];
          populateSectionDropdown();
          renderSectionsCards();
          updateSectionUI();
        }
        currentSeats = [];
        currentStudents = [];
        renderClassroomDesks();
        renderSectionRoster();
        renderRecitationLedger();
      }
      if (role === "teacher") fetchSeats();
      fetchRecitationLedger();
      if (role === "teacher") fetchSessionsHistory();
      break;

    case "GESTURE_EVENT":
    case "POINT_AWARDED":
    case "EVENT_DISMISSED":
      // Ultra-low latency: if ledger attached in WebSocket message, update in-memory instantly (<1ms)!
      if (role === "guest" && msg.ledger && Array.isArray(msg.ledger)) {
        currentLedger = msg.ledger;
        renderRecitationLedger();
      } else {
        fetchRecitationLedger();
      }
      break;

    case "SESSION_STARTED":
      activeSession = msg.payload;
      updateSessionUI();
      if (role === "guest" && msg.ledger) {
        currentLedger = msg.ledger;
        renderRecitationLedger();
      } else {
        fetchRecitationLedger();
      }
      if (role === "teacher") fetchSessionsHistory();
      break;

    case "SESSION_STOPPED":
      if (role === "guest") {
        enterGuestMode();
        showToast("The class session ended. Ask your teacher for a new viewing code.", "info");
        break;
      }
      activeSession = null;
      updateSessionUI();
      if (role === "guest" && msg.ledger) {
        currentLedger = msg.ledger;
        renderRecitationLedger();
      } else {
        fetchRecitationLedger();
      }
      if (role === "teacher") fetchSessionsHistory();
      break;

    case "SEATS_UPDATED":
    case "STUDENTS_UPDATED":
      if (role === "teacher") fetchSeats();
      fetchRecitationLedger();
      break;

    case "SECTIONS_UPDATED":
      fetchSections();
      break;

    case "CAMERA_SETTINGS_CHANGED":
      if (msg.settings) {
        currentCameraSettings = msg.settings;
        syncCameraSettingsUI();
      }
      break;
  }
}

// ============================================================================
// 8. CAMERA SETTINGS CONTROLLER
// ============================================================================
let currentCameraSettings = {
  show_skeleton: false,
  model_name: "yolo11s-pose.pt",
};
let availableAiModels = [];

window.fetchCameraSettings = async function () {
  try {
    const res = await fetch("/api/camera/settings");
    if (!res.ok) return;
    const data = await res.json();
    currentCameraSettings = data.settings || currentCameraSettings;
    availableAiModels = data.available_models || [];
    renderCameraSettingsUI(data.connected_nodes || 0);
  } catch (e) {
    console.error("Failed to fetch camera settings:", e);
  }
};

function renderCameraSettingsUI(connectedNodes = 0) {
  // 1. Badge
  const nodeBadge = document.getElementById("cam-settings-node-badge");
  if (nodeBadge) {
    if (connectedNodes > 0) {
      nodeBadge.className = "px-2.5 py-1 rounded-full text-xs font-bold bg-emerald-100 text-emerald-800";
      nodeBadge.textContent = `${connectedNodes} Camera Node(s) Active`;
    } else {
      nodeBadge.className = "px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-100 text-slate-500";
      nodeBadge.textContent = "No Camera Nodes Connected";
    }
  }

  syncCameraSettingsUI();
  renderAiModelCards();
}

function syncCameraSettingsUI() {
  // Skeleton checkbox
  const toggle = document.getElementById("cam-toggle-skeleton");
  if (toggle) {
    toggle.checked = Boolean(currentCameraSettings.show_skeleton);
  }

}

function renderAiModelCards() {
  const container = document.getElementById("ai-model-cards-container");
  if (!container) return;

  if (availableAiModels.length === 0) {
    availableAiModels = [
      { id: "yolo11s-pose.pt", name: "YOLO11 Small (Pose)", desc: "Recommended — high precision hand-raise detection", recommended: true },
      { id: "yolov8n-pose.pt", name: "YOLOv8 Nano (Pose)", desc: "Ultra-fast & lightweight (ideal for low-spec PCs)", recommended: false },
      { id: "yolov8s-pose.pt", name: "YOLOv8 Small (Pose)", desc: "Balanced speed and accuracy", recommended: false },
      { id: "yolov8m-pose.pt", name: "YOLOv8 Medium (Pose)", desc: "Maximum accuracy (requires dedicated GPU)", recommended: false },
    ];
  }

  container.innerHTML = "";
  availableAiModels.forEach((m) => {
    const isSelected = currentCameraSettings.model_name === m.id;
    const card = document.createElement("div");
    card.className = `p-4 rounded-xl border-2 transition cursor-pointer flex flex-col justify-between space-y-2 ${
      isSelected
        ? "border-brand-700 bg-brand-50/50 shadow-xs"
        : "border-slate-200 bg-white hover:border-slate-300"
    }`;
    card.onclick = () => selectAiModel(m.id);

    card.innerHTML = `
      <div class="flex items-start justify-between">
        <div>
          <div class="flex items-center space-x-1.5">
            <span class="text-sm font-bold text-slate-900">${m.name}</span>
            ${m.recommended ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-100 text-amber-900">Recommended</span>' : ""}
          </div>
          <p class="text-xs text-slate-500 mt-0.5">${m.desc}</p>
        </div>
        <div class="w-4 h-4 rounded-full border-2 flex items-center justify-center ${isSelected ? "border-brand-700 bg-brand-700" : "border-slate-300"}">
          ${isSelected ? '<div class="w-1.5 h-1.5 rounded-full bg-white"></div>' : ""}
        </div>
      </div>
      <div class="text-[11px] font-mono text-slate-400">${m.id}</div>
    `;
    container.appendChild(card);
  });
}

window.selectAiModel = async function (modelId) {
  currentCameraSettings.model_name = modelId;
  renderAiModelCards();
  await pushCameraSettings({ model_name: modelId });
};

window.toggleSkeletonOverlay = async function (checked) {
  currentCameraSettings.show_skeleton = checked;
  await pushCameraSettings({ show_skeleton: checked });
};

window.saveCameraSettings = async function () {
  await pushCameraSettings({ show_skeleton: currentCameraSettings.show_skeleton,
    model_name: currentCameraSettings.model_name }, true);
};

async function pushCameraSettings(partial, notify = false) {
  try {
    const res = await fetch("/api/camera/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(partial),
    });
    if (!res.ok) throw new Error("Failed to update camera settings");
    const data = await res.json();
    currentCameraSettings = data.settings;
    if (notify) {
      showToast("Camera settings synced with all Camera Nodes", "success");
    }
  } catch (e) {
    showToast("Error updating camera settings: " + e.message, "error");
  }
}

// Initial Boot
async function bootApp() {
  startClock();
  const authenticated = await restoreAuth();
  if (authenticated) {
    showView("class");
    await loadAuthorizedData();
    initEventsWebSocket();
  }
  fetchEdgeStatus();
  setInterval(fetchEdgeStatus, 15000);
  setInterval(reconcileLiveSession, 5000);
}
bootApp();

