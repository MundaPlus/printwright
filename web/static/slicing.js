// Print settings panel: material, layer height, infill, walls, supports, brim and the
// turn on the bed per part, or the layout of the copies per plate. Settings live
// in presets/parts/<part>.json or presets/parts/plate_<project>.json.
// "Save and re-slice" runs only the slicer (the CAD model stays as it is), then the
// server rebuilds the bed view. The panel polls the job and refreshes the info bar.
import { state, api, toast, refreshParts, fmtMin, esc } from "./app.js";

const $ = (id) => document.getElementById(id);
const AXIS_STEPS = [0, 90, 180, 270];
const STALE_TEXT = { material: "material", placement: "turn on the bed", arrange: "plate layout" };
const ALIGNS = [["center", "Centre"], ["front_left", "Front left"], ["front_right", "Front right"],
  ["back_left", "Back left"], ["back_right", "Back right"]];
const ps = { target: null, data: null, form: null, saved: "", polls: {} };

// ---------- Button in the info bar ----------
const openBtn = document.createElement("button");
openBtn.id = "ps-open";
openBtn.type = "button";
openBtn.className = "btn small";
openBtn.innerHTML = `<span class="ps-icon" aria-hidden="true">⚙</span><span class="ps-label">Settings</span>`;
openBtn.title = "Print settings: material, infill, supports, brim, turn, plate layout";
$("print-btn").before(openBtn);

// ---------- Side sheet ----------
document.body.insertAdjacentHTML("beforeend", `
  <div id="ps-wrap" class="ps-wrap" hidden>
    <div class="ps-backdrop" data-close></div>
    <aside class="ps-sheet" role="dialog" aria-modal="true" aria-labelledby="ps-title">
      <header class="ps-head">
        <div>
          <h2 id="ps-title">Print settings</h2>
          <div id="ps-sub" class="muted small"></div>
        </div>
        <button type="button" class="btn small ghost" data-close title="Close">×</button>
      </header>
      <div id="ps-status" class="ps-status"></div>
      <form id="ps-form" class="ps-body" autocomplete="off"></form>
      <footer class="ps-foot">
        <button type="button" id="ps-reset" class="btn small ghost" title="Use the defaults for everything">Reset all</button>
        <span class="ps-spacer"></span>
        <button type="button" id="ps-save" class="btn small">Save</button>
        <button type="button" id="ps-reslice" class="btn small primary">Save and re-slice</button>
      </footer>
    </aside>
  </div>`);

function current() {
  if (state.plateMode) {
    return state.project && state.project !== "unsorted" ? { name: `plate_${state.project}`, kind: "plate" } : null;
  }
  // An imported G-code has no print settings: Claude edits the G-code itself.
  const p = state.parts.find((x) => x.name === state.selected);
  return state.selected && p?.kind !== "gcode" ? { name: state.selected, kind: "part" } : null;
}

const isOpen = () => !$("ps-wrap").hidden;
const running = (name) => ps.polls[name]?.state === "running";

// ---------- Form model ----------
function formFrom(data) {
  return {
    values: { ...data.values },
    material: data.material.value,
    placement: data.placement ? { rotate: [...data.placement.rotate], auto_orient: data.placement.auto_orient } : null,
    arrange: data.arrange ? { ...data.arrange } : null,
  };
}
const snapshot = (f) => JSON.stringify(f);
const same = (a, b) => {
  const x = parseFloat(String(a)), y = parseFloat(String(b));
  return Number.isFinite(x) && Number.isFinite(y) ? Math.abs(x - y) < 1e-9 : String(a) === String(b);
};
const changed = (key) => !same(ps.form.values[key], ps.data.defaults[key]);
const num = (v) => String(v ?? "").replace("%", "");

function optionLabel(field, value) {
  const o = (field.options || []).find((x) => same(x[0], value));
  if (o) return o[1];
  if (field.type === "bool") return value === "1" ? "on" : "off";
  return `${num(value)}${field.unit ? (field.unit === "%" || field.unit === "°" ? "" : " ") + field.unit : ""}`;
}

function matLabel(m) {
  const temps = m.nozzle ? ` · ${m.nozzle}/${m.bed} °C` : "";
  return `${m.id}${temps}`;
}

// ---------- Rendering ----------
function row(key, label, control, isChanged, defText, extra = "") {
  return `<div class="ps-row${isChanged ? " changed" : ""}${extra}" data-row="${key}">
    <label for="ps-${key}">${esc(label)}</label>
    <div class="ps-ctl">${control}</div>
    <div class="ps-def">${isChanged ? `<button type="button" class="btn tiny ghost" data-reset="${key}">Reset</button>` : ""}<span>default ${esc(defText)}</span></div>
  </div>`;
}

function seg(name, options, value, disabled = false) {
  return `<div class="ps-seg" role="group">${options.map(([v, l]) =>
    `<button type="button" class="btn tiny${same(v, value) ? " on" : ""}" data-seg="${name}" data-value="${esc(String(v))}"${disabled ? " disabled" : ""}>${esc(l)}</button>`).join("")}</div>`;
}

function fieldControl(f, value, disabled = false) {
  const dis = disabled ? " disabled" : "";
  if (f.type === "choice") {
    return `<select id="ps-${f.key}" data-key="${f.key}"${dis}>${f.options.map(([v, l]) =>
      `<option value="${esc(v)}"${same(v, value) ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  }
  if (f.type === "bool") {
    return `<input type="checkbox" id="ps-${f.key}" data-key="${f.key}"${value === "1" ? " checked" : ""}${dis}>`;
  }
  const step = f.type === "float" ? "0.5" : "1";
  return `<span class="ps-num"><input type="number" id="ps-${f.key}" data-key="${f.key}" inputmode="decimal"
    min="${f.min}" max="${f.max}" step="${step}" value="${esc(num(value))}"${dis}>${f.unit ? `<span class="muted">${esc(f.unit)}</span>` : ""}</span>`;
}

function render() {
  const d = ps.data, form = ps.form;
  const F = Object.fromEntries(d.fields.map((f) => [f.key, f]));
  const plain = (key, disabled = false) => row(key, F[key].label, fieldControl(F[key], form.values[key], disabled),
    changed(key), optionLabel(F[key], d.defaults[key]), disabled ? " off" : "");
  const parts = [];

  const mats = d.material.options;
  const defMat = mats.find((m) => m.id === d.material.default);
  parts.push(`<fieldset><legend>Material</legend>${row("material", "Filament",
    `<select id="ps-material" data-material>${mats.map((m) =>
      `<option value="${esc(m.id)}"${m.id === form.material ? " selected" : ""}>${esc(matLabel(m))}</option>`).join("")}</select>`,
    form.material !== d.material.default, defMat ? matLabel(defMat) : d.material.default)}</fieldset>`);

  parts.push(`<fieldset><legend>Quality</legend>${plain("layer_height")}</fieldset>`);
  parts.push(`<fieldset><legend>Strength</legend>${["sparse_infill_pattern", "sparse_infill_density", "wall_loops",
    "top_shell_layers", "bottom_shell_layers"].map((k) => plain(k)).join("")}</fieldset>`);

  // Supports: one choice for enable_support + support_type.
  const sup = form.values.enable_support === "1" ? (form.values.support_type.startsWith("tree") ? "tree" : "normal") : "none";
  const supDef = d.defaults.enable_support === "1" ? (d.defaults.support_type.startsWith("tree") ? "Tree" : "Normal") : "None";
  const supChanged = changed("enable_support") || (sup !== "none" && changed("support_type"));
  parts.push(`<fieldset><legend>Supports</legend>
    ${row("supports", "Supports", seg("supports", [["none", "None"], ["normal", "Normal"], ["tree", "Tree"]], sup), supChanged, supDef)}
    ${plain("support_on_build_plate_only", sup === "none")}
    ${plain("support_threshold_angle", sup === "none")}
  </fieldset>`);

  parts.push(`<fieldset><legend>Bed adhesion</legend>
    ${row("brim_type", F.brim_type.label, seg("brim_type", F.brim_type.options, form.values.brim_type), changed("brim_type"),
      optionLabel(F.brim_type, d.defaults.brim_type))}
    ${plain("brim_width", form.values.brim_type !== "outer_only")}
  </fieldset>`);

  if (form.placement) {
    const [rx, ry, rz] = form.placement.rotate;
    const free = !AXIS_STEPS.includes(rz);
    parts.push(`<fieldset><legend>Place on the bed</legend>
      ${row("rotate_z", "Turn around Z", `${seg("rz", AXIS_STEPS.map((a) => [a, `${a}°`]), rz)}
        <span class="ps-num"><input type="number" id="ps-rotate_z" data-rz min="-360" max="360" step="1" value="${rz}"
        title="Any angle"${free ? ' class="on"' : ""}><span class="muted">°</span></span>`, rz !== 0, "0°")}
      ${row("rotate_x", "Turn around X", seg("rx", AXIS_STEPS.map((a) => [a, `${a}°`]), rx), rx !== 0, "0°")}
      ${row("rotate_y", "Turn around Y", seg("ry", AXIS_STEPS.map((a) => [a, `${a}°`]), ry), ry !== 0, "0°")}
      ${row("auto_orient", "Lay flat (auto)", `<input type="checkbox" id="ps-auto_orient" data-orient${form.placement.auto_orient ? " checked" : ""}>`,
        form.placement.auto_orient, "off")}
      <p class="muted small ps-note">X and Y lay the part on another face. The slicer turns X first, then Y, then Z.
        "Lay flat (auto)" lets OrcaSlicer choose the face; the bed view then shows the part as designed.</p>
    </fieldset>`);
  } else if (form.arrange) {
    const a = form.arrange, def = d.arrange_defaults, grid = a.mode === "grid";
    const alignText = (v) => ALIGNS.find((x) => x[0] === v)?.[1] || v;
    parts.push(`<fieldset><legend>Arrange on the plate</legend>
      ${row("arrange_mode", "Layout", seg("amode", [["auto", "Auto (tight)"], ["grid", "Grid"]], a.mode),
        a.mode !== def.mode, def.mode === "grid" ? "Grid" : "Auto")}
      ${row("arrange_spacing", "Spacing", `<span class="ps-num"><input type="number" id="ps-arrange_spacing" data-spacing
        min="0" max="50" step="1" value="${a.spacing}"${grid ? "" : " disabled"}><span class="muted">mm</span></span>`,
        !same(a.spacing, def.spacing), `${def.spacing} mm`, grid ? "" : " off")}
      ${row("arrange_align", "Position", `<select id="ps-arrange_align" data-align${grid ? "" : " disabled"}>${ALIGNS.map(([v, l]) =>
        `<option value="${v}"${v === a.align ? " selected" : ""}>${l}</option>`).join("")}</select>`,
        a.align !== def.align, alignText(def.align), grid ? "" : " off")}
      ${row("arrange_turn", "Turn copies to fit", `<input type="checkbox" id="ps-arrange_turn" data-turn${a.turn ? " checked" : ""}>`,
        a.turn !== def.turn, def.turn ? "on" : "off")}
      <p class="muted small ps-note">Auto: OrcaSlicer packs the copies tight, about 2 mm apart, in the middle of the bed.
        Grid: rows of copies with your spacing, in the middle or in a corner. "Turn copies to fit" lets auto turn
        each copy, and turns all grid copies 90° when that fits better.</p>
      <p class="muted small ps-note">A plate uses its own settings: OrcaSlicer cannot mix settings on one plate.
        Each part keeps its own turn. Set the turn in the settings of the part.</p>
    </fieldset>`);
  }

  const extra = Object.entries(d.extra || {});
  if (extra.length) {
    parts.push(`<fieldset><legend>Other settings in the file</legend><ul class="ps-extra">${extra.map(([k, v]) =>
      `<li><code>${esc(k)}</code> = <code>${esc(JSON.stringify(v))}</code></li>`).join("")}</ul>
      <p class="muted small ps-note">Edit these in <code>${esc(d.file || "")}</code>, or ask Claude.</p></fieldset>`);
  }
  $("ps-form").innerHTML = parts.join("");
  renderStatus();
}

function renderStatus() {
  const d = ps.data, job = ps.polls[d.name] || d.job;
  const el = $("ps-status");
  const dirty = snapshot(ps.form) !== ps.saved;
  $("ps-save").disabled = !dirty || running(d.name);
  $("ps-reslice").disabled = running(d.name);
  $("ps-reslice").textContent = d.kind === "plate" ? "Save and arrange" : "Save and re-slice";
  $("ps-reset").disabled = running(d.name);
  if (job?.state === "running") {
    el.className = "ps-status busy";
    el.innerHTML = `<span class="spinner"></span> ${esc(job.text || "Slicing…")}`;
    return;
  }
  if (job?.state === "error" && job.finished > (d.sliced?.updated || 0)) {
    el.className = "ps-status bad";
    el.textContent = job.error || "Slicing failed.";
    return;
  }
  const s = d.sliced;
  if (!s) {
    el.className = "ps-status warn";
    el.textContent = "Not sliced yet.";
  } else if (d.stale.length || dirty) {
    const what = d.stale.map((k) => STALE_TEXT[k] || d.fields.find((f) => f.key === k)?.label.toLowerCase() || k);
    el.className = "ps-status warn";
    el.textContent = dirty ? "You changed settings. Save and re-slice to apply them."
      : `The G-code does not use these settings yet (${what.join(", ")}). Re-slice to apply them.`;
  } else {
    const v = s.values;
    const sup = v.enable_support === "1" ? `${(v.support_type || "").startsWith("tree") ? "tree" : "normal"} supports` : "no supports";
    el.className = "ps-status ok";
    el.textContent = `G-code: ${s.material} ${s.nozzle}/${s.bed} °C · ${v.layer_height} mm · ${v.sparse_infill_density} ${v.sparse_infill_pattern} · ${sup}`;
  }
  if (job?.warning) el.textContent += ` (${job.warning})`;
}

// ---------- Open, load, save ----------
async function load(target) {
  ps.target = target;
  const data = await api(`/api/slicing/settings/${target.name}`);
  if (ps.target?.name !== target.name) return;
  ps.data = data;
  ps.form = formFrom(data);
  ps.saved = snapshot(ps.form);
  if (data.job?.state === "running" && !ps.polls[data.name]) poll(data.name);
  $("ps-title").textContent = target.kind === "plate" ? "Plate settings" : "Print settings";
  $("ps-sub").textContent = target.kind === "plate" ? `${state.proj?.name || state.project} · ${data.file || "defaults"}`
    : `${target.name} · ${data.file || "defaults from the presets"}`;
  render();
}

async function openSheet() {
  const t = current();
  if (!t) return toast(state.plateMode ? "Choose a project first." : "Select a part first.");
  $("ps-wrap").hidden = false;
  $("ps-form").innerHTML = `<p class="muted"><span class="spinner"></span> Loading…</p>`;
  $("ps-status").textContent = "";
  try { await load(t); } catch (e) { toast(e.message); closeSheet(); }
}

function closeSheet() {
  if (ps.data && ps.form && snapshot(ps.form) !== ps.saved && !confirm("Close without saving your changes?")) return;
  $("ps-wrap").hidden = true;
}

async function save() {
  const d = ps.data;
  const body = { values: ps.form.values, material: ps.form.material };
  if (ps.form.placement) body.placement = ps.form.placement;
  if (ps.form.arrange) body.arrange = ps.form.arrange;
  const data = await api(`/api/slicing/settings/${d.name}`, { method: "PUT", body: JSON.stringify(body) });
  ps.data = data;
  ps.form = formFrom(data);
  ps.saved = snapshot(ps.form);
  render();
  refreshButton();
}

async function reslice() {
  const d = ps.data;
  try {
    if (snapshot(ps.form) !== ps.saved) await save();
    if (d.kind === "plate") {
      const arrange = $("plate-arrange");
      if (!state.plateMode || arrange.hidden) return toast("Open Plate… first.");
      $("ps-wrap").hidden = true;
      toast("Arranging the plate with these settings…");
      arrange.click();
      return;
    }
    const job = await api(`/api/slicing/reslice/${d.name}`, { method: "POST" });
    ps.polls[d.name] = job;
    renderStatus();
    refreshButton();
    poll(d.name);
  } catch (e) { toast(e.message, 6000); }
}

// ---------- Job polling ----------
async function poll(name) {
  let step = ps.polls[name]?.step;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let job;
    try { job = await api(`/api/slicing/jobs/${name}`); } catch { continue; }
    ps.polls[name] = job;
    if (job.step !== step) {
      if (step === "slice" || job.state !== "running") await refreshParts().catch(() => {}); // new time and grams
      step = job.step;
    }
    if (isOpen() && ps.data?.name === name) {
      if (job.state === "running") renderStatus(); else await load(ps.target).catch(() => {});
    }
    refreshButton();
    if (job.state !== "running") {
      if (job.state === "done") {
        const p = state.parts.find((x) => x.name === name);
        const s = p?.stats;
        toast(s ? `${name} re-sliced: ~${fmtMin(s.total_min)}, ${s.grams.toFixed(1)} g ${s.material}` : `${name} re-sliced.`);
      } else {
        toast(`${name}: ${job.error || "slicing failed"}`, 8000);
      }
      return;
    }
  }
}

// ---------- Form events ----------
$("ps-form").addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.key && t.type === "number") {
    const f = ps.data.fields.find((x) => x.key === t.dataset.key);
    ps.form.values[f.key] = f.type === "percent" ? `${t.value}%` : t.value;
    t.closest(".ps-row").classList.toggle("changed", changed(f.key));
    renderStatus();
  } else if (t.dataset.spacing !== undefined) {
    const v = parseFloat(t.value);
    if (Number.isFinite(v)) ps.form.arrange.spacing = v;
    t.closest(".ps-row").classList.toggle("changed", !same(v, ps.data.arrange_defaults.spacing));
    renderStatus();
  } else if (t.dataset.rz !== undefined) {
    const v = parseFloat(t.value);
    if (Number.isFinite(v)) ps.form.placement.rotate[2] = Math.round((((v % 360) + 360) % 360) * 100) / 100;
    renderStatus();
  }
});
$("ps-form").addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.key && t.type === "checkbox") ps.form.values[t.dataset.key] = t.checked ? "1" : "0";
  else if (t.dataset.key && t.tagName === "SELECT") ps.form.values[t.dataset.key] = t.value;
  else if (t.dataset.material !== undefined) ps.form.material = t.value;
  else if (t.dataset.orient !== undefined) ps.form.placement.auto_orient = t.checked;
  else if (t.dataset.turn !== undefined) ps.form.arrange.turn = t.checked;
  else if (t.dataset.align !== undefined) ps.form.arrange.align = t.value;
  else if (t.dataset.rz === undefined && t.dataset.spacing === undefined) return; // number inputs keep focus; their row is already marked
  render();
});
$("ps-form").addEventListener("click", (e) => {
  const b = e.target.closest("[data-seg], [data-reset]");
  if (!b) return;
  const v = ps.form.values, d = ps.data.defaults;
  if (b.dataset.reset) {
    const k = b.dataset.reset;
    if (k === "material") ps.form.material = ps.data.material.default;
    else if (k === "supports") { v.enable_support = d.enable_support; v.support_type = d.support_type; }
    else if (k.startsWith("rotate_")) ps.form.placement.rotate["xyz".indexOf(k.slice(-1))] = 0;
    else if (k === "auto_orient") ps.form.placement.auto_orient = false;
    else if (k.startsWith("arrange_")) ps.form.arrange[k.slice(8)] = ps.data.arrange_defaults[k.slice(8)];
    else v[k] = d[k];
  } else {
    const name = b.dataset.seg, val = b.dataset.value;
    if (name === "supports") {
      v.enable_support = val === "none" ? "0" : "1";
      if (val !== "none") v.support_type = `${val}(auto)`;
    } else if (name === "brim_type") {
      v.brim_type = val;
      if (val === "outer_only" && !(parseFloat(v.brim_width) > 0)) v.brim_width = "5";
    } else if (name === "amode") {
      ps.form.arrange.mode = val;
    } else if (["rx", "ry", "rz"].includes(name)) {
      ps.form.placement.rotate[["rx", "ry", "rz"].indexOf(name)] = Number(val);
    }
  }
  render();
});
$("ps-form").addEventListener("submit", (e) => e.preventDefault());

$("ps-save").addEventListener("click", () => save().then(() => toast("Settings saved. Re-slice to apply them.")).catch((e) => toast(e.message, 6000)));
$("ps-reslice").addEventListener("click", reslice);
$("ps-reset").addEventListener("click", () => {
  const d = ps.data;
  ps.form.values = { ...d.defaults };
  ps.form.material = d.material.default;
  if (ps.form.placement) ps.form.placement = { rotate: [0, 0, 0], auto_orient: false };
  if (ps.form.arrange) ps.form.arrange = { ...d.arrange_defaults };
  render();
});
$("ps-wrap").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeSheet(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) closeSheet(); });
openBtn.addEventListener("click", openSheet);

// ---------- Button state ----------
// The page changes #info-name for every part or plate it shows. Then the button follows.
let badgeTimer = null;
function refreshButton() {
  const t = current();
  openBtn.disabled = !t;
  openBtn.classList.toggle("busy", !!t && running(t.name));
  openBtn.querySelector(".ps-icon").innerHTML = t && running(t.name) ? `<span class="spinner"></span>` : "⚙";
  clearTimeout(badgeTimer);
  if (!t) return;
  badgeTimer = setTimeout(async () => {
    try {
      const d = await api(`/api/slicing/settings/${t.name}`);
      if (current()?.name !== t.name) return;
      const custom = Object.keys(d.overrides).length || d.material.value !== d.material.default ||
        (d.placement && (d.placement.auto_orient || d.placement.rotate.some((a) => a))) ||
        (d.arrange && JSON.stringify(d.arrange) !== JSON.stringify(d.arrange_defaults));
      openBtn.classList.toggle("custom", !!custom);
      openBtn.classList.toggle("stale", d.stale.length > 0);
      openBtn.title = (d.stale.length ? "The G-code does not use these settings yet. " : "") +
        `Print settings${custom ? " (changed for this " + t.kind + ")" : ""}: ${d.material.value}, ` +
        `${d.values.layer_height} mm, ${d.values.sparse_infill_density} ${d.values.sparse_infill_pattern}`;
      if (d.job?.state === "running" && !ps.polls[t.name]) { ps.polls[t.name] = d.job; poll(t.name); }
    } catch { /* not built yet or gone */ }
  }, 250);
}
new MutationObserver(refreshButton).observe($("info-name"), { childList: true, characterData: true, subtree: true });
refreshButton();

// ---------- Print dialog: material of the G-code ----------
// app.js fills the dialog first (its listener runs first). This adds what the G-code says.
$("print-btn").addEventListener("click", async () => {
  const t = current();
  if (!t) return;
  try {
    const d = await api(`/api/slicing/settings/${t.name}`);
    const s = d.sliced;
    if (!s || $("print-modal").hidden) return;
    $("chk-fil-text").textContent = `${s.material} is loaded.`;
    const v = s.values;
    const sup = v.enable_support === "1" ? `${(v.support_type || "").startsWith("tree") ? "tree" : "normal"} supports` : "no supports";
    const rows = [["Material", `${s.material} · nozzle ${s.nozzle} °C · bed ${s.bed} °C`],
      ["Settings", `${v.sparse_infill_density} ${v.sparse_infill_pattern} · ${v.wall_loops} walls · ${sup}`]];
    const dl = $("print-details");
    dl.querySelectorAll(".ps-added").forEach((n) => n.remove());
    dl.insertAdjacentHTML("beforeend", rows.map(([k, val]) =>
      `<dt class="ps-added">${k}</dt><dd class="ps-added">${esc(val)}</dd>`).join("") +
      (d.stale.length ? `<dd class="ps-added ps-stale">The settings changed after slicing. This G-code uses the old settings.</dd>` : ""));
  } catch { /* the dialog keeps what app.js wrote */ }
});
