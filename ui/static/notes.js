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

function highlightRx(root, rx) {
  root.querySelectorAll("mark.hit").forEach((m) => m.replaceWith(document.createTextNode(m.textContent)));
  root.normalize();
  if (!rx) return [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest(".katex, mark") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    rx.lastIndex = 0;
    let m, last = 0, frag = null;
    while ((m = rx.exec(n.nodeValue))) {
      if (m[0] === "") { rx.lastIndex++; continue; }
      frag = frag || document.createDocumentFragment();
      frag.append(n.nodeValue.slice(last, m.index), h("mark", { class: "hit" }, m[0]));
      last = m.index + m[0].length;
    }
    if (frag) { frag.append(n.nodeValue.slice(last)); n.replaceWith(frag); }
  }
  return [...root.querySelectorAll("mark.hit")];
}

// podświetlenie dosłownego tekstu (pole szukania w notatkach)
function highlight(root, query) {
  return highlightRx(root, query ? new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi") : null);
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
  const WIDTHS = [["standard", "Wąsko"], ["wide", "Szeroko"], ["full", "Pełna"]];
  const pref = (k, d) => { try { return localStorage.getItem(`lecturenoter.notes.${k}`) || d; } catch (_) { return d; } };
  const setPref = (k, v) => { try { localStorage.setItem(`lecturenoter.notes.${k}`, v); } catch (_) { /* tryb prywatny */ } };
  let tocOn = pref("toc", "on") === "on", width = pref("width", "wide");
  let framesOn = pref("frames", "on") === "on", framesData = null, verifyOn = false;
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

  // Układ czytnika: spis treści można schować, a szerokość tekstu przełączać (zapamiętywane w przeglądarce).
  function applyLayout() {
    const lay = root.querySelector(".notes-layout");
    if (lay) {
      lay.classList.toggle("no-toc", !tocOn);
      lay.dataset.width = width;
    }
    const tb = root.querySelector("#toc-toggle"), wb = root.querySelector("#width-toggle");
    if (tb) { tb.textContent = tocOn ? "Ukryj spis" : "Pokaż spis"; tb.setAttribute("aria-pressed", String(!tocOn)); }
    if (wb) wb.textContent = `Szerokość: ${(WIDTHS.find((w) => w[0] === width) || WIDTHS[1])[1]}`;
  }
  function toggleToc() { tocOn = !tocOn; setPref("toc", tocOn ? "on" : "off"); applyLayout(); }
  function cycleWidth() {
    width = WIDTHS[(WIDTHS.findIndex((w) => w[0] === width) + 1) % WIDTHS.length][0];
    setPref("width", width);
    applyLayout();
  }

  // ---------- klatki z tablicy przy sekcjach ----------
  async function insertFrames() {
    const body = root.querySelector("#nbody");
    if (!body) return;
    body.querySelectorAll("figure.nframes").forEach((f) => f.remove());
    if (!framesOn) return;
    if (!framesData) { try { framesData = await api(`/api/videos/${videoId}/frames`); } catch (_) { framesData = []; } }
    if (!framesData.length || !root.contains(body)) return;
    // Czas każdej sekcji: pierwszy znacznik HH:MM:SS w nagłówku lub tekście sekcji; gdy go brak (modele często pomijają znaczniki),
    // czas jest szacowany z kolejności i długości sekcji (notatki idą chronologicznie), więc klatki pojawiają się zawsze.
    const TS3 = /(\d{1,2}):(\d{2}):(\d{2})/;
    let heads = [...body.querySelectorAll("h2")];
    if (!heads.length) heads = [...body.querySelectorAll("h3")];
    heads = heads.filter((hd) => !/^(spis treści|podsumowanie)\b/i.test(hd.textContent.trim()));
    if (!heads.length) return;
    let pos = 0;
    const secs = heads.map((hd) => {
      let el = hd.nextElementSibling, text = hd.textContent, len = 0;
      const stop = hd.tagName === "H2" ? /^H[12]$/ : /^H[123]$/;   // sekcja trwa do następnego nagłówka tego samego lub wyższego poziomu
      while (el && !stop.test(el.tagName)) { text += "\n" + el.textContent; len += el.textContent.length; el = el.nextElementSibling; }
      const m = text.match(TS3);
      const sec = { hd, t: m ? tsToSec(m[1], m[2], m[3]) : null, explicit: !!m, at: pos };
      pos += Math.max(len, 80);
      return sec;
    });
    const tmin = framesData[0].timestamp, tmax = framesData[framesData.length - 1].timestamp + 1;
    const anchors = [{ at: 0, t: tmin }, ...secs.filter((x) => x.t != null).map((x) => ({ at: x.at, t: x.t })), { at: pos, t: tmax }]
      .sort((x, y) => x.at - y.at || x.t - y.t);
    secs.forEach((sec) => {
      if (sec.t != null) return;
      let lo = anchors[0], hi = anchors[anchors.length - 1];
      for (const an of anchors) { if (an.at <= sec.at) lo = an; }
      for (let i = anchors.length - 1; i >= 0; i--) { if (anchors[i].at > sec.at) hi = anchors[i]; }
      sec.t = hi.at === lo.at ? lo.t : lo.t + (hi.t - lo.t) * (sec.at - lo.at) / (hi.at - lo.at);
    });
    let prev = -Infinity;
    secs.forEach((sec) => { sec.t = Math.max(sec.t, prev); prev = sec.t; });   // czas nie cofa się między sekcjami
    secs.forEach((sec, i) => {
      const next = secs[i + 1] ? secs[i + 1].t : Infinity;
      const slack = sec.explicit ? 45 : 0;
      let fr = framesData.filter((f) => f.timestamp >= sec.t - slack && f.timestamp < next);
      if (!fr.length && !secs[i + 1]) fr = framesData.filter((f) => f.timestamp >= sec.t - slack);
      if (!fr.length) return;
      const picks = fr.length <= 3 ? fr : [0, 1, 2].map((k) => fr[Math.round(k * (fr.length - 1) / 2)]);
      sec.hd.after(h("figure", { class: "nframes", title: sec.explicit ? "" : "Dopasowanie przybliżone: w tej sekcji brak znacznika czasu" },
        picks.map((f) => h("button", { class: "nframe", title: `Tablica ${fmtDur(f.timestamp)}: pokaż kontekst${sec.explicit ? "" : " (dopasowanie przybliżone)"}`,
          onclick: () => openContextDialog(videoId, f.timestamp) },
          h("img", { src: `/api/videos/${videoId}/frames/${f.file}`, loading: "lazy", alt: `Tablica ${fmtDur(f.timestamp)}` }),
          h("span", { class: "muted small" }, `${fmtDur(f.timestamp)}${sec.explicit ? "" : " ~"}`)))));
    });
  }
  function toggleFrames() {
    framesOn = !framesOn;
    setPref("frames", framesOn ? "on" : "off");
    const b = root.querySelector("#frames-toggle");
    if (b) b.textContent = framesOn ? "Ukryj klatki" : "Pokaż klatki";
    insertFrames();
  }

  // ---------- kontrola wierności ----------
  async function toggleVerify() {
    const body = root.querySelector("#nbody"), box = root.querySelector("#verify-box");
    body.querySelectorAll(".nverify-weak, .nverify-uncertain").forEach((el) => el.classList.remove("nverify-weak", "nverify-uncertain"));
    if (verifyOn) { verifyOn = false; box.hidden = true; root.querySelector("#verify-toggle").textContent = "Sprawdź wierność"; return; }
    let r;
    try { r = await api(`/api/videos/${videoId}/notes/verify`); } catch (e) { toast(e.message); return; }
    verifyOn = true;
    root.querySelector("#verify-toggle").textContent = "Ukryj kontrolę";
    const norm = (t) => t.toLowerCase().replace(/\[\d+:\d+:\d+\]/g, "").replace(/[^\p{L}\p{N} ]/gu, " ").replace(/\s+/g, " ").trim();
    const blocks = [...body.querySelectorAll("p, li, blockquote")].map((b) => ({ b, n: norm(b.textContent) }));
    let weak = 0, unc = 0;
    r.flagged.forEach((f) => {
      const hit = blocks.find((x) => x.n.includes(norm(f.text).slice(0, 50)));
      if (!hit) return;
      hit.b.classList.add(f.kind === "uncertain" ? "nverify-uncertain" : "nverify-weak");
      hit.b.title = f.kind === "uncertain" ? "Miejsce oznaczone [?]: model nie był pewny odczytu" : `Słabo poparte transkryptem i tablicą (${Math.round(f.support * 100)}% słów występuje w materiale)`;
      if (f.kind === "uncertain") unc++; else weak++;
    });
    box.hidden = false;
    box.textContent = r.has_sources
      ? `Sprawdzono ${r.sentences} zdań: ${weak} słabo poparte materiałem (przerywana linia), ${unc} miejsc oznaczonych [?]. To heurystyka: zdanie bywa poprawnym streszczeniem własnymi słowami, więc traktuj je jako podpowiedź do weryfikacji.`
      : "Brak transkryptu i odczytu tablicy do porównania.";
  }

  // ---------- zaznaczenie → wyjaśnij / rozwiń / zapytaj w czacie ----------
  let selText = "";
  const selBar = h("div", { class: "selbar", hidden: true, role: "toolbar", "aria-label": "Akcje dla zaznaczonego tekstu" },
    h("button", { class: "small", onclick: () => explainSelection("simpler") }, "Wyjaśnij prościej"),
    h("button", { class: "small", onclick: () => explainSelection("expand") }, "Rozwiń"),
    h("button", { class: "small", onclick: askInChat }, "Zapytaj w czacie"));
  function checkSelection() {
    const sel = window.getSelection(), body = root.querySelector("#nbody");
    const text = sel ? sel.toString().trim() : "";
    if (!body || !sel.rangeCount || text.length < 12 || !body.contains(sel.anchorNode) || mode !== "view") { selBar.hidden = true; return; }
    selText = text.slice(0, 2000);
    const r = sel.getRangeAt(0).getBoundingClientRect();
    selBar.hidden = false;
    selBar.style.top = `${Math.max(8, r.top - 46)}px`;
    selBar.style.left = `${Math.min(window.innerWidth - 330, Math.max(8, r.left))}px`;
  }
  async function explainSelection(kind) {
    const text = selText;
    selBar.hidden = true;
    const out = h("div", { class: "notes-body", style: "max-width:none" }, h("span", { class: "muted" }, "Model układa wyjaśnienie…"));
    openDialog(h("h2", {}, kind === "simpler" ? "Prostsze wyjaśnienie" : "Rozwinięcie"),
      h("blockquote", { class: "small" }, text.length > 300 ? text.slice(0, 300) + "…" : text), out,
      h("div", { class: "actions" }, h("button", { onclick: () => { closeDialog(); askInChat(text); } }, "Zapytaj w czacie"), h("button", { onclick: closeDialog }, "Zamknij")));
    try {
      const r = await api(`/api/videos/${videoId}/explain`, { method: "POST", body: { text, mode: kind } });
      out.innerHTML = renderMarkdownHtml(r.text || "(pusta odpowiedź)");
      linkifyTimestamps(out, (t) => openContextDialog(videoId, t));
    } catch (e) { out.textContent = e.message; }
  }
  function askInChat(text) {
    const t = typeof text === "string" ? text : selText;
    selBar.hidden = true;
    window.pendingChatPrompt = { videoId, text: `Wyjaśnij ten fragment notatek i podaj kontekst z wykładu: «${t.slice(0, 600)}»` };
    location.hash = `#/video/${videoId}/chat`;
  }
  document.addEventListener("selectionchange", () => { if (!root.isConnected) return; clearTimeout(checkSelection.t); checkSelection.t = setTimeout(checkSelection, 250); });

  function toolbar() {
    const pandoc = sys && sys.pandoc;
    return h("div", { class: "toolbar" },
      h("div", { class: "row" },
        chip(data.source === "edited" ? "stale" : "done", data.source === "edited" ? "wersja edytowana" : "wersja wygenerowana"),
        h("span", { class: "muted small" }, fmtDate(data.updated)),
        h("button", { class: "small", id: "toc-toggle", title: "Schowaj lub pokaż spis treści", onclick: toggleToc }, "Ukryj spis"),
        h("button", { class: "small", id: "width-toggle", title: "Zmień szerokość tekstu notatek", onclick: cycleWidth }, "Szerokość"),
        h("button", { class: "small", id: "frames-toggle", title: "Pokaż miniatury tablicy przy sekcjach", onclick: toggleFrames }, framesOn ? "Ukryj klatki" : "Pokaż klatki"),
        h("button", { class: "small", id: "verify-toggle", title: "Zaznacz zdania słabo poparte transkryptem i tablicą oraz miejsca [?]", onclick: toggleVerify }, "Sprawdź wierność")),
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
    root.replaceChildren(...kids(toolbar(), qBanner, h("div", { id: "verify-box", class: "banner warn", hidden: true, style: "margin:0 0 14px" }),
      h("div", { class: "notes-layout" }, tocEl, body), selBar));
    applyLayout();
    verifyOn = false;
    insertFrames();
    const pf = window.pendingNotesFocus;
    if (pf && pf.videoId === videoId) {  // przejście z wyszukiwarki biblioteki: podświetl rdzenie słów i przewiń do sekcji
      window.pendingNotesFocus = null;
      hits = highlightRx(body, termRegex(pf.terms));
      hitIdx = -1;
      const sec = [...body.querySelectorAll("h1, h2, h3")].find((x) => x.textContent.trim() === (pf.heading || "").trim());
      const target = sec || hits[0];
      if (target) setTimeout(() => target.scrollIntoView({ block: "start", behavior: "smooth" }), 80);
      const hitsEl = $("#nhits");
      if (hitsEl) hitsEl.textContent = hits.length ? `${hits.length} trafień z wyszukiwarki` : "";
    }
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
