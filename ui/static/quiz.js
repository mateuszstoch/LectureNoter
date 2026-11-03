"use strict";
// Zakładka "Quiz": generowanie quizów z wykładu (opisowy, ABCD jedno-/wielokrotny wybór, definicja → nazwa),
// rozwiązywanie z natychmiastową informacją zwrotną, historia podejść i usuwanie. Korzysta z helperów app.js i notes.js.

const QUIZ_TYPES = {
  auto: { label: "Mieszany (automatyczny)", hint: "Model sam wybiera zagadnienia i najlepszy typ pytania do każdego z nich." },
  single: { label: "ABCD – jedna odpowiedź", hint: "Jedna poprawna odpowiedź spośród czterech." },
  multi: { label: "ABCD – wiele odpowiedzi", hint: "Poprawnych odpowiedzi może być kilka; trzeba zaznaczyć wszystkie." },
  term: { label: "Definicja → nazwa", hint: "Dostajesz opis pojęcia i podajesz, jak się nazywa." },
  open: { label: "Opisowy", hint: "Odpowiadasz własnymi słowami; ocenia lokalny model." },
};

function mountQuizTab(container, videoId) {
  const MODEL_KEY = "lecturenoter.chatModel";
  let list = [], view = { name: "list" }, pollTimer = null;
  const root = h("div", { class: "quiz" });
  container.replaceChildren(root);
  const base = `/api/videos/${videoId}/quizzes`;

  const md = (cls, text) => { const el = h("div", { class: `${cls} notes-body` }); el.innerHTML = renderMarkdownHtml(text || ""); return el; };
  const nq = (n) => `${n} ${n === 1 ? "pytanie" : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) ? "pytania" : "pytań"}`;
  const pct = (v) => (v == null ? "–" : `${Math.round(v)}%`);
  const typeLabel = (t) => (QUIZ_TYPES[t] ? QUIZ_TYPES[t].label : t);

  const paint = (...items) => root.replaceChildren(...kids(items)); // tablice i puste elementy zamiast tekstu "[object ...]"

  function go(next) { view = next; render(); }

  // ---------- lista ----------
  async function loadList() {
    try { list = await api(base); } catch (e) { toast(e.message); return; }
    if (view.name === "list") renderList();
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (list.some((q) => q.status === "generating") && view.name === "list") {
      pollTimer = setTimeout(() => { if (container.isConnected) loadList(); }, 2000);
    }
  }

  function scopeText(s) {
    if (!s) return "cały wykład";
    const parts = [];
    if (s.sections && s.sections.length) parts.push(`sekcje: ${s.sections.join(", ")}`);
    if (s.topic) parts.push(`temat: ${s.topic}`);
    if (s.adaptive) parts.push("powtórka słabych stron");
    return parts.join("; ") || "cały wykład";
  }

  function renderList() {
    paint(
      h("div", { class: "row spread", style: "margin-bottom:12px" },
        h("h2", { style: "margin:0" }, "Quizy z tego wykładu"),
        h("button", { class: "primary", onclick: () => go({ name: "new" }) }, "Nowy quiz")),
      list.length ? list.map(quizCard) : h("div", { class: "empty" }, "Nie ma jeszcze quizów. Wygeneruj pierwszy: wybierz typ pytań, liczbę i (opcjonalnie) obszar wykładu.",
        h("div", { class: "muted small", style: "margin-top:8px" }, "Zaliczenie quizu (domyślnie 80% poprawnych odpowiedzi) ma potwierdzać, że rozumiesz materiał.")));
    schedulePoll();
  }

  function quizCard(q) {
    const ready = q.status === "ready";
    const status = q.status === "generating"
      ? h("span", { class: "chip running" }, `Generowanie ${q.progress ? `${q.progress.done}/${q.progress.total}` : ""}`)
      : q.status === "error" ? h("span", { class: "chip error" }, "Błąd")
        : q.attempts ? h("span", { class: `chip ${q.passed ? "ok" : "stale"}` }, q.passed ? "Zaliczony" : "Niezaliczony") : null;
    return h("div", { class: "card quiz-card" },
      h("div", { class: "row spread" },
        h("div", { style: "min-width:0" },
          ready ? h("a", { href: "#", class: "vtitle", onclick: (e) => { e.preventDefault(); openQuiz(q.id); } }, q.title) : h("span", { class: "vtitle" }, q.title),
          h("div", { class: "muted small" }, [typeLabel(q.type), ready ? nq(q.count) : null, scopeText(q.scope), fmtDate(q.created)].filter(Boolean).join("  ·  "))),
        status),
      q.status === "error" ? h("div", { class: "small", style: "color:var(--accent);margin-top:6px" }, q.error) : null,
      q.warning ? h("div", { class: "small", style: "color:var(--warn);margin-top:6px" }, q.warning) : null,
      ready ? h("div", { class: "muted small", style: "margin-top:6px" },
        q.attempts ? `Podejścia: ${q.attempts} · najlepszy wynik: ${pct(q.best)} · ostatni: ${pct(q.last)}` : "Jeszcze nie rozwiązywany.") : null,
      h("div", { class: "row", style: "margin-top:10px" },
        ready ? h("button", { class: "primary small", onclick: () => startAttempt(q.id) }, q.attempts ? "Powtórz" : "Rozpocznij") : null,
        ready ? h("button", { class: "small", onclick: () => openQuiz(q.id) }, "Szczegóły") : null,
        h("button", { class: "small danger", onclick: () => delQuiz(q) }, q.status === "generating" ? "Anuluj" : "Usuń")));
  }

  async function delQuiz(q) {
    const ok = await confirmDialog({ title: q.status === "generating" ? "Anuluj generowanie" : "Usuń quiz",
      text: `„${q.title}” zostanie trwale usunięty razem z historią podejść.`, confirmLabel: "Usuń", danger: true });
    if (!ok) return;
    try { await api(`${base}/${q.id}`, { method: "DELETE" }); } catch (e) { toast(e.message); }
    list = list.filter((x) => x.id !== q.id); // znika od razu, bez przeładowania strony
    if (view.name !== "list") view = { name: "list" };
    render();
    loadList();
  }

  // ---------- nowy quiz ----------
  async function renderNew() {
    let scopes = [], weak = [];
    try { scopes = await api(`/api/videos/${videoId}/quiz-scopes`); } catch (_) { /* brak notatek: tylko temat */ }
    try { weak = await api(`/api/videos/${videoId}/quiz-weak`); } catch (_) { /* brak historii */ }
    let type = "auto";
    const allowed = new Set(["term", "single", "multi", "open"]);
    const allowedBox = h("div", { class: "row small", style: "gap:14px;margin-top:8px;flex-wrap:wrap" });
    const paintAllowed = () => {
      allowedBox.hidden = type !== "auto";
      allowedBox.replaceChildren(h("span", { class: "muted" }, "Dozwolone typy:"), ...["term", "single", "multi", "open"].map((k) =>
        h("label", { class: "row", style: "gap:5px" }, h("input", { type: "checkbox", checked: allowed.has(k), onchange: (e) => { if (e.target.checked) allowed.add(k); else allowed.delete(k); } }),
          QUIZ_TYPES[k].label)));
    };
    const adaptive = h("input", { type: "checkbox", disabled: !weak.length });
    const typeBox = h("div", { class: "quiz-types" });
    const paintTypes = () => typeBox.replaceChildren(...Object.entries(QUIZ_TYPES).map(([k, t]) =>
      h("label", { class: `opt${type === k ? " on" : ""}` },
        h("input", { type: "radio", name: "qtype", value: k, checked: type === k, onchange: () => { type = k; paintTypes(); paintAllowed(); } }),
        h("span", {}, h("strong", {}, t.label), h("span", { class: "muted small", style: "display:block" }, t.hint)))));
    paintTypes();
    paintAllowed();
    const count = h("input", { type: "number", min: "1", max: "30", value: "10", style: "width:80px", "aria-label": "Liczba pytań" });
    const topic = h("input", { type: "text", placeholder: "np. maszyna Turinga albo problemy NP-zupełne", style: "width:100%", "aria-label": "Obszar" });
    const title = h("input", { type: "text", placeholder: "(opcjonalnie) własna nazwa", style: "width:100%", "aria-label": "Nazwa quizu" });
    const model = h("select", { "aria-label": "Model" });
    const installed = ((state.system && state.system.ollama && state.system.ollama.installed) || []);
    const def = state.system && state.system.models ? state.system.models.llm : null;
    const opts = installed.length ? installed : (def ? [def] : []);
    model.replaceChildren(...opts.map((m) => h("option", { value: m }, m)));
    const saved = localStorage.getItem(MODEL_KEY);
    model.value = opts.includes(saved) ? saved : (opts.includes(def) ? def : opts[0] || "");
    const boxes = scopes.map((s) => h("input", { type: "checkbox", value: s }));
    const go_ = h("button", { class: "primary", onclick: submit }, "Generuj quiz");
    async function submit() {
      if (type === "auto" && !allowed.size) { toast("Wybierz co najmniej jeden dozwolony typ pytań."); return; }
      const n = parseInt(count.value, 10);
      if (!(n >= 1 && n <= 30)) { toast("Liczba pytań musi być z zakresu 1-30."); return; }
      go_.disabled = true;
      try {
        await api(base, { method: "POST", body: { type, count: n, types: type === "auto" ? [...allowed] : null, adaptive: adaptive.checked, topic: topic.value.trim(), title: title.value.trim() || null,
          sections: boxes.filter((b) => b.checked).map((b) => b.value), model: model.value || null } });
        go({ name: "list" });
        loadList();
      } catch (e) { toast(e.message); go_.disabled = false; }
    }
    paint(
      h("h2", {}, "Nowy quiz"),
      h("div", { class: "card" },
        h("div", { class: "field-label" }, "Typ pytań"), typeBox, allowedBox,
        h("div", { class: "field-label" }, "Liczba pytań"), count,
        h("div", { class: "field-label" }, "Obszar wykładu ", h("span", { class: "muted small" }, "(puste = cały wykład)")),
        topic,
        scopes.length ? h("details", { style: "margin-top:8px" }, h("summary", { class: "small" }, `Wybierz sekcje notatek (${scopes.length})`),
          h("div", { class: "quiz-scopes" }, scopes.map((s, i) => h("label", { class: "row small", style: "gap:8px" }, boxes[i], s)))) : null,
        h("label", { class: "row", style: "gap:8px;margin-top:12px" }, adaptive,
          h("span", {}, "Skup się na moich słabych stronach ", h("span", { class: "muted small" },
            weak.length ? `(${weak.length} zagadnień do powtórki: ${weak.slice(0, 3).map((w) => w.topic).join("; ")}${weak.length > 3 ? "…" : ""})` : "(dostępne po rozwiązaniu quizu z błędami)"))),
        h("div", { class: "field-label" }, "Nazwa"), title,
        h("div", { class: "field-label" }, "Model"), model,
        h("div", { class: "muted small", style: "margin:10px 0" }, "Pytania powstają lokalnie z notatek i transkryptu. Generowanie zajmuje model (GPU) na kilkadziesiąt sekund do kilku minut; możesz w tym czasie przeglądać inne zakładki."),
        h("div", { class: "row" }, go_, h("button", { onclick: () => go({ name: "list" }) }, "Anuluj"))));
  }

  // ---------- szczegóły quizu ----------
  async function openQuiz(id) {
    let quiz;
    try { quiz = await api(`${base}/${id}`); } catch (e) { toast(e.message); return; }
    go({ name: "overview", quiz });
  }

  function renderOverview() {
    const q = view.quiz, s = q.summary;
    const done = (q.attempts || []).filter((a) => a.finished).slice().reverse();
    paint(
      h("div", { class: "row spread" }, h("a", { href: "#", onclick: (e) => { e.preventDefault(); go({ name: "list" }); loadList(); } }, "← Wszystkie quizy"),
        h("button", { class: "small danger", onclick: () => delQuiz(s) }, "Usuń quiz")),
      h("h2", { style: "margin-top:14px" }, q.title),
      h("div", { class: "muted small" }, [typeLabel(q.type), nq(q.questions.length), scopeText(q.scope), q.model].filter(Boolean).join("  ·  ")),
      q.warning ? h("div", { class: "banner warn", style: "margin-top:8px" }, q.warning) : null,
      h("div", { class: "card", style: "margin-top:12px" },
        h("div", { class: "row spread" },
          h("div", {}, h("div", { class: "quiz-score" }, pct(s.best)), h("div", { class: "muted small" }, `najlepszy wynik · próg zaliczenia ${Math.round(q.pass_ratio * 100)}%`)),
          s.attempts ? h("span", { class: `chip ${s.passed ? "ok" : "stale"}` }, s.passed ? "Zaliczony" : "Niezaliczony") : null),
        h("div", { style: "margin-top:12px" }, h("button", { class: "primary", onclick: () => startAttempt(q.id) }, s.attempts ? "Powtórz quiz" : "Rozpocznij quiz"))),
      done.length ? h("div", {}, h("h2", { style: "margin-top:18px" }, "Historia podejść"),
        done.map((a) => h("div", { class: "row spread quiz-hist" },
          h("span", {}, `${fmtDate(a.finished)} · `, h("strong", {}, pct(a.percent)), a.percent >= q.pass_ratio * 100 ? " · zaliczone" : ""),
          h("button", { class: "small", onclick: () => go({ name: "result", quiz: q, attempt: a, review: true }) }, "Przejrzyj")))) : null);
  }

  // ---------- rozwiązywanie ----------
  async function startAttempt(id) {
    let quiz, attempt;
    try {
      quiz = await api(`${base}/${id}`);
      attempt = await api(`${base}/${id}/attempts`, { method: "POST", body: {} });
    } catch (e) { toast(e.message); return; }
    const order = quiz.questions.map((_, i) => i).sort(() => Math.random() - 0.5);
    go({ name: "run", quiz, attempt, order, idx: 0, answers: {} });
  }

  function choiceReview(q, selected, optOrder) {
    const sel = new Set(selected || []);
    return h("div", { class: "quiz-opts" }, optOrder.map((oi, pos) => {
      const o = q.options[oi], chosen = sel.has(oi);
      const cls = o.correct ? "ok" : chosen ? "bad" : "";
      return h("div", { class: `qopt ${cls}${chosen ? " chosen" : ""}` },
        h("div", { class: "row", style: "gap:8px;align-items:baseline" },
          h("span", { class: "qmark" }, o.correct ? "✓" : chosen ? "✗" : "·"),
          h("strong", {}, `${"ABCDEF"[pos]}. `), h("span", {}, o.text)),
        o.why ? h("div", { class: "qwhy small" }, (o.correct ? "Poprawna: " : "Błędna: ") + o.why) : null,
        chosen ? h("div", { class: "muted small qwhy" }, "Twój wybór") : null);
    }));
  }

  function verdict(res, q) {
    const s = res.score;
    if (res.graded === false) return h("div", { class: "qverdict warn" }, "Nie udało się automatycznie ocenić odpowiedzi.");
    return h("div", { class: `qverdict ${s === 1 ? "ok" : s === 0.5 ? "warn" : "bad"}` }, s === 1 ? "Dobrze!" : s === 0.5 ? "Częściowo poprawnie" : "Niepoprawnie");
  }

  // Informacja zwrotna po sprawdzeniu pytania (także w przeglądzie podejścia): poprawna odpowiedź i uzasadnienie.
  function feedback(q, res, optOrder, onSelf) {
    const parts = [verdict(res, q)];
    if (q.type === "single" || q.type === "multi") {
      parts.push(choiceReview(q, res.answer, optOrder));
    } else if (q.type === "term") {
      parts.push(h("div", { class: "qans" }, h("span", { class: "muted small" }, "Twoja odpowiedź: "), h("strong", {}, res.answer || "–")));
      parts.push(h("div", { class: "qans" }, h("span", { class: "muted small" }, "Poprawna nazwa: "), h("strong", {}, q.term),
        q.aliases && q.aliases.length ? h("span", { class: "muted small" }, `  (akceptowane też: ${q.aliases.join(", ")})`) : null));
      if (q.explanation) parts.push(md("qwhy small", q.explanation));
      if (!res.correct && onSelf) parts.push(h("button", { class: "small", onclick: () => onSelf(1) }, "Uznaj moją odpowiedź za poprawną (synonim)"));
    } else {
      parts.push(h("div", { class: "qans" }, h("div", { class: "muted small" }, "Twoja odpowiedź:"), h("div", { class: "qyours" }, res.answer || "–")));
      if (res.feedback) parts.push(h("div", { class: "qwhy" }, h("strong", {}, "Ocena: "), res.feedback));
      if (res.graded === false) {
        parts.push(h("div", { class: "small", style: "color:var(--warn)" }, res.error || ""));
        if (onSelf) parts.push(h("div", { class: "row" }, h("span", { class: "muted small" }, "Oceń sam:"),
          h("button", { class: "small", onclick: () => onSelf(1) }, "Poprawna"), h("button", { class: "small", onclick: () => onSelf(0.5) }, "Częściowo"),
          h("button", { class: "small", onclick: () => onSelf(0) }, "Błędna")));
      }
      parts.push(h("div", { class: "qans" }, h("div", { class: "muted small" }, "Wzorcowa odpowiedź:"), md("qmodel", q.model_answer),
        q.key_points && q.key_points.length ? h("ul", { class: "small" }, q.key_points.map((k) => h("li", {}, k))) : null));
    }
    return h("div", { class: "qfeedback" }, parts);
  }

  function renderRun() {
    const { quiz, attempt, order } = view;
    const total = quiz.questions.length;
    const q = quiz.questions[order[view.idx]];
    const res = view.answers[q.id];
    const optOrder = view.optOrders && view.optOrders[q.id] || (q.options ? q.options.map((_, i) => i).sort(() => Math.random() - 0.5) : []);
    (view.optOrders = view.optOrders || {})[q.id] = optOrder;
    const checks = [];
    const input = answerInput(q, optOrder, checks);
    const submitBtn = h("button", { class: "primary", onclick: () => submit(q, input, submitBtn, optOrder) }, q.type === "open" ? "Sprawdź i oceń" : "Sprawdź");
    const last = view.idx === total - 1;
    const answered = Object.keys(view.answers).length;
    paint(
      h("div", { class: "row spread" },
        h("a", { href: "#", onclick: async (e) => { e.preventDefault(); if (await confirmDialog({ title: "Przerwać quiz?", text: "Odpowiedzi z tego podejścia nie zostaną zapisane w historii.", confirmLabel: "Przerwij", danger: true })) { go({ name: "list" }); loadList(); } } }, "← Przerwij"),
        h("span", { class: "muted small" }, `Pytanie ${view.idx + 1} z ${total}`)),
      h("div", { class: "qbar", role: "progressbar", "aria-valuenow": String(answered), "aria-valuemax": String(total) }, h("div", { style: `width:${Math.round(100 * answered / total)}%` })),
      h("div", { class: "card qcard" },
        q.section ? h("div", { class: "muted small", style: "margin-bottom:6px" }, q.section) : null,
        q.type === "term" ? h("div", { class: "small muted" }, "Jak nazywa się pojęcie opisane poniżej?") : null,
        md("qtext", q.question),
        res ? (q.type === "single" || q.type === "multi" ? null : null) : input.el,
        res ? feedback(q, res, optOrder, (score) => submit(q, null, null, optOrder, score)) : h("div", { class: "row", style: "margin-top:12px" }, submitBtn),
        res ? h("div", { class: "row", style: "margin-top:14px;justify-content:flex-end" },
          h("button", { class: "primary", onclick: () => (last ? finish() : go({ ...view, idx: view.idx + 1 })) }, last ? "Zakończ i zobacz wynik" : "Dalej →")) : null));
    const f = root.querySelector("textarea, input[type=text]");
    if (f) f.focus();
  }

  function answerInput(q, optOrder) {
    if (q.type === "single" || q.type === "multi") {
      const multi = q.type === "multi";
      const inputs = optOrder.map((oi) => h("input", { type: multi ? "checkbox" : "radio", name: "ans", value: String(oi) }));
      const el = h("div", { class: "quiz-opts" },
        h("div", { class: "muted small" }, multi ? "Zaznacz wszystkie poprawne odpowiedzi." : "Zaznacz jedną odpowiedź."),
        optOrder.map((oi, pos) => h("label", { class: "qopt pick" }, inputs[pos], h("strong", {}, `${"ABCDEF"[pos]}. `), h("span", {}, q.options[oi].text))));
      return { el, value: () => inputs.filter((i) => i.checked).map((i) => +i.value) };
    }
    if (q.type === "term") {
      const t = h("input", { type: "text", class: "qinput", placeholder: "Wpisz nazwę pojęcia…", "aria-label": "Odpowiedź",
        onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); t.closest(".qcard").querySelector("button.primary").click(); } } });
      return { el: t, value: () => t.value.trim() };
    }
    const t = h("textarea", { class: "qinput", rows: "5", placeholder: "Odpowiedz własnymi słowami…", "aria-label": "Odpowiedź" });
    return { el: t, value: () => t.value.trim() };
  }

  async function submit(q, input, btn, optOrder, selfScore) {
    const prev = view.answers[q.id];
    const answer = selfScore != null ? (prev ? prev.answer : "") : input.value();
    if (selfScore == null && (Array.isArray(answer) ? !answer.length : !answer)) { toast("Najpierw odpowiedz na pytanie."); return; }
    if (btn) { btn.disabled = true; btn.textContent = q.type === "open" ? "Model ocenia odpowiedź…" : "Sprawdzam…"; }
    const body = { question_id: q.id, answer };
    if (selfScore != null) body.self_score = selfScore;
    try {
      const r = await api(`${base}/${view.quiz.id}/attempts/${view.attempt.id}/answer`, { method: "POST", body });
      const res = { ...r.result, answer };
      if (prev && selfScore != null) { res.graded = true; delete res.error; }
      view.answers[q.id] = res;
      view.finishedAttempt = r.attempt;
      renderRun();
    } catch (e) { toast(e.message); if (btn) { btn.disabled = false; btn.textContent = "Sprawdź"; } }
  }

  async function finish() {
    let quiz;
    try { quiz = await api(`${base}/${view.quiz.id}`); } catch (e) { toast(e.message); return; }
    const attempt = quiz.attempts.find((a) => a.id === view.attempt.id);
    go({ name: "result", quiz, attempt, orders: view.optOrders });
  }

  // ---------- wynik ----------
  function renderResult() {
    const { quiz, attempt } = view;
    const passed = attempt.percent >= quiz.pass_ratio * 100;
    const total = quiz.questions.length;
    const good = quiz.questions.filter((q) => (attempt.answers[q.id] || {}).score === 1).length;
    const order = (q) => (view.orders && view.orders[q.id]) || (q.options ? q.options.map((_, i) => i) : []);
    paint(
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); if (view.review) openQuiz(quiz.id); else { go({ name: "list" }); loadList(); } } }, view.review ? "← Szczegóły quizu" : "← Wszystkie quizy"),
      h("div", { class: "card quiz-result", style: "margin-top:12px" },
        h("div", { class: "quiz-score" }, pct(attempt.percent)),
        h("div", { class: `qverdict ${passed ? "ok" : "bad"}` }, passed ? "Quiz zaliczony" : "Quiz niezaliczony"),
        h("div", { class: "muted small" }, `${good} z ${total} pytań w pełni poprawnie · próg zaliczenia ${Math.round(quiz.pass_ratio * 100)}%`),
        passed ? h("div", { class: "small", style: "margin-top:6px" }, "Wynik sugeruje, że rozumiesz materiał tego wykładu.")
          : h("div", { class: "small", style: "margin-top:6px" }, "Przejrzyj błędne odpowiedzi poniżej, wróć do notatek i spróbuj ponownie."),
        h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "primary", onclick: () => startAttempt(quiz.id) }, "Powtórz quiz"))),
      h("h2", { style: "margin-top:18px" }, "Przegląd odpowiedzi"),
      quiz.questions.map((q, i) => {
        const res = attempt.answers[q.id];
        if (!res) return null;
        return h("details", { class: "card qrev", open: res.score !== 1 },
          h("summary", {}, h("span", { class: `qmark ${res.score === 1 ? "ok" : res.score === 0.5 ? "warn" : "bad"}` }, res.score === 1 ? "✓" : res.score === 0.5 ? "½" : "✗"),
            ` ${i + 1}. `, (q.question || "").slice(0, 110) + ((q.question || "").length > 110 ? "…" : "")),
          md("qtext", q.question), feedback(q, res, order(q), null));
      }));
  }

  function render() {
    clearTimeout(pollTimer);
    if (view.name === "list") renderList();
    else if (view.name === "new") renderNew();
    else if (view.name === "overview") renderOverview();
    else if (view.name === "run") renderRun();
    else if (view.name === "result") renderResult();
    if (view.name !== "list") window.scrollTo({ top: 0 });
  }

  activeTab = {
    onState() { /* quizy odświeżają się same (odpytywanie podczas generowania) */ },
    dirty: () => false,
  };
  renderList();
  loadList();
}
