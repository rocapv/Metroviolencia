/* Metroviolencia · panells del menú: calculadora de rutes i retards/cancel·lacions.
   Usa els globals d'app.js (map, porNomGlobal, colores, subTracat, pintarTrenes…). */
(function () {
  const RETARD_MIN = 180;          // s: a partir d'ací un tren "va tard"
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const chip = (l) => l
    ? `<span class="pop-linea" style="background:#${colores[l] || "888"}">${NOMBRES_LINEA[l] || "L" + l}</span>`
    : "";
  const hm = (t) => t ? new Date(t * 1000).toLocaleTimeString("ca-ES", { hour: "2-digit", minute: "2-digit" }) : "—";
  const fmtRet = (s) => s == null ? "" : Math.abs(s) < 60 ? "a l'hora"
    : (s > 0 ? "+" : "−") + Math.round(Math.abs(s) / 60) + " min";
  const guarda = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };
  const llig = (k) => { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } };

  /* ---------------- menú: un sol panell obert ---------------- */
  function obri(id) {
    document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.id !== id || !p.hidden; });
    document.querySelectorAll("#menu button").forEach((b) =>
      b.classList.toggle("on", !$(b.dataset.panel).hidden));
    if (id === "panel-retards" && !$(id).hidden) carregaRetards();
  }

  /* ---------------- RUTA ---------------- */
  let ruta = null;                 // resposta d'/api/ruta
  let altSel = 0;
  let rutaTs = 0;                  // hora (client) de l'última resposta
  const rutaLayer = L.layerGroup().addTo(map);

  function idDe(nom) { return (porNomGlobal[nom.trim()] || {}).id; }

  async function calcula(silenci) {
    const o = idDe($("ruta-orig").value), d = idDe($("ruta-dest").value);
    if (!o || !d) { if (!silenci) $("ruta-res").innerHTML = "<p class='ruta-err'>Tria dues estacions de la llista.</p>"; return; }
    if (o === d) { $("ruta-res").innerHTML = "<p class='ruta-err'>L'origen i el destí són la mateixa estació.</p>"; return; }
    if (!silenci) $("ruta-res").innerHTML = "<p class='ruta-muted'>calculant…</p>";
    try {
      const r = await fetch(`api/ruta?origen=${o}&destino=${d}&_=${Date.now()}`, { cache: "no-store" });
      const j = await r.json();
      if (j.error) throw new Error(j.error);
      const nova = !ruta || ruta.origen !== j.origen || ruta.destino !== j.destino;
      ruta = j; rutaTs = Date.now();
      if (nova) altSel = 0;
      altSel = Math.min(altSel, Math.max(0, ruta.alternativas.length - 1));
      guarda("mv_ruta", { o: $("ruta-orig").value, d: $("ruta-dest").value });
      pinta();
      dibuixa(nova);
      aplicaFiltre();
    } catch (e) {
      if (!silenci) $("ruta-res").innerHTML = `<p class='ruta-err'>No s'ha pogut calcular: ${esc(e.message)}</p>`;
    }
  }

  // desfase del tren en directe respecte a la seua hora programada
  function desfase(t) {
    if (t.desfase_s == null) return "";
    const d = t.desfase_s, min = Math.round(Math.abs(d) / 60);
    const [cls, txt] = Math.abs(d) < 90 ? ["hora", "a l'hora"]
      : d > 0 ? [d >= 180 ? "tard" : "poc", `+${min} min`] : ["avanc", `−${min} min`];
    return `<span class="ruta-desf ${cls}" title="programat ${esc(t.hora_prog)}">${esc(t.hora_prog)} · ${txt}</span>`;
  }

  function alt() { return ruta && ruta.alternativas[altSel]; }

  function pinta() {
    const a = alt();
    if (!ruta.alternativas.length || !a) {
      $("ruta-res").innerHTML = "<p class='ruta-err'>El planificador no troba cap ruta ara mateix.</p>";
      return;
    }
    const passat = Math.max(0, (Date.now() - rutaTs) / 1000);
    let h = "";
    if (ruta.ventana === "hoy") {
      h += "<p class='ruta-avis'>No hi ha trens en les pròximes 2 hores; mire l'horari de la resta del dia.</p>";
    } else if (ruta.ventana === "manana") {
      const p0 = (a.pasos[0] || {}).salidas || [];
      h += `<p class='ruta-avis'>Hui ja no hi ha servei. Primer tren demà a les <b>${esc(p0[0] || "?")}</b>.</p>`;
    }
    if (ruta.alternativas.length > 1) {
      h += "<div class='ruta-alts'>" + ruta.alternativas.map((x, i) =>
        `<button data-i="${i}" class="${i === altSel ? "on" : ""}">${x.duracion_min ?? "?"} min · ${x.transbordos} transb.</button>`
      ).join("") + "</div>";
    } else {
      h += `<p class="ruta-muted">${a.duracion_min ?? "?"} min · ${a.transbordos ? a.transbordos + " transbord" + (a.transbordos > 1 ? "s" : "") : "directe"}</p>`;
    }
    a.pasos.forEach((p, i) => {
      const ult = i === a.pasos.length - 1;
      const trens = (p.trenes || []).slice(0, 3).map((t) => {
        const eta = t.eta_subida == null ? null : Math.max(0, t.eta_subida - passat);
        return `<div class="ruta-tren">${chip(t.linea)}<span class="arr-dest">→ ${esc(t.destino)}</span>
          ${desfase(t)}
          <span class="arr-eta ${eta != null && eta <= 60 ? "imminent" : ""}">${eta == null ? "de camí" : fmtEta(eta)}</span></div>`;
      }).join("");
      const prox = !trens && p.proxima_prog
        ? `<div class="ruta-prox">Pròxim programat: <b>${esc(p.proxima_prog)}</b>${ruta.ventana === "manana" ? " (demà)" : ""}</div>` : "";
      const sal = p.salidas || [];
      const desDe = Math.max(0, sal.indexOf(p.proxima_prog));   // no mostrar hores passades
      const horari = sal.slice(desDe, desDe + 4).join(" · ");
      h += `<div class="ruta-pas">
        <div class="ruta-pas-cap"><span class="ruta-n">${i + 1}</span>Puja a <b>${esc(p.de)}</b> ${(p.lineas || []).map(chip).join("")}</div>
        <div class="ruta-dir">direcció ${esc((p.destinos || []).join(" / ") || "—")}</div>
        ${trens || "<div class='ruta-muted'>cap tren en directe per a este tram ara mateix</div>"}
        ${prox}
        ${(p.sin_tren || []).length ? `<div class="ruta-canc">⚠ Programat ${p.sin_tren.map(esc).join(", ")} però no apareix en directe: possible cancel·lació.</div>` : ""}
        ${horari ? `<div class="ruta-horari">Horari: ${horari}</div>` : ""}
        <div class="ruta-baixa">${ult ? "Arribes a" : "Transbord a"} <b>${esc(p.a)}</b></div>
      </div>`;
    });
    $("ruta-res").innerHTML = h;
    $("ruta-res").querySelectorAll(".ruta-alts button").forEach((b) => b.onclick = () => {
      altSel = Number(b.dataset.i); pinta(); dibuixa(true); aplicaFiltre();
    });
  }

  function dibuixa(enquadra) {
    rutaLayer.clearLayers();
    const a = alt();
    if (!a) return;
    const punts = [];
    a.pasos.forEach((p, i) => {
      const l = (p.lineas || [])[0];
      const tros = l ? subTracat(l, p.de, p.a, porNomGlobal) : null;
      if (tros) {
        L.polyline(tros, { color: "#fff", weight: 11, opacity: 0.22, interactive: false }).addTo(rutaLayer);
        L.polyline(tros, { color: "#" + (colores[l] || "888"), weight: 6, opacity: 1, interactive: false }).addTo(rutaLayer);
        punts.push(...tros);
      }
      const e = porNomGlobal[p.de];
      if (e) {
        L.circleMarker([e.lat, e.lng], { radius: 8, color: "#fff", weight: 2.5,
          fillColor: i === 0 ? "#35d07f" : "#f5a623", fillOpacity: 1 })
          .bindTooltip(i === 0 ? `Origen: ${p.de}` : `Transbord: ${p.de}`).addTo(rutaLayer);
      }
    });
    const fi = porNomGlobal[a.pasos[a.pasos.length - 1].a];
    if (fi) {
      L.circleMarker([fi.lat, fi.lng], { radius: 8, color: "#fff", weight: 2.5,
        fillColor: "#c026d3", fillOpacity: 1 }).bindTooltip(`Destí: ${fi.nombre}`).addTo(rutaLayer);
      punts.push([fi.lat, fi.lng]);
    }
    if (enquadra && punts.length) map.fitBounds(L.latLngBounds(punts), { padding: [60, 60], maxZoom: 15 });
  }

  function aplicaFiltre() {
    const a = alt();
    if (a && $("ruta-nomes").checked) {
      rutaFiltre = new Set(a.pasos.flatMap((p) => (p.trenes || []).map((t) => t.id)));
    } else {
      rutaFiltre = null;
    }
    pintarTrenes(ultimoTrenes);
  }

  function esborra() {
    ruta = null; rutaFiltre = null; rutaLayer.clearLayers();
    $("ruta-orig").value = ""; $("ruta-dest").value = ""; $("ruta-res").innerHTML = "";
    guarda("mv_ruta", null);
    pintarTrenes(ultimoTrenes);
  }

  window.rutaFixa = function (quin, nom) {
    $(quin === "orig" ? "ruta-orig" : "ruta-dest").value = nom;
    if ($("panel-ruta").hidden) obri("panel-ruta");
    if ($("ruta-orig").value && $("ruta-dest").value) calcula();
  };

  /* ---------------- RETARDS ---------------- */
  let registre = null;
  let filtreRet = "tard";

  async function carregaRetards() {
    try {
      const r = await fetch("api/registro?_=" + Date.now(), { cache: "no-store" });
      registre = await r.json();
      pintaRetards();
    } catch (e) {
      $("ret-llista").innerHTML = "<p class='ruta-err'>Sense connexió amb el registre.</p>";
    }
  }

  function pintaRetards() {
    const fs = registre.salidas || [];
    const fetes = fs.filter((f) => f.estado === "realizada");
    const tard = fs.filter((f) => (f.estado === "realizada" || f.estado === "prevista") && f.retraso_s >= RETARD_MIN);
    const canc = fs.filter((f) => f.estado === "cancelada");
    const mitja = fetes.length ? fetes.reduce((a, f) => a + f.retraso_s, 0) / fetes.length : null;
    const t = registre.tipo_dia || {};
    $("ret-tipus").innerHTML = [["metro", "Metro"], ["tranvia", "Tramvia"], ["l10", "L10"]].map(([k, n]) => {
      if (!t[k]) return "";
      const [c, noct] = t[k].split("+");
      return `<span class="tipus-chip c-${c}">${n}: ${c}${noct ? " · nocturn" : ""}</span>`;
    }).join("");
    $("ret-kpis").innerHTML = `
      <div><b>${fetes.length}</b><span>eixides fetes</span></div>
      <div class="k-tard"><b>${tard.length}</b><span>van tard (≥${RETARD_MIN / 60} min)</span></div>
      <div class="k-canc"><b>${canc.length}</b><span>cancel·lats</span></div>
      <div><b>${mitja == null ? "—" : fmtRet(mitja)}</b><span>desfase mitjà</span></div>`;
    document.querySelectorAll(".ret-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.f === filtreRet));
    let llista = filtreRet === "tard" ? tard : filtreRet === "cancelada" ? canc
      : fs.filter((f) => ["realizada", "prevista", "cancelada"].includes(f.estado));
    llista = llista.slice().sort((a, b) => (b.ts_prog || b.real) - (a.ts_prog || a.real)).slice(0, 150);
    $("ret-llista").innerHTML = llista.length ? llista.map((f) => {
      const cls = f.estado === "cancelada" ? "canc" : f.retraso_s >= RETARD_MIN ? "tard" : "ok";
      const dreta = f.estado === "cancelada" ? "cancel·lat"
        : `${hm(f.real)} <b>${fmtRet(f.retraso_s)}</b>${f.estado === "prevista" ? " (previst)" : ""}`;
      return `<div class="ret-fila ${cls}">
        <span class="ret-hora">${esc(f.hora || "—")}</span>
        ${chip(f.linea_real || f.linea)}
        <span class="ret-txt">${esc(f.cabecera)} → ${esc(f.destino_real || f.destino || "?")}</span>
        <span class="ret-est">${dreta}</span></div>`;
    }).join("") : "<p class='ruta-muted'>Res a mostrar ara mateix.</p>";
  }

  /* ---------------- enganxall amb app.js ---------------- */
  window.iniciaPanells = function () {
    document.querySelectorAll("#menu button").forEach((b) => b.onclick = () => obri(b.dataset.panel));
    document.querySelectorAll(".panel-tanca").forEach((b) => b.onclick = () => obri(null));
    $("ruta-est").innerHTML = Object.keys(porNomGlobal).sort((a, b) => a.localeCompare(b, "ca"))
      .map((n) => `<option value="${esc(n)}">`).join("");
    $("ruta-calc").onclick = () => calcula();
    $("ruta-esborra").onclick = esborra;
    $("ruta-swap").onclick = () => {
      const o = $("ruta-orig").value; $("ruta-orig").value = $("ruta-dest").value; $("ruta-dest").value = o;
      if ($("ruta-orig").value && $("ruta-dest").value) calcula();
    };
    $("ruta-nomes").onchange = aplicaFiltre;
    document.querySelectorAll(".ret-tabs button").forEach((b) => b.onclick = () => {
      filtreRet = b.dataset.f; if (registre) pintaRetards();
    });
    const g = llig("mv_ruta");
    if (g && g.o && g.d) { $("ruta-orig").value = g.o; $("ruta-dest").value = g.d; calcula(true); }
  };

  window.refrescaPanells = function () {
    if (ruta) calcula(true);                         // trens de la ruta al dia
    if (!$("panel-retards").hidden) carregaRetards();
  };

  // compte enrere viu dels trens de la ruta
  setInterval(() => { if (ruta && !$("panel-ruta").hidden) pinta(); }, 5000);
})();
