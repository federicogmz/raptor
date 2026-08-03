"""Geovisor: el resumen de situación no debe romperse cuando la misión no
tiene multiespectral (situation.json con severidad=null, area_ha=null —
ver compute_situation_summary.py, modo solo_termico).

LÍMITE: estructural sobre el fuente, no de comportamiento — la imagen no trae
runtime de JavaScript (mismo caso que test_form_export.py). Lo que SÍ se
prueba de punta a punta con GDAL real es de dónde sale s.solo_termico (ver
tests/test_situation_summary.py).
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO, "geovisor", "app.js")


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


class TestNoRompeSinMultiespectral:
    """La regresión concreta: `s.severidad.dominante` sin guarda revienta
    con TypeError apenas s.severidad es null (misión solo-térmico)."""

    def test_buildRecommendationText_verifica_solo_termico_antes_de_severidad(self):
        cuerpo = _cuerpo_de(_js(), "buildRecommendationText")
        i = cuerpo.index("s.solo_termico")
        assert cuerpo.index("s.severidad.dominante") > i, \
            "tiene que chequear solo_termico ANTES de tocar s.severidad"

    def test_renderSummaryCards_no_accede_severidad_sin_guarda(self):
        cuerpo = _cuerpo_de(_js(), "renderSummaryCards")
        assert "s.solo_termico?" in cuerpo or "s.solo_termico ?" in cuerpo
        # Todo acceso a s.severidad.dominante tiene que quedar DENTRO de la
        # rama que ya descartó solo_termico (el IIFE de la rama false).
        assert "()=>{" in cuerpo, "se esperaba la rama no-solo_termico como IIFE"

    def test_openReport_reusa_buildRecommendationText_en_vez_de_duplicar(self):
        """Antes duplicaba la lógica a mano acá adentro (con el mismo
        s.severidad.dominante sin guarda) — se unificó para no tener dos
        lugares que puedan desincronizarse o romperse por separado."""
        cuerpo = _cuerpo_de(_js(), "openReport")
        assert "buildRecommendationText(s)" in cuerpo
        assert "solo_termico" in cuerpo, \
            "report-stats también tiene que distinguir el modo solo-térmico"
        i_guarda = cuerpo.index("solo_termico")
        i_severidad = cuerpo.index("s.severidad.dominante")
        assert i_guarda < i_severidad, \
            "el chequeo de solo_termico tiene que preceder al acceso a s.severidad"

    def test_buildReportCanvas_no_imprime_null_para_area_ha(self):
        cuerpo = _cuerpo_de(_js(), "buildReportCanvas")
        assert "s.solo_termico?" in cuerpo


class TestFechaDeCaptura:
    def test_capture_date_label_lee_situation_captura(self):
        js = _js()
        assert "capture-date-label" in js
        assert "s?.captura" in js or "s.captura" in js
