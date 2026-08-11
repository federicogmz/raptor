"""scripts/print_timings.py: resumen de tiempos por etapa a partir de los
T_*_START/T_*_END que docker/entrypoint.sh exporta durante la corrida."""
import json
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "print_timings.py")


def _correr(env_extra, cwd):
    env = {**os.environ, **env_extra}
    return subprocess.run(["python3", SCRIPT], cwd=str(cwd),
                          capture_output=True, text=True, env=env, timeout=10)


class TestPrintTimings:
    def test_imprime_solo_las_etapas_que_corrieron(self, tmp_path):
        r = _correr({
            "T_MISSION_START": "1000", "T_MISSION_END": "4600",
            "T_PREP_START": "1000", "T_PREP_END": "1500",
            "T_ODM_RGB_START": "1500", "T_ODM_RGB_END": "3000",
            "T_ODM_THERMAL_START": "", "T_ODM_THERMAL_END": "",
            "T_POST_START": "3600", "T_POST_END": "4600",
        }, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "Preparación" in r.stdout
        assert "ODM RGB" in r.stdout
        assert "ODM Térmico" not in r.stdout, "no debería listar una etapa que no corrió"
        assert "Total de la misión" in r.stdout

    def test_escribe_outputs_logs_timings_json(self, tmp_path):
        (tmp_path / "outputs").mkdir()
        r = _correr({
            "T_MISSION_START": "0", "T_MISSION_END": "100",
            "T_PREP_START": "0", "T_PREP_END": "20",
        }, tmp_path)
        assert r.returncode == 0, r.stderr
        data = json.loads((tmp_path / "outputs" / "logs" / "timings.json").read_text())
        assert data["prep"] == 20
        assert data["total"] == 100
        assert "odm_rgb" not in data

    def test_sin_ninguna_variable_no_falla(self, tmp_path):
        r = _correr({}, tmp_path)
        assert r.returncode == 0, r.stderr

    def test_incluye_la_banda_d(self, tmp_path):
        """La banda D es un ODM propio (procesamiento extra, opt-in) — el
        resumen tenía que mostrarla; antes T_ODM_DBAND_* ni siquiera se
        exportaba, así que nunca aparecía."""
        r = _correr({
            "T_MISSION_START": "0", "T_MISSION_END": "100",
            "T_ODM_DBAND_START": "10", "T_ODM_DBAND_END": "50",
        }, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "ODM Banda D" in r.stdout
        data = json.loads((tmp_path / "outputs" / "logs" / "timings.json").read_text())
        assert data["odm_dband"] == 40

    def test_sin_banda_d_no_la_lista(self, tmp_path):
        r = _correr({
            "T_MISSION_START": "0", "T_MISSION_END": "10",
            "T_ODM_DBAND_START": "", "T_ODM_DBAND_END": "",
        }, tmp_path)
        assert "ODM Banda D" not in r.stdout
