"""NoCacheStaticMiddleware (webapp/main.py): los PNG de tiles son
inmutables (una corrida nunca los pisa in-place, siempre los regenera
desde cero) — SALVO cuando la respuesta es un 404. Bug real, reportado en
vivo: un tile pedido ANTES de que existiera (típico mientras una misión
sigue procesando) también recibía el header "immutable, max-age=1 año", así
que el navegador se quedaba con ese 404 cacheado para siempre — el térmico
nunca aparecía en el geovisor aunque el archivo real ya estuviera escrito y
bounds.json ya lo listara en capas_disponibles.
"""
import importlib

import pytest


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    # RAPTOR_APP_DIR aislado: sin esto GEOVISOR_DIR apunta al /app real
    # (el repo montado adentro del contenedor de test) y el test de más
    # abajo escribiría un archivo de verdad ahí.
    (tmp_path / "geovisor").mkdir()
    (tmp_path / "static").mkdir()
    monkeypatch.setenv("RAPTOR_APP_DIR", str(tmp_path))
    monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
    from fastapi.testclient import TestClient
    from core import scan
    importlib.reload(scan)
    from webapp import main as M
    importlib.reload(M)
    return TestClient(M.app)


class TestCacheDeTilesPNG:
    def test_tile_inexistente_no_se_cachea_para_siempre(self, cliente):
        r = cliente.get("/geovisor/tiles/thermal/17/38104/63193.png")
        assert r.status_code == 404
        assert "immutable" not in r.headers.get("cache-control", "")

    def test_tile_real_si_es_inmutable(self, cliente):
        from webapp import main as M
        tile_dir = M.GEOVISOR_DIR / "tiles" / "thermal" / "0" / "0"
        tile_dir.mkdir(parents=True, exist_ok=True)
        (tile_dir / "0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        r = cliente.get("/geovisor/tiles/thermal/0/0/0.png")
        assert r.status_code == 200
        assert "immutable" in r.headers.get("cache-control", "")

    def test_bounds_json_sigue_sin_cachear(self, cliente):
        r = cliente.get("/geovisor/tiles/bounds.json")
        assert "immutable" not in r.headers.get("cache-control", "")
