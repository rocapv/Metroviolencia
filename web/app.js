/* Metroviolencia · frontend (valencià) — trens animats sobre la via */
const API = "";
const REFRESH_MS = 15000;   // sondeig del backend (el poller fa pull cada 60 s)
const ANIM_MS = 500;        // pas d'animació dels punts

const NOMBRES_LINEA = {
  1: "L1", 2: "L2", 3: "L3", 4: "L4", 5: "L5",
  6: "L6", 7: "L7", 8: "L8", 9: "L9", 10: "L10",
};

const map = L.map("map", { zoomControl: false, attributionControl: false })
  .setView([39.47, -0.38], 12);

L.control.zoom({ position: "bottomleft" }).addTo(map);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd" }).addTo(map);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd", opacity: 0.7 }).addTo(map);

const lineasVisibles = new Set();
const trenLayer = L.layerGroup().addTo(map);
const marcadores = new Map();      // clau -> {marker, tren}
let colores = {};                  // color per línia comercial
let comerciales = {};              // {lid: {path, cum, length}}
let ultimoTrenes = [];
let ultimoTs = 0;
let noinfo = [];                   // línies metro sense info ara mateix (tall de font)
let sinTiempoReal = [];            // trams sense temps real (sempre)
let arrivalsCache = { ts: 0, estaciones: {} };

const hex = (c) => (c && c.startsWith("#") ? c : "#" + (c || "888"));

/* ---------- distància sobre la via -> (lat,lng) ---------- */
function distToLatLng(lid, d) {
  const lc = comerciales[lid];
  if (!lc || !lc.cum || lc.cum.length < 2) return null;
  const cum = lc.cum, path = lc.path;
  const L = cum[cum.length - 1];
  d = Math.max(0, Math.min(L, d));
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
const etaViu = (t) => Math.max(0, t.eta_seg - Math.max(0, Math.floor(Date.now() / 1000) - ultimoTs));

/* ---------- xarxa ---------- */
async function cargarRed() {
  const net = await (await fetch(API + "api/network")).json();
  colores = net.colores_comercial || net.colores || {};
  comerciales = net.comerciales || {};
  sinTiempoReal = net.sin_tiempo_real || [];

  // traçats de via (segments) — color propi, ja correcte
  for (const [, info] of Object.entries(net.lineas)) {
    if (!info.path || info.path.length < 2) continue;
    L.polyline(info.path, { color: hex("#" + info.color), weight: 3.5,
      opacity: 0.85, lineJoin: "round" }).addTo(map);
  }
  // traçat real dels tramvies (L4/L6/L8/L10) des d'OSM, en color comercial
  for (const [ref, ways] of Object.entries(net.tram_tracks || {})) {
    const col = hex("#" + (colores[ref] || "888"));
    for (const w of ways) {
      L.polyline(w, { color: col, weight: 3.5, opacity: 0.9, lineJoin: "round" })
        .addTo(map);
    }
  }
  // connectors: trams de via que falten en les polylines de metro
  for (const c of net.conectores || []) {
    L.polyline(c.path, { color: hex("#" + c.color), weight: 3.5, opacity: 0.85,
      lineJoin: "round" }).addTo(map)
      .bindTooltip(`${c.a} – ${c.b}`, { sticky: true });
  }

  const porNom = {};
  for (const e of net.estaciones) {
    porNom[e.nombre] = e;
    const m = L.circleMarker([e.lat, e.lng], { radius: 3.2, color: "#fff",
      weight: 1, fillColor: "#111", fillOpacity: 1 }).addTo(map);
    m.bindTooltip(e.nombre, { direction: "top", opacity: 0.9 });
    m.estId = e.id; m.estNom = e.nombre; m.estLineas = e.lineas;
    m.on("click", () => obrirPopupEstacio(m));
  }

  dibuixarCierres(net.cierres || [], porNom);

  const lineas = Object.keys(colores).map(Number)
    .filter((n) => n >= 1 && n <= 10).sort((a, b) => a - b);
  construirLeyenda(lineas);
}

function dibuixarCierres(cierres, porNom) {
  const av = document.getElementById("avisos");
  av.dataset.cierres = "";
  for (const c of cierres) {
    const pts = (c.tramo || []).map((n) => porNom[n]).filter(Boolean).map((e) => [e.lat, e.lng]);
    if (pts.length >= 2) {
      L.polyline(pts, { color: "#7a7a86", weight: 5, opacity: 0.9,
        dashArray: "2 9", lineCap: "round" }).addTo(map)
        .bindTooltip(c.nota || "Tram tallat", { sticky: true });
      for (const p of pts) L.circleMarker(p, { radius: 4, color: "#7a7a86",
        weight: 2, fillColor: "#1a1a22", fillOpacity: 1 }).addTo(map);
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
      if (lineasVisibles.has(l)) { lineasVisibles.delete(l); chip.classList.add("off"); }
      else { lineasVisibles.add(l); chip.classList.remove("off"); }
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
  const tau = Math.max(0, Math.floor(Date.now() / 1000) - ultimoTs);
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
  return (d == null ? null : distToLatLng(t.linea, d)) || [t.lat, t.lng];
}
// pròxima parada viva (primer waypoint amb nom i eta > temps transcorregut)
function proxParada(t) {
  const tau = Math.max(0, Math.floor(Date.now() / 1000) - ultimoTs);
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
    const ll = posicioViva(t);
    let best = null, bestD = Infinity;
    for (const e of previos) {
      if (e.usado || e.tren.linea !== t.linea || e.tren.destino !== t.destino) continue;
      const p = e.marker.getLatLng();
      const dd = Math.hypot((p.lat - ll[0]) * 111000, (p.lng - ll[1]) * 85000);
      if (dd < bestD) { bestD = dd; best = e; }
    }
    if (best && bestD < 1500) {   // mateix tren físic: reaprofita el marcador
      best.usado = true; best.tren = t;
      best.marker.setLatLng(ll);
      best.marker.setPopupContent(popupTren(t));
    } else {
      const marker = L.marker(ll, { icon: iconoTren(t) }).bindPopup(popupTren(t));
      marker.addTo(trenLayer);
      nuevos.push({ marker, tren: t, usado: true });
    }
  }
  // esborrar marcadors no reaprofitats i reconstruir el mapa
  for (const e of previos) if (!e.usado) trenLayer.removeLayer(e.marker);
  marcadores.clear();
  let n = 0;
  for (const e of previos) if (e.usado) marcadores.set("m" + n++, e);
  for (const e of nuevos) marcadores.set("m" + n++, e);
}

function iconoTren(t) {
  const c = "#" + (t.color || "888");
  return L.divIcon({ className: "",
    html: `<div class="tren" style="width:13px;height:13px;background:${c}"></div>`,
    iconSize: [13, 13], iconAnchor: [6, 6] });
}

function popupTren(t) {
  const c = "#" + (t.color || "888");
  const p = proxParada(t);
  const espera = t.origen ? ' <span style="color:#e8c07a">(en eixida)</span>' : "";
  return `<span class="pop-linea" style="background:${c}">${NOMBRES_LINEA[t.linea] || "L" + t.linea}</span>
    → <b>${t.destino}</b>${espera}<br>
    <span style="color:#9a9aac">pròxima parada: ${p.nom} · <b class="c-eta">${fmtEta(p.eta)}</b></span>`;
}

/* ---------- animació contínua sobre la via ---------- */
function animar() {
  for (const { marker, tren } of marcadores.values()) {
    marker.setLatLng(posicioViva(tren));
    if (marker.isPopupOpen()) {
      const b = marker.getPopup().getElement()?.querySelector(".c-eta");
      if (b) b.textContent = fmtEta(proxParada(tren).eta);
    }
  }
}

/* ---------- popup d'estació ---------- */
async function obrirPopupEstacio(m) {
  const cont = document.createElement("div");
  cont.innerHTML = `<b>${m.estNom}</b><br>
    <span style="color:#9a9aac">línies ${m.estLineas.join(", ")}</span>
    <div class="pop-arr" style="margin-top:8px">carregant…</div>`;
  L.popup({ maxWidth: 260 }).setLatLng(m.getLatLng()).setContent(cont).openOn(map);
  if (Date.now() / 1000 - arrivalsCache.ts > 20) {
    try { arrivalsCache = await (await fetch(API + "api/arrivals")).json(); } catch (e) {}
  }
  pintarLlegadesPopup(cont.querySelector(".pop-arr"), m.estId);
}

function pintarLlegadesPopup(el, estId) {
  const arr = (arrivalsCache.estaciones || {})[estId] || [];
  const passat = Math.max(0, Math.floor(Date.now() / 1000) - (arrivalsCache.ts || 0));
  if (!arr.length) { el.innerHTML = "<i style='color:#9a9aac'>sense pròximes arribades</i>"; return; }
  el.innerHTML = arr.slice(0, 6).map((a) => {
    const c = "#" + (colores[a.linea] || "888");
    const eta = Math.max(0, a.eta_seg - passat);
    return `<div class="arr-row">
      <span class="pop-linea" style="background:${c}">${NOMBRES_LINEA[a.linea] || "L" + a.linea}</span>
      <span class="arr-dest">${a.destino}</span>
      <span class="arr-eta" data-eta="${a.eta_seg}">${fmtEta(eta)}</span></div>`;
  }).join("");
}

/* ---------- refresc ---------- */
function pintarNoinfo() {
  for (const [l, chip] of Object.entries(chipsPorLinea)) {
    const n = Number(l);
    const sense = noinfo.includes(n) || sinTiempoReal.includes(n);
    chip.classList.toggle("noinfo", sense);
    chip.title = sinTiempoReal.includes(n) ? "sense temps real (tramvia)"
      : (noinfo.includes(n) ? "sense informació ara mateix" : "");
  }
}

async function refrescar() {
  try {
    const data = await (await fetch(API + "api/trains")).json();
    ultimoTrenes = data.trenes || [];
    ultimoTs = data.ts || Math.floor(Date.now() / 1000);
    noinfo = data.noinfo || [];
    pintarTrenes(ultimoTrenes);
    pintarNoinfo();
    const edad = data.edad_seg == null ? "—" : data.edad_seg + " s";
    const ni = noinfo.length ? `<br><span style="color:#e8c07a">sense info: ${noinfo.map(l=>"L"+l).join(", ")}</span>` : "";
    document.getElementById("stats").innerHTML =
      `<b>${ultimoTrenes.length}</b> trens en directe<br>dades fa <b>${edad}</b>${ni}`;
  } catch (e) {
    document.getElementById("stats").textContent = "sense connexió amb el backend";
  }
}

function tickSegon() {
  document.getElementById("reloj").textContent = new Date().toLocaleTimeString("ca-ES");
  const arrEls = document.querySelectorAll(".pop-arr .arr-eta");
  if (arrEls.length) {
    const passat = Math.max(0, Math.floor(Date.now() / 1000) - (arrivalsCache.ts || 0));
    arrEls.forEach((el) => { el.textContent = fmtEta(Math.max(0, parseInt(el.dataset.eta, 10) - passat)); });
  }
}

(async function main() {
  await cargarRed();
  await refrescar();
  setInterval(refrescar, REFRESH_MS);
  setInterval(animar, ANIM_MS);
  setInterval(tickSegon, 1000);
  tickSegon();
})();
