/* Metroviolencia · «quant tarda aquest tren a…» i «avisa'm en arribar».
   Des del popup d'un tren es tria una estació del mapa (mentre es tria, els
   trens no es poden tocar). Si l'estació és entre les pròximes del tren, el
   temps és el de FGV; si és més enllà, s'estima amb la velocitat mitjana del
   tren sobre la seua via (aprox.). L'avís és un rètol discret de 10 s amb un
   so fluix; tot viu en aquesta pestanya (res es guarda ni s'envia). */
(function () {
  const AVIS_S = 20;          // s: avisa quan li queda això (o menys) per arribar
  const TOAST_MS = 10000;     // el rètol d'avís desapareix sol als 10 s
  const VEL_DEF = 11;         // m/s si el tren encara no dona velocitat pròpia

  let triant = null;          // tren (id) del qual s'està triant estació
  let desti = null;           // {id, estId, estNom, avisa, exacte}
  let audio = null;

  const $ = (id) => document.getElementById(id);
  const nomLinia = (t) => NOMBRES_LINEA[t.linea] || "L" + t.linea;
  const trenViu = (id) => (ultimoTrenes || []).find((t) => t.id === id) || null;
  const ara = () => serverNow() / 1000;

  function crea() {
    if ($("desti-banner")) return;
    const b = document.createElement("div");
    b.id = "desti-banner"; b.hidden = true;
    b.innerHTML = `<span id="desti-banner-txt"></span><button id="desti-cancela">Cancel·la</button>`;
    const c = document.createElement("div");
    c.id = "desti-card"; c.hidden = true;
    c.innerHTML = `<div id="desti-txt"></div><div class="desti-acc">` +
      `<button id="desti-avisa" class="primari">Avisa'm en arribar</button>` +
      `<button id="desti-altra">Una altra estació</button>` +
      `<button id="desti-tanca" aria-label="Tanca">×</button></div>`;
    const t = document.createElement("div");
    t.id = "desti-toast"; t.hidden = true;
    document.body.append(b, c, t);
    $("desti-cancela").onclick = () => surt();
    $("desti-tanca").onclick = () => { desti = null; pinta(); };
    $("desti-altra").onclick = () => { if (desti) tria(desti.id); };
    $("desti-avisa").onclick = () => {
      if (!desti) return;
      desti.avisa = !desti.avisa;
      if (desti.avisa) preparaSo();
      pinta();
    };
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && triant != null) surt(); });
  }

  // modo tria: els trens no es poden tocar (la capa de marcadors no rep clics)
  function tria(id) {
    const t = trenViu(id);
    if (!t) return;
    crea();
    map.closePopup();
    triant = id;
    document.body.classList.add("mode-desti");
    $("desti-banner-txt").innerHTML =
      `Tria l'estació · <span class="pop-linea" style="background:#${t.color || "888"}">${nomLinia(t)}</span> → ${t.destino}`;
    $("desti-banner").hidden = false;
    marcaTren();
  }

  function surt() {
    triant = null;
    document.body.classList.remove("mode-desti");
    if ($("desti-banner")) $("desti-banner").hidden = true;
    marcaTren();
  }

  // el tren seguit es veu ressaltat (i per damunt dels altres)
  function marcaTren() {
    const id = triant != null ? triant : desti ? desti.id : null;
    for (const e of marcadores.values()) {
      const el = e.marker.getElement();
      const sel = id != null && e.tren.id === id;
      if (el) el.classList.toggle("desti-sel", sel);
      e.marker.setZIndexOffset(sel ? 1000 : 0);
    }
  }

  // ETA (s des d'ara) del tren a l'estació: {eta, exacte} | {passat} | {fora}
  function etaA(t, estId, estNom) {
    const wps = t.waypoints || [];
    const edat = ara() - (ultimoTs || ara());
    const wp = wps.find((w) => w.nom === estNom);
    if (wp) return { eta: wp.eta - edat, exacte: true };
    const lc = comerciales[viaDe(t)] || comerciales[t.linea];
    const e = lc && (lc.estaciones || []).find((x) => x.id === estId);
    if (!e || !wps.length) return { fora: true };
    const d0 = wps[0].d, ult = wps[wps.length - 1];
    const dir = ult.d >= d0 ? 1 : -1;
    if ((e.d - d0) * dir < 0) return { passat: true };
    const dest = (lc.estaciones || []).find((x) => x.id === (porNomGlobal[t.destino] || {}).id);
    if (dest && (e.d - dest.d) * dir > 0) return { fora: true, acaba: t.destino };
    const v = ult.eta > 60 && Math.abs(ult.d - d0) > 200 ? Math.abs(ult.d - d0) / ult.eta : VEL_DEF;
    return { eta: ult.eta + Math.abs(e.d - ult.d) / v - edat, exacte: false };
  }

  // clic en una estació mentre es tria: true = consumit (no obre el popup)
  window.destiTriaEstacio = function (m) {
    if (triant == null) return false;
    desti = { id: triant, estId: m.estId, estNom: m.estNom, avisa: false };
    surt();
    marcaTren();
    pinta();
    return true;
  };

  function fmtMin(s) {
    if (s < 45) return "ara mateix";
    return `${Math.round(s / 60)} min`;
  }

  function pinta() {
    crea();
    const card = $("desti-card");
    if (!desti) { card.hidden = true; marcaTren(); return; }
    const t = trenViu(desti.id);
    card.hidden = false;
    let txt;
    if (!t) {
      txt = `S'ha perdut el tren (ja no apareix en les dades de FGV).`;
      desti.avisa = false;
    } else {
      const r = etaA(t, desti.estId, desti.estNom);
      const cap = `<span class="pop-linea" style="background:#${t.color || "888"}">${nomLinia(t)}</span> → ${t.destino}`;
      if (r.passat) txt = `${cap}<br>ja ha passat per <b>${desti.estNom}</b>.`;
      else if (r.fora) txt = `${cap}<br>no passa per <b>${desti.estNom}</b>` + (r.acaba ? ` (acaba a ${r.acaba}).` : ".");
      else {
        const hora = new Date((ara() + r.eta) * 1000).toLocaleTimeString("ca-ES", { hour: "2-digit", minute: "2-digit" });
        txt = `${cap}<br>arriba a <b>${desti.estNom}</b> en <b>${r.exacte ? "" : "~"}${fmtMin(Math.max(0, r.eta))}</b>` +
          ` <span class="desti-hora">(${r.exacte ? "" : "aprox. "}${hora})</span>`;
        desti.exacte = r.exacte;
        if (desti.avisa && r.eta <= AVIS_S) avisa(cap, desti.estNom);
      }
      if ((r.passat || r.fora) && desti.avisa) {
        if (r.passat && desti.exacte) avisa(cap, desti.estNom);   // ha passat entre dues pasades
        desti.avisa = false;
      }
    }
    if (desti) {
      $("desti-txt").innerHTML = txt;
      const b = $("desti-avisa");
      b.textContent = desti.avisa ? "Avís actiu · desactiva" : "Avisa'm en arribar";
      b.classList.toggle("on", !!desti.avisa);
      b.hidden = !t;
    }
  }

  function avisa(cap, estNom) {
    if (desti) desti.avisa = false;
    const toast = $("desti-toast");
    toast.innerHTML = `${cap} arriba a <b>${estNom}</b>`;
    toast.hidden = false;
    toast.classList.remove("fora");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      toast.classList.add("fora");
      setTimeout(() => { toast.hidden = true; }, 600);
    }, TOAST_MS);
    so();
  }

  // so fluix: dos tons curts. L'AudioContext es crea amb el clic d'«Avisa'm»
  // (els navegadors no deixen sonar res sense un gest de l'usuari).
  function preparaSo() {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume();
    } catch (e) { audio = null; }
  }

  function so() {
    if (!audio) return;
    const t0 = audio.currentTime;
    [[880, 0], [660, 0.18]].forEach(([f, dt]) => {
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0, t0 + dt);
      g.gain.linearRampToValueAtTime(0.05, t0 + dt + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.16);
      o.connect(g).connect(audio.destination);
      o.start(t0 + dt); o.stop(t0 + dt + 0.2);
    });
  }

  // botó del popup del tren
  window.lligaDesti = function (el) {
    if (!el) return;
    el.querySelectorAll("button[data-desti]").forEach((b) => {
      b.onclick = () => tria(Number(b.dataset.desti));
    });
  };

  // cada segon: compte enrere i avís; a cada refresc, el ressaltat del tren
  window.destiRefresc = () => { if (desti) pinta(); marcaTren(); };
  setInterval(() => { if (desti) pinta(); }, 1000);
})();
