"""Contador de etapas de la barra de progreso (docker/entrypoint.sh).

El total y la numeración salen de una sola declaración (STAGE_FLAGS +
stage_begin). Antes eran dos cuentas paralelas y bastaba agregar una etapa sin
sumarla al total para que la barra terminara en "4/5".

Estos tests EXTRAEN el bloque real del entrypoint y lo ejecutan con stubs, así
que no pueden quedar desincronizados de la fuente: si alguien agrega una etapa
y se olvida del flag, fallan acá.

El bloque real va desde las banderas hasta el chequeo final de desfase — hoy
eso incluye el despacho asíncrono por sensor (semáforo de reconstrucción,
preparación, recorte, exportación por sensor) y el análisis cruzado en
paralelo, no solo una lista de `make` secuenciales como antes. Los stubs
cubren esa superficie: python3 (hardware.py + export_cog/copc.py) y make.
"""
import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")

# Todo lo que la etapa invoca de verdad se reemplaza por un stub que solo
# registra: acá se prueba el CONTEO, no el pipeline.
STUBS = r"""
set -uo pipefail
pipeline_progress_start() { echo "STAGE $2/$3 $1"; }
pipeline_progress_done()  { :; }
publish_partial()         { :; }
make()                    { :; }
run_odm()                 { :; }
safe_concurrency()        { echo 2; }
nproc()                   { echo 8; }
LIGHT_CONCURRENCY=1
PREP_SPLIT=2
PRESET_TITULO=Estándar
PC_QUALITY=medium; FEAT_QUALITY=high; ODM_RES_CM=4; MIN_FEATURES=8000
SFM_ALGORITHM=incremental; MATCHER_NEIGHBORS=8
HYBRID_BA=1; HYBRID_BA_FLAG=()
FAST_ORTHOPHOTO_RGB=0
# DO_DBAND se decide antes de este bloque (según archivos *_D.JPG encontrados
# en la organización de datos), no a partir de MODE/RUN_* como el resto —
# acá no es el foco del test, se le da el default de "no hay banda D".
DO_DBAND="${DO_DBAND:-0}"
SKIP_ODM="${SKIP_ODM:-0}"
SUB_SAMPLE="${SUB_SAMPLE:-0}"
python3() {
  if [[ "$1" == "scripts/hardware.py" ]]; then
    printf '2\n2 2 2 2\n4 4 4 4\n'
    return 0
  fi
  if [[ "$1" == scripts/export_* ]]; then return 0; fi
  return 0
}
"""


def _bloque_de_etapas():
    """El bloque de etapas + despacho por sensor + análisis cruzado + tiles
    + exportación, tal como está hoy en el entrypoint — desde las banderas
    hasta el chequeo final de desfase."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("# ── Etapas: banderas + contador")
    marca = src.index("Revisá STAGE_FLAGS en docker/entrypoint.sh")
    fin = src.index("\nfi", marca) + 3
    return src[ini:fin]


def _correr(mode, run_rgb, run_ms, export_dir="", tmp_path=None, export_products="rgb,thermal"):
    # export_products deliberadamente SIN "confidence" ni "all" por default:
    # DO_CONFIANZA ahora también exige que ese producto puntual esté pedido
    # (ver el comentario grande junto a DO_CONFIANZA en entrypoint.sh — nada
    # más lo consume), así que un export_dir puesto sin pensar en eso no
    # debería, de rebote, sumar la etapa de confianza en tests que no la
    # están probando a propósito.
    tmp_path.mkdir(parents=True, exist_ok=True)
    guion = STUBS + f"""
cd "{tmp_path}"
mkdir -p outputs/logs scripts
MODE={mode}
RUN_RGB={run_rgb}
RUN_MULTISPECTRAL={run_ms}
EXPORT_DIR="{export_dir}"
EXPORT_PRODUCTS="{export_products}"
RUN_THERMAL=0; [[ "$MODE" == "rgb+thermal" || "$MODE" == "thermal" ]] && RUN_THERMAL=1
""" + _bloque_de_etapas() + "\necho \"FINAL $STAGE $TOTAL_STAGES\"\n"
    r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                       timeout=30, cwd=str(tmp_path))
    assert r.returncode == 0, f"el bloque falló:\n{r.stdout}\n{r.stderr}"
    etapas = re.findall(r"^STAGE (\d+)/(\d+) (.+)$", r.stdout, re.M)
    final = re.search(r"^FINAL (\d+) (\d+)$", r.stdout, re.M)
    return etapas, int(final.group(1)), int(final.group(2)), r.stdout


# (descripción, MODE, RUN_RGB, RUN_MULTISPECTRAL, EXPORT_DIR)
CONFIGS = [
    ("RGB solo",                      "rgb",         1, 0, ""),
    ("RGB + térmico",                 "rgb+thermal", 1, 0, ""),
    ("RGB + multiespectral",          "rgb",         1, 1, ""),
    ("RGB + térmico + multiespectral", "rgb+thermal", 1, 1, ""),
    ("solo multiespectral",           "none",        0, 1, ""),
    ("RGB + entrega",                 "rgb",         1, 0, "/export"),
    ("completo + entrega",            "rgb+thermal", 1, 1, "/export"),
    ("solo multiespectral + entrega", "none",        0, 1, "/export"),
]


class TestContadorDeEtapas:
    @pytest.mark.parametrize("desc,mode,rgb,ms,exp", CONFIGS)
    def test_el_total_coincide_con_las_etapas_que_corren(self, desc, mode, rgb, ms, exp, tmp_path):
        """Lo que la barra promete al empezar es lo que efectivamente pasa."""
        etapas, stage_final, total, salida = _correr(mode, rgb, ms, exp, tmp_path)
        assert len(etapas) == total, (
            f"{desc}: corrieron {len(etapas)} etapas pero el total dice {total}\n{salida}")
        assert stage_final == total, f"{desc}: contador final {stage_final} != {total}"
        assert "progreso desfasado" not in salida, desc

    @pytest.mark.parametrize("desc,mode,rgb,ms,exp", CONFIGS)
    def test_la_numeracion_es_consecutiva_desde_1(self, desc, mode, rgb, ms, exp, tmp_path):
        etapas, _, total, salida = _correr(mode, rgb, ms, exp, tmp_path)
        nums = [int(n) for n, _, _ in etapas]
        assert nums == list(range(1, total + 1)), f"{desc}: numeración {nums}\n{salida}"

    @pytest.mark.parametrize("desc,mode,rgb,ms,exp", CONFIGS)
    def test_el_denominador_es_constante_durante_la_corrida(self, desc, mode, rgb, ms, exp, tmp_path):
        """La barra no puede cambiar de denominador a mitad de camino."""
        etapas, _, total, _ = _correr(mode, rgb, ms, exp, tmp_path)
        assert {int(d) for _, d, _ in etapas} == {total}

    def test_la_entrega_suma_exactamente_una_etapa(self, tmp_path):
        sin, _, total_sin, _ = _correr("rgb+thermal", 1, 1, "", tmp_path / "sin")
        con, _, total_con, _ = _correr("rgb+thermal", 1, 1, "/export", tmp_path / "con")
        assert total_con == total_sin + 1
        assert len(con) == len(sin) + 1
        assert "entrega" in con[-1][2].lower()

    def test_pedir_confianza_en_la_entrega_suma_una_etapa_mas(self, tmp_path):
        """confidence_mask.tif no lo usa nada del pipeline salvo la entrega
        (ver DO_CONFIANZA en entrypoint.sh) — pedirla explícitamente en
        EXPORT_PRODUCTS tiene que sumar SU PROPIA etapa además de la de
        entrega, no pedirla no debería sumar nada de más."""
        sin_confianza, _, total_sin, _ = _correr(
            "rgb+thermal", 1, 1, "/export", tmp_path / "sin_confianza",
            export_products="rgb,thermal")
        con_confianza, _, total_con, _ = _correr(
            "rgb+thermal", 1, 1, "/export", tmp_path / "con_confianza",
            export_products="rgb,thermal,confidence")
        assert total_con == total_sin + 1
        assert len(con_confianza) == len(sin_confianza) + 1
        assert any("confianza" in n.lower() for _, _, n in con_confianza)
        assert not any("confianza" in n.lower() for _, _, n in sin_confianza)

    def test_detecta_el_desfase_si_alguien_agrega_una_etapa_sin_su_flag(self, tmp_path):
        """La red de seguridad del propio entrypoint: se agrega un stage_begin
        de más y tiene que avisar en vez de dejar la barra mintiendo."""
        guion = STUBS + f"""
cd "{tmp_path}"
mkdir -p outputs/logs scripts
MODE=rgb
RUN_RGB=1
RUN_MULTISPECTRAL=0
RUN_THERMAL=0
EXPORT_DIR=""
""" + _bloque_de_etapas().replace(
            'stage_begin "Generación de tiles XYZ"',
            'stage_begin "Etapa nueva sin flag"\nstage_begin "Generación de tiles XYZ"', 1)
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                           timeout=30, cwd=str(tmp_path))
        assert "progreso desfasado" in r.stdout, \
            f"el entrypoint no avisó del desfase:\n{r.stdout}"
