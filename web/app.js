/* Metroviolencia · frontend (valencià) — trens animats sobre la via */
const API = "";
const REFRESH_MS = 90000;   // refresc de seguretat; el normal el dispara el servidor (vigilaServidor)
const VIGILA_MS = 2000;     // cada quant es pregunta a /api/ultimo si hi ha pasada nova
const ANIM_MS = 500;        // pas d'animació dels punts

const NOMBRES_LINEA = {
  1: "L1", 2: "L2", 3: "L3", 4: "L4", 5: "L5",
  6: "L6", 7: "L7", 8: "L8", 9: "L9", 10: "L10",
};

const map = L.map("map", { zoomControl: false })
  .setView([39.47, -0.38], 12);
map.attributionControl.setPrefix(false);

L.control.zoom({ position: "bottomleft" }).addTo(map);

// Mapa base OBERT, sense API key: OpenFreeMap (codi obert, dades d'OpenStreetMap),
// en vectorial amb MapLibre. Si el dispositiu no té WebGL, teseles raster d'OSM
// enfosquides per CSS.
function teWebGL() {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl") || c.getContext("experimental-webgl"));
  } catch (e) { return false; }
}
if (window.maplibregl && L.maplibreGL && teWebGL()) {
  L.maplibreGL({
    style: "https://tiles.openfreemap.org/styles/dark",
    attribution: '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> · ' +
      '© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>',
  }).addTo(map);
} else {
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, className: "osm-fosc",
    attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>',
  }).addTo(map);
}

const lineasVisibles = new Set();
const trenLayer = L.layerGroup().addTo(map);
const marcadores = new Map();      // clau -> {marker, tren}
const marcadorsEstacio = new Map(); // id estació -> circleMarker (bombolles del xat)
let colores = {};                  // color per línia comercial
let comerciales = {};              // {lid: {path, cum, length}}
let ultimoTrenes = [];
let ultimoTs = 0;
let ultimoFetchMs = 0;   // Date.now() de l'últim /api/trains rebut (comptador 'client' del rellotge)
let ultimoTsFin = 0;     // fi de l'última pasada del poller (comptador 'FGV' del rellotge)
let refrescant = false;
let noinfo = [];                   // línies metro sense info ara mateix (tall de font)
let sinTiempoReal = [];            // trams sense temps real (sempre)
let estacionesRetorno = new Set(); // capçaleres/retorns on els trens descansen
let lineLayers = {};               // lid -> [capes de via/connectors] per al toggle
let porNomGlobal = {};             // nom estació -> objecte estació
let arrivalsCache = { ts: 0, estaciones: {} };
let rutaFiltre = null;             // Set d'ids de tren de la ruta activa (null = tots)

// Compensació de ping/rellotge: el servidor (Raspa) marca el temps dels trens amb
// EL SEU rellotge (data.ts / srv_ms), però el client interpola amb el SEU. Si els
// rellotges no coincideixen o hi ha latència de xarxa, tots els trens es desplacen.
// Mesurem el round-trip (ping) i el desfàs de rellotge en cada refresc i corregim:
// serverNow() = hora del servidor estimada al client (Date.now() + offset).
let clockOffsetMs = 0;   // ms a sumar a Date.now() per obtenir l'hora de la Raspa
let pingMs = 0;          // últim round-trip client↔Raspa (ms), suavitzat
const serverNow = () => Date.now() + clockOffsetMs;

// Detecció de "tren congelat": la font de tramvia (L4/6/8/10) dóna ETAs
// d'horari (estàtiques). El mateix tren torna pull rere pull amb el MATEIX d0,
// però el client l'extrapola cap avant pels ETAs i, al refrescar, torna a d0:
// és el "rollback" (avança i recula). Si el d0 del servidor no canvia entre
// pulls, el marquem congelat i deixem d'extrapolar-lo (es queda quiet a d0).
const histTren = new Map();   // track_id -> {d0, iguals, congelat}
const FREEZE_TOL = 25;        // m: variació de d0 per baix de la qual és "el mateix"
const trenCongelat = (t) => {
  if (t.id == null) return false;
  const h = histTren.get(t.id);
  return !!(h && h.congelat);
};

const hex = (c) => (c && c.startsWith("#") ? c : "#" + (c || "888"));
// geometria d'un tren: la seua via (variant de ramal, p. ex. 42 = L4 per Ll. Llarga) o la línia
const viaDe = (t) => t.via || t.linea;

/* ---------- distància sobre la via -> (lat,lng) ---------- */
function distToLatLng(lid, d) {
  const lc = comerciales[lid];
  if (!lc || !lc.cum || lc.cum.length < 2) return null;
  const cum = lc.cum, path = lc.path;
  const L = cum[cum.length - 1];
  d = Math.max(cum[0], Math.min(L, d));   // una variant de ramal no comença en 0
  let lo = 0, hi = cum.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (cum[m] < d) lo = m + 1; else hi = m; }
  const i = Math.max(1, lo);
  const seg = cum[i] - cum[i - 1];
  const t = seg === 0 ? 0 : (d - cum[i - 1]) / seg;
  return [path[i - 1][0] + t * (path[i][0] - path[i - 1][0]),
          path[i - 1][1] + t * (path[i][1] - path[i - 1][1])];
}

/* ---------- format de temps en valencià ---------- */
function fmtEta(seg) {
  if (seg <= 0) return "arribant";
  const m = Math.floor(seg / 60), s = seg % 60;
  if (m === 0) return `${s} s`;
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
const etaViu = (t) => Math.max(0, t.eta_seg - Math.max(0, Math.floor(serverNow() / 1000) - ultimoTs));

/* Separa lateralment les línies de metro que comparteixen corredor (p. ex.
   L2+L7 Torrent–Torrent Av, o el nucli L5/L7/L9): desplaça cada línia perpendicular
   a la seua direcció segons el seu ordre entre les que comparteixen eixe punt.
   Modifica comerciales[lid].path -> el dibuix I els trens (que usen eixa geometria)
   queden costat a costat. On una línia va sola, no es mou. */
function offsetCorredores(net) {
  const tram = net.tram_tracks || {};
  const metro = Object.keys(comerciales).map(Number)
    .filter((l) => !(String(l) in tram) && !comerciales[String(l)].variante_de).sort((a, b) => a - b);
  if (metro.length < 2) return;
  const SPACING = 7, CELL = 16;
  const grid = new Map();
  const cellKey = (la, ln) => Math.round(la * 111000 / CELL) + "|" + Math.round(ln * 85000 / CELL);
  for (const lid of metro)
    for (const [la, ln] of comerciales[String(lid)].path) {
      const k = cellKey(la, ln);
      if (!grid.has(k)) grid.set(k, new Set());
      grid.get(k).add(lid);
    }
  const near = (la, ln) => {
    const s = new Set();
    const cx = Math.round(la * 111000 / CELL), cy = Math.round(ln * 85000 / CELL);
    for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
      const g = grid.get((cx + i) + "|" + (cy + j));
      if (g) g.forEach((l) => s.add(l));
    }
    return [...s].sort((a, b) => a - b);
  };
  for (const lid of metro) {
    const lc = comerciales[String(lid)], path = lc.path, n = path.length;
    const off = new Array(n).fill(0);
    for (let i = 0; i < n; i++) {
      const grp = near(path[i][0], path[i][1]);
      if (grp.length > 1) off[i] = (grp.indexOf(lid) - (grp.length - 1) / 2) * SPACING;
    }
    const sm = off.map((_, i) => {
      let s = 0, c = 0;
      for (let j = Math.max(0, i - 2); j <= Math.min(n - 1, i + 2); j++) { s += off[j]; c++; }
      return s / c;
    });
    lc.path = path.map((p, i) => {
      const a = path[Math.max(0, i - 1)], b = path[Math.min(n - 1, i + 1)];
      let dx = (b[1] - a[1]) * 85000, dy = (b[0] - a[0]) * 111000;
      const L = Math.hypot(dx, dy) || 1;
      const px = -dy / L, py = dx / L;   // perpendicular unitari
      return [p[0] + (py * sm[i]) / 111000, p[1] + (px * sm[i]) / 85000];
    });
  }
}

/* ---------- xarxa ---------- */
async function cargarRed() {
  const net = await (await fetch(API + "api/network")).json();
  colores = net.colores_comercial || net.colores || {};
  comerciales = net.comerciales || {};
  sinTiempoReal = net.sin_tiempo_real || [];
  estacionesRetorno = new Set(net.estaciones_retorno || []);
  offsetCorredores(net);   // separa lateralmente les línies de metro que comparteixen via

  const porNom = {};
  for (const e of net.estaciones) porNom[e.nombre] = e;
  porNomGlobal = porNom;
  lineLayers = {};
  const addLayer = (lid, layer) => {
    (lineLayers[lid] = lineLayers[lid] || []).push(layer);
    layer.addTo(map);
  };

  // trams tallats per línia (d0,d1 sobre la seua via) -> per retallar L5/L7
  const closedRanges = {};
  for (const c of net.cierres || []) {
    for (const lid of c.lineas || []) {
      const lc = comerciales[String(lid)];
      if (!lc) continue;
      const idA = porNom[c.tramo[0]]?.id, idB = porNom[c.tramo[c.tramo.length - 1]]?.id;
      const eA = lc.estaciones.find((e) => e.id === idA);
      const eB = lc.estaciones.find((e) => e.id === idB);
      if (!eA || !eB) continue;
      (closedRanges[lid] = closedRanges[lid] || [])
        .push([Math.min(eA.d, eB.d), Math.max(eA.d, eB.d)]);
    }
  }

  // METRO: dibuixa la via comercial, RETALLANT els trams tallats (no es veu L5/L7
  // entre Alameda i Marítim; allà només el punteado gris animat)
  for (const [lid, lc] of Object.entries(comerciales)) {
    if (String(lid) in (net.tram_tracks || {}) || lc.variante_de) continue;   // trams a part; variants no es dibuixen
    const col = hex("#" + lc.color);
    for (const piece of clipPath(lid, closedRanges[lid] || [])) {
      addLayer(Number(lid), L.polyline(piece, { color: col, weight: 3.5,
        opacity: 0.85, lineJoin: "round" }));
    }
  }
  // TRAMVIES des d'OSM, en color comercial
  for (const [ref, ways] of Object.entries(net.tram_tracks || {})) {
    const col = hex("#" + (colores[ref] || "888"));
    for (const w of ways) addLayer(Number(ref),
      L.polyline(w, { color: col, weight: 3.5, opacity: 0.9, lineJoin: "round" }));
  }
  // trams que només fa el sentit contrari (carrer Doctor Lluch a la platja…)
  for (const [ref, ways] of Object.entries(net.tram_tracks_extra || {})) {
    const col = hex("#" + (colores[ref] || "888"));
    for (const w of ways) addLayer(Number(ref),
      L.polyline(w, { color: col, weight: 3.5, opacity: 0.9, lineJoin: "round" }));
  }
  // connectors de metro
  for (const c of net.conectores || []) {
    addLayer(c.linea, L.polyline(c.path, { color: hex("#" + c.color), weight: 3.5,
      opacity: 0.85, lineJoin: "round" }).bindTooltip(`${c.a} – ${c.b}`, { sticky: true }));
  }
  // trams SUBTERRANIS: casing blanc a ratlles damunt del color (efecte túnel)
  for (const sub of net.subterraneos || []) {
    const ways = (net.tram_tracks || {})[String(sub.linea)];
    if (!ways) continue;
    const merged = mergeWays(ways);
    const a = porNom[sub.tramo[0]], b = porNom[sub.tramo[sub.tramo.length - 1]];
    if (!merged.length || !a || !b) continue;
    const slice = sliceLatLng(merged, [a.lat, a.lng], [b.lat, b.lng]);
    if (slice.length >= 2) {
      addLayer(sub.linea, L.polyline(slice, { color: "#e8e8ef", weight: 2,
        opacity: 0.95, dashArray: "2 6", lineCap: "round" })
        .bindTooltip(sub.nota || "tram subterrani", { sticky: true }));
    }
  }

  for (const e of net.estaciones) {
    const ret = estacionesRetorno.has(e.nombre);   // capçalera/retorn
    const m = L.circleMarker([e.lat, e.lng], {
      radius: ret ? 5 : 3.2,
      color: ret ? "#cfcfe0" : "#fff",
      weight: ret ? 2.5 : 1,
      fillColor: ret ? "#2a2a36" : "#111",
      fillOpacity: 1,
    }).addTo(map);
    m.bindTooltip(ret ? `${e.nombre} · capçalera/retorn` : e.nombre,
      { direction: "top", opacity: 0.9 });
    m.estId = e.id; m.estNom = e.nombre; m.estLineas = e.lineas;
    m.on("click", () => obrirPopupEstacio(m));
    marcadorsEstacio.set(e.id, m);
  }

  dibuixarCierres(net.cierres || [], porNom);

  const lineas = Object.keys(colores).map(Number)
    .filter((n) => n >= 1 && n <= 10).sort((a, b) => a - b);
  construirLeyenda(lineas);
}

// uneix les ways d'un tramvia (OSM) en una polilínia contínua encadenant extrems
function mergeWays(ways) {
  if (!ways || !ways.length) return [];
  const segs = ways.map((w) => w.slice());
  const used = new Array(segs.length).fill(false);
  const near = (a, b) => Math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 85000) < 35;
  let path = segs[0].slice(); used[0] = true;
  let changed = true;
  while (changed) {
    changed = false;
    for (let i = 0; i < segs.length; i++) {
      if (used[i]) continue;
      const s = segs[i], head = path[0], tail = path[path.length - 1];
      if (near(tail, s[0])) { path = path.concat(s.slice(1)); used[i] = true; changed = true; }
      else if (near(tail, s[s.length - 1])) { path = path.concat(s.slice().reverse().slice(1)); used[i] = true; changed = true; }
      else if (near(head, s[s.length - 1])) { path = s.slice().concat(path.slice(1)); used[i] = true; changed = true; }
      else if (near(head, s[0])) { path = s.slice().reverse().concat(path.slice(1)); used[i] = true; changed = true; }
    }
  }
  return path;
}
// tros d'una polilínia entre els punts més pròxims a a i b
function sliceLatLng(path, a, b) {
  const d = (p, q) => Math.hypot((p[0] - q[0]) * 111000, (p[1] - q[1]) * 85000);
  let ia = 0, ib = 0, da = Infinity, db = Infinity;
  path.forEach((p, i) => {
    const x = d(p, a); if (x < da) { da = x; ia = i; }
    const y = d(p, b); if (y < db) { db = y; ib = i; }
  });
  return path.slice(Math.min(ia, ib), Math.max(ia, ib) + 1);
}

// retalla la via d'una línia excloent els rangs [d0,d1] tancats -> trossos
function clipPath(lid, ranges) {
  const lc = comerciales[String(lid)];
  if (!lc || !lc.cum) return [];
  if (!ranges.length) return [lc.path];
  const cuts = [...ranges].sort((a, b) => a[0] - b[0]);
  const total = lc.cum[lc.cum.length - 1];
  const pieces = [];
  let cur = 0;
  for (const [a, b] of cuts) {
    if (a > cur) pieces.push(sliceByD(lid, cur, a));
    cur = Math.max(cur, b);
  }
  if (cur < total) pieces.push(sliceByD(lid, cur, total));
  return pieces.filter((p) => p && p.length >= 2);
}
function sliceByD(lid, d0, d1) {
  const lc = comerciales[String(lid)];
  const out = [distToLatLng(lid, d0)];
  for (let i = 0; i < lc.cum.length; i++)
    if (lc.cum[i] > d0 && lc.cum[i] < d1) out.push(lc.path[i]);
  out.push(distToLatLng(lid, d1));
  return out.filter(Boolean);
}

// sub-traçat d'una línia comercial entre dues estacions (segueix la via real)
function subTracat(lid, nomA, nomB, porNom) {
  const lc = comerciales[String(lid)];
  if (!lc || !lc.cum) return null;
  const idA = porNom[nomA]?.id, idB = porNom[nomB]?.id;
  const eA = lc.estaciones.find((e) => e.id === idA);
  const eB = lc.estaciones.find((e) => e.id === idB);
  if (!eA || !eB) return null;
  const d0 = Math.min(eA.d, eB.d), d1 = Math.max(eA.d, eB.d);
  const out = [distToLatLng(lid, d0)];
  for (let i = 0; i < lc.cum.length; i++)
    if (lc.cum[i] > d0 && lc.cum[i] < d1) out.push(lc.path[i]);
  out.push(distToLatLng(lid, d1));
  return out;
}

function dibuixarCierres(cierres, porNom) {
  const av = document.getElementById("avisos");
  for (const c of cierres) {
    // dibuixa el corredor tallat seguint la via real (una línia de la llista);
    // com L5 i L7 comparteixen corredor, cobreix les dues.
    let pts = null;
    for (const lid of c.lineas || []) {
      pts = subTracat(lid, c.tramo[0], c.tramo[c.tramo.length - 1], porNom);
      if (pts) break;
    }
    if (!pts) pts = (c.tramo || []).map((n) => porNom[n]).filter(Boolean).map((e) => [e.lat, e.lng]);
    if (pts && pts.length >= 2) {
      // gris fosc i subtil: prou per veure's però sense cridar l'atenció
      L.polyline(pts, { color: "#33333c", weight: 5, opacity: 0.9,
        dashArray: "1 11", lineCap: "round", className: "cierre-anim" }).addTo(map)
        .bindTooltip(c.nota || "Tram tallat", { sticky: true });
      for (const n of c.tramo || []) {
        const e = porNom[n];
        if (e) L.circleMarker([e.lat, e.lng], { radius: 4, color: "#3a3a44",
          weight: 2, fillColor: "#15151b", fillOpacity: 1 }).addTo(map)
          .bindTooltip(`${n} · tancada`, { direction: "top" });
      }
    }
    const div = document.createElement("div");
    div.className = "aviso";
    div.innerHTML = `⚠️ ${c.nota || "Tram tallat"}`;
    av.appendChild(div);
  }
}

let chipsPorLinea = {};
function construirLeyenda(lineas) {
  const ley = document.getElementById("leyenda");
  ley.innerHTML = ""; chipsPorLinea = {};
  for (const l of lineas) {
    lineasVisibles.add(l);
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = NOMBRES_LINEA[l] || "L" + l;
    chip.style.background = "#" + (colores[l] || "888");
    chip.title = "";
    chip.onclick = () => {
      const capes = lineLayers[l] || [];
      if (lineasVisibles.has(l)) {
        lineasVisibles.delete(l); chip.classList.add("off");
        capes.forEach((x) => map.removeLayer(x));          // amaga la via també
      } else {
        lineasVisibles.add(l); chip.classList.remove("off");
        capes.forEach((x) => x.addTo(map));
      }
      pintarTrenes(ultimoTrenes);
    };
    chipsPorLinea[l] = chip;
    ley.appendChild(chip);
  }
}

/* ---------- trens: posició sobre la trajectòria (waypoints) ---------- */
// distància sobre la via en el moment actual, interpolant els waypoints per ETA
function distVivaTren(t) {
  const wp = t.waypoints;
  if (!wp || !wp.length) return null;
  if (trenCongelat(t)) return wp[0].d;   // dades estàtiques: no extrapolar (evita el rollback)
  const tau = Math.max(0, Math.floor(serverNow() / 1000) - ultimoTs);
  if (tau <= wp[0].eta) return wp[0].d;
  for (let i = 0; i < wp.length - 1; i++) {
    if (tau <= wp[i + 1].eta) {
      const dt = wp[i + 1].eta - wp[i].eta;
      const f = dt <= 0 ? 1 : (tau - wp[i].eta) / dt;
      return wp[i].d + f * (wp[i + 1].d - wp[i].d);
    }
  }
  return wp[wp.length - 1].d;   // ha arribat al final (terminal)
}
function posicioViva(t) {
  const d = distVivaTren(t);
  return (d == null ? null : distToLatLng(viaDe(t), d)) || [t.lat, t.lng];
}
// pròxima parada viva (primer waypoint amb nom i eta > temps transcorregut)
function proxParada(t) {
  const tau = Math.max(0, Math.floor(serverNow() / 1000) - ultimoTs);
  for (const w of t.waypoints) if (w.nom && w.eta > tau) return { nom: w.nom, eta: w.eta - tau };
  const last = [...t.waypoints].reverse().find((w) => w.nom);
  return last ? { nom: last.nom, eta: 0 } : { nom: t.prox_estacion, eta: 0 };
}

// Emparellament estable: cada tren nou es lliga al marcador més pròxim de la
// mateixa línia+destí (evita salts; el punt continua endavant entre pulls).
function pintarTrenes(trenes) {
  const previos = [...marcadores.values()];
  for (const e of previos) e.usado = false;
  const nuevos = [];
  for (const t of trenes) {
    if (!lineasVisibles.has(t.linea)) continue;
    if (rutaFiltre && !rutaFiltre.has(t.id)) continue;   // només els trens de la ruta
    const ll = posicioViva(t);
    let best = null, bestD = Infinity;
    // 1r: emparella pel track_id persistent (mateix tren físic entre pulls)
    if (t.id != null) {
      for (const e of previos) {
        if (!e.usado && e.tren.id === t.id) { best = e; bestD = 0; break; }
      }
    }
    // 2n (sense id o no trobat): heurística per proximitat, mateixa línia+destí
    if (!best) {
      for (const e of previos) {
        if (e.usado || e.tren.linea !== t.linea || e.tren.destino !== t.destino) continue;
        const p = e.marker.getLatLng();
        const dd = Math.hypot((p.lat - ll[0]) * 111000, (p.lng - ll[1]) * 85000);
        if (dd < bestD) { bestD = dd; best = e; }
      }
    }
    if (best && bestD < 1500) {   // mateix tren físic: reaprofita el marcador
      best.usado = true; best.tren = t;
      const k = classeRetard(t);
      if (best.kRet !== k) { best.marker.setIcon(iconoTren(t)); best.kRet = k; }
      // NO reposicionem ací: animar() suavitza la posició (e.dViz) cada frame,
      // així no es produeix cap salt al refrescar (ni cap avant ni cap arrere).
      best.marker.setPopupContent(popupTren(t));
      if (best.marker.isPopupOpen() && window.xatOmpliPopup) window.xatOmpliPopup(best.marker.getPopup().getElement());
    } else {
      const marker = L.marker(ll, { icon: iconoTren(t) }).bindPopup(popupTren(t));
      marker.on("popupopen", (ev) => { if (window.xatOmpliPopup) window.xatOmpliPopup(ev.popup.getElement()); });
      marker.addTo(trenLayer);
      nuevos.push({ marker, tren: t, usado: true, kRet: classeRetard(t) });
    }
  }
  // esborrar marcadors no reaprofitats i reconstruir el mapa
  for (const e of previos) if (!e.usado) trenLayer.removeLayer(e.marker);
  marcadores.clear();
  let n = 0;
  for (const e of previos) if (e.usado) marcadores.set("m" + n++, e);
  for (const e of nuevos) marcadores.set("m" + n++, e);
}

// vora de la fletxa segons el retard: el farcit continua sent el color de la
// línia; la vora només canvia si el tren NO va a l'hora (mapa tranquil)
// (no hi ha "avançat": un tren no circula abans del seu horari; si ho pareix, és
// l'anterior que va tard o que s'ha cancel·lat — el servidor ja ho casa així)
const VORA_RETARD = { "": "#fff", poc: "#f59e0b", tard: "#ef4444" };
function classeRetard(t) {
  const d = t.retraso ? t.retraso.retraso_s : null;
  if (d == null) return "";
  if (d >= 180) return "tard";
  if (d >= 90) return "poc";
  return "";
}

function iconoTren(t) {
  const c = "#" + (t.color || "888");
  const k = classeRetard(t);
  // punt (parat en capçalera/retorn) + fletxa (en marxa, girada al rumb).
  // Tots dos amb vora blanca per a ressaltar sobre la via.
  return L.divIcon({
    className: "",
    html:
      `<div class="tren-wrap">` +
      `<span class="tren-dot" style="background:${c}"></span>` +
      `<svg class="tren-arrow" viewBox="0 0 24 24" width="22" height="22">` +
      `<path d="M21 12 L6 4 L11 12 L6 20 Z" fill="${c}" stroke="${VORA_RETARD[k]}" ` +
      `stroke-width="${k ? 2.6 : 1.7}" stroke-linejoin="round"/></svg>` +
      `</div>`,
    iconSize: [22, 22], iconAnchor: [11, 11],
  });
}

// direcció de marxa sobre la via (signe de d final - d inicial)
function dirTren(t) {
  const wp = t.waypoints;
  return wp[wp.length - 1].d >= wp[0].d ? 1 : -1;
}
// velocitat instantània (m/s) segons el tram de trajectòria actiu; 0 = parat
function velTren(t) {
  const wp = t.waypoints;
  const tau = Math.max(0, Math.floor(serverNow() / 1000) - ultimoTs);
  let i = 0;
  while (i < wp.length - 1 && wp[i + 1].eta <= tau) i++;
  if (i >= wp.length - 1) return 0;            // ha arribat al final
  const dt = wp[i + 1].eta - wp[i].eta;
  return dt > 0 ? Math.abs(wp[i + 1].d - wp[i].d) / dt : 0;
}
// angle en pantalla (graus) del rumb del tren a la posició d
function rumbPantalla(t, d) {
  const a = distToLatLng(viaDe(t), d);
  const b = distToLatLng(viaDe(t), d + dirTren(t) * 40);
  if (!a || !b) return 0;
  const pa = map.latLngToContainerPoint(a), pb = map.latLngToContainerPoint(b);
  return Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180 / Math.PI;
}

function popupTren(t) {
  const c = "#" + (t.color || "888");
  const p = proxParada(t);
  const espera = t.origen ? ' <span style="color:#e8c07a">(en eixida)</span>' : "";
  const idTxt = t.id != null ? ` <span style="color:#6a6a7a">#${t.id}</span>` : "";
  return `<span class="pop-linea" style="background:${c}">${NOMBRES_LINEA[t.linea] || "L" + t.linea}</span>
    → <b>${t.destino}</b>${idTxt}${espera}<br>
    <span style="color:#9a9aac">pròxima parada: ${p.nom} · <b class="c-eta">${fmtEta(p.eta)}</b></span>` +
    retardTren(t) +
    (t.id != null ? `<div class="pop-xat" data-tipus="tren" data-lloc="${t.id}" data-nom="${NOMBRES_LINEA[t.linea] || "L" + t.linea} → ${t.destino}"></div>` : "");
}

// retard ara mateix respecte a l'horari de pas (calculat al servidor)
function retardTren(t) {
  const r = t.retraso;
  if (!r) return "";
  const d = r.retraso_s, min = Math.round(Math.abs(d) / 60);
  const [col, txt] = d < 90 ? ["#4ade80", "va a l'hora"]
    : [d >= 180 ? "#f87171" : "#fbbf24", `va <b>+${min} min</b> tard`];
  return `<br><span style="color:${col}">${txt}</span>
    <span style="color:#9a9aac">· programat a ${r.estacion} a les ${r.hora_prog}</span>`;
}

/* ---------- posar en fila els trens que comparteixen via ---------- */
// Dues línies comercials poden compartir la MATEIXA via física entre estacions
// (p. ex. L3/L5 al ramal de l'Aeroport, L2/L7 cap a Torrent, el nucli L3/L5/L7/L9).
// El dibuix les separa lateralment per veure els dos colors (offsetCorredores),
// però dos trens no poden ocupar el mateix punt d'una via: si estan molt pròxims
// i van en la MATEIXA direcció, reculem el de darrere pel seu propi raïl fins
// deixar un headway mínim -> queden EN FILA, mai costat a costat.
const COLA_MIN_SEP = 110;    // m mínims entre dos trens a la mateixa via compartida
const COLA_MAX_SHIFT = 260;  // m màxims que reculem un tren (no passar-se de tram)
const COLA_STEP = 18;        // m per frame cap al desplaçament objectiu (suavitzat)

function metresEntre(a, b) {
  return Math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 85000);
}
// vector unitari (en metres) del rumb del tren a la distància d sobre la via
function headingVec(t, d) {
  const dir = dirTren(t);
  const a = distToLatLng(viaDe(t), d), b = distToLatLng(viaDe(t), d + dir * 30);
  if (!a || !b) return [0, 0];
  const vx = (b[0] - a[0]) * 111000, vy = (b[1] - a[1]) * 85000;
  const L = Math.hypot(vx, vy) || 1;
  return [vx / L, vy / L];
}
// calcula per a cada tren el desplaçament (m enrere) que el separa dels altres
// de la mateixa via; només toca al de DARRERE, el davanter es queda al seu lloc.
function resolveColas(items) {
  for (let pass = 0; pass < 5; pass++) {
    let mogut = false;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const A = items[i], B = items[j];
        if (A.t.linea === B.t.linea) continue;                 // mateixa línia: ja van en fila
        if (A.h[0] * B.h[0] + A.h[1] * B.h[1] < 0.5) continue;  // sentits distints: doble via, ok
        const pA = distToLatLng(viaDe(A.t), A.d - A.dir * A.shift);
        const pB = distToLatLng(viaDe(B.t), B.d - B.dir * B.shift);
        if (!pA || !pB) continue;
        const gap = metresEntre(pA, pB);
        if (gap >= COLA_MIN_SEP) continue;
        // qui va darrere? projecció del vector A->B sobre el rumb mitjà
        const hx = A.h[0] + B.h[0], hy = A.h[1] + B.h[1];
        const proj = ((pB[0] - pA[0]) * 111000) * hx + ((pB[1] - pA[1]) * 85000) * hy;
        const darrere = proj > 0 ? A : (proj < 0 ? B : (A.t.linea < B.t.linea ? A : B));
        const nou = Math.min(COLA_MAX_SHIFT, darrere.shift + (COLA_MIN_SEP - gap));
        if (nou > darrere.shift + 0.5) { darrere.shift = nou; mogut = true; }
      }
    }
    if (!mogut) break;
  }
}

/* ---------- animació contínua sobre la via ---------- */
function animar() {
  // 1) estat viu de cada tren visible (distància sobre la via, direcció, rumb)
  const items = [];
  for (const e of marcadores.values()) {
    let d = distVivaTren(e.tren);
    if (d == null) continue;
    const dir = dirTren(e.tren);
    // Mai cap arrere: si una pasada nova porta la mateixa trajectòria (la font no
    // s'ha mogut) o el tren es marca congelat, recalcular des del seu d0 el tornaria
    // on era en obrir el mapa. Es queda on es veu fins que les dades l'atrapen. Amb
    // el mateix track_id és el mateix tren físic: sempre es reté. Sense id, un salt
    // gran (>1,5 km) es respecta (pot ser un altre tren); el canvi de sentit, sempre.
    const mateix = e.tren.id != null && e.idTop === e.tren.id;
    if (e.dTop != null && e.dirTop === dir && (e.dTop - d) * dir > 0
        && (mateix || (e.dTop - d) * dir < 1500)) d = e.dTop;
    e.dTop = d; e.dirTop = dir; e.idTop = e.tren.id;
    items.push({ e, t: e.tren, d, dir, h: headingVec(e.tren, d), shift: 0 });
  }
  // 2) posa en fila els trens de línies diferents que comparteixen via
  resolveColas(items);
  // 3) suavitza el desplaçament i pinta cada tren en la seua posició efectiva
  for (const it of items) {
    const e = it.e, t = it.t;
    const prev = e.colaShift || 0;
    e.colaShift = prev + Math.max(-COLA_STEP, Math.min(COLA_STEP, it.shift - prev));
    const dEff = it.d - it.dir * e.colaShift;
    // suavitzat de posició: e.dViz s'acosta a dEff cada frame. Elimina qualsevol
    // salt residual (p. ex. la 1a vegada que un tren es detecta congelat i cal
    // corregir cap arrere) fent-lo un lliscament continu en compte d'un bot.
    if (e.dViz == null) e.dViz = dEff;
    const gap = dEff - e.dViz, ag = Math.abs(gap);
    if (ag <= 8) e.dViz = dEff;
    else e.dViz += Math.sign(gap) * Math.min(ag, Math.max(8, ag * 0.30));
    const dViz = e.dViz;
    e.marker.setLatLng(distToLatLng(viaDe(t), dViz) || [t.lat, t.lng]);

    // punt NOMÉS quan és un tren esperant en la seua capçalera d'origen;
    // qualsevol tren en trànsit (encara que arribe a una terminal) va amb fletxa
    const movent = !(t.origen && velTren(t) < 2);
    const el = e.marker.getElement();
    if (el) {
      const dot = el.querySelector(".tren-dot");
      const arr = el.querySelector(".tren-arrow");
      if (dot && arr) {
        if (movent) {
          arr.style.transform = `rotate(${rumbPantalla(t, dViz)}deg)`;
          arr.style.display = "block"; dot.style.display = "none";
        } else {
          arr.style.display = "none"; dot.style.display = "block";
        }
      }
    }
    if (e.marker.isPopupOpen()) {
      const b = e.marker.getPopup().getElement()?.querySelector(".c-eta");
      if (b) b.textContent = fmtEta(proxParada(t).eta);
    }
  }
}

/* ---------- popup d'estació ---------- */
async function obrirPopupEstacio(m) {
  const chips = m.estLineas
    .map((l) => `<span class="pop-linea" style="background:#${colores[l] || "888"}">${NOMBRES_LINEA[l] || "L" + l}</span>`)
    .join("");
  const cont = document.createElement("div");
  cont.innerHTML =
    `<div class="pop-est-nom">${m.estNom}</div>` +
    `<div class="pop-est-lin">${chips}</div>` +
    `<div class="pop-ruta"><button data-r="orig">Des d'ací</button><button data-r="dest">Fins ací</button></div>` +
    `<div class="pop-arr-tit">Pròximes arribades</div>` +
    `<div class="pop-arr">carregant…</div>` +
    `<div class="pop-canc"></div>` +
    `<div class="pop-xat" data-tipus="estacio" data-lloc="${m.estId}" data-nom="${m.estNom}"></div>`;
  cont.querySelectorAll(".pop-ruta button").forEach((b) => b.onclick = () => {
    map.closePopup();
    if (window.rutaFixa) window.rutaFixa(b.dataset.r, m.estNom);
  });
  L.popup({ maxWidth: 300, minWidth: 230 }).setLatLng(m.getLatLng()).setContent(cont).openOn(map);
  if (serverNow() / 1000 - arrivalsCache.ts > 20) {
    try { arrivalsCache = await (await fetch(API + "api/arrivals?_=" + Date.now(), { cache: "no-store" })).json(); } catch (e) {}
  }
  pintarLlegadesPopup(cont.querySelector(".pop-arr"), m.estId);
  if (window.xatOmpliPopup) window.xatOmpliPopup(cont);
  pintarCancelatsPopup(cont.querySelector(".pop-canc"), m.estId);
}

// desfase breu d'una arribada respecte a la seua hora programada de pas:
// +2' (tard), · (a l'hora, d < 90 s) o res si no se sap
function desfaseArribada(r) {
  if (!r || r.retraso_s == null) return "";
  const d = r.retraso_s, min = Math.round(Math.abs(d) / 60);
  const [cls, txt] = d < 90 ? ["hora", "·"] : [d >= 180 ? "tard" : "poc", `+${min}'`];
  return `<span class="ruta-desf arr-desf ${cls}" title="programat a les ${r.hora_prog}">${txt}</span>`;
}

function pintarLlegadesPopup(el, estId) {
  const arr = (arrivalsCache.estaciones || {})[estId] || [];
  const passat = Math.max(0, Math.floor(serverNow() / 1000) - (arrivalsCache.ts || 0));
  if (!arr.length) { el.innerHTML = "<div class='pop-buida'>sense pròximes arribades</div>"; return; }
  el.innerHTML = arr.slice(0, 6).map((a) => {
    const c = "#" + (colores[a.linea] || "888");
    const eta = Math.max(0, a.eta_seg - passat);
    const cls = eta <= 30 ? "arr-eta imminent" : "arr-eta";
    return `<div class="arr-row">
      <span class="pop-linea" style="background:${c}">${NOMBRES_LINEA[a.linea] || "L" + a.linea}</span>
      <span class="arr-dest">→ ${a.destino}</span>${desfaseArribada(a.retraso)}
      <span class="${cls}" data-eta="${a.eta_seg}">${fmtEta(eta)}</span></div>`;
  }).join("");
}

// Trens programats que NO han passat per l'estació hui (ni a la seua hora ni
// abans de l'hora del següent): el servidor els manté tot el dia. Caché 60 s.
const cancelatsCache = new Map();   // id estació -> {ms, cancelats}
async function pintarCancelatsPopup(el, estId) {
  if (!el) return;
  let c = cancelatsCache.get(estId);
  if (!c || Date.now() - c.ms > 60000) {
    try {
      const j = await (await fetch(API + "api/cancelats?estacio=" + estId + "&_=" + Date.now(), { cache: "no-store" })).json();
      c = { ms: Date.now(), cancelats: j.cancelats || [] };
      cancelatsCache.set(estId, c);
    } catch (e) { return; }
  }
  if (!el.isConnected || !c.cancelats.length) { if (el.isConnected) el.innerHTML = ""; return; }
  el.innerHTML = `<div class="pop-arr-tit pop-canc-tit">Cancel·lats hui</div>` +
    c.cancelats.map((x) => {
      const chips = (x.lineas || []).map((l) =>
        `<span class="pop-linea" style="background:#${colores[l] || "888"}">${NOMBRES_LINEA[l] || "L" + l}</span>`).join("");
      return `<div class="canc-row"><span class="canc-hora">${x.hora}</span> · ${chips}<span class="canc-cap">cap a ${x.hacia}</span></div>`;
    }).join("");
}

/* ---------- refresc ---------- */
function pintarNoinfo() {
  // Els tramvies (sinTiempoReal) NO es diferencien a la llegenda; només es marca
  // una línia de metro que ara mateix no dóna informació (tall real de la font).
  for (const [l, chip] of Object.entries(chipsPorLinea)) {
    const n = Number(l);
    chip.classList.toggle("noinfo", noinfo.includes(n));
    chip.title = noinfo.includes(n) ? "sense informació ara mateix" : "";
  }
}

// Marca com a congelats els trens el d0 dels quals no ha canviat entre pulls
// (dades d'horari estàtiques). Cal 2 pulls iguals seguits per a evitar falsos
// positius amb trens realment quiets un instant. Poda els ids desapareguts.
function actualitzaCongelats(trenes) {
  const vius = new Set();
  for (const t of trenes) {
    if (t.id == null || !t.waypoints || !t.waypoints.length) continue;
    vius.add(t.id);
    const d0 = t.waypoints[0].d;
    const h = histTren.get(t.id);
    if (h && Math.abs(d0 - h.d0) < FREEZE_TOL) {
      h.iguals += 1;
      h.congelat = h.iguals >= 1;   // 2 pulls seguits amb el mateix d0
      h.d0 = d0;
    } else {
      histTren.set(t.id, { d0, iguals: 0, congelat: false });
    }
  }
  for (const id of [...histTren.keys()]) if (!vius.has(id)) histTren.delete(id);
}

async function refrescar() {
  if (refrescant) return;
  refrescant = true;
  try {
    const t0 = Date.now();
    const data = await (await fetch(API + "api/trains?_=" + Date.now(), { cache: "no-store" })).json();
    const t1 = Date.now();
    // ---- ping + desfàs de rellotge (client↔Raspa) ----
    if (data.srv_ms) {
      const rtt = t1 - t0;                       // round-trip d'aquesta petició
      // el servidor va marcar srv_ms ~a meitat del viatge; la seua hora a t1 ≈ srv_ms + rtt/2
      const mostra = (data.srv_ms + rtt / 2) - t1;   // offset a sumar a Date.now()
      pingMs = pingMs === 0 ? rtt : pingMs * 0.7 + rtt * 0.3;   // sempre actualitza el ping (EMA)
      // només actualitza l'offset amb mostres netes (descarta pics de latència que embruten el rellotge)
      if (clockOffsetMs === 0 || rtt <= pingMs * 1.8 + 60) {
        clockOffsetMs = clockOffsetMs === 0 ? mostra : clockOffsetMs * 0.75 + mostra * 0.25;
      }
    }
    ultimoTrenes = data.trenes || [];
    ultimoTs = data.ts || Math.floor(serverNow() / 1000);
    ultimoFetchMs = t1;
    ultimoTsFin = data.ts_fin || ultimoTs;
    noinfo = data.noinfo || [];
    actualitzaCongelats(ultimoTrenes);
    pintarTrenes(ultimoTrenes);
    pintarNoinfo();
    if (window.refrescaPanells) window.refrescaPanells();
    const edad = data.edad_seg == null ? "—" : data.edad_seg + " s";
    const ni = noinfo.length ? `<br><span style="color:#e8c07a">sense info: ${noinfo.map(l=>"L"+l).join(", ")}</span>` : "";
    const ping = pingMs ? `<br><span style="opacity:.65">ping ${Math.round(pingMs)} ms</span>` : "";
    document.getElementById("stats").innerHTML =
      `<b>${ultimoTrenes.length}</b> trens en directe<br>dades fa <b>${edad}</b>${ni}${ping}`;
  } catch (e) {
    document.getElementById("stats").textContent = "sense connexió amb el backend";
  } finally {
    refrescant = false;
  }
}

// El poller llig FGV cada minut (pasada de ~26 s). En lloc de refrescar cada
// minut a cegues (fins a 60 s de retard afegit), es pregunta cada 2 s si hi ha
// pasada nova i, si n'hi ha, es demanen els trens: els comptadors del rellotge
// es reinicien quan toca, no quan cau el temporitzador.
async function vigilaServidor() {
  if (refrescant) return;
  try {
    const j = await (await fetch(API + "api/ultimo?_=" + Date.now(), { cache: "no-store" })).json();
    const marca = j.ts_fin || j.ts || 0;
    if (marca > (ultimoTsFin || ultimoTs)) refrescar();
    if (window.xatNovetats) window.xatNovetats(j.xat_id || 0);
  } catch (e) { /* sense xarxa: el refresc de seguretat ja ho dirà */ }
}

// per al xat (xat.js): trens vius amb la seua posició en pantalla, i marcadors per id
window.trensVius = () => [...marcadores.values()].map((e) => ({ t: e.tren, ll: e.marker.getLatLng() }));
window.marcadorTren = (id) => { for (const e of marcadores.values()) if (e.tren.id === id) return e.marker; return null; };
window.marcadorEstacio = (id) => marcadorsEstacio.get(Number(id)) || null;

function tickSegon() {
  document.getElementById("reloj-hora").textContent = new Date().toLocaleTimeString("ca-ES");
  // segons des de l'última lectura de FGV (snapshot del servidor, hora de la Raspa)
  // i des de l'últim refresc d'aquest client; passats 150 s de FGV, en ambre
  const fgv = document.getElementById("reloj-fgv");
  const cli = document.getElementById("reloj-cli");
  if (ultimoTs) {
    const s = Math.max(0, Math.floor(serverNow() / 1000) - (ultimoTsFin || ultimoTs));
    fgv.textContent = `FGV ${s} s`;
    fgv.classList.toggle("reloj-vell", s > 150);
  }
  if (ultimoFetchMs) cli.textContent = `client ${Math.max(0, Math.floor((Date.now() - ultimoFetchMs) / 1000))} s`;
  const arrEls = document.querySelectorAll(".pop-arr .arr-eta");
  if (arrEls.length) {
    const passat = Math.max(0, Math.floor(serverNow() / 1000) - (arrivalsCache.ts || 0));
    arrEls.forEach((el) => { el.textContent = fmtEta(Math.max(0, parseInt(el.dataset.eta, 10) - passat)); });
  }
}

// panell colapsable a un lateral
(function () {
  const hud = document.getElementById("hud");
  const btn = document.getElementById("hud-toggle");
  if (btn && hud) {
    btn.addEventListener("click", () => {
      const col = hud.classList.toggle("collapsed");
      btn.textContent = col ? "»" : "«";
      btn.setAttribute("aria-label", col ? "Mostra el panell" : "Amaga el panell");
    });
  }
})();

(async function main() {
  await cargarRed();
  if (window.iniciaPanells) window.iniciaPanells();
  if (window.iniciaXat) window.iniciaXat();
  await refrescar();
  setInterval(refrescar, REFRESH_MS);
  setInterval(vigilaServidor, VIGILA_MS);
  setInterval(animar, ANIM_MS);
  setInterval(tickSegon, 1000);
  tickSegon();
})();
