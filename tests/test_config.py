import copy
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main as m  # noqa: E402
from config_loader import LiveConfig  # noqa: E402
from ui.library import Library  # noqa: E402


def write_cfg(path, prompt_version):
    cfg = yaml.safe_load(open(ROOT / "config.yaml"))
    cfg["llm"]["prompt_version"] = prompt_version
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    t = time.time() + prompt_version  # rozne mtime nawet przy szybkim zapisie
    os.utime(path, (t, t))


def test_live_config_reloads_on_change_and_survives_bad_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    write_cfg(p, 1)
    live = LiveConfig(p)
    assert live()["llm"]["prompt_version"] == 1
    write_cfg(p, 2)
    assert live()["llm"]["prompt_version"] == 2
    p.write_text("llm: [zepsute: {", encoding="utf-8")
    os.utime(p, (time.time() + 50, time.time() + 50))
    assert live()["llm"]["prompt_version"] == 2   # zly plik nie wywala serwera: zostaje ostatnia dobra konfiguracja
    write_cfg(p, 3)
    assert live()["llm"]["prompt_version"] == 3


def test_status_stays_current_when_config_changes_without_server_restart(tmp_path):
    """Regresja: po `git pull` (zmiana config.yaml) swiezo wygenerowane notatki wygladaly na 'nieaktualne' do restartu."""
    cfg_path = tmp_path / "c.yaml"
    write_cfg(cfg_path, 1)
    live = LiveConfig(cfg_path)
    videos, work = tmp_path / "videos", tmp_path / "work"
    videos.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5", "-t", "1",
                    "-pix_fmt", "yuv420p", str(videos / "a.mp4")], check=True)
    static_cfg = copy.deepcopy(live())           # tak dzialal serwer: konfiguracja z chwili startu
    lib_live, lib_static = Library(videos, work, live), Library(videos, work, static_cfg)
    vid = lib_live.add_video("a.mp4")
    wd = lib_live.workdir(vid)

    def run_llm_job(cfg):
        """Nowy proces zadania: czyta aktualny plik konfiguracji i zapisuje stan etapu z jego podpisem."""
        o = m.PipelineOrchestrator.from_workdir(wd, cfg)
        o.has_audio = False
        o.runners = {s: (lambda st: [(wd / f).write_text("x") for f in m.OUTPUTS[st]]) for s in m.STAGES}
        o.run(only_stage="llm", force=True)

    run_llm_job(live())
    assert lib_live.summary(vid)["stages"]["llm"]["status"] == "done"
    write_cfg(cfg_path, 2)                        # 'git pull' zmienil config.yaml, serwer dziala dalej
    run_llm_job(live())                           # regeneracja notatek w nowym procesie
    assert lib_live.summary(vid)["stages"]["llm"]["status"] == "done"      # naprawione: serwer widzi nowa konfiguracje
    assert lib_static.summary(vid)["stages"]["llm"]["status"] == "stale"   # stare zachowanie (konfiguracja zamrozona)
