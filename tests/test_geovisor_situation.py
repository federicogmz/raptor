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
        """Mismo criterio para solo_termico Y sin_impacto_detectado (misión
        con los dos sensores pero sin área/foco que superara el umbral) —
        ninguno de los dos tiene area_ha real que mostrar."""
        cuerpo = _cuerpo_de(_js(), "buildReportCanvas")
        assert "s.solo_termico||s.sin_impacto_detectado" in cuerpo


class TestFechaDeCaptura:
    def test_capture_date_label_lee_situation_captura(self):
        js = _js()
        assert "capture-date-label" in js
        assert "s?.captura" in js or "s.captura" in js


class TestCalidadDelLevantamientoReemplazaConfianza:
    """Reportado en dos vueltas:

    1. La tarjeta "Confianza del dato" mostraba "Baja" sin ningún número
       detrás — se le agregó s.cobertura_pct al subtítulo (commit anterior).
    2. El concepto en sí no servía: saber qué fracción del ortomosaico
       térmico tiene dato real no dice si el VUELO estuvo bien volado, que
       es la pregunta real detrás de "¿confío en esto?". Se reemplazó por
       "Calidad del levantamiento" (compute_flight_quality.py: solape de
       cámaras, velocidad de vuelo, % de imágenes reconstruidas — el mismo
       lenguaje que un reporte de Terra/Agisoft/Pix4D), en las tres
       superficies que antes mostraban confianza: la tarjeta del panel en
       vivo, la píldora de la imagen exportada y el encabezado de
       "Situación actual"."""

    def test_renderSummaryCards_ya_no_usa_s_confianza(self):
        cuerpo = _cuerpo_de(_js(), "renderSummaryCards")
        assert "s.confianza" not in cuerpo
        assert "s.cobertura_pct" not in cuerpo
        assert "flightQualityCardHTML(fq)" in cuerpo

    def test_flightQualityCardHTML_usa_las_senales_de_calidad_del_vuelo(self):
        cuerpo = _cuerpo_de(_js(), "flightQualityCardHTML")
        assert "fq.calidad" in cuerpo
        assert "fq.solape_p50" in cuerpo
        assert "fq.velocidad_media_ms" in cuerpo
        assert "reconMin" in cuerpo  # % de imágenes reconstruidas, por sensor

    def test_renderSituationHeader_ya_no_usa_s_confianza(self):
        cuerpo = _cuerpo_de(_js(), "renderSituationHeader")
        assert "s?.confianza" not in cuerpo and "s.confianza" not in cuerpo
        assert "fq?.calidad" in cuerpo

    def test_buildReportCanvas_pildora_usa_calidad_no_confianza(self):
        cuerpo = _cuerpo_de(_js(), "buildReportCanvas")
        assert "s.confianza" not in cuerpo
        assert "fq.calidad" in cuerpo

    def test_buildRecommendationText_ya_no_menciona_confianza(self):
        """La calidad del vuelo tiene su propia píldora (ver
        buildReportCanvas) — repetirla acá duplicaba la misma cifra dos
        veces en la misma imagen."""
        cuerpo = _cuerpo_de(_js(), "buildRecommendationText")
        assert "onfianza" not in cuerpo

    def test_ultima_captura_muestra_el_equipo_real(self):
        """Reportado: la tarjeta "Última captura" decía "Dron UAV" genérico
        sin importar qué equipo se usó de verdad — ahora usa
        fq.equipo (compute_flight_quality.py, Make/Model EXIF real del
        proyecto RGB, p.ej. "DJI Zenmuse H20T")."""
        cuerpo = _cuerpo_de(_js(), "renderSummaryCards")
        assert "fq?.equipo||'Dron UAV'" in cuerpo


class TestExportNoAnunciaLoQueFalta:
    """Reportado: la imagen de "Generar resumen de situación" (y su vista
    previa en el modal) decían "Sin MS" / "esta misión no tiene
    multiespectral: agregalo..." — una pieza pensada para COMPARTIR fuera
    del geovisor (por WhatsApp, en un reporte) no debería anunciar lo que
    falta ni invitar a una acción que solo existe DENTRO de la app; en su
    lugar debe mostrar temperatura real del ortomosaico térmico, que existe
    tenga o no la misión focos activos."""

    def test_buildRecommendationText_no_invita_a_agregar_ms(self):
        cuerpo = _cuerpo_de(_js(), "buildRecommendationText")
        assert "agregalo" not in cuerpo.lower()
        assert "no tiene multiespectral" not in cuerpo.lower()
        assert "s.temp_max" in cuerpo and "s.temp_promedio" in cuerpo

    def test_buildReportCanvas_no_dice_sin_ms(self):
        cuerpo = _cuerpo_de(_js(), "buildReportCanvas")
        assert "['Sin MS'" not in cuerpo, \
            "la tarjeta que anunciaba 'Sin MS' en la imagen exportada sigue ahí"
        assert "s.temp_max" in cuerpo and "s.temp_promedio" in cuerpo

    def test_openReport_no_dice_sin_multiespectral(self):
        cuerpo = _cuerpo_de(_js(), "openReport")
        assert "no hay área afectada ni severidad para mostrar" not in cuerpo
        assert "s.temp_max" in cuerpo and "s.temp_promedio" in cuerpo
