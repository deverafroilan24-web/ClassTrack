"use strict";
const byId = id => document.getElementById(id);
let token = sessionStorage.getItem("classtrack.adminToken") || "";
let accounts = [];
let editingId = null;
let deleting = null;
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = path => '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' + path + '"/></svg>';
function message(id, text, error = false) {
  const element = byId(id);
  element.textContent = text;
  element.className = "notice" + (error ? " error" : "");
  element.hidden = !text;
}
function signedOut() {
  token = "";
  sessionStorage.removeItem("classtrack.adminToken");
  byId("view-teachers").hidden = true;
  byId("admin-logout").hidden = true;
  byId("welcome-portal").hidden = false;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
}
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"Content-Type": "application/json", ...(token ? {"X-Teacher-Token": token} : {})}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") {
      signedOut();
      message("teacher-login-error", "Your session expired. Please sign in again.", true);
    }
    const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg).join("; ") : data.detail;
    throw new Error(detail || "Unable to complete the request. Please try again.");
  }
  return data;
}
async function loadAccounts() {
  accounts = await api("/api/admin/teachers");
  byId("teacher-count").textContent = accounts.length + (accounts.length === 1 ? " account" : " accounts");
  byId("accounts-empty").hidden = accounts.length > 0;
  byId("teachers-table-body").innerHTML = accounts.map((a, index) => '<tr>' +
    '<td data-label="Teacher"><strong>' + escapeHtml(a.name) + '</strong><small>' + escapeHtml(a.login_id) + '</small></td>' +
    '<td data-label="Department">' + escapeHtml(a.department || "—") + '</td>' +
    '<td data-label="Status"><span class="badge ' + (a.is_active && a.has_password ? "enabled" : "") + '">' +
    (a.is_active ? (a.has_password ? "Enabled" : "Password required") : "Disabled") + '</span></td>' +
    '<td><div class="account-actions"><button class="button secondary" data-edit="' + index + '">' +
    icon("m16 3 5 5-12 12H4v-5L16 3ZM14 5l5 5") + 'Edit</button><button class="button danger" data-delete="' + index + '">' +
    icon("M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7") + 'Delete</button></div></td></tr>').join("");
}
async function signedIn() {
  byId("welcome-portal").hidden = true;
  byId("view-teachers").hidden = false;
  byId("admin-logout").hidden = false;
  try { await loadAccounts(); } catch (error) { message("teacher-admin-message", error.message, true); }
}
byId("admin-login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = byId("teacher-login-submit");
  if (button.disabled) return;
  button.disabled = true;
  message("teacher-login-error", "");
  try {
    const data = await api("/api/auth/login", {method: "POST", body: JSON.stringify({login_id: byId("teacher-id-input").value.trim(), password: byId("teacher-password-input").value})});
    if (!data.is_admin) throw new Error("Use an administrator account here. Teachers sign in on the classroom page.");
    token = data.token;
    sessionStorage.setItem("classtrack.adminToken", token);
    byId("teacher-password-input").value = "";
    await signedIn();
  } catch (error) { message("teacher-login-error", error.message, true); }
  finally { button.disabled = false; }
});
byId("show-password").addEventListener("change", event => { byId("teacher-password-input").type = event.target.checked ? "text" : "password"; });
byId("admin-logout").addEventListener("click", signedOut);
function editAccount(account = null) {
  editingId = account?.id || null;
  byId("teacher-create-form").reset();
  byId("editor-title").textContent = account ? "Edit teacher" : "Add teacher";
  byId("teacher-create-submit").textContent = account ? "Save changes" : "Add teacher";
  byId("new-teacher-name").value = account?.name || "";
  byId("new-teacher-department").value = account?.department || "";
  byId("new-teacher-id").value = account?.login_id || "";
  byId("new-teacher-password").required = !account?.has_password;
  byId("password-hint").textContent = account?.has_password ? "Leave blank to keep the current password." : "At least 12 characters.";
  byId("teacher-active-field").hidden = !account;
  byId("new-teacher-active").checked = account ? !!account.is_active : true;
  message("editor-error", "");
  byId("teacher-editor").showModal();
  byId("new-teacher-name").focus();
}
byId("add-teacher").addEventListener("click", () => editAccount());
byId("editor-close").addEventListener("click", () => byId("teacher-editor").close());
byId("teacher-edit-cancel").addEventListener("click", () => byId("teacher-editor").close());
byId("teachers-table-body").addEventListener("click", event => {
  const edit = event.target.closest("[data-edit]");
  const remove = event.target.closest("[data-delete]");
  if (edit) editAccount(accounts[Number(edit.dataset.edit)]);
  if (remove) {
    deleting = accounts[Number(remove.dataset.delete)];
    byId("delete-description").textContent = "Delete " + deleting.name + " (" + deleting.login_id + ")?";
    message("delete-error", "");
    byId("delete-dialog").showModal();
    byId("delete-cancel").focus();
  }
});
byId("teacher-create-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = byId("teacher-create-submit");
  if (button.disabled) return;
  button.disabled = true;
  const wasEditing = !!editingId;
  try {
    const payload = {name: byId("new-teacher-name").value.trim(), department: byId("new-teacher-department").value.trim(), login_id: byId("new-teacher-id").value.trim(), password: byId("new-teacher-password").value};
    if (wasEditing) {
      payload.is_active = byId("new-teacher-active").checked;
      if (!payload.password) delete payload.password;
    }
    await api("/api/admin/teachers" + (wasEditing ? "/" + encodeURIComponent(editingId) : ""), {method: wasEditing ? "PUT" : "POST", body: JSON.stringify(payload)});
    byId("teacher-editor").close();
    byId("new-teacher-password").value = "";
    message("teacher-admin-message", wasEditing ? "Credentials updated." : "Teacher account created.");
    await loadAccounts();
  } catch (error) { message(byId("teacher-editor").open ? "editor-error" : "teacher-admin-message", error.message, true); }
  finally { button.disabled = false; }
});
byId("delete-cancel").addEventListener("click", () => byId("delete-dialog").close());
byId("delete-confirm").addEventListener("click", async () => {
  const button = byId("delete-confirm");
  if (button.disabled || !deleting) return;
  button.disabled = true;
  try {
    await api("/api/admin/teachers/" + encodeURIComponent(deleting.id), {method: "DELETE"});
    byId("delete-dialog").close();
    message("teacher-admin-message", "Teacher account deleted.");
    deleting = null;
    await loadAccounts();
  } catch (error) { message(byId("delete-dialog").open ? "delete-error" : "teacher-admin-message", error.message, true); }
  finally { button.disabled = false; }
});
(async () => {
  // Carry an existing administrator session over from the previous combined page.
  token = token || sessionStorage.getItem("classtrack.teacherToken") || "";
  if (!token) return;
  try {
    const session = await api("/api/auth/session");
    if (!session.is_admin) { token = ""; return; }
    sessionStorage.setItem("classtrack.adminToken", token);
    sessionStorage.removeItem("classtrack.teacherToken");
    await signedIn();
  } catch { signedOut(); }
})();
