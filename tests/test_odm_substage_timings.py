"""scripts/odm_progress_filter.py cronometra las sub-etapas de OpenSfM y las
deja en outputs/logs/odm_<id>_substages.json, y scripts/print_timings.py las
incorpora al resumen.

Por qué existe esto: "ODM multiespectral: 8:27" no dice si el tiempo se fue en
algo paralelizable o en algo secuencial por diseño, que es justo la diferencia
entre "se arregla con hilos" y "hay que cambiar de algoritmo". Para encontrar
que la banda D de barbosa-picodegallo eran 108 min de features + 56 de matching
+ 267 de SfM incremental + 107 de undistort hubo que parsear a mano los
timestamps de un log de 2.5 MB.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILTRO = os.path.join(REPO, "scripts", "odm_progress_filter.py")
TIMINGS = os.path.join(REPO, "scripts", "print_timings.py")

# Líneas tal como las imprime ODM/OpenSfM de verdad (copiadas de
# runs/barbosa-picodegallo/outputs/logs/odm_dband.log).
SALIDA_ODM = """[INFO]    Running dataset stage
[INFO]    Finished dataset stage
[INFO]    Running opensfm stage
2026-08-07 13:15:08,731 DEBUG: Extracting ROOT_DSPSIFT features for image DJI_0001_D.JPG
2026-08-07 13:15:19,102 DEBUG: Extracting ROOT_DSPSIFT features for image DJI_0002_D.JPG
2026-08-07 15:04:00,000 INFO: Matching 9087 pairs
2026-08-07 17:07:00,000 INFO: Adding DJI_0001_D.JPG to the reconstruction
2026-08-07 17:08:00,000 INFO: Adding DJI_0002_D.JPG to the reconstruction
2026-08-07 21:51:00,000 DEBUG: Undistorting image DJI_0001_D.JPG
[INFO]    Finished opensfm stage
"""


def _correr_filtro(tmp_path, salida, label="odm_dband.log", resume=None):
    logdir = tmp_path / "outputs" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, FILTRO, "ODM DBAND", str(logdir / label)]
    if resume:
        args.append(resume)
    return subprocess.run(args, input=salida, capture_output=True, text=True,
                          timeout=20, cwd=str(tmp_path))


class TestSubstages:
    def test_escribe_un_json_con_cada_sub_etapa(self, tmp_path):
        r = _correr_filtro(tmp_path, SALIDA_ODM)
        assert r.returncode == 0, r.stdout + r.stderr

        destino = tmp_path / "outputs" / "logs" / "odm_dband_substages.json"
        assert destino.is_file(), "el filtro no dejó el JSON de sub-etapas"
        datos = json.loads(destino.read_text())

        assert set(datos) == {"etapas", "features", "matching", "sfm", "undistort"}
        # Las ETAPAS de ODM son la medida principal: cubren el 100% del
        # tiempo, incluidas las que reportan progreso con \r (OpenMVS) y que
        # las sub-etapas no pueden ver.
        assert set(datos["etapas"]) == {"dataset", "opensfm"}
        assert datos["features"]["n"] == 2
        assert datos["sfm"]["n"] == 2
        assert datos["matching"]["n"] == 1
        assert datos["undistort"]["n"] == 1
        for clave, valores in datos.items():
            if clave == "etapas":
                assert all(v >= 0 for v in valores.values())
            else:
                assert valores["segundos"] >= 0

    def test_el_log_crudo_se_escribe_entero(self, tmp_path):
        """El buffer del log ya no se vacía línea por línea (era un write(2)
        por línea sobre millones de líneas), pero cerrar el archivo al terminar
        tiene que dejarlo completo igual — si no, entrypoint.sh volcaría un log
        truncado justo cuando ODM falla."""
        _correr_filtro(tmp_path, SALIDA_ODM)
        contenido = (tmp_path / "outputs" / "logs" / "odm_dband.log").read_text()
        assert contenido == SALIDA_ODM

    def test_las_dos_fases_se_suman_en_vez_de_pisarse(self, tmp_path):
        """run_odm() invoca el filtro DOS veces sobre el mismo log (SfM en la
        primera, MVS en la segunda). La segunda no puede borrar lo que midió la
        primera."""
        _correr_filtro(tmp_path, SALIDA_ODM)
        _correr_filtro(tmp_path,
                       "[INFO]    Running openmvs stage\n"
                       "[INFO]    Estimated depth-maps 1 (10.00%, ETA 5m)\n"
                       "[INFO]    Finished openmvs stage\n",
                       resume="openmvs")

        datos = json.loads((tmp_path / "outputs" / "logs" / "odm_dband_substages.json").read_text())
        assert "features" in datos, "la segunda fase pisó lo que midió la primera"
        # Las etapas de las DOS fases se acumulan en el mismo archivo.
        assert set(datos["etapas"]) == {"dataset", "opensfm", "openmvs"}

    def test_print_timings_incluye_las_sub_etapas(self, tmp_path):
        _correr_filtro(tmp_path, SALIDA_ODM)
        entorno = dict(os.environ, T_MISSION_START="1000", T_MISSION_END="4600")
        r = subprocess.run([sys.executable, TIMINGS], capture_output=True, text=True,
                           timeout=20, cwd=str(tmp_path), env=entorno)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Dentro de cada reconstrucción" in r.stdout, r.stdout
        assert "dentro del SfM" in r.stdout, r.stdout

        timings = json.loads((tmp_path / "outputs" / "logs" / "timings.json").read_text())
        assert timings["total"] == 3600
        assert "dband" in timings["substages"]
        assert "features" in timings["substages"]["dband"]
        assert "etapas" in timings["substages"]["dband"]
