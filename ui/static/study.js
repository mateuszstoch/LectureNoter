"use strict";
// Zakładka "Słownik": pojęcia wykładu z definicjami, szybkie przejścia do notatek, czatu i quizu.

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
      h("div", { class: "gloss-term" }, t.term), h("div", { class: "gloss-def" }, t.definition),
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
