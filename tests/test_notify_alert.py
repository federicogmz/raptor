"""scripts/notify_alert.py — aviso al puesto de mando cuando hay focos
térmicos activos.

Se llama después de cada situation-summary; nunca debe romper el pipeline.
El disparador solía tener un segundo camino ("área afectada sin focos" —
via el ya eliminado area_ha) — ahora es puramente hotspots_activos > 0,
porque situation.json ya no tiene ninguna noción de área/severidad.
"""
import json
import os
import sys
import urllib.request


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import notify_alert  # noqa: E402


def _situation(tmp_path, focos=2, temp_max=125.9):
    outputs = tmp_path / "outputs"
    outputs.mkdir(exist_ok=True)
    s = {
        "hotspots_activos": focos,
        "hotspots": [{"lat": 6.33, "lon": -75.48, "px": 126, "temp_c": temp_max}],
        "temp_max": temp_max,
        "temp_promedio": 34.2,
        "confianza": "media",
        "cobertura_pct": 55.0,
        "captura": "2026-08-08T11:49:18",
    }
    (outputs / "situation.json").write_text(json.dumps(s))
    return s


class TestAlerta:
    def test_con_focos_escribe_alert_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _situation(tmp_path, focos=2)
        monkeypatch.setenv("RAPTOR_MISSION", "mision_prueba")
        assert notify_alert.main() == 0
        alerta = json.loads((tmp_path / "outputs" / "alert.json").read_text())
        assert alerta["evento"] == "focos_activos"
        assert alerta["hotspots_activos"] == 2
        assert alerta["mision"] == "mision_prueba"
        assert alerta["hotspots"][0]["temp_c"] == 125.9
        for clave in ("area_ha", "severidad", "solo_termico"):
            assert clave not in alerta, f"'{clave}' ya no debería aparecer en el payload"

    def test_sin_focos_no_escribe_alerta(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _situation(tmp_path, focos=0)
        assert notify_alert.main() == 0
        assert not (tmp_path / "outputs" / "alert.json").exists()

    def test_sin_situation_no_hace_nada(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "outputs").mkdir(exist_ok=True)
        assert notify_alert.main() == 0

    def test_envia_el_payload_al_webhook(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _situation(tmp_path, focos=3)
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://ejemplo/alerta")
        enviado = {}

        class _Resp:
            status = 200

        def fake_urlopen(req, timeout=10):
            enviado["url"] = req.full_url
            enviado["data"] = json.loads(req.data.decode())
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert notify_alert.main() == 0
        assert enviado["url"] == "http://ejemplo/alerta"
        assert enviado["data"]["hotspots_activos"] == 3
        assert enviado["data"]["mision"] == "mision"

    def test_error_de_red_no_rompe(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _situation(tmp_path, focos=1)
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://no-existe.invalid/x")

        def explota(req, timeout=10):
            raise OSError("sin red")

        monkeypatch.setattr(urllib.request, "urlopen", explota)
        assert notify_alert.main() == 0  # best-effort: nunca falla el pipeline
        assert (tmp_path / "outputs" / "alert.json").exists()
