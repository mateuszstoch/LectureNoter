"use strict";
// Zakładka "Czat": rozmowa z lokalnym LLM o notatkach i transkrypcie jednego filmu. Korzysta z helperów app.js i notes.js.

// Podpowiedzi z wykładu (słowa kluczowe i pytania): zapisane przez model albo, gdy ich brak, z nagłówków notatek.
async function fetchSuggestions(videoId) {
  try { return await api(`/api/videos/${videoId}/suggestions`); } catch (_) { return { keywords: [], questions: [], source: "none" }; }
}
async function regenerateSuggestions(videoId) {
  return api(`/api/videos/${videoId}/suggestions`, { method: "POST", body: {} });
}

function mountChatTab(container, videoId) {
  let chats = [], current = null, streaming = null; // streaming: { controller }
  let sugg = { keywords: [], questions: [], source: "none" };
  const isAll = videoId === "_all";               // czat po wszystkich wykładach (lub wybranym folderze)
  const scopeSel = h("select", { "aria-label": "Zakres czatu", title: "Z jakich wykładów korzysta nowa rozmowa", style: "width:100%;margin-bottom:8px" },
    h("option", { value: "" }, "Wszystkie wykłady"), ...(state.folders || []).map((f) => h("option", { value: f.id }, `Folder: ${folderPathName(f.id)}`)));
  const MODEL_KEY = "lecturenoter.chatModel";
  const root = h("div", { class: "chat-layout" });
  container.replaceChildren(root);

  const side = h("aside", { class: "chat-side" });
  const main = h("section", { class: "chat-main" });
  root.append(side, main);

  const msgBox = h("div", { class: "chat-msgs", "aria-live": "polite" });
  const input = h("textarea", { class: "chat-input", rows: "2", placeholder: (videoId === "_all" ? "Zadaj pytanie o swoje wykłady… " : "Zadaj pytanie o ten wykład… ") + "(Enter wysyła, Shift+Enter nowa linia)", "aria-label": "Pytanie" });
  const sendBtn = h("button", { class: "primary", onclick: () => send() }, "Wyślij");
  const stopBtn = h("button", { class: "danger", hidden: true, onclick: () => stop() }, "Stop");
  const modelSel = h("select", { "aria-label": "Model czatu", onchange: () => localStorage.setItem(MODEL_KEY, modelSel.value) });
  const statusEl = h("span", { class: "muted small" });
  const notice = h("div", { class: "banner warn", hidden: true, style: "margin:0 0 8px" });
  const suggBox = h("details", { class: "sugg", hidden: true });
  const releaseBtn = h("button", { class: "small", title: "Rozładuj model czatu z pamięci GPU", onclick: releaseGpu }, "Zwolnij GPU");
  const mdLink = h("a", { class: "btn small", href: "#", download: "rozmowa.md", title: "Pobierz rozmowę jako Markdown" }, "Markdown");
  const pdfBtn = h("button", { class: "small", title: "Otwiera okno drukowania: wybierz „Zapisz jako PDF”", onclick: () => printChat() }, "PDF");

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });

  main.append(
    h("div", { class: "row spread", style: "margin-bottom:8px" },
      h("div", { class: "row" }, h("span", { class: "muted small" }, "Model:"), modelSel),
      h("div", { class: "row" }, statusEl, releaseBtn, h("span", { class: "muted small" }, "Eksport:"), mdLink, pdfBtn)),
    notice, suggBox, msgBox,
    h("div", { class: "chat-form" }, input, h("div", { class: "row", style: "justify-content:flex-end" }, stopBtn, sendBtn)),
    h("div", { class: "muted small", style: "margin-top:6px" }, isAll ? "Odpowiedzi opierają się na notatkach wybranych wykładów (mogą zawierać błędy); przy każdym fragmencie widać, z którego wykładu pochodzi. Wszystko działa lokalnie." : "Odpowiedzi opierają się na automatycznie wygenerowanych notatkach i transkrypcie (mogą zawierać błędy). Wszystko działa lokalnie."));

  function paintSuggestions() {
    const fill = (text) => { input.value = text; input.focus(); };
    const has = sugg.keywords.length || sugg.questions.length;
    suggBox.hidden = !has;
    suggBox.replaceChildren(...kids(
      h("summary", { class: "small muted" }, "Podpowiedzi z tego wykładu"),
      sugg.keywords.length ? h("div", { class: "row sugg-kw" }, h("span", { class: "muted small" }, "Pojęcia:"),
        sugg.keywords.map((k) => h("button", { class: "small", title: "Wstaw pytanie o to pojęcie", onclick: () => fill(`Wyjaśnij pojęcie: ${k}`) }, k))) : null,
      sugg.questions.length ? h("div", { class: "sugg-q" }, sugg.questions.map((q) => h("button", { class: "small", title: "Wstaw pytanie", onclick: () => fill(q) }, q))) : null,
      sugg.source !== "model" ? h("div", { class: "small muted", style: "margin-top:6px" }, "Podpowiedzi powstały z nagłówków notatek. ",
        h("a", { href: "#", onclick: async (e) => {
          e.preventDefault();
          try { toast("Model układa podpowiedzi…"); sugg = await regenerateSuggestions(videoId); paintSuggestions(); renderMessages(); } catch (err) { toast(err.message); }
        } }, "Wygeneruj lepsze modelem")) : null));
  }

  function fillModels() {
    const sys = state.system;
    const installed = (sys && sys.ollama && sys.ollama.installed) || [];
    const def = sys && sys.models ? sys.models.llm : null;
    const saved = localStorage.getItem(MODEL_KEY);
    const options = installed.length ? installed : (def ? [def] : []);
    modelSel.replaceChildren(...options.map((m) => h("option", { value: m }, m)));
    modelSel.value = options.includes(saved) ? saved : (options.includes(def) ? def : options[0] || "");
  }

  function gpuBusyBy() {
    const g = state.gpu;
    return g && g.owner !== "chat" ? g.owner : null;
  }

  function refreshControls() {
    const busy = gpuBusyBy();
    const job = activeJob(videoId);
    const off = !!busy || !!streaming;
    input.disabled = !!busy;
    sendBtn.disabled = off;
    stopBtn.hidden = !streaming;
    sendBtn.hidden = !!streaming;
    modelSel.disabled = !!streaming;
    notice.hidden = !busy;
    if (busy) notice.textContent = `GPU jest zajęte przez: ${busy}${job ? " (przetwarzanie tego filmu)" : ""}. Czat będzie dostępny po zakończeniu zadania.`;
    const g = state.gpu;
    statusEl.textContent = g && g.owner === "chat" ? "model załadowany w GPU" : "model nie jest załadowany";
    releaseBtn.hidden = !(g && g.owner === "chat") || !!streaming;
  }

  // ---------- lista rozmów ----------
  async function loadChats(selectId) {
    try { chats = await api(`/api/videos/${videoId}/chats`); } catch (e) { toast(e.message); return; }
    const want = selectId || (current && chats.find((c) => c.id === current.id) ? current.id : chats[0] && chats[0].id);
    if (want) await openChat(want); else { current = null; renderSide(); renderMessages(); }
  }

  function renderSide() {
    side.replaceChildren(...kids(
      isAll ? scopeSel : null,
      h("button", { class: "primary", disabled: !!streaming, onclick: newChat }, "Nowa rozmowa"),
      h("div", { class: "chat-list" }, chats.length ? chats.map((c) => h("div", { class: `chat-item${current && current.id === c.id ? " active" : ""}` },
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); if (!streaming) openChat(c.id); }, title: c.title }, c.title,
          isAll ? h("span", { class: "muted small", style: "display:block" }, c.scope ? `Folder: ${folderPathName(c.scope) || "usunięty"}` : "Wszystkie wykłady") : null),
        h("button", { class: "small danger", title: "Usuń rozmowę", disabled: !!streaming, onclick: () => delChat(c.id) }, "✕")))
        : h("div", { class: "muted small", style: "padding:8px 2px" }, "Brak rozmów."))));
  }

  async function openChat(id) {
    try { current = await api(`/api/videos/${videoId}/chats/${id}`); } catch (e) { toast(e.message); return; }
    renderSide();
    renderMessages();
  }
  async function newChat() {
    try { const c = await api(`/api/videos/${videoId}/chats`, { method: "POST", body: isAll ? { scope: scopeSel.value || null } : {} }); await loadChats(c.id); input.focus(); } catch (e) { toast(e.message); }
  }
  async function delChat(id) {
    const title = (chats.find((c) => c.id === id) || {}).title || "ta rozmowa";
    const ok = await confirmDialog({ title: "Usuń rozmowę", text: `„${title}” zostanie trwale usunięta. Możesz ją wcześniej wyeksportować (Markdown lub PDF).`,
      confirmLabel: "Usuń", danger: true });
    if (!ok) return;
    try { await api(`/api/videos/${videoId}/chats/${id}`, { method: "DELETE" }); if (current && current.id === id) current = null; await loadChats(); } catch (e) { toast(e.message); }
  }
  async function releaseGpu() {
    try { await api("/api/chat/release", { method: "POST" }); } catch (e) { toast(e.message); }
  }

  // ---------- eksport ----------
  function refreshExport() {
    const has = !!(current && current.messages.length);
    mdLink.setAttribute("href", has ? `/api/videos/${videoId}/chats/${current.id}/export?format=md` : "#");
    mdLink.setAttribute("download", has ? `${videoId}-rozmowa-${current.id}.md` : "rozmowa.md");
    mdLink.classList.toggle("disabled", !has);
    mdLink.onclick = (e) => { if (!has) e.preventDefault(); };
    pdfBtn.disabled = !has;
  }

  function printChat() {
    if (!current || !current.messages.length) return;
    const area = h("article", { id: "print-area", class: "print-chat" },
      h("h1", {}, current.title),
      h("div", { class: "meta" }, [isAll ? "Wszystkie wykłady" : (videoById(videoId) || {}).title, current.model, fmtDate(current.created)].filter(Boolean).join("  ·  ")),
      current.messages.map((m) => {
        const body = h("div", { class: "pc-body notes-body" });
        if (m.role === "user") body.textContent = m.content; else setAssistant(body, m.content);
        const src = (m.sources || []).map((s) => (s.kind === "notes" ? `Notatki: ${s.label}` : s.label)).join("; ");
        return h("section", { class: `pc-msg ${m.role}` }, h("div", { class: "who" }, m.role === "user" ? "Ty" : "Asystent"), body,
          src ? h("div", { class: "pc-src" }, `Użyte fragmenty: ${src}`) : null);
      }));
    document.body.append(area);
    document.body.classList.add("printing");
    const cleanup = () => { document.body.classList.remove("printing"); area.remove(); window.removeEventListener("afterprint", cleanup); };
    window.addEventListener("afterprint", cleanup);
    window.print();
  }

  // ---------- wiadomości ----------
  function sourceChips(sources) {
    if (!sources || !sources.length) return null;
    return h("details", { class: "sources" }, h("summary", { class: "small muted" }, `Użyte fragmenty (${sources.length})`),
      h("div", { class: "row", style: "gap:6px;margin-top:6px" }, sources.map((s) => (s.t != null
        ? h("button", { class: "small", title: "Pokaż kontekst", onclick: () => openContextDialog(videoId, s.t) }, s.label)
        : s.kind === "lecture"
          ? h("button", { class: "small", title: "Otwórz notatki tego wykładu", onclick: () => {
            window.pendingNotesFocus = { videoId: s.video_id, terms: [], heading: s.section };
            location.hash = `#/video/${s.video_id}/notes`;
          } }, s.label)
          : h("span", { class: "chip" }, `Notatki: ${s.label}`)))));
  }

  function bubble(m) {
    if (m.role === "user") return h("div", { class: "msg user" }, h("div", { class: "who" }, "Ty"), h("div", { class: "bubble" }, m.content));
    const body = h("div", { class: "bubble md notes-body" });
    setAssistant(body, m.content);
    return h("div", { class: "msg assistant" }, h("div", { class: "who" }, "Asystent"), body, m.stopped ? h("div", { class: "muted small" }, "odpowiedź przerwana") : null, sourceChips(m.sources),
      m.model ? h("div", { class: "muted small" }, m.model) : null);
  }

  function setAssistant(el, text) {
    el.innerHTML = renderMarkdownHtml(text || "");
    el.querySelectorAll("a[href^=http]").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    linkifyTimestamps(el, (t) => openContextDialog(videoId, t));
  }

  function renderMessages() {
    if (!current) {
      msgBox.replaceChildren(h("div", { class: "empty" }, "Wybierz rozmowę z listy albo zacznij nową, wpisując pytanie poniżej."));
    } else if (!current.messages.length) {
      msgBox.replaceChildren(h("div", { class: "empty" }, "Zadaj pytanie o ten wykład.",
        h("div", { class: "row", style: "margin-top:12px;gap:8px" },
          (sugg.questions.length ? sugg.questions.slice(0, 4) : isAll
            ? ["Porównaj, jak poszczególne wykłady wyjaśniają to samo pojęcie", "Które wykłady dotyczą algorytmów i co w nich jest najważniejsze?", "Zrób listę najważniejszych definicji ze wszystkich wykładów"]
            : ["Podsumuj ten wykład w punktach", "Jakie są najważniejsze definicje?"]).map((q) =>
            h("button", { class: "small", onclick: () => { input.value = q; send(); } }, q)))));
    } else {
      msgBox.replaceChildren(...current.messages.map(bubble));
    }
    msgBox.scrollTop = msgBox.scrollHeight;
    suggBox.open = !(current && current.messages.length); // rozwinięte, dopóki rozmowa jest pusta
    refreshControls();
    refreshExport();
  }

  // ---------- wysyłanie i strumień ----------
  async function stop() { if (streaming) streaming.controller.abort(); }

  async function send() {
    const text = input.value.trim();
    if (!text || streaming || gpuBusyBy()) return;
    if (!current) {
      try { current = await api(`/api/videos/${videoId}/chats`, { method: "POST", body: {} }); } catch (e) { toast(e.message); return; }
    }
    input.value = "";
    const chatId = current.id;
    current.messages.push({ role: "user", content: text });
    const body = h("div", { class: "bubble md notes-body" }, h("span", { class: "muted" }, "Ładowanie modelu i szukanie fragmentów…"));
    const srcBox = h("div", {});
    msgBox.querySelector(".empty")?.remove();
    msgBox.append(h("div", { class: "msg user" }, h("div", { class: "who" }, "Ty"), h("div", { class: "bubble" }, text)),
      h("div", { class: "msg assistant" }, h("div", { class: "who" }, "Asystent"), body, srcBox));
    msgBox.scrollTop = msgBox.scrollHeight;
    streaming = { controller: new AbortController() };
    refreshControls();
    renderSide();
    let answer = "", sources = [], errored = null, pending = false;
    const paint = () => {
      pending = false;
      setAssistant(body, answer);
      const near = msgBox.scrollHeight - msgBox.scrollTop - msgBox.clientHeight < 120;
      if (near) msgBox.scrollTop = msgBox.scrollHeight;
    };
    try {
      const r = await fetch(`/api/videos/${videoId}/chats/${chatId}/messages`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ content: text, model: modelSel.value || null }), signal: streaming.controller.signal,
      });
      if (!r.ok) { let d = null; try { d = (await r.json()).detail; } catch (_) { /* brak */ } throw new Error(d || `Błąd ${r.status}`); }
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2);
          const nl = block.indexOf("\n");
          const ev = block.slice(7, nl), data = JSON.parse(block.slice(nl + 7));
          if (ev === "sources") { sources = data; srcBox.replaceChildren(...[sourceChips(data)].filter(Boolean)); }
          else if (ev === "token") { answer += data; if (!pending) { pending = true; setTimeout(paint, 60); } }
          else if (ev === "error") errored = data;
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") errored = e.message;
    } finally {
      streaming = null;
      paint();
    }
    if (errored) toast(errored);
    await loadChats(chatId); // zsynchronizuj z zapisem serwera (czesciowa odpowiedz, tytul)
    await loadSystemSoon();
  }

  async function loadSystemSoon() { try { state.system = await api("/api/system"); } catch (_) { /* pomin */ } fillModels(); refreshControls(); }

  activeTab = {
    onState() {
      const n = ((state.system && state.system.ollama && state.system.ollama.installed) || []).length;
      if (!modelSel.options.length || (!streaming && n && modelSel.options.length !== n)) fillModels();
      refreshControls();
    },
    dirty: () => false,
  };
  fillModels();
  renderSide();
  renderMessages();
  loadChats();
  if (window.pendingChatPrompt && window.pendingChatPrompt.videoId === videoId) { // np. z zaznaczonego fragmentu notatek lub słownika
    input.value = window.pendingChatPrompt.text;
    window.pendingChatPrompt = null;
    setTimeout(() => input.focus(), 50);
  }
  if (!isAll) fetchSuggestions(videoId).then((d) => { sugg = d; paintSuggestions(); renderMessages(); });
}
