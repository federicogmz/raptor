"""run_odm() en docker/entrypoint.sh corre ODM en DOS invocaciones de run.py,
no una: `--end-with opensfm` con una concurrencia liviana (todo el SfM —
detect/match_features, reconstruct, undistort— trabaja sobre fotos reducidas a
feature_process_size, muy lejos de la memoria por hilo que hace falta cuidar en
MVS) y `--rerun-from openmvs` con la concurrencia "segura" de siempre para
OpenMVS/band alignment.

DÓNDE SE PARTE IMPORTA, y el corte anterior estaba mal. El único lugar que
escribe opensfm/config.yaml —con `processes: %s % args.max_concurrency`, la
concurrencia que usan de verdad las sub-etapas de OpenSfM— es
OSFMContext.setup() en opendm/osfm.py, llamada desde la etapa **opensfm**
(stages/run_opensfm.py:32), NO desde dataset; y solo (re)escribe el config si
image_list.txt todavía no existe. Con `--end-with dataset` la fase 1 no llegaba
nunca a crear image_list.txt, así que el config lo terminaba escribiendo la
FASE 2 con la concurrencia PESADA: el split hacía exactamente lo contrario de
lo que buscaba. Medido en vivo sobre 20 núcleos: `processes: 2` en RGB y
`processes: 6` en multiespectral.

Estos tests EXTRAEN el bloque real (mismo patrón que el resto de
tests/test_*.py que tocan entrypoint.sh), así que no pueden quedar
desincronizados de la fuente.
"""
import os
import subprocess

ENTRYPOINT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docker", "entrypoint.sh")


def _bloque_run_odm():
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("LIGHT_CONCURRENCY=$(safe_concurrency 2)")
    fin = src.index("\n}\n", src.index("run_odm() {")) + 3
    return src[ini:fin]


STUBS = r"""
set -uo pipefail
safe_concurrency() { echo "9"; }
mkdir -p outputs/logs
"""


def _correr(guion_extra, args_extra="--feature-quality high", cwd=None):
    """Corre el bloque real en un directorio propio.

    `cwd` no es opcional por gusto: run_odm() escribe (y ahora TRUNCA)
    outputs/logs/odm_<label>.log relativo al directorio actual, y en el repo
    `outputs` es un symlink a la misión activa del contenedor. Sin un cwd
    aparte, el test escribiría sobre los logs de una misión real.
    """
    guion = STUBS + _bloque_run_odm() + f"""
{guion_extra}
run_odm rgb rgb_odm 3 7 {args_extra}
echo "EXIT=$?"
"""
    return subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                          timeout=15, cwd=str(cwd) if cwd else None)


def _stub_registra(registro, exit_code="0"):
    return f"""
python3() {{
  if [[ "$1" == "run.py" ]]; then
    echo "$*" >> "{registro}"
    exit {exit_code}
  fi
  cat >/dev/null  # odm_progress_filter.py (consume el pipe)
}}
"""


class TestDosFasesDeODM:
    def test_corre_dos_invocaciones_con_concurrencias_distintas(self, tmp_path):
        registro = tmp_path / "llamadas.txt"
        r = _correr(_stub_registra(registro), cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "EXIT=0" in r.stdout, r.stdout

        llamadas = registro.read_text().splitlines()
        assert len(llamadas) == 2, f"se esperaban 2 invocaciones de run.py, hubo {len(llamadas)}: {llamadas}"

        fase1, fase2 = llamadas
        assert "--max-concurrency 3" in fase1
        assert "--end-with opensfm" in fase1
        assert "--rerun-from" not in fase1

        assert "--max-concurrency 7" in fase2
        assert "--rerun-from openmvs" in fase2
        assert "--end-with" not in fase2

    def test_la_fase_que_escribe_config_yaml_lleva_la_concurrencia_liviana(self, tmp_path):
        """La regresión que este archivo existe para atajar.

        El config.yaml de OpenSfM lo escribe la etapa `opensfm`, así que la
        concurrencia que termina en `processes:` es la de la invocación que
        LLEGA a esa etapa por primera vez. Esa tiene que ser la liviana; si
        alguna vez vuelve a ser la pesada, features y undistort se arrastran
        con 2 hilos sobre 20 núcleos y nadie se entera hasta la próxima
        misión de 10 horas.
        """
        registro = tmp_path / "llamadas.txt"
        r = _correr(_stub_registra(registro), cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        fase1, fase2 = registro.read_text().splitlines()

        # La fase 1 termina EN opensfm (inclusive: ODM corre la etapa y recién
        # ahí para — opendm/types.py, `if args.end_with == self.name`).
        assert "--end-with opensfm" in fase1, fase1
        assert "--max-concurrency 3" in fase1, fase1

        # Y la fase 2 NO puede retomar en opensfm ni antes: `--rerun-from
        # opensfm` borra el proyecto de OpenSfM entero (osfm.py: rerun →
        # shutil.rmtree) y lo rehace con la concurrencia pesada, tirando todo
        # el SfM que la fase 1 acaba de pagar.
        assert "--rerun-from openmvs" in fase2, fase2
        for prohibido in ("--rerun-from dataset", "--rerun-from opensfm"):
            assert prohibido not in fase2, fase2

    def test_fast_orthophoto_corre_una_sola_invocacion(self, tmp_path):
        """Con --fast-orthophoto ODM ni conecta openmvs a la cadena de etapas
        (stages/odm_app.py: opensfm.connect(filterpoints) directo), así que no
        hay etapa pesada que proteger: partir la corrida ahí es puro costo."""
        registro = tmp_path / "llamadas.txt"
        r = _correr(_stub_registra(registro),
                    args_extra="--feature-quality high --fast-orthophoto",
                    cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "EXIT=0" in r.stdout, r.stdout

        llamadas = registro.read_text().splitlines()
        assert len(llamadas) == 1, f"se esperaba 1 sola invocación: {llamadas}"
        assert "--max-concurrency 3" in llamadas[0], llamadas[0]
        assert "--end-with" not in llamadas[0]
        assert "--rerun-from" not in llamadas[0]

    def test_pasa_los_argumentos_propios_de_odm_a_ambas_fases(self, tmp_path):
        registro = tmp_path / "llamadas.txt"
        r = _correr(_stub_registra(registro),
                    args_extra="--feature-quality high --pc-quality medium",
                    cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        fase1, fase2 = registro.read_text().splitlines()
        for fase in (fase1, fase2):
            assert "--feature-quality high" in fase
            assert "--pc-quality medium" in fase

    def test_si_falla_la_fase_1_no_corre_la_fase_2(self, tmp_path):
        registro = tmp_path / "llamadas.txt"
        r = _correr(_stub_registra(registro, exit_code="1"), cwd=tmp_path)
        assert "EXIT=0" not in r.stdout
        llamadas = registro.read_text().splitlines()
        assert len(llamadas) == 1, f"la fase 2 no debería correr si la 1 falló: {llamadas}"
        assert "--end-with opensfm" in llamadas[0]

    def test_si_falla_la_fase_2_se_propaga_el_error(self, tmp_path):
        registro = tmp_path / "llamadas.txt"
        guion_extra = f"""
python3() {{
  if [[ "$1" == "run.py" ]]; then
    echo "$*" >> "{registro}"
    if [[ "$*" == *"--end-with opensfm"* ]]; then exit 0; else exit 137; fi
  fi
  cat >/dev/null
}}
"""
        r = _correr(guion_extra, cwd=tmp_path)
        assert "EXIT=0" not in r.stdout
        assert "MATÓ el proceso" in r.stdout, r.stdout
        llamadas = registro.read_text().splitlines()
        assert len(llamadas) == 2

    def test_trunca_el_log_al_empezar(self, tmp_path):
        """Los logs se acumulaban entre corridas (el filtro abre en modo "a"),
        y odm_thermal.log de barbosa-picodegallo terminó con DOS
        reconstrucciones completas superpuestas — imposible leer de ahí cuánto
        tardó cada etapa. Se trunca acá, no en el filtro: las dos fases
        escriben al mismo archivo y truncar del lado del filtro borraría la
        fase 1 al arrancar la fase 2."""
        registro = tmp_path / "llamadas.txt"
        guion_extra = _stub_registra(registro) + """
mkdir -p outputs/logs
echo "RESTO DE LA CORRIDA ANTERIOR" > outputs/logs/odm_rgb.log
"""
        r = _correr(guion_extra, cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        contenido = (tmp_path / "outputs/logs/odm_rgb.log").read_text(encoding="utf-8")
        assert "RESTO DE LA CORRIDA ANTERIOR" not in contenido, contenido[:200]
