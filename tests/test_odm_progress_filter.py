"""scripts/odm_progress_filter.py — el resume_from opcional suprime los
headers/barras de etapas anteriores a esa, para la fase 2 de run_odm()
(docker/entrypoint.sh): ODM siempre reimprime "Running dataset stage" en
CUALQUIER invocación (stages/odm_app.py arranca la cadena en "dataset" pase
lo que pase), aunque esa etapa sea un no-op real cuando ya corrió antes —
sin resume_from, la fase 2 mostraba "[1/13] Cargando dataset" de nuevo,
dando la falsa impresión de que estaba reprocesando imágenes.
"""
import os
import subprocess

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "odm_progress_filter.py")

ENTRADA = (
    "Running dataset stage\n"
    "Finished dataset stage\n"
    "Running opensfm stage\n"
    "Finished opensfm stage\n"
)


def _correr(args):
    # print_stage_header/print_progress/print_done (scripts/progress.py)
    # escriben a STDERR a propósito (una barra de progreso no es "salida" en
    # el sentido de datos, y así no se mezcla con algo que alguien redirija).
    r = subprocess.run(["python3", SCRIPT, *args], input=ENTRADA,
                        capture_output=True, text=True, timeout=10)
    return r.stderr


class TestResumeFrom:
    def test_sin_resume_from_muestra_todas_las_etapas(self, tmp_path):
        salida = _correr(["ODM RGB", str(tmp_path / "log.txt")])
        assert "Cargando dataset" in salida
        assert "SfM" in salida

    def test_con_resume_from_opensfm_suprime_dataset(self, tmp_path):
        salida = _correr(["ODM RGB", str(tmp_path / "log.txt"), "opensfm"])
        assert "Cargando dataset" not in salida
        assert "SfM" in salida

    def test_el_log_crudo_completo_se_escribe_igual(self, tmp_path):
        """Suprimir el header no debe perder nada del log real por si falla."""
        logfile = tmp_path / "log.txt"
        _correr(["ODM RGB", str(logfile), "opensfm"])
        contenido = logfile.read_text()
        assert "Running dataset stage" in contenido
        assert "Running opensfm stage" in contenido

    def test_resume_from_desconocido_no_rompe_nada(self, tmp_path):
        salida = _correr(["ODM RGB", str(tmp_path / "log.txt"), "algo-que-no-existe"])
        assert "Cargando dataset" in salida
