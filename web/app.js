// Airborne Survey Flight Planner -- web front end.
//
// The planning itself is survey_engine.py, run in the browser by Pyodide. This file only
// collects the form into a plan dictionary (the same JSON the desktop app saves), hands it
// to the engine, and shows what comes back. Nothing typed here is sent to a server.

const PYODIDE_INDEX = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
const STORAGE_KEY = "flightplan.plan";
const BOUNDARY_ROWS = 10;
const TRANSIT_ROWS = 10;
const AREA_MODES = ["Boundary GPS (Max 10)", "CenterPoint"];

// kind: "param" lives under plan.parameters, "top" is a top-level string, "flag" a boolean,
// "choice" a top-level dropdown. Tips are the desktop app's hover help, shortened.
const FIELDS = {
  "export-fields": [
    { kind: "top", key: "area_name", label: "Filename / Area Name", value: "Clairmont_Fire",
      tip: "Names the plan and every file it produces. Spaces become underscores." },
    { kind: "top", key: "waypoint_prefix", label: "Line ID Prefix (optional)", value: "",
      tip: "One or two letters or digits put in front of every waypoint name, to tell two areas apart in the same database. Names are limited to five characters, so a prefix only fits while there are fewer than 100 lines." },
  ],
  "area-fields": [
    { kind: "choice", key: "survey_area_mode", label: "Survey Area Defined By", value: AREA_MODES[0],
      options: AREA_MODES,
      tip: "Boundary GPS uses the corner points on the Boundary GPS tab. CenterPoint ignores them and builds a rectangle from the CenterPoint tab." },
    { kind: "flag", key: "rectangular_box", label: "Rectangular Box", value: true,
      tip: "On: fly the smallest rectangle enclosing the target plus its margin, with equal-length parallel lines. Off: clip the lines to the padded target outline, which covers less ground but gives lines of very uneven length." },
    { kind: "flag", key: "survey_only", label: "Survey box only", value: false,
      tip: "Leaves the lead-in and the transit legs out of everything drawn and downloaded. The survey lines themselves do not change. Turn it off before sending anything to the pilot." },
    { kind: "flag", key: "skip_box", label: "Skip survey box", value: false,
      tip: "Drops the box and flies only the transit waypoints and their make-lines. Needs at least two transit waypoints with coordinates." },
  ],
  "flight-fields": [
    { kind: "choice", key: "repeats", label: "Repeats (fly box N times)", value: "1",
      options: ["1", "2", "3", "4"],
      tip: "Flies the whole box that many times, with the same line directions each time." },
    { kind: "flag", key: "retrace_lines", label: "Retrace line", value: false,
      tip: "Flies each line out and back before moving to the next." },
    { kind: "choice", key: "skip_edges", label: "Skip Edge Lines (each end)", value: "0",
      options: ["0", "1", "2", "3"],
      tip: "Drops that many lines from both sides of the box. This gives up real coverage; the Summary tab reports the padding actually achieved." },
    { kind: "param", key: "groundspeed_kt", label: "Survey Groundspeed (knots)", value: "200",
      tip: "Speed flown in the pattern: the lead-in, the survey lines and the turns between them. Sets the in-pattern time and, with the bank angle, the turn radius." },
    { kind: "param", key: "transit_groundspeed_kt", label: "Transit Groundspeed (knots)", value: "200",
      tip: "Speed flown to and from the pattern: the transit waypoints and the legs from the origin airport and to the destination. Sets the transit time, and is sent to ForeFlight as the cruise speed." },
    { kind: "param", key: "bank_angle_deg", label: "Bank Angle (deg)", value: "25",
      tip: "Sets the turn radius between survey lines, together with the groundspeed. 25 is the usual autopilot bank limit. Distance, time and the drawn track include the turns; the waypoint files do not change. 0 counts turns as straight legs." },
    { kind: "param", key: "swath_width_km", label: "Swath Width (km)", value: "10.0",
      tip: "Width of ground the sensor covers on one pass." },
    { kind: "param", key: "swath_overlap", label: "Swath Overlap (0.0-1.0)", value: "0.1",
      tip: "Fraction of each swath that overlaps its neighbour. 0.1 is 10%." },
    { kind: "param", key: "perimeter_margin_km", label: "Perimeter Margin (km)", value: "5.0",
      tip: "Padding flown around the target on all sides." },
    { kind: "param", key: "initial_heading_deg", label: "Initial Heading (deg True)", value: "0",
      tip: "Direction the survey lines run, in degrees True. For magnetic north-south, enter 000 minus the local variation, e.g. 346.5 for 13.5°E." },
    { kind: "param", key: "lat_offset", label: "Latitude Offset (deg)", value: "0.0",
      tip: "Shifts the whole coverage area north (+) or south (-). A shift eats into the margin on the trailing side." },
    { kind: "param", key: "lon_offset", label: "Longitude Offset (deg)", value: "0.0",
      tip: "Shifts the whole coverage area east (+) or west (-)." },
    { kind: "param", key: "lead_in_km", label: "Lead-in Distance (km)", value: "0.0",
      tip: "Adds a waypoint this far back from the first line's start, on the line's own bearing, so the aircraft is established on track before the line begins. 0 turns it off." },
  ],
  "route-fields": [
    { kind: "param", key: "origin_airport", label: "Origin Airport", value: "KBOI",
      tip: "ICAO code, e.g. KLAX. Starts the ForeFlight route link, and when the code is in the built-in airport table it is drawn, counted in the flight distance and time, and used to pick the corner the box is entered from. Never added to the waypoint files." },
    { kind: "param", key: "destination_airport", label: "Destination Airport", value: "KBOI",
      tip: "ICAO code, e.g. KSLC. Ends the ForeFlight route link, and when the code is in the built-in airport table it is drawn and counted like the origin. Never added to the waypoint files." },
    { kind: "param", key: "survey_altitude", label: "Survey Flight Level", value: "200",
      tip: "200 means FL200, 20,000 ft. Sent to ForeFlight as the cruise altitude." },
  ],
  "center-fields": [
    { kind: "param", key: "center_lat", label: "Center Latitude", value: "43.6475", tip: "Decimal degrees; south is negative." },
    { kind: "param", key: "center_lon", label: "Center Longitude", value: "-116.1195", tip: "Decimal degrees; west is negative." },
    { kind: "param", key: "center_width_km", label: "Rectangle Width (km)", value: "12.0", tip: "Across the Initial Heading." },
    { kind: "param", key: "center_length_km", label: "Rectangle Length (km)", value: "18.0", tip: "Along the Initial Heading." },
  ],
  "line-fields": [
    { kind: "param", key: "make_line_km", label: "Make-Line Length (km)", value: "20.0",
      tip: "The whole length of a make-line, end to end. 20 gives a 20 km line centred on the waypoint." },
    { kind: "choice", key: "make_line_bearing", label: "Make-Line Bearing", value: "Along track",
      options: ["Along track", "Survey heading"],
      tip: "Along track: on the course from the waypoint above to the one below. Survey heading: parallel to the survey lines." },
    { kind: "flag", key: "make_line_keep_centre", label: "Keep centre point", value: false,
      tip: "Keeps the original waypoint between the two ends of its make-line. Costs one extra waypoint per line." },
  ],
};

const DEFAULT_BOUNDARY = [
  { lat: "43.703", lon: "-116.089", label: "Boise_Peak" },
  { lat: "43.640", lon: "-116.062", label: "Aldape_Summit" },
  { lat: "43.5949", lon: "-116.1429", label: "Table_Rock" },
  { lat: "43.635", lon: "-116.176", label: "Crestline_Trailhead" },
];

const $ = (id) => document.getElementById(id);
const controls = {};          // key -> {field, element}
let engine = null;            // survey_engine.generate_json, once Pyodide is up
let result = null;            // the last successful run
let map, layers, baseLayer = null;

// ---------- form ----------

function buildFields() {
  for (const [containerId, fields] of Object.entries(FIELDS)) {
    const box = $(containerId);
    for (const field of fields) {
      const id = "f-" + field.key;
      let element;
      if (field.kind === "flag") {
        const label = document.createElement("label");
        label.className = "check";
        label.title = field.tip;
        element = Object.assign(document.createElement("input"), { type: "checkbox", id });
        label.append(element, field.label);
        box.append(label);
      } else {
        const label = Object.assign(document.createElement("label"), { htmlFor: id, textContent: field.label });
        label.title = field.tip;
        if (field.kind === "choice") {
          element = Object.assign(document.createElement("select"), { id });
          for (const option of field.options) element.add(new Option(option, option));
        } else {
          element = Object.assign(document.createElement("input"), { type: "text", id, autocomplete: "off", spellcheck: false });
        }
        element.title = field.tip;
        box.append(label, element);
      }
      controls[field.key] = { field, element };
    }
  }
}

function textInput(className, placeholder) {
  const input = Object.assign(document.createElement("input"), { type: "text", autocomplete: "off", spellcheck: false });
  if (className) input.className = className;
  if (placeholder) input.placeholder = placeholder;
  return input;
}

// One grid row. `columns` names the cells in plan-file order; "make_line" is a checkbox.
function buildRows(tableId, count, columns) {
  const body = document.querySelector(`#${tableId} tbody`);
  for (let i = 0; i < count; i++) {
    const row = body.insertRow();
    row.insertCell().textContent = i + 1;
    for (const column of columns) {
      const cell = row.insertCell();
      const input = column === "make_line"
        ? Object.assign(document.createElement("input"), { type: "checkbox" })
        : textInput(column === "ident" ? "ident" : "", "");
      input.dataset.column = column;
      cell.append(input);
    }
    const move = row.insertCell();
    move.className = "move";
    for (const [glyph, delta, title] of [["▲", -1, "Move this row up"], ["▼", 1, "Move this row down"]]) {
      const button = Object.assign(document.createElement("button"), { type: "button", textContent: glyph, title });
      button.addEventListener("click", () => swapRows(body, i, i + delta));
      move.append(button);
    }
  }
}

function rowValues(row) {
  const values = {};
  for (const input of row.querySelectorAll("[data-column]")) {
    values[input.dataset.column] = input.type === "checkbox" ? input.checked : input.value.trim();
  }
  return values;
}

function setRowValues(row, values) {
  for (const input of row.querySelectorAll("[data-column]")) {
    const value = values ? values[input.dataset.column] : undefined;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value === undefined || value === null ? "" : String(value);
  }
}

function swapRows(body, a, b) {
  if (b < 0 || b >= body.rows.length) return;
  const first = rowValues(body.rows[a]), second = rowValues(body.rows[b]);
  setRowValues(body.rows[a], second);
  setRowValues(body.rows[b], first);
  rememberPlan();
}

function tableRows(tableId, keepIf) {
  return [...document.querySelectorAll(`#${tableId} tbody tr`)].map(rowValues).filter(keepIf);
}

function fillTable(tableId, rows) {
  const trs = document.querySelectorAll(`#${tableId} tbody tr`);
  trs.forEach((tr, i) => setRowValues(tr, rows[i]));
}

// The form as a plan dictionary: the desktop app's <AREA>_plan.json, key for key.
function planFromForm() {
  const plan = { schema: "airborne_survey_plan/1", parameters: {} };
  for (const { field, element } of Object.values(controls)) {
    if (field.kind === "flag") plan[field.key] = element.checked;
    else if (field.kind === "param") plan.parameters[field.key] = element.value.trim();
    else plan[field.key] = element.value.trim();
  }
  plan.boundary = tableRows("boundary-table", (r) => r.lat || r.lon);
  const filled = (r) => r.ident || r.lat || r.lon || r.label;
  plan.transit = { before: tableRows("before-table", filled), after: tableRows("after-table", filled) };
  return plan;
}

function defaultPlan() {
  const plan = { schema: "airborne_survey_plan/1", parameters: {}, boundary: DEFAULT_BOUNDARY,
                 transit: { before: [], after: [] } };
  for (const fields of Object.values(FIELDS)) {
    for (const field of fields) {
      if (field.kind === "param") plan.parameters[field.key] = field.value;
      else plan[field.key] = field.value;
    }
  }
  return plan;
}

// Throws on a file that is not a plan, before anything on the form is touched.
function applyPlan(data) {
  if (!data || typeof data !== "object" || !Array.isArray(data.boundary)) {
    throw new Error("Not a flight plan file: no 'boundary' list.");
  }
  if (data.schema && data.schema !== "airborne_survey_plan/1") {
    throw new Error(`Unsupported plan schema "${data.schema}".`);
  }
  const transit = data.transit || {};
  if (data.boundary.length > BOUNDARY_ROWS) {
    throw new Error(`File has ${data.boundary.length} boundary points; only ${BOUNDARY_ROWS} rows are available.`);
  }
  for (const group of ["before", "after"]) {
    if ((transit[group] || []).length > TRANSIT_ROWS) {
      throw new Error(`File has ${transit[group].length} '${group}' waypoints; only ${TRANSIT_ROWS} rows are available.`);
    }
  }
  const base = defaultPlan();
  for (const { field, element } of Object.values(controls)) {
    let value = field.kind === "param" ? (data.parameters || {})[field.key] : data[field.key];
    if (value === undefined || value === null) {
      value = field.kind === "param" ? base.parameters[field.key] : base[field.key];
    }
    if (field.kind === "flag") element.checked = Boolean(value);
    else if (field.kind === "choice") element.value = field.options.includes(String(value)) ? String(value) : field.value;
    else element.value = String(value);
  }
  fillTable("boundary-table", data.boundary);
  fillTable("before-table", transit.before || []);
  fillTable("after-table", transit.after || []);
}

function rememberPlan() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(planFromForm())); } catch (_) { /* private window */ }
}

function restorePlan() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) { applyPlan(JSON.parse(saved)); return; }
  } catch (_) { /* unreadable or stale: fall through to the demo area */ }
  applyPlan(defaultPlan());
}

// ---------- status ----------

function setStatus(text) { $("status").textContent = text; }

function showError(title, message) {
  const box = $("error");
  box.hidden = !message;
  box.textContent = message ? `${title}: ${message}` : "";
}

function showWarning(message) {
  $("warning").hidden = !message;
  $("warning").textContent = message || "";
}

// ---------- engine ----------

async function startEngine() {
  setStatus("Loading the planning engine. The first visit takes 10–20 seconds…");
  const pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX });
  await pyodide.loadPackage(["numpy", "shapely", "pyproj"]);
  const response = await fetch("survey_engine.py", { cache: "no-cache" });
  if (!response.ok) throw new Error(`survey_engine.py: HTTP ${response.status}`);
  pyodide.FS.writeFile("survey_engine.py", await response.text());
  // The airport table is optional: without it airports go undrawn and the summary says so.
  try {
    const airports = await fetch("airports.csv");
    if (airports.ok) pyodide.FS.writeFile("airports.csv", await airports.text());
  } catch (_) { /* offline or blocked: carry on without it */ }
  engine = pyodide.runPython("import survey_engine\nsurvey_engine.generate_json");
}

function generate() {
  if (!engine) return;
  const plan = planFromForm();
  rememberPlan();
  let out;
  try {
    out = JSON.parse(engine(JSON.stringify(plan)));
  } catch (err) {
    out = { ok: false, title: "Execution Error", message: String(err) };
  }
  if (!out.ok) {
    showError(out.title, out.message);
    setStatus("Not generated — fix the problem above and try again. The preview and downloads below are from the last successful run.");
    return;
  }
  result = out;
  showError("", "");
  showWarning(out.warning);
  setStatus(out.status);
  $("summary").textContent = out.summary;
  $("plan-title").textContent = out.title;
  for (const button of document.querySelectorAll("[data-file]")) button.disabled = false;
  $("save-plan").disabled = false;
  $("toggle-qr").disabled = false;
  $("open-foreflight").disabled = false;
  drawMap();
  if (!$("qr").hidden) drawQr();
}

// ---------- downloads ----------

function download(file) {
  let blob;
  if (file.b64 !== undefined) {
    const bytes = Uint8Array.from(atob(file.b64), (c) => c.charCodeAt(0));
    blob = new Blob([bytes], { type: file.mime });
  } else {
    blob = new Blob([file.text], { type: file.mime + ";charset=utf-8" });
  }
  const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: file.name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
}

// Saves what is on the form now, not the last generated plan, so nothing typed is lost.
function savePlan() {
  const plan = planFromForm();
  plan.area_name = plan.area_name.replace(/ /g, "_");
  plan.saved_utc = new Date().toISOString().slice(0, 16).replace("T", " ") + "Z";
  download({ name: `${plan.area_name || "survey_area"}_plan.json`, mime: "application/json",
             text: JSON.stringify(plan, null, 2) });
}

async function loadPlanFile(file) {
  try {
    applyPlan(JSON.parse(await file.text()));
  } catch (err) {
    showError("Load Failed", err.message);
    return;
  }
  setStatus(`Loaded plan ${file.name}.`);
  generate();
}

// ---------- map ----------

const BASEMAPS = {
  topo: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
  satellite: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
};

function startMap() {
  map = L.map("map", { zoomSnap: 0.25, attributionControl: true }).setView([43.65, -116.12], 10);
  map.attributionControl.setPrefix("");
  L.control.scale({ imperial: false }).addTo(map);
  layers = L.layerGroup().addTo(map);
}

function setBasemap(name) {
  if (baseLayer) { map.removeLayer(baseLayer); baseLayer = null; }
  if (BASEMAPS[name]) {
    baseLayer = L.tileLayer(BASEMAPS[name], { maxZoom: 19, attribution: "Tiles © Esri" }).addTo(map);
    baseLayer.bringToBack();
  }
}

function dot(lat, lon, name, options, labelClass, permanent) {
  const marker = L.circleMarker([lat, lon], options);
  marker.bindTooltip(name, { permanent, direction: "right", offset: [6, 0], className: "wp-label " + labelClass });
  marker.bindPopup(`${name}<br>${lat.toFixed(6)}, ${lon.toFixed(6)}`);
  return marker.addTo(layers);
}

function drawMap() {
  const p = result.preview;
  const labels = $("show-labels").checked;
  layers.clearLayers();
  const everything = [];

  if (p.envelope.length) {
    L.polygon(p.envelope, { color: "#1f6fd0", weight: 2, fillOpacity: 0.08 }).addTo(layers);
    everything.push(...p.envelope);
  }
  L.polyline(p.track, { color: "#d81b1b", weight: 3, dashArray: "5 6" }).addTo(layers);
  everything.push(...p.track);

  // Transit legs join the chain to the end of the track they lead into or out of.
  const legs = [[p.transit_before, true], [p.transit_after, false]];
  for (const [chain, leads] of legs) {
    if (!chain.length) continue;
    const points = chain.map(([lat, lon]) => [lat, lon]);
    const anchor = leads ? p.track[0] : p.track[p.track.length - 1];
    L.polyline(leads ? [...points, anchor] : [anchor, ...points],
               { color: "#8a8a8a", weight: 3, dashArray: "2 8" }).addTo(layers);
    for (const [lat, lon, name] of chain) {
      dot(lat, lon, name, { radius: 5, color: "#444444", weight: 2, fillColor: "#8a8a8a", fillOpacity: 0.9 }, "transit", labels);
    }
    everything.push(...points);
  }

  for (const [lat, lon, label] of p.boundary) {
    dot(lat, lon, label, { radius: 5, color: "#7b2fbe", weight: 1, fillColor: "#7b2fbe", fillOpacity: 0.9 }, "boundary", labels);
  }
  // Past a couple of dozen, permanent labels are an unreadable mat; fall back to hover.
  const waypointLabels = labels && p.waypoints.length <= 24;
  p.waypoints.forEach(([lat, lon, name], i) => {
    const first = i === 0;
    dot(lat, lon, name, { radius: first ? 7 : 4, color: first ? "#0a8a3a" : "#d81b1b", weight: first ? 3 : 1,
                          fillColor: "#d81b1b", fillOpacity: 0.9 }, "", waypointLabels);
  });

  map.invalidateSize();
  if (everything.length) map.fitBounds(L.latLngBounds(everything).pad(0.08));
}

// ---------- QR ----------

function drawQr() {
  const canvas = $("qr-canvas"), note = $("qr-note");
  let code;
  try {
    // Lowest error correction: the route is long, and density is what stops a scan.
    code = qrcode(0, "L");
    code.addData(result.route_url);
    code.make();
  } catch (_) {
    canvas.width = canvas.height = 10;
    note.textContent = "This route is too long to fit in a QR code. Use the content pack or the route link file instead.";
    return;
  }
  const count = code.getModuleCount(), quiet = 4;
  const scale = Math.max(2, Math.floor(640 / (count + 2 * quiet)));
  canvas.width = canvas.height = (count + 2 * quiet) * scale;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#000000";
  for (let r = 0; r < count; r++) {
    for (let c = 0; c < count; c++) {
      if (code.isDark(r, c)) ctx.fillRect((c + quiet) * scale, (r + quiet) * scale, scale, scale);
    }
  }
  note.textContent = "Scan with the iPad camera to open this survey in ForeFlight as a route " +
    `(${result.route_url.length} characters).`;
}

// The browser hands foreflightmobile:// to ForeFlight when it is installed on this device.
// When it is not, most browsers do nothing at all, so say what to do instead.
function openInForeFlight() {
  if (!result) return;
  window.location.href = result.route_url;
  setStatus("Opening ForeFlight… If nothing happens, ForeFlight is not installed on this " +
            "device: use Show QR and scan it with the iPad, or download the content pack.");
}

function toggleQr() {
  const showing = !$("qr").hidden;
  $("qr").hidden = showing;
  $("toggle-qr").textContent = showing ? "Show QR" : "Show Map";
  if (showing) map.invalidateSize(); else drawQr();
}

// ---------- wiring ----------

function selectTab(name) {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  }
  for (const tab of document.querySelectorAll(".tab")) tab.hidden = tab.id !== "tab-" + name;
}

async function main() {
  buildFields();
  buildRows("boundary-table", BOUNDARY_ROWS, ["lat", "lon", "label"]);
  buildRows("before-table", TRANSIT_ROWS, ["ident", "lat", "lon", "label", "make_line"]);
  buildRows("after-table", TRANSIT_ROWS, ["ident", "lat", "lon", "label", "make_line"]);
  restorePlan();
  startMap();

  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
  }
  $("generate").addEventListener("click", generate);
  $("inputs").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.tagName === "INPUT") generate();
  });
  $("inputs").addEventListener("change", rememberPlan);
  $("save-plan").addEventListener("click", savePlan);
  $("load-plan").addEventListener("click", () => $("plan-file").click());
  $("plan-file").addEventListener("change", (event) => {
    if (event.target.files[0]) loadPlanFile(event.target.files[0]);
    event.target.value = "";
  });
  $("reset-plan").addEventListener("click", () => { applyPlan(defaultPlan()); generate(); });
  $("clear-boundary").addEventListener("click", () => { fillTable("boundary-table", []); rememberPlan(); });
  $("clear-transit").addEventListener("click", () => {
    fillTable("before-table", []); fillTable("after-table", []); rememberPlan();
  });
  $("show-labels").addEventListener("change", () => { if (result) drawMap(); });
  $("basemap").addEventListener("change", (event) => setBasemap(event.target.value));
  $("toggle-qr").addEventListener("click", toggleQr);
  $("open-foreflight").addEventListener("click", openInForeFlight);
  for (const button of document.querySelectorAll("[data-file]")) {
    button.addEventListener("click", () => download(result.files[button.dataset.file]));
  }

  try {
    await startEngine();
  } catch (err) {
    showError("Could not start", `The planning engine failed to load (${err.message || err}). Check the internet connection and reload the page.`);
    setStatus("Not ready.");
    return;
  }
  $("generate").disabled = false;
  generate();
}

main();
