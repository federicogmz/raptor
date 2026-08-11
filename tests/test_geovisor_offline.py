"""El geovisor tiene que abrir SIN internet, y servirse comprimido.

RAPTOR es una herramienta de respuesta a incendios: se usa en campo, donde la
conexión es el hotspot de un celular o directamente nada. Con Leaflet cargado
desde unpkg.com el geovisor no abría — ni el mapa, ni el panel, ni los
productos ya procesados que estaban en el disco de al lado. Los tiles del
mapa BASE (CARTO/Esri) sí son inevitablemente remotos: no se pueden
empaquetar, pero su ausencia tiene que explicarse en vez de dejar un fondo
gris que se lee como "está roto".
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEOVISOR = os.path.join(REPO, "geovisor")


def _leer(*partes):
    with open(os.path.join(GEOVISOR, *partes), encoding="utf-8") as f:
        return f.read()


class TestSinCDN:
    def test_leaflet_se_sirve_desde_la_imagen(self):
        html = _leer("index.html")
        assert 'href="vendor/leaflet.css"' in html
        assert 'src="vendor/leaflet.js"' in html

    def test_no_queda_ningun_script_ni_hoja_de_estilo_remota(self):
        """Lo que de verdad importa: que NADA de lo necesario para abrir el
        geovisor venga de la red. Los tiles del mapa base y los enlaces de
        atribución sí son remotos y no cuentan."""
        html = _leer("index.html")
        remotos = re.findall(r'<(?:script|link)[^>]*(?:src|href)="(https?://[^"]+)"', html)
        assert remotos == [], f"cargas remotas en el geovisor: {remotos}"

    @pytest.mark.parametrize("archivo", ["leaflet.js", "leaflet.css"])
    def test_los_archivos_vendorizados_existen_y_no_estan_vacios(self, archivo):
        ruta = os.path.join(GEOVISOR, "vendor", archivo)
        assert os.path.isfile(ruta), f"falta {ruta}"
        assert os.path.getsize(ruta) > 10_000, "parece truncado"

    def test_leaflet_vendorizado_es_la_version_esperada(self):
        """Si alguien lo reemplaza por otra versión, que se note acá y no en
        una misión."""
        assert "1.9.4" in _leer("vendor", "leaflet.js")[:2000]

    def test_los_iconos_de_leaflet_tambien_estan(self):
        """leaflet.css los pide por ruta relativa (images/marker-icon.png):
        sin ellos los marcadores salen rotos aunque el JS cargue bien."""
        for img in ("marker-icon.png", "marker-shadow.png", "layers.png"):
            ruta = os.path.join(GEOVISOR, "vendor", "images", img)
            assert os.path.isfile(ruta), f"falta {ruta}"


class TestAvisoDeSinMapaBase:
    def test_las_tres_capas_base_reportan_error_de_tile(self):
        js = _leer("app.js")
        assert "tileerror" in js
        assert "avisarSiSinMapaBase" in js
        assert "[osmBase,satBase,satLabels]" in js

    def test_no_avisa_por_un_tile_suelto(self):
        """Un 404 aislado en el borde del área es normal. El aviso solo tiene
        sentido cuando fallan varios."""
        js = _leer("app.js")
        m = re.search(r"_tilesBaseFallidos\s*<\s*(\d+)", js)
        assert m, "no se encontró el umbral de tiles fallados"
        assert int(m.group(1)) >= 3, "el umbral es demasiado bajo"

    def test_avisa_una_sola_vez(self):
        assert "_avisoOfflineDado" in _leer("app.js")

    def test_el_aviso_dice_que_las_capas_de_la_mision_si_se_ven(self):
        """El punto del mensaje: distinguir "no hay mapa de fondo" de "el
        geovisor está roto"."""
        js = _leer("app.js")
        assert "Sin conexión al mapa base" in js
        assert "se ven igual" in js

    def test_el_aviso_tiene_estilo(self):
        assert ".aviso-offline" in _leer("style.css")


class TestCompresion:
    def test_la_webapp_monta_gzip(self):
        with open(os.path.join(REPO, "webapp", "main.py"), encoding="utf-8") as f:
            src = f.read()
        assert "GZipMiddleware" in src
        assert "add_middleware(GZipMiddleware" in src

    def test_las_respuestas_de_texto_grandes_llegan_comprimidas(self, tmp_path,
                                                                monkeypatch):
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        from fastapi.testclient import TestClient
        import webapp.main as M
        import importlib
        importlib.reload(M)

        c = TestClient(M.app)
        r = c.get("/geovisor/app.js", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip", dict(r.headers)
