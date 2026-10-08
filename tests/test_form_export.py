"""Modal de exportación: que se ofrezca cuando de verdad está disponible.

LÍMITE DE ESTOS TESTS: la imagen no trae runtime de JavaScript, así que no se
ejecuta el modal — se comprueba su ESTRUCTURA sobre el fuente. Es menos que un
test de comportamiento, pero alcanza para que un rename de un lado (JS o
backend) no deje la exportación muda del otro.

La exportación ya NO se pregunta durante el setup (ver tests/test_webapp.py::
TestExportacionADemanda para el comportamiento real del backend) — se pregunta
por misión, al abrir el modal desde el botón "Exportar" del listado.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, "webapp", "static", "index.html")


def _js():
    html = open(INDEX, encoding="utf-8").read()
    return re.search(r"<script>(.*?)</script>", html, re.S).group(1)


def _cuerpo_de(js, nombre):
    """Cuerpo de una función, por conteo de llaves."""
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


class TestModalDeExportacion:
    def test_openExportModal_consulta_export_options(self):
        cuerpo = _cuerpo_de(_js(), "openExportModal")
        assert "export-options" in cuerpo, "no consulta /export-options"
        assert "export_enabled" in cuerpo
        assert "available_products" in cuerpo
        assert "default_export_dir" in cuerpo

    def test_si_falla_la_consulta_el_formulario_queda_deshabilitado(self):
        """Sin respuesta del servidor no se puede afirmar que la exportación
        esté disponible: el catch tiene que mostrar el aviso de deshabilitado,
        no dejar el formulario abierto con datos viejos."""
        cuerpo = _cuerpo_de(_js(), "openExportModal")
        catch = cuerpo[cuerpo.index("catch"):]
        assert re.search(r"xm-disabled.*remove\('hidden'\)", catch), (
            "el catch de openExportModal no muestra xm-disabled")
        assert re.search(r"xm-form.*add\('hidden'\)", catch), (
            "el catch de openExportModal no oculta xm-form")

    def test_renderExportModalProducts_solo_ofrece_lo_disponible(self):
        """Los checkboxes salen de xmAvailable (lo que devolvió el server para
        ESTA misión), no de un catálogo fijo — así no se ofrece exportar algo
        que la misión nunca generó."""
        cuerpo = _cuerpo_de(_js(), "renderExportModalProducts")
        assert "xmAvailable" in cuerpo

    def test_submitExportModal_manda_los_campos_que_el_backend_espera(self):
        cuerpo = _cuerpo_de(_js(), "submitExportModal")
        for campo in ("export_dir", "export_products", "export_raster_format",
                      "export_vector_format", "export_epsg"):
            assert campo in cuerpo, f"submitExportModal no manda {campo}"
        assert "xmMission" in cuerpo  # el POST va a la misión abierta, no a una fija


class TestBackendCoincideConElModal:
    """Las claves que el modal lee/manda tienen que ser las que el backend
    ofrece/espera; un rename de un lado deja la exportación muda del otro."""

    def test_export_options_manda_las_claves_que_el_modal_lee(self):
        backend = open(os.path.join(REPO, "webapp", "main.py"), encoding="utf-8").read()
        for clave in ("available_products", "export_enabled", "export_host_root",
                      "default_export_dir"):
            assert f'"{clave}"' in backend, f"{clave} no lo manda el backend"

    @pytest.mark.parametrize("campo", [
        "export_dir", "export_products", "export_raster_format",
        "export_vector_format", "export_epsg",
    ])
    def test_los_campos_del_modal_existen_en_export(self, campo):
        js = _js()
        backend = open(os.path.join(REPO, "webapp", "main.py"), encoding="utf-8").read()
        assert f"'{campo}'" in js or f'"{campo}"' in js, f"el modal no manda {campo}"
        assert f"{campo}:" in backend, f"/export no recibe {campo}"
