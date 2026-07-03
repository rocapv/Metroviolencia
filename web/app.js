/* Metroviolencia · frontend */
const API = "";           // misma-origin (FastAPI sirve /api y estáticos)
const REFRESH_MS = 15000; // el poller va a ~30s; refrescamos suave

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

const lineasVisibles = new Set();   // líneas activas (por defecto todas)
let trenLayer = L.layerGroup().addTo(map);
const marcadores = new Map();       // clave tren -> marker (para animar)
let colores = {};

const hex = (c) => (c && c.startsWith("#") ? c : "#" + (c || "888"));

async function cargarRed() {
  const net = await (await fetch(API + "/api/network")).json();
  colores = net.colores || {};

  // polylines (trazado de vía) — se dibujan por debajo de las estaciones
  for (const [lid, info] of Object.entries(net.lineas)) {
    if (!info.path || info.path.length < 2) continue;
    L.polyline(info.path, {
      color: hex("#" + info.color),
      weight: 3.5,
      opacity: 0.85,
      lineJoin: "round",
    }).addTo(map);
  }

  // estaciones
  for (const e of net.estaciones) {
    L.circleMarker([e.lat, e.lng], {
      radius: 3.2,
      color: "#fff",
      weight: 1,
      fillColor: "#111",
      fillOpacity: 1,
    })
      .bindTooltip(e.nombre, { direction: "top", opacity: 0.9 })
      .bindPopup(popupEstacion(e))
      .addTo(map);
  }

  // líneas presentes en trenes -> leyenda
  const lineasConColor = Object.keys(net.colores || {})
    .map(Number)
    .filter((n) => n >= 1 && n <= 10)
    .sort((a, b) => a - b);
  construirLeyenda(lineasConColor);
}

function popupEstacion(e) {
  const el = document.createElement("div");
  el.innerHTML = `<b>${e.nombre}</b><br><span style="color:#9a9aac">líneas ${e.lineas.join(", ")}</span>
    <div class="pop-arr" data-est="${e.id}" style="margin-top:6px;color:#9a9aac">…</div>`;
  el.querySelector(".pop-arr").dataset.pending = "1";
  return el;
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

let ultimoTrenes = [];

function claveTren(t) {
  return `${t.linea}|${t.destino}|${t.prox_estacion_id}`;
}

function pintarTrenes(trenes) {
  const vivos = new Set();
  for (const t of trenes) {
    if (!lineasVisibles.has(t.linea)) continue;
    const k = claveTren(t);
    vivos.add(k);
    let m = marcadores.get(k);
    if (m) {
      m.setLatLng([t.lat, t.lng]);   // transición suave vía CSS
    } else {
      m = L.marker([t.lat, t.lng], { icon: iconoTren(t) }).bindPopup(popupTren(t));
      m.addTo(trenLayer);
      marcadores.set(k, m);
    }
    m.setPopupContent(popupTren(t));
  }
  // eliminar los que ya no están
  for (const [k, m] of marcadores) {
    if (!vivos.has(k)) { trenLayer.removeLayer(m); marcadores.delete(k); }
  }
}

function iconoTren(t) {
  const c = "#" + (t.color || "888");
  return L.divIcon({
    className: "",
    html: `<div class="tren" style="width:13px;height:13px;background:${c}"></div>`,
    iconSize: [13, 13],
    iconAnchor: [6, 6],
  });
}

function popupTren(t) {
  const c = "#" + (t.color || "888");
  const min = Math.round(t.eta_seg / 60);
  return `<span class="pop-linea" style="background:${c}">${NOMBRES_LINEA[t.linea] || "L" + t.linea}</span>
    → <b>${t.destino}</b><br>
    <span style="color:#9a9aac">próxima: ${t.prox_estacion} · ${min} min</span>`;
}

async function refrescar() {
  try {
    const data = await (await fetch(API + "/api/trains")).json();
    ultimoTrenes = data.trenes || [];
    pintarTrenes(ultimoTrenes);
    const edad = data.edad_seg == null ? "—" : data.edad_seg + "s";
    document.getElementById("stats").innerHTML =
      `<b>${ultimoTrenes.length}</b> trenes en vivo<br>datos hace <b>${edad}</b>`;
  } catch (e) {
    document.getElementById("stats").textContent = "sin conexión con el backend";
  }
}

function tick() {
  const d = new Date();
  document.getElementById("reloj").textContent = d.toLocaleTimeString("es-ES");
}

(async function main() {
  await cargarRed();
  await refrescar();
  setInterval(refrescar, REFRESH_MS);
  setInterval(tick, 1000);
  tick();
})();
