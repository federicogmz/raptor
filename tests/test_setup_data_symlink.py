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
    scripts_dir = cwd / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    shutil.copy(os.path.join(REPO, "scripts", "source_filter.py"), scripts_dir / "source_filter.py")
    return subprocess.run([str(copia), str(src)], cwd=str(cwd), capture_output=True, text=True, timeout=30)


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
        assert (cwd / "data" / "rgb_mosaico" / "DJI_0001_V.JPG").exists(), (
            "el símlink de origen tiene que atravesarse, no tratarse como un archivo suelto"
        )
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


class TestFiltradoYMrk:
    def test_poda_subcarpetas_no_cartograficas_y_separa_mrk_sin_inflar_conteo(self, tmp_path):
        src = tmp_path / "mision"
        carto = src / "20260803_raw" / "M3T"
        carto.mkdir(parents=True)
        (carto / "DJI_20260803120000_0001_W.JPG").write_bytes(b"rgb1")
        (carto / "DJI_20260803120005_0002_W.JPG").write_bytes(b"rgb2")
        (carto / "DJI_20260803120000_0001_T.JPG").write_bytes(b"th1")
        (carto / "100_0001_Timestamp.MRK").write_text("1\t0\n", encoding="utf-8")

        vuelo2 = src / "20260803_raw" / "M3T_vuelo2"
        vuelo2.mkdir()
        (vuelo2 / "100_0001_Timestamp.MRK").write_text("2\t0\n", encoding="utf-8")

        for excluido, nombre_w in (
            (src / "20260803_audiovisual" / "panoramicas", "pano_01_W.JPG"),
            (src / "rgb_panoramicas", "pano_02_W.JPG"),
            (src / "rgb_360", "esfera_W.JPG"),
        ):
            excluido.mkdir(parents=True)
            (excluido / nombre_w).write_bytes(b"decoy")
        (src / "20260803_audiovisual" / "panoramicas" / "pano_01_T.JPG").write_bytes(b"decoy_t")
        (src / "20260803_audiovisual" / "panoramicas" / "pano_Timestamp.MRK").write_text("1\t0\n", encoding="utf-8")

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr(SETUP_DATA, src, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert sorted(os.listdir(cwd / "data" / "rgb_mosaico")) == [
            "DJI_20260803120000_0001_W.JPG",
            "DJI_20260803120005_0002_W.JPG",
        ]
        assert sorted(os.listdir(cwd / "data" / "termica_mosaico")) == ["DJI_20260803120000_0001_T.JPG"]
        assert sorted(os.listdir(cwd / "data" / "mrk" / "rgb")) == [
            "100_0001_Timestamp.MRK",
            "1_100_0001_Timestamp.MRK",
        ]
        assert "2 copiadas a data/rgb_mosaico/" in r.stdout, r.stdout
        assert "RGB:     2 imágenes" in r.stdout, r.stdout
        assert "3 copiadas" not in r.stdout

    def test_import_local_symlink_raiz_panoramicas_se_preserva_pero_poda_audiovisual_interno(self, tmp_path):
        user_src = tmp_path / "carpeta_usuario_panoramicas"
        (user_src / "audiovisual").mkdir(parents=True)
        (user_src / "DJI_20260803120000_0001_W.JPG").write_bytes(b"carto")
        (user_src / "audiovisual" / "clip_W.JPG").write_bytes(b"clip")

        raw_rgb = tmp_path / "raw_rgb"
        raw_rgb.mkdir()
        (raw_rgb / "panoramicas_vuelo").symlink_to(user_src)

        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / "data").mkdir()

        r = _correr(SETUP_DATA, raw_rgb, cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert sorted(os.listdir(cwd / "data" / "rgb_mosaico")) == ["DJI_20260803120000_0001_W.JPG"]

    def test_paridad_con_webapp_listdir_names(self, tmp_path, monkeypatch):
        import importlib
        from core import scan
        from source_filter import iter_source_files

        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        importlib.reload(scan)
        from webapp import main as webapp_main

        importlib.reload(webapp_main)
        _listdir_names = webapp_main._listdir_names

        user_src = tmp_path / "origen_importado"
        (user_src / "audiovisual").mkdir(parents=True)
        (user_src / "DJI_20260803120000_0001_W.JPG").write_bytes(b"ok")
        (user_src / "audiovisual" / "extra_W.JPG").write_bytes(b"no")

        raw_rgb = tmp_path / "raw_rgb"
        (raw_rgb / "20260803_audiovisual").mkdir(parents=True)
        (raw_rgb / "20260803_audiovisual" / "pano_W.JPG").write_bytes(b"no2")
        (raw_rgb / "panoramicas_vuelo").symlink_to(user_src)

        esperados = {os.path.basename(p) for p in iter_source_files(raw_rgb)}
        obtenidos = {os.path.basename(p) for p in _listdir_names(raw_rgb)}
        assert esperados == {"DJI_20260803120000_0001_W.JPG"}
        assert obtenidos == esperados


def test_quality_estimate_sin_multiespectral_no_falla_con_set_u(tmp_path):
    """quality-estimate no debe fallar con set -u cuando falta MS_SOURCE_DIR o SOURCE_DIR."""
    import json

    entrypoint = os.path.join(REPO, "docker", "entrypoint.sh")

    app_dir = tmp_path / "app"
    scripts_dir = app_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    # Stub hermético de scripts/hardware.py: responde a `preset ... --shell` (10 líneas)
    # y registra el argumento exacto de `por_sensor` recibido desde bash.
    (scripts_dir / "hardware.py").write_text(
        "import json, os, sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'preset':\n"
        "    print('\\n'.join(['cartografico', 'Cartográfico', 'medium', 'high', '4', '8000', '8', 'incremental', '1', '0']))\n"
        "def estimate_message(preset, n_imgs, terreno='plano', por_sensor=None):\n"
        "    expected = json.loads(os.environ['EXPECTED_POR_SENSOR'])\n"
        "    assert por_sensor == expected, f'{por_sensor} != {expected}'\n"
        "    return {\n"
        "        'hardware': {'cores': 4, 'mem_available_mb': 8192, 'gpu': False, 'gpu_name': None},\n"
        "        'modelo_texto': 'modelo ok',\n"
        "        'resolucion_texto': 'resolucion ok',\n"
        "        'tiempo_texto': 'tiempo ok',\n"
        "    }\n",
        encoding="utf-8",
    )

    rgb_src = tmp_path / "input_rgb"
    rgb_src.mkdir()
    for i in (1, 2):
        (rgb_src / f"DJI_2026081612000{i}_000{i}_V.JPG").write_bytes(b"rgb")
        (rgb_src / f"DJI_2026081612000{i}_000{i}_T.JPG").write_bytes(b"th")

    ms_src = tmp_path / "input_ms"
    ms_src.mkdir()
    (ms_src / "DJI_20260816120001_0001_MS_NIR.TIF").write_bytes(b"ms")

    casos = [
        # Sin multiespectral (N_MS=0), con SUB_SAMPLE apagado y encendido
        (
            {
                "SOURCE_DIR": str(rgb_src),
                "MS_SOURCE_DIR": str(tmp_path / "no_ms"),
                "MODE": "rgb+thermal",
                "PRESET": "cartografico",
            },
            {"rgb": 2, "thermal": 2, "multispectral": 0},
        ),
        (
            {
                "SOURCE_DIR": str(rgb_src),
                "MS_SOURCE_DIR": str(tmp_path / "no_ms"),
                "MODE": "rgb+thermal",
                "PRESET": "cartografico",
                "SUB_SAMPLE": "2",
            },
            {"rgb": 1, "thermal": 1, "multispectral": 0},
        ),
        # Sin vuelo RGB/térmico (N_RGB=0, N_TH=0) con MODE=none y MODE=rgb+thermal
        (
            {
                "SOURCE_DIR": str(tmp_path / "no_rgb"),
                "MS_SOURCE_DIR": str(ms_src),
                "MODE": "none",
                "PRESET": "cartografico",
            },
            {"rgb": 0, "thermal": 0, "multispectral": 4},
        ),
        (
            {
                "SOURCE_DIR": str(tmp_path / "no_rgb"),
                "MS_SOURCE_DIR": str(ms_src),
                "MODE": "rgb+thermal",
                "PRESET": "cartografico",
            },
            {"rgb": 0, "thermal": 0, "multispectral": 4},
        ),
    ]

    vars_pipeline = {"SUB_SAMPLE", "TERRENO", "QUALITY", "PRESET", "DBAND", "SKIP_ODM", "MODE"}
    base_env = {k: v for k, v in os.environ.items() if k not in vars_pipeline}
    for extra_env, expected in casos:
        env = {
            **base_env,
            "RAPTOR_APP_DIR": str(app_dir),
            "EXPECTED_POR_SENSOR": json.dumps(expected),
            **extra_env,
        }
        res = subprocess.run(
            ["bash", entrypoint, "quality-estimate"],
            cwd=app_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr
        assert "📐 resolucion ok" in res.stdout, res.stdout
        assert "⏱️  tiempo ok" in res.stdout, res.stdout
