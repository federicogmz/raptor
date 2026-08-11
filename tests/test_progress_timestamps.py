"""scripts/progress.py escribe t=<epoch> en CADA evento de PROGRESS_FILE —
el HUD del geovisor (geovisor/app.js::advancePhase) lo usa para calcular
cuánto duró cada fase real de la corrida (diff entre el t de una fase y el
de la siguiente), con la hora del SERVIDOR en vez de Date.now() del
navegador, para que siga siendo correcto aunque se reconecte a mitad de una
corrida de horas.
"""
import importlib
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))


def _recargar_progress(tmp_path, monkeypatch):
    """progress.py lee PROGRESS_FILE una sola vez, al importarse (variable
    de módulo) — hay que fijar el env ANTES de (re)importar."""
    progress_file = tmp_path / "progress.ndjson"
    monkeypatch.setenv("PROGRESS_FILE", str(progress_file))
    if "progress" in sys.modules:
        importlib.reload(sys.modules["progress"])
    else:
        importlib.import_module("progress")
    return sys.modules["progress"], progress_file


class TestTimestampsEnEventos:
    def test_stage_incluye_t(self, tmp_path, monkeypatch):
        progress, pf = _recargar_progress(tmp_path, monkeypatch)
        antes = int(time.time())
        progress.print_stage_header("Cargando dataset", 1, 13)
        despues = int(time.time())
        linea = pf.read_text().strip().splitlines()[0]
        campos = dict(p.split("=", 1) for p in linea.split("\t")[1:])
        assert "t" in campos
        assert antes <= int(campos["t"]) <= despues

    def test_bar_y_done_tambien_incluyen_t(self, tmp_path, monkeypatch):
        progress, pf = _recargar_progress(tmp_path, monkeypatch)
        progress.print_progress(50, 100, label="x")
        progress.print_done(label="listo")
        lineas = pf.read_text().strip().splitlines()
        assert len(lineas) == 2
        for linea in lineas:
            campos = dict(p.split("=", 1) for p in linea.split("\t")[1:])
            assert "t" in campos

    def test_el_runner_parsea_t_como_cualquier_otro_campo(self, tmp_path, monkeypatch):
        progress, pf = _recargar_progress(tmp_path, monkeypatch)
        progress.print_stage_header("SfM", 4, 13)
        sys.path.insert(0, REPO)
        from core.runner import parse_progress_events
        _, eventos = parse_progress_events(str(pf), 0)
        assert len(eventos) == 1
        assert "t" in eventos[0]
        assert eventos[0]["name"] == "SfM"
