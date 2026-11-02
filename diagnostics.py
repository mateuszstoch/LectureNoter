"""Diagnostyka bledow etapow: podsumowanie przyczyny i podpowiedzi naprawy (po polsku)."""
import re
import signal

HINTS = [
    (r"libcublas|libcudnn|cudnn|cublas", "Brak bibliotek CUDA (cuBLAS/cuDNN): uruchom `pip install -r requirements.txt` w aktywnym .venv."),
    (r"out of memory|CUDA failed with error out of memory|OutOfMemory", "Brak pamieci GPU: zamknij inne programy uzywajace GPU lub uzyj profilu 8gb."),
    (r"no CUDA-capable device|CUDA driver version is insufficient|cuda.*not available|Could not load.*cuda", "GPU niedostepne w WSL: sprawdz `nvidia-smi`, sterownik NVIDIA w Windowsie i wsparcie CUDA."),
    (r"runner|unexpectedly stopped|terminated|exit status|HTTP 500|Ollama zwrocila blad", "Proces modelu w Ollamie padl (zwykle brak VRAM/RAM, zbyt duzy obraz albo blad sterownika). Zamknij inne programy uzywajace GPU, zmniejsz vlm.max_side w config.yaml, a przyczyne sprawdz w logu Ollamy (np. `journalctl -u ollama` albo okno `ollama serve`)."),
    (r"No module named", "Brak pakietu Pythona: uruchom `pip install -r requirements.txt` w aktywnym .venv."),
    (r"Connection refused|Max retries exceeded|Ollama niedostepna|Serwer Ollama nie odpowiada", "Ollama nie odpowiada: uruchom `ollama serve` (w WSL)."),
    (r"ollama pull", "Brakuje modelu w Ollamie: wykonaj podane `ollama pull ...`."),
    (r"ffmpeg|ffprobe", "Problem z ffmpeg: zainstaluj go (`sudo apt install ffmpeg`) i sprawdz, czy plik wideo nie jest uszkodzony."),
    (r"Permission denied", "Brak uprawnien do pliku lub katalogu: sprawdz uprawnienia w katalogu projektu."),
    (r"No space left", "Brak miejsca na dysku."),
]


def hint_for(text):
    for pattern, hint in HINTS:
        if re.search(pattern, text or "", re.I):
            return hint
    return None


def signal_name(rc):
    """Kod wyjscia ujemny = zabity sygnalem (np. -6 SIGABRT przy awarii bibliotek C)."""
    if rc is not None and rc < 0:
        try:
            return signal.Signals(-rc).name
        except ValueError:
            return f"sygnal {-rc}"
    return None


def summarize_tail(lines, n=6):
    """Ostatnie niepuste linie stderr w jednej linii (do komunikatu bledu)."""
    keep = [l.strip() for l in lines if l.strip()]
    return " | ".join(keep[-n:])[:700]


def describe_failure(stage, rc, error_info, tail):
    """Zwraca (komunikat, pelny tekst do logu). error_info: dict z wyjatkiem workera lub None."""
    sig = signal_name(rc)
    head = f"etap {stage} zakonczyl sie kodem {rc}" if not sig else f"etap {stage} zostal zabity sygnalem {sig} (awaria na poziomie biblioteki natywnej)"
    cause = f"{error_info['type']}: {error_info['message']}" if error_info else summarize_tail(tail)
    hint = hint_for(cause + "\n" + "\n".join(tail))
    msg = head + (f": {cause}" if cause else "") + (f" | Wskazowka: {hint}" if hint else "")
    return msg
