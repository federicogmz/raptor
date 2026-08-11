"""MODE=thermal (solo ortomosaico térmico, sin reconstruir RGB) —
simétrico a MODE=rgb (solo RGB, sin térmico). RGB y térmico son proyectos
ODM independientes (processing/rgb_odm vs. processing/thermal_native_odm),
así que la carpeta del vuelo (SOURCE_DIR) sigue haciendo falta para llegar
a las fotos térmicas, pero RUN_RGB queda en 0 — no se reconstruye.

Estos tests EXTRAEN los bloques reales de docker/entrypoint.sh y
webapp/main.py, así que no pueden quedar desincronizados de la fuente.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")
sys.path.insert(0, REPO)


def _bloque_modo():
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index('if [[ "$MODE" != "rgb"')
    fin = src.index("\nfi", src.index("requiere un vuelo multiespectral")) + 3
    return src[ini:fin]


def _correr_modo(mode, ms_dir_existe=False):
    guion = f"""
set -uo pipefail
MODE={mode}
MS_SOURCE_DIR={"/tmp" if ms_dir_existe else "/no-existe-de-verdad"}
""" + _bloque_modo() + """
echo "RUN_RGB=$RUN_RGB RUN_THERMAL=$RUN_THERMAL RUN_MULTISPECTRAL=$RUN_MULTISPECTRAL"
"""
    return subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=10)


class TestBanderasPorModo:
    def test_rgb_thermal_corre_ambos(self):
        r = _correr_modo("rgb+thermal")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "RUN_RGB=1 RUN_THERMAL=1" in r.stdout

    def test_rgb_solo_no_corre_termico(self):
        r = _correr_modo("rgb")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "RUN_RGB=1 RUN_THERMAL=0" in r.stdout

    def test_thermal_solo_no_corre_rgb(self):
        r = _correr_modo("thermal")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "RUN_RGB=0 RUN_THERMAL=1" in r.stdout

    def test_none_no_corre_ninguno_de_los_dos(self):
        r = _correr_modo("none", ms_dir_existe=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "RUN_RGB=0 RUN_THERMAL=0" in r.stdout

    def test_none_sin_multiespectral_falla(self):
        r = _correr_modo("none", ms_dir_existe=False)
        assert r.returncode != 0
        assert "requiere un vuelo multiespectral" in r.stdout

    def test_thermal_sin_multiespectral_no_exige_nada_mas(self):
        """A diferencia de none, MODE=thermal por sí solo ya tiene qué
        procesar (el térmico) — no depende de que haya multiespectral."""
        r = _correr_modo("thermal", ms_dir_existe=False)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_modo_invalido_falla(self):
        r = _correr_modo("solo-termico-mal-escrito")
        assert r.returncode != 0
        assert "MODE debe ser" in r.stdout


class TestBloqueDsmPostProcesamiento:
    """La limpieza/recorte del DSM ya no es una etapa propia: corre inline,
    de fondo, dentro del job de recorte de RGB (o de multiespectral cuando
    no hay RGB) — ver POST_PIDS en docker/entrypoint.sh. Con SKIP_ODM=1 no
    hace falta stubear run_odm: solo importa qué recortes de fondo se
    lanzan según qué sensor está presente."""

    def _bloque(self):
        src = open(ENTRYPOINT, encoding="utf-8").read()
        ini = src.index("# ── Etapas: banderas + contador")
        marca = src.index("Revisá STAGE_FLAGS en docker/entrypoint.sh")
        fin = src.index("\nfi", marca) + 3
        return src[ini:fin]

    def _correr(self, run_rgb, run_ms, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        guion = f"""
set -uo pipefail
cd "{tmp_path}"
mkdir -p outputs/logs scripts
pipeline_progress_start() {{ :; }}
pipeline_progress_done()  {{ :; }}
publish_partial()         {{ :; }}
make() {{ echo "make:$1 DSM_SRC=${{DSM_SRC:-}} ODM_RGB_DIR=${{ODM_RGB_DIR:-}}"; }}
python3() {{
  if [[ "$1" == "scripts/hardware.py" ]]; then printf '2\\n2 2\\n4 4\\n'; return 0; fi
  if [[ "$1" == scripts/export_* ]]; then return 0; fi
  return 0
}}
LIGHT_CONCURRENCY=1
PREP_SPLIT=2
PRESET_TITULO=Estándar
PC_QUALITY=medium; FEAT_QUALITY=high; ODM_RES_CM=4; MIN_FEATURES=8000
SFM_ALGORITHM=incremental; MATCHER_NEIGHBORS=8
HYBRID_BA=1; HYBRID_BA_FLAG=()
DO_DBAND=0
SKIP_ODM=1
RUN_RGB={run_rgb}
RUN_MULTISPECTRAL={run_ms}
RUN_THERMAL=1
EXPORT_DIR=""
""" + self._bloque()
        return subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                              timeout=15, cwd=str(tmp_path))

    def test_rgb_limpia_su_propio_dsm(self, tmp_path):
        r = self._correr(run_rgb=1, run_ms=0, tmp_path=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "make:clean-dsm" in r.stdout

    def test_sin_rgb_pero_con_ms_usa_el_dsm_multiespectral(self, tmp_path):
        r = self._correr(run_rgb=0, run_ms=1, tmp_path=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "processing/multispectral_odm/odm_dem/dsm.tif" in r.stdout

    def test_solo_termico_sin_ms_no_toca_dsm(self, tmp_path):
        r = self._correr(run_rgb=0, run_ms=0, tmp_path=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "make:clean-dsm" not in r.stdout


class TestValidacionWebapp:
    def test_modo_thermal_es_valido(self):
        from webapp.main import VALID_MODES
        assert "thermal" in VALID_MODES

    def test_thermal_exige_fotos_termicas(self):
        from webapp.main import _validate
        uploads = {"rgb_thermal": {"rgb": 10, "thermal": 0, "total": 10},
                   "multispectral": {"ms": 0, "total": 0}}
        errors = _validate("thermal", False, uploads)
        assert any("térmicas" in e for e in errors)

    def test_thermal_con_fotos_termicas_no_exige_nada_mas(self):
        from webapp.main import _validate
        uploads = {"rgb_thermal": {"rgb": 10, "thermal": 10, "total": 20},
                   "multispectral": {"ms": 0, "total": 0}}
        assert _validate("thermal", False, uploads) == []

    def test_reusar_odm_en_modo_thermal_no_exige_rgb_reconstruido(self, tmp_path, monkeypatch):
        monkeypatch.chdir(REPO)
        from webapp.main import _validate_reuse_odm
        mision = tmp_path / "m1"
        (mision / "processing" / "thermal_native_odm" / "opensfm").mkdir(parents=True)
        (mision / "processing" / "thermal_native_odm" / "opensfm" / "reconstruction.json").write_text("[]")
        errors = _validate_reuse_odm(mision, "thermal", False, reuse_odm=True)
        assert errors == []

    def test_reusar_odm_en_modo_thermal_exige_termico_reconstruido(self, tmp_path, monkeypatch):
        monkeypatch.chdir(REPO)
        from webapp.main import _validate_reuse_odm
        mision = tmp_path / "m2"
        mision.mkdir()
        errors = _validate_reuse_odm(mision, "thermal", False, reuse_odm=True)
        assert errors and "térmico" in errors[0]


class TestPostProcesamientoTermicoSinRgb:
    """Bug real, encontrado en vivo: una misión MODE=thermal terminaba TODO
    el ODM (horas) y recién entonces fallaba, porque confidence_mask.py abre
    outputs/rgb_orthomosaic.tif sin chequear que exista — y en MODE=thermal
    (RUN_RGB=0) nunca existe. La corrida entera abortaba ahí, antes de
    llegar a generar tiles, así que el ortomosaico térmico —ya calculado—
    nunca llegaba a mostrarse en el geovisor."""

    def _bloque(self):
        """confidence-mask ya no comparte stage_begin con el recorte
        térmico: es su propio bloque, gateado por DO_CONFIANZA (que ya
        exige RUN_RGB Y DO_THERMAL — ver docker/entrypoint.sh)."""
        src = open(ENTRYPOINT, encoding="utf-8").read()
        ini = src.index("# ── Etapas: banderas + contador")
        marca = src.index("Revisá STAGE_FLAGS en docker/entrypoint.sh")
        fin = src.index("\nfi", marca) + 3
        return src[ini:fin]

    def test_confidence_mask_no_corre_sin_rgb(self):
        bloque = self._bloque()
        assert 'DO_CONFIANZA=0; [[ "$RUN_RGB" -eq 1 && "$DO_THERMAL" -eq 1 ]]' in bloque, (
            "DO_CONFIANZA (que gatea `make confidence-mask`, necesita "
            "outputs/rgb_orthomosaic.tif que en MODE=thermal no existe) "
            "tiene que exigir RUN_RGB")

    def test_trim_edges_thermal_corre_siempre(self):
        """El recorte del propio térmico no depende de RGB — solo la
        máscara de confianza (RGB ∩ térmico) lo necesita."""
        assert "make trim-edges-thermal" in self._bloque()

    def _correr(self, run_rgb):
        guion = f"""
set -uo pipefail
mkdir -p outputs/logs scripts
pipeline_progress_start() {{ echo "STAGE: $1"; }}
pipeline_progress_done()  {{ echo "DONE: $1"; }}
publish_partial()         {{ echo "PUBLISH"; }}
make() {{
  if [[ "$1" == "confidence-mask" ]]; then
    echo "confidence-mask: $([[ -f outputs/rgb_orthomosaic.tif ]] && echo ok || {{ echo "CRASH: no existe rgb_orthomosaic.tif"; exit 1; }})"
  else
    echo "make:$1"
  fi
}}
python3() {{ return 0; }}
LIGHT_CONCURRENCY=1
DO_DBAND=0
SKIP_ODM=1
RUN_RGB={run_rgb}
RUN_MULTISPECTRAL=0
RUN_THERMAL=1
EXPORT_DIR=""
""" + self._bloque()
        import subprocess
        return subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=10)

    def test_sin_rgb_no_crashea(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r = self._correr(run_rgb=0)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "confidence-mask" not in r.stdout
        assert "PUBLISH" in r.stdout

    def test_con_rgb_sigue_corriendo_confidence_mask(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "outputs").mkdir()
        (tmp_path / "outputs" / "rgb_orthomosaic.tif").write_bytes(b"x")
        r = self._correr(run_rgb=1)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "confidence-mask: ok" in r.stdout
