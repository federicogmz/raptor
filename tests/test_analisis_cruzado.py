"""Análisis cruzado (docker/entrypoint.sh): máscara de confianza, calidad de
vuelo y área afectada+severidad leen ortomosaicos/índices ya recortados, pero
NO se leen ni se escriben entre sí — confirmado archivo por archivo:
  confidence-mask   lee outputs/{rgb,thermal}_orthomosaic.tif
  flight-quality    lee reconstruction.json + esos mismos dos ortomosaicos
  área+severidad    lee outputs/{multispectral,thermal}_orthomosaic.tif
                     + outputs/indices/*.tif
Antes corrían estrictamente uno detrás del otro por costumbre, no por
necesidad real.

Este test EXTRAE el bloque real del entrypoint y lo corre con stubs, así que
no puede quedar desincronizado de la fuente.
"""
import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")


def _bloque():
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index('[[ "$DO_CONFIANZA" -eq 1 ]] && stage_begin "Máscara de confianza"')
    fin = src.index('[[ "${#CROSS_PIDS[@]}" -gt 0 ]] && publish_partial', ini)
    fin = src.index("\n", fin) + 1
    return src[ini:fin]


STUBS = r"""
set -uo pipefail
STAGE=0
stage_begin() { STAGE=$((STAGE+1)); echo "stage_begin:$1"; }
pipeline_progress_done() { echo "done:$1"; }
publish_partial() { echo "publish_partial"; }
"""


def _correr(guion_extra, do_confianza=1, do_thermal=1, do_ms=0, do_area=0, timeout=15):
    guion = STUBS + f"""
DO_CONFIANZA={do_confianza}
DO_THERMAL={do_thermal}
DO_MS={do_ms}
DO_AREA={do_area}
""" + guion_extra + "\n" + _bloque() + '\necho "SALIDA=$?"\n'
    return subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                          timeout=timeout)


class TestLosTresCorrenEnParalelo:
    def test_confianza_y_area_se_solapan_no_van_uno_detras_del_otro(self):
        """Antes 'confidence-mask espera a que terminen los recortes' se leía
        como 'y TERMINA antes de que arranque área afectada' — hoy los dos
        corren a la vez. Se comparan intervalos, no el orden de finalización:
        con jobs de fondo el orden de los prints no está garantizado."""
        guion_extra = r"""
T0=$(date +%s%N)
marca() { echo "MARCA $1 $(( ($(date +%s%N)-T0)/1000000 ))"; }
make() {
  case "$1" in
    confidence-mask)       marca "confianza-ini"; sleep 0.3; marca "confianza-fin" ;;
    detect-area-afectada)  marca "area-ini"; sleep 0.3 ;;
    compute-severity)      : ;;
    situation-summary)     marca "area-fin" ;;
    flight-quality)        : ;;
  esac
}
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_area=1, do_ms=1, timeout=30)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        m = dict((n, int(t)) for n, t in re.findall(r"MARCA (\S+) (\d+)", r.stdout))
        assert m["area-ini"] < m["confianza-fin"] and m["confianza-ini"] < m["area-fin"], (
            f"confianza y área no se solaparon: {m}")

    def test_flight_quality_no_espera_a_confianza(self):
        guion_extra = r"""
T0=$(date +%s%N)
marca() { echo "MARCA $1 $(( ($(date +%s%N)-T0)/1000000 ))"; }
make() {
  case "$1" in
    confidence-mask) marca "confianza-ini"; sleep 0.3; marca "confianza-fin" ;;
    flight-quality)  marca "vuelo-ini"; sleep 0.05; marca "vuelo-fin" ;;
  esac
}
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_thermal=1, do_ms=1, timeout=30)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        m = dict((n, int(t)) for n, t in re.findall(r"MARCA (\S+) (\d+)", r.stdout))
        # flight-quality (rápido) termina mucho antes de que confidence-mask
        # (lento acá) siquiera empiece a terminar — si esperara a confianza,
        # vuelo-fin sería >= confianza-fin.
        assert m["vuelo-fin"] < m["confianza-fin"], m


class TestHotspotYSituacionSoloSinMultiespectral:
    def test_con_ms_no_genera_hotspot_ni_situacion_desde_termico(self):
        guion_extra = r"""
make() { echo "make:$1"; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=0, do_thermal=1, do_ms=1, do_area=0)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "make:compute-thermal-hotspot" not in r.stdout
        assert "make:flight-quality" in r.stdout

    def test_sin_ms_genera_hotspot_y_situacion(self):
        guion_extra = r"""
make() { echo "make:$1"; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=0, do_thermal=1, do_ms=0, do_area=0)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "make:compute-thermal-hotspot" in r.stdout
        assert "make:situation-summary" in r.stdout


class TestExportacionPorProducto:
    def test_confianza_exporta_su_propio_cog(self):
        guion_extra = r"""
make() { :; }
python3() { echo "python3:$*"; return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_thermal=0)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "python3:scripts/export_cog.py outputs/confidence_mask.tif" in r.stdout


class TestManejoDeFallos:
    def test_un_fallo_en_cualquiera_corta_con_error_claro(self):
        guion_extra = r"""
make() { [[ "$1" == "confidence-mask" ]] && exit 1; :; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_thermal=1, do_ms=0)
        assert "SALIDA=0" not in r.stdout
        assert "falló el análisis cruzado" in r.stdout, r.stdout

    def test_stage_begin_se_emite_para_confianza_y_area(self):
        guion_extra = r"""
make() { :; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_area=1, do_ms=1, do_thermal=1)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "stage_begin:Máscara de confianza" in r.stdout
        assert "stage_begin:Área afectada + clasificación de severidad" in r.stdout
        assert "done:Máscara de confianza lista" in r.stdout
        assert "done:Área afectada y severidad listas" in r.stdout

    def test_sin_ninguno_de_los_tres_no_publica_tiles_de_gratis(self):
        """Si DO_CONFIANZA/DO_THERMAL/DO_AREA están todos en 0 (p.ej. una
        misión sin térmico ni RGB, imposible en la práctica pero el bloque
        no debería asumirlo), no debería llamar publish_partial sin haber
        hecho nada.

        No se afirma "SALIDA=0" acá: la última línea del bloque real es
        `[[ "${#CROSS_PIDS[@]}" -gt 0 ]] && publish_partial`, y con
        CROSS_PIDS vacío el test de la izquierda del && es falso — el $?
        que queda es 1, pero eso es inofensivo bajo `set -e` real (errexit
        NO se dispara en el lado izquierdo de una lista &&, solo en el
        comando final) y el script de verdad sigue de largo. Acá solo se
        chequea el efecto observable: nada corrió, nada explotó."""
        guion_extra = r"""
make() { :; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=0, do_thermal=0, do_ms=0, do_area=0)
        assert "ERROR" not in r.stdout, r.stdout + r.stderr
        assert "publish_partial" not in r.stdout
