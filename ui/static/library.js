"use strict";
// Biblioteka: foldery (zagnieżdżone, z przeciąganiem), zmiana nazw, sortowanie i wyszukiwarka pełnotekstowa.
// Korzysta z pomocników z app.js (h, kids, api, toast, chip, fmtDur, fmtDate, openDialog, closeDialog, confirmDialog,
// state, trackOf, queuePanel, openAddDialog, openDeleteDialog).

function lsGet(key, def) { try { return localStorage.getItem(key) || def; } catch (_) { return def; } }
function lsSet(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* tryb prywatny */ } }

const libView = {
  query: "", results: null, searching: false, onlyHere: false, timer: null, seq: 0, dragging: false,
  sort: lsGet("lecturenoter.sort", "new"), mounted: null,
};

const plCompare = (a, b) => (a || "").localeCompare(b || "", "pl", { numeric: true, sensitivity: "base" });
const escapeRx = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// ---------- dane pochodne ----------
function currentFolderId() { const m = location.hash.match(/^#\/f\/([a-z0-9]+)/); return m ? m[1] : null; }
function folderById(id) { return state.folders.find((f) => f.id === id); }
function childFolders(parent) {
  return state.folders.filter((f) => (f.parent || null) === (parent || null)).sort((a, b) => plCompare(a.name, b.name));
}
function descendantIds(fid) {
  const out = new Set([fid]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const f of state.folders) if (f.parent && out.has(f.parent) && !out.has(f.id)) { out.add(f.id); grew = true; }
  }
  return out;
}
function folderChain(fid) {
  const chain = [];
  for (let f = fid && folderById(fid); f && chain.length < 10; f = f.parent && folderById(f.parent)) chain.unshift(f);
  return chain;
}
function lecturesIn(fid) {
  return state.videos.filter((v) => ((v.folder && folderById(v.folder)) ? v.folder : null) === (fid || null));
}
function sortVideos(list) {
  const c = [...list];
  if (libView.sort === "name") c.sort((a, b) => plCompare(a.title || a.id, b.title || b.id));
  else if (libView.sort === "old") c.sort((a, b) => (a.created || 0) - (b.created || 0));
  else c.sort((a, b) => (b.created || 0) - (a.created || 0));
  return c;
}
function folderLabel(fid) {
  const chain = folderChain(fid);
  return chain.length ? chain.map((f) => f.name).join(" › ") : "";
}

// ---------- podświetlanie trafień ----------
function termRegex(terms) {
  if (!terms || !terms.length) return null;
  // początek słowa = rdzeń (prosta polska fleksja); lookbehind zamiast \b, bo \b nie zna polskich liter
  return new RegExp("(?<![\\p{L}\\p{N}_])(?:" + terms.map(escapeRx).join("|") + ")[\\p{L}\\p{N}_]*", "giu");
}
function highlightNodes(text, terms) {
  const rx = termRegex(terms);
  if (!rx) return [text];
  const out = [];
  let last = 0, m;
  while ((m = rx.exec(text))) {
    if (m[0] === "") { rx.lastIndex++; continue; }
    out.push(text.slice(last, m.index), h("mark", { class: "hit" }, m[0]));
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

// ---------- okna: nazwa, przenoszenie ----------
function openTextDialog({ title, label, value = "", confirmLabel = "Zapisz", maxlength = 120, hint, onSubmit }) {
  const input = h("input", { type: "text", maxlength: String(maxlength), "aria-label": label, style: "width:100%;margin-top:6px" });
  input.value = value;
  const err = h("div", { class: "errline", hidden: true });
  const submit = async () => {
    const v = input.value.trim();
    if (!v) { err.textContent = "Wpisz nazwę."; err.hidden = false; return; }
    try { await onSubmit(v); closeDialog(); } catch (e) { err.textContent = e.message; err.hidden = false; }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
  openDialog(h("h2", {}, title), h("label", { class: "small muted" }, label), input, hint ? h("div", { class: "small muted", style: "margin-top:8px" }, hint) : null, err,
    h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"), h("button", { class: "primary", onclick: submit }, confirmLabel)));
  setTimeout(() => { input.focus(); input.select(); }, 30);
}

function openMoveDialog({ title, subject, currentParent, exclude = new Set(), onPick }) {
  let selected = currentParent || "";
  const err = h("div", { class: "errline", hidden: true });
  const rows = [{ id: "", name: "Wszystkie wykłady (bez folderu)", depth: 0 }, ...state.folders.map((f) => ({ id: f.id, name: f.name, depth: f.depth, disabled: exclude.has(f.id) }))];
  const list = h("div", { class: "files", role: "radiogroup" }, rows.map((r) => {
    const radio = h("input", { type: "radio", name: "dest", value: r.id, checked: r.id === selected, disabled: !!r.disabled, onchange: () => { selected = r.id; } });
    return h("label", { class: "file", style: `padding-left:${8 + r.depth * 18}px;${r.disabled ? "opacity:.45;cursor:not-allowed" : ""}` }, radio,
      h("span", { class: "name", style: "font-family:var(--sans)" }, r.name));
  }));
  openDialog(h("h2", {}, title), h("p", { class: "small muted", style: "margin-top:0" }, subject),
    state.folders.length ? list : h("div", { class: "muted small" }, "Nie ma jeszcze żadnych folderów. Utwórz folder przyciskiem „Nowy folder” na liście."),
    err,
    h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"),
      h("button", { class: "primary", onclick: async () => {
        try { await onPick(selected || null); closeDialog(); } catch (e) { err.textContent = e.message; err.hidden = false; }
      } }, "Przenieś")));
}

function openRenameLecture(v) {
  openTextDialog({ title: "Zmień nazwę wykładu", label: "Nazwa wykładu", value: v.title || v.id, maxlength: 120,
    hint: "Zmienia się tylko wyświetlana nazwa. Plik wideo i dane przetwarzania zostają bez zmian.",
    onSubmit: async (title) => { await api(`/api/videos/${v.id}`, { method: "PATCH", body: { title } }); await refresh(); } });
}
function openMoveLecture(v) {
  openMoveDialog({ title: "Przenieś wykład", subject: v.title || v.id, currentParent: v.folder,
    onPick: async (folder) => { await api(`/api/videos/${v.id}`, { method: "PATCH", body: { folder } }); await refresh(); } });
}
function openNewFolder() {
  const parent = currentFolderId();
  openTextDialog({ title: "Nowy folder", label: parent ? `Nazwa folderu (w: ${folderLabel(parent)})` : "Nazwa folderu", maxlength: 60, confirmLabel: "Utwórz",
    onSubmit: async (name) => { await api("/api/folders", { method: "POST", body: { name, parent } }); await refresh(); } });
}
function openRenameFolder(f) {
  openTextDialog({ title: "Zmień nazwę folderu", label: "Nazwa folderu", value: f.name, maxlength: 60,
    onSubmit: async (name) => { await api(`/api/folders/${f.id}`, { method: "PATCH", body: { name } }); await refresh(); } });
}
function openMoveFolder(f) {
  openMoveDialog({ title: "Przenieś folder", subject: folderLabel(f.id), currentParent: f.parent, exclude: descendantIds(f.id),
    onPick: async (parent) => { await api(`/api/folders/${f.id}`, { method: "PATCH", body: { parent } }); await refresh(); } });
}
async function openDeleteFolder(f) {
  const n = lecturesIn(f.id).length, sub = childFolders(f.id).length;
  const ok = await confirmDialog({ title: "Usuń folder", danger: true, confirmLabel: "Usuń folder",
    text: `Folder „${f.name}” zostanie usunięty. Wykłady (${n}) i podfoldery (${sub}) NIE są usuwane: trafią do folderu nadrzędnego.` });
  if (!ok) return;
  try {
    await api(`/api/folders/${f.id}`, { method: "DELETE" });
    if (currentFolderId() === f.id) location.hash = f.parent ? `#/f/${f.parent}` : "#/";
    await refresh();
  } catch (e) { toast(e.message); }
}

async function moveLecture(id, folder) {
  try { await api(`/api/videos/${id}`, { method: "PATCH", body: { folder } }); await refresh(); } catch (e) { toast(e.message); }
}
async function moveFolderTo(id, parent) {
  try { await api(`/api/folders/${id}`, { method: "PATCH", body: { parent } }); await refresh(); } catch (e) { toast(e.message); }
}

// ---------- przeciąganie ----------
function dropTarget(el, folderId) {
  el.addEventListener("dragover", (e) => { if (libView.dragging) { e.preventDefault(); el.classList.add("drop"); } });
  el.addEventListener("dragleave", () => el.classList.remove("drop"));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("drop");
    const data = (e.dataTransfer && e.dataTransfer.getData("text/plain")) || "";
    const [kind, id] = data.split(":");
    libView.dragging = false;
    if (kind === "lecture") moveLecture(id, folderId);
    else if (kind === "folder" && id !== folderId) moveFolderTo(id, folderId);
  });
  return el;
}
function draggable(el, kind, id) {
  el.setAttribute("draggable", "true");
  el.addEventListener("dragstart", (e) => { libView.dragging = true; e.dataTransfer.setData("text/plain", `${kind}:${id}`); e.dataTransfer.effectAllowed = "move"; el.classList.add("dragging"); });
  el.addEventListener("dragend", () => { libView.dragging = false; el.classList.remove("dragging"); rerender(); });
  return el;
}

// ---------- elementy listy ----------
const linkBtn = (label, onclick, extra = {}) => h("button", Object.assign({ class: "linkbtn", onclick }, extra), label);

function lectureRow(v) {
  const job = activeJob(v.id);
  const run = Object.entries(v.stages).find(([, s]) => s.status === "running");
  const row = h("article", { class: "lecture" },
    h("div", { class: "lecture-head" },
      h("div", {},
        h("a", { class: "vtitle", href: `#/video/${v.id}` }, v.title || v.id),
        h("div", { class: "meta" }, [v.duration != null ? fmtDur(v.duration) : null, fmtDate(v.created), v.has_audio === false ? "bez audio" : null,
          v.video_exists ? null : "brak pliku wideo"].filter(Boolean).join("  ·  ")),
        h("div", { class: "linkrow" }, linkBtn("Zmień nazwę", () => openRenameLecture(v)), linkBtn("Przenieś", () => openMoveLecture(v)),
          linkBtn("Eksportuj", () => openExportDialog(v), { disabled: !!job || v.overall === "running", title: job ? "Poczekaj na koniec przetwarzania" : "Zapisz wykład jako plik .lec" }))),
      h("div", { class: "lecture-side" },
        job && job.status === "queued" ? chip("queued") : chip(v.overall === "idle" ? "pending" : v.overall, v.overall === "idle" ? STATUS_PL.idle : null),
        h("button", { class: "small danger", title: job ? "Najpierw anuluj zadanie" : "Usuń wykład", "aria-label": `Usuń wykład ${v.title || v.id}`,
          disabled: !!job || v.overall === "running", onclick: () => openDeleteDialog(v) }, "Usuń"))),
    trackOf(v),
    run && run[1].progress && run[1].progress.message ? h("div", { class: "meta", style: "margin-top:8px" }, progressText(run[0], run[1].progress)) : null,
    v.error && v.overall === "error" ? h("div", { class: "errline" }, v.error) : null);
  return draggable(row, "lecture", v.id);
}

function folderRow(f) {
  const ids = descendantIds(f.id);
  const total = state.videos.filter((v) => v.folder && ids.has(v.folder)).length;
  const subs = childFolders(f.id).length;
  const row = h("div", { class: "folder-row" },
    h("div", { class: "folder-main" },
      h("a", { class: "folder-name", href: `#/f/${f.id}` }, f.name),
      h("div", { class: "meta" }, `${total} ${total === 1 ? "wykład" : total >= 2 && total <= 4 ? "wykłady" : "wykładów"}` + (subs ? `  ·  podfoldery: ${subs}` : ""))),
    h("div", { class: "linkrow" }, linkBtn("Zmień nazwę", () => openRenameFolder(f)), linkBtn("Przenieś", () => openMoveFolder(f)), linkBtn("Usuń", () => openDeleteFolder(f), { class: "linkbtn danger" })));
  draggable(row, "folder", f.id);
  return dropTarget(row, f.id);
}

function crumbs(fid) {
  const parts = [dropTarget(h("a", { href: "#/", class: "crumb" }, "Wszystkie wykłady"), null)];
  for (const f of folderChain(fid)) {
    parts.push(h("span", { class: "sep" }, "›"), dropTarget(h("a", { href: `#/f/${f.id}`, class: "crumb" }, f.name), f.id));
  }
  return h("nav", { class: "crumbs", "aria-label": "Ścieżka folderów" }, parts);
}

// ---------- wyniki wyszukiwania ----------
function openHit(res, hit) {
  if (hit.kind === "notes") {
    window.pendingNotesFocus = { videoId: res.video_id, terms: libView.results.terms, heading: hit.heading };
    location.hash = `#/video/${res.video_id}/notes`;
  } else if (hit.t != null) {
    openContextDialog(res.video_id, hit.t);
  } else {
    location.hash = `#/video/${res.video_id}`;
  }
}

function resultsView() {
  const r = libView.results;
  if (!r) return h("div", { class: "empty" }, "Szukam…");
  if (r.error) return h("div", { class: "errline" }, r.error);
  if (!r.results.length) {
    return h("div", { class: "empty" }, `Brak wyników dla „${r.query}”.`,
      h("div", { class: "small", style: "margin-top:8px" }, "Spróbuj innej formy słowa (wyszukiwarka rozpoznaje odmiany), mniejszej liczby słów albo frazy w cudzysłowie."));
  }
  const kindName = { notes: "Notatki", transcript: "Mowa", board: "Tablica", title: "Tytuł" };
  return h("div", {},
    h("div", { class: "meta", style: "margin-bottom:10px" }, `${r.total_hits} trafień w ${r.results.length} ${r.results.length === 1 ? "wykładzie" : "wykładach"}`),
    h("div", { class: "results" }, r.results.map((res) => h("article", { class: "result" },
      h("div", { class: "result-head" },
        h("a", { class: "vtitle small-title", href: `#/video/${res.video_id}` }, highlightNodes(res.title, r.terms)),
        res.folder_path.length ? h("span", { class: "meta" }, res.folder_path.join(" › ")) : null),
      res.hits.map((hit) => h("a", { class: "hit-row", href: "#", onclick: (e) => { e.preventDefault(); openHit(res, hit); } },
        h("span", { class: "hit-kind" }, hit.kind === "notes" ? `${kindName.notes} · ${hit.heading}` : (hit.kind === "title" ? kindName.title : hit.label)),
        h("span", { class: "hit-snippet" }, highlightNodes(hit.snippet, r.terms))))))));
}

function onSearchInput(value) {
  libView.query = value;
  clearTimeout(libView.timer);
  if (value.trim().length < 2) { libView.results = null; libView.searching = false; libView.seq++; updateLibrary(); return; }
  libView.searching = true;
  libView.results = libView.results || null;
  updateLibrary();
  libView.timer = setTimeout(runSearch, 250);
}

async function runSearch() {
  const q = libView.query.trim();
  if (q.length < 2) return;
  const seq = ++libView.seq;
  const folder = libView.onlyHere ? currentFolderId() : null;
  let res;
  try { res = await api(`/api/search?q=${encodeURIComponent(q)}${folder ? `&folder=${folder}` : ""}`); } catch (e) { res = { error: e.message, results: [], query: q, terms: [] }; }
  if (seq !== libView.seq) return; // nowsze zapytanie wyprzedziło to
  libView.results = res;
  libView.searching = false;
  updateLibrary();
}

// ---------- widok biblioteki ----------
function mountLibrary(fid) {
  libView.mounted = fid;
  const search = h("input", { type: "search", id: "libsearch", class: "search", placeholder: "Szukaj w tytułach, notatkach, transkrypcjach i tablicach…",
    "aria-label": "Szukaj we wszystkich wykładach", autocomplete: "off", oninput: (e) => onSearchInput(e.target.value) });
  search.value = libView.query;
  search.addEventListener("keydown", (e) => { if (e.key === "Escape") { search.value = ""; onSearchInput(""); } });
  const sort = h("select", { "aria-label": "Sortowanie", onchange: (e) => { libView.sort = e.target.value; lsSet("lecturenoter.sort", libView.sort); updateLibrary(); } },
    h("option", { value: "new" }, "Najnowsze"), h("option", { value: "old" }, "Najstarsze"), h("option", { value: "name" }, "Nazwa A–Z"));
  sort.value = libView.sort;
  const only = h("label", { class: "row small muted", id: "libonly", hidden: !fid },
    h("input", { type: "checkbox", checked: libView.onlyHere, onchange: (e) => { libView.onlyHere = e.target.checked; if (libView.query.trim().length >= 2) { libView.searching = true; updateLibrary(); runSearch(); } } }), "Tylko w tym folderze");
  $("#view").replaceChildren(
    h("div", { id: "libroot" },
      h("div", { class: "page-head" },
        h("div", {}, h("h1", { id: "libtitle" }, "Wykłady"), h("p", { id: "libsub" }, "")),
        h("div", { class: "row" }, h("button", { onclick: () => openRefreshDialog(currentFolderId() || null),
          title: "Dodaje do kolejki wszystkie wykłady z nieaktualnymi etapami (w tym folderze i podfolderach)" }, "Zaktualizuj nieaktualne"),
          h("button", { onclick: openNewFolder }, "Nowy folder"), h("button", { onclick: pickImportFile, title: "Wczytaj wykład z pliku .lec (można też przeciągnąć plik na tę stronę)" }, "Importuj"),
          h("button", { class: "primary", onclick: openAddDialog }, "Dodaj nagranie"))),
      h("div", { class: "searchbar" }, search, sort),
      only,
      h("div", { id: "libcrumbs" }),
      h("div", { class: "layout", style: "margin-top:18px" }, h("div", { id: "libmain" }), h("div", { id: "libqueue" }))));
}

function renderLibrary() {
  const fid = currentFolderId() || "";
  if (fid && state.loaded && !folderById(fid)) { location.hash = "#/"; return; } // folder usunięty lub nieistniejący
  if (!$("#libroot") || libView.mounted !== fid) mountLibrary(fid);
  updateLibrary();
}

function updateLibrary() {
  if (libView.dragging || !$("#libroot")) return; // nie niszcz elementów w trakcie przeciągania
  const fid = currentFolderId();
  const folder = fid ? folderById(fid) : null;
  const searching = libView.query.trim().length >= 2;
  $("#libtitle").textContent = folder ? folder.name : "Wykłady";
  $("#libsub").textContent = folder ? "" : "Nagrania zamieniane w notatki, lokalnie na tym komputerze.";
  $("#libsub").hidden = !!folder;
  $("#libcrumbs").replaceChildren(...(searching ? [] : [crumbs(fid)]));
  $("#libonly").hidden = !fid;
  let main;
  if (searching) {
    main = resultsView();
  } else {
    const subs = childFolders(fid), lectures = sortVideos(lecturesIn(fid));
    const nothing = !subs.length && !lectures.length;
    main = h("div", {},
      subs.length ? h("div", { class: "folders" }, subs.map(folderRow)) : null,
      lectures.length ? dropTarget(h("div", { class: "lectures" }, lectures.map(lectureRow)), fid || null) : null,
      nothing ? h("div", { class: "empty" }, !state.loaded ? "Ładowanie…" : folder
        ? ["Ten folder jest pusty.", h("div", { class: "small", style: "margin-top:8px" }, "Przeciągnij tu wykład albo użyj przycisku „Przenieś” przy wykładzie.")]
        : state.videos.length ? "Wszystkie wykłady są w folderach."
          : ["Jeszcze nic tu nie ma.", h("div", { class: "small", style: "margin-top:8px" }, "Wrzuć nagranie do katalogu ", h("code", {}, "videos/"), " i dodaj je przyciskiem u góry.")]) : null);
  }
  $("#libmain").replaceChildren(main);
  $("#libqueue").replaceChildren(queuePanel());
}

// skrót "/" przenosi do wyszukiwarki
document.addEventListener("keydown", (e) => {
  const el = document.activeElement;
  const typing = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
  if (e.key === "/" && !typing && $("#libsearch")) { e.preventDefault(); $("#libsearch").focus(); }
});


// ---------- aktualizacja nieaktualnych etapów w całym folderze ----------
async function openRefreshDialog(fid) {
  const scope = fid ? `folderu „${(folderById(fid) || {}).name || ""}” (z podfolderami)` : "całej biblioteki";
  const body = h("div", { class: "muted" }, "Sprawdzam wykłady…");
  let incomplete = false, plan = null;
  const confirm = h("button", { class: "primary", disabled: true }, "Dodaj do kolejki");
  openDialog(h("h2", {}, "Zaktualizuj nieaktualne etapy"), body, h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"), confirm));

  async function load() {
    confirm.disabled = true;
    try { plan = await api("/api/refresh", { method: "POST", body: { folder: fid, include_incomplete: incomplete, commit: false } }); }
    catch (e) { body.textContent = e.message; return; }
    const names = (list) => list.map((s) => STAGE_NAME[s] || s).join(", ");
    body.replaceChildren(...kids(
      h("p", { class: "small", style: "margin-top:0" }, `Zakres: ${scope}. Dla każdego wykładu uruchomione zostaną etapy, których wynik jest nieaktualny (i zależne od nich), po jednym wykładzie naraz.`),
      h("label", { class: "row small", style: "gap:8px;margin-bottom:10px" },
        h("input", { type: "checkbox", checked: incomplete, onchange: (e) => { incomplete = e.target.checked; load(); } }),
        "Uwzględnij też niedokończone (oczekujące, przerwane lub zakończone błędem)"),
      plan.will_run.length ? h("div", {}, h("div", { style: "font-weight:600;margin-bottom:6px" }, `Do zaktualizowania: ${plan.will_run.length}`),
        h("div", { class: "refresh-list" }, plan.will_run.map((x) => h("div", { class: "small" }, h("strong", {}, x.title), h("span", { class: "muted" }, ` · ${names(x.stages)}`),
          x.has_edited_notes && x.stages.includes("llm") ? h("span", { class: "muted" }, " · ręcznie edytowane notatki zostaną zachowane") : null))))
        : h("div", { class: "empty", style: "padding:14px 0" }, plan.up_to_date ? `Wszystko aktualne (${plan.up_to_date} wykładów).` : "Brak wykładów w tym zakresie."),
      plan.skipped.length ? h("div", { style: "margin-top:12px" }, h("div", { class: "small", style: "font-weight:600" }, `Pominięte: ${plan.skipped.length}`),
        plan.skipped.map((x) => h("div", { class: "small muted" }, `${x.title}: ${x.reason}`))) : null));
    confirm.disabled = !plan.will_run.length;
    confirm.textContent = plan.will_run.length ? `Dodaj do kolejki (${plan.will_run.length})` : "Dodaj do kolejki";
  }
  confirm.onclick = async () => {
    confirm.disabled = true;
    try {
      const r = await api("/api/refresh", { method: "POST", body: { folder: fid, include_incomplete: incomplete, commit: true } });
      closeDialog();
      toast(r.queued ? `Dodano do kolejki: ${r.queued} ${r.queued === 1 ? "wykład" : "wykładów"}.` : "Nic nie dodano do kolejki.");
      await refresh();
    } catch (e) { toast(e.message); confirm.disabled = false; }
  };
  load();
}
