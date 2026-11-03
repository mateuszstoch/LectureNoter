"""Wyjasnianie zaznaczonego fragmentu notatek modelem, z kontekstem z wykladu (retrieval + szkielet wykladu)."""
from pathlib import Path

from llm_synthesis import est_tokens
from ui.retrieval import build_context

MODES = {
    "simpler": ("Wyjaśnij poniższy fragment notatek prostszymi słowami, tak aby zrozumiała go osoba, która nie była na wykładzie. "
                "Wyjaśnij użyte pojęcia i skróty. Odpowiedź: 3-6 zdań, bez powtarzania całego fragmentu."),
    "expand": ("Rozwiń poniższy fragment notatek: wyjaśnij dlaczego tak jest, co z czego wynika i jak łączy się z resztą wykładu; "
               "dodaj przykład z wykładu, jeśli występuje w materiale. Odpowiedź: kilka akapitów lub krótka lista."),
}
SYSTEM = ("Jesteś asystentem, który pomaga zrozumieć notatki z jednego wykładu. Korzystasz wyłącznie z dostarczonego materiału "
          "(kontekst wykładu, fragmenty notatek, mowy i tablicy). Odpowiadasz po polsku, rzeczowo, wzory zapisujesz w LaTeX ($...$). "
          "Jeśli materiał nie wystarcza, napisz to wprost zamiast zgadywać. Nie używaj znaków spoza alfabetu łacińskiego.")
MAX_TEXT = 2000


def explain(client, wd, text, mode, model, num_ctx, notes_md, transcript, vision):
    """Zwraca {'text': markdown, 'sources': [...]}."""
    if mode not in MODES:
        raise ValueError("Nieznany tryb.")
    text = (text or "").strip()[:MAX_TEXT]
    if len(text) < 3:
        raise ValueError("Zaznacz fragment notatek.")
    outline_path = Path(wd) / "outline.md"
    outline = outline_path.read_text(encoding="utf-8").strip() if outline_path.exists() else ""
    budget = max(800, min(4000, num_ctx - 2500 - est_tokens(text) - est_tokens(outline)))
    ctx, sources = build_context(text, [], notes_md, transcript, vision, budget)
    prompt = (f"{('# Kontekst całego wykładu' + chr(10) + outline + chr(10) * 2) if outline else ''}"
              f"# Fragmenty wykładu\n{ctx or '(brak)'}\n\n# Fragment do wyjaśnienia\n{text}\n\n# Zadanie\n{MODES[mode]}")
    out = client.generate(model, prompt, system=SYSTEM, options={"num_ctx": num_ctx, "num_predict": 900, "temperature": 0.3}, keep_alive="10m")
    return {"text": (out or "").strip(), "sources": sources}
