"""Formulario: que la exportación se ofrezca cuando de verdad está disponible.

LÍMITE DE ESTOS TESTS: la imagen no trae runtime de JavaScript, así que no se
ejecuta el formulario — se comprueba su ESTRUCTURA sobre el fuente. Es menos
que un test de comportamiento, pero alcanza para la regresión concreta que
motivó el archivo: `openSetup()` preguntaba al servidor si la exportación
estaba disponible SOLO al abrir una misión existente. Al crear una misión
nueva nunca preguntaba, `expEnabled` se quedaba en false y la entrega aparecía
deshabilitada aunque el contenedor se hubiera arrancado con la carpeta del host
montada.
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


class TestDisponibilidadDeExportacion:
    def test_openSetup_consulta_el_estado_tambien_en_mision_nueva(self):
        """La regresión: `await loadStatus(...)` tiene que estar FUERA del
        `if(name){…}else{…}`, porque la disponibilidad de la exportación
        depende de cómo se arrancó el contenedor, no de si la misión existe."""
        cuerpo = _cuerpo_de(_js(), "openSetup")
        assert "loadStatus" in cuerpo, "openSetup ya no consulta el estado"

        # Se recorta el bloque if/else completo; lo que quede es nivel función.
        m = re.search(r"\n  if\(name\)\{", cuerpo)
        assert m, "cambió la forma del if(name) — revisar este test"
        i = m.end()
        prof = 1
        while prof:
            if cuerpo[i] == "{":
                prof += 1
            elif cuerpo[i] == "}":
                prof -= 1
            i += 1
        resto = cuerpo[i:]                       # después del if
        # …y también hay que saltar el bloque else, si está
        m_else = re.match(r"\s*else\s*\{", resto)
        if m_else:
            j = m_else.end()
            prof = 1
            while prof:
                if resto[j] == "{":
                    prof += 1
                elif resto[j] == "}":
                    prof -= 1
                j += 1
            resto = resto[j:]

        assert "loadStatus" in resto, (
            "loadStatus() solo se llama dentro del if(name)/else: una misión "
            "NUEVA nunca preguntaría si la exportación está disponible")

    def test_loadStatus_toma_la_disponibilidad_del_servidor(self):
        cuerpo = _cuerpo_de(_js(), "loadStatus")
        assert "export_enabled" in cuerpo, "no lee export_enabled de /status"
        assert "expEnabled" in cuerpo, "no actualiza el estado del formulario"
        assert "export_host_root" in cuerpo

    def test_si_falla_la_consulta_la_exportacion_queda_deshabilitada(self):
        """Sin respuesta del servidor no se puede afirmar que esté disponible:
        el catch tiene que apagarla, no dejar un valor viejo."""
        cuerpo = _cuerpo_de(_js(), "loadStatus")
        catch = cuerpo[cuerpo.index("catch"):]
        assert re.search(r"expEnabled\s*=\s*false", catch), \
            "el catch de loadStatus no apaga expEnabled"

    def test_exporting_respeta_la_disponibilidad(self):
        """`exporting()` gobierna si se muestra el bloque y si se mandan los
        campos al arrancar: tiene que cortar por expEnabled antes que nada."""
        cuerpo = _cuerpo_de(_js(), "exporting")
        assert re.search(r"if\(!expEnabled\)\s*return false", cuerpo), cuerpo

    def test_el_estado_arranca_deshabilitado(self):
        """Por defecto NO se ofrece: se habilita solo cuando el servidor lo
        confirma, no al revés."""
        js = _js()
        assert re.search(r"let expEnabled\s*=\s*false", js)


class TestBackendCoincideConElFormulario:
    """Las claves que el formulario lee tienen que ser las que el backend
    manda; un rename de un lado deja la exportación muda del otro."""

    def test_las_claves_de_status_existen_en_el_backend(self):
        backend = open(os.path.join(REPO, "webapp", "main.py"), encoding="utf-8").read()
        for clave in ("export_enabled", "export_host_root", "default_export_dir"):
            assert f'"{clave}"' in backend, f"{clave} no lo manda el backend"

    @pytest.mark.parametrize("campo", [
        "export_dir", "export_products", "export_raster_format",
        "export_vector_format", "export_epsg",
    ])
    def test_los_campos_del_form_existen_en_start(self, campo):
        js = _js()
        backend = open(os.path.join(REPO, "webapp", "main.py"), encoding="utf-8").read()
        assert f"'{campo}'" in js or f'"{campo}"' in js, f"el form no manda {campo}"
        assert f"{campo}:" in backend, f"/start no recibe {campo}"
