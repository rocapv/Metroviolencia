/* Metro + Bus · ruta combinada i navegació entre metro.pr1.es i bus.pr1.es.
   EL MATEIX FITXER va a les dues webs (repos Metroviolencia i Bus): l'atribut
   data-app de l'etiqueta <script> diu on està ("metro" o "bus").

   - Botó «⇄ Bus» / «⇄ Metro»: obri l'altra web en el mateix tros de mapa (i amb la
     mateixa ruta, si n'hi ha) per mitjà del hash: #v=lat,lng,zoom&o=lat,lng&d=lat,lng
   - Panell «Metro+Bus»: amb el panell obert, el primer toc al mapa és l'origen i el
     segon el destí (els punts es poden arrossegar). També amb pulsació llarga (o
     clic dret) en qualsevol moment: «Ruta des d'ací / fins ací».
   - La ruta la calcula Bus (/api/intermodal): parades d'EMT, estacions de metro, a
     peu entre elles i l'espera real dels panells de FGV per al metro.
   L'estat (punts i ruta) és d'esta pestanya i ningú més el veu. */
(function () {
  const APP = (document.currentScript && document.currentScript.dataset.app) || "metro";
  const ALTRA = APP === "metro" ? "bus" : "metro";
  const NOM = { metro: "Metro", bus: "Bus" };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ---------- adreça de l'altra web ---------- */
  function urlAltra() {
    if (/(^|\.)pr1\.es$/.test(location.hostname)) return `https://${ALTRA}.pr1.es/`;
    // LAN: les dues penjen del mateix Apache (/metroviolencia/ i /bus/)
    return location.origin + (ALTRA === "metro" ? "/metroviolencia/" : "/bus/");
  }
  const fmtLL = (ll) => ll.lat.toFixed(5) + "," + ll.lng.toFixed(5);
  function hashVista() {
    const c = map.getCenter();
    let h = `v=${c.lat.toFixed(5)},${c.lng.toFixed(5)},${map.getZoom()}`;
    if (orig && dest) h += `&o=${fmtLL(orig.getLatLng())}&d=${fmtLL(dest.getLatLng())}`;
    return "#" + h;
  }
  function vesAltra(ev) {
    if (ev) ev.preventDefault();
    location.href = urlAltra() + hashVista();
  }

  /* ---------- estils (la web Bus no té els de panell del metro) ---------- */
  const css = document.createElement("style");
  css.textContent = `
#im-menu { position: absolute; top: 12px; right: 12px; z-index: 1001; display: flex; gap: 6px; }
#im-menu button, #im-panel button {
  font: inherit; font-size: 13px; font-weight: 600; color: var(--txt, #e8e8ef);
  background: var(--panel, rgba(18,18,26,.82)); border: 1px solid rgba(255,255,255,.12);
  border-radius: 9px; padding: 7px 12px; cursor: pointer; backdrop-filter: blur(10px); }
#im-menu button.on, #im-panel button.on { border-color: #c026d3; color: #fff; }
#im-panel { position: absolute; top: 56px; right: 12px; z-index: 1001; width: 360px;
  max-height: calc(100% - 120px); overflow-y: auto; color: var(--txt, #e8e8ef);
  background: var(--panel, rgba(18,18,26,.82)); backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,.08); border-radius: 12px; padding: 12px 14px;
  box-shadow: 0 8px 30px rgba(0,0,0,.5); font-size: 13px; }
#im-panel[hidden] { display: none; }
#im-panel .im-cap { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
#im-panel .im-cap b { font-size: 15px; }
#im-panel .im-tanca { padding: 2px 9px; font-size: 16px; }
#im-panel .im-ajuda { color: var(--muted, #9a9aac); line-height: 1.45; margin-bottom: 8px; }
#im-panel .im-punts { display: grid; grid-template-columns: auto 1fr; gap: 4px 8px; align-items: center; margin-bottom: 8px; }
#im-panel .im-punt { width: 18px; height: 18px; border-radius: 50%; display: inline-flex; align-items: center;
  justify-content: center; font-size: 11px; font-weight: 800; color: #000; }
#im-panel .im-A { background: #34d399; } #im-panel .im-B { background: #f87171; }
#im-panel .im-pv { color: var(--muted, #9a9aac); }
#im-panel .im-pv.ok { color: var(--txt, #e8e8ef); }
#im-panel .im-acc { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
#im-panel .im-acc button { padding: 5px 10px; font-size: 12px; }
#im-panel .im-alt { border: 1px solid rgba(255,255,255,.1); border-radius: 10px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }
#im-panel .im-alt.on { border-color: #c026d3; background: rgba(192,38,211,.08); }
#im-panel .im-alt-cap { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 4px; }
#im-panel .im-min { font-size: 18px; font-weight: 800; }
#im-panel .im-tipus { color: var(--muted, #9a9aac); font-size: 12px; }
#im-panel .im-tram { display: grid; grid-template-columns: 22px 1fr auto; gap: 6px; padding: 3px 0; align-items: start; }
#im-panel .im-ico { text-align: center; }
#im-panel .im-lin { display: inline-block; font-size: 11px; font-weight: 800; color: #000; border-radius: 5px; padding: 0 6px; margin-right: 4px; }
#im-panel .im-det { color: var(--muted, #9a9aac); font-size: 12px; }
#im-panel .im-viu { color: #34d399; }
#im-panel .im-t { white-space: nowrap; font-variant-numeric: tabular-nums; }
#im-panel .im-err { color: #f87171; }
#im-panel .im-peu { color: var(--muted, #9a9aac); font-size: 11px; margin-top: 6px; line-height: 1.4; }
.im-marca { width: 26px; height: 26px; border-radius: 50%; border: 2px solid #fff; display: flex;
  align-items: center; justify-content: center; font: 800 13px system-ui, sans-serif; color: #000;
  box-shadow: 0 2px 8px rgba(0,0,0,.6); }
.im-ctx button { display: block; width: 100%; margin: 3px 0; font: 600 13px system-ui, sans-serif;
  border: 1px solid #ccc; border-radius: 7px; background: #fff; padding: 5px 10px; cursor: pointer; }
@media (max-width: 520px) {
  #im-panel { left: 12px; right: 12px; width: auto; max-height: 55%; }
  /* amb els dos botons nous, el menú de MetroViu no cap al costat del panell esquerre */
  #menu, #im-menu { gap: 4px; right: 8px; }
  #menu button, #im-menu button { padding: 6px 8px; font-size: 12px; }
}
`;
  document.head.appendChild(css);

  /* ---------- botons i panell ---------- */
  const menuMetro = $("menu");                       // a MetroViu, el panell s'integra al seu menú
  const menu = menuMetro || (() => {
    const n = document.createElement("nav");
    n.id = "im-menu";
    document.body.appendChild(n);
    return n;
  })();
  const bRuta = document.createElement("button");
  bRuta.id = "btn-intermodal";
  bRuta.textContent = "Metro+Bus";
  bRuta.title = "Ruta combinada de metro i bus entre dos punts del mapa";
  const bAltra = document.createElement("button");
  bAltra.id = "btn-altra-web";
  bAltra.textContent = "⇄ " + NOM[ALTRA];
  bAltra.title = `Obri ${ALTRA}.pr1.es en este mateix tros de mapa`;
  bAltra.onclick = vesAltra;
  menu.appendChild(bRuta);
  menu.appendChild(bAltra);

  const panel = document.createElement("section");
  panel.id = "im-panel";
  panel.hidden = true;
  panel.innerHTML = `
    <div class="im-cap"><b>Ruta metro + bus</b><button class="im-tanca" aria-label="Tanca">×</button></div>
    <div class="im-ajuda">Toca el mapa: primer l'<b>origen</b> i després el <b>destí</b>. Els punts es poden arrossegar.
      En qualsevol moment, una pulsació llarga al mapa també els posa.</div>
    <div class="im-punts">
      <span class="im-punt im-A">A</span><span id="im-pA" class="im-pv">toca el mapa</span>
      <span class="im-punt im-B">B</span><span id="im-pB" class="im-pv">—</span>
    </div>
    <div class="im-acc">
      <button id="im-ubic" title="La posició es queda al mòbil: només s'envia el punt de la ruta">Origen: on estic</button>
      <button id="im-inv">⇅ Inverteix</button>
      <button id="im-esb">Esborra</button>
      <button id="im-altra">Veure-la en ${NOM[ALTRA]} ⇄</button>
    </div>
    <div id="im-res"></div>`;
  document.body.appendChild(panel);
  if (menuMetro) {                                   // panells.js: un sol panell obert alhora
    panel.classList.add("panel");
    bRuta.dataset.panel = "im-panel";
    panel.querySelector(".im-tanca").classList.add("panel-tanca");
  } else {
    bRuta.onclick = () => obri(panel.hidden);
    panel.querySelector(".im-tanca").onclick = () => obri(false);
  }
  function obri(si) {
    if (panel.hidden === !si) return;
    if (menuMetro && bRuta.onclick) {
      bRuta.click();                                 // que ho faça panells.js (tanca els altres)
    } else {
      panel.hidden = !si;
      bRuta.classList.toggle("on", !!si);
    }
  }

  /* ---------- punts A/B ---------- */
  const capa = L.layerGroup().addTo(map);            // traçat de l'alternativa triada
  let orig = null, dest = null;
  let resposta = null, altSel = 0;
  const marca = (txt, col) => L.divIcon({ className: "", iconSize: [26, 26], iconAnchor: [13, 13],
    html: `<div class="im-marca" style="background:${col}">${txt}</div>` });

  function posa(quin, ll, calc = true) {
    let m = quin === "A" ? orig : dest;
    if (!m) {
      m = L.marker(ll, { icon: marca(quin, quin === "A" ? "#34d399" : "#f87171"),
        draggable: true, zIndexOffset: 2000, autoPan: true }).addTo(map);
      m.on("dragend", () => { pintaPunts(); calcula(); });
      if (quin === "A") orig = m; else dest = m;
    } else {
      m.setLatLng(ll);
    }
    pintaPunts();
    if (calc) calcula();
  }
  function pintaPunts() {
    for (const [m, id] of [[orig, "im-pA"], [dest, "im-pB"]]) {
      const el = $(id);
      el.classList.toggle("ok", !!m);
      el.textContent = m ? fmtLL(m.getLatLng()) : (id === "im-pA" ? "toca el mapa" : orig ? "toca el mapa" : "—");
    }
    guardaEstat();
  }
  function esborra() {
    for (const m of [orig, dest]) if (m) map.removeLayer(m);
    orig = dest = null; resposta = null; capa.clearLayers();
    $("im-res").innerHTML = "";
    pintaPunts();
  }

  // amb el panell obert: 1r toc = origen, 2n = destí; després es mouen arrossegant
  map.on("click", (e) => {
    if (panel.hidden) return;
    if (!orig) posa("A", e.latlng);
    else if (!dest) posa("B", e.latlng);
  });
  // pulsació llarga (mòbil) o clic dret: sempre disponible
  map.on("contextmenu", (e) => {
    const div = document.createElement("div");
    div.className = "im-ctx";
    div.innerHTML = `<button data-q="A">Ruta des d'ací</button><button data-q="B">Ruta fins ací</button>`;
    const pop = L.popup({ closeButton: false, minWidth: 140 }).setLatLng(e.latlng).setContent(div).openOn(map);
    div.querySelectorAll("button").forEach((b) => b.onclick = () => {
      map.closePopup(pop);
      obri(true);
      posa(b.dataset.q, e.latlng);
    });
  });

  /* ---------- càlcul ---------- */
  let peticio = 0;
  async function calcula() {
    if (!orig || !dest) { capa.clearLayers(); return; }
    const n = ++peticio;
    $("im-res").innerHTML = "<p class='im-det'>calculant…</p>";
    try {
      const r = await fetch(`api/intermodal?o=${fmtLL(orig.getLatLng())}&d=${fmtLL(dest.getLatLng())}&_=${Date.now()}`,
        { cache: "no-store" });
      const j = await r.json();
      if (n !== peticio) return;                     // ja n'hi ha una de més nova
      if (!r.ok) throw new Error(j.error || r.status);
      resposta = j; altSel = 0;
      pintaRes(true);
    } catch (e) {
      if (n !== peticio) return;
      resposta = null; capa.clearLayers();
      $("im-res").innerHTML = `<p class="im-err">No s'ha pogut calcular la ruta (${esc(e.message || e)}).</p>`;
    }
  }

  const ICO = { peu: "🚶", bus: "🚌", metro: "🚇", tram: "🚊" };
  const TIPUS = { combinada: "metro + bus", metro: "només metro", bus: "només bus" };
  const fmtMin = (m) => m < 1 ? "<1 min" : m < 60 ? `${Math.round(m)} min` : `${Math.floor(m / 60)} h ${Math.round(m % 60)} min`;
  const hora = (min) => new Date(Date.now() + min * 60000).toLocaleTimeString("ca-ES", { hour: "2-digit", minute: "2-digit" });
  const xip = (l) => `<span class="im-lin" style="background:${esc(l.color)}">${esc(l.linia)}</span>`;

  function filaTram(l, t0) {                        // t0: minuts des d'ara en què comença el tram
    if (l.tipus === "peu") {
      return `<div class="im-tram"><span class="im-ico">${ICO.peu}</span>
        <span>A peu ${l.a === "Destí" ? "fins al <b>destí</b>" : `fins a <b>${esc(l.a)}</b>`} <span class="im-det">· ${l.metres} m</span></span>
        <span class="im-t">${fmtMin(l.min)}</span></div>`;
    }
    let esp;
    if (l.tipus === "bus") {
      esp = `espera estimada ~${Math.round(l.espera_min)} min (EMT no publica arribades)` +
        (l.busos_en_servei ? ` · ${l.busos_en_servei} ${l.busos_en_servei === 1 ? "bus" : "busos"} de la línia ara` : "");
    } else if (l.espera_viva) {
      esp = `<span class="im-viu">● tren de les ${hora(t0 + l.espera_min)}</span>` +
        (l.cap_a ? ` cap a ${esc(l.cap_a)}` : "") +
        (l.espera_min >= 1 ? `, ${Math.round(l.espera_min)} min d'espera` : ", just quan arribes") + " (panells de FGV)";
    } else {
      esp = `espera estimada ~${Math.round(l.espera_min)} min` + (l.tipus === "tram" ? " (tramvia sense temps real)" : "");
    }
    const par = l.parades ? ` · ${l.parades} ${l.parades === 1 ? "parada" : "parades"}` : "";
    return `<div class="im-tram"><span class="im-ico">${ICO[l.tipus] || "•"}</span>
      <span>${xip(l)}<b>${esc(l.de)}</b> → <b>${esc(l.a)}</b>${par}<br><span class="im-det">${esp}</span></span>
      <span class="im-t">${fmtMin(l.espera_min + l.min)}</span></div>`;
  }

  function files(trams) {
    let t = 0;
    return trams.map((l) => { const f = filaTram(l, t); t += (l.espera_min || 0) + l.min; return f; }).join("");
  }

  function pintaRes(enquadra) {
    const res = $("im-res");
    const alts = (resposta && resposta.alternatives) || [];
    if (!alts.length) {
      res.innerHTML = "<p class='im-err'>No hi ha cap ruta amb metro o bus entre estos dos punts ara mateix.</p>";
      capa.clearLayers();
      return;
    }
    res.innerHTML = alts.map((a, i) => `
      <div class="im-alt${i === altSel ? " on" : ""}" data-i="${i}">
        <div class="im-alt-cap"><span class="im-min">${fmtMin(a.min)}</span>
          <span class="im-tipus">${esc(TIPUS[a.nom] || a.nom)} · arribada ~${hora(a.min)} · ${a.peu_m} m a peu</span></div>
        ${i === altSel ? files(a.trams) : `<div class="im-det">${a.trams.filter((l) => l.tipus !== "peu").map(xip).join("")}</div>`}
      </div>`).join("") +
      `<div class="im-peu">Temps aproximats: bus a ~13 km/h pel seu recorregut i metro per la via.
        ${(resposta.fora_de_servei || []).length ? "Ara no circulen: " + resposta.fora_de_servei.map(esc).join(", ") + "." : ""}</div>`;
    res.querySelectorAll(".im-alt").forEach((d) => d.onclick = () => {
      const i = Number(d.dataset.i);
      if (i === altSel) return;
      altSel = i; pintaRes(true);
    });
    dibuixa(alts[altSel], enquadra);
    guardaEstat();
  }

  function dibuixa(a, enquadra) {
    capa.clearLayers();
    const punts = [];
    for (const l of a.trams) {
      const geo = l.geo || [l.des_de, l.fins];
      punts.push(...geo);
      if (l.tipus === "peu") {
        L.polyline(geo, { color: "#e8e8ef", weight: 3, opacity: 0.85, dashArray: "2 7", lineCap: "round",
          interactive: false }).addTo(capa);
        continue;
      }
      L.polyline(geo, { color: "#000", weight: 9, opacity: 0.55, interactive: false }).addTo(capa);
      L.polyline(geo, { color: l.color, weight: 5.5, opacity: 1, lineJoin: "round" })
        .bindTooltip(`${l.linia}: ${l.de} → ${l.a}`, { sticky: true }).addTo(capa);
      for (const [p, nom, txt] of [[l.des_de, l.de, "puja"], [l.fins, l.a, "baixa"]]) {
        L.circleMarker(p, { radius: 6, color: "#fff", weight: 2.5, fillColor: l.color, fillOpacity: 1 })
          .bindTooltip(`${txt}: ${nom} (${l.linia})`, { direction: "top" }).addTo(capa);
      }
    }
    if (enquadra && punts.length) {
      const b = L.latLngBounds(punts);
      if (orig) b.extend(orig.getLatLng());
      if (dest) b.extend(dest.getLatLng());
      const ample = window.innerWidth > 520;
      // el panell tapa la dreta (escriptori) o la part de dalt (mòbil)
      const dalt = ample || panel.hidden ? 60 : Math.min(panel.offsetHeight + 70, window.innerHeight * 0.6);
      map.fitBounds(b, { paddingTopLeft: [30, dalt], paddingBottomRight: [ample && !panel.hidden ? 400 : 30, 40], maxZoom: 16 });
    }
  }

  /* ---------- estat de la pestanya ---------- */
  function guardaEstat() {
    try {
      sessionStorage.setItem("im_estat", JSON.stringify(orig && dest
        ? { o: fmtLL(orig.getLatLng()), d: fmtLL(dest.getLatLng()), alt: altSel } : null));
    } catch (e) { /* sense sessionStorage */ }
  }
  const llLL = (t) => { const p = (t || "").split(",").map(Number); return p.length === 2 && p.every(isFinite) ? L.latLng(p) : null; };

  /* ---------- botons del panell ---------- */
  panel.querySelector("#im-esb").onclick = esborra;
  panel.querySelector("#im-inv").onclick = () => {
    if (!orig || !dest) return;
    const a = orig.getLatLng(), b = dest.getLatLng();
    orig.setLatLng(b); dest.setLatLng(a);
    pintaPunts(); calcula();
  };
  panel.querySelector("#im-altra").onclick = vesAltra;
  panel.querySelector("#im-ubic").onclick = () => {
    if (!navigator.geolocation) { $("im-res").innerHTML = "<p class='im-err'>Este navegador no dona la ubicació.</p>"; return; }
    navigator.geolocation.getCurrentPosition(
      (p) => { posa("A", L.latLng(p.coords.latitude, p.coords.longitude)); },
      () => { $("im-res").innerHTML = "<p class='im-err'>Sense permís d'ubicació (o la web no va per HTTPS).</p>"; },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 });
  };

  /* ---------- en arrancar: ve de l'altra web (#v=…&o=…&d=…) o F5 ---------- */
  function inici(nomesHash) {
    let o = null, d = null;
    const h = new URLSearchParams(location.hash.replace(/^#/, ""));
    if (h.has("v")) {
      const v = h.get("v").split(",").map(Number);
      if (v.length === 3 && v.every(isFinite)) map.setView([v[0], v[1]], v[2]);
      o = llLL(h.get("o")); d = llLL(h.get("d"));
      try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* file:// */ }
    } else if (!nomesHash) {
      try {
        const s = JSON.parse(sessionStorage.getItem("im_estat"));
        if (s) { o = llLL(s.o); d = llLL(s.d); }
      } catch (e) { /* res guardat */ }
    }
    if (o && d) {
      posa("A", o, false);
      posa("B", d, false);
      obri(true);
      calcula();
    }
  }
  inici(false);
  window.addEventListener("hashchange", () => inici(true));   // enllaç #v=… dins de la mateixa pàgina
})();
