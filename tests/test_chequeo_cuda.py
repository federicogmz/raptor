"""El chequeo temprano del runtime de CUDA (docker/entrypoint.sh).

Encontrado corriendo una misión de verificación SIN `--gpus all`, siguiendo lo
que decía el propio README ("sin GPU, omitir --gpus: ODM cae a CPU solo"). Es
falso para esta imagen base: el binario de reconstrucción densa está enlazado
contra libcuda.so.1 y ni siquiera carga sin el runtime de NVIDIA —

    DensifyPointCloud: error while loading shared libraries: libcuda.so.1
    opendm.system.SubprocessException: Child returned 127

La corrida moría en la etapa openmvs, o sea DESPUÉS de la preparación y de
todo el SfM. En una misión real eso son horas tiradas para terminar en un
error que no dice qué pasó. Ahora se comprueba en el primer segundo.

Los tests extraen el bloque REAL del entrypoint y lo corren con un `ldd`
simulado, así que no pueden quedar desincronizados de la fuente.
"""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")


def _bloque():
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("_DENSIFY=/code/SuperBuild/install/bin/DensifyPointCloud")
    fin = src.index('if [[ ( "$RUN_RGB" -eq 1 || "$RUN_THERMAL" -eq 1 ) && ! -d "$SOURCE_DIR" ]]', ini)
    return src[ini:fin]


def _correr(*, falta_libcuda, skip_odm=0, rgb=1, thermal=0, ms=0,
            fast_ortho_rgb=0, tmp_path):
    # DensifyPointCloud simulado: tiene que existir y ser ejecutable para que
    # el chequeo llegue al ldd.
    falso = tmp_path / "DensifyPointCloud"
    falso.write_text("#!/bin/sh\n")
    falso.chmod(0o755)
    salida_ldd = ("\tlibcuda.so.1 => not found" if falta_libcuda
                  else "\tlibcuda.so.1 => /usr/lib/x86_64-linux-gnu/libcuda.so.1")
    guion = f"""
set -uo pipefail
SKIP_ODM={skip_odm}; RUN_RGB={rgb}; RUN_THERMAL={thermal}; RUN_MULTISPECTRAL={ms}
FAST_ORTHOPHOTO_RGB={fast_ortho_rgb}
ldd() {{ printf '%s\\n' "{salida_ldd}"; }}
""" + _bloque().replace("_DENSIFY=/code/SuperBuild/install/bin/DensifyPointCloud",
                         f'_DENSIFY={falso}') + '\necho "SIGUIO=ok"\n'
    return subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                          timeout=20)


class TestChequeoDeCuda:
    def test_aborta_si_falta_libcuda(self, tmp_path):
        r = _correr(falta_libcuda=True, tmp_path=tmp_path)
        assert r.returncode != 0
        assert "SIGUIO=ok" not in r.stdout, "tenía que abortar ANTES de seguir"
        assert "falta el runtime de CUDA" in r.stdout, r.stdout

    def test_el_mensaje_dice_qué_hacer(self, tmp_path):
        """Un error que no dice cómo salir del problema es medio error."""
        salida = _correr(falta_libcuda=True, tmp_path=tmp_path).stdout
        assert "--gpus all" in salida
        assert "openmvs" in salida, "hay que decir DÓNDE habría muerto"

    def test_no_estorba_cuando_libcuda_esta(self, tmp_path):
        r = _correr(falta_libcuda=False, tmp_path=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SIGUIO=ok" in r.stdout

    def test_no_chequea_si_se_reusa_la_reconstruccion(self, tmp_path):
        """SKIP_ODM=1 no corre ODM: exigir CUDA ahí sería bloquear por gusto
        una corrida que solo re-hace el post-procesamiento."""
        r = _correr(falta_libcuda=True, skip_odm=1, tmp_path=tmp_path)
        assert r.returncode == 0, r.stdout
        assert "SIGUIO=ok" in r.stdout

    def test_no_chequea_si_ningun_sensor_usa_la_etapa_densa(self, tmp_path):
        """Una misión de solo banda D corre con --fast-orthophoto, que ni
        toca DensifyPointCloud (stages/odm_app.py: opensfm.connect(
        filterpoints) directo)."""
        r = _correr(falta_libcuda=True, rgb=0, thermal=0, ms=0, tmp_path=tmp_path)
        assert r.returncode == 0, r.stdout
        assert "SIGUIO=ok" in r.stdout

    @pytest.mark.parametrize("sensor", ["rgb", "thermal", "ms"])
    def test_chequea_para_cada_sensor_que_si_la_usa(self, sensor, tmp_path):
        kwargs = {"rgb": 0, "thermal": 0, "ms": 0, sensor: 1}
        r = _correr(falta_libcuda=True, tmp_path=tmp_path, **kwargs)
        assert r.returncode != 0, f"{sensor} usa la etapa densa y no se chequeó"

    def test_no_chequea_rgb_con_fast_orthophoto(self, tmp_path):
        """Bug real: RGB en vistazo/rápido (terreno escarpado o no) salta
        DensifyPointCloud igual que banda D — antes este chequeo lo exigía
        igual, bloqueando una corrida CPU-only que nunca iba a tocar CUDA."""
        r = _correr(falta_libcuda=True, rgb=1, thermal=0, ms=0,
                    fast_ortho_rgb=1, tmp_path=tmp_path)
        assert r.returncode == 0, r.stdout
        assert "SIGUIO=ok" in r.stdout

    def test_si_chequea_rgb_sin_fast_orthophoto(self, tmp_path):
        r = _correr(falta_libcuda=True, rgb=1, thermal=0, ms=0,
                    fast_ortho_rgb=0, tmp_path=tmp_path)
        assert r.returncode != 0

    def test_chequea_igual_si_otro_sensor_si_usa_la_etapa_densa(self, tmp_path):
        """RGB solo no necesita CUDA con fast-orthophoto, pero si además
        corre térmico (que SIEMPRE pasa por DensifyPointCloud), sigue
        haciendo falta."""
        r = _correr(falta_libcuda=True, rgb=1, thermal=1, ms=0,
                    fast_ortho_rgb=1, tmp_path=tmp_path)
        assert r.returncode != 0, "térmico sigue necesitando CUDA"


class TestDocumentacionCoherente:
    """Comprobación POSITIVA, no por ausencia de una frase: el entrypoint y el
    README explican por qué hace falta --gpus (con el síntoma exacto, para
    que sea buscable) Y que el chequeo es condicional — RGB en vistazo/rápido
    y banda D usan --fast-orthophoto y no lo necesitan."""

    def test_el_readme_explica_la_gpu_y_la_excepcion(self):
        readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
        assert "libcuda.so.1" in readme
        assert "Child returned 127" in readme
        assert "fast-orthophoto" in readme

    def test_el_entrypoint_documenta_lo_mismo(self):
        src = open(ENTRYPOINT, encoding="utf-8").read()
        assert "libcuda.so.1" in src
        assert "`--gpus all` SIEMPRE" in src
        assert "fast-orthophoto" in src
