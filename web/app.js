/* Metroviolencia · frontend (valencià) */
// Rutas RELATIVAS: funciona tanto en local (/) como tras proxy (/metroviolencia/).
const API = "";
const REFRESH_MS = 15000;   // sondeo del backend (el poller hace pull cada 60 s)

const NOMBRES_LINEA = {
  1: "L1", 2: "L2", 3: "L3", 4: "L4", 5: "L5",
  6: "L6", 7: "L7", 8: "L8", 9: "L9", 10: "L10",
};

const map = L.map("map", { zoomControl: false, attributionControl: false })
  .setView([39.47, -0.38], 12);

L.control.zoom({ position: "bottomleft" }).addTo(map);
L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd" }
).addTo(map);
L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd", opacity: 0.7 }
).addTo(map);

const lineasVisibles = new Set();
const trenLayer = L.layerGroup().addTo(map);
const marcadores = new Map();       // clau tren -> {marker, tren}
let colores = {};
let ultimoTrenes = [];
let ultimoTs = 0;                   // ts (epoch s) de l'últim snapshot rebut
let arrivalsCache = { ts: 0, estaciones: {} };

const hex = (c) => (c && c.startsWith("#") ? c : "#" + (c || "888"));

/* ---------- format de temps en valencià ---------- */
function fmtEta(seg) {
  if (seg <= 0) return "arribant";
  const m = Math.floor(seg / 60);
  const s = seg % 60;
  if (m === 0) return `${s} s`;
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}
// segons transcorreguts des de l'últim snapshot -> ETA "en viu"
function etaViu(tren) {
  const passat = Math.max(0, Math.floor(Date.now() / 1000) - ultimoTs);
  return Math.max(0, tren.eta_seg - passat);
}

/* ---------- xarxa ---------- */
async function cargarRed() {
  const net = await (await fetch(API + "api/network")).json();
  colores = net.colores || {};

  for (const [, info] of Object.entries(net.lineas)) {
    if (!info.path || info.path.length < 2) continue;
    L.polyline(info.path, {
      color: hex("#" + info.color), weight: 3.5, opacity: 0.85, lineJoin: "round",
    }).addTo(map);
  }

  for (const e of net.estaciones) {
    const m = L.circleMarker([e.lat, e.lng], {
      radius: 3.2, color: "#fff", weight: 1, fillColor: "#111", fillOpacity: 1,
    }).addTo(map);
    m.bindTooltip(e.nombre, { direction: "top", opacity: 0.9 });
    m.estId = e.id;
    m.estNom = e.nombre;
    m.estLineas = e.lineas;
    m.on("click", () => obrirPopupEstacio(m));
  }

  const lineas = Object.keys(colores).map(Number)
    .filter((n) => n >= 1 && n <= 10).sort((a, b) => a - b);
  construirLeyenda(lineas);
}

function construirLeyenda(lineas) {
  const ley = document.getElementById("leyenda");
  ley.innerHTML = "";
  for (const l of lineas) {
    lineasVisibles.add(l);
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = NOMBRES_LINEA[l] || "L" + l;
    chip.style.background = "#" + (colores[l] || "888");
    chip.onclick = () => {
      if (lineasVisibles.has(l)) { lineasVisibles.delete(l); chip.classList.add("off"); }
      else { lineasVisibles.add(l); chip.classList.remove("off"); }
      pintarTrenes(ultimoTrenes);
    };
    ley.appendChild(chip);
  }
}

/* ---------- trens ---------- */
const claveTren = (t) => `${t.linea}|${t.destino}|${t.prox_estacion_id}`;

function pintarTrenes(trenes) {
  const vius = new Set();
  for (const t of trenes) {
    if (!lineasVisibles.has(t.linea)) continue;
    const k = claveTren(t);
    vius.add(k);
    let entry = marcadores.get(k);
    if (entry) {
      entry.marker.setLatLng([t.lat, t.lng]);
      entry.tren = t;
    } else {
      const marker = L.marker([t.lat, t.lng], { icon: iconoTren(t) });
      marker.addTo(trenLayer);
      entry = { marker, tren: t };
      marcadores.set(k, entry);
    }
    entry.marker.setPopupContent(popupTren(entry.tren));
    if (!entry.marker.getPopup()) entry.marker.bindPopup(popupTren(entry.tren));
  }
  for (const [k, entry] of marcadores) {
    if (!vius.has(k)) { trenLayer.removeLayer(entry.marker); marcadores.delete(k); }
  }
}

function iconoTren(t) {
  const c = "#" + (t.color || "888");
  return L.divIcon({
    className: "",
    html: `<div class="tren" style="width:13px;height:13px;background:${c}"></div>`,
    iconSize: [13, 13], iconAnchor: [6, 6],
  });
}

function popupTren(t) {
  const c = "#" + (t.color || "888");
  return `<span class="pop-linea" style="background:${c}">${NOMBRES_LINEA[t.linea] || "L" + t.linea}</span>
    → <b>${t.destino}</b><br>
    <span style="color:#9a9aac">pròxima parada: ${t.prox_estacion} · <b class="c-eta">${fmtEta(etaViu(t))}</b></span>`;
}

/* ---------- popup d'estació amb llegades en viu ---------- */
async function obrirPopupEstacio(m) {
  const cont = document.createElement("div");
  cont.innerHTML = `<b>${m.estNom}</b><br>
    <span style="color:#9a9aac">línies ${m.estLineas.join(", ")}</span>
    <div class="pop-arr" style="margin-top:8px">carregant…</div>`;
  const pop = L.popup({ maxWidth: 260 }).setLatLng(m.getLatLng()).setContent(cont);
  pop.openOn(map);

  if (Date.now() / 1000 - arrivalsCache.ts > 20) {
    try {
      arrivalsCache = await (await fetch(API + "api/arrivals")).json();
    } catch (e) { /* usa caché */ }
  }
  pintarLlegadesPopup(cont.querySelector(".pop-arr"), m.estId);
}

function pintarLlegadesPopup(el, estId) {
  const arr = (arrivalsCache.estaciones || {})[estId] || [];
  const passat = Math.max(0, Math.floor(Date.now() / 1000) - (arrivalsCache.ts || 0));
  if (!arr.length) { el.innerHTML = "<i style='color:#9a9aac'>sense pròximes arribades</i>"; return; }
  el.dataset.est = estId;
  el.innerHTML = arr.slice(0, 6).map((a) => {
    const c = "#" + (colores[a.linea] || "888");
    const eta = Math.max(0, a.eta_seg - passat);
    return `<div class="arr-row">
      <span class="pop-linea" style="background:${c}">${NOMBRES_LINEA[a.linea] || "L" + a.linea}</span>
      <span class="arr-dest">${a.destino}</span>
      <span class="arr-eta" data-eta="${a.eta_seg}">${fmtEta(eta)}</span>
    </div>`;
  }).join("");
}

/* ---------- refresc i compte arrere ---------- */
async function refrescar() {
  try {
    const data = await (await fetch(API + "api/trains")).json();
    ultimoTrenes = data.trenes || [];
    ultimoTs = data.ts || Math.floor(Date.now() / 1000);
    pintarTrenes(ultimoTrenes);
    const edad = data.edad_seg == null ? "—" : data.edad_seg + " s";
    document.getElementById("stats").innerHTML =
      `<b>${ultimoTrenes.length}</b> trens en directe<br>dades fa <b>${edad}</b>`;
  } catch (e) {
    document.getElementById("stats").textContent = "sense connexió amb el backend";
  }
}

// cada segon: actualitza ETAs visibles (compte arrere) sense tornar a sondejar
function tickSegon() {
  const d = new Date();
  document.getElementById("reloj").textContent = d.toLocaleTimeString("ca-ES");

  // popup de tren obert
  for (const { marker, tren } of marcadores.values()) {
    if (marker.isPopupOpen()) {
      const b = marker.getPopup().getElement()?.querySelector(".c-eta");
      if (b) b.textContent = fmtEta(etaViu(tren));
    }
  }
  // popup d'estació obert
  const arrEls = document.querySelectorAll(".pop-arr .arr-eta");
  if (arrEls.length) {
    const passat = Math.max(0, Math.floor(Date.now() / 1000) - (arrivalsCache.ts || 0));
    arrEls.forEach((el) => {
      const base = parseInt(el.dataset.eta, 10);
      el.textContent = fmtEta(Math.max(0, base - passat));
    });
  }
}

(async function main() {
  await cargarRed();
  await refrescar();
  setInterval(refrescar, REFRESH_MS);
  setInterval(tickSegon, 1000);
  tickSegon();
})();
