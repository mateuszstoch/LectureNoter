"use strict";
// Zakładka "Słownik": pojęcia wykładu z definicjami, szybkie przejścia do notatek, czatu i quizu.

// Tekst z wzorami (KaTeX) i Markdownem: pojęcia, definicje i fiszki zawierają notację matematyczną ($...$).
function mathEl(cls, text) {
  const el = h("div", { class: `${cls} notes-body math-inline` });
  el.innerHTML = renderMarkdownHtml(text || "");
  return el;
}

function mountGlossaryTab(container, videoId) {
  const root = h("div", {});
  container.replaceChildren(root);
  let data = { terms: [], source: "none" }, query = "";
  const list = h("div", { class: "gloss-list" });
  const search = h("input", { type: "text", placeholder: "Szukaj pojęcia…", "aria-label": "Szukaj w słowniku", oninput: (e) => { query = e.target.value.trim().toLowerCase(); paint(); } });

  function goChat(term) {
    window.pendingChatPrompt = { videoId, text: `Wyjaśnij pojęcie: ${term}. Podaj definicję i przykład z wykładu.` };
    location.hash = `#/video/${videoId}/chat`;
  }
  function goQuiz(term) {
    window.pendingQuizTopic = { videoId, topic: term };
    location.hash = `#/video/${videoId}/quiz`;
  }
  function goNotes(term) {
    window.pendingNotesFocus = { videoId, terms: [term] };
    location.hash = `#/video/${videoId}/notes`;
  }

  function paint() {
    const shown = data.terms.filter((t) => !query || t.term.toLowerCase().includes(query) || t.definition.toLowerCase().includes(query));
    list.replaceChildren(...kids(shown.length ? shown.map((t) => h("div", { class: "card gloss-item" },
      mathEl("gloss-term", t.term), mathEl("gloss-def", t.definition),
      h("div", { class: "row", style: "margin-top:8px;gap:6px" },
        h("button", { class: "small", onclick: () => goNotes(t.term) }, "W notatkach"),
        h("button", { class: "small", onclick: () => goChat(t.term) }, "Zapytaj"),
        h("button", { class: "small", onclick: () => goQuiz(t.term) }, "Quiz z pojęcia"),
        typeof addFlashcard === "function" ? h("button", { class: "small", onclick: () => addFlashcard(videoId, t.term, t.definition, "słownik") }, "+ Fiszka") : null))) : [
      h("div", { class: "empty" }, data.terms.length ? "Nic nie pasuje do wyszukiwania." : "Brak słownika dla tego wykładu.",
        data.terms.length ? null : h("div", { class: "muted small", style: "margin-top:6px" }, "Słownik powstaje przy generowaniu notatek (szkielet wykładu). Możesz go wygenerować teraz modelem na podstawie notatek."))]));
  }

  async function generate(btn) {
    btn.disabled = true;
    btn.textContent = "Model układa słownik…";
    try { data = await api(`/api/videos/${videoId}/glossary`, { method: "POST", body: {} }); } catch (e) { toast(e.message); }
    render();
  }

  function render() {
    const genBtn = h("button", { class: "small", onclick: (e) => generate(e.target) }, data.source === "model" ? "Wygeneruj ponownie" : "Wygeneruj modelem");
    root.replaceChildren(h("div", { class: "row spread", style: "margin-bottom:12px" },
      h("div", { class: "row" }, search, h("span", { class: "muted small" }, data.terms.length ? `${data.terms.length} pojęć` : "")), genBtn), list);
    paint();
  }

  api(`/api/videos/${videoId}/glossary`).then((d) => { data = d; render(); }).catch((e) => { root.textContent = e.message; });
  render();
  activeTab = { onState() {}, dirty: () => false };
}

// ---------- fiszki ----------
async function addFlashcard(videoId, front, back, source) {
  try {
    const r = await api(`/api/videos/${videoId}/flashcards`, { method: "POST", body: { front, back, source } });
    toast(r.added ? "Dodano fiszkę." : "Taka fiszka już istnieje.");
  } catch (e) { toast(e.message); }
}

function fmtDue(ts) {
  const d = ts - Date.now() / 1000;
  if (d <= 0) return "do powtórki teraz";
  if (d < 3600) return `za ${Math.max(1, Math.round(d / 60))} min`;
  if (d < 86400) return `za ${Math.round(d / 3600)} godz.`;
  return `za ${Math.round(d / 86400)} dni`;
}

function mountFlashcardsTab(container, videoId) {
  const root = h("div", {});
  container.replaceChildren(root);
  const base = `/api/videos/${videoId}/flashcards`;
  let data = { cards: [], total: 0, due: 0 }, session = null; // session: { queue, idx, shown, done }

  async function call(path, opts) {
    try { const r = await api(path, opts); if (r.cards) data = r; return r; } catch (e) { toast(e.message); return null; }
  }
  async function load() { await call(base); session ? renderSession() : render(); }

  async function bulk(path, label) {
    const r = await call(`${base}/${path}`, { method: "POST", body: {} });
    if (r) { toast(r.added ? `${label}: dodano ${r.added}.` : `${label}: nic nowego do dodania.`); render(); }
  }

  function startSession() {
    const now = Date.now() / 1000;
    session = { queue: data.cards.filter((c) => c.due <= now), idx: 0, shown: false, done: 0 };
    renderSession();
  }

  function nextHint(c, g) { // przybliżony odstęp po ocenie (tak jak w serwerze)
    if (g === 0) return "10 min";
    const e = c.ease || 2.5, i = c.interval || 0, r = c.reps || 0;
    const days = g === 1 ? Math.max(1, i * 1.2) : g === 2 ? (r === 0 ? 1 : r === 1 ? 3 : i * e) : (r === 0 ? 4 : Math.max(5, i * e * 1.3));
    return days < 1.5 ? "1 dzień" : `${Math.round(days)} dni`;
  }

  function renderSession() {
    const c = session.queue[session.idx];
    if (!c) {
      root.replaceChildren(h("div", { class: "card quiz-result" }, h("div", { class: "quiz-score" }, `${session.done}`), h("div", { class: "muted" }, "fiszek powtórzonych w tej sesji"),
        h("div", { class: "row", style: "justify-content:center;margin-top:12px" }, h("button", { class: "primary", onclick: () => { session = null; load(); } }, "Wróć do fiszek"))));
      return;
    }
    const grade = async (g) => {
      const r = await call(`${base}/${c.id}/review`, { method: "POST", body: { grade: g } });
      if (!r) return;
      session.done++;
      if (g === 0) session.queue.push(r.card);   // nie pamiętam: karta wróci jeszcze w tej sesji
      session.idx++; session.shown = false;
      renderSession();
    };
    root.replaceChildren(
      h("div", { class: "row spread" }, h("a", { href: "#", onclick: (e) => { e.preventDefault(); session = null; load(); } }, "← Zakończ sesję"),
        h("span", { class: "muted small" }, `Fiszka ${session.idx + 1} z ${session.queue.length}`)),
      h("div", { class: "card qcard", style: "margin-top:12px" },
        c.source ? h("div", { class: "muted small", style: "margin-bottom:6px" }, c.source) : null,
        mathEl("qtext", c.front),
        session.shown ? h("div", { class: "qfeedback" }, h("div", { class: "qyours" }, mathEl("", c.back)),
          h("div", { class: "row", style: "gap:8px;margin-top:8px;flex-wrap:wrap" },
            [[0, "Nie pamiętam"], [1, "Trudne"], [2, "Dobrze"], [3, "Łatwo"]].map(([g, l]) =>
              h("button", { class: g === 2 ? "primary" : "", onclick: () => grade(g) }, `${l} · ${nextHint(c, g)}`))))
          : h("div", { style: "margin-top:12px" }, h("button", { class: "primary", onclick: () => { session.shown = true; renderSession(); } }, "Pokaż odpowiedź"))));
  }

  function render() {
    const front = h("input", { type: "text", placeholder: "Awers (pytanie / pojęcie)", style: "width:100%", "aria-label": "Awers" });
    const back = h("textarea", { rows: "2", placeholder: "Rewers (odpowiedź)", style: "width:100%", "aria-label": "Rewers" });
    root.replaceChildren(
      h("div", { class: "row spread", style: "margin-bottom:12px;flex-wrap:wrap" },
        h("div", {}, h("span", { class: "quiz-score", style: "font-size:30px" }, `${data.due}`), h("span", { class: "muted" }, ` do powtórki · ${data.total} fiszek`)),
        h("div", { class: "row", style: "flex-wrap:wrap" },
          h("button", { class: "primary", disabled: !data.due, onclick: startSession }, data.due ? `Ucz się (${data.due})` : "Brak fiszek do powtórki"),
          h("button", { class: "small", onclick: () => bulk("from-glossary", "Ze słownika") }, "Dodaj ze słownika"),
          h("button", { class: "small", onclick: () => bulk("from-quizzes", "Z quizów") }, "Dodaj błędne z quizów"),
          h("a", { class: `btn small${data.total ? "" : " disabled"}`, href: data.total ? `${base}/export` : "#", download: `${videoId}-fiszki-anki.txt`,
            title: "Plik do importu w Anki (Plik → Importuj)" }, "Eksport do Anki"))),
      h("details", { class: "card", style: "margin-bottom:12px" }, h("summary", { class: "small" }, "Dodaj własną fiszkę"),
        h("div", { style: "display:grid;gap:8px;margin-top:8px" }, front, back,
          h("div", {}, h("button", { class: "small", onclick: async () => {
            if (!front.value.trim() || !back.value.trim()) { toast("Wpisz awers i rewers."); return; }
            const r = await call(base, { method: "POST", body: { front: front.value, back: back.value, source: "ręczna" } });
            if (r) { toast(r.added ? "Dodano fiszkę." : "Taka fiszka już istnieje."); render(); }
          } }, "Dodaj")))),
      data.cards.length ? h("div", { class: "gloss-list" }, data.cards.map((c) => h("div", { class: "card gloss-item" },
        mathEl("gloss-term", c.front), mathEl("gloss-def", c.back),
        h("div", { class: "row spread", style: "margin-top:8px" }, h("span", { class: "muted small" }, `${c.source || ""} · ${fmtDue(c.due)}`),
          h("button", { class: "small danger", onclick: async () => { if (await confirmDialog({ title: "Usuń fiszkę", text: "Fiszka zostanie usunięta.", confirmLabel: "Usuń", danger: true })) { await call(`${base}/${c.id}`, { method: "DELETE" }); render(); } } }, "Usuń")))))
        : h("div", { class: "empty" }, "Brak fiszek. Dodaj je ze słownika wykładu lub z błędnych odpowiedzi w quizach."));
  }

  render();
  load();
  activeTab = { onState() {}, dirty: () => false };
}

// ---------- przegląd postępów ----------
function folderPathName(id) {
  const parts = [];
  for (let f = state.folders.find((x) => x.id === id), n = 0; f && n < 6; f = state.folders.find((x) => x.id === f.parent), n++) parts.unshift(f.name);
  return parts.join(" / ");
}

async function mountProgressView() {
  const view = $("#view");
  activeTab = null;
  view.replaceChildren(h("div", { class: "muted" }, "Ładowanie…"));
  let rows;
  try { rows = await api("/api/progress"); } catch (e) { view.textContent = e.message; return; }
  let folder = "", sort = "attention";
  const desc = (id) => { const ids = new Set([id]); let grew = true; while (grew) { grew = false; state.folders.forEach((f) => { if (ids.has(f.parent) && !ids.has(f.id)) { ids.add(f.id); grew = true; } }); } return ids; };
  const body = h("div", {});
  function paint() {
    const ids = folder ? desc(folder) : null;
    let shown = rows.filter((r) => !ids || ids.has(r.folder));
    const att = (r) => (r.cards_due ? 2 : 0) + (r.weak.length ? 2 : 0) + (r.quizzes && !r.passed ? 1 : 0) + (!r.quizzes ? 1 : 0);
    shown = shown.slice().sort(sort === "title" ? (a, b) => (a.title || "").localeCompare(b.title || "", "pl") : (a, b) => att(b) - att(a));
    const withQuiz = shown.filter((r) => r.quizzes), passed = shown.filter((r) => r.passed);
    body.replaceChildren(...kids(
      h("div", { class: "row", style: "gap:28px;margin:6px 0 16px;flex-wrap:wrap" },
        [[`${passed.length}/${shown.length}`, "wykładów z zaliczonym quizem"], [`${withQuiz.length}`, "z jakimkolwiek quizem"], [`${shown.reduce((s, r) => s + r.cards_due, 0)}`, "fiszek do powtórki"],
          [`${shown.reduce((s, r) => s + r.attempts, 0)}`, "podejść do quizów"]].map(([n, l]) => h("div", {}, h("div", { class: "quiz-score", style: "font-size:30px" }, n), h("div", { class: "muted small" }, l)))),
      shown.length ? shown.map((r) => h("div", { class: "card", style: "margin-bottom:10px" },
        h("div", { class: "row spread" },
          h("div", { style: "min-width:0" }, h("a", { class: "vtitle", href: `#/video/${r.id}/quiz` }, r.title || r.id),
            h("div", { class: "muted small" }, [r.folder ? folderPathName(r.folder) : "bez folderu", r.quizzes ? `quizy: ${r.quizzes}` : "brak quizów", r.attempts ? `podejścia: ${r.attempts}` : null,
              r.last_attempt ? `ostatnio ${fmtDate(r.last_attempt)}` : null].filter(Boolean).join("  ·  "))),
          h("div", { class: "row" }, r.best != null ? h("span", { class: `chip ${r.passed ? "ok" : "stale"}` }, `${Math.round(r.best)}% · ${r.passed ? "zaliczony" : "niezaliczony"}`) : h("span", { class: "chip" }, "bez quizu"))),
        r.weak.length ? h("div", { class: "small", style: "margin-top:6px" }, h("span", { class: "muted" }, "Do powtórki: "), r.weak.join("; ")) : null,
        h("div", { class: "row", style: "margin-top:8px" },
          h("a", { class: "btn small", href: `#/video/${r.id}/quiz` }, r.quizzes ? "Quizy" : "Zrób quiz"),
          h("a", { class: "btn small", href: `#/video/${r.id}/flashcards` }, r.cards_due ? `Fiszki (${r.cards_due} do powtórki)` : `Fiszki (${r.cards})`),
          h("a", { class: "btn small", href: `#/video/${r.id}/notes` }, "Notatki")))) : h("div", { class: "empty" }, "Brak wykładów w tym folderze.")));
  }
  const fsel = h("select", { "aria-label": "Folder", onchange: (e) => { folder = e.target.value; paint(); } }, h("option", { value: "" }, "Wszystkie wykłady"),
    state.folders.map((f) => h("option", { value: f.id }, folderPathName(f.id))));
  const ssel = h("select", { "aria-label": "Sortowanie", onchange: (e) => { sort = e.target.value; paint(); } },
    h("option", { value: "attention" }, "Wymagające uwagi najpierw"), h("option", { value: "title" }, "Tytuł"));
  view.replaceChildren(h("div", { class: "row spread", style: "flex-wrap:wrap" }, h("h1", {}, "Postępy w nauce"), h("div", { class: "row" }, fsel, ssel)), body);
  paint();
}

// ---------- czat po wszystkich wykładach ----------
function mountGlobalChat() {
  const view = $("#view");
  activeTab = null;
  view.classList.add("wide");
  const content = h("div", {});
  view.replaceChildren(h("h1", {}, "Czat po wszystkich wykładach"),
    h("p", { class: "muted", style: "margin:-6px 0 16px" }, "Pytania do notatek całej biblioteki lub wybranego folderu."), content);
  mountChatTab(content, "_all");
}

// ---------- diagnostyka środowiska i pobieranie modeli ----------
let envTimer = null;

async function mountEnvironmentView() {
  const view = $("#view");
  activeTab = null;
  view.classList.remove("wide");
  clearTimeout(envTimer);
  const list = h("div", {});
  view.replaceChildren(h("div", { class: "row spread", style: "flex-wrap:wrap" }, h("h1", {}, "Diagnostyka"),
    h("button", { class: "small", onclick: () => load() }, "Sprawdź ponownie")),
    h("p", { class: "muted", style: "margin:-6px 0 16px" }, "Czy środowisko ma wszystko, czego potrzebuje aplikacja. Brakujące modele można pobrać jednym kliknięciem."), list);

  async function pull(model) {
    try { await api("/api/models/pull", { method: "POST", body: { model } }); } catch (e) { toast(e.message); }
    load();
  }

  async function load() {
    clearTimeout(envTimer);
    let d;
    try { d = await api("/api/environment"); } catch (e) { list.textContent = e.message; return; }
    if (location.hash !== "#/environment") return;
    const label = { ok: "ok", warn: "uwaga", fail: "brak" }, cls = { ok: "ok", warn: "stale", fail: "error" };
    list.replaceChildren(...kids(d.checks.map((c) => {
      const p = c.model ? d.pulls[c.model] : null;
      const running = p && !p.done;
      return h("div", { class: "card env-row" },
        h("div", { class: "row spread" }, h("div", { style: "min-width:0" }, h("strong", {}, c.label), h("div", { class: "muted small", style: "overflow-wrap:anywhere" }, c.detail)),
          h("div", { class: "row" },
            c.model && c.status !== "ok" ? h("button", { class: "small primary", disabled: !!running, onclick: () => pull(c.model) }, running ? "Pobieranie…" : "Pobierz") : null,
            h("span", { class: `chip ${cls[c.status]}` }, label[c.status]))),
        p ? h("div", { style: "margin-top:8px" },
          p.error ? h("div", { class: "small", style: "color:var(--accent)" }, `Błąd: ${p.error}`)
            : h("div", {}, h("div", { class: "qbar", style: "margin:4px 0" }, h("div", { style: `width:${Math.round(p.percent || 0)}%` })),
              h("div", { class: "muted small" }, p.done ? "Pobrano." : `${p.status || ""} ${p.percent ? Math.round(p.percent) + "%" : ""}`))) : null);
    })));
    if (Object.values(d.pulls).some((p) => !p.done)) envTimer = setTimeout(load, 1500);
  }
  load();
}
