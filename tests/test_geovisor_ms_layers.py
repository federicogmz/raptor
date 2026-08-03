"""Geovisor: severidad/hotspot/índices clasificados no deben ofrecerse en el
panel cuando la misión no tiene los datos de origen — y cuando el motivo es
"falta el vuelo multiespectral", tiene que haber una vía directa para
agregarlo.

LÍMITE DE ESTOS TESTS: la imagen no trae runtime de JavaScript (mismo caso que
tests/test_form_export.py), así que son ESTRUCTURALES sobre el fuente, no de
comportamiento. La parte que SÍ se prueba de punta a punta es de dónde sale la
señal que consumen (bounds.json's capas_disponibles — ver
tests/test_generate_tiles.py, que ejercita el código real de GDAL).
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO, "geovisor", "app.js")
INDEX_HTML = os.path.join(REPO, "webapp", "static", "index.html")


def _js():
    return open(APP_JS, encoding="utf-8").read()


def _cuerpo_de(js, nombre):
    m = re.search(rf"function {nombre}\s*\([^)]*\)\s*{{", js)
    assert m, f"no se encontró {nombre}()"
    i = m.end()
    prof = 1
    while prof:
        if js[i] == "{":
            prof += 1
        elif js[i] == "}":
            prof -= 1
        i += 1
    return js[m.end():i - 1]


class TestRegistroCondicionado:
    """severidad/hotspot_termico/índices clasificados: el objeto Layer de
    Leaflet existe siempre (su pane ya está creado desde antes), pero la
    entrada en LAYER_REGISTRY —lo que los hace aparecer en el panel— se crea
    SOLO si CAPAS_DISPONIBLES lo confirma."""

    @pytest.mark.parametrize("fn,capa", [
        ("registerSeveridad", "'severidad'"),
        ("registerHotspot", "'hotspot_termico'"),
    ])
    def test_verifica_capas_disponibles_antes_de_registrar(self, fn, capa):
        cuerpo = _cuerpo_de(_js(), fn)
        assert "CAPAS_DISPONIBLES.has(" in cuerpo and capa in cuerpo
        assert "return false" in cuerpo

    def test_index_class_verifica_capas_disponibles(self):
        cuerpo = _cuerpo_de(_js(), "registerIndexClass")
        assert "CAPAS_DISPONIBLES.has(name)" in cuerpo
        assert "return false" in cuerpo

    def test_no_quedo_ninguna_asignacion_incondicional_de_severidad(self):
        """La regresión concreta: antes de este fix,
        `LAYER_REGISTRY.severidad={...}` corría SIEMPRE, sin ningún `if`
        antepuesto. Ahora solo debe existir DENTRO de registerSeveridad()."""
        js = _js()
        asignaciones = [m.start() for m in re.finditer(
            r"LAYER_REGISTRY\.severidad\s*=\s*\{", js)]
        assert len(asignaciones) == 1, \
            "tiene que haber exactamente una asignación (dentro de registerSeveridad)"
        cuerpo = _cuerpo_de(js, "registerSeveridad")
        assert "LAYER_REGISTRY.severidad={" in cuerpo or "LAYER_REGISTRY.severidad = {" in cuerpo

    def test_bounds_json_capas_disponibles_alimenta_el_registro(self):
        js = _js()
        assert "b.capas_disponibles" in js
        assert "CAPAS_DISPONIBLES=new Set(" in js

    def test_el_sondeo_en_vivo_reintenta_el_registro(self):
        """pollBoundsForChanges corre cada 6s durante una misión en curso —
        severidad/hotspot pueden aparecer bastante después de que bounds.json
        cambie por primera vez, y el registro tiene que reintentarse ahí, no
        solo una vez al cargar la página."""
        cuerpo = _cuerpo_de(_js(), "pollBoundsForChanges")
        assert "registerSeveridad()" in cuerpo
        assert "registerHotspot()" in cuerpo
        assert "registerIndexClass" in cuerpo


class TestBotonAgregarMultiespectral:
    def test_existe_la_funcion_de_llamada_a_la_accion(self):
        js = _js()
        assert "function addMsCtaHTML" in js
        assert "addms=1" in js

    def test_se_ofrece_cuando_el_grupo_de_indices_esta_vacio(self):
        cuerpo = _cuerpo_de(_js(), "layersPanelHTML")
        assert "addMsCtaHTML" in cuerpo
        assert "'indices'" in cuerpo or '"indices"' in cuerpo

    def test_la_webapp_reconoce_el_parametro_addms(self):
        """El botón linkea a /?mission=X&addms=1 — la webapp tiene que leerlo
        y marcar el sensor multiespectral solo, no dejar que el usuario tenga
        que descubrir el checkbox por su cuenta."""
        html = open(INDEX_HTML, encoding="utf-8").read()
        js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        assert "addms" in js
        assert "sensors.ms = true" in js


class TestReusarOdmNoSaltaUnSensorNuevo:
    """docker/entrypoint.sh: SKIP_ODM es un interruptor GLOBAL (una sola
    reconstrucción, no una por sensor) — agregar multiespectral a una misión
    que ya tenía RGB+térmico y pedir 'reusar' saltearía la reconstrucción MS
    que nunca existió, sin ningún error claro más adelante."""

    def test_start_rechaza_reusar_si_falta_reconstruir_el_sensor_nuevo(self):
        """La función en sí (_validate_reuse_odm) se prueba de punta a punta
        en tests/test_webapp.py::TestAgregarSensorYReusarOdm — acá solo se
        confirma que start_mission() la llama de verdad."""
        main_py = os.path.join(REPO, "webapp", "main.py")
        src = open(main_py, encoding="utf-8").read()
        assert "def _validate_reuse_odm(" in src
        i = src.index("def _validate_reuse_odm(")
        bloque = src[i:i + 1600]
        assert 'odm_prev["multispectral"]' in bloque
        assert 'odm_prev["rgb"]' in bloque
        assert 'odm_prev["thermal"]' in bloque
        assert "errors += _validate_reuse_odm(mission_dir, mode, has_multispectral, reuse_odm)" in src
