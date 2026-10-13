// G-code layer view: shows the sliced moves of the selected part or plate, layer by layer.
// The server parses the G-code into segments per feature type (web/layers.py). Each type
// becomes one instanced mesh of thin ribbons. The layer slider only changes instanceCount
// and one uniform, so moving it never rebuilds geometry.
// app.js calls initLayers() once and then the hooks update() and printer().
import * as THREE from "three";

// Feature colours, close to OrcaSlicer's preview. Unknown types get a colour from their name.
const COLORS = {
  "Outer wall": "#ff8a1f", "Inner wall": "#ffd84d", "Overhang wall": "#3d5afe",
  "Sparse infill": "#c0392b", "Internal solid infill": "#9b59d0", "Solid infill": "#9b59d0",
  "Top surface": "#ff4f5e", "Bottom surface": "#f28cc4", "Bridge": "#3f8fd8",
  "Internal Bridge": "#7fb3e6", "Gap infill": "#f2f2f2", "Skirt": "#1fb5a0", "Brim": "#1fb5a0",
  "Skirt/Brim": "#1fb5a0", "Support": "#3ddc4a", "Support interface": "#1e8a3c",
  "Support transition": "#9be07a", "Ironing": "#ff9f80", "Prime tower": "#b5b5b5",
  "Custom": "#8b939e", "Travel": "#38b6ff",
};
const ORDER = Object.keys(COLORS);
const HALF_WIDTH = 0.21; // mm, half an extrusion line
const STEP_BIG = 10;

function colorOf(name) {
  if (COLORS[name]) return COLORS[name];
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 65%, 60%)`;
}

const VERT = /* glsl */ `
attribute vec3 iStart;
attribute vec3 iEnd;
uniform float uHalfW;
uniform float uPx;
uniform float uTopZ;
varying float vSide;
varying float vDim;
void main() {
  vec4 a = modelViewMatrix * vec4(iStart, 1.0);
  vec4 b = modelViewMatrix * vec4(iEnd, 1.0);
  vec4 p = mix(a, b, position.x);
  vec2 d = b.xy - a.xy;
  float len = length(d);
  vec2 t = len > 1e-6 ? d / len : vec2(1.0, 0.0);
  vec2 n = vec2(-t.y, t.x);
  // At least about 1.2 screen pixels wide, so far views stay visible.
  float hw = max(uHalfW, 0.6 * uPx * max(-p.z, 0.0));
  p.xy += n * hw * position.y + t * hw * (position.x * 2.0 - 1.0);
  gl_Position = projectionMatrix * p;
  vSide = position.y;
  vDim = iStart.z < uTopZ - 0.0001 ? 1.0 : 0.0;
}`;
const FRAG = /* glsl */ `
uniform vec3 uColor;
uniform float uDimAmount;
varying float vSide;
varying float vDim;
void main() {
  vec3 c = uColor * (1.0 - 0.38 * vSide * vSide);
  c = mix(c, mix(c, vec3(0.45), 0.4) * 0.55, vDim * uDimAmount);
  gl_FragColor = vec4(c, 1.0);
}`;

function lowerBound(arr, value) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] < value) lo = mid + 1; else hi = mid;
  }
  return lo;
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

export function initLayers(ctx) {
  const { scene, camera, controls, viewerEl, state } = ctx;
  const L = {
    on: false, name: null, mtime: null, index: null, types: [], layer: 0, frac: 1,
    follow: false, job: null, hidden: new Set(["Travel"]), loading: null, layerZ: [], layerSeq: [],
  };
  // Shared uniforms: one update moves every type.
  const uPx = { value: 0.001 };
  const uTopZ = { value: 0 };
  const uDimAmount = { value: 1 };

  // Everything of this view sits on three.js layer 1; the camera shows layer 1 only
  // while the view is on. The app's model and plate stay on layer 0 and hide by themselves.
  scene.traverse((o) => { if (o.isLight) o.layers.enable(1); });
  const root = new THREE.Group();
  root.rotation.x = -Math.PI / 2; // printer coordinates (mm, Z up) to three.js (Y up)
  scene.add(root);
  const paths = new THREE.Group();
  root.add(paths);
  const bed = new THREE.Group();
  root.add(bed);
  const bedMats = [];
  function buildBed() {
    bed.clear();
    const B = ctx.bed();
    const box = new THREE.Mesh(new THREE.BoxGeometry(B.x + 20, B.y + 20, 3),
      new THREE.MeshStandardMaterial({ color: 0x30353d, roughness: 0.9 }));
    box.position.set(B.x / 2, B.y / 2, -1.52);
    const pts = [];
    for (let x = 0; x <= B.x; x += 10) pts.push(x, 0, 0.0, x, B.y, 0.0);
    for (let y = 0; y <= B.y; y += 10) pts.push(0, y, 0.0, B.x, y, 0.0);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    const grid = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0x59616d }));
    grid.position.z = 0.02;
    bed.add(box, grid);
    bedMats.length = 0;
    bedMats.push(box.material, grid.material);
    bed.traverse((o) => o.layers.set(1));
    seeThrough(true);
  }
  let below = null;
  function seeThrough(force) {
    const b = camera.position.y < 0;
    if (b === below && !force) return;
    below = b;
    for (const m of bedMats) {
      m.transparent = b;
      m.opacity = b ? 0.15 : 1;
      m.depthWrite = !b;
      m.needsUpdate = true;
    }
  }
  controls.addEventListener("change", () => L.on && seeThrough());

  // Nozzle mark at the last drawn move (shown while a layer is only partly drawn).
  const nozzle = new THREE.Mesh(new THREE.ConeGeometry(1.4, 4, 18),
    new THREE.MeshStandardMaterial({ color: 0xe8ecf1, roughness: 0.4, metalness: 0.2 }));
  nozzle.rotation.x = -Math.PI / 2; // tip down (printer Z)
  nozzle.layers.set(1);
  nozzle.visible = false;
  root.add(nozzle);

  // Quad of one ribbon: x = 0 or 1 along the move, y = -1 or 1 across it.
  const quad = new THREE.BufferAttribute(new Float32Array([0, -1, 0, 1, -1, 0, 0, 1, 0, 1, 1, 0]), 3);
  const quadIndex = new THREE.BufferAttribute(new Uint16Array([0, 1, 2, 2, 1, 3]), 1);

  // ---------- UI ----------
  const tools = viewerEl.querySelector(".view-tools");
  const toggle = el("button", "btn small", "Layers");
  toggle.id = "layers-toggle";
  toggle.title = "Show the sliced G-code layer by layer";
  tools.appendChild(toggle);

  const side = el("div", "lv-side");
  side.hidden = true;
  side.innerHTML = `
    <button type="button" class="lv-step" data-step="1" title="Up one layer (Arrow Up)">▲</button>
    <div class="lv-track" role="slider" tabindex="0" aria-orientation="vertical" aria-label="Top layer">
      <div class="lv-rail"><div class="lv-fill"></div></div>
      <div class="lv-thumb"><span class="lv-label"></span></div>
    </div>
    <button type="button" class="lv-step" data-step="-1" title="Down one layer (Arrow Down)">▼</button>`;
  const track = side.querySelector(".lv-track");
  const thumb = side.querySelector(".lv-thumb");
  const fill = side.querySelector(".lv-fill");
  const label = side.querySelector(".lv-label");

  const bottom = el("div", "lv-bottom");
  bottom.hidden = true;
  bottom.innerHTML = `<input type="range" min="0" max="1000" value="1000" aria-label="Moves in the top layer">
    <span class="lv-moves"></span>`;
  const progress = bottom.querySelector("input");
  const movesLabel = bottom.querySelector(".lv-moves");

  const panel = el("div", "lv-panel");
  panel.hidden = true;
  panel.innerHTML = `
    <button type="button" class="lv-head" aria-expanded="true"><span class="lv-title">Layers</span><span class="lv-caret">▾</span></button>
    <div class="lv-body">
      <div class="lv-note muted"></div>
      <ul class="lv-legend"></ul>
      <label class="lv-follow" hidden><input type="checkbox"> <span>Follow print</span></label>
    </div>`;
  const legend = panel.querySelector(".lv-legend");
  const note = panel.querySelector(".lv-note");
  const followRow = panel.querySelector(".lv-follow");
  const followBox = followRow.querySelector("input");
  const head = panel.querySelector(".lv-head");
  if (window.matchMedia("(max-width: 860px)").matches) panel.classList.add("closed");
  head.addEventListener("click", () => {
    panel.classList.toggle("closed");
    head.setAttribute("aria-expanded", String(!panel.classList.contains("closed")));
  });

  const msg = el("div", "lv-msg");
  msg.hidden = true;
  viewerEl.append(side, bottom, panel, msg);

  // Time left in the printer pill.
  const eta = el("span", "lv-eta");
  eta.id = "printer-eta";
  eta.hidden = true;
  document.getElementById("printer-state")?.after(eta);

  function message(html) {
    msg.innerHTML = html || "";
    msg.hidden = !html;
  }

  // ---------- data ----------
  function clearPaths() {
    for (const t of L.types) {
      t.mesh.geometry.dispose();
      t.mesh.material.dispose();
    }
    paths.clear();
    L.types = [];
    L.index = null;
    L.name = null;
    L.mtime = null;
    nozzle.visible = false;
  }

  function build(index, buf) {
    clearPaths();
    const nL = index.layers.length;
    L.layerZ = index.layers.map((l) => l[0]);
    L.layerSeq = Uint32Array.from(index.layers.map((l) => l[2]));
    const scale = index.scale;
    for (const t of index.types) {
      const coords = new Int16Array(buf, t.coords, t.count * 4);
      const seq = new Uint32Array(buf, t.seq, t.count);
      const inst = new Float32Array(t.count * 6);
      let li = 0;
      for (let k = 0; k < t.count; k++) {
        const s = seq[k];
        while (li + 1 < nL && L.layerSeq[li + 1] <= s) li++;
        const z = L.layerZ[li], o = k * 6, c = k * 4;
        inst[o] = coords[c] * scale;
        inst[o + 1] = coords[c + 1] * scale;
        inst[o + 2] = z;
        inst[o + 3] = coords[c + 2] * scale;
        inst[o + 4] = coords[c + 3] * scale;
        inst[o + 5] = z;
      }
      const geo = new THREE.InstancedBufferGeometry();
      geo.setAttribute("position", quad);
      geo.setIndex(quadIndex);
      const ib = new THREE.InstancedInterleavedBuffer(inst, 6);
      geo.setAttribute("iStart", new THREE.InterleavedBufferAttribute(ib, 3, 0));
      geo.setAttribute("iEnd", new THREE.InterleavedBufferAttribute(ib, 3, 3));
      geo.instanceCount = t.count;
      const travel = t.name === "Travel";
      const mat = new THREE.ShaderMaterial({
        vertexShader: VERT, fragmentShader: FRAG, side: THREE.DoubleSide,
        uniforms: {
          uColor: { value: new THREE.Color(colorOf(t.name)) }, uHalfW: { value: travel ? 0 : HALF_WIDTH },
          uPx, uTopZ, uDimAmount,
        },
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.frustumCulled = false;
      mesh.layers.set(1);
      mesh.onBeforeRender = setPixelSize;
      paths.add(mesh);
      L.types.push({ name: t.name, count: t.count, seq, inst, mesh });
    }
    L.index = index;
    L.marks = new Uint32Array(buf, index.marks.offset, index.marks.count * 2);
    renderLegend();
  }

  function setPixelSize(renderer, _scene, cam) {
    const h = renderer.domElement.clientHeight || 1;
    uPx.value = 2 * Math.tan(THREE.MathUtils.degToRad(cam.fov / 2)) / h;
  }

  async function load(name) {
    const token = (L.loading = { name });
    // Show the spinner only for slow loads: a cached, unchanged file returns at once.
    const slow = setTimeout(() => L.loading === token && message(`<span class="spinner"></span> Reading G-code…`), 250);
    try {
      const t0 = performance.now();
      const index = await ctx.api(`/api/layers/${name}`);
      if (L.loading !== token) return;
      if (L.name === name && L.mtime === index.gcode_mtime) { message(""); return; }
      const t1 = performance.now();
      const res = await fetch(`/api/layers/${name}/data?v=${index.gcode_mtime}`);
      if (!res.ok) throw new Error(res.statusText);
      const buf = await res.arrayBuffer();
      if (L.loading !== token) return;
      const t2 = performance.now();
      const keep = L.name === name ? L.layer : null;
      build(index, buf);
      L.name = name;
      L.mtime = index.gcode_mtime;
      const last = index.layers.length - 1;
      L.layer = keep != null ? Math.min(keep, last) : last;
      L.frac = 1;
      apply();
      message("");
      L.timing = { index_ms: Math.round(t1 - t0), data_ms: Math.round(t2 - t1),
        build_ms: Math.round(performance.now() - t2), parse_ms: index.parse_ms, bytes: index.data_bytes };
      console.info("layers", name, L.timing);
    } catch (e) {
      if (L.loading !== token) return;
      clearPaths();
      renderLegend();
      message(/not sliced/i.test(e.message) ? "This part is not sliced yet." : `Could not read the G-code: ${e.message}`);
    } finally {
      clearTimeout(slow);
      if (L.loading === token) L.loading = null;
    }
  }

  // ---------- view state ----------
  function apply() {
    const idx = L.index;
    if (!idx) return;
    const nL = idx.layers.length;
    L.layer = Math.max(0, Math.min(nL - 1, L.layer));
    const s0 = L.layerSeq[L.layer];
    const s1 = L.layer + 1 < nL ? L.layerSeq[L.layer + 1] : idx.segments;
    const maxSeq = s0 + Math.round((s1 - s0) * L.frac);
    let lastSeq = -1, lastPos = null;
    for (const t of L.types) {
      const n = lowerBound(t.seq, maxSeq);
      t.mesh.geometry.instanceCount = n;
      t.mesh.visible = !L.hidden.has(t.name) && n > 0;
      if (n > 0 && t.seq[n - 1] >= s0 && t.seq[n - 1] > lastSeq && t.name !== "Travel") {
        lastSeq = t.seq[n - 1];
        lastPos = t.inst.subarray((n - 1) * 6 + 3, (n - 1) * 6 + 6);
      }
    }
    uTopZ.value = L.layerZ[L.layer];
    nozzle.visible = L.frac < 1 && !!lastPos;
    if (lastPos) nozzle.position.set(lastPos[0], lastPos[1], lastPos[2] + 2.2);
    // Slider and labels
    const f = nL > 1 ? L.layer / (nL - 1) : 1;
    thumb.style.bottom = `calc((100% - var(--lv-thumb)) * ${f.toFixed(5)})`;
    fill.style.height = `${(f * 100).toFixed(3)}%`;
    const z = L.layerZ[L.layer];
    label.innerHTML = `<b>${L.layer + 1}</b> / ${nL}<br>${z.toFixed(2)} mm`;
    track.setAttribute("aria-valuemin", "1");
    track.setAttribute("aria-valuemax", String(nL));
    track.setAttribute("aria-valuenow", String(L.layer + 1));
    track.setAttribute("aria-valuetext", `Layer ${L.layer + 1} of ${nL}, Z ${z.toFixed(2)} mm`);
    progress.value = String(Math.round(L.frac * 1000));
    const moves = maxSeq - s0;
    movesLabel.textContent = L.frac >= 1 ? `all ${s1 - s0} moves` : `${moves} / ${s1 - s0} moves`;
  }

  function setLayer(i, manual = true) {
    if (!L.index) return;
    if (manual) setFollow(false, false);
    L.layer = i;
    L.frac = 1;
    apply();
  }

  // ---------- legend ----------
  function renderLegend() {
    const types = [...L.types].sort((a, b) => {
      const ia = ORDER.indexOf(a.name), ib = ORDER.indexOf(b.name);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.name.localeCompare(b.name);
    });
    legend.innerHTML = types.map((t) => `
      <li><label><input type="checkbox" data-type="${t.name.replace(/"/g, "&quot;")}" ${L.hidden.has(t.name) ? "" : "checked"}>
      <span class="lv-swatch" style="background:${colorOf(t.name)}"></span><span>${t.name.replace(/</g, "&lt;")}</span></label></li>`).join("");
    const name = L.name || "";
    const p = state.parts.find((x) => x.name === name);
    const stale = (p && !p.gcode_ready) || (name.startsWith("plate_") && state.plate && !state.plate.ready);
    note.innerHTML = L.index
      ? `${name.replace(/</g, "&lt;")} · ${L.index.layers.length} layers` +
        (stale ? `<br><span class="lv-warn">The model changed after slicing.</span>` : "")
      : "";
  }
  legend.addEventListener("change", (e) => {
    const box = e.target.closest("[data-type]");
    if (!box) return;
    if (box.checked) L.hidden.delete(box.dataset.type); else L.hidden.add(box.dataset.type);
    apply();
  });

  // ---------- slider input ----------
  function layerFromPointer(ev) {
    const r = track.getBoundingClientRect();
    const pad = thumb.offsetHeight / 2;
    const f = 1 - (ev.clientY - r.top - pad) / Math.max(1, r.height - 2 * pad);
    const n = L.index ? L.index.layers.length : 1;
    return Math.round(Math.max(0, Math.min(1, f)) * (n - 1));
  }
  track.addEventListener("pointerdown", (ev) => {
    if (!L.index) return;
    ev.preventDefault();
    track.setPointerCapture(ev.pointerId);
    track.focus({ preventScroll: true });
    track.classList.add("drag");
    setLayer(layerFromPointer(ev));
  });
  track.addEventListener("pointermove", (ev) => {
    if (track.hasPointerCapture(ev.pointerId)) setLayer(layerFromPointer(ev));
  });
  const endDrag = (ev) => {
    if (track.hasPointerCapture?.(ev.pointerId)) track.releasePointerCapture(ev.pointerId);
    track.classList.remove("drag");
  };
  track.addEventListener("pointerup", endDrag);
  track.addEventListener("pointercancel", endDrag);
  track.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    setLayer(L.layer + (ev.deltaY < 0 ? 1 : -1));
  }, { passive: false });
  side.querySelectorAll(".lv-step").forEach((b) => {
    let timer = null;
    const stop = () => { clearInterval(timer); timer = null; };
    b.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      const d = +b.dataset.step;
      setLayer(L.layer + d);
      stop();
      const start = Date.now();
      timer = setInterval(() => { if (Date.now() - start > 350) setLayer(L.layer + d); }, 60);
    });
    for (const t of ["pointerup", "pointerleave", "pointercancel"]) b.addEventListener(t, stop);
  });
  progress.addEventListener("input", () => {
    setFollow(false, false);
    L.frac = +progress.value / 1000;
    apply();
  });

  function onKey(ev) {
    if (!L.on || !L.index) return;
    const t = ev.target;
    if (t !== track && t.closest?.("input, textarea, select, [contenteditable]")) return;
    if (document.querySelector(".overlay:not([hidden])")) return;
    const n = L.index.layers.length;
    const steps = { ArrowUp: 1, ArrowDown: -1, PageUp: STEP_BIG, PageDown: -STEP_BIG, Home: -n, End: n };
    if (ev.key in steps) {
      ev.preventDefault();
      setLayer(L.layer + steps[ev.key]);
    } else if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      ev.preventDefault();
      setFollow(false, false);
      L.frac = Math.max(0, Math.min(1, L.frac + (ev.key === "ArrowRight" ? 0.02 : -0.02)));
      apply();
    }
  }
  document.addEventListener("keydown", onKey);

  // ---------- mode ----------
  function target() {
    if (L.follow && L.job?.name) return L.job.name;
    if (state.plateMode) return state.plate?.layout?.fits && state.plate?.stats ? `plate_${state.project}` : null;
    return state.selected;
  }

  function setOn(on) {
    L.on = on;
    toggle.classList.toggle("on", on);
    viewerEl.classList.toggle("layers-on", on);
    side.hidden = bottom.hidden = panel.hidden = !on;
    camera.layers.set(on ? 1 : 0);
    if (!on) {
      message("");
      return;
    }
    if (!bed.children.length) buildBed();
    below = null;
    seeThrough();
    update();
  }
  toggle.addEventListener("click", () => setOn(!L.on));

  function update() {
    if (!L.on) return;
    const name = target();
    L.loading = null; // a load for another file still running is dropped
    if (!name) {
      clearPaths();
      renderLegend();
      message(state.plateMode ? "Arrange the plate first. Then you see its layers here." : "Select a part.");
      return;
    }
    const part = state.plateMode || L.follow ? null : state.parts.find((x) => x.name === name);
    if (part && !part.stats) { // no G-code yet: skip the request
      clearPaths();
      renderLegend();
      message("This part is not sliced yet.");
      return;
    }
    load(name).then(renderLegend);
  }

  // ---------- printer ----------
  // A manual slider move stops following but keeps the printing file on screen.
  function setFollow(on, reload = true) {
    if (L.follow === on) return;
    L.follow = on;
    followBox.checked = on;
    if (L.on && reload) update();
  }
  followBox.addEventListener("change", () => {
    setFollow(followBox.checked);
    if (followBox.checked && !L.on) setOn(true);
    if (followBox.checked) followJob();
  });

  function followJob() {
    const j = L.job;
    if (!L.follow || !j?.name || L.name !== j.name || !L.index) return;
    const nL = L.index.layers.length;
    L.layer = Math.max(0, Math.min(nL - 1, j.layer));
    // The move from the byte position: the last mark at or before it.
    let seq = L.layerSeq[L.layer];
    const m = L.marks;
    if (m && m.length) {
      let lo = 0, hi = m.length / 2 - 1, best = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (m[mid * 2] <= j.pos) { best = mid; lo = mid + 1; } else hi = mid - 1;
      }
      if (best >= 0) seq = m[best * 2 + 1];
    }
    const s0 = L.layerSeq[L.layer], s1 = L.layer + 1 < nL ? L.layerSeq[L.layer + 1] : L.index.segments;
    L.frac = Math.max(0, Math.min(1, (seq - s0) / Math.max(1, s1 - s0)));
    apply();
  }

  let jobBusy = false;
  async function printer(p) {
    const busy = p.state === "printing" || p.state === "paused";
    const ours = busy && p.file && ctx.config().file_prefix && p.file.split("/").pop().startsWith(ctx.config().file_prefix);
    if (!busy) {
      L.job = null;
      eta.hidden = true;
      followRow.hidden = true;
      return;
    }
    if (jobBusy) return;
    jobBusy = true;
    try {
      const j = await ctx.api("/api/printjob");
      L.job = j.state === "printing" || j.state === "paused" ? j : null;
    } catch { L.job = null; } finally { jobBusy = false; }
    const j = L.job;
    eta.hidden = !(j && j.time_left_min != null);
    if (j && j.time_left_min != null) {
      eta.textContent = `${ctx.fmtMin(j.time_left_min)} left`;
      eta.title = j.time_source === "slicer" ? "Slicer time for the rest of the file, times the learned factor"
        : "From the print time so far and the progress";
    }
    followRow.hidden = !(ours && j?.name);
    followRow.querySelector("span").textContent = j?.name ? `Follow print (${j.name}, layer ${j.layer + 1})` : "Follow print";
    followRow.title = j && j.exact === false ? "The local G-code changed after the upload. The position is an estimate." : "";
    if (L.follow && j?.name) {
      if (L.on && L.name !== j.name && !L.loading) await load(j.name);
      followJob();
    }
  }

  return { update, printer, get state() { return L; } };
}
