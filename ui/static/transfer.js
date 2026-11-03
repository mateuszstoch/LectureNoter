"use strict";
// Eksport i import wykładów (pliki .lec) oraz wskazanie pliku wideo.
// Korzysta z pomocników z app.js i library.js (h, kids, api, toast, openDialog, closeDialog, state, refresh, folderLabel...).

function fmtBytes(n) {
  if (n == null) return "";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
}

const progressBlock = () => {
  const bar = h("i", { style: "width:0%" });
  const label = h("div", { class: "small muted", style: "margin-top:6px" });
  return { el: h("div", {}, h("div", { class: "bar" }, bar), label), set(pct, text) { bar.style.width = `${Math.min(100, pct)}%`; label.textContent = text; } };
};

// ---------- eksport ----------
async function openExportDialog(v) {
  const body = h("div", { class: "muted" }, "Sprawdzanie danych wykładu…");
  openDialog(h("h2", {}, "Eksportuj wykład"), body);
  let info;
  try { info = await api(`/api/videos/${v.id}/export-info`); } catch (e) { body.textContent = e.message; return; }
  const withVideo = h("input", { type: "checkbox", id: "expvideo", disabled: !info.has_video });
  const actions = h("div", { class: "actions" });
  const err = h("div", { class: "errline", hidden: true });
  const startBtn = h("button", { class: "primary" }, "Eksportuj");
  const cancelBtn = h("button", { onclick: closeDialog }, "Anuluj");
  actions.append(cancelBtn, startBtn);
  body.replaceChildren(
    h("p", { style: "margin-top:0" }, h("b", {}, v.title || v.id)),
    h("p", { class: "small muted" }, `Dane wykładu (transkrypt, klatki, odczyt tablicy, notatki wraz z edycjami i historią, rozmowy): ${fmtBytes(info.data_bytes)}, ${info.files} plików. `
      + "Powstaje plik .lec, który zaimportujesz w LectureNoter na innym komputerze."),
    h("label", { class: "opt", style: "margin-top:10px" }, withVideo,
      h("span", {}, info.has_video ? `Dołącz plik wideo (${info.video_name}, ${fmtBytes(info.video_bytes)})` : "Plik wideo jest niedostępny, więc zostanie pominięty",
        h("div", { class: "muted small" }, "Bez filmu archiwum jest małe; wykład po imporcie działa (notatki, czat, materiały), a film można wskazać później."))),
    err, actions);
  startBtn.onclick = async () => {
    startBtn.disabled = true; withVideo.disabled = true;
    const prog = progressBlock();
    body.insertBefore(prog.el, err);
    prog.set(0, "Przygotowywanie archiwum…");
    let token;
    try { token = (await api(`/api/videos/${v.id}/export`, { method: "POST", body: { include_video: withVideo.checked } })).token; }
    catch (e) { err.textContent = e.message; err.hidden = false; prog.el.remove(); startBtn.disabled = false; withVideo.disabled = !info.has_video; return; }
    let cancelled = false;
    cancelBtn.onclick = () => { cancelled = true; api(`/api/exports/${token}`, { method: "DELETE" }).catch(() => {}); closeDialog(); };
    for (;;) {
      await new Promise((r) => setTimeout(r, 400));
      if (cancelled) return;
      let st;
      try { st = await api(`/api/exports/${token}`); } catch (e) { err.textContent = e.message; err.hidden = false; return; }
      if (st.state === "error") { err.textContent = `Eksport nie powiódł się: ${st.error}`; err.hidden = false; prog.el.remove(); cancelBtn.textContent = "Zamknij"; return; }
      prog.set(100 * st.done / st.total, `Przygotowywanie archiwum… ${Math.round(100 * st.done / st.total)}% (${fmtBytes(st.done)} z ${fmtBytes(st.total)})`);
      if (st.state === "ready") {
        prog.set(100, `Gotowe (${fmtBytes(st.size)}). Pobieranie rozpoczęte: ${st.filename}`);
        const a = h("a", { href: `/api/exports/${token}/download`, download: st.filename });
        document.body.append(a); a.click(); a.remove();
        cancelBtn.textContent = "Zamknij"; cancelBtn.onclick = closeDialog;
        startBtn.hidden = true;
        return;
      }
    }
  };
}

// ---------- import ----------
function pickImportFile() {
  const input = h("input", { type: "file", accept: ".lec,.zip,application/zip", style: "display:none" });
  input.addEventListener("change", () => { if (input.files[0]) handleImportFile(input.files[0]); input.remove(); });
  document.body.append(input);
  input.click();
}

function uploadArchive(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/import/upload?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("X-LectureNoter", "1");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* brak JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && typeof data.detail === "string" && data.detail) || `Błąd ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Nie udało się wysłać pliku (przerwane połączenie z serwerem)."));
    xhr.send(file);
  });
}

async function handleImportFile(file) {
  if (!/\.(lec|zip)$/i.test(file.name)) { toast("Wybierz plik .lec (archiwum wykładu LectureNoter)."); return; }
  const prog = progressBlock();
  const cancel = h("button", { onclick: closeDialog }, "Anuluj");
  openDialog(h("h2", {}, "Importuj wykład"), h("p", { class: "small muted", style: "margin-top:0" }, file.name), prog.el, h("div", { class: "actions" }, cancel));
  prog.set(0, "Wysyłanie…");
  let sum;
  try {
    sum = await uploadArchive(file, (done, total) => {
      prog.set(100 * done / total, done >= total ? "Sprawdzanie archiwum (integralność, bezpieczeństwo)…" : `Wysyłanie… ${Math.round(100 * done / total)}% (${fmtBytes(done)} z ${fmtBytes(total)})`);
    });
  } catch (e) {
    openDialog(h("h2", {}, "Nie można zaimportować"), h("p", {}, e.message), h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Zamknij")));
    return;
  }
  showImportOptions(sum);
}

function showImportOptions(sum) {
  const folderNow = currentFolderId();
  const radios = (name, options) => options.map((o, i) => h("label", { class: "opt" },
    h("input", { type: "radio", name, value: o.value, checked: i === 0 }), h("span", {}, o.label, o.hint ? h("div", { class: "muted small" }, o.hint) : null)));
  const checked = (name) => (document.querySelector(`#dlg input[name=${name}]:checked`) || {}).value;
  const err = h("div", { class: "errline", hidden: true });
  const withVideo = h("input", { type: "checkbox", checked: true });
  const conflictBox = sum.conflict ? h("div", { style: "margin-top:14px" },
    h("div", { class: "banner warn", style: "margin:0 0 8px" }, `Ten wykład już istnieje w bibliotece: „${sum.conflict.title}”.`),
    ...radios("conflict", [
      { value: "copy", label: "Zaimportuj jako kopię", hint: "Powstanie nowy wykład z dopiskiem „(kopia)”; istniejący zostaje." },
      { value: "replace", label: "Zastąp istniejący wykład", hint: "Dane istniejącego wykładu (też Twoje edycje notatek i rozmowy) zostaną zastąpione danymi z archiwum." },
      { value: "skip", label: "Pomiń import" }])) : null;
  const folderOptions = [{ value: "current", label: folderNow ? `Do bieżącego folderu (${folderLabel(folderNow)})` : "Do głównej listy (bez folderu)" }];
  if (sum.folder_path.length) folderOptions.push({ value: "recreate", label: `Odtwórz folder z archiwum (${sum.folder_path.join(" › ")})` });
  const when = sum.exported_at ? new Date(sum.exported_at * 1000).toLocaleString("pl-PL", { dateStyle: "medium", timeStyle: "short" }) : "";
  const importBtn = h("button", { class: "primary" }, "Importuj");
  const cancelBtn = h("button", { onclick: () => { api(`/api/import/${sum.token}`, { method: "DELETE" }).catch(() => {}); closeDialog(); } }, "Anuluj");
  openDialog(h("h2", {}, "Importuj wykład"),
    h("p", { style: "margin:0 0 4px" }, h("b", {}, sum.title)),
    h("div", { class: "meta" }, [when ? `eksport: ${when}` : null, sum.duration != null ? `film: ${fmtDur(sum.duration)}` : null, `${sum.files} plików`, `dane: ${fmtBytes(sum.data_bytes)}`].filter(Boolean).join("  ·  ")),
    sum.has_video ? h("label", { class: "opt", style: "margin-top:12px" }, withVideo,
      h("span", {}, `Zaimportuj też plik wideo (${sum.video_name}, ${fmtBytes(sum.video_bytes)})`,
        h("div", { class: "muted small" }, "Zostanie zapisany w katalogu videos/ (istniejące pliki nie są nadpisywane)."))) :
      h("div", { class: "muted small", style: "margin-top:10px" }, "Archiwum nie zawiera pliku wideo. Wykład będzie działał (notatki, czat, materiały), a film wskażesz później w jego szczegółach."),
    conflictBox,
    h("div", { style: "margin-top:14px" }, h("div", { class: "small muted" }, "Miejsce w bibliotece"), ...radios("fmode", folderOptions)),
    err, h("div", { class: "actions" }, cancelBtn, importBtn));
  let committed = false;
  // Esc lub zamkniecie okna bez importu: usun przeslany plik z dysku serwera
  $("#dlg").addEventListener("close", () => { if (!committed) api(`/api/import/${sum.token}`, { method: "DELETE" }).catch(() => {}); }, { once: true });
  importBtn.onclick = async () => {
    const on_conflict = sum.conflict ? checked("conflict") : null;
    if (on_conflict === "skip") { cancelBtn.click(); return; }
    importBtn.disabled = true; importBtn.textContent = "Importowanie…"; err.hidden = true;
    committed = true;
    try {
      const res = await api("/api/import/commit", { method: "POST", body: {
        token: sum.token, folder: folderNow, folder_mode: checked("fmode") || "current", on_conflict, include_video: withVideo.checked } });
      closeDialog();
      await refresh();
      toast(`Zaimportowano wykład „${res.title}”.`);
      location.hash = `#/video/${res.id}`;
    } catch (e) { committed = false; err.textContent = e.message; err.hidden = false; importBtn.disabled = false; importBtn.textContent = "Importuj"; }
  };
}

// ---------- wskazanie pliku wideo ----------
async function openRelinkDialog(v) {
  const body = h("div", { class: "muted" }, "Ładowanie listy plików…");
  openDialog(h("h2", {}, "Wskaż plik wideo"), body);
  let files;
  try { files = await api("/api/library/files"); } catch (e) { body.textContent = e.message; return; }
  const dir = state.system && state.system.paths ? state.system.paths.videos_dir : "videos/";
  if (!files.length) {
    body.replaceChildren(h("p", {}, "Katalog z nagraniami jest pusty."), h("p", { class: "muted small" }, "Skopiuj plik wideo do: ", h("code", {}, dir), " i wróć tutaj."),
      h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Zamknij")));
    return;
  }
  let chosen = null;
  const err = h("div", { class: "errline", hidden: true });
  const rows = files.map((f) => {
    const taken = f.video_id && f.video_id !== v.id;
    const radio = h("input", { type: "radio", name: "relink", value: f.file, disabled: !!taken, onchange: () => { chosen = f.file; } });
    return h("label", { class: "file", style: taken ? "opacity:.45;cursor:not-allowed" : "" }, radio, h("span", { class: "name" }, f.file),
      taken ? h("span", { class: "muted small" }, "przypisany do innego wykładu") : null, h("span", { class: "muted small" }, fmtBytes(f.size)));
  });
  body.replaceChildren(
    h("p", { class: "small muted", style: "margin-top:0" }, `Wykład „${v.title || v.id}” wskaże wybrany plik. Dotychczasowe wyniki (transkrypt, klatki, notatki) zostają uznane za dotyczące tego filmu.`),
    h("div", { class: "files" }, rows), err,
    h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Anuluj"), h("button", { class: "primary", onclick: async () => {
      if (!chosen) { err.textContent = "Wybierz plik z listy."; err.hidden = false; return; }
      try { await api(`/api/videos/${v.id}/relink`, { method: "POST", body: { file: chosen } }); closeDialog(); await refresh(); toast("Plik wideo został przypisany."); }
      catch (e) { err.textContent = e.message; err.hidden = false; }
    } }, "Przypisz")));
}

// ---------- przeciągnięcie pliku .lec na bibliotekę ----------
document.addEventListener("dragover", (e) => {
  if ($("#libroot") && e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); $("#libroot").classList.add("filedrop"); }
});
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) $("#libroot")?.classList.remove("filedrop"); });
document.addEventListener("drop", (e) => {
  const root = $("#libroot");
  if (!root || !e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault();
  root.classList.remove("filedrop");
  const f = e.dataTransfer.files[0];
  if (f) handleImportFile(f);
});


// ---------- napisy (.srt/.vtt) i slajdy (PDF) ----------
async function uploadRaw(url, file) {
  const r = await fetch(url, { method: "PUT", headers: { "X-LectureNoter": "1", "content-type": "application/octet-stream" }, body: file });
  let d = null;
  try { d = await r.json(); } catch (_) { /* brak treści */ }
  if (!r.ok) throw new Error((d && typeof d.detail === "string" && d.detail) || `Błąd ${r.status}`);
  return d;
}

async function openExtrasDialog(v) {
  const subsMsg = h("div", { class: "small", style: "margin-top:6px" });
  const slidesInfo = h("div", { class: "small muted", style: "margin-top:6px" }, "Ładowanie…");
  const slidesMsg = h("div", { class: "small", style: "margin-top:6px" });
  const subsInput = h("input", { type: "file", accept: ".srt,.vtt,text/vtt,application/x-subrip", "aria-label": "Plik z napisami" });
  const pdfInput = h("input", { type: "file", accept: ".pdf,application/pdf", "aria-label": "Slajdy PDF" });

  async function showSlides() {
    try {
      const d = await api(`/api/videos/${v.id}/slides`);
      slidesInfo.replaceChildren(...kids(d.slides ? [`Dodane: ${d.slides.name} (${d.slides.pages} stron z tekstem). `,
        h("a", { href: "#", onclick: async (e) => { e.preventDefault(); await api(`/api/videos/${v.id}/slides`, { method: "DELETE" }); showSlides(); } }, "Usuń")] : ["Brak slajdów."]));
    } catch (e) { slidesInfo.textContent = e.message; }
  }
  subsInput.onchange = async () => {
    const f = subsInput.files[0];
    if (!f) return;
    subsMsg.textContent = "Wczytuję…";
    try {
      const r = await uploadRaw(`/api/videos/${v.id}/subtitles`, f);
      subsMsg.textContent = `Wczytano ${r.segments} napisów (do ${fmtDur(r.duration)}). Wygeneruj ponownie notatki, aby użyły nowego transkryptu.`;
      await refresh();
    } catch (e) { subsMsg.textContent = e.message; subsMsg.style.color = "var(--accent)"; }
  };
  pdfInput.onchange = async () => {
    const f = pdfInput.files[0];
    if (!f) return;
    slidesMsg.textContent = "Przetwarzam PDF…";
    try {
      await uploadRaw(`/api/videos/${v.id}/slides?name=${encodeURIComponent(f.name)}`, f);
      slidesMsg.textContent = "Dodano. Czat będzie korzystał ze slajdów; szkielet notatek uwzględni je po ponownym wygenerowaniu notatek.";
      showSlides();
    } catch (e) { slidesMsg.textContent = e.message; slidesMsg.style.color = "var(--accent)"; }
  };
  openDialog(h("h2", {}, "Napisy i slajdy"),
    h("h3", { style: "font-size:14px;margin:6px 0 2px" }, "Napisy (.srt, .vtt)"),
    h("p", { class: "small muted", style: "margin:0 0 6px" }, "Zastępują automatyczną transkrypcję (np. napisy z platformy wykładowej). Nadpisują istniejący transkrypt."),
    subsInput, subsMsg,
    h("h3", { style: "font-size:14px;margin:16px 0 2px" }, "Slajdy (PDF)"),
    h("p", { class: "small muted", style: "margin:0 0 6px" }, "Materiał pomocniczy: tekst slajdów uzupełnia czat i pomaga w poprawnych nazwach i terminach w notatkach. Skany bez tekstu nie są obsługiwane."),
    pdfInput, slidesMsg, slidesInfo,
    h("div", { class: "actions" }, h("button", { onclick: closeDialog }, "Zamknij")));
  showSlides();
}
