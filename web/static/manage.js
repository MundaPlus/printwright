// Manage parts and projects, and the app settings (web/manage.py).
// Import… in the parts panel turns an STL or G-code file into a part (web/importer.py).
// Part rows get a "⋯" menu: rename, move, older versions, delete.
// The project bar gets a "⋯" menu: rename, delete. The gear opens the settings.
// The first-run setup shows when printwright.json still has the example printer URL.
import { state, api, toast, esc, hooks, refreshParts, loadProjectList, openProject, selectPart } from "./app.js";

const $ = (id) => document.getElementById(id);
const UNSORTED = "unsorted";
const PLACEHOLDER_URL = "http://printer.local"; // the example URL: the setup screen asks for the real one
let info = { git: false, autocommit: false, needs_setup: false };
let builds = {};

// ---------- Dialog ----------
// dialog() resolves with the field value (or true), or null on Cancel, Escape or a click outside.
// field: {value, placeholder, max, check(v) -> error text}. submit(v, form): an async server call;
// its error shows in the dialog and the dialog stays open.
function dialog({ title, html = "", field = null, ok = "OK", okClass = "primary", cancel = "Cancel", wide = false, onOpen, submit }) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "overlay manage-overlay";
    wrap.innerHTML = `<form class="card modal${wide ? " wide" : ""}" novalidate>
      <h2>${esc(title)}</h2>
      <div class="dlg-body">${html}</div>
      ${field ? `<input class="field" name="value" type="text" autocomplete="off" spellcheck="false">` : ""}
      <p class="error" hidden></p>
      <div class="modal-actions">
        ${cancel ? `<button type="button" class="btn ghost" data-act="cancel">${esc(cancel)}</button>` : ""}
        ${ok ? `<button type="submit" class="btn ${okClass}">${esc(ok)}</button>` : ""}
      </div></form>`;
    document.body.appendChild(wrap);
    const form = wrap.querySelector("form");
    const input = form.querySelector("input[name=value]");
    const err = form.querySelector(".error");
    const close = (v) => { wrap.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") close(null); };
    document.addEventListener("keydown", onKey);
    wrap.addEventListener("mousedown", (e) => { if (e.target === wrap) close(null); });
    form.querySelector("[data-act=cancel]")?.addEventListener("click", () => close(null));
    if (input) {
      Object.assign(input, { value: field.value || "", placeholder: field.placeholder || "", maxLength: field.max || 80 });
      setTimeout(() => { input.focus(); input.select(); }, 0);
    } else setTimeout(() => form.querySelector("button[type=submit]")?.focus(), 0);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const value = input ? input.value.trim() : true;
      if (field?.check) {
        const why = field.check(value);
        if (why) { err.textContent = why; err.hidden = false; return; }
      }
      if (!submit) return close(value);
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      try { await submit(value, form); close(value); }
      catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; }
    });
    onOpen?.(form, close);
  });
}

const confirmDanger = (title, html, ok) => dialog({ title, html, ok, okClass: "danger" });

// ---------- Popup menu ----------
function menu(anchor, items) {
  document.querySelector(".pop-menu")?.remove();
  const m = document.createElement("div");
  m.className = "pop-menu card";
  m.setAttribute("role", "menu");
  m.innerHTML = items.filter(Boolean).map((it, i) =>
    `<button type="button" role="menuitem" class="${it.danger ? "danger-text" : ""}" data-i="${i}" ${it.disabled ? "disabled" : ""}>${esc(it.label)}</button>`).join("");
  document.body.appendChild(m);
  const list = items.filter(Boolean);
  const r = anchor.getBoundingClientRect();
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = `${Math.max(8, Math.min(r.right - w, innerWidth - w - 8))}px`;
  m.style.top = `${r.bottom + 4 + h > innerHeight - 8 ? Math.max(8, r.top - h - 4) : r.bottom + 4}px`;
  const close = () => { m.remove(); document.removeEventListener("mousedown", outside, true); document.removeEventListener("keydown", onKey); };
  const outside = (e) => { if (!m.contains(e.target) && e.target !== anchor) close(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("mousedown", outside, true);
  document.addEventListener("keydown", onKey);
  m.addEventListener("click", (e) => {
    const b = e.target.closest("[data-i]");
    if (!b) return;
    close();
    list[+b.dataset.i].run();
  });
  m.querySelector("button:not([disabled])")?.focus();
}

function busy() {
  if (state.runId) { toast("Wait until Claude finishes."); return true; }
  return false;
}

// After a change: read the project again (parts, plate counts) and the lists.
async function reload() {
  if (state.project) state.proj = await api(`/api/projects/${state.project}`).catch(() => state.proj);
  await loadProjectList();
  await refreshParts();
}

// ---------- Part rows ----------
hooks.partItem.push((li, p) => {
  li.classList.add("has-menu");
  const b = builds[p.name];
  if (b?.state === "running") {
    li.querySelector(".pmeta").insertAdjacentHTML("beforeend", ` · <span class="building"><span class="spinner"></span> building…</span>`);
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn ghost part-menu";
  btn.title = `Actions for ${p.name}`;
  btn.setAttribute("aria-label", `Actions for ${p.name}`);
  btn.textContent = "⋯";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu(btn, [
      { label: "Rename…", run: () => renamePart(p.name) },
      { label: "Move to project…", run: () => movePart(p.name) },
      info.git && { label: "Older versions…", run: () => history(p.name) },
      { label: "Delete…", danger: true, run: () => deletePart(p.name) },
    ]);
  });
  li.appendChild(btn);
});

async function renamePart(name) {
  if (busy()) return;
  const to = await dialog({
    title: `Rename ${name}`,
    html: `<p class="muted">Lowercase letters, digits and _ only. The app renames the file, the model functions and the generated files, then builds the part again.</p>`,
    ok: "Rename",
    field: {
      value: name, max: 60, placeholder: "new_name",
      check: (v) => !/^[a-z][a-z0-9_]*$/.test(v) ? "Use lowercase letters, digits and _. Start with a letter."
        : v === name ? "Give a new name." : "",
    },
    submit: (v) => api(`/api/manage/parts/${name}/rename`, { method: "POST", body: JSON.stringify({ to: v }) }),
  });
  if (!to) return;
  if (state.selected === name) { state.selected = to; try { localStorage.setItem(`part:${state.project}`, to); } catch { /* private mode */ } }
  toast(`Renamed to ${to}. It builds again in the background.`);
  watchBuilds();
  await reload();
}

async function movePart(name) {
  if (busy()) return;
  const list = (await api("/api/projects")).filter((p) => p.id !== state.project && p.id !== UNSORTED);
  const options = list.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`);
  if (state.project !== UNSORTED) options.push(`<option value="${UNSORTED}">Not in a project</option>`);
  if (!options.length) return toast("There is no other project. Create one with + Project.");
  let select;
  const ok = await dialog({
    title: `Move ${name}`,
    html: `<label class="muted" for="move-to">Move the part to:</label>
      <select id="move-to" class="field">${options.join("")}</select>
      <p class="muted small">The model stays in src/. Only the project list changes.</p>`,
    ok: "Move",
    onOpen: (form) => { select = form.querySelector("#move-to"); select.focus(); },
  });
  if (!ok) return;
  try {
    await api(`/api/manage/parts/${name}/move`, { method: "POST", body: JSON.stringify({ from: state.project, to: select.value }) });
    toast(`Moved ${name} to ${select.selectedOptions[0].textContent}.`);
    await reload();
  } catch (e) { toast(e.message, 6000); }
}

async function deletePart(name) {
  if (busy()) return;
  const keep = info.git && info.autocommit
    ? "The model stays in the git history of the workspace. You can get it back from there."
    : info.git ? "The auto-commit is off: commit the workspace first if you want to keep the model in git."
    : "The workspace is not a git repo. You cannot get the part back.";
  const ok = await confirmDanger(`Delete ${name}?`,
    `<p>This deletes <code>src/${esc(name)}.py</code> and its STEP, STL, GLB and G-code files. It also removes the part from its projects and plates.</p>
     <p class="muted">${esc(keep)}</p>`, "Delete part");
  if (!ok) return;
  try {
    await api(`/api/manage/parts/${name}`, { method: "DELETE" });
    toast(`Deleted ${name}.`);
    await reload();
  } catch (e) { toast(e.message, 6000); }
}

// ---------- Older versions ----------
const fmtDate = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

async function history(name) {
  if (busy()) return;
  let data;
  try { data = await api(`/api/manage/parts/${name}/history`); } catch (e) { return toast(e.message, 6000); }
  if (!data.items.length) return toast(`${name} has no saved versions yet.`);
  const rows = data.items.map((it, i) => `
    <li>
      <div class="ver-text"><strong>${esc(fmtDate(it.date))}</strong>${it.current ? ` <span class="chip">current</span>` : ""}
        <div class="muted small">${esc(it.subject)}${it.name !== name ? ` · as ${esc(it.name)}` : ""}</div></div>
      <div class="ver-actions">
        <button type="button" class="btn small ghost" data-preview="${i}">Preview</button>
        <button type="button" class="btn small" data-restore="${i}" ${it.current ? "disabled" : ""}>Restore</button>
      </div>
    </li>`).join("");
  const warn = data.dirty && !data.autocommit
    ? `<p class="error small">The file has changes that are not in git. Restore replaces them.</p>` : "";
  await dialog({
    title: `Older versions of ${name}`,
    html: `${warn}<ul class="versions">${rows}</ul><pre class="ver-preview" hidden></pre>`,
    ok: "", cancel: "Close", wide: true,
    onOpen: (form, close) => {
      const pre = form.querySelector(".ver-preview");
      form.querySelector(".versions").addEventListener("click", async (e) => {
        const pv = e.target.closest("[data-preview]"), rs = e.target.closest("[data-restore]");
        if (pv) {
          const it = data.items[+pv.dataset.preview];
          try {
            const v = await api(`/api/manage/parts/${name}/history/${it.sha}`);
            pre.textContent = v.source;
            pre.hidden = false;
            pre.scrollIntoView({ block: "nearest" });
          } catch (ex) { toast(ex.message); }
        } else if (rs) {
          const it = data.items[+rs.dataset.restore];
          close(null);
          await restore(name, it);
        }
      });
    },
  });
}

async function restore(name, it) {
  const ok = await dialog({
    title: `Restore ${name}?`,
    html: `<p>The model goes back to the version of <strong>${esc(fmtDate(it.date))}</strong> (${esc(it.subject)}).</p>
      <p class="muted">This saves a new version. The history stays. The part builds again in the background.</p>`,
    ok: "Restore",
  });
  if (!ok || busy()) return;
  try {
    await api(`/api/manage/parts/${name}/restore`, { method: "POST", body: JSON.stringify({ sha: it.sha }) });
    toast(`Restored ${name}. It builds again in the background.`);
    watchBuilds();
    await reload();
  } catch (e) { toast(e.message, 6000); }
}

// ---------- Background builds ----------
async function watchBuilds() {
  clearTimeout(watchBuilds.timer);
  let next;
  try { next = await api("/api/manage/builds"); } catch { next = builds; }
  const running = (all) => Object.keys(all).filter((n) => all[n].state === "running").sort().join(",");
  const finished = Object.entries(next).filter(([n, b]) => b.state !== "running" && builds[n]?.state === "running");
  const changed = running(next) !== running(builds);
  builds = next;
  for (const [n, b] of finished) {
    if (b.state === "done") toast(`${n} is built.${b.note ? " " + b.note : ""}`, b.note ? 7000 : 3500);
    else toast(`Build of ${n} failed: ${(b.log || "").split("\n").slice(-2).join(" ")}`, 9000);
  }
  if (changed) await refreshParts().catch(() => {}); // shows or hides "building…", loads the new model
  if (Object.values(builds).some((b) => b.state === "running")) watchBuilds.timer = setTimeout(watchBuilds, 2500);
}

// ---------- Import an STL or G-code file ----------
const IMPORT_MAX_MB = 100; // MAX_IMPORT in web/importer.py
const IMPORT_TEXT = {
  stl: "The STL becomes a part in this project. Claude can change it in the chat: holes, cuts, " +
    "a new size, extra parts. A small mesh becomes a CAD solid. A big mesh (more than 20 000 triangles) " +
    "gets fast mesh edits.",
  gcode: "The G-code becomes a part in this project. You see its layers and can print it. " +
    "Claude can change the temperatures, fan, speed and flow, or add a pause at a layer. The shape stays as sliced.",
};

$("parts-import").addEventListener("click", () => {
  if (busy()) return;
  $("import-input").value = "";
  $("import-input").click();
});

$("import-input").addEventListener("change", async () => {
  const file = $("import-input").files[0];
  if (!file) return;
  const ext = file.name.split(".").pop().toLowerCase();
  const kind = ext === "stl" ? "stl" : ["gcode", "gco", "g"].includes(ext) ? "gcode" : null;
  if (!kind) return toast("Import an .stl or a .gcode file.");
  if (file.size > IMPORT_MAX_MB * 2 ** 20) return toast(`The file is larger than ${IMPORT_MAX_MB} MB.`);
  const suggested = file.name.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9]+/g, "_")
    .replace(/^[^a-z]+/, "").replace(/_+$/, "").slice(0, 40) || "imported_part";
  let result = null;
  const name = await dialog({
    title: `Import ${file.name}`,
    html: `<p class="muted">${esc(IMPORT_TEXT[kind])}</p><p>Name of the part:</p>`,
    field: { value: suggested, max: 60, check: (v) => /^[a-z][a-z0-9_]*$/.test(v) ? null : "Use lowercase letters, digits and _ only. Start with a letter." },
    ok: "Import",
    submit: async (value, form) => {
      const btn = form.querySelector("button[type=submit]");
      btn.innerHTML = `<span class="spinner"></span> Uploading…`;
      try {
        const q = new URLSearchParams({ name: value, filename: file.name, project: state.project || "" });
        const res = await fetch(`/api/import?${q}`, { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || res.statusText);
        result = data;
      } finally { btn.textContent = "Import"; }
    },
  });
  if (!name || !result) return;
  toast(kind === "gcode" ? `${name} is imported. It builds now. Then ask Claude in the chat for changes.`
    : `${name} is imported (${result.kind === "mesh" ? "mesh" : "CAD solid"}). It builds now. Then ask Claude in the chat for changes.`, 6000);
  await reload();
  selectPart(name);
  watchBuilds();
});

// ---------- Project actions ----------
$("project-menu").addEventListener("click", () => {
  const virtual = !state.project || state.project === UNSORTED;
  menu($("project-menu"), [
    { label: "Rename project…", disabled: virtual, run: renameProject },
    { label: "Delete project…", danger: true, disabled: virtual, run: deleteProject },
  ]);
});

async function renameProject() {
  if (busy()) return;
  const pid = state.project;
  const name = await dialog({
    title: "Rename project",
    ok: "Rename",
    field: {
      value: state.proj?.name || "", max: 80, placeholder: "Project name",
      check: (v) => v ? "" : "Give the project a name.",
    },
    submit: (v) => api(`/api/manage/projects/${pid}/rename`, { method: "POST", body: JSON.stringify({ name: v }) }),
  });
  if (!name) return;
  toast(`Renamed the project to ${name}.`);
  await reload();
}

async function deleteProject() {
  if (busy()) return;
  const pid = state.project;
  const proj = await api(`/api/projects/${pid}`);
  const parts = proj.parts || [], files = proj.files || [];
  const ok = await confirmDanger(`Delete project ${proj.name}?`,
    (parts.length
      ? `<p>Its ${parts.length} part${parts.length > 1 ? "s" : ""} (${esc(parts.join(", "))}) move to <strong>Not in a project</strong>. The models stay.</p>`
      : `<p>The project has no parts.</p>`) +
    `<p class="muted">Its plate and ideas go away. Its chats stay on the server, but the page does not show them.</p>`,
    "Delete project");
  if (!ok) return;
  let delFiles = false;
  if (files.length) {
    const names = files.slice(0, 8).map((f) => esc(f.name)).join(", ") + (files.length > 8 ? ", …" : "");
    delFiles = !!(await confirmDanger(`Also delete ${files.length} file${files.length > 1 ? "s" : ""}?`,
      `<p>The project folder holds pictures and files: ${names}.</p>
       <p>Delete them too? ${info.git && info.autocommit ? "They stay in the git history." : "You cannot get them back."}</p>`,
      "Delete files"));
    if (!delFiles) return toast("Nothing deleted.");
  }
  try {
    const q = new URLSearchParams({ parts: parts.length ? "unsort" : "", files: delFiles ? "delete" : "" });
    await api(`/api/manage/projects/${pid}?${q}`, { method: "DELETE" });
    toast(`Deleted project ${proj.name}.`);
    try { localStorage.removeItem("project"); } catch { /* private mode */ }
    state.project = "";
    await loadProjectList();
    await openProject(state.project);
  } catch (e) { toast(e.message, 6000); }
}

// ---------- Settings and first-run setup ----------
async function openSettings(setup = false) {
  let s;
  try { s = await api("/api/manage/settings"); } catch (e) { return toast(e.message); }
  const bed = s.bed || ["", "", ""];
  const html = `
    ${setup ? `<p class="muted">Tell Printwright about you and your printer. You can change this later with the ⚙ button.</p>` : ""}
    ${s.problem && !setup ? `<p class="error small">${esc(s.problem)}</p>` : ""}
    <div class="settings-grid">
      <label>Your name<input class="field" name="owner" maxlength="60" required></label>
      <label>Printer name<input class="field" name="printer_name" maxlength="80" required placeholder="e.g. Elegoo Neptune 4 Pro"></label>
      <label class="span">Moonraker URL
        <span class="url-row"><input class="field" name="moonraker_url" type="url" maxlength="200" required placeholder="http://my-printer.local" inputmode="url" autocapitalize="off" spellcheck="false">
        <button type="button" class="btn" data-test>Test connection</button></span>
        <span class="test-result muted small" aria-live="polite"></span>
        ${s.env_url ? `<span class="muted small">PRINTER_URL in the app environment overrides this URL.</span>` : ""}
      </label>
      <fieldset class="span bed"><legend>Bed size (usable), mm</legend>
        <label>X<input class="field" name="bed0" type="number" min="20" max="2000" step="any" inputmode="decimal" required></label>
        <label>Y<input class="field" name="bed1" type="number" min="20" max="2000" step="any" inputmode="decimal" required></label>
        <label>Z<input class="field" name="bed2" type="number" min="20" max="2000" step="any" inputmode="decimal" required></label>
      </fieldset>
      <label>Nozzle, mm<input class="field" name="nozzle" type="number" min="0.1" max="2" step="0.05" inputmode="decimal" list="nozzles" required></label>
      <label>Default material<input class="field" name="material" maxlength="20" list="materials" required></label>
    </div>
    <datalist id="nozzles"><option value="0.2"><option value="0.4"><option value="0.6"><option value="0.8"></datalist>
    <datalist id="materials">${(s.materials || []).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
    <p class="muted small">Saved in ${esc(s.path)} in the workspace. Print settings for one part are not here.</p>`;
  const values = (f) => ({
    owner: f.elements.owner.value, printer_name: f.elements.printer_name.value,
    moonraker_url: f.elements.moonraker_url.value,
    bed: ["bed0", "bed1", "bed2"].map((k) => Number(f.elements[k].value)),
    nozzle: Number(f.elements.nozzle.value), material: f.elements.material.value,
  });
  const saved = await dialog({
    title: setup ? "Set up Printwright" : "Settings",
    html, ok: "Save", cancel: setup ? "Later" : "Cancel", wide: true,
    submit: async (_, f) => {
      if (!f.checkValidity()) { f.reportValidity(); throw new Error("Fill in every field with a valid value."); }
      await api("/api/manage/settings", { method: "PUT", body: JSON.stringify(values(f)) });
    },
    onOpen: (f) => {
      const el = f.elements;
      f.classList.add("settings");
      el.owner.value = s.owner === "the user" ? "" : s.owner || "";
      el.printer_name.value = s.printer_name || "";
      el.moonraker_url.value = s.moonraker_url === PLACEHOLDER_URL ? "" : s.moonraker_url || "";
      ["bed0", "bed1", "bed2"].forEach((k, i) => (el[k].value = bed[i] ?? ""));
      el.nozzle.value = s.nozzle ?? 0.4;
      el.material.value = s.material || "PLA";
      const out = f.querySelector(".test-result");
      f.querySelector("[data-test]").addEventListener("click", async (e) => {
        const b = e.currentTarget;
        b.disabled = true;
        out.className = "test-result muted small";
        out.textContent = "Testing…";
        try {
          const r = await api("/api/manage/settings/test", { method: "POST", body: JSON.stringify({ moonraker_url: el.moonraker_url.value }) });
          out.className = `test-result small ${r.ok ? "ok-text" : "error"}`;
          out.textContent = (r.ok ? "OK. " : "Failed. ") + r.message;
        } catch (ex) { out.className = "test-result small error"; out.textContent = ex.message; }
        b.disabled = false;
      });
      setTimeout(() => (el.owner.value ? el.printer_name : el.owner).focus(), 0);
    },
  });
  if (saved) {
    toast("Settings saved.");
    setTimeout(() => location.reload(), 600); // the bed view and the prompts use the new values
  } else if (setup) {
    try { sessionStorage.setItem("setup-later", "1"); } catch { /* private mode */ }
  }
}
$("settings-open").addEventListener("click", () => openSettings(false));

// ---------- Start ----------
(async function start() {
  try { info = await api("/api/manage/info"); } catch { return; }
  await watchBuilds();
  let later = null;
  try { later = sessionStorage.getItem("setup-later"); } catch { /* private mode */ }
  if (info.needs_setup && !later) openSettings(true);
})();
