"use strict";
// Zakładki "Notatki" i "Materiały" widoku filmu. Korzysta z pomocników z app.js (h, api, kids, toast, openDialog...).

let activeTab = null; // { onState(v) } dla aktualnie otwartej zakładki

// ---------- Markdown + LaTeX ----------
function renderMarkdownHtml(md) {
  const math = [];
  const stash = (tex, display) => { math.push({ tex: tex.trim(), display }); return `@@M${math.length - 1}@@`; };
  const parts = md.split(/(```[\s\S]*?```|`[^`\n]*`)/g); // kod pomijamy przy wyszukiwaniu wzorów
  const protectedMd = parts.map((p, i) => (i % 2 ? p : p
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => stash(t, true))
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, t) => stash(t, true))
    .replace(/\\\((.+?)\\\)/g, (_, t) => stash(t, false))
    .replace(/(?<![\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])/g, (_, t) => stash(t, false)))).join("");
  const clean = DOMPurify.sanitize(marked.parse(protectedMd, { gfm: true }));
  return clean.replace(/@@M(\d+)@@/g, (_, i) => {
    const m = math[+i];
    try { return katex.renderToString(m.tex, { displayMode: m.display, throwOnError: false, strict: false }); } catch (_) { return h("code", {}, m.tex).outerHTML; }
  });
}

function tsToSec(hh, mm, ss) { return +hh * 3600 + +mm * 60 + +ss; }

function linkifyTimestamps(root, onClick) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest("pre, code, a, .katex, mark") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  const re = /\[(\d{1,2}):(\d{2}):(\d{2})\]/g;
  for (const n of nodes) {
    if (!re.test(n.nodeValue)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(n.nodeValue))) {
      frag.append(n.nodeValue.slice(last, m.index));
      const t = tsToSec(m[1], m[2], m[3]);
      frag.append(h("a", { class: "ts", href: "#", title: "Pokaż tablicę i mowę z tego momentu", onclick: (e) => { e.preventDefault(); onClick(t); } }, m[0]));
      last = m.index + m[0].length;
    }
    frag.append(n.nodeValue.slice(last));
    n.replaceWith(frag);
  }
}

function renderNotesInto(el, md, videoId) {
  el.innerHTML = renderMarkdownHtml(md);
  el.querySelectorAll("a[href^=http]").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
  const toc = [];
  el.querySelectorAll("h1, h2, h3").forEach((hd, i) => {
    hd.id = `sec-${i}`;
    toc.push({ id: hd.id, level: +hd.tagName[1], text: hd.textContent });
  });
  if (videoId) linkifyTimestamps(el, (t) => openContextDialog(videoId, t));
  return toc;
}

function highlight(root, query) {
  root.querySelectorAll("mark.hit").forEach((m) => m.replaceWith(document.createTextNode(m.textContent)));
  root.normalize();
  if (!query) return [];
  const q = query.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest(".katex, mark") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const low = n.nodeValue.toLowerCase();
    let i = low.indexOf(q);
    if (i < 0) continue;
    const frag = document.createDocumentFragment();
    let last = 0;
    while (i >= 0) {
      frag.append(n.nodeValue.slice(last, i), h("mark", { class: "hit" }, n.nodeValue.slice(i, i + q.length)));
      last = i + q.length;
      i = low.indexOf(q, last);
    }
    frag.append(n.nodeValue.slice(last));
    n.replaceWith(frag);
  }
  return [...root.querySelectorAll("mark.hit")];
}

// ---------- okno: moment wykładu (klatka + mowa) ----------
async function openContextDialog(videoId, t) {
  const body = h("div", {}, h("div", { class: "muted" }, "Ładowanie…"));
  openDialog(h("h2", {}, `Moment ${fmtDur(t)}`), body, h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Zamknij")));
  let c;
  try { c = await api(`/api/videos/${videoId}/context?t=${t}`); } catch (e) { body.textContent = e.message; return; }
  const segs = c.transcript.map((s) => {
    const near = t >= s.start && t <= s.end;
    return h("div", { class: `seg${near ? " now" : ""}` }, h("span", { class: "muted small" }, `[${fmtDur(s.start)}] `), s.text);
  });
  body.replaceChildren(
    c.frame ? h("div", {},
      h("img", { class: "frame-img", src: `/api/videos/${videoId}/frames/${c.frame.file}`, alt: "Tablica", loading: "lazy" }),
      h("div", { class: "muted small" }, `Stan tablicy zapisany o ${fmtDur(c.frame.timestamp)}`),
      c.frame.text ? h("details", {}, h("summary", { class: "small" }, "Odczytany tekst (VLM)"), h("pre", { class: "vlm" }, c.frame.text)) : null)
      : h("div", { class: "muted small" }, "Brak klatek tablicy dla tego filmu."),
    h("h3", { style: "margin:14px 0 6px;font-size:14px" }, "Mowa wokół tego momentu"),
    segs.length ? h("div", { class: "segs" }, segs) : h("div", { class: "muted small" }, "Brak transkryptu."));
  const now = body.querySelector(".seg.now");
  if (now) now.scrollIntoView({ block: "center" });
}

// ---------- zakładka: Notatki ----------
function mountNotesTab(container, videoId) {
  let data = null, mode = "view", draft = "", dirty = false, hits = [], hitIdx = -1;
  let sys = state.system;
  const root = h("div", {});
  container.replaceChildren(root);

  async function load() {
    try {
      data = await api(`/api/videos/${videoId}/notes`);
    } catch (e) {
      data = null;
      root.replaceChildren(h("div", { class: "empty" }, e.message, h("div", { style: "margin-top:8px" }, h("a", { href: `#/video/${videoId}` }, "Przejdź do przetwarzania"))));
      return;
    }
    show();
  }

  function toolbar() {
    const pandoc = sys && sys.pandoc;
    return h("div", { class: "toolbar" },
      h("div", { class: "row" },
        chip(data.source === "edited" ? "stale" : "done", data.source === "edited" ? "wersja edytowana" : "wersja wygenerowana"),
        h("span", { class: "muted small" }, fmtDate(data.updated))),
      h("div", { class: "row" },
        h("input", { type: "text", id: "nsearch", placeholder: "Szukaj w notatkach…", "aria-label": "Szukaj w notatkach", oninput: (e) => doSearch(e.target.value), onkeydown: (e) => { if (e.key === "Enter") nextHit(); } }),
        h("span", { class: "muted small", id: "nhits" }),
        h("button", { onclick: startEdit }, "Edytuj"),
        h("button", { onclick: () => window.print(), title: "Otwiera okno drukowania: wybierz „Zapisz jako PDF”" }, "PDF"),
        h("a", { class: "btn", href: `/api/videos/${videoId}/notes/export?format=md`, download: `${videoId}.md` }, "Markdown"),
        h("button", { disabled: !pandoc, title: pandoc ? "" : "Wymaga programu pandoc (sudo apt install pandoc)", onclick: () => { location.href = `/api/videos/${videoId}/notes/export?format=docx`; } }, "Word"),
        h("button", { onclick: openVersions }, "Historia"),
        data.has_edited && data.has_generated ? h("button", { class: "danger", onclick: confirmRevert }, "Wróć do wygenerowanej") : null));
  }

  function show() {
    mode = "view";
    const body = h("article", { class: "notes-body", id: "nbody" });
    const toc = renderNotesInto(body, data.md, videoId);
    const tocEl = h("nav", { class: "toc", "aria-label": "Spis treści" }, h("div", { class: "label" }, "Spis treści"),
      toc.filter((t) => t.level <= 3).map((t) => h("a", { class: `toc-l${t.level}`, href: "#", onclick: (e) => { e.preventDefault(); document.getElementById(t.id).scrollIntoView({ behavior: "smooth", block: "start" }); } }, t.text)));
    const q = data.quality;
    const bad = q && q.degraded_chunks && q.degraded_chunks.length;
    const qBanner = bad ? h("div", { class: "banner warn" },
      `W ${q.degraded_chunks.length} z ${q.chunks} fragmentów model zapętlił się i powtórzenia zostały usunięte automatycznie, ale treść tych fragmentów (nr ${q.degraded_chunks.join(", ")}) może być niepełna. `,
      "Możesz wygenerować notatki ponownie (Przetwarzanie → Notatki (LLM) → Regeneruj).") : null;
    root.replaceChildren(...kids(toolbar(), qBanner, h("div", { class: "notes-layout" }, tocEl, body)));
  }

  function doSearch(q) {
    hits = highlight($("#nbody"), q.trim());
    hitIdx = -1;
    $("#nhits").textContent = q.trim() ? `${hits.length} trafień` : "";
    if (hits.length) nextHit();
  }
  function nextHit() {
    if (!hits.length) return;
    if (hitIdx >= 0) hits[hitIdx].classList.remove("cur");
    hitIdx = (hitIdx + 1) % hits.length;
    hits[hitIdx].classList.add("cur");
    hits[hitIdx].scrollIntoView({ block: "center" });
    $("#nhits").textContent = `${hitIdx + 1} / ${hits.length}`;
  }

  // --- edycja ---
  function startEdit(initial) {
    mode = "edit";
    draft = typeof initial === "string" ? initial : data.md;
    dirty = typeof initial === "string";
    const ta = h("textarea", { class: "editor", id: "ned", spellcheck: "false", "aria-label": "Edytor notatek" });
    ta.value = draft;
    const preview = h("article", { class: "notes-body preview" });
    let timer = null;
    const upd = () => { renderNotesInto(preview, ta.value, null); };
    ta.addEventListener("input", () => { dirty = true; draft = ta.value; clearTimeout(timer); timer = setTimeout(upd, 250); });
    upd();
    const save = async () => {
      try {
        data = await api(`/api/videos/${videoId}/notes`, { method: "PUT", body: { md: ta.value } });
        dirty = false;
        toast("Zapisano wersję edytowaną.");
        show();
      } catch (e) { toast(e.message); }
    };
    const cancel = async () => {
      if (dirty && !(await confirmDialog({ title: "Odrzucić zmiany?", text: "Niezapisane zmiany w notatkach zostaną utracone.", confirmLabel: "Odrzuć", danger: true }))) return;
      dirty = false;
      show();
    };
    root.replaceChildren(
      h("div", { class: "toolbar" }, h("div", { class: "muted small" }, "Edycja: podgląd po prawej odświeża się na bieżąco. Zapis tworzy wersję edytowaną; wygenerowana pozostaje nietknięta."),
        h("div", { class: "row" }, h("button", { onclick: cancel }, "Anuluj"), h("button", { class: "primary", onclick: save }, "Zapisz"))),
      h("div", { class: "edit-layout" }, ta, preview));
    ta.focus();
  }

  function confirmRevert() {
    openDialog(h("h2", {}, "Wróć do wygenerowanej wersji"),
      h("p", {}, "Wersja edytowana zostanie zarchiwizowana w historii, a aktywna będzie wersja wygenerowana przez LLM."),
      h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"),
        h("button", { class: "danger", onclick: async () => {
          closeDialog();
          try { data = await api(`/api/videos/${videoId}/notes/edited`, { method: "DELETE" }); show(); } catch (e) { toast(e.message); }
        } }, "Wróć do wygenerowanej")));
  }

  async function openVersions() {
    const box = h("div", { class: "muted" }, "Ładowanie…");
    openDialog(h("h2", {}, "Historia wersji edytowanych"), box, h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Zamknij")));
    try {
      const vs = await api(`/api/videos/${videoId}/notes/versions`);
      box.replaceChildren(vs.length ? h("div", { class: "files" }, vs.map((v) => h("div", { class: "file" },
        h("span", { class: "name" }, fmtDate(v.saved_at), " ", h("span", { class: "muted small" }, `${Math.round(v.size / 1024 * 10) / 10} KB`)),
        h("button", { class: "small", onclick: async () => {
          try {
            const r = await api(`/api/videos/${videoId}/notes/versions/${v.id}`);
            closeDialog();
            startEdit(r.md);
          } catch (e) { toast(e.message); }
        } }, "Wczytaj do edytora")))) : h("div", { class: "muted" }, "Brak zarchiwizowanych wersji. Poprzednie wersje pojawią się po kolejnych zapisach."));
    } catch (e) { box.textContent = e.message; }
  }

  activeTab = {
    onState(v) { sys = state.system; if (!data && v.stages.llm.status === "done") load(); },
    dirty: () => dirty,
  };
  load();
}

// ---------- zakładka: Materiały (klatki i transkrypt) ----------
function mountMaterialsTab(container, videoId) {
  const root = h("div", {});
  container.replaceChildren(root);
  let transcript = [], frames = [];

  function framesView() {
    if (!frames.length) return h("div", { class: "empty" }, "Brak klatek. Uruchom etap „Klatki z tablicy”.");
    return h("div", { class: "grid-frames" }, frames.map((f) => h("button", { class: "thumb", title: fmtDur(f.timestamp), onclick: () => openContextDialog(videoId, f.timestamp) },
      h("img", { src: `/api/videos/${videoId}/frames/${f.file}`, alt: `Tablica ${fmtDur(f.timestamp)}`, loading: "lazy" }),
      h("span", { class: "small" }, fmtDur(f.timestamp)))));
  }

  function transcriptView() {
    if (!transcript.length) return h("div", { class: "empty" }, "Brak transkryptu (etap „Transkrypcja mowy” nie był wykonany lub film nie ma audio).");
    const list = h("div", { class: "segs tall" });
    const hitsEl = h("span", { class: "muted small" });
    const draw = (q) => {
      const ql = q.trim().toLowerCase();
      const shown = ql ? transcript.filter((s) => s.text.toLowerCase().includes(ql)) : transcript;
      list.replaceChildren(...shown.slice(0, 1500).map((s) => h("div", { class: "seg" }, h("a", { class: "ts", href: "#", onclick: (e) => { e.preventDefault(); openContextDialog(videoId, s.start); } }, `[${fmtDur(s.start)}]`), " ", s.text)));
      if (ql) highlight(list, q.trim());
      hitsEl.textContent = ql ? `${shown.length} segmentów` : `${transcript.length} segmentów`;
    };
    draw("");
    return h("div", { class: "card" }, h("div", { class: "row spread" }, h("h2", {}, "Transkrypt"),
      h("div", { class: "row" }, h("input", { type: "text", placeholder: "Szukaj w transkrypcie…", "aria-label": "Szukaj w transkrypcie", oninput: (e) => draw(e.target.value) }), hitsEl)), list);
  }

  (async () => {
    try {
      [transcript, frames] = await Promise.all([api(`/api/videos/${videoId}/transcript`), api(`/api/videos/${videoId}/frames`)]);
    } catch (e) { root.textContent = e.message; return; }
    root.replaceChildren(h("div", { class: "card" }, h("h2", {}, `Klatki z tablicy (${frames.length})`), framesView()), transcriptView());
  })();
  activeTab = { onState() {}, dirty: () => false };
}
