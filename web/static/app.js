import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { initLayers } from "./layers.js"; // layers: G-code layer view

const $ = (id) => document.getElementById(id);
let BED = { x: 235, y: 230 }; // printable area, mm; boot() loads it from /api/config (printwright.json)
let CONFIG = { material: "PLA", printer_name: "Printer" };
const READY_STATES = ["standby", "complete", "cancelled"];

const store = {
  get(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const state = {
  parts: [],
  project: store.get("project") || "",
  proj: null,
  selected: null,
  sessionId: null,
  plateMode: false,
  plate: null, // last plate answer from the server
  counts: {}, // plate copies per part
  runId: null,
  printer: { state: "offline" },
  loadedGlb: null,
};
// Hooks for other page modules (manage.js): functions called for each part row.
export const hooks = { partItem: [] };

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
  return data;
}

function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

const fmtMin = (m) => {
  const r = Math.round(m), h = Math.floor(r / 60), mm = r % 60;
  return h ? `${h} h ${String(mm).padStart(2, "0")} min` : `${mm} min`;
};
const fmtSize = (s) => s ? `${s[0].toFixed(1)} × ${s[1].toFixed(1)} × ${s[2].toFixed(1)} mm` : "";
// Print time of a stats object. An imported G-code may name no time (total_min null).
const fmtTime = (s) => s?.total_min != null ? `~${fmtMin(s.total_min)}` : "time ?";
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------- Viewer ----------
// GLB from cadgen: metres, Y up. CAD (X, Y, Z) in mm maps to three (X, Z, -Y) after scaling.
const cad = (x, y, z) => new THREE.Vector3(x, z, -y);
const viewerEl = $("viewer");
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
viewerEl.prepend(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, 1, 5000);
camera.position.copy(cad(-150, -250, 250));
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.copy(cad(BED.x / 2, BED.y / 2, 0));
scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.copy(cad(-200, -300, 500));
scene.add(sun);
const fill = new THREE.DirectionalLight(0xffffff, 0.5);
fill.position.copy(cad(300, 300, 200));
scene.add(fill);
const under = new THREE.DirectionalLight(0xffffff, 0.9); // lights the bottom for the Bottom view
under.position.copy(cad(100, 80, -500));
scene.add(under);
let model = null;
let plateGroup = null;
// Flat things on the bed (plate, grid, flat rulers) turn see-through when the camera is below the bed.
let flatMaterials = new Set();
let below = null;
function collectFlat() {
  flatMaterials = new Set();
  for (const root of [model, plateGroup]) {
    root?.traverse((o) => {
      if (!o.isMesh && !o.isLine) return;
      const box = new THREE.Box3().setFromObject(o);
      if (box.max.y < 1.0) [].concat(o.material).forEach((m) => flatMaterials.add(m));
    });
  }
  below = null;
}
function applySeeThrough() {
  const b = camera.position.y < 0;
  if (b === below) return;
  below = b;
  for (const m of flatMaterials) {
    m.transparent = b;
    m.opacity = b ? 0.15 : 1;
    m.depthWrite = !b;
    m.needsUpdate = true;
  }
}

function resize() {
  const w = viewerEl.clientWidth, h = viewerEl.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewerEl);
(function loop() {
  requestAnimationFrame(loop);
  controls.update();
  applySeeThrough();
  renderer.render(scene, camera);
})();

function partBox() {
  const p = state.parts.find((x) => x.name === state.selected);
  if (state.plateMode) return { min: [-14, -14, 0], max: [BED.x + 14, BED.y + 14, 40] };
  const s = p?.size || [40, 40, 20];
  const pad = 18; // rulers sit beside the part
  return {
    min: [BED.x / 2 - s[0] / 2 - pad, BED.y / 2 - s[1] / 2 - pad, 0],
    max: [BED.x / 2 + s[0] / 2 + 8, BED.y / 2 + s[1] / 2 + 8, s[2] + 6],
  };
}

function setView(kind) {
  const bed = { min: [-14, -14, 0], max: [BED.x + 14, BED.y + 14, 10] };
  const box = kind === "bed" ? bed : kind === "part" || state.lastFocus === "part" ? partBox() : bed;
  if (kind === "part" || kind === "bed") state.lastFocus = kind;
  const c = cad((box.min[0] + box.max[0]) / 2, (box.min[1] + box.max[1]) / 2, (box.min[2] + box.max[2]) / 2);
  const r = Math.hypot(box.max[0] - box.min[0], box.max[1] - box.min[1], box.max[2] - box.min[2]) / 2;
  const dist = r / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * (camera.aspect < 1 ? 1.25 / camera.aspect : 1.0);
  const dirs = {
    iso: [-0.75, -1.1, 0.95],
    top: [0, -0.002, 1], // a hair toward the front keeps +Y pointing up on screen
    front: [0, -1, 0.12],
    bottom: [0, -0.002, -1], // from under the bed; the bed turns see-through
  };
  const d = dirs[kind] || dirs[state.lastDir || "iso"];
  if (dirs[kind]) state.lastDir = kind;
  const v = cad(...d).normalize().multiplyScalar(dist);
  camera.position.copy(c).add(v);
  controls.target.copy(c);
  camera.near = Math.max(0.5, dist / 100);
  camera.far = dist * 20;
  camera.updateProjectionMatrix();
}
document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

const loader = new GLTFLoader();
// meshUrl: a mesh part (an imported STL) has a bed view without the mesh. The page
// draws the placed STL on it, centred on the bed like lib/bed.py places a part.
async function loadModel(url, meshUrl = null) {
  const key = `${url}|${meshUrl || ""}`;
  if (state.loadedGlb === key) return;
  state.loadedGlb = key;
  $("viewer-empty").hidden = true;
  $("viewer-loading").hidden = false;
  try {
    const gltf = await loader.loadAsync(url);
    const geo = meshUrl ? await stlGeometry(meshUrl) : null;
    if (state.loadedGlb !== key) return; // a newer load won
    if (model) {
      scene.remove(model);
      model.traverse((o) => { if (!o.userData.shared) o.geometry?.dispose(); [].concat(o.material || []).forEach((m) => m.dispose()); });
    }
    gltf.scene.scale.setScalar(1000);
    model = new THREE.Group();
    model.add(gltf.scene);
    model.traverse((o) => {
      if (o.isMesh) {
        o.material.side = THREE.DoubleSide;
        if ("roughness" in o.material) { o.material.roughness = 0.75; o.material.metalness = 0; }
      }
    });
    if (geo) { // printer coordinates (mm, Z up), turned to three.js Y up
      const g = new THREE.Group();
      g.rotation.x = -Math.PI / 2;
      const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x4c9be8, roughness: 0.75, side: THREE.DoubleSide }));
      mesh.userData.shared = true; // the geometry stays in stlCache
      mesh.position.set(BED.x / 2, BED.y / 2, 0);
      g.add(mesh);
      model.add(g);
    }
    model.visible = !state.plateMode;
    scene.add(model);
    collectFlat();
    if (!state.viewSet) { state.viewSet = true; state.lastFocus = "part"; setView("iso"); }
  } catch (err) {
    toast("Could not load the model: " + err.message);
    state.loadedGlb = null;
  } finally {
    $("viewer-loading").hidden = true;
  }
}

function clearModel() {
  if (model) scene.remove(model);
  model = null;
  state.loadedGlb = null;
  $("viewer-empty").hidden = false;
}

// ---------- Plate preview ----------
const stlLoader = new STLLoader();
const stlCache = new Map(); // url -> geometry, centred in XY, bottom on Z=0
const PLATE_COLORS = [0xf2a33a, 0x4c9be8, 0x2fbf71, 0xd9534f, 0x9b6fe0, 0x17b6b0, 0xe06fb5, 0xc0c63a];

async function stlGeometry(url) {
  if (!stlCache.has(url)) {
    const g = await stlLoader.loadAsync(url);
    g.computeBoundingBox();
    const b = g.boundingBox;
    g.translate(-(b.min.x + b.max.x) / 2, -(b.min.y + b.max.y) / 2, -b.min.z);
    g.computeBoundingBox();
    g.computeVertexNormals();
    stlCache.set(url, g);
  }
  return stlCache.get(url);
}

function clearPlate() {
  if (!plateGroup) return;
  scene.remove(plateGroup);
  plateGroup.traverse((o) => { if (o.isLine || o.userData.own) o.geometry?.dispose(); });
  plateGroup = null;
}

// Built in printer coordinates (mm, Z up), then turned to three.js Y up.
async function drawPlate(data) {
  clearPlate();
  const g = new THREE.Group();
  g.rotation.x = -Math.PI / 2;
  const bed = new THREE.Mesh(new THREE.BoxGeometry(BED.x + 20, BED.y + 20, 3),
    new THREE.MeshStandardMaterial({ color: 0x30353d, roughness: 0.9 }));
  bed.position.set(BED.x / 2, BED.y / 2, -1.52);
  bed.userData.own = true;
  const pts = [];
  for (let x = 0; x <= BED.x; x += 10) pts.push(x, 0, 0.02, x, BED.y, 0.02);
  for (let y = 0; y <= BED.y; y += 10) pts.push(0, y, 0.02, BED.x, y, 0.02);
  const gridGeo = new THREE.BufferGeometry();
  gridGeo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  g.add(bed, new THREE.LineSegments(gridGeo, new THREE.LineBasicMaterial({ color: 0x59616d })));
  const layout = data.layout;
  const parts = Object.keys(layout.items);
  for (const o of layout.objects) {
    const url = data.stl[o.part];
    if (!url) continue;
    const geo = await stlGeometry(url);
    const color = PLATE_COLORS[parts.indexOf(o.part) % PLATE_COLORS.length];
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.75, side: THREE.DoubleSide }));
    const sx = geo.boundingBox.max.x - geo.boundingBox.min.x, sy = geo.boundingBox.max.y - geo.boundingBox.min.y;
    const [fx, fy] = o.footprint; // Orca may turn a part by 90 degrees
    if (Math.abs(fx - sy) + Math.abs(fy - sx) < Math.abs(fx - sx) + Math.abs(fy - sy) - 0.5) mesh.rotation.z = Math.PI / 2;
    mesh.position.set(o.center[0], o.center[1], 0);
    const outline = new THREE.BufferGeometry();
    outline.setAttribute("position", new THREE.Float32BufferAttribute(o.polygon.flatMap(([x, y]) => [x, y, 0.06]), 3));
    g.add(mesh, new THREE.LineLoop(outline, new THREE.LineBasicMaterial({ color })));
  }
  if (!state.plateMode) return; // left plate mode while loading
  plateGroup = g;
  scene.add(g);
  if (model) model.visible = false;
  collectFlat();
}

function plateSummary(data) {
  const l = data?.layout;
  return l ? Object.entries(l.items).map(([p, n]) => `${n} × ${p}`).join(", ") : "";
}

async function showPlate(data) {
  state.plate = data;
  const l = data?.layout;
  $("info-name").textContent = l ? `Plate: ${l.copies} part${l.copies > 1 ? "s" : ""}` : "Plate";
  $("info-size").textContent = l ? plateSummary(data) : "Nothing arranged yet";
  $("info-size").classList.toggle("warn", !!l && !l.fits);
  const s = data?.stats;
  $("info-time").textContent = s ? fmtTime(s) : l && !l.fits ? "does not fit" : "";
  $("info-time").classList.toggle("warn", !!l && !data.ready);
  $("info-grams").textContent = s ? `${s.grams.toFixed(1)} g ${s.material}` : "";
  $("dl-stl").hidden = true;
  $("dl-gcode").hidden = !data?.ready;
  $("dl-gcode").href = `/files/gcode/plate_${state.project}`;
  updatePrintButton();
  if (l && !l.fits && l.error) toast(l.error, 6000);
  if (l) await drawPlate(data); else clearPlate();
  $("viewer-empty").hidden = !!l;
  if (l) { state.lastFocus = "bed"; setView("iso"); }
  layers?.update(); // layers: show the plate's G-code
}

function setPlateMode(on) {
  state.plateMode = on;
  $("plate-toggle").classList.toggle("on", on);
  $("plate-toggle").textContent = on ? "Done" : "Plate…";
  $("plate-bar").hidden = $("plate-help").hidden = !on;
  if (on) {
    state.counts = { ...(state.proj?.plate || {}) };
    renderParts();
    api(`/api/projects/${state.project}/plate`).then(showPlate).catch((e) => toast(e.message));
  } else {
    clearPlate();
    state.plate = null;
    layers?.update(); // layers: back to the part's G-code
    renderParts();
    state.viewSet = false;
    if (model) { model.visible = true; collectFlat(); }
    $("viewer-empty").hidden = !!model; // an empty plate showed the hint over the part
    showPart();
  }
}
$("plate-toggle").addEventListener("click", () => {
  if (!state.project || state.project === "unsorted") return toast("Move parts into a project first.");
  setPlateMode(!state.plateMode);
});
$("plate-arrange").addEventListener("click", async () => {
  const items = Object.fromEntries(Object.entries(state.counts).filter(([p, n]) => n > 0 && state.parts.some((x) => x.name === p)));
  if (!Object.keys(items).length) return toast("Set a count for at least one part.");
  const btn = $("plate-arrange");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Slicing…`;
  try {
    const data = await api(`/api/projects/${state.project}/plate`, { method: "POST", body: JSON.stringify({ items }) });
    if (state.proj) state.proj.plate = items;
    if (state.plateMode) await showPlate(data);
  } catch (e) { toast(e.message, 6000); }
  btn.disabled = false;
  btn.textContent = "Arrange on plate";
});

// ---------- Parts ----------
async function refreshParts() {
  state.parts = await api(`/api/parts?project=${encodeURIComponent(state.project)}`);
  renderParts();
  if (state.selected && !state.parts.some((p) => p.name === state.selected)) state.selected = null;
  if (!state.selected && state.parts.length) {
    state.selected = state.parts[0].name;
    store.set(`part:${state.project}`, state.selected);
    renderParts();
  }
  if (!state.plateMode) showPart();
}

function renderParts() {
  const ul = $("parts");
  ul.innerHTML = "";
  $("parts-empty").hidden = state.parts.length > 0;
  for (const p of state.parts) {
    const li = document.createElement("li");
    li.className = p.name === state.selected ? "active" : "";
    const kind = { gcode: "G-code", mesh: "mesh" }[p.kind] || "";
    const meta = [kind, p.size ? fmtSize(p.size) : "not built", p.stats ? fmtTime(p.stats) : ""].filter(Boolean).join(" · ");
    li.innerHTML = `<div class="pname">${esc(p.name)}</div><div class="pmeta">${esc(meta)}</div>`;
    if (state.plateMode) {
      li.className = "plate-item";
      const n = document.createElement("input");
      Object.assign(n, { type: "number", min: 0, max: 50, value: state.counts[p.name] || 0, title: "Copies on the plate" });
      n.disabled = !p.size || p.kind === "gcode";
      if (p.kind === "gcode") n.title = "An imported G-code cannot go on a plate";
      n.addEventListener("input", () => { state.counts[p.name] = Math.max(0, Math.min(50, +n.value || 0)); plateTotal(); });
      li.appendChild(n);
    } else {
      li.addEventListener("click", () => { selectPart(p.name); setTab("view"); });
      hooks.partItem.forEach((f) => f(li, p));
    }
    ul.appendChild(li);
  }
  plateTotal();
}

function plateTotal() {
  const n = Object.entries(state.counts).filter(([p]) => state.parts.some((x) => x.name === p)).reduce((a, [, c]) => a + c, 0);
  $("plate-total").textContent = `${n} part${n === 1 ? "" : "s"}`;
}

function selectPart(name) {
  state.selected = name;
  store.set(`part:${state.project}`, name);
  state.viewSet = false;
  document.querySelectorAll("#parts li").forEach((li) => li.classList.toggle("active", li.querySelector(".pname").textContent === name));
  showPart();
}

function showPart() {
  if (state.plateMode) return;
  const p = state.parts.find((x) => x.name === state.selected);
  $("info-size").classList.remove("warn");
  $("context-chip").hidden = !p;
  $("context-part").textContent = p?.name || "";
  $("info-name").textContent = p?.name || "—";
  const warn = p?.warnings || [];
  $("info-size").textContent = (p?.size ? fmtSize(p.size) : "") + (warn.length ? " ⚠" : "");
  $("info-size").classList.toggle("warn", warn.length > 0);
  $("info-size").title = warn.join("\n");
  const s = p?.stats;
  $("info-time").textContent = s ? fmtTime(s) : p ? "not sliced" : "";
  $("info-time").classList.toggle("warn", !!p && !p.gcode_ready);
  $("info-time").title = s?.orca_min == null ? (s ? "The G-code names no print time" : "")
    : `Slicer ${fmtMin(s.orca_min)} × ${s.factor} + ${s.warmup_min} min warm-up` +
    (s.factor_from_prints ? ` (factor from ${s.factor_from_prints} prints)` : " (not calibrated yet)");
  $("info-grams").textContent = s ? `${s.grams.toFixed(1)} g ${s.material} · ${s.layer_height} mm` : "";
  for (const [id, kind, ok] of [["dl-stl", "stl", !!p?.size && p.kind !== "gcode"], ["dl-gcode", "gcode", !!p?.gcode_ready]]) {
    $(id).hidden = !ok;
    if (p) $(id).href = `/files/${kind}/${p.name}`;
  }
  updatePrintButton();
  if (p?.glb) loadModel(p.glb, p.mesh); else clearModel();
  // An imported G-code has no 3D model: the page turns the layer view on for it,
  // and off again for the next part when it turned it on itself.
  if (p?.kind === "gcode") {
    $("viewer-empty").hidden = true;
    if (layers && !layers.state.on) { state.autoLayers = true; return layers.show(true); }
  } else if (state.autoLayers) {
    state.autoLayers = false;
    if (layers?.state.on) layers.show(false);
  }
  layers?.update(); // layers: show the new part's G-code
}

function updatePrintButton() {
  const p = state.parts.find((x) => x.name === state.selected);
  const idle = READY_STATES.includes(state.printer.state);
  const btn = $("print-btn");
  if (state.plateMode) {
    const pl = state.plate;
    btn.textContent = "Print plate…";
    btn.disabled = !(pl?.ready && idle);
    btn.title = !pl?.layout ? "Arrange the plate first" : !pl.layout.fits ? "The parts do not fit"
      : !pl.ready ? "A part changed. Arrange again." : !idle ? `Printer is ${state.printer.state}` : "Print this plate";
    return;
  }
  btn.textContent = "Print…";
  btn.disabled = !(p && p.gcode_ready && idle);
  btn.title = !p ? "Select a part" : !p.gcode_ready ? "Not sliced, or the model changed after slicing"
    : !idle ? `Printer is ${state.printer.state}` : "Print this part";
}

// ---------- Printer ----------
async function pollPrinter() {
  try {
    state.printer = await api("/api/printer");
  } catch { state.printer = { state: "offline" }; }
  const p = state.printer;
  $("printer").dataset.state = p.state;
  const busy = p.state === "printing" || p.state === "paused";
  $("printer-state").textContent = busy ? `${p.state} ${p.progress}%` : p.state;
  $("printer-state").title = p.file || "";
  $("printer").title = `${p.name || CONFIG.printer_name}: ${p.state}` + (p.state === "offline" && p.message ? ` (${p.message})` : "");
  $("printer-temps").textContent = p.nozzle ? `🌡 ${p.nozzle} · bed ${p.bed}` : "";
  $("printer-progress").hidden = !busy;
  $("printer-progress").firstElementChild.style.width = `${p.progress || 0}%`;
  $("printer-cancel").hidden = !busy;
  updatePrintButton();
  layers?.printer(p); // layers: time left and follow print
}

$("print-btn").addEventListener("click", () => {
  const p = state.parts.find((x) => x.name === state.selected);
  if (!state.plateMode && !p) return;
  const s = (state.plateMode ? state.plate?.stats : p.stats) || {};
  const head = state.plateMode
    ? [["Plate", plateSummary(state.plate)], ["Project", state.proj?.name || state.project]]
    : [["Part", p.name + (p.kind === "gcode" ? " (imported G-code)" : "")], ["Size", fmtSize(p.size)],
      ...(p.warnings || []).map((w) => ["⚠ Check", w])];
  $("print-details").innerHTML = [
    ...head,
    ["Time", fmtTime(s)],
    ["Filament", s.grams ? `${s.grams.toFixed(1)} g ${s.material}` : "?"],
    ["Layers", s.layer_height ? `${s.layer_height} mm` : "?"],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${esc(String(v))}</dd>`).join("");
  const mat = s.material && s.material !== "?" ? s.material : CONFIG.material; // "?": the G-code names no material
  $("chk-fil-text").textContent = `${mat || "The right filament"} is loaded.`;
  $("chk-bed").checked = $("chk-fil").checked = false;
  $("print-yes").disabled = true;
  $("print-error").hidden = true;
  $("print-modal").hidden = false;
});
for (const id of ["chk-bed", "chk-fil"]) {
  $(id).addEventListener("change", () => ($("print-yes").disabled = !($("chk-bed").checked && $("chk-fil").checked)));
}
$("print-no").addEventListener("click", () => ($("print-modal").hidden = true));
$("print-yes").addEventListener("click", async () => {
  $("print-yes").disabled = true;
  try {
    const r = await api(state.plateMode ? `/api/projects/${state.project}/plate/print` : `/api/print/${state.selected}`, { method: "POST" });
    $("print-modal").hidden = true;
    toast(`Print started: ${r.file}`);
    pollPrinter();
  } catch (err) {
    $("print-error").textContent = err.message;
    $("print-error").hidden = false;
    $("print-yes").disabled = false;
  }
});
$("printer-cancel").addEventListener("click", () => ($("cancel-modal").hidden = false));
$("cancel-no").addEventListener("click", () => ($("cancel-modal").hidden = true));
$("cancel-yes").addEventListener("click", async () => {
  $("cancel-modal").hidden = true;
  try { await api("/api/printer/cancel", { method: "POST" }); toast("Print cancelled."); } catch (err) { toast(err.message); }
  pollPrinter();
});

// ---------- Markdown (small, safe subset) ----------
function md(src) {
  const blocks = [];
  src = src.replace(/```[^\n]*\n([\s\S]*?)(```|$)/g, (_, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of src.split("\n")) {
    let m;
    if ((m = line.match(/^\u0000(\d+)\u0000$/))) { closeList(); out.push(blocks[+m[1]]); }
    else if ((m = line.match(/^\s*[-*]\s+(.*)/))) { if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^#{1,6}\s+(.*)/))) { closeList(); out.push(`<h4>${inline(m[1])}</h4>`); }
    else if (line.trim() === "") { closeList(); }
    else { closeList(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

// ---------- Chat ----------
const messagesEl = $("messages");

function scrollDown() { messagesEl.scrollTop = messagesEl.scrollHeight; }

function fileUrl(name) { return `/api/projects/${state.project}/files/${encodeURIComponent(name)}`; }
const isImage = (name) => /\.(png|jpe?g|webp|gif)$/i.test(name);

function addUser(text, part, files = []) {
  messagesEl.querySelector(".hint")?.remove();
  const div = document.createElement("div");
  div.className = "msg user";
  const att = files.map((f) => isImage(f)
    ? `<a href="${fileUrl(f)}" target="_blank"><img class="thumb" src="${fileUrl(f)}" alt="${esc(f)}"></a>`
    : `<span class="about">📄 ${esc(f)}</span>`).join("");
  div.innerHTML = (part ? `<span class="about">about ${esc(part)}</span>` : "") + (att ? `<div class="msg-files">${att}</div>` : "") + esc(text);
  messagesEl.appendChild(div);
  scrollDown();
}

function addAssistant(text = "", tools = []) {
  messagesEl.querySelector(".hint")?.remove();
  const div = document.createElement("div");
  div.className = "msg assistant";
  div.innerHTML = `<details class="tools" hidden><summary></summary><ol></ol></details><div class="body"></div>`;
  messagesEl.appendChild(div);
  const bubble = { div, text, tools: [] };
  tools.forEach((t) => addTool(bubble, t));
  renderBubble(bubble);
  return bubble;
}

function addTool(bubble, text) {
  const det = bubble.div.querySelector(".tools");
  det.hidden = false;
  const li = document.createElement("li");
  li.textContent = text;
  det.querySelector("ol").appendChild(li);
  bubble.tools.push(text);
  det.querySelector("summary").textContent = `${bubble.tools.length} step${bubble.tools.length > 1 ? "s" : ""}: ${text.slice(0, 70)}`;
}

function renderBubble(b) {
  b.div.querySelector(".body").innerHTML = md(b.text);
  scrollDown();
}

function setBusy(busy, label = "") {
  $("chat-send").disabled = busy;
  $("chat-stop").hidden = !busy;
  $("chat-status").textContent = label;
}

function attach(runId, bubble, start = 0) {
  state.runId = runId;
  setBusy(true, "Claude is working…");
  let seen = start;
  bubble.div.querySelector(".body").classList.add("typing");
  const es = new EventSource(`/api/runs/${runId}/events?start=${start}`);
  let pending = false;
  es.onmessage = (e) => {
    seen += 1;
    const ev = JSON.parse(e.data);
    if (ev.type === "session") {
      state.sessionId = ev.session_id;
      store.set(`session:${state.project}`, ev.session_id);
    } else if (ev.type === "text") {
      bubble.text += ev.text;
      if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; renderBubble(bubble); }); }
    } else if (ev.type === "tool") {
      addTool(bubble, ev.text);
      $("chat-status").textContent = ev.text.startsWith("$ tools/make.sh") ? "Building and slicing…" : "Claude is working…";
    } else if (ev.type === "tool_error") {
      bubble.div.querySelector(".tools ol li:last-child")?.classList.add("fail");
    } else if (ev.type === "error") {
      const p = document.createElement("p");
      p.className = "msg error";
      p.textContent = ev.text;
      bubble.div.appendChild(p);
    } else if (ev.type === "done") {
      es.close();
      finish(ev, bubble);
    }
  };
  es.onerror = () => {
    es.close();
    if (state.runId === runId) setTimeout(() => attach(runId, bubble, seen), 1500);
  };
}

async function finish(ev, bubble) {
  state.runId = null;
  bubble.div.querySelector(".body").classList.remove("typing");
  renderBubble(bubble);
  setBusy(false);
  await refreshParts();
  await loadChatList();
  const changed = ev.changed || [];
  if (changed.length) {
    const pick = changed.includes(state.selected) ? state.selected : changed[0];
    if (pick !== state.selected) selectPart(pick);
    state.loadedGlb = null; // force reload of the new file
    showPart();
    toast(`Updated: ${changed.join(", ")}`);
  }
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("chat-input").value.trim();
  if (state.uploading) return toast("Wait until the files are uploaded.");
  if (!text || state.runId) return;
  const part = $("context-chip").hidden ? null : state.selected;
  const files = attachments.chat.map((f) => f.name);
  $("chat-input").value = "";
  autosize();
  attachments.chat = [];
  renderFiles("chat");
  addUser(text, part, files);
  const bubble = addAssistant();
  try {
    const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text, session_id: state.sessionId, part, project: state.project, files }) });
    attach(r.run_id, bubble);
  } catch (err) {
    bubble.text = "";
    renderBubble(bubble);
    bubble.div.innerHTML = `<p class="msg error">${esc(err.message)}</p>`;
    setBusy(false);
  }
});
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("chat-form").requestSubmit(); }
});
function autosize() { const t = $("chat-input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; }
$("chat-input").addEventListener("input", autosize);
$("chat-stop").addEventListener("click", () => state.runId && api(`/api/runs/${state.runId}/stop`, { method: "POST" }).catch(() => {}));
$("context-clear").addEventListener("click", () => ($("context-chip").hidden = true));

$("chat-new").addEventListener("click", () => {
  if (state.runId) return toast("Wait until Claude finishes.");
  state.sessionId = null;
  store.set(`session:${state.project}`, null);
  showHint();
  $("chat-select").value = "";
  $("chat-input").focus();
});

async function loadChatList() {
  const chats = await api(`/api/chats?project=${encodeURIComponent(state.project)}`);
  const sel = $("chat-select");
  sel.innerHTML = `<option value="">New conversation</option>` +
    chats.map((c) => `<option value="${c.id}">${esc(c.title)}</option>`).join("");
  sel.value = state.sessionId && chats.some((c) => c.id === state.sessionId) ? state.sessionId : "";
}
$("chat-select").addEventListener("change", async (e) => {
  if (state.runId) { toast("Wait until Claude finishes."); e.target.value = state.sessionId || ""; return; }
  state.sessionId = e.target.value || null;
  store.set(`session:${state.project}`, state.sessionId);
  await loadChat();
});

async function loadChat() {
  messagesEl.innerHTML = "";
  if (!state.sessionId) return;
  try {
    const chat = await api(`/api/chats/${state.sessionId}`);
    for (const m of chat.messages) {
      if (m.role === "user") addUser(m.text, m.part, m.files || []);
      else addAssistant(m.text, m.tools || []);
    }
  } catch { /* chat file gone: start fresh */ }
}

// ---------- Ideas ----------
// Product specs or a product page in, 3 printable ideas out. "3 new ideas" and the
// category chips ask again and skip every title already shown.
const STEERS = ["Any", "Holder / mount", "Storage / organizer", "Cover / protection",
  "Accessory / upgrade", "Repair / spare part", "Decor / fun"];
const ideas = {}; // per project, saved in projects/<id>/project.json
function saveIdeas() {
  if (!state.project || state.project === "unsorted") return;
  const { busy, ...rest } = ideas;
  api(`/api/projects/${state.project}`, { method: "PATCH", body: JSON.stringify({ ideas: rest }) }).catch(() => {});
}

function ideasStep() {
  const has = !!ideas.product;
  $("ideas-start").hidden = has;
  $("ideas-result").hidden = !has;
  if (!has) return;
  const p = ideas.product;
  $("ideas-product").textContent = p.name;
  $("ideas-summary").textContent = p.summary;
  $("ideas-facts").innerHTML = p.facts.map((f) => `<li>${esc(f)}</li>`).join("");
  $("ideas-steer").innerHTML = STEERS.map((s) =>
    `<button type="button" class="btn tiny${(ideas.steer || "Any") === s ? " on" : ""}" data-steer="${s}">${s}</button>`).join("");
  $("ideas-wishes").value = ideas.wishes || "";
  $("ideas-list").innerHTML = (ideas.list || []).map((it, i) => `
    <article class="idea">
      <h3>${esc(it.title)}</h3>
      <p>${esc(it.description)}</p>
      <div class="idea-meta"><span class="chip">${esc(it.size)}</span><span class="chip">${esc(it.print)}</span></div>
      <button type="button" class="btn small primary" data-use="${i}">Use this</button>
    </article>`).join("");
}

async function askIdeas(fresh) {
  if (ideas.busy) return;
  const body = fresh
    ? { source: $("ideas-source").value.trim(), project: state.project, files: attachments.ideas.map((f) => f.name) }
    : { product: ideas.product, steer: ideas.steer === "Any" ? "" : ideas.steer || "",
        wishes: $("ideas-wishes").value.trim(), exclude: ideas.shown || [] };
  if (fresh && !body.source && !body.files.length) return $("ideas-source").focus();
  if (state.uploading) return toast("Wait until the files are uploaded.");
  ideas.busy = true;
  $("ideas-error").hidden = true;
  $("ideas-loading").hidden = false;
  $("ideas-loading-text").textContent = fresh
    ? (/https?:\/\//.test(body.source) ? "Claude reads the product page… (up to 2 min)" : "Claude reads the specs and pictures…")
    : "Claude thinks of 3 new ideas…";
  document.querySelectorAll("#ideas-modal button, #ideas-modal textarea").forEach((b) => (b.disabled = true));
  try {
    const r = await api("/api/ideas", { method: "POST", body: JSON.stringify(body) });
    if (fresh) Object.assign(ideas, { source: body.source, files: body.files, shown: [], steer: "Any", wishes: "" });
    ideas.product = r.product;
    ideas.list = r.ideas;
    ideas.shown = [...(ideas.shown || []), ...r.ideas.map((it) => it.title)];
    saveIdeas();
    ideasStep();
  } catch (e) {
    $("ideas-error").textContent = e.message;
    $("ideas-error").hidden = false;
  } finally {
    ideas.busy = false;
    $("ideas-loading").hidden = true;
    document.querySelectorAll("#ideas-modal button, #ideas-modal textarea").forEach((b) => (b.disabled = false));
  }
}

function productText() {
  const p = ideas.product;
  return `Product: ${p.name}\n${p.facts.map((f) => `- ${f}`).join("\n")}`;
}

function toChat(text) {
  if (state.runId) return toast("Wait until Claude finishes.");
  $("chat-new").click();
  $("context-chip").hidden = true; // a new part, not a change to the selected one
  const input = $("chat-input");
  input.value = text;
  $("ideas-modal").hidden = true;
  setTab("chat");
  autosize();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  toast("Check the request, then press Send.");
}

function openIdeas() {
  $("ideas-source").value = ideas.source || "";
  attachments.ideas = (ideas.files || []).map((name) => ({ name }));
  renderFiles("ideas");
  ideasStep();
  $("ideas-modal").hidden = false;
  (ideas.product ? $("ideas-more") : $("ideas-source")).focus();
}
$("ideas-open").addEventListener("click", openIdeas);
$("ideas-close").addEventListener("click", () => ($("ideas-modal").hidden = true));
$("ideas-start").addEventListener("submit", (e) => { e.preventDefault(); askIdeas(true); });
$("ideas-source").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); askIdeas(true); }
});
$("ideas-change").addEventListener("click", () => {
  ideas.product = null;
  saveIdeas();
  ideasStep();
  $("ideas-source").focus();
});
$("ideas-more").addEventListener("click", () => askIdeas(false));
$("ideas-wishes").addEventListener("change", (e) => { ideas.wishes = e.target.value; saveIdeas(); });
$("ideas-wishes").addEventListener("keydown", (e) => { if (e.key === "Enter") askIdeas(false); });
$("ideas-steer").addEventListener("click", (e) => {
  const b = e.target.closest("[data-steer]");
  if (!b || ideas.busy) return;
  ideas.steer = b.dataset.steer;
  ideasStep();
  askIdeas(false);
});
$("ideas-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-use]");
  if (b) toChat(`${ideas.list[+b.dataset.use].prompt}\n\n${productText()}`);
});
$("ideas-other").addEventListener("click", () => toChat(`${productText()}\n\nMake: `));

// ---------- Tabs (phones) ----------
function setTab(tab) {
  document.querySelector(".layout").dataset.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (tab === "view") requestAnimationFrame(resize);
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

// ---------- Attachments ----------
// Pictures and files go to projects/<id>/files/ at once. The message or the
// Ideas request then names them, and Claude opens them with Read.
const attachments = { chat: [], ideas: [] };
let attachTarget = "chat";

function renderFiles(kind) {
  const el = $(`${kind}-files`);
  el.hidden = !attachments[kind].length;
  el.innerHTML = attachments[kind].map((f, i) => `
    <span class="file-chip" title="${esc(f.name)}">
      ${isImage(f.name) ? `<img src="${fileUrl(f.name)}" alt="">` : "📄"}
      <span>${esc(f.name)}</span>
      <button type="button" class="btn tiny ghost" data-drop="${i}" title="Remove">×</button>
    </span>`).join("");
}
for (const kind of ["chat", "ideas"]) {
  $(`${kind}-files`).addEventListener("click", (e) => {
    const b = e.target.closest("[data-drop]");
    if (!b) return;
    attachments[kind].splice(+b.dataset.drop, 1);
    renderFiles(kind);
  });
}

async function uploadFiles(kind, files) {
  if (!state.project || state.project === "unsorted") return toast("Choose or create a project first.");
  state.uploading = true;
  for (const file of files) {
    const ext = (file.name.match(/\.[^.]+$/) || [""])[0] || (file.type === "image/png" ? ".png" : ".jpg");
    const name = file.name && file.name !== "image.png" ? file.name : `pasted_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}${ext}`;
    try {
      const res = await fetch(`/api/projects/${state.project}/files?name=${encodeURIComponent(name)}`, {
        method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || res.statusText);
      attachments[kind].push(data);
      renderFiles(kind);
    } catch (e) { toast(`${name}: ${e.message}`, 5000); }
  }
  state.uploading = false;
}

function pastedFiles(e) {
  return [...(e.clipboardData?.items || [])].filter((i) => i.kind === "file").map((i) => i.getAsFile()).filter(Boolean);
}
$("chat-attach").addEventListener("click", () => { attachTarget = "chat"; $("file-input").click(); });
$("ideas-attach").addEventListener("click", () => { attachTarget = "ideas"; $("file-input").click(); });
$("file-input").addEventListener("change", (e) => { uploadFiles(attachTarget, [...e.target.files]); e.target.value = ""; });
$("chat-input").addEventListener("paste", (e) => { const f = pastedFiles(e); if (f.length) { e.preventDefault(); uploadFiles("chat", f); } });
$("ideas-source").addEventListener("paste", (e) => { const f = pastedFiles(e); if (f.length) { e.preventDefault(); uploadFiles("ideas", f); } });
for (const [zone, kind] of [["chat-form", "chat"], ["ideas-start", "ideas"]]) {
  $(zone).addEventListener("dragover", (e) => { e.preventDefault(); $(zone).classList.add("drop"); });
  $(zone).addEventListener("dragleave", () => $(zone).classList.remove("drop"));
  $(zone).addEventListener("drop", (e) => {
    e.preventDefault();
    $(zone).classList.remove("drop");
    if (e.dataTransfer.files.length) uploadFiles(kind, [...e.dataTransfer.files]);
  });
}

// ---------- Projects ----------
async function loadProjectList() {
  const list = await api("/api/projects");
  if (!list.some((p) => p.id === state.project)) state.project = list[0]?.id || "";
  $("project-select").innerHTML = list.map((p) =>
    `<option value="${p.id}">${esc(p.name)}${p.id === "unsorted" ? ` (${p.parts})` : ""}</option>`).join("");
  $("project-select").value = state.project;
}

async function openProject(pid) {
  if (state.runId) { toast("Wait until Claude finishes."); $("project-select").value = state.project; return; }
  if (!pid) { // a new workspace has no project yet
    messagesEl.innerHTML = `<div class="hint muted"><p>Start with <strong>+ Project</strong>. A project keeps its own parts, chats, ideas, files and plate.</p></div>`;
    $("project-new").click();
    return;
  }
  state.project = pid;
  store.set("project", pid);
  $("project-select").value = pid;
  state.proj = await api(`/api/projects/${pid}`);
  for (const k of Object.keys(ideas)) delete ideas[k];
  Object.assign(ideas, state.proj.ideas || {});
  attachments.chat = [];
  attachments.ideas = [];
  renderFiles("chat");
  if (state.plateMode) setPlateMode(false);
  state.selected = store.get(`part:${pid}`);
  state.sessionId = store.get(`session:${pid}`);
  state.viewSet = false;
  clearModel();
  await refreshParts();
  await loadChatList();
  if (state.sessionId && $("chat-select").value !== state.sessionId) state.sessionId = null;
  await loadChat();
  if (!state.sessionId) showHint();
}

function showHint() {
  messagesEl.innerHTML = `<div class="hint muted">
    <p><strong>${esc(state.proj?.name || "")}</strong>: describe a part, or paste a picture of what it must fit.</p>
    <p>No idea yet? Use <strong>💡 Ideas</strong> with the specs, a web page or a photo of a product.</p>
    <p>Claude designs and slices it. You start the print with the Print button, or put several parts on one plate with <strong>Plate…</strong>.</p></div>`;
}

$("project-select").addEventListener("change", (e) => openProject(e.target.value));
$("project-new").addEventListener("click", () => {
  if (state.runId) return toast("Wait until Claude finishes.");
  $("project-name").value = "";
  $("project-modal").hidden = false;
  $("project-name").focus();
});
$("project-cancel").addEventListener("click", () => ($("project-modal").hidden = true));
$("project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const proj = await api("/api/projects", { method: "POST", body: JSON.stringify({ name: $("project-name").value }) });
    $("project-modal").hidden = true;
    state.project = proj.id;
    await loadProjectList();
    await openProject(proj.id);
    setTab("chat");
    if ($("project-ideas").checked) openIdeas();
  } catch (err) { toast(err.message); }
});

// ---------- Exports for manage.js ----------
export { state, api, toast, esc, fmtMin, refreshParts, loadProjectList, openProject, setTab, selectPart };

// ---------- Boot ----------
$("parts-refresh").addEventListener("click", () => refreshParts());

async function loadConfig() {
  CONFIG = await api("/api/config");
  BED = { x: CONFIG.bed[0], y: CONFIG.bed[1] };
  controls.target.copy(cad(BED.x / 2, BED.y / 2, 0));
  $("chk-fil-text").textContent = `${CONFIG.material} is loaded.`;
}

// layers: G-code layer view (web/static/layers.js)
const layers = initLayers({ scene, camera, controls, viewerEl, state, api, fmtMin, bed: () => BED, config: () => CONFIG });

async function boot() {
  try {
    await loadConfig();
    await loadProjectList();
  } catch (err) {
    toast(`Cannot reach the server: ${err.message}`, 8000);
    return;
  }
  await openProject(state.project);
  const cur = state.project ? await api("/api/runs/current") : {};
  if (cur.run_id) {
    // Re-attach to a run that is still going (page reload, other device).
    if (cur.session_id && cur.session_id !== state.sessionId) {
      state.sessionId = cur.session_id;
      store.set(`session:${state.project}`, cur.session_id);
      await loadChat();
    }
    addUser(cur.message);
    attach(cur.run_id, addAssistant());
  }
  pollPrinter();
  clearInterval(boot.timer);
  boot.timer = setInterval(pollPrinter, 5000);
}
boot();

// ---------- Hooks for other modules ----------
// slicing.js (print settings panel) uses these.
