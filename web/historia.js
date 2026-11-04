/* Metro · recorregut d'un tren que ja no circula (metro.pr1.es/1234).
   Reprodueix, amb el rellotge accelerat, la posició que tenia el tren a cada
   pasada del poller. El traç es pinta amb el color del retard que portava (a
   l'hora / +1,5 min / +3 min / avançat). Si va desaparéixer a mitjan trajecte,
   l'animació s'hi para i la marca diu què va passar (final.veredicte de
   backend/finals.py): cancel·lat (roja), el vam perdre però va seguir
   (taronja, amb l'id que el continua), retirat, sense dades o pendent.
   Les dades: /api/historia_tren. */
(function () {
  const COLOR = { "": "#4ade80", poc: "#f59e0b", tard: "#ef4444", avanc: "#38bdf8", nd: "#8a8a98" };
  const SALT_MAX = 600;     // s: entre dues pasades més separades no s'interpola (forat de dades)
  let h = null;             // el recorregut obert

  const $ = (id) => document.getElementById(id);
  const hora = (ts, seg) => new Date(ts * 1000).toLocaleTimeString("ca-ES", seg
    ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
  const classe = (r) => r == null ? "nd" : r >= 180 ? "tard" : r >= 90 ? "poc" : r <= -90 ? "avanc" : "";
  function txtRetard(r) {
    if (r == null) return "sense hora programada";
    const m = Math.round(Math.abs(r) / 60);
    return Math.abs(r) < 90 ? "a l'hora" : r > 0 ? `+${m} min tard` : `${m} min avançat`;
  }

  // com va acabar un tren perdut (backend/finals.py): marca, color i textos
  function final(j) {
    const f = j.final || { veredicte: "indeterminat" };
    const abans = j.fi.prox && !j.fi.origen ? `, abans d'arribar a ${j.fi.prox}` : j.fi.origen ? ", sense eixir de la capçalera" : "";
    const ests = (f.estacions || []).filter((e) => !e.sense_dades);
    const vist = ests.find((e) => e.pas);
    const canc = ests.find((e) => e.cancel_prog);
    const hh = (ts) => ts ? hora(ts) : "";
    switch (f.veredicte) {
      case "cancellat":
        return { cls: "perdut", ic: "✕", ratlla: true,
          curt: `<b>Cancel·lat</b> · desapareix a les ${hora(j.fi.ts)}${abans}`,
          llarg: `Cancel·lat a les ${hora(j.fi.ts)}${abans}` +
            (canc ? ` · no va passar per ${canc.estacio} (hi tenia un tren programat a les ${canc.cancel_prog})` : ""),
          ajuda: "Cap estació per davant no el va veure passar i al seu horari hi falta un tren" };
      case "continua":
        return { cls: "seguit", ic: "↷", ratlla: false,
          curt: `<b>El vam perdre de vista</b> a les ${hora(j.fi.ts)} · però va seguir`,
          llarg: `El vam perdre de vista a les ${hora(j.fi.ts)}, però va seguir` +
            (vist ? `: ${vist.estacio} el va veure passar a les ${vist.pas}` : ""),
          ajuda: "Les estacions per davant van registrar el seu pas a l'hora prevista: no el va cancel·lar FGV, el va perdre el nostre seguiment",
          seguit: f.seguit_per };
      case "retirat":
        return { cls: "retirat", ic: "⏏", ratlla: true,
          curt: `<b>Retirat</b> a les ${hora(j.fi.ts)}${abans}`,
          llarg: `Retirat a les ${hora(j.fi.ts)}${abans} · ja no tenia cap pas programat per davant`,
          ajuda: "Fi de servei o tren que se'n va a cotxeres" };
      case "sense_dades":
        return { cls: "nd", ic: "?", ratlla: false,
          curt: `<b>Sense dades</b> des de les ${hora(j.fi.ts)}`,
          llarg: `Sense dades des de les ${hora(j.fi.ts)}` + (f.buit ? ` fins a les ${hh(f.buit[1])}` : "") +
            " · no podem saber què li va passar",
          ajuda: "Les lectures de FGV es van interrompre just després" };
      case "pendent":
        return { cls: "pendent", ic: "?", ratlla: true,
          curt: `<b>Desapareix</b> a les ${hora(j.fi.ts)}${abans}`,
          llarg: `Desapareix a les ${hora(j.fi.ts)}${abans}` +
            (f.fins ? ` · cap a les ${hh(f.fins)} sabrem si està cancel·lat` : " · encara no sabem si està cancel·lat"),
          ajuda: "Cal esperar que passe l'hora que tenia a les estacions següents" };
      default:
        return { cls: "perdut", ic: "✕", ratlla: true,
          curt: `<b>Desapareix</b> a les ${hora(j.fi.ts)}${abans}`,
          llarg: `Desapareix a les ${hora(j.fi.ts)}${abans} · sense confirmar si està cancel·lat` +
            (f.motiu ? ` (${f.motiu})` : ""),
          ajuda: "Les dades no permeten dir si el van cancel·lar, si el van retirar o si el vam perdre de vista" };
    }
  }

  function panes() {
    if (!map.getPane("historiaVia")) {
      map.createPane("historiaVia").style.zIndex = 450;   // damunt de les vies, davall dels trens
      map.createPane("historia").style.zIndex = 640;      // damunt dels trens en directe
    }
  }

  // tros de via entre d1 i d2 seguint la geometria de la línia
  function tros(via, d1, d2) {
    const lc = comerciales[via];
    if (!lc || !lc.cum) return [];
    const a = Math.min(d1, d2), b = Math.max(d1, d2);
    let lo = 0, hi = lc.cum.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (lc.cum[m] <= a) lo = m + 1; else hi = m; }
    const pts = [distToLatLng(via, a)];
    for (let k = lo; k < lc.cum.length && lc.cum[k] < b; k++) pts.push(lc.path[k]);
    pts.push(distToLatLng(via, b));
    const out = pts.filter(Boolean);
    return d1 <= d2 ? out : out.reverse();
  }

  // retard que portava a l'instant T: el de l'última estació que havia passat
  function retardA(T) {
    let r = null;
    for (const f of h.passades) { if (f.ts > T) break; r = f.retraso_s; }
    return r;
  }

  // on era a l'instant T
  function posA(T) {
    const P = h.punts;
    if (T <= P[0].ts) return { p: P[0], d: P[0].d, i: 0 };
    let i = P[h.iPos].ts <= T ? h.iPos : 0;
    while (i + 1 < P.length && P[i + 1].ts <= T) i++;
    h.iPos = i;
    const a = P[i], b = P[i + 1];
    if (!b || b.via !== a.via || b.ts - a.ts > SALT_MAX) return { p: a, d: a.d, i };
    return { p: a, d: a.d + (T - a.ts) / (b.ts - a.ts) * (b.d - a.d), i };
  }

  function icona(k) {
    const c = "#" + (h.j.color || "888");
    return L.divIcon({
      className: "hist-icona",
      html: `<div class="tren-wrap"><span class="tren-dot" style="background:${c}"></span>` +
        `<svg class="tren-arrow" viewBox="0 0 24 24" width="26" height="26">` +
        `<path d="M21 12 L6 4 L11 12 L6 20 Z" fill="${c}" stroke="${VORA_RETARD[k] || "#fff"}" ` +
        `stroke-width="${k ? 2.6 : 1.7}" stroke-linejoin="round"/></svg></div>`,
      iconSize: [26, 26], iconAnchor: [13, 13],
    });
  }

  function prepara(j) {
    const P = j.punts;
    // direcció de marxa a cada punt: la del pròxim moviment (o l'últim, al final)
    let dir = 1;
    for (let i = 0; i < P.length - 1; i++) {
      if (P[i + 1].via === P[i].via && Math.abs(P[i + 1].d - P[i].d) > 5) dir = Math.sign(P[i + 1].d - P[i].d);
      P[i].dirF = dir;
    }
    P[P.length - 1].dirF = dir;
    let seg = null;
    for (let i = P.length - 1; i >= 0; i--) {
      const n = P[i + 1];
      if (n && n.via === P[i].via && Math.abs(n.d - P[i].d) > 5) seg = Math.sign(n.d - P[i].d);
      P[i].dir = seg ?? P[i].dirF;
    }
    const passades = (j.estacions || []).filter((f) => !f.previst).sort((a, b) => a.ts - b.ts);
    return { j, punts: P, passades, iPos: 0, ini: P[0].ts, fi: P[P.length - 1].ts };
  }

  function construeix() {
    panes();
    const rend = L.svg({ pane: "historiaVia" });
    h.capa = L.layerGroup().addTo(map);
    h.segs = [];
    const P = h.punts;
    let fantasma = [];
    const tots = [];
    const tancaFantasma = () => {
      if (fantasma.length > 1) L.polyline(fantasma, { renderer: rend, color: "#c9c9d6", weight: 3, opacity: 0.45, dashArray: "4 6", interactive: false }).addTo(h.capa);
      fantasma = [];
    };
    for (let i = 0; i < P.length - 1; i++) {
      const a = P[i], b = P[i + 1];
      if (b.via !== a.via || b.ts - a.ts > SALT_MAX) { tancaFantasma(); continue; }
      const pts = tros(a.via, a.d, b.d);
      if (pts.length < 2) continue;
      fantasma = fantasma.length ? fantasma.concat(pts.slice(1)) : pts.slice();
      tots.push(...pts);
      const cls = classe(retardA(b.ts));
      h.segs.push({
        t0: a.ts, t1: b.ts, via: a.via, d0: a.d,
        linia: L.polyline(pts, { renderer: rend, color: COLOR[cls], weight: 5, opacity: 0.95, lineCap: "round", interactive: false }),
      });
    }
    tancaFantasma();
    h.vistos = 0;
    h.cua = L.polyline([], { renderer: rend, weight: 5, opacity: 0.95, lineCap: "round", interactive: false }).addTo(h.capa);
    const ll0 = distToLatLng(P[0].via, P[0].d) || map.getCenter();
    h.kIcona = null;
    h.marca = L.marker(ll0, { icon: icona(""), pane: "historia", interactive: false, keyboard: false }).addTo(h.capa);
    if (!tots.length) tots.push(ll0);
    const estret = window.innerWidth < 600;
    map.fitBounds(L.latLngBounds(tots), {
      paddingTopLeft: [40, estret ? 60 : 200], paddingBottomRight: [40, estret ? 230 : 40], maxZoom: 15, animate: false,
    });
  }

  function marcaFi(si) {
    if (!si) {
      if (h.marcaFi) { h.capa.removeLayer(h.marcaFi); h.marcaFi = null; }
      h.marca.setOpacity(1);
      return;
    }
    if (h.marcaFi) return;
    const j = h.j, ult = h.punts[h.punts.length - 1];
    const ll = distToLatLng(ult.via, ult.d);
    if (!ll) return;
    let html, txt;
    if (j.estat === "perdut") {
      const f = final(j);
      html = `<div class="hist-fi ${f.cls}" aria-hidden="true">${f.ic}</div>`;
      txt = f.curt;
      h.marca.setOpacity(0.35);
    } else if (j.estat === "arribat") {
      html = `<div class="hist-fi arribat" aria-hidden="true">✓</div>`;
      txt = `Arriba a ${j.destino} cap a les ${hora(ult.ts)}`;
    } else {
      html = `<div class="hist-fi viu" aria-hidden="true">●</div>`;
      txt = "Continua en directe";
    }
    h.marcaFi = L.marker(ll, {
      icon: L.divIcon({ className: "", html, iconSize: [26, 26], iconAnchor: [13, 13] }),
      pane: "historia", interactive: false, keyboard: false,
    }).bindTooltip(txt, { permanent: true, direction: "top", offset: [0, -14],
      className: "hist-tt" + (j.estat === "perdut" ? " " + final(j).cls : "") }).addTo(h.capa);
  }

  function pinta() {
    const T = h.T;
    const { p, d } = posA(T);
    const ll = distToLatLng(p.via, d);
    if (ll) h.marca.setLatLng(ll);
    const r = retardA(T), cls = classe(r), k = cls === "nd" ? "" : cls;
    if (k !== h.kIcona) { h.marca.setIcon(icona(k)); h.kIcona = k; }
    const el = h.marca.getElement();
    if (el) {
      const arr = el.querySelector(".tren-arrow"), dot = el.querySelector(".tren-dot");
      const n = h.punts[h.iPos + 1];
      const parat = p.origen && (!n || Math.abs(n.d - p.d) < 30);
      const a = distToLatLng(p.via, d), b = distToLatLng(p.via, d + p.dir * 40);
      if (arr && dot) {
        arr.style.display = parat ? "none" : "block";
        dot.style.display = parat ? "block" : "none";
        if (!parat && a && b) {
          const pa = map.latLngToContainerPoint(a), pb = map.latLngToContainerPoint(b);
          arr.style.transform = `rotate(${Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180 / Math.PI}deg)`;
        }
      }
    }
    // traç: els trossos acabats fins a T i el que s'està fent
    while (h.vistos < h.segs.length && h.segs[h.vistos].t1 <= T) h.segs[h.vistos++].linia.addTo(h.capa);
    while (h.vistos > 0 && h.segs[h.vistos - 1].t1 > T) h.capa.removeLayer(h.segs[--h.vistos].linia);
    const s = h.segs[h.vistos];
    if (s && s.t0 <= T && s.via === p.via) {
      h.cua.setLatLngs(tros(s.via, s.d0, d));
      h.cua.setStyle({ color: COLOR[cls] });
    } else h.cua.setLatLngs([]);
    marcaFi(T >= h.fi);
    $("hist-hora").textContent = hora(T, true);
    $("hist-pos").value = Math.round(1000 * (T - h.ini) / Math.max(1, h.fi - h.ini));
    $("hist-ret").innerHTML = `<span class="hist-dot" style="background:${COLOR[cls]}"></span>` +
      `${txtRetard(r)}` + (h.variosDestins ? ` · <span class="hist-mut">→ ${p.destino}</span>` : "") +
      (p.origen && T < h.fi ? ` · <span class="hist-mut">en capçalera</span>` : "");
  }

  function tic() {
    if (!h) return;
    const ara = performance.now();
    const dt = h.ultim ? (ara - h.ultim) / 1000 : 0;
    h.ultim = ara;
    if (h.enMarxa) {
      h.T = Math.min(h.fi, h.T + dt * h.vel);
      if (h.T >= h.fi) posaMarxa(false);
    }
    pinta();
  }

  function posaMarxa(si) {
    if (si && h.T >= h.fi) h.T = h.ini;   // tornar a començar
    h.enMarxa = si;
    const b = $("hist-play");
    b.textContent = si ? "❚❚" : "▶";
    b.setAttribute("aria-label", si ? "Pausa" : "Reprodueix");
  }

  function llistaEstacions() {
    const fs = h.j.estacions || [];
    if (!fs.length) return "<div class='pop-buida'>sense pas registrat per estació</div>";
    const perdut = h.j.estat === "perdut" && final(h.j).ratlla;
    return `<div class="reg-row reg-cap"><span class="reg-h">pas</span><span class="reg-p">horari</span></div>` +
      fs.map((f) => {
        const no = f.previst && perdut;
        const hr = no ? "—" : (f.previst ? "~" : "") + f.hora;
        return `<div class="reg-row${f.previst ? " previst" : ""}${no ? " no-arriba" : ""}"` +
          ` title="${no ? "no hi va arribar" : f.previst ? "previst" : "ja havia passat"}">` +
          `<span class="reg-h">${hr}</span><span class="reg-p">${f.hora_prog || "—"}</span>` +
          `${no ? '<span class="ruta-desf arr-desf tard">✕</span>' : (desfaseArribada(f) || REG_BUIT)}` +
          `<span class="reg-nom">${f.estacion}</span></div>`;
      }).join("");
  }

  function panell() {
    const j = h.j;
    const lin = NOMBRES_LINEA[j.linea] || "L" + j.linea;
    const chip = `<span class="pop-linea" style="background:#${j.color || "888"}">${lin}</span>`;
    const fin = j.estat === "perdut" ? final(j) : null;
    const estat = fin
      ? `<span class="hist-estat ${fin.cls}" title="${fin.ajuda}">${fin.llarg}</span>` +
        (fin.seguit ? ` <button type="button" class="hist-seguix" data-id="${fin.seguit}">Segueix-lo: #${fin.seguit} →</button>` : "")
      : j.estat === "arribat"
        ? `<span class="hist-estat arribat">Va acabar el trajecte a ${j.destino}</span>`
        : `<span class="hist-estat viu">Encara circula</span>` +
          (j.id_actual && j.id_actual !== j.id
            ? ` <button type="button" class="hist-seguix" data-viu="${j.id_actual}">En directe: #${j.id_actual} →</button>` : "");
    const tr = j.trossos || [];
    const unit = tr.length > 1
      ? ` <span class="hist-mut" title="El seguiment el va perdre i retrobar amb un altre número: ${tr.map((t) => "#" + t.id).join(" → ")}">` +
        `recorregut sencer: ${tr.length} trossos units</span>` : "";
    const esp = j.espera
      ? ` <span class="hist-mut" title="FGV anuncia la pròxima eixida molt abans: eixe temps no circulava">` +
        `esperava en capçalera des de les ${hora(j.espera.des_de)}</span>` : "";
    const vels = [30, 60, 120, 300, 600];
    const div = document.createElement("section");
    div.id = "hist-panell";
    div.setAttribute("aria-label", `Recorregut del tren ${j.id}`);
    div.innerHTML =
      `<div class="hist-cap">${capCompartir(`${chip}→ ${j.destino} <span class="pop-id">#${j.id}</span>`,
        String(j.id), `el tren ${lin} cap a ${j.destino}`, "pop-tren-nom")}` +
      `<button type="button" class="hist-tanca" aria-label="Tanca el recorregut">×</button></div>` +
      `<div class="hist-sub">${estat} <span class="hist-mut">${hora(h.ini)}–${hora(h.fi)}</span>${unit}${esp}</div>` +
      `<div class="hist-ctrl"><button type="button" id="hist-play" aria-label="Pausa">❚❚</button>` +
      `<span id="hist-hora"></span>` +
      `<input id="hist-pos" type="range" min="0" max="1000" value="0" aria-label="Moment del recorregut">` +
      `<select id="hist-vel" aria-label="Velocitat">` +
      vels.map((v) => `<option value="${v}"${v === h.vel ? " selected" : ""}>×${v}</option>`).join("") +
      `</select></div>` +
      `<div id="hist-ret" class="hist-ret"></div>` +
      `<details class="hist-llista"><summary>Horari i pas per estació</summary>` +
      `<div class="pop-reg-llista">${llistaEstacions()}</div></details>`;
    document.body.appendChild(div);
    lligaCompartir(div);
    div.querySelector(".hist-tanca").onclick = tanca;
    const seg = div.querySelector(".hist-seguix");
    if (seg) seg.onclick = () => {
      if (seg.dataset.viu) { tanca(); window.obrirEnllac(seg.dataset.viu); }
      else window.historiaTren(Number(seg.dataset.id));
    };
    $("hist-play").onclick = () => posaMarxa(!h.enMarxa);
    $("hist-vel").onchange = (e) => { h.vel = Number(e.target.value); };
    $("hist-pos").oninput = (e) => {
      h.T = h.ini + (h.fi - h.ini) * Number(e.target.value) / 1000;
      pinta();
    };
  }

  function tanca() {
    if (!h) return;
    clearInterval(h.timer);
    map.removeLayer(h.capa);
    const p = $("hist-panell");
    if (p) p.remove();
    document.body.classList.remove("mode-historia");
    llevaEnllac(String(h.j.id));
    h = null;
  }

  window.historiaTren = async function (id) {
    tanca();
    posaEnllac(String(id));
    let j;
    try {
      const r = await fetch(`${API}api/historia_tren?id=${id}&_=${Date.now()}`, { cache: "no-store" });
      if (r.status === 404) {
        avisCurt(`No hi ha registre del tren #${id}: es guarden els últims 7 dies`, 6000);
        llevaEnllac(String(id));
        return;
      }
      j = await r.json();
    } catch (e) {
      avisCurt("No s'ha pogut carregar el recorregut del tren", 5000);
      return;
    }
    if (!j || !j.punts || !j.punts.length) { llevaEnllac(String(id)); return; }
    // encara circula i es veu al mapa: millor el seu popup en directe
    if (j.estat === "viu" && window.obrirEnllac && window.marcadorTren && window.marcadorTren(id)) {
      window.obrirEnllac(String(id));
      return;
    }
    h = prepara(j);
    h.variosDestins = new Set(j.punts.map((p) => p.destino)).size > 1;
    const dur = h.fi - h.ini;
    h.vel = dur > 3 * 3600 ? 300 : dur > 3600 ? 120 : 60;
    h.T = h.ini;
    h.enMarxa = true;
    map.closePopup();
    document.body.classList.add("mode-historia");
    construeix();
    panell();
    pinta();
    h.timer = setInterval(tic, 100);
  };
  window.tancaHistoria = tanca;
})();
