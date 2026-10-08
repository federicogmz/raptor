"""NoCacheStaticMiddleware (webapp/main.py): todo bajo /geovisor y /static
—tiles PNG incluidos— usa "no-cache", no "immutable, max-age=1 año".

Los tiles tenían el cache "fuerte" bajo la premisa de que una corrida nunca
los pisa in-place, siempre los regenera desde cero. Falso: "Corregir y
reintentar" sobre la MISMA misión reescribe los tiles en el MISMO path, y con
esa marca el navegador nunca revalidaba — se quedaba con el tile de la
corrida anterior (mosaico más recortado) para siempre, aunque el archivo real
en disco ya fuera el nuevo. Bug real, reportado en vivo durante un reintento.

Related bug ya cubierto acá: un tile pedido ANTES de que existiera (típico
mientras una misión sigue procesando) tampoco debe cachearse "para siempre"
como 404 — con "no-cache" uniforme esto queda cubierto solo, sin caso
especial.
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
        assert r.headers.get("cache-control") == "no-cache"

    def test_tile_real_no_es_inmutable(self, cliente):
        """Un reintento sobre la misma misión reescribe el tile en el mismo
        path — "immutable" le mentiría al navegador."""
        from webapp import main as M
        tile_dir = M.GEOVISOR_DIR / "tiles" / "thermal" / "0" / "0"
        tile_dir.mkdir(parents=True, exist_ok=True)
        (tile_dir / "0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        r = cliente.get("/geovisor/tiles/thermal/0/0/0.png")
        assert r.status_code == 200
        assert "immutable" not in r.headers.get("cache-control", "")
        assert r.headers.get("cache-control") == "no-cache"

    def test_bounds_json_sigue_sin_cachear(self, cliente):
        r = cliente.get("/geovisor/tiles/bounds.json")
        assert "immutable" not in r.headers.get("cache-control", "")
