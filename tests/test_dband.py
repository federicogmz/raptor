"""Banda D del M3M (cámara RGB, mosaico visible rápido) — módulo opcional
opt-in, independiente de las 4 bandas espectrales y del vuelo M3T/H20T.
Cubre: organización de archivos (docker/setup-data-multispectral.sh),
preparación para ODM (scripts/prepare_dband_odm.py), y el conteo de
etapas en docker/entrypoint.sh (STAGE_FLAGS + presupuesto de concurrencia
compartido con multiespectral/térmico).
"""
import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP_DATA_MS = os.path.join(REPO, "docker", "setup-data-multispectral.sh")
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")


def _correr_setup(src, cwd):
    """Mismo patrón que test_setup_data_symlink.py: setup-data-multispectral.sh
    calcula DATA_DIR relativo a la ubicación DEL PROPIO script, no al cwd."""
    docker_dir = cwd / "docker"
    docker_dir.mkdir(exist_ok=True)
    copia = docker_dir / "setup-data-multispectral.sh"
    shutil.copy(SETUP_DATA_MS, copia)
    copia.chmod(0o755)
    return subprocess.run([str(copia), str(src)], cwd=str(cwd),
                          capture_output=True, text=True, timeout=30)


class TestOrganizacionBandaD:
    def test_organiza_archivos_d_jpg(self, tmp_path):
        fuente = tmp_path / "vuelo_m3m"
        fuente.mkdir()
        for banda in ("G", "R", "RE", "NIR"):
            (fuente / f"DJI_0001_MS_{banda}.TIF").write_bytes(b"x")
        (fuente / "DJI_0001_D.JPG").write_bytes(b"x")
        (fuente / "DJI_0002_D.JPG").write_bytes(b"x")

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr_setup(fuente, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (cwd / "data" / "dband_mosaico" / "DJI_0001_D.JPG").exists()
        assert (cwd / "data" / "dband_mosaico" / "DJI_0002_D.JPG").exists()
        assert "Banda D (RGB, opcional): 2 imágenes" in r.stdout, r.stdout

    def test_sin_banda_d_no_es_un_error(self, tmp_path):
        """A diferencia de las 4 bandas espectrales (obligatorias), la banda
        D es opcional — su ausencia no debe hacer fallar el script."""
        fuente = tmp_path / "vuelo_m3m"
        fuente.mkdir()
        for banda in ("G", "R", "RE", "NIR"):
            (fuente / f"DJI_0001_MS_{banda}.TIF").write_bytes(b"x")

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr_setup(fuente, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "sin banda D en esta carpeta" in r.stdout, r.stdout

    def test_banda_d_sigue_symlinks(self, tmp_path):
        real = tmp_path / "fotos_reales"
        real.mkdir()
        for banda in ("G", "R", "RE", "NIR"):
            (real / f"DJI_0001_MS_{banda}.TIF").write_bytes(b"x")
        (real / "DJI_0001_D.JPG").write_bytes(b"x")

        symlink_src = tmp_path / "raw_multispectral"
        os.symlink(real, symlink_src)

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr_setup(symlink_src, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (cwd / "data" / "dband_mosaico" / "DJI_0001_D.JPG").exists()


class TestPrepareDbandOdm:
    """scripts/prepare_dband_odm.py — mismo patrón que
    prepare_multispectral_odm.py (copia + geo.txt vía exiftool), pero una
    imagen por captura, sin la validación de "4 bandas tienen que emparejar"."""

    @pytest.fixture
    def repo_falso(self, tmp_path, monkeypatch):
        """prepare_dband_odm.py usa rutas relativas al cwd (data/dband_mosaico,
        processing/dband_odm) — se corre con cwd=tmp_path."""
        (tmp_path / "data" / "dband_mosaico").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_sin_carpeta_fuente_falla_con_mensaje_claro(self, tmp_path, monkeypatch):
        vacio = tmp_path / "vacio"
        vacio.mkdir()
        monkeypatch.chdir(vacio)
        r = subprocess.run(
            ["python3", os.path.join(REPO, "scripts", "prepare_dband_odm.py")],
            capture_output=True, text=True, timeout=15)
        assert r.returncode != 0
        assert "no encontrado" in r.stdout.lower() or "no encontrado" in r.stderr.lower()

    def test_sin_imagenes_d_falla_con_mensaje_claro(self, repo_falso):
        r = subprocess.run(
            ["python3", os.path.join(REPO, "scripts", "prepare_dband_odm.py")],
            capture_output=True, text=True, timeout=15, cwd=str(repo_falso))
        assert r.returncode != 0
        assert "no se encontraron" in (r.stdout + r.stderr).lower()


class TestConteoDeEtapasConBandaD:
    """docker/entrypoint.sh: DO_DBAND tiene que sumar exactamente una etapa
    al total cuando está activo (mismo patrón que test_stage_counter.py) —
    y el presupuesto de concurrencia compartido de la preparación tiene que
    contarlo como un stream más (PREP_SPLIT, calculado ANTES del despacho
    por sensor — ver docker/entrypoint.sh, justo antes de `_odm_mp() {`)."""

    def _bloque_prep_split(self):
        """El cálculo real de PREP_SPLIT: desde PREP_BUDGET hasta el
        redondeo final, tal como está en entrypoint.sh."""
        src = open(ENTRYPOINT, encoding="utf-8").read()
        ini = src.index("PREP_BUDGET=$(safe_concurrency 5)")
        fin = src.index('[[ "$PREP_SPLIT" -lt 1 ]] && PREP_SPLIT=1')
        fin = src.index("\n", fin) + 1
        return src[ini:fin]

    def _correr(self, run_ms, do_dband, presupuesto, make_extra):
        guion = f"""
set -uo pipefail
python3() {{
  if [[ "$1" == "scripts/hardware.py" ]]; then echo {presupuesto}; return 0; fi
  command python3 "$@"
}}
RUN_MULTISPECTRAL={run_ms}
DO_DBAND={do_dband}
safe_concurrency() {{ python3 scripts/hardware.py concurrency "$1"; }}
""" + self._bloque_prep_split() + f"""
make() {{
  case "$1" in
    {make_extra}
  esac
}}
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && MAX_CONCURRENCY=$PREP_SPLIT make prepare-multispectral
[[ "$DO_DBAND" -eq 1 ]] && MAX_CONCURRENCY=$PREP_SPLIT make prepare-dband
echo "th:MAX_CONCURRENCY=${{MAX_CONCURRENCY:-sin-fijar}}"
"""
        return subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=15)

    def test_presupuesto_se_reparte_entre_ms_y_dband_no_con_termico(self):
        """El térmico (sdk-convert) NO entra en este reparto — mide ~9MB RSS
        por proceso (dji_irp, medido en vivo), nada que ver con el perfil de
        5 MP que calibra este presupuesto. Solo multiespectral y banda D
        (mismo perfil de memoria real) lo comparten. La última línea del
        guión (echo th:...) corre FUERA de la rama de MAX_CONCURRENCY
        adrede, para confirmar que sin fijarlo explícitamente la variable no
        queda contaminada por el reparto de los otros dos."""
        r = self._correr(run_ms=1, do_dband=1, presupuesto=9, make_extra=(
            'prepare-multispectral) echo "ms:MAX_CONCURRENCY=${MAX_CONCURRENCY:-sin-fijar}" ;;\n'
            '    prepare-dband) echo "db:MAX_CONCURRENCY=${MAX_CONCURRENCY:-sin-fijar}" ;;'))
        assert r.returncode == 0, r.stdout + r.stderr
        # 9 repartido entre 2 streams (multiespectral, banda D) = 5 (redondeo hacia arriba) cada uno
        assert "ms:MAX_CONCURRENCY=5" in r.stdout, r.stdout
        assert "db:MAX_CONCURRENCY=5" in r.stdout, r.stdout
        assert "th:MAX_CONCURRENCY=sin-fijar" in r.stdout, r.stdout

    def test_sin_dband_multiespectral_se_lleva_todo_el_presupuesto(self):
        """Confirma que el conteo de streams es DINÁMICO: sin banda D
        activa, multiespectral (el único que queda) se lleva TODO
        PREP_BUDGET sin dividir."""
        r = self._correr(run_ms=1, do_dband=0, presupuesto=8, make_extra=(
            'prepare-multispectral) echo "ms:MAX_CONCURRENCY=${MAX_CONCURRENCY:-sin-fijar}" ;;\n'
            '    prepare-dband) echo "NO DEBERÍA CORRER" ;;'))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ms:MAX_CONCURRENCY=8" in r.stdout, r.stdout
        assert "NO DEBERÍA CORRER" not in r.stdout, r.stdout

    def test_stage_flags_incluye_dband(self):
        """Banda D ya no tiene su propio elemento en STAGE_FLAGS — el
        recorte de bordes de los cuatro sensores se cuenta como UNA sola
        etapa (DO_TRIM, ver docker/entrypoint.sh), pero DO_DBAND tiene que
        seguir siendo una de las condiciones que la activan: sin RGB,
        térmico ni multiespectral, una misión solo-banda-D igual necesita
        recortar su ortofoto."""
        src = open(ENTRYPOINT, encoding="utf-8").read()
        m = re.search(r'DO_TRIM=0; \[\[ ([^\]]*) \]\]', src)
        assert m, "no se encontró la declaración de DO_TRIM"
        assert "DO_DBAND" in m.group(1), \
            f"DO_TRIM no incluye DO_DBAND: {m.group(1)}"


class TestBandaDUsaFastOrthophoto:
    """--fast-orthophoto salta DensifyPointCloud (MVS) — sin esto, banda D
    corría la etapa más cara de toda la misión igual (confirmado en vivo:
    ~5h50m para 39 capturas) aunque nunca pide --dsm, porque ODM arma la
    ortofoto sobre la nube DENSA salvo que se le pida lo contrario."""

    def _bloque_dband(self):
        """Los argumentos de ODM para la banda D: la rama `dband)` de
        _odm_args() en entrypoint.sh."""
        src = open(ENTRYPOINT, encoding="utf-8").read()
        args = src.index("_odm_args() {")
        ini = src.index("    dband)", args)
        fin = src.index(";;", ini) + 2
        return src[ini:fin]

    def test_pide_fast_orthophoto(self):
        assert "--fast-orthophoto" in self._bloque_dband()

    def test_sigue_sin_pedir_dsm(self):
        # Solo los `echo` con los argumentos reales, no los comentarios de
        # arriba (que sí mencionan --dsm para explicar por qué NO se pide).
        argumentos = "\n".join(l for l in self._bloque_dband().splitlines()
                               if not l.lstrip().startswith("#"))
        assert "--dsm" not in argumentos, argumentos
