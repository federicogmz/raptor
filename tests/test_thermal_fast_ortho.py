"""--fast-orthophoto en el TÉRMICO (vistazo/rápido).

El térmico (sensor de 640×512, 0.33 MP) pagaba la reconstrucción densa
completa — confirmado en vivo (mision_2026-08-08, vistazo+escarpado): la
etapa openmvs sola se llevó ~3.3 h de las 6.2 h del térmico, para una nube
densa que ningún producto usa (el térmico no pide --dsm; su entrega es el
ortomosaico en °C del render nativo de ODM). Con fast-orthophoto la malla
sale de la nube dispersa (mismo mecanismo que banda D).

Estos tests extraen el bloque REAL de docker/entrypoint.sh (_odm_args y el
chequeo de CUDA), así que no pueden quedar desincronizados de la fuente.
"""
import os
import re
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")


def _bloque_odm_args():
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("_odm_args() {")
    fin = src.index("  esac\n}", ini) + len("  esac\n}")
    return src[ini:fin]


def _args_termico(fast_ortho_thermal=0, fast_ortho_rgb=0):
    """Corre la rama thermal de _odm_args() real con las variables que lee."""
    guion = f"""
set -uo pipefail
FEAT_QUALITY=medium; PC_QUALITY=medium; ODM_RES_CM=15
MIN_FEATURES=4000; MATCHER_NEIGHBORS=8; SFM_ALGORITHM=incremental
FAST_ORTHOPHOTO_THERMAL={fast_ortho_thermal}
{_bloque_odm_args()}
_odm_args thermal
"""
    return subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                          timeout=15)


class TestFastOrthophotoTermico:
    def test_vistazo_rapido_agrega_fast_orthophoto(self):
        r = _args_termico(fast_ortho_thermal=1)
        assert r.returncode == 0, r.stderr
        assert "--fast-orthophoto" in r.stdout, r.stdout

    def test_estandar_en_adelante_no_lo_agrega(self):
        r = _args_termico(fast_ortho_thermal=0)
        assert r.returncode == 0, r.stderr
        assert "--fast-orthophoto" not in r.stdout, r.stdout

    def test_mantiene_la_calibracion_radiometrica(self):
        """fast-orthophoto acelera la fase densa, pero el render nativo con
        calibración Kelvin→°C tiene que seguir ahí — es el ortomosaico en °C."""
        r = _args_termico(fast_ortho_thermal=1)
        assert "--radiometric-calibration camera" in r.stdout, r.stdout

    def test_fast_ortho_termico_sigue_a_rgb_en_el_entrypoint(self):
        """FAST_ORTHOPHOTO_THERMAL se deriva del mismo preset que RGB — una
        sola regla en un solo lugar, sin dos copias que se desincronicen."""
        src = open(ENTRYPOINT, encoding="utf-8").read()
        assert re.search(r"FAST_ORTHOPHOTO_THERMAL=\"\$FAST_ORTHOPHOTO_RGB\"", src)


class TestChequeoCudaTieneEnCuentaAlTermico:
    """El chequeo temprano de CUDA (ver test_chequeo_cuda.py) exige el
    runtime solo a los sensores que DE VERDAD van a correr la etapa densa.
    Con el térmico en fast-orthophoto (vistazo/rápido) esa condición tiene
    que incluir a la variable nueva: si no, el chequeo seguiría exigiendo
    CUDA a una corrida que nunca la toca."""

    def _condicion(self):
        src = open(ENTRYPOINT, encoding="utf-8").read()
        ini = src.index('if [[ "$SKIP_ODM" -eq 0 \\')
        fin = src.index("&& -x \"$_DENSIFY\" ]]", ini)
        return src[ini:fin]

    def test_la_condicion_menciona_a_FAST_ORTHOPHOTO_THERMAL(self):
        assert "FAST_ORTHOPHOTO_THERMAL" in self._condicion()
