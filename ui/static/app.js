"use strict";

const STAGES = ["asr", "frames", "vlm", "llm"];
const STAGE_NAME = {
  asr: "Transkrypcja mowy", frames: "Klatki z tablicy", vlm: "Odczyt tablicy (VLM)", llm: "Notatki (LLM)",
};
const STAGE_SHORT = { asr: "Transkrypcja", frames: "Klatki", vlm: "Odczyt VLM", llm: "Notatki" };
const STATUS_PL = {
  pending: "oczekuje", running: "w toku", done: "gotowe", skipped: "pominięte", error: "błąd",
  cancelled: "anulowane", interrupted: "przerwane", stale: "nieaktualne", queued: "w kolejce",
  idle: "nieprzetworzony",
};

const state = {
  queue: { jobs: [], paused: false, blocked_by: null },
  videos: [], gpu: null, system: null, connected: false, loaded: false,
};

// ---------- pomocnicze ----------
const $ = (sel) => document.querySelector(sel);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { /* brak treści */ }
  if (!r.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `Błąd ${r.status}`);
  }
  return data;
}

function fmtDur(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
}
function fmtTook(sec) {
  if (sec == null) return "";
  if (sec < 60) return `${Math.round(sec)} s`;
  return `${Math.floor(sec / 60)} min ${Math.round(sec % 60)} s`;
}
function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("pl-PL", { dateStyle: "medium", timeStyle: "short" }) : "";
}
function fmtSize(b) {
  return b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`;
}
function chip(status, text) { return h("span", { class: `chip ${status}` }, text || STATUS_PL[status] || status); }

function progressText(stage, p) {
  if (!p) return "";
  if (stage === "asr" && p.message === "transkrypcja") return `${p.message}: ${fmtDur(p.done)} / ${fmtDur(p.total)}`;
  return `${p.message || ""} ${Math.round(p.done)} / ${Math.round(p.total)}`.trim();
}
function progressBar(p) {
  const pct = p && p.total ? Math.min(100, (100 * p.done) / p.total) : 0;
  return h("div", { class: "bar" }, h("i", { style: `width:${pct}%` }));
}

// replaceChildren zamienia null na tekst "null", wiec filtrujemy puste wartosci
const kids = (...a) => a.flat(Infinity).filter((x) => x != null && x !== false);

function videoById(id) { return state.videos.find((v) => v.id === id); }
function activeJob(videoId) {
  return state.queue.jobs.find((j) => j.video_id === videoId && (j.status === "queued" || j.status === "running"));
}
function toast(msg) {
  const b = $("#banner");
  b.className = "banner warn";
  b.textContent = msg;
  b.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(updateBanner, 5000);
}

// ---------- okno dialogowe ----------
function openDialog(...content) {
  const d = $("#dlg");
  d.replaceChildren(...content);
  if (!d.open) d.showModal();
  return d;
}
function closeDialog() { const d = $("#dlg"); if (d.open) d.close(); }

// ---------- pasek systemowy i baner ----------
function updateBanner() {
  const b = $("#banner");
  if (!state.connected) {
    b.className = "banner";
    b.replaceChildren(
      "Brak połączenia z serwerem. Uruchom aplikację skryptem ", h("code", {}, "scripts/start.bat"),
      " (Windows) albo ", h("code", {}, "python server.py"), " w WSL. Połączenie wróci automatycznie.");
    b.hidden = false;
    return;
  }
  const sys = state.system;
  const missing = [];
  if (sys && sys.ollama && sys.ollama.ok) {
    for (const m of [sys.models.vlm, sys.models.llm]) {
      if (!sys.ollama.installed.includes(m)) missing.push(m);
    }
  }
  if (sys && sys.ollama && !sys.ollama.ok) {
    b.className = "banner warn";
    b.textContent = "Ollama nie odpowiada. Uruchom ją (ollama serve), inaczej etapy VLM i LLM zakończą się błędem.";
    b.hidden = false;
  } else if (missing.length) {
    b.className = "banner warn";
    b.replaceChildren("Brak modeli w Ollamie: ", ...missing.flatMap((m) => [h("code", {}, `ollama pull ${m}`), " "]));
    b.hidden = false;
  } else {
    b.hidden = true;
  }
}

function renderSys() {
  const el = $("#sys");
  const sys = state.system;
  const parts = [];
  parts.push(chip(state.connected ? "ok" : "bad", state.connected ? "serwer" : "brak serwera"));
  if (sys && state.connected) {
    parts.push(chip(sys.ollama.ok ? "ok" : "bad", sys.ollama.ok ? "Ollama" : "Ollama: brak"));
    if (state.gpu) parts.push(chip("running", `GPU: ${state.gpu.owner}`));
    if (sys.vram && sys.vram.total_gb) {
      parts.push(h("span", { class: "chip" }, `VRAM ${sys.vram.used_gb?.toFixed(1) ?? "?"}/${sys.vram.total_gb.toFixed(0)} GB`));
    }
  }
  el.replaceChildren(...parts);
}

// ---------- widok: biblioteka ----------
function videoCard(v) {
  const run = Object.entries(v.stages).find(([, s]) => s.status === "running");
  const job = activeJob(v.id);
  return h("div", { class: "card" },
    h("div", { class: "row spread" },
      h("div", {},
        h("a", { class: "vtitle", href: `#/video/${v.id}` }, v.title || v.id),
        h("div", { class: "muted small" },
          [v.duration != null ? fmtDur(v.duration) : null, fmtDate(v.created), v.has_audio === false ? "bez audio" : null,
            v.video_exists ? null : "brak pliku wideo"].filter(Boolean).join(" · "))),
      job && job.status === "queued" ? chip("queued") : chip(v.overall === "idle" ? "pending" : v.overall,
        v.overall === "idle" ? STATUS_PL.idle : null)),
    h("div", { class: "pills" }, STAGES.map((s) => h("div", { class: `pill ${v.stages[s].status}` },
      h("b", {}, STAGE_SHORT[s]), STATUS_PL[v.stages[s].status]))),
    run ? h("div", { class: "small muted", style: "margin-top:8px" }, progressText(run[0], run[1].progress), progressBar(run[1].progress)) : null,
    v.error && v.overall === "error" ? h("div", { class: "small", style: "margin-top:8px;color:var(--err)" }, v.error) : null);
}

function queuePanel() {
  const q = state.queue;
  const queued = q.jobs.filter((j) => j.status === "queued");
  const running = q.jobs.filter((j) => j.status === "running");
  const recent = q.jobs.filter((j) => !["queued", "running"].includes(j.status)).slice(-5).reverse();
  const title = (j) => (videoById(j.video_id) || {}).title || j.video_id;
  const label = (j) => (j.only_stage ? `tylko: ${STAGE_SHORT[j.only_stage]}` : j.from_stage ? `od: ${STAGE_SHORT[j.from_stage]}` : "całość") + (j.force ? " (od nowa)" : "");
  const row = (j, idx) => h("div", { class: "job" },
    h("div", { class: "name" }, h("a", { href: `#/video/${j.video_id}` }, title(j)), h("div", { class: "muted small" }, label(j))),
    chip(j.status),
    j.status === "queued" ? [
      h("button", { class: "small", title: "Wyżej", disabled: idx === 0, onclick: () => moveJob(j.id, idx - 1) }, "↑"),
      h("button", { class: "small", title: "Niżej", disabled: idx === queued.length - 1, onclick: () => moveJob(j.id, idx + 1) }, "↓"),
    ] : null,
    j.status === "queued" || j.status === "running"
      ? h("button", { class: "small danger", title: "Anuluj", onclick: () => cancelJob(j.id) }, "✕") : null);
  const empty = !running.length && !queued.length;
  return h("div", { class: "card" },
    h("div", { class: "row spread" }, h("h2", {}, "Kolejka"),
      h("button", { class: "small", onclick: () => togglePause() }, q.paused ? "Wznów kolejkę" : "Wstrzymaj")),
    q.paused ? h("div", { class: "small", style: "color:var(--warn)" }, "Kolejka wstrzymana: nowe zadania nie startują.") : null,
    q.blocked_by ? h("div", { class: "small", style: "color:var(--warn)" }, `GPU zajęte przez: ${q.blocked_by.owner}. Zadanie ruszy po zwolnieniu.`) : null,
    empty ? h("div", { class: "muted small" }, "Brak zadań w kolejce.") : null,
    running.map((j) => row(j, -1)), queued.map(row),
    recent.length ? h("details", { style: "margin-top:8px" }, h("summary", { class: "small" }, "Ostatnio zakończone"),
      recent.map((j) => h("div", { class: "job" }, h("div", { class: "name" }, title(j), j.error ? h("div", { class: "small", style: "color:var(--err)" }, j.error) : null), chip(j.status)))) : null);
}

function renderLibrary() {
  const view = $("#view");
  const list = state.videos.length
    ? h("div", { class: "list" }, state.videos.map(videoCard))
    : h("div", { class: "card empty" }, state.loaded ? "Nie dodano jeszcze żadnego filmu. Kliknij „Dodaj film”, aby wybrać nagranie z katalogu." : "Ładowanie…");
  view.replaceChildren(
    h("div", { class: "row spread", style: "margin-bottom:14px" },
      h("div", {}, h("h1", {}, "Wykłady"), h("div", { class: "muted small" }, "Filmy przetwarzane lokalnie: transkrypcja, odczyt tablicy, notatki.")),
      h("button", { class: "primary", onclick: openAddDialog }, "+ Dodaj film")),
    h("div", { class: "layout" }, list, queuePanel()));
}

// ---------- dodawanie filmu ----------
async function openAddDialog() {
  const body = h("div", {}, h("div", { class: "muted" }, "Ładowanie listy plików…"));
  openDialog(h("h2", {}, "Dodaj film z katalogu"), body);
  let files;
  try { files = await api("/api/library/files"); } catch (e) { body.textContent = e.message; return; }
  const dir = state.system && state.system.paths ? state.system.paths.videos_dir : "videos/";
  if (!files.length) {
    body.replaceChildren(h("p", {}, "Katalog z nagraniami jest pusty."), h("p", { class: "muted small" }, "Skopiuj pliki wideo (mp4, mkv, mov, webm, avi) do: ", h("code", {}, dir)),
      h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Zamknij")));
    return;
  }
  const checks = new Map();
  const rows = files.map((f) => {
    const cb = h("input", { type: "checkbox" });
    checks.set(f.file, cb);
    return h("label", { class: "file" }, cb, h("span", { class: "name" }, f.file),
      f.video_id ? chip("done", "dodany") : null, h("span", { class: "muted small" }, fmtSize(f.size)));
  });
  const profile = h("select", {}, h("option", { value: "auto" }, "auto (wykryj VRAM)"), h("option", { value: "8gb" }, "8 GB"), h("option", { value: "16gb" }, "16 GB"));
  const go = async (enqueue) => {
    const sel = [...checks].filter(([, cb]) => cb.checked).map(([f]) => f);
    if (!sel.length) { toast("Zaznacz co najmniej jeden plik."); return; }
    try {
      await api("/api/videos", { method: "POST", body: { files: sel, enqueue, profile: profile.value } });
      closeDialog();
      await refresh();
    } catch (e) { toast(e.message); }
  };
  body.replaceChildren(
    h("div", { class: "files" }, rows),
    h("div", { class: "row", style: "margin-top:12px" }, h("span", { class: "muted small" }, "Profil VRAM:"), profile),
    h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"),
      h("button", { onclick: () => go(false) }, "Tylko dodaj"), h("button", { class: "primary", onclick: () => go(true) }, "Dodaj i uruchom")));
}

// ---------- kolejka: akcje ----------
async function cancelJob(id) {
  try { await api(`/api/queue/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
}
async function moveJob(id, pos) {
  try { await api(`/api/queue/${id}/move`, { method: "POST", body: { position: pos } }); } catch (e) { toast(e.message); }
}
async function togglePause() {
  try { await api(`/api/queue/${state.queue.paused ? "resume" : "pause"}`, { method: "POST" }); } catch (e) { toast(e.message); }
}

// ---------- widok: szczegóły filmu ----------
let logSource = null;
let detailId = null;

function stopLogs() { if (logSource) { logSource.close(); logSource = null; } }

function mountDetail(id) {
  detailId = id;
  stopLogs();
  const view = $("#view");
  const logs = h("pre", { class: "logs", id: "logbox" }, "");
  const tabs = h("div", { class: "tabs", role: "tablist" },
    h("button", { role: "tab", "aria-selected": "true" }, "Przetwarzanie"),
    h("button", { role: "tab", disabled: true, title: "Dostępne w kolejnej fazie" }, "Notatki"),
    h("button", { role: "tab", disabled: true, title: "Dostępne w kolejnej fazie" }, "Czat"));
  view.replaceChildren(
    h("div", { id: "dhead" }), tabs, h("div", { id: "dstages" }),
    h("details", { class: "card", style: "margin-top:12px", ontoggle: (e) => { if (e.target.open) startLogs(id); else stopLogs(); } },
      h("summary", {}, "Logi"), logs));
  updateDetail();
}

function startLogs(id) {
  stopLogs();
  const box = $("#logbox");
  box.textContent = "";
  logSource = new EventSource(`/api/videos/${id}/logs`);
  logSource.onmessage = (e) => {
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.append(document.createTextNode(JSON.parse(e.data) + "\n"));
    while (box.childNodes.length > 600) box.removeChild(box.firstChild);
    if (near) box.scrollTop = box.scrollHeight;
  };
}

async function runVideo(id, body) {
  try { await api(`/api/videos/${id}/run`, { method: "POST", body }); await refresh(); } catch (e) { toast(e.message); }
}

async function openRegenDialog(v, stage) {
  let removes = [];
  try { removes = (await api(`/api/videos/${v.id}/impact?stage=${stage}`)).removes; } catch (_) { /* pokaż bez listy */ }
  const only = h("input", { type: "radio", name: "scope", value: "only", checked: true });
  const after = h("input", { type: "radio", name: "scope", value: "after" });
  const warn = h("div", { class: "small", style: "margin-top:10px;color:var(--warn)" });
  const upd = () => {
    const list = after.checked ? removes : removes.filter((s) => s === stage);
    warn.textContent = list.length ? `Zostaną usunięte wyniki: ${list.map((s) => STAGE_SHORT[s]).join(", ")}.` : "Brak istniejących wyników do usunięcia.";
  };
  only.onchange = after.onchange = upd;
  upd();
  openDialog(h("h2", {}, `Regeneruj: ${STAGE_NAME[stage]}`),
    h("label", { class: "row" }, only, "Tylko ten etap"),
    h("label", { class: "row" }, after, "Ten etap i wszystkie następne"),
    warn,
    h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"),
      h("button", { class: "primary", onclick: async () => {
        closeDialog();
        await runVideo(v.id, after.checked ? { from_stage: stage, force: true } : { only_stage: stage, force: true });
      } }, "Regeneruj")));
}

function openRerunAllDialog(v) {
  openDialog(h("h2", {}, "Uruchom całość od nowa"),
    h("p", {}, "Wszystkie etapy zostaną wykonane ponownie, a dotychczasowe wyniki (transkrypt, klatki, opisy, notatki) usunięte."),
    h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"),
      h("button", { class: "danger", onclick: async () => { closeDialog(); await runVideo(v.id, { force: true }); } }, "Uruchom od nowa")));
}

function updateDetail() {
  if (!detailId) return;
  const v = videoById(detailId);
  const head = $("#dhead"), stages = $("#dstages");
  if (!head || !stages) return;
  if (!v) {
    head.replaceChildren(h("a", { href: "#/" }, "← Wróć"), h("h1", {}, state.loaded ? "Nie znaleziono filmu" : "Ładowanie…"));
    stages.replaceChildren();
    return;
  }
  const job = activeJob(v.id);
  const resumable = ["error", "cancelled", "interrupted", "stale"].includes(v.overall)
    || Object.values(v.stages).some((s) => ["stale", "pending"].includes(s.status) && v.overall !== "running");
  head.replaceChildren(...kids(
    h("a", { href: "#/", class: "small" }, "← Wykłady"),
    h("div", { class: "row spread", style: "margin-top:6px" },
      h("div", {}, h("h1", {}, v.title || v.id),
        h("div", { class: "muted small" }, [v.duration != null ? fmtDur(v.duration) : null, fmtDate(v.created), v.has_audio === false ? "bez audio" : null, v.profile ? `profil ${v.profile}` : null].filter(Boolean).join(" · "))),
      h("div", { class: "row" },
        job ? h("button", { class: "danger", onclick: () => cancelJob(job.id) }, job.status === "running" ? "Zatrzymaj" : "Usuń z kolejki") : null,
        !job && resumable ? h("button", { class: "primary", onclick: () => runVideo(v.id, {}) }, v.overall === "idle" ? "Uruchom" : "Wznów") : null,
        h("button", { disabled: !!job, onclick: () => openRerunAllDialog(v) }, "Uruchom całość od nowa"))),
    !v.video_exists ? h("div", { class: "banner", style: "margin:10px 0 0" }, `Plik wideo nie istnieje: ${v.video_path}`) : null,
    v.error && v.overall === "error" ? h("div", { class: "banner", style: "margin:10px 0 0" }, `Błąd: ${v.error}`) : null,
    v.overall === "interrupted" ? h("div", { class: "banner warn", style: "margin:10px 0 0" }, "Poprzednie uruchomienie zostało przerwane (np. wyłączenie WSL). Wznów, aby kontynuować od ostatniego checkpointu.") : null));
  stages.replaceChildren(h("div", { class: "card" }, STAGES.map((s) => {
    const st = v.stages[s];
    const showBar = st.progress && (st.status === "running" || st.status === "interrupted" || st.status === "cancelled" || st.status === "error");
    return h("div", { class: "stage" },
      h("div", {}, h("div", { class: "row" }, h("h3", {}, STAGE_NAME[s]), chip(st.status),
        st.seconds != null && st.status === "done" ? h("span", { class: "muted small" }, fmtTook(st.seconds)) : null),
      showBar || (st.progress && st.status === "running") ? h("div", { class: "small muted", style: "margin-top:4px" }, progressText(s, st.progress), progressBar(st.progress)) : null,
      s === "asr" && v.has_audio === false && st.status === "skipped" ? h("div", { class: "small muted" }, "Brak ścieżki audio: pominięto, notatki powstaną tylko z tablicy.") : null),
      h("button", { class: "small", disabled: !!job || st.status === "running", onclick: () => openRegenDialog(v, s) }, "Regeneruj"));
  })));
}

// ---------- routing i dane ----------
function route() {
  const m = location.hash.match(/^#\/video\/([a-z0-9_-]+)/);
  if (m) {
    if (detailId !== m[1] || !$("#dhead")) mountDetail(m[1]);
    else updateDetail();
  } else {
    detailId = null;
    stopLogs();
    renderLibrary();
  }
}
function rerender() {
  renderSys();
  updateBanner();
  if (detailId && location.hash.startsWith("#/video/")) updateDetail();
  else renderLibrary();
}

async function refresh() {
  try {
    const [queue, videos] = await Promise.all([api("/api/queue"), api("/api/videos")]);
    state.queue = queue; state.videos = videos; state.loaded = true; state.connected = true;
  } catch (_) { /* SSE przejmie */ }
  rerender();
}

async function loadSystem() {
  try { state.system = await api("/api/system"); } catch (_) { state.system = null; }
  rerender();
}

function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("state", (e) => {
    const d = JSON.parse(e.data);
    state.queue = d.queue; state.videos = d.videos; state.gpu = d.gpu_holder;
    state.loaded = true; state.connected = true;
    rerender();
  });
  es.onopen = () => { state.connected = true; loadSystem(); };
  es.onerror = () => { state.connected = false; rerender(); };
}

window.addEventListener("hashchange", route);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
route();
connect();
loadSystem();
setInterval(loadSystem, 15000);
