/* Metroviolencia · xat de la xarxa: missatges des del tren en què vas (triat per
   proximitat, calculada en ESTE dispositiu: la posició no ix mai del mòbil) o
   des d'una estació. Els llig tothom. Usa els globals d'app.js (map, marcadors,
   porNomGlobal, colores, NOMBRES_LINEA…). */
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const RADI_CANDIDAT = 600;    // m: trens que es proposen com a candidats
  const RADI_SEGUR = 150;       // m: si només n'hi ha un tan prop, es proposa el primer
  const BOMBOLLA_MS = 5000;     // ms que dura la bombolla d'un missatge nou
  const FEED_N = 40;

  let alias = "";
  // per pestanya (sessionStorage), com la ruta: un altre client del mateix navegador no hereta l'àlies
  try { alias = sessionStorage.getItem("mv_alias") || ""; } catch (e) {}
  let lloc = null;              // {tipus:'tren'|'estacio', id, nom}
  let ultimId = 0;              // últim id de missatge rebut
  let primera = true;           // 1a càrrega del feed: sense bombolles
  let geoWatch = null, posicio = null, posicioTs = 0;
  const bombolles = [];         // {tt, marker, fins}
  let carregant = false;

  const hm = (ts) => new Date(ts * 1000).toLocaleTimeString("ca-ES", { hour: "2-digit", minute: "2-digit" });
  const chip = (l) => l ? `<span class="pop-linea" style="background:#${colores[l] || "888"}">${NOMBRES_LINEA[l] || "L" + l}</span>` : "";
  const metres = (a, b) => Math.hypot((a.lat - b.lat) * 111000, (a.lng - b.lng) * 85000);
  const nomTren = (t) => `${NOMBRES_LINEA[t.linea] || "L" + t.linea} → ${t.destino}`;

  /* ---------------- on soc ---------------- */
  function fixaLloc(l) {
    lloc = l;
    pintaLloc();
    const text = $("xat-text");
    if (text && !$("panel-xat").hidden) text.focus();
  }
  function pintaLloc() {
    const el = $("xat-lloc");
    if (!el) return;
    if (!lloc) { el.innerHTML = "<span class='xat-muted'>Encara no has dit on ets: tria el tren en què vas o una estació.</span>"; return; }
    const dist = (lloc.tipus === "tren" && posicio) ? distTren(lloc.id) : null;
    el.innerHTML = (lloc.tipus === "tren" ? "Vas en el tren " : "Escrius des de l'estació ") +
      `<b>${esc(lloc.nom)}</b>` +
      (dist != null ? ` <span class="xat-muted">(a ${Math.round(dist)} m)</span>` : "") +
      ` <button id="xat-ix" class="xat-mini" title="Deixa este lloc">×</button>`;
    $("xat-ix").onclick = () => { lloc = null; pintaLloc(); };
  }
  function distTren(id) {
    const m = window.marcadorTren && window.marcadorTren(id);
    return m && posicio ? metres(posicio, m.getLatLng()) : null;
  }
  // des d'un popup (tren o estació)
  window.xatDesDe = function (tipus, id, nom) {
    fixaLloc({ tipus, id: Number(id), nom });
    if (window.obriPanell) window.obriPanell("panel-xat", true);
  };

  /* ---------------- ubicació: tria el tren per proximitat (en local) ---------------- */
  function novaPosicio(lat, lng) {
    posicio = { lat, lng };
    posicioTs = Date.now();
    avis("");
    pintaCandidats();
    if (lloc && lloc.tipus === "tren") pintaLloc();
  }
  // La APK injecta `MetroviolenciaNatiu`: la posició la dóna Android i arriba per
  // window.mvPosicio. Cal perquè la Geolocation API de la WebView només funciona en
  // orígens HTTPS i dins de la LAN la web va per http.
  window.mvPosicio = (lat, lng, acc) => novaPosicio(lat, lng);
  window.mvPosicioError = (msg) => { avis(msg || "No s'ha pogut obtindre la posició."); geoWatch = null; };
  function activaUbicacio() {
    if (window.MetroviolenciaNatiu && window.MetroviolenciaNatiu.demanaUbicacio) {
      if (geoWatch != null) { pintaCandidats(); return; }
      geoWatch = "natiu";
      avis("Buscant la teua posició…", true);
      window.MetroviolenciaNatiu.demanaUbicacio();
      return;
    }
    if (!navigator.geolocation) { avis("Este navegador no té geolocalització."); return; }
    if (!window.isSecureContext) { avis("La ubicació només funciona per HTTPS (o des de l'app)."); return; }
    if (geoWatch != null) { pintaCandidats(); return; }
    avis("Buscant la teua posició…", true);
    geoWatch = navigator.geolocation.watchPosition((p) => {
      novaPosicio(p.coords.latitude, p.coords.longitude);
    }, (err) => {
      avis(err.code === 1 ? "Has denegat la ubicació. Pots triar una estació a mà." : "No s'ha pogut obtindre la posició.");
      navigator.geolocation.clearWatch(geoWatch); geoWatch = null;
    }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
  }
  function candidats() {
    if (!posicio || !window.trensVius) return [];
    return window.trensVius()
      .map(({ t, ll }) => ({ t, dist: metres(posicio, ll) }))
      .filter((c) => c.dist <= RADI_CANDIDAT && c.t.id != null)
      .sort((a, b) => a.dist - b.dist);
  }
  function pintaCandidats() {
    const el = $("xat-candidats");
    if (!el) return;
    const cs = candidats();
    if (!posicio) { el.innerHTML = ""; return; }
    if (!cs.length) { el.innerHTML = "<div class='xat-muted'>Cap tren a menys de 600 m de tu ara mateix.</div>"; return; }
    el.innerHTML = "<div class='xat-muted'>Trens prop de tu (tria el teu):</div>" + cs.slice(0, 6).map((c, i) => {
      const segur = i === 0 && c.dist <= RADI_SEGUR && (cs.length === 1 || cs[1].dist > RADI_SEGUR);
      return `<button class="xat-cand${segur ? " segur" : ""}" data-id="${c.t.id}">` +
        `${chip(c.t.linea)} → ${esc(c.t.destino)} <span class="xat-muted">${Math.round(c.dist)} m</span></button>`;
    }).join("");
    el.querySelectorAll(".xat-cand").forEach((b) => b.onclick = () => {
      const c = cs.find((x) => String(x.t.id) === b.dataset.id);
      if (c) { fixaLloc({ tipus: "tren", id: c.t.id, nom: nomTren(c.t) }); el.innerHTML = ""; }
    });
  }

  /* ---------------- enviar ---------------- */
  function avis(txt, suau) {
    const el = $("xat-avis");
    if (el) { el.textContent = txt; el.classList.toggle("suau", !!suau); }
  }
  async function envia() {
    const text = ($("xat-text").value || "").trim();
    if (!lloc) { avis("Primer digues des d'on escrius."); return; }
    if (!text) { avis("El missatge està buit."); return; }
    alias = ($("xat-alias").value || "").trim().slice(0, 24);
    try { sessionStorage.setItem("mv_alias", alias); } catch (e) {}
    $("xat-envia").disabled = true;
    try {
      const r = await fetch("api/xat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tipus: lloc.tipus, lloc: lloc.id, alias, text }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      $("xat-text").value = "";
      avis("Enviat.", true);
      if (window.xatNovetats) window.xatNovetats(j.id);
    } catch (e) {
      avis("No s'ha enviat: " + e.message);
    } finally {
      $("xat-envia").disabled = false;
    }
  }

  /* ---------------- feed ---------------- */
  function htmlMissatge(m, ambLloc) {
    const qui = m.alias ? `<b>${esc(m.alias)}</b>` : "<span class='xat-muted'>algú</span>";
    const on = ambLloc ? ` <span class="xat-on-chip">${m.tipus === "tren" ? chip(m.linea) + " " : "⌂ "}${esc(m.nom_lloc)}</span>` : "";
    return `<div class="xat-msg"><span class="xat-hora">${hm(m.ts)}</span> ${qui}${on}<div class="xat-txt">${esc(m.text)}</div></div>`;
  }
  async function carregaFeed(desDe) {
    if (carregant) return;
    carregant = true;
    try {
      const j = await (await fetch(`api/xat?des_de=${desDe}&n=${FEED_N}&_=${Date.now()}`, { cache: "no-store" })).json();
      const ms = j.missatges || [];
      if (ms.length) {
        const feed = $("xat-feed");
        if (feed) {
          if (desDe === 0) feed.innerHTML = "";
          for (const m of ms) feed.insertAdjacentHTML("afterbegin", htmlMissatge(m, true));
          while (feed.children.length > FEED_N) feed.lastElementChild.remove();
        }
        if (!primera) for (const m of ms) bombolla(m);
        ultimId = Math.max(ultimId, ...ms.map((m) => m.id));
      }
      ultimId = Math.max(ultimId, j.ultim_id || 0);
      primera = false;
    } catch (e) { /* sense xarxa: ja es reintentarà */ } finally { carregant = false; }
  }
  // la crida vigilaServidor (app.js) amb l'últim id que coneix el servidor
  window.xatNovetats = function (xatId) {
    if (xatId > ultimId) carregaFeed(ultimId);
  };

  /* ---------------- missatges d'un lloc, dins dels popups ---------------- */
  window.xatOmpliPopup = async function (root) {
    const el = root && root.querySelector(".pop-xat");
    if (!el || el.dataset.ple) return;
    el.dataset.ple = "1";
    const { tipus, lloc: id, nom } = el.dataset;
    el.innerHTML = `<div class="pop-arr-tit">Xat</div><div class="pop-xat-llista xat-muted">carregant…</div>` +
      `<button class="pop-xat-btn">${tipus === "tren" ? "Escriu des d'este tren" : "Escriu des d'ací"}</button>`;
    el.querySelector(".pop-xat-btn").onclick = () => { map.closePopup(); window.xatDesDe(tipus, id, nom); };
    try {
      const j = await (await fetch(`api/xat?tipus=${tipus}&lloc=${id}&n=5&_=${Date.now()}`, { cache: "no-store" })).json();
      const ms = j.missatges || [];
      el.querySelector(".pop-xat-llista").innerHTML = ms.length
        ? ms.map((m) => htmlMissatge(m, false)).join("")
        : "<span class='xat-muted'>encara no hi ha missatges</span>";
    } catch (e) { el.querySelector(".pop-xat-llista").textContent = "sense connexió"; }
  };

  /* ---------------- bombolles: un missatge acabat d'arribar, 5 s damunt del tren/estació ---------------- */
  function bombolla(m) {
    const marker = m.tipus === "tren"
      ? (window.marcadorTren && window.marcadorTren(m.lloc))
      : (window.marcadorEstacio && window.marcadorEstacio(m.lloc));
    if (!marker) return;
    const tt = L.tooltip({ permanent: true, direction: "top", offset: [0, -14], className: "xat-bombolla", interactive: false })
      .setLatLng(marker.getLatLng())
      .setContent(`${m.alias ? `<b>${esc(m.alias)}</b>: ` : ""}${esc(m.text)}`)
      .addTo(map);
    bombolles.push({ tt, marker, fins: Date.now() + BOMBOLLA_MS });
  }
  setInterval(() => {
    const ara = Date.now();
    for (let i = bombolles.length - 1; i >= 0; i--) {
      const b = bombolles[i];
      if (ara > b.fins) { map.removeLayer(b.tt); bombolles.splice(i, 1); }
      else b.tt.setLatLng(b.marker.getLatLng());   // segueix el tren mentre es mou
    }
  }, 250);

  /* ---------------- inici ---------------- */
  window.iniciaXat = function () {
    if ($("xat-alias")) $("xat-alias").value = alias;
    $("xat-ubicacio").onclick = activaUbicacio;
    $("xat-envia").onclick = envia;
    $("xat-text").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); envia(); } });
    $("xat-est").addEventListener("change", () => {
      const e = porNomGlobal[$("xat-est").value.trim()];
      if (e) { fixaLloc({ tipus: "estacio", id: e.id, nom: e.nombre }); $("xat-est").value = ""; }
    });
    pintaLloc();
    carregaFeed(0);
  };
})();
