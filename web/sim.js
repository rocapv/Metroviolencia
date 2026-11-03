/* Metroviolencia · simulació d'un matí: reprodueix, amb el rellotge accelerat,
   les pasades guardades des de l'obertura del metro (primer tren en marxa de
   matinada) fins a una hora (per defecte les 10:00).
   S'activa amb ?sim=AAAA-MM-DD[&fins=HH:MM] (o ?sim=1 = hui). Mentre dura, la
   web no llig el directe: el rellotge del mapa (serverNow) és el de la simulació. */
(function () {
  const qs = new URLSearchParams(location.search);
  const DIA = qs.get("sim");
  window.simActiva = () => !!DIA;
  if (!DIA) return;

  let pasades = [], i = -1, simT = 0, inici = 0, fi = 0;
  let vel = Number(qs.get("vel")) || 60, enMarxa = true, ultimTic = 0;
  const $ = (id) => document.getElementById(id);
  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString("ca-ES", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  function barra() {
    const b = document.createElement("div");
    b.id = "sim-barra";
    b.innerHTML =
      `<b>Simulació</b> <span id="sim-dia"></span>` +
      `<button id="sim-play" aria-label="Pausa">❚❚</button>` +
      `<span id="sim-hora">carregant…</span>` +
      `<input id="sim-pos" type="range" min="0" max="1000" value="0" aria-label="Moment de la simulació">` +
      `<select id="sim-vel" aria-label="Velocitat">` +
      [10, 30, 60, 120, 300].map((v) => `<option value="${v}"${v === vel ? " selected" : ""}>×${v}</option>`).join("") +
      `</select><a id="sim-ix" href="./">Directe</a>`;
    document.body.appendChild(b);
    $("sim-play").onclick = () => { enMarxa = !enMarxa; $("sim-play").textContent = enMarxa ? "❚❚" : "▶"; };
    $("sim-vel").onchange = (e) => { vel = Number(e.target.value); };
    $("sim-pos").oninput = (e) => { salta(inici + (fi - inici) * e.target.value / 1000); };
  }

  // pasada vigent al moment simT -> la web la pinta com si acabara d'arribar
  function aplica(k) {
    if (k === i || k < 0) return;
    i = k;
    const p = pasades[k];
    ultimoTrenes = p.trenes;
    ultimoTs = p.ts;
    ultimoTsFin = p.ts;
    ultimoFetchMs = Date.now();
    noinfo = [];
    actualitzaCongelats(ultimoTrenes);
    pintarTrenes(ultimoTrenes);
    if (window.destiRefresc) window.destiRefresc();
    const lin = {};
    for (const t of p.trenes) if (!t.origen) lin[t.linea] = (lin[t.linea] || 0) + 1;
    const n = Object.values(lin).reduce((a, b) => a + b, 0);
    $("stats").innerHTML = `<b>${n}</b> trens en marxa · ${p.trenes.length - n} en capçalera<br>` +
      `<span style="opacity:.7">simulació · pasada de les ${hhmm(p.ts)}</span>`;
  }

  function salta(t) {
    simT = Math.max(inici, Math.min(fi, t));
    let k = 0;
    while (k + 1 < pasades.length && pasades[k + 1].ts <= simT) k++;
    if (k < i) histTren.clear();          // enrere: sense historial de congelats
    i = -1;
    aplica(k);
  }

  function tic() {
    const ara = performance.now();
    const dt = ultimTic ? (ara - ultimTic) / 1000 : 0;
    ultimTic = ara;
    if (enMarxa && pasades.length) {
      simT = Math.min(fi, simT + dt * vel);
      if (simT >= fi) { enMarxa = false; $("sim-play").textContent = "▶"; }
      let k = Math.max(i, 0);
      while (k + 1 < pasades.length && pasades[k + 1].ts <= simT) k++;
      aplica(k);
    }
    // el mapa anima amb serverNow(): que siga l'hora simulada
    clockOffsetMs = simT * 1000 - Date.now();
    $("sim-hora").textContent = hhmm(simT);
    if (fi > inici) $("sim-pos").value = Math.round(1000 * (simT - inici) / (fi - inici));
  }

  window.iniciaSimulacio = async function () {
    barra();
    const dia = DIA === "1" ? "" : DIA;
    const fins = qs.get("fins") || "10:00";
    $("sim-dia").textContent = (dia || "hui") + ` · fins a les ${fins}`;
    let j;
    try {
      j = await (await fetch(`${API}api/simulacio?fins=${encodeURIComponent(fins)}` + (dia ? `&dia=${dia}` : ""), { cache: "no-store" })).json();
    } catch (e) { $("sim-hora").textContent = "sense connexió"; return; }
    pasades = j.pasades || [];
    if (!pasades.length) { $("sim-hora").textContent = "no hi ha dades d'eixe matí (es guarden 3 dies)"; return; }
    inici = pasades[0].ts;
    fi = Math.max(j.fins || 0, pasades[pasades.length - 1].ts);
    $("sim-dia").textContent += ` · obertura ${hhmm(j.obertura)}`;
    salta(inici);
    setInterval(tic, 100);
    setInterval(animar, 100);              // a ×60 el pas de 500 ms es veuria a salts
  };
})();
