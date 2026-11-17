"""Tests de ciclo de vida en disco para ``run_odm``, ``_firma_proyecto_odm`` y ``_purgar_intermedios_odm``.

Extrae las funciones reales de ``docker/entrypoint.sh`` (igual que
``test_odm_paralelo.py``) y las ejecuta en bash estricto (``set -euo pipefail``)
con ``_odm_invocar`` stubbeado que registra las llamadas y simula artefactos ODM.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "docker" / "entrypoint.sh"


def _extraer_funcion(texto: str, nombre: str) -> str:
    """Extrae una función bash de nivel superior contando llaves ``{``/``}``."""
    lineas = texto.splitlines()
    inicio = None
    cabecera = f"{nombre}() {{"
    for i, ln in enumerate(lineas):
        if ln.startswith(cabecera):
            inicio = i
            break
    assert inicio is not None, f"No se encontró {nombre}() en docker/entrypoint.sh"

    profundidad = 0
    for j in range(inicio, len(lineas)):
        for ch in lineas[j]:
            if ch == "{":
                profundidad += 1
            elif ch == "}":
                profundidad -= 1
                if profundidad == 0:
                    return "\n".join(lineas[inicio : j + 1])
    raise AssertionError(f"Función {nombre}() sin llave de cierre en docker/entrypoint.sh")


def _cargar_funciones_disk() -> str:
    txt = ENTRYPOINT.read_text(encoding="utf-8")
    partes = [
        _extraer_funcion(txt, "_firma_proyecto_odm"),
        _extraer_funcion(txt, "run_odm"),
        _extraer_funcion(txt, "_purgar_intermedios_odm"),
    ]
    return "\n\n".join(partes)


def _preparar_proyecto(tmp_path: Path, name: str = "rgb_odm", n_imgs: int = 2) -> Path:
    proj = tmp_path / "processing" / name
    (proj / "images").mkdir(parents=True, exist_ok=True)
    for idx in range(1, n_imgs + 1):
        (proj / "images" / f"DJI_{idx:04d}.JPG").write_bytes(b"img" * idx)
    (proj / "geo.txt").write_text("EPSG:4326\nDJI_0001.JPG -75.5 6.2 1800\n", encoding="utf-8")
    (tmp_path / "outputs" / "logs").mkdir(parents=True, exist_ok=True)
    return proj


def _correr_bash(tmp_path: Path, body: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    funciones = _cargar_funciones_disk()
    script = tmp_path / "run_case.sh"
    script.write_text(
        f'#!/usr/bin/env bash\nset -euo pipefail\ncd "{tmp_path}"\n{funciones}\n{body}\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=30)


STUB_INVOCAR_COMPLETO = r"""
_odm_invocar() {
  local name="$1" conc="$2" logfile="$3" label="$4" resume="$5"; shift 5
  echo "$name|$conc|$label|$resume|$*" >> calls.log
  local p="processing/$name"
  mkdir -p "$p/opensfm/undistorted/images" "$p/opensfm/features" "$p/opensfm/matches" "$p/opensfm/reports"
  mkdir -p "$p/odm_filterpoints" "$p/odm_meshing" "$p/odm_texturing" "$p/odm_texturing_25d"
  mkdir -p "$p/odm_orthophoto" "$p/odm_georeferencing" "$p/odm_dem"
  echo "run:${label}:${resume}:$$" > "$p/opensfm/reconstruction.json"
  # Simula ODM stages/dataset.py: solo escribe images.json si NO existe ya en disco
  if [[ ! -f "$p/images.json" ]]; then
    if [[ -f "$p/geo.txt" ]]; then
      cp "$p/geo.txt" "$p/images.json"
    else
      echo '{}' > "$p/images.json"
    fi
  fi
  echo "header_tracks_line_ok" > "$p/opensfm/tracks.csv"
  echo "undist_tracks_ok" > "$p/opensfm/undistorted/tracks.csv"
  echo "done" > "$p/opensfm/undistorted/nominal_done.txt"
  echo "u" > "$p/opensfm/undistorted/images/DJI_0001.JPG"
  echo "tif" > "$p/odm_orthophoto/odm_orthophoto.tif"
  echo "laz" > "$p/odm_georeferencing/odm_georeferenced_model.laz"
  echo "dsm" > "$p/odm_dem/dsm.tif"
  if [[ " $* " == *" --optimize-disk-space "* ]]; then
    # Simula run_opensfm.py (--fast-orthophoto o rerun) / mvstex eliminando tracks/undistorted
    rm -f "$p/opensfm/tracks.csv"
  fi
  return 0
}
"""


class TestRunOdmOptimizeDisk:
    def test_dense_2pass_crea_backup_y_solo_pasa_optimize_en_fase2(self, tmp_path):
        proj = _preparar_proyecto(tmp_path)
        r = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm --orthophoto-resolution 5\n",
        )
        assert r.returncode == 0, r.stdout + r.stderr
        calls = (tmp_path / "calls.log").read_text(encoding="utf-8").strip().splitlines()
        assert len(calls) == 2
        assert "--end-with opensfm" in calls[0]
        assert "--optimize-disk-space" not in calls[0], "Fase 1 NO debe llevar --optimize-disk-space"
        assert "--rerun-from openmvs" in calls[1]
        assert "--optimize-disk-space" in calls[1], "Fase 2 sí debe llevar --optimize-disk-space"
        assert (proj / "opensfm" / ".tracks.csv.bak").is_file()
        assert (proj / ".raptor_odm_ok").is_file()

    def test_fast_ortho_limpia_opensfm_si_tracks_falta_y_pasa_optimize(self, tmp_path):
        proj = _preparar_proyecto(tmp_path, name="dband_odm")
        # Simula estado roto previo: reconstruction.json presente pero sin tracks.csv
        (proj / "opensfm" / "features").mkdir(parents=True)
        (proj / "opensfm" / "reconstruction.json").write_text("{}", encoding="utf-8")
        (proj / "opensfm" / "stale_marker.txt").write_text("stale", encoding="utf-8")

        stub = r"""
_odm_invocar() {
  local name="$1" conc="$2" logfile="$3" label="$4" resume="$5"; shift 5
  echo "$name|$conc|$label|$resume|$*" >> calls.log
  [[ ! -e "processing/$name/opensfm/stale_marker.txt" ]] || { echo "STALE_NOT_CLEANED"; return 9; }
  mkdir -p "processing/$name/opensfm" "processing/$name/odm_orthophoto" "processing/$name/odm_georeferencing"
  echo '{}' > "processing/$name/opensfm/reconstruction.json"
  echo 'tif' > "processing/$name/odm_orthophoto/odm_orthophoto.tif"
  echo 'laz' > "processing/$name/odm_georeferencing/odm_georeferenced_model.laz"
  return 0
}
"""
        r = _correr_bash(
            tmp_path,
            stub + "\nrun_odm dband dband_odm 8 4 --fast-orthophoto\n",
        )
        assert r.returncode == 0, r.stdout + r.stderr
        calls = (tmp_path / "calls.log").read_text(encoding="utf-8").strip().splitlines()
        assert len(calls) == 1
        assert "--fast-orthophoto" in calls[0]
        assert "--optimize-disk-space" in calls[0]
        assert (proj / ".raptor_odm_ok").is_file()

    def test_raptor_optimize_disk_0_preserva_comportamiento_legacy_y_no_purga(self, tmp_path):
        proj = _preparar_proyecto(tmp_path)
        r = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO
            + "\nrun_odm rgb rgb_odm 8 4 --dsm\n"
            + "_purgar_intermedios_odm processing/rgb_odm\n",
            env_extra={"RAPTOR_OPTIMIZE_DISK": "0"},
        )
        assert r.returncode == 0, r.stdout + r.stderr
        calls = (tmp_path / "calls.log").read_text(encoding="utf-8").strip().splitlines()
        assert len(calls) == 2
        assert all("--optimize-disk-space" not in c for c in calls)
        assert not (proj / "opensfm" / ".tracks.csv.bak").exists()
        assert not (proj / ".raptor_odm_ok").exists()
        assert not (proj / ".raptor_purgado").exists()
        assert (proj / "opensfm" / "undistorted" / "images" / "DJI_0001.JPG").is_file()
        assert (proj / "odm_filterpoints").is_dir()

    def test_reintento_tras_fallo_fase2_restaura_tracks_bak_y_quita_nominal_done(self, tmp_path):
        proj = _preparar_proyecto(tmp_path)
        # Intento 1: Fase 1 ok, Fase 2 falla tras borrar tracks.csv y undistorted/images
        stub_intentos = r"""
_odm_invocar() {
  local name="$1" conc="$2" logfile="$3" label="$4" resume="$5"; shift 5
  local p="processing/$name"
  local attempt_num=1
  [[ -f phase2_started.flag ]] && attempt_num=2
  echo "attempt=${attempt_num}|resume=${resume}|$*" >> calls.log
  if [[ "$resume" == "" ]]; then
    if [[ "$attempt_num" -eq 2 ]]; then
      [[ -s "$p/opensfm/tracks.csv" ]] || { echo "NO_TRACKS_RESTORED"; return 12; }
      [[ ! -e "$p/opensfm/undistorted/nominal_done.txt" ]] || { echo "NOMINAL_DONE_STILL_THERE"; return 13; }
      [[ -d "$p/opensfm/features" ]] || { echo "FEATURES_WIPED_UNEXPECTEDLY"; return 14; }
    fi
    mkdir -p "$p/opensfm/undistorted/images" "$p/opensfm/features" "$p/opensfm/matches"
    echo '{}' > "$p/opensfm/reconstruction.json"
    echo "real_tracks_12345" > "$p/opensfm/tracks.csv"
    echo "u_tracks" > "$p/opensfm/undistorted/tracks.csv"
    echo "done" > "$p/opensfm/undistorted/nominal_done.txt"
    echo "u" > "$p/opensfm/undistorted/images/DJI_0001.JPG"
    return 0
  else
    if [[ ! -f phase2_started.flag ]]; then
      : > phase2_started.flag
      rm -f "$p/opensfm/tracks.csv"
      rm -rf "$p/opensfm/undistorted/images"
      return 1
    fi
    mkdir -p "$p/odm_orthophoto" "$p/odm_georeferencing" "$p/odm_dem"
    echo 'tif' > "$p/odm_orthophoto/odm_orthophoto.tif"
    echo 'laz' > "$p/odm_georeferencing/odm_georeferenced_model.laz"
    echo 'dsm' > "$p/odm_dem/dsm.tif"
    return 0
  fi
}
"""
        r1 = _correr_bash(
            tmp_path,
            stub_intentos + "\nset +e\nrun_odm rgb rgb_odm 8 4 --dsm\n",
        )
        # run_odm hace exit $status si falla; verificamos en dos invocaciones como hace el bucle de retry
        assert r1.returncode == 1
        assert not (proj / ".raptor_odm_ok").exists()
        assert (proj / "opensfm" / ".tracks.csv.bak").is_file()

        r2 = _correr_bash(
            tmp_path,
            stub_intentos + "\nrun_odm rgb rgb_odm 8 4 --dsm\n",
        )
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert (proj / ".raptor_odm_ok").is_file()
        assert "attempt=2|resume=openmvs" in (tmp_path / "calls.log").read_text(encoding="utf-8")

        # Si NO existe .tracks.csv.bak (o es <= 10 B), limpia opensfm/ en lugar de chocar
        proj_bad = _preparar_proyecto(tmp_path, name="thermal_odm")
        (proj_bad / "opensfm").mkdir(parents=True)
        (proj_bad / "opensfm" / "reconstruction.json").write_text("{}", encoding="utf-8")
        (proj_bad / "opensfm" / "tracks.csv").write_text("#\n", encoding="utf-8")
        (proj_bad / "opensfm" / "canary.txt").write_text("old", encoding="utf-8")
        r3 = _correr_bash(
            tmp_path,
            r"""
_odm_invocar() {
  local name="$1" conc="$2" logfile="$3" label="$4" resume="$5"; shift 5
  if [[ "$resume" == "" ]]; then
    [[ ! -e "processing/$name/opensfm/canary.txt" ]] || return 21
  fi
  mkdir -p "processing/$name/opensfm" "processing/$name/odm_orthophoto" "processing/$name/odm_georeferencing"
  echo '{}' > "processing/$name/opensfm/reconstruction.json"
  echo "header_tracks_line_ok" > "processing/$name/opensfm/tracks.csv"
  echo 'tif' > "processing/$name/odm_orthophoto/odm_orthophoto.tif"
  echo 'laz' > "processing/$name/odm_georeferencing/odm_georeferenced_model.laz"
  return 0
}
run_odm thermal thermal_odm 8 4
""",
        )
        assert r3.returncode == 0, r3.stdout + r3.stderr

    def test_firma_reusa_e_invalida_por_geo_fotos_flags_y_force(self, tmp_path):
        proj = _preparar_proyecto(tmp_path)
        # 1. Corrida inicial + purga
        r1 = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n_purgar_intermedios_odm processing/rgb_odm\n",
        )
        assert r1.returncode == 0, r1.stdout + r1.stderr
        assert (proj / ".raptor_odm_ok").is_file()
        assert (proj / ".raptor_purgado").is_file()
        assert len((tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()) == 2

        # 2. Reuso tras purga (0 llamadas nuevas a _odm_invocar, toca .reusado_rgb)
        (tmp_path / "calls.log").unlink()
        r2 = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n",
        )
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert not (tmp_path / "calls.log").exists()
        assert (tmp_path / "outputs" / "logs" / ".reusado_rgb").is_file()

        # 3. Invalidación por cambio en geo.txt (además limpia .raptor_purgado e images.json)
        (proj / "geo.txt").write_text("EPSG:4326\nDJI_0001.JPG -75.5 6.2 1805\n", encoding="utf-8")
        r3 = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n",
        )
        assert r3.returncode == 0, r3.stdout + r3.stderr
        assert len((tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()) == 2
        assert not (tmp_path / "outputs" / "logs" / ".reusado_rgb").exists()
        assert not (proj / ".raptor_purgado").exists()
        assert "1805" in (proj / "images.json").read_text(encoding="utf-8"), (
            "images.json viejo debe borrarse al cambiar la firma para cargar el nuevo geo.txt"
        )

        # 4. Invalidación por cambio en images/
        (tmp_path / "calls.log").unlink()
        (proj / "images" / "DJI_0003.JPG").write_bytes(b"new_photo")
        r4 = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n",
        )
        assert r4.returncode == 0, r4.stdout + r4.stderr
        assert len((tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()) == 2

        # 5. RAPTOR_FORCE_ODM=1 fuerza reconstrucción incluso en proyecto NO purgado y limpia images.json/reconstrucción previa
        (tmp_path / "calls.log").unlink()
        (proj / "images.json").write_text("STALE_IMAGES_JSON", encoding="utf-8")
        (proj / "opensfm" / "reconstruction.json").write_text("STALE_RECON", encoding="utf-8")
        r5 = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n",
            env_extra={"RAPTOR_FORCE_ODM": "1"},
        )
        assert r5.returncode == 0, r5.stdout + r5.stderr
        assert len((tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()) == 2
        assert "STALE_IMAGES_JSON" not in (proj / "images.json").read_text(encoding="utf-8")
        assert "STALE_RECON" not in (proj / "opensfm" / "reconstruction.json").read_text(encoding="utf-8")

        # 6. Artefacto faltante (dsm.tif borrado cuando se pide --dsm) invalida reuso
        (tmp_path / "calls.log").unlink()
        (proj / "odm_dem" / "dsm.tif").unlink()
        r6 = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n",
        )
        assert r6.returncode == 0, r6.stdout + r6.stderr
        assert len((tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()) == 2

    def test_purgar_intermedios_whitelist_y_blacklist(self, tmp_path):
        proj = _preparar_proyecto(tmp_path)
        r = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO
            + "\nrun_odm rgb rgb_odm 8 4 --dsm\n_purgar_intermedios_odm processing/rgb_odm\n_purgar_intermedios_odm processing/no_existe\n",
            env_extra={"VERBOSE": "1"},
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert not (tmp_path / "processing" / "no_existe" / ".raptor_purgado").exists()

        # Blacklist borrada
        for borrado in [
            proj / "opensfm" / ".tracks.csv.bak",
            proj / "opensfm" / "undistorted",
            proj / "opensfm" / "features",
            proj / "opensfm" / "matches",
            proj / "opensfm" / "reports",
            proj / "opensfm" / "tracks.csv",
            proj / "odm_filterpoints",
            proj / "odm_meshing",
            proj / "odm_texturing",
            proj / "odm_texturing_25d",
        ]:
            assert not borrado.exists(), f"{borrado} debería haberse purgado"

        # Whitelist conservada
        for conservado in [
            proj / "opensfm" / "reconstruction.json",
            proj / "images.json",
            proj / "images" / "DJI_0001.JPG",
            proj / "geo.txt",
            proj / "odm_orthophoto" / "odm_orthophoto.tif",
            proj / "odm_dem" / "dsm.tif",
            proj / "odm_georeferencing" / "odm_georeferenced_model.laz",
            proj / ".raptor_odm_ok",
            proj / ".raptor_purgado",
        ]:
            assert conservado.exists(), f"{conservado} NO debería haberse borrado"
        assert "Purga disco" in r.stdout

    def test_reintento_fase2_con_tracks_intacto_invalida_done_txt(self, tmp_path):
        proj = _preparar_proyecto(tmp_path)
        # Escenario real --rerun-from openmvs --optimize-disk-space: tracks.csv sobrevive,
        # mvstex borra undistorted/images y luego georeferencing/DEM falla.
        stub = r"""
_odm_invocar() {
  local name="$1" conc="$2" logfile="$3" label="$4" resume="$5"; shift 5
  local p="processing/$name"
  local attempt_num=1
  [[ -f phase2_failed.flag ]] && attempt_num=2
  echo "attempt=${attempt_num}|resume=${resume}" >> calls.log
  if [[ "$resume" == "" ]]; then
    if [[ "$attempt_num" -eq 2 ]]; then
      [[ -s "$p/opensfm/tracks.csv" ]] || return 12
      [[ ! -e "$p/opensfm/undistorted/nominal_done.txt" ]] || return 13
      [[ ! -e "$p/opensfm/undistorted/primary_done.txt" ]] || return 14
    fi
    mkdir -p "$p/opensfm/undistorted/images" "$p/opensfm/features" "$p/opensfm/matches"
    echo '{}' > "$p/opensfm/reconstruction.json"
    echo "real_tracks_intact_12345" > "$p/opensfm/tracks.csv"
    echo "u_tracks" > "$p/opensfm/undistorted/tracks.csv"
    echo "done" > "$p/opensfm/undistorted/nominal_done.txt"
    echo "done" > "$p/opensfm/undistorted/primary_done.txt"
    echo "u" > "$p/opensfm/undistorted/images/DJI_0001.JPG"
    return 0
  else
    if [[ ! -f phase2_failed.flag ]]; then
      : > phase2_failed.flag
      rm -rf "$p/opensfm/undistorted/images"
      return 1
    fi
    mkdir -p "$p/odm_orthophoto" "$p/odm_georeferencing" "$p/odm_dem"
    echo '{}' > "$p/images.json"
    echo 'tif' > "$p/odm_orthophoto/odm_orthophoto.tif"
    echo 'laz' > "$p/odm_georeferencing/odm_georeferenced_model.laz"
    echo 'dsm' > "$p/odm_dem/dsm.tif"
    return 0
  fi
}
"""
        r1 = _correr_bash(tmp_path, stub + "\nset +e\nrun_odm rgb rgb_odm 8 4 --dsm\n")
        assert r1.returncode == 1
        assert (proj / "opensfm" / "tracks.csv").is_file()
        assert not (proj / "opensfm" / "undistorted" / "images").exists()

        r2 = _correr_bash(tmp_path, stub + "\nrun_odm rgb rgb_odm 8 4 --dsm\n")
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert (proj / ".raptor_odm_ok").is_file()

    def test_reuso_tolera_mtime_termico_y_subsample_restaurando_images_json_y_flags_no_numericos(self, tmp_path):
        proj = _preparar_proyecto(tmp_path)
        r1 = _correr_bash(
            tmp_path,
            STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n_purgar_intermedios_odm processing/rgb_odm\n",
        )
        assert r1.returncode == 0, r1.stdout + r1.stderr
        assert (proj / ".raptor_odm_ok").is_file()
        assert (proj / ".raptor_images.json.bak").is_file()
        assert (proj / ".raptor_purgado").is_file()

        # Simula prepare_thermal_native_odm.py re-etiquetando mtime y
        # subsample_photos.py / copiar_y_limpiar_exif borrando images.json.
        img1 = proj / "images" / "DJI_0001.JPG"
        st = img1.stat()
        os.utime(img1, (st.st_atime + 3600, st.st_mtime + 3600))
        (proj / "images.json").unlink()
        (tmp_path / "calls.log").unlink()

        r2 = _correr_bash(tmp_path, STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n")
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert not (tmp_path / "calls.log").exists(), (
            "Cambio de mtime o borrado de images.json NO debe invalidar ni purgar"
        )
        assert (tmp_path / "outputs" / "logs" / ".reusado_rgb").is_file()
        assert (proj / "images.json").is_file(), "Debe restaurar images.json desde .raptor_images.json.bak"
        assert (proj / "odm_orthophoto" / "odm_orthophoto.tif").is_file()
        assert (proj / "odm_dem" / "dsm.tif").is_file()

        # Valores no numéricos ("yes", "1.0") no deben romper con error de sintaxis de bash -eq
        for bad_val in ("yes", "1.0"):
            (tmp_path / "calls.log").unlink(missing_ok=True)
            r3 = _correr_bash(
                tmp_path,
                STUB_INVOCAR_COMPLETO + "\nrun_odm rgb rgb_odm 8 4 --dsm\n_purgar_intermedios_odm processing/rgb_odm\n",
                env_extra={"RAPTOR_OPTIMIZE_DISK": bad_val, "VERBOSE": bad_val},
            )
            assert r3.returncode == 0, f"Valor {bad_val} falló: {r3.stdout + r3.stderr}"
            assert all(
                "--optimize-disk-space" not in c
                for c in (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
            )
