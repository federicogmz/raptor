"""docker/setup-data.sh (y setup-data-multispectral.sh) contra una carpeta
fuente que es un SYMLINK, no un directorio real.

Reportado en vivo (no hipotético): import_local() en webapp/main.py deja
raw/<kind> como un symlink directo a la carpeta del usuario, para no
duplicar sus fotos (ver el docstring de ese endpoint). La primera misión
real que probó ese camino falló de una con "No se encontraron imágenes
RGB" — 0 encontradas, aunque las fotos estaban ahí y eran perfectamente
legibles con `ls`.

Causa real: `find "$SRC" ...` (sin -L) NO sigue un symlink que sea el
argumento de partida cuando no termina en "/" — lo trata como un archivo
suelto de tipo symlink en vez de como el directorio al que apunta, así que
nunca entra a buscar adentro. `ls`/Python's pathlib no tienen este
problema (por eso la webapp SÍ contaba bien las fotos al validar el
formulario) — es específico de cómo `find` interpreta sus argumentos de
línea de comandos.
"""
import os
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP_DATA = os.path.join(REPO, "docker", "setup-data.sh")
SETUP_DATA_MS = os.path.join(REPO, "docker", "setup-data-multispectral.sh")


def _correr(script_real, src, cwd):
    """setup-data.sh calcula DATA_DIR relativo a la ubicación DEL PROPIO
    script ($(dirname .../docker)/data), no al cwd — así que para probarlo
    aislado (sin escribir en el data/ real del repo) hay que copiar el
    script a un "repo" de mentira bajo tmp_path, con la misma forma
    docker/<script> un nivel arriba de data/."""
    docker_dir = cwd / "docker"
    docker_dir.mkdir(exist_ok=True)
    copia = docker_dir / os.path.basename(script_real)
    shutil.copy(script_real, copia)
    copia.chmod(0o755)
    return subprocess.run([str(copia), str(src)], cwd=str(cwd),
                          capture_output=True, text=True, timeout=30)


class TestSetupDataSigueSymlinks:
    def test_encuentra_fotos_a_traves_de_un_symlink(self, tmp_path):
        # Mismo layout que el caso real: el symlink apunta a una carpeta
        # con subcarpetas propias (rgb_mosaico/, termica/) — no un
        # directorio plano.
        real = tmp_path / "fotos_del_dron" / "rgb_mosaico"
        real.mkdir(parents=True)
        (real / "DJI_0001_V.JPG").write_bytes(b"x")
        (real / "DJI_0002_T.JPG").write_bytes(b"x")

        symlink_src = tmp_path / "raw_rgb_thermal"
        os.symlink(tmp_path / "fotos_del_dron", symlink_src)

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr(SETUP_DATA, symlink_src, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "No se encontraron imágenes RGB" not in r.stdout, r.stdout
        assert (cwd / "data" / "rgb_mosaico" / "DJI_0001_V.JPG").exists(), \
            "el símlink de origen tiene que atravesarse, no tratarse como un archivo suelto"
        assert (cwd / "data" / "termica_mosaico" / "DJI_0002_T.JPG").exists()

    def test_multiespectral_tambien_sigue_symlinks(self, tmp_path):
        real = tmp_path / "fotos_ms"
        real.mkdir()
        for banda in ("G", "R", "RE", "NIR"):
            (real / f"DJI_0001_MS_{banda}.TIF").write_bytes(b"x")

        symlink_src = tmp_path / "raw_multispectral"
        os.symlink(real, symlink_src)

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr(SETUP_DATA_MS, symlink_src, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "No se encontraron bandas" not in r.stdout, r.stdout
        assert (cwd / "data" / "multiespectral_mosaico" / "DJI_0001_MS_G.TIF").exists()

    def test_sigue_andando_con_un_directorio_real_no_symlink(self, tmp_path):
        """No romper el caso de toda la vida (SRC real, sin symlink de por
        medio) — sigue siendo lo que usa ./raptor run --input DIR."""
        real = tmp_path / "fotos_del_dron"
        real.mkdir()
        (real / "DJI_0001_V.JPG").write_bytes(b"x")

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr(SETUP_DATA, real, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (cwd / "data" / "rgb_mosaico" / "DJI_0001_V.JPG").exists()


class TestSetupDataReanudaSinCrashear:
    """Bug real, reportado en vivo: al reanudar una misión cuyas fotos YA
    estaban copiadas a data/ (p.ej. reiniciar con otra calidad), el script
    moría justo después de "Encontradas: N", sin ningún mensaje de error.

    Causa: `[[ ! -f "$dest" ]] && echo "$f"` dentro de un `while read` — con
    set -e, el exit status de un `while` es el de su ÚLTIMO comando
    ejecutado. Si el ÚLTIMO archivo que entrega `find` ya existe en destino,
    ese `[[ ]]` da falso, y ESE exit status no-cero tumba el
    `TO_COPY=$(...)` de más arriba. Como `find` no garantiza orden, esto era
    no determinístico en general pero SEGURO cuando (como acá) TODOS los
    archivos ya existen: no importa cuál sea el último, siempre da falso."""

    def test_reanudar_con_todo_ya_copiado_no_crashea(self, tmp_path):
        real = tmp_path / "fotos_del_dron"
        real.mkdir()
        for i in range(5):
            (real / f"DJI_{i:04d}_V.JPG").write_bytes(b"x")
            (real / f"DJI_{i:04d}_T.JPG").write_bytes(b"x")

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r1 = _correr(SETUP_DATA, real, cwd)
        assert r1.returncode == 0, r1.stdout + r1.stderr

        # Segunda pasada: TODO ya está en data/ — antes esto crasheaba.
        r2 = _correr(SETUP_DATA, real, cwd)
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert "✅ 0 copiadas a data/rgb_mosaico/" in r2.stdout, r2.stdout
        assert "✅ 0 copiadas a data/termica_mosaico/" in r2.stdout, r2.stdout

    def test_reanudar_multiespectral_con_todo_ya_copiado_no_crashea(self, tmp_path):
        real = tmp_path / "fotos_ms"
        real.mkdir()
        for banda in ("G", "R", "RE", "NIR"):
            (real / f"DJI_0001_MS_{banda}.TIF").write_bytes(b"x")

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r1 = _correr(SETUP_DATA_MS, real, cwd)
        assert r1.returncode == 0, r1.stdout + r1.stderr

        r2 = _correr(SETUP_DATA_MS, real, cwd)
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert "✅ 0 copiadas a data/multiespectral_mosaico/" in r2.stdout, r2.stdout
