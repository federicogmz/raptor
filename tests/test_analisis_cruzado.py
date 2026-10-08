"""Análisis cruzado (docker/entrypoint.sh): máscara de confianza y calidad de
vuelo (hotspot térmico + situation.json incluidos, ver comentario grande
junto al bloque "Calidad del LEVANTAMIENTO" en entrypoint.sh). El tercer
camino que existía acá (área afectada + severidad, vía detect-area-afectada
y compute-severity) fue eliminado por completo — ya no hay DO_AREA ni ese
subshell.

confidence-mask es de solo lectura (confidence_mask.py lee outputs/
{rgb,thermal}_orthomosaic.tif ya definitivos y solo escribe confidence_mask.
tif) — ya no reescribe esos ortomosaicos in-place como antes, así que la
condición de carrera que motivó correrlo antes y sincrónico ya no existe.
Igual sigue corriendo así (primero, sincrónico, y recién después flight-
quality/área en paralelo entre sí) — simplemente no hay necesidad real de
cambiarlo, y estos tests fijan ese orden.

Este test EXTRAE el bloque real del entrypoint y lo corre con stubs, así que
no puede quedar desincronizado de la fuente.
"""
import os
import re
import subprocess


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")


def _bloque():
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("declare -A _CROSS_PID_LABEL=()")
    fin = src.index('[[ "${#_CROSS_PID_LABEL[@]}" -gt 0 ]] && publish_partial', ini)
    fin = src.index("\n", fin) + 1
    return src[ini:fin]


STUBS = r"""
set -uo pipefail
STAGE=0
stage_begin() { STAGE=$((STAGE+1)); echo "stage_begin:$1"; }
pipeline_progress_done() { echo "done:$1"; }
publish_partial() { echo "publish_partial"; }
PIPELINE_HAD_FAILURE=0
# Este bloque solo se extrae DESPUÉS de que las reconstrucciones ODM ya
# corrieron (ver ODM_ORDEN/_SENSOR_OK/_dep_ok más arriba en entrypoint.sh) —
# lo que se prueba acá es el ORDEN y el manejo de fallos DENTRO del análisis
# cruzado, no el gate de "¿falló el sensor?" (eso lo cubre test_odm_paralelo.py).
# Se estubea siempre-ok para no tener que reconstruir todo ese estado.
_dep_ok() { return 0; }
"""


def _bloque_completo():
    """Desde _odm_mp() (donde arrancan ODM_ORDEN/_SENSOR_OK/_wait_sensor/
    _dep_ok REALES, sin estubear) hasta el final del resumen de fallos del
    análisis cruzado — el despacho completo MÁS confianza/área/flight-
    quality, para probar la propiedad de punta a punta: área/flight-quality
    esperan solo lo suyo, no a TODOS los sensores despachados."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("_odm_mp() {")
    fin = src.index('echo "   El detalle de cada fallo está más arriba."', ini)
    fin = src.index("\nfi\n", fin) + len("\nfi\n")
    return src[ini:fin]


def _correr(guion_extra, do_confianza=1, do_thermal=1, do_ms=0, timeout=15):
    guion = STUBS + f"""
DO_CONFIANZA={do_confianza}
DO_THERMAL={do_thermal}
DO_MS={do_ms}
""" + guion_extra + "\n" + _bloque() + '\necho "SALIDA=$?"\necho "PIPELINE_HAD_FAILURE=$PIPELINE_HAD_FAILURE"\n'
    return subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                          timeout=timeout)


class TestConfianzaYFlightQualityCorrenEnParalelo:
    """confidence-mask pasó a ser de solo lectura (lee rgb/thermal_
    orthomosaic.tif ya definitivos, solo escribe confidence_mask.tif que
    nadie más lee — ver confidence_mask.py) — la condición de carrera que
    antes exigía correrla sincrónica y primera ya no existe. Sincrónica-
    primera además se había vuelto un problema nuevo: confianza SÍ depende
    de RGB, así que bloquearla en el hilo principal ANTES de flight-quality
    la bloqueaba detrás de RGB también, aunque flight-quality no lo
    necesite. Ahora ambas (confianza/flight-quality) se lanzan en subshells
    de fondo, cada una esperando solo lo suyo — este test confirma que SE
    SOLAPAN, no que una espera a la otra."""

    def test_confianza_y_flight_quality_corren_en_paralelo(self):
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
        assert m["vuelo-ini"] < m["confianza-fin"], (
            f"flight-quality esperó a que confianza terminara — ya no "
            f"debería, ninguna depende de la otra: {m}")


class TestHotspotYSituacionSiempreConTermico:
    """compute_thermal_hotspot.py (ver scripts/raster_classify.py) es ahora
    la ÚNICA fuente de clasificación de hotspot para toda misión con
    térmico — antes solo se invocaba acá cuando NO había multiespectral (con
    MS, la fuente era compute-severity, recortada al área afectada; ese
    camino y el módulo que lo generaba ya no existen). Debe correr siempre
    que haya térmico, con o sin multiespectral."""

    def test_con_ms_tambien_genera_hotspot_y_situacion(self):
        guion_extra = r"""
make() { echo "make:$1"; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=0, do_thermal=1, do_ms=1)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "make:flight-quality" in r.stdout
        assert "make:compute-thermal-hotspot" in r.stdout
        assert "make:situation-summary" in r.stdout

    def test_sin_ms_tambien_genera_hotspot_y_situacion(self):
        guion_extra = r"""
make() { echo "make:$1"; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=0, do_thermal=1, do_ms=0)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "make:compute-thermal-hotspot" in r.stdout
        assert "make:situation-summary" in r.stdout


class TestExportacionPorProducto:
    def test_confianza_exporta_su_propio_cog(self):
        # do_thermal=1: combinación real (DO_CONFIANZA solo se prende cuando
        # RUN_RGB Y DO_THERMAL están en 1, ver docker/entrypoint.sh) — con
        # do_thermal=0 _CROSS_PID_LABEL queda vacío y la última línea del
        # bloque ([[ "${#_CROSS_PID_LABEL[@]}" -gt 0 ]] && publish_partial)
        # da $?=1 sin que nada haya fallado (mismo caso ya cubierto en
        # test_sin_ninguno_de_los_tres_no_publica_tiles_de_gratis).
        guion_extra = r"""
make() { :; }
python3() { echo "python3:$*"; return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_thermal=1)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "python3:scripts/export_cog.py outputs/confidence_mask.tif" in r.stdout


class TestManejoDeFallos:
    def test_un_fallo_de_confianza_no_corta_pero_reporta_su_propio_error(self):
        """confianza corre en su propio subshell de fondo, igual que
        flight-quality/área (ver _CROSS_PID_LABEL) — un fallo ahí se reporta
        con su propio nombre vía el mismo mensaje genérico del wait loop, no
        con un mensaje especial sincrónico. Con etapas independientes (ver
        _dep_ok en entrypoint.sh), un fallo acá ya NO corta el resto de la
        corrida — el resto de las etapas no dependen de que confianza haya
        salido bien, así que siguen. El fallo se acumula en
        PIPELINE_HAD_FAILURE para el código de salida final."""
        guion_extra = r"""
make() { [[ "$1" == "confidence-mask" ]] && exit 1; :; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_thermal=1, do_ms=0)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "falló máscara de confianza" in r.stdout, r.stdout
        assert "PIPELINE_HAD_FAILURE=1" in r.stdout

    def test_un_fallo_en_flight_quality_no_corta_pero_reporta_su_error(self):
        """flight-quality corre en su propio subshell dentro del grupo
        paralelo, y un fallo ahí ya no corta al resto de la corrida — solo
        se reporta con su propio nombre (antes era un mensaje genérico
        compartido)."""
        guion_extra = r"""
make() { [[ "$1" == "flight-quality" ]] && exit 1; :; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_thermal=1, do_ms=0)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "falló calidad de vuelo" in r.stdout, r.stdout
        assert "PIPELINE_HAD_FAILURE=1" in r.stdout

    def test_stage_begin_se_emite_solo_para_confianza(self):
        """Calidad de vuelo es liviana y no tiene stage_begin propio (ver
        comentario junto a su bloque en entrypoint.sh) — dentro de este
        bloque de análisis cruzado, la única etapa que se anuncia es
        confianza."""
        guion_extra = r"""
make() { :; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=1, do_ms=1, do_thermal=1)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "stage_begin:Máscara de confianza" in r.stdout
        assert "done:Máscara de confianza lista" in r.stdout
        assert "stage_begin:Calidad de vuelo" not in r.stdout

    def test_sin_ninguno_de_los_dos_no_publica_tiles_de_gratis(self):
        """Si DO_CONFIANZA/DO_THERMAL están ambos en 0 (p.ej. una misión sin
        térmico ni RGB, imposible en la práctica pero el bloque no debería
        asumirlo), no debería llamar publish_partial sin haber hecho nada.

        No se afirma "SALIDA=0" acá: la última línea del bloque real es
        `[[ "${#_CROSS_PID_LABEL[@]}" -gt 0 ]] && publish_partial`, y con
        _CROSS_PID_LABEL vacío el test de la izquierda del && es falso — el $?
        que queda es 1, pero eso es inofensivo bajo `set -e` real (errexit
        NO se dispara en el lado izquierdo de una lista &&, solo en el
        comando final) y el script de verdad sigue de largo. Acá solo se
        chequea el efecto observable: nada corrió, nada explotó."""
        guion_extra = r"""
make() { :; }
python3() { return 0; }
"""
        r = _correr(guion_extra, do_confianza=0, do_thermal=0, do_ms=0)
        assert "ERROR" not in r.stdout, r.stdout + r.stderr
        assert "publish_partial" not in r.stdout


STUBS_DESPACHO_COMPLETO = r"""
set -uo pipefail
STAGE=0
stage_begin() { STAGE=$((STAGE+1)); echo "stage_begin:$1"; }
pipeline_progress_done() { echo "done:$1"; }
publish_partial() { :; }
nproc() { echo 16; }
PRESET=estandar; PRESET_NOMBRE=estandar; PRESET_TITULO=Estándar
PC_QUALITY=medium; FEAT_QUALITY=high; ODM_RES_CM=4; MIN_FEATURES=8000
SFM_ALGORITHM=incremental; MATCHER_NEIGHBORS=8
HYBRID_BA=1; HYBRID_BA_FLAG=(--use-hybrid-bundle-adjustment)
FAST_ORTHOPHOTO_RGB=0; FAST_ORTHOPHOTO_THERMAL=0
PREP_SPLIT=4
SKIP_ODM=0
"""


class TestCalidadDeVueloNoEsperaASensoresQueNoNecesita:
    """Prueba de punta a punta (dispatch REAL + análisis cruzado REAL, sin
    estubear _dep_ok esta vez) de la propiedad central de la independencia
    entre etapas: calidad de vuelo (flight-quality, que solo depende de
    térmico) no debería quedarse esperando a que RGB termine su
    reconstrucción — mucho más lenta (SfM incremental, secuencial por
    diseño) — aunque no necesita nada de RGB. Con _wait_sensor/_dep_ok lazy
    (ver el comentario grande en entrypoint.sh), calidad de vuelo tiene que
    arrancar apenas térmico esté listo, sin importar si RGB sigue
    corriendo. (Esto reemplaza el escenario histórico, probado igual antes
    de este refactor con detect-area-afectada — módulo hoy eliminado —, que
    tenía la misma propiedad de independencia respecto de RGB.)"""

    def test_flight_quality_arranca_antes_de_que_termine_rgb(self, tmp_path):
        extra = r"""
cd "%s"
mkdir -p outputs/logs scripts
RUN_RGB=1; RUN_THERMAL=1; RUN_MULTISPECTRAL=1; DO_DBAND=0
DO_THERMAL=1; DO_MS=1; DO_CONFIANZA=1; DO_TRIM=1
T0=$(date +%%s%%N)
marca() { echo "MARCA $1 $(( ($(date +%%s%%N)-T0)/1000000 ))"; }
make() {
  case "$1" in
    flight-quality)   marca "vuelo-ini" ;;
    confidence-mask)  marca "confianza-ini" ;;
    *) : ;;
  esac
}
python3() {
  if [[ "$1" == "scripts/hardware.py" ]]; then printf '3\n2 2 2\n2 2 2\n'; return 0; fi
  return 0
}
run_odm() {
  if [[ "$1" == "rgb" ]]; then
    sleep 1.5
    marca "rgb-fin"
  fi
  return 0
}
""" % str(tmp_path)
        guion = STUBS_DESPACHO_COMPLETO + extra + "\n" + _bloque_completo() + '\necho "SALIDA=$?"\n'
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                           timeout=30, cwd=str(tmp_path))
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        m = dict((n, int(t)) for n, t in re.findall(r"MARCA (\S+) (\d+)", r.stdout))
        assert "vuelo-ini" in m, r.stdout + r.stderr
        assert "rgb-fin" in m, r.stdout + r.stderr
        assert m["vuelo-ini"] < m["rgb-fin"], (
            f"calidad de vuelo esperó a que RGB terminara aunque no depende "
            f"de RGB (solo de térmico): {m}")
        # Confianza SÍ depende de RGB (rgb+térmico) — tiene que esperarlo,
        # a diferencia de calidad de vuelo. Mismo escenario, resultado
        # opuesto a propósito: confirma que la independencia es del sensor
        # correcto, no que "nada espera a nada".
        assert "confianza-ini" in m, r.stdout + r.stderr
        assert m["confianza-ini"] >= m["rgb-fin"], (
            f"máscara de confianza SÍ depende de RGB — no debería arrancar "
            f"antes de que termine: {m}")
