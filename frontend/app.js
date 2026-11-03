/* Housing Opportunity Finder - frontend. Talks only to /api (proxied by the gateway). */
(() => {
  "use strict";
  const API = (window.API_BASE || "") + "/api";
  const $ = (id) => document.getElementById(id);
  const fmt = (n, d = 0) => (n === null || n === undefined || Number.isNaN(n)) ? "–"
    : Number(n).toLocaleString("en-AU", { maximumFractionDigits: d, minimumFractionDigits: d });

  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
  const OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

  let meta = null, map = null, inflight = null, debounce = null, lastScenario = null, lastLots = null;

  // ------------------------------------------------------------ api
  async function api(path, body, opts = {}) {
    const res = await fetch(API + path, {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: opts.signal,
    });
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); msg = j.detail?.[0]?.msg || j.detail || msg; } catch (_) { /* not json */ }
      const rid = res.headers.get("X-Request-ID");
      throw new Error(rid ? `${msg} (request ${rid})` : msg);
    }
    return opts.raw ? res : res.json();
  }

  function status(text, isError = false) {
    const el = $("status");
    if (!text) { el.hidden = true; return; }
    el.textContent = text; el.className = "status" + (isError ? " error" : ""); el.hidden = false;
  }

  // ------------------------------------------------------------ scenario state
  const SLIDERS = ["radius_m", "min_lot_m2", "min_width_m", "max_coverage", "dwellings_per_ha",
                   "services_walk_m", "max_supermarket_m", "w_services",
                   "w_proximity", "w_lot_size", "w_open_land", "w_demand", "max_green_frac"];
  const CHECKS = ["exclude_overlays", "exclude_review_overlays", "use_walking", "exclude_recent_change"];

  function readScenario() {
    const s = {};
    SLIDERS.forEach((k) => (s[k] = parseFloat($(k).value)));
    CHECKS.forEach((k) => (s[k] = $(k).checked));
    s.zones = [...document.querySelectorAll('input[name=zone]:checked')].map((i) => i.value);
    return s;
  }

  function writeOutputs(s) {
    $("o-radius").textContent = `${fmt(s.radius_m)} m`;
    $("o-minlot").textContent = `${fmt(s.min_lot_m2)} m²`;
    $("o-width").textContent = s.min_width_m > 0 ? `${fmt(s.min_width_m)} m` : "any";
    $("o-cov").textContent = `${Math.round(s.max_coverage * 100)}%`;
    $("o-dph").textContent = `${fmt(s.dwellings_per_ha)} homes/ha`;
    $("o-wp").textContent = s.w_proximity.toFixed(2);
    $("o-wl").textContent = s.w_lot_size.toFixed(2);
    $("o-wo").textContent = s.w_open_land.toFixed(2);
    $("o-wd").textContent = s.w_demand.toFixed(2);
    $("o-ws").textContent = s.w_services.toFixed(2);
    $("o-svc").textContent = `${fmt(s.services_walk_m)} m walk`;
    $("o-super").textContent = s.max_supermarket_m > 0 ? `${fmt(s.max_supermarket_m)} m walk` : "off";
    $("o-green").textContent = s.max_green_frac >= 1 ? "off" : `${Math.round(s.max_green_frac * 100)}%`;

    const zones = s.zones.length === 2 ? "residential" : s.zones.includes("GRZ") ? "General Residential" : "Neighbourhood Residential";
    const how = s.use_walking && meta?.flags.walking ? "walk" : "straight line";
    const mins = Math.round(s.radius_m / 80);
    $("sentence").innerHTML =
      `If ${zones} lots within <b>${fmt(s.radius_m)} m</b> ${how === "walk" ? `(about ${mins} minutes' walk)` : "in a straight line"} of a station, ` +
      `at least <b>${fmt(s.min_lot_m2)} m²</b> and less than <b>${Math.round(s.max_coverage * 100)}%</b> built on, ` +
      `were allowed <b>${fmt(s.dwellings_per_ha)} homes per hectare</b>…`;
  }

  function onChange() {
    const s = readScenario();
    writeOutputs(s);
    clearTimeout(debounce);
    debounce = setTimeout(() => runScenario(s), 220);
  }

  // ------------------------------------------------------------ results
  async function runScenario(s) {
    if (inflight) inflight.abort();
    inflight = new AbortController();
    lastScenario = s;
    $("answer").setAttribute("aria-busy", "true");
    try {
      const d = await api("/scenario", { scenario: s, max_lots: 8000 }, { signal: inflight.signal });
      renderResults(d);
      lastLots = d.lots;
      if (map?.getSource("lots")) map.getSource("lots").setData(d.lots);
      status(d.summary.eligible_lots > d.summary.lots_returned
        ? `Map shows the top ${fmt(d.summary.lots_returned)} of ${fmt(d.summary.eligible_lots)} lots. Downloads include all of them.` : "");
    } catch (e) {
      if (e.name !== "AbortError") status(`Couldn't run the scenario: ${e.message}`, true);
    } finally {
      $("answer").setAttribute("aria-busy", "false");
    }
  }

  function renderResults(d) {
    const s = d.summary, inf = d.infrastructure;
    $("r-homes").textContent = fmt(s.new_dwellings);
    $("r-lots").textContent = fmt(s.eligible_lots);
    $("r-people").textContent = fmt(inf.new_residents);
    $("r-open").textContent = `${fmt(s.open_land_ha, 1)} ha`;
    $("r-review").textContent = fmt(s.needs_review);
    const w = d.warnings || [];
    $("warnings").hidden = !w.length;
    $("warnings").innerHTML = w.map((t) => `<p>${esc(t)}</p>`).join("");
    $("answer").classList.toggle("has-warning", w.length > 0);

    const max = Math.max(1, ...d.by_station.map((r) => r.new_dwellings));
    $("t-stations").innerHTML = d.by_station.length ? `<table><thead><tr><th>Station</th><th class="n">Lots</th><th class="n">Homes</th></tr></thead><tbody>${
      d.by_station.map((r) => `<tr><td>${esc(r.station)}<span class="bar" style="width:${(r.new_dwellings / max) * 100}%"></span></td><td class="n">${fmt(r.lots)}</td><td class="n">${fmt(r.new_dwellings)}</td></tr>`).join("")
    }</tbody></table>` : `<p class="muted">No lots qualify. Try a longer distance or a smaller minimum lot.</p>`;

    $("t-excl").innerHTML = `<table><thead><tr><th>Reason</th><th class="n">Lots</th></tr></thead><tbody>${
      d.exclusions.map((r) => `<tr><td>${esc(r.reason)}</td><td class="n">${fmt(r.lots)}</td></tr>`).join("")
    }</tbody></table><p class="muted">Each lot is counted under the first rule it fails.</p>`;

    const sa = d.summary.services_access || {};
    const cats = meta.amenities?.categories || {};
    $("t-needs").innerHTML = Object.keys(sa).length ? `<table class="needs-table"><thead><tr><th>Everyday need</th><th class="n">Within ${fmt(lastScenario?.services_walk_m)} m</th></tr></thead><tbody>${
      Object.entries(sa).map(([k, v]) => `<tr><td>${esc(cats[k]?.label || k)}<span class="bar" style="width:${v * 100}%"></span></td><td class="n">${Math.round(v * 100)}%</td></tr>`).join("")
    }</tbody></table><p class="muted">Measured as a walk along streets and paths. Click a lot to see its nearest supermarket, GP, hospital and more.</p>`
      : `<p class="muted">Services data isn't loaded yet. Run the pipeline's refresh step.</p>`;

    const a = d.assumptions;
    $("t-infra").innerHTML = `<div class="infra-grid">
        <div><span>Water</span><b>${fmt(inf.water_kl_day, 1)} kL/day</b></div>
        <div><span>Wastewater</span><b>${fmt(inf.wastewater_kl_day, 1)} kL/day</b></div>
        <div><span>Peak electricity</span><b>${fmt(inf.peak_mva, 2)} MVA</b></div>
        <div><span>School places</span><b>${fmt(inf.primary_students)} primary, ${fmt(inf.secondary_students)} secondary</b></div>
      </div>
      <p class="muted">Extra demand only. Spare capacity is held by the water corporation, electricity distributor and Department of Education.
      Rates are placeholders until confirmed: ${a.water_l_per_person_day} L water per person per day, ${a.peak_kva_per_dwelling} kVA per home,
      ${a.primary_students_per_dwelling} primary and ${a.secondary_students_per_dwelling} secondary students per home; residents per home from ${esc(String(a.persons_per_new_dwelling))}.</p>`;
  }

  const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ------------------------------------------------------------ tabs
  function initTabs() {
    const tabs = [...document.querySelectorAll("[role=tab]")];
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => select(t));
      t.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          const n = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
          n.focus(); select(n);
        }
      });
    });
    function select(tab) {
      tabs.forEach((t) => { const on = t === tab; t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1; $(t.getAttribute("aria-controls")).hidden = !on; });
    }
  }

  // ------------------------------------------------------------ map
  function initMap() {
    const sat = meta.satellite;
    const b = sat?.png_bounds; // [lon0, lat0, lon1, lat1]
    map = new maplibregl.Map({
      container: "map",
      center: meta.center, zoom: 13.2, maxZoom: 20,
      attributionControl: { compact: true },
      style: {
        version: 8,
        sources: {
          street: { type: "raster", tiles: [OSM], tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors" },
          aerial: { type: "raster", tiles: [ESRI], tileSize: 256, maxzoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics (display only)" },
          ...(meta.flags.satellite_image && b ? { sentinel: { type: "image", url: `${API}/layers/satellite.png`,
            coordinates: [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]] } } : {}),
        },
        layers: [
          { id: "bg", type: "background", paint: { "background-color": "#e4e8e2" } },
          { id: "street", type: "raster", source: "street", paint: { "raster-saturation": -0.6, "raster-opacity": 0.85 } },
          { id: "aerial", type: "raster", source: "aerial", layout: { visibility: "none" } },
          ...(meta.flags.satellite_image && b ? [{ id: "sentinel", type: "raster", source: "sentinel", layout: { visibility: "none" }, paint: { "raster-opacity": 0.9, "raster-resampling": "nearest" } }] : []),
        ],
      },
    });
    window.housingMap = map; // handy for debugging in the browser console
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

    // Attach data layers as soon as the style is ready - not on "load", which waits for every
    // background tile and never fires if tile servers are blocked (common on government networks).
    const setup = async () => {
      const [boundary, stations] = await Promise.all([api("/layers/boundary"), api("/layers/stations")]);
      map.addSource("boundary", { type: "geojson", data: boundary });
      map.addLayer({ id: "boundary", type: "line", source: "boundary", paint: { "line-color": "#18302a", "line-width": 2, "line-dasharray": [3, 2] } });

      map.addSource("lots", { type: "geojson", data: { type: "FeatureCollection", features: [] }, promoteId: "parcel_id" });
      map.addLayer({ id: "lots", type: "fill", source: "lots", paint: {
        "fill-color": ["interpolate", ["linear"], ["get", "score"], 0, "#f3de8a", 0.5, "#e9962e", 1, "#8c2f55"],
        "fill-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 0.95, 0.78] } });
      map.addLayer({ id: "lots-line", type: "line", source: "lots", paint: {
        "line-color": ["case", ["boolean", ["feature-state", "selected"], false], "#0b57d0", "#18302a"],
        "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 3, 0.4] } });

      map.addLayer({ id: "lots-review", type: "line", source: "lots", filter: ["==", ["get", "needs_review"], true],
        paint: { "line-color": "#18302a", "line-width": 1.8, "line-dasharray": [2, 1.5] } });

      map.addSource("changed", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "changed", type: "line", source: "changed", layout: { visibility: "none" }, paint: { "line-color": "#6d28d9", "line-width": 2.5 } });

      map.addSource("stations", { type: "geojson", data: stations });
      map.addLayer({ id: "stations", type: "circle", source: "stations", paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 4, 16, 9], "circle-color": "#0072ce",
        "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 } });

      map.addSource("amenities", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "amenities", type: "circle", source: "amenities", filter: ["in", ["get", "group"], ["literal", []]],
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 3.5, 16, 7],
                 "circle-color": ["match", ["get", "group"], "shops", "#0d9488", "health", "#e11d48", "education", "#4f46e5",
                                  "parks", "#15803d", "emergency", "#111827", "#666"],
                 "circle-stroke-color": ["match", ["get", "group"], "emergency", "#dc2626", "#ffffff"],
                 "circle-stroke-width": ["match", ["get", "group"], "emergency", 2.5, 1.5] } });

      const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
      map.on("mousemove", "amenities", (e) => {
        const p = e.features[0].properties;
        pop.setLngLat(e.lngLat).setHTML(`<b>${esc(p.name)}</b><br>${esc(p.label)}${p.emergency_dept === true || p.emergency_dept === "true" ? " (emergency department)" : ""}`).addTo(map);
      });
      map.on("mouseleave", "amenities", () => pop.remove());
      map.on("mousemove", "stations", (e) => pop.setLngLat(e.lngLat).setHTML(`<b>${esc(e.features[0].properties.stop_name)}</b>`).addTo(map));
      map.on("mouseleave", "stations", () => pop.remove());
      map.on("mousemove", "lots", (e) => {
        map.getCanvas().style.cursor = "pointer";
        const p = e.features[0].properties;
        pop.setLngLat(e.lngLat).setHTML(`${p.address ? `<b>${esc(p.address)}</b><br>` : ""}${fmt(p.new_dwellings)} more homes in theory<br>${fmt(p.area_m2)} m², ${Math.round(p.coverage * 100)}% built on<br>${fmt(p.access_m)} m to ${esc(p.station)}`).addTo(map);
      });
      map.on("mouseleave", "lots", () => { map.getCanvas().style.cursor = ""; pop.remove(); });
      map.on("click", "lots", (e) => inspect(e.features[0].properties.parcel_id));
      if (lastLots) map.getSource("lots").setData(lastLots);
    };
    if (map.isStyleLoaded()) setup(); else map.once("style.load", setup);
  }

  let amenitiesLoaded = false;
  function initServiceLayers() {
    const groups = meta.amenities?.groups || {};
    const box = $("svc-layers");
    if (!meta.flags.services) { box.innerHTML = `<span class="muted">Services not loaded yet</span>`; return; }
    box.innerHTML = Object.entries(groups).map(([k, label]) =>
      `<label class="layer"><input type="checkbox" data-group="${k}"> <span class="dot ${k}" style="background:var(--g-${k})"></span> ${esc(label)}</label>`).join("");
    box.querySelectorAll("input").forEach((cb) => cb.addEventListener("change", async () => {
      if (!amenitiesLoaded) { map.getSource("amenities").setData(await api("/layers/amenities")); amenitiesLoaded = true; }
      const on = [...box.querySelectorAll("input:checked")].map((i) => i.dataset.group);
      map.setFilter("amenities", ["in", ["get", "group"], ["literal", on]]);
    }));
  }

  function initLayerControls() {
    initServiceLayers();
    document.querySelectorAll("input[name=base]").forEach((r) => r.addEventListener("change", () => {
      const aerial = document.querySelector("input[name=base]:checked").value === "aerial";
      map.setLayoutProperty("aerial", "visibility", aerial ? "visible" : "none");
      map.setLayoutProperty("street", "visibility", aerial ? "none" : "visible");
    }));
    const satBox = $("lyr-sat");
    if (!meta.flags.satellite_image) { satBox.disabled = true; $("lyr-sat-label").textContent = "Satellite image not built yet"; }
    else $("lyr-sat-label").textContent = `Satellite pass ${meta.satellite.current[0].date}`;
    satBox.addEventListener("change", () => map.getLayer("sentinel") && map.setLayoutProperty("sentinel", "visibility", satBox.checked ? "visible" : "none"));

    const ch = $("lyr-changed");
    if (!meta.flags.satellite) ch.disabled = true;
    ch.addEventListener("change", async () => {
      if (ch.checked && !ch.dataset.loaded) { map.getSource("changed").setData(await api("/layers/changed")); ch.dataset.loaded = "1"; }
      map.setLayoutProperty("changed", "visibility", ch.checked ? "visible" : "none");
    });
  }

  // ------------------------------------------------------------ inspector
  let selected = null;
  async function inspect(pid) {
    try {
      const d = await api("/parcels/detail", { parcel_id: pid, scenario: lastScenario || readScenario() });
      const p = d.parcel;
      if (selected !== null) map.setFeatureState({ source: "lots", id: selected }, { selected: false });
      selected = pid;
      map.setFeatureState({ source: "lots", id: pid }, { selected: true });
      $("ins-title").textContent = p.address || p.lot_plan || `Lot ${pid}`;
      const extra = p.address_count > 1 ? ` · ${p.address_count - 1} more address${p.address_count > 2 ? "es" : ""} on this lot` : "";
      $("ins-sub").textContent = [p.address ? p.lot_plan : "", p.spi ? `SPI ${p.spi}` : "", `Vicmap ID ${pid}`]
        .filter(Boolean).join(" · ") + extra;
      const v = $("ins-verdict");
      v.textContent = p.eligible ? `Qualifies: room for ${fmt(p.new_dwellings)} more homes in theory` : `Left out: ${p.excluded_reason}`;
      v.className = "verdict" + (p.eligible ? "" : " no");
      const rn = $("ins-review");
      rn.hidden = !p.needs_review;
      rn.textContent = p.needs_review ? `Qualifies on the numbers, but ${p.overlays_review} affects what can be built. A planner should check the controls.` : "";
      const rows = [
        ["Zone", p.zone], ["Lot area", `${fmt(p.area_m2)} m²`], ["Built on", `${Math.round((p.coverage || 0) * 100)}%`],
        ["Nearest station", p.station], [`Distance (${p.access_method})`, `${fmt(p.access_m)} m`],
        ["Straight-line distance", `${fmt(p.dist_station_m)} m`],
        ["Lot width (approx.)", p.lot_width_m == null ? "–" : `${fmt(p.lot_width_m, 1)} m`],
        ["Excluding overlays", p.overlays_exclude || "none"],
        ["Overlays needing review", p.overlays_review || "none"],
      ];
      if (p.walk_status && p.walk_status !== "ok") rows.splice(5, 0, ["Walking route", p.walk_status]);
      if (meta.flags.census) rows.push(["Median rent nearby", p.median_rent_wk ? `$${fmt(p.median_rent_wk)}/week` : "–"],
                                        ["Rent as share of income", p.rent_to_income ? `${Math.round(p.rent_to_income * 100)}%` : "–"]);
      if (meta.flags.satellite) rows.push(["Green cover (satellite)", p.green_frac == null ? "–" : `${Math.round(p.green_frac * 100)}%`],
                                          ["Greenness change, 1 year", p.d_ndvi == null ? "–" : (p.d_ndvi > 0 ? "+" : "") + p.d_ndvi.toFixed(2)],
                                          ["Satellite reading", p.sat_low_confidence ? "low confidence (small lot)" : "ok"]);
      $("ins-evidence").innerHTML = rows.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val)}</dd>`).join("");
      const svc = p.services || [];
      const row = (x) => {
        const mark = x.within_reach === true ? `<span class="ok" aria-label="within reach">✓</span>` : x.within_reach === false ? `<span class="no" aria-label="not within reach">✗</span>` : "<span></span>";
        const dist = x.distance_m == null ? "none found" : x.distance_m >= 1000 ? `${fmt(x.distance_m / 1000, 1)} km` : `${fmt(x.distance_m)} m`;
        const mins = x.kind !== "emergency" && x.distance_m != null && x.method === "walking network" ? `, ${Math.round(x.distance_m / 80)} min` : "";
        return `<div class="row">${mark}<span>${esc(x.name || "–")}<span class="what">${esc(x.label)}</span></span><span class="d">${dist}${mins}</span></div>`;
      };
      const daily = svc.filter((x) => x.kind !== "emergency"), emerg = svc.filter((x) => x.kind === "emergency");
      $("ins-svc").innerHTML = svc.length ? `<h3>Everyday needs (${esc(daily[0]?.method || "walk")})</h3>${daily.map(row).join("")}
        <h3>Emergency services (straight line)</h3>${emerg.map(row).join("")}` : "";

      const parts = [["Close to a station", p.f_proximity], ["Large lot", p.f_lot_size], ["Unbuilt land", p.f_open_land], ["Rental pressure", p.f_demand], ["Everyday needs", p.f_services]];
      $("ins-parts").innerHTML = `<p class="muted" style="margin:0">Ranking parts, 0 to 1. Overall: <b>${(p.score ?? 0).toFixed(2)}</b></p>` +
        parts.map(([k, x]) => `<div class="p"><span>${k}</span><span class="track"><span class="fill" style="width:${(x || 0) * 100}%"></span></span><span>${(x || 0).toFixed(2)}</span></div>`).join("");
      $("inspector").hidden = false;
      $("ins-close").focus();
    } catch (e) { status(`Couldn't load lot ${pid}: ${e.message}`, true); }
  }
  function closeInspector() {
    $("inspector").hidden = true;
    if (selected !== null) map.setFeatureState({ source: "lots", id: selected }, { selected: false });
    selected = null;
  }

  // ------------------------------------------------------------ exports
  async function download(fmtName) {
    try {
      status("Preparing download…");
      const res = await api(`/export/${fmtName}`, { scenario: readScenario() }, { raw: true });
      const blob = await res.blob();
      const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || `export.${fmtName}`;
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
      status("");
    } catch (e) { status(`Download failed: ${e.message}`, true); }
  }

  // ------------------------------------------------------------ address search
  function initSearch() {
    const input = $("q"), list = $("q-results");
    let timer = null, items = [], active = -1, ctrl = null;
    const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
    const choose = (r) => {
      close();
      input.value = r.address || r.lot_plan || r.parcel_id;
      map.flyTo({ center: [r.lon, r.lat], zoom: 18 });
      inspect(r.parcel_id);
    };
    const render = () => {
      list.innerHTML = items.length ? items.map((r, i) =>
        `<li role="option" id="q-${i}" aria-selected="${i === active}">${esc(r.address || r.lot_plan || r.parcel_id)}
           <small>${esc([r.address ? r.lot_plan : "", `ID ${r.parcel_id}`].filter(Boolean).join(" · "))}</small></li>`).join("")
        : `<li class="empty">No lots match. Try a street name, or a plan number like LP12345.</li>`;
      list.hidden = false; input.setAttribute("aria-expanded", "true");
      input.setAttribute("aria-activedescendant", active >= 0 ? `q-${active}` : "");
      list.querySelectorAll("li[role=option]").forEach((li, i) => li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(items[i]); }));
    };
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 3) { close(); return; }
      timer = setTimeout(async () => {
        if (ctrl) ctrl.abort();
        ctrl = new AbortController();
        try { items = (await api(`/parcels/search?q=${encodeURIComponent(q)}&limit=8`, null, { signal: ctrl.signal })).results; active = -1; render(); }
        catch (e) { if (e.name !== "AbortError") status(`Search failed: ${e.message}`, true); }
      }, 250);
    });
    input.addEventListener("keydown", (e) => {
      if (list.hidden || !items.length) return;
      if (e.key === "ArrowDown") { active = (active + 1) % items.length; render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = (active - 1 + items.length) % items.length; render(); e.preventDefault(); }
      else if (e.key === "Enter") { choose(items[Math.max(active, 0)]); e.preventDefault(); }
      else if (e.key === "Escape") close();
    });
    input.addEventListener("blur", () => setTimeout(close, 150));
  }

  // ------------------------------------------------------------ data status
  async function loadDataStatus() {
    try {
      const st = await api("/data-status");
      const src = Object.entries(st.sources || {});
      const labels = { ok: "All sources current", warning: "Some data needs attention", failed: "Some data is missing" };
      $("ds-dot").className = "ds-dot " + st.overall;
      $("ds-summary").textContent = `Data sources: ${labels[st.overall] || "unknown"}`;
      const day = (t) => t ? new Date(t).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" }) : "never";
      $("ds-list").innerHTML = src.map(([, v]) => `<div class="ds-item"><span class="ds-dot ${esc(v.state)}"></span> <b>${esc(v.label)}</b>
          <div class="meta">Retrieved ${day(v.retrieved_at)}${v.rows ? `, ${fmt(v.rows)} records` : ""}${v.updates ? `. Source ${esc(v.updates)}.` : ""}</div>
          ${v.note ? `<div class="note">${esc(v.note)}</div>` : ""}</div>`).join("") +
        (st.processing?.processed_at ? `<div class="ds-item"><span class="meta">Lot table built ${day(st.processing.processed_at)}</span></div>` : "");
      if (st.overall !== "ok") $("ds").open = true;
    } catch (_) { $("ds-summary").textContent = "Data sources: status unavailable"; }
  }

  // ------------------------------------------------------------ boot
  async function boot() {
    try { meta = await api("/meta"); }
    catch (e) {
      $("area").textContent = "No data loaded";
      status(`The server has no processed data yet. Run the pipeline, then reload this page. (${e.message})`, true);
      return;
    }
    $("area").textContent = `${meta.study_area}, ${fmt(meta.parcels)} lots analysed`;
    const d = meta.defaults;
    SLIDERS.forEach((k) => ($(k).value = d[k]));
    CHECKS.forEach((k) => ($(k).checked = d[k]));
    if (!meta.flags.walking) { $("use_walking").checked = false; $("use_walking").disabled = true; }
    if (!meta.flags.satellite) { $("exclude_recent_change").checked = false; $("exclude_recent_change").disabled = true; }
    if (!meta.flags.census) $("w_demand").value = 0;

    const sat = meta.satellite;
    $("sources").innerHTML = `Data: Vicmap Property and Planning (DTP), Transport Victoria GTFS, OpenStreetMap, ABS Census 2021` +
      (sat ? `, Sentinel-2 satellite passes ${sat.current.map((c) => c.date).join(", ")} (${esc(sat.attribution)})` : "") +
      `.<br>Shows opportunities to investigate. It does not decide whether land can or should be rezoned.`;

    document.querySelectorAll(".controls input").forEach((i) => i.addEventListener("input", onChange));
    document.querySelectorAll("[data-export]").forEach((b) => b.addEventListener("click", () => download(b.dataset.export)));
    $("ins-close").addEventListener("click", closeInspector);
    document.addEventListener("keydown", (e) => e.key === "Escape" && !$("inspector").hidden && closeInspector());
    initTabs();
    initSearch();
    writeOutputs(readScenario());
    runScenario(readScenario());   // results don't wait for map tiles
    loadDataStatus();
    initMap();
    initLayerControls();
  }

  boot();
})();
