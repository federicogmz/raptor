"""Motor de ejecución para el Modo 2.5D de RAPTOR empleando Zenith.

Orquesta la ortorrectificación directa por rayos en Rust (Zenith), la
extracción radiométrica con dji_irp (DJI Thermal SDK), y la integración
con los productos post-procesados del Geovisor (hotspots, resumen de
situación y teselas XYZ).
"""

import glob
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core.thermal_params import (
    SIATA_DISTANCE,
    SIATA_EMISSIVITY,
    SIATA_HUMIDITY,
    SIATA_REFLECTION,
    resolve_thermal_params,
)

_BASE_DIR = Path(__file__).resolve().parent.parent
SDK_LIB_DIR = _BASE_DIR / "dji_thermal_sdk" / "tsdk-core" / "lib" / "linux" / "release_x64"
DJI_IRP = _BASE_DIR / "dji_thermal_sdk" / "utility" / "bin" / "linux" / "release_x64" / "dji_irp"

# Parámetros ambientales por defecto (estándar operativo SIATA para calibración con dji_irp).
EMISSIVITY = SIATA_EMISSIVITY
HUMIDITY = SIATA_HUMIDITY
DISTANCE = SIATA_DISTANCE
REFLECTION = SIATA_REFLECTION

ZENITH_RGB_STAGE = "Zenith RGB"
ZENITH_THERMAL_STAGE = "Zenith THERMAL"

# Dimensiones nominales del arreglo térmico (512x640 estándar; 1024x1280 en super-resolución)
IMAGE_HEIGHT = 512
IMAGE_WIDTH = 640


def _build_dji_irp_cmd(
    src_jpg: str,
    dst_raw: str,
    emissivity: float,
    humidity: float,
    distance: float,
    reflection: float,
) -> List[str]:
    return [
        str(DJI_IRP),
        "-s",
        src_jpg,
        "-a",
        "measure",
        "-o",
        dst_raw,
        "--measurefmt",
        "float32",
        "--emissivity",
        str(emissivity),
        "--humidity",
        str(humidity),
        "--distance",
        str(distance),
        "--reflection",
        str(reflection),
    ]


def _emit_progress(
    progress_file: Optional[Path],
    event: str,
    stage: str,
    detail: str = "",
    channel_stage: Optional[str] = None,
    step_n: Optional[int] = None,
    step_total: Optional[int] = None,
):
    if not progress_file:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}\t{event}\t{stage}\t{detail}\n"
    if channel_stage:
        n_part = f"\tn={step_n}" if step_n is not None else ""
        tot_part = f"\ttotal={step_total}" if step_total is not None else ""
        ch_line = f"stage\tt={int(time.time())}\tname={channel_stage}{n_part}{tot_part}\n"
        line = line + ch_line
    try:
        with open(progress_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        print(f"  ⚠ Aviso al escribir progreso: {exc}")


def _run_zenith_streaming(
    cmd: List[str],
    log_path: Path,
    progress_file: Optional[Path],
    channel_stage: str,
    step_total: int = 100,
    timeout: int = 900,
) -> int:
    """Ejecuta un comando de Zenith capturando salida en streaming continuo a log_path y emitiendo progreso SSE."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _emit_progress(
        progress_file,
        "start",
        "thermal_ortho" if "thermal" in channel_stage.lower() else "rgb_odm",
        channel_stage,
        channel_stage=channel_stage,
        step_n=1,
        step_total=step_total,
    )

    t0 = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    last_step = 1
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n--- Invocando: {' '.join(cmd)} (PID: {proc.pid}) ---\n")
        log_file.flush()

        try:
            while True:
                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue

                log_file.write(line)
                log_file.flush()

                if time.time() - t0 > timeout:
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

                # Detección heurística de progreso (e.g. [15/50])
                if "/" in line:
                    for token in line.strip().split():
                        clean_tok = token.strip("[](),")
                        if "/" in clean_tok:
                            parts = clean_tok.split("/")
                            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                                cur = int(parts[0])
                                tot = int(parts[1])
                                if tot > 0 and cur <= tot:
                                    step_n = max(1, min(step_total, int((cur / tot) * step_total)))
                                    if step_n != last_step:
                                        last_step = step_n
                                        _emit_progress(
                                            progress_file,
                                            "progress",
                                            "thermal_ortho" if "thermal" in channel_stage.lower() else "rgb_odm",
                                            f"{channel_stage} ({cur}/{tot})",
                                            channel_stage=channel_stage,
                                            step_n=last_step,
                                            step_total=step_total,
                                        )

            proc.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            log_file.write(f"\n❌ TIMEOUT ({timeout}s) en {channel_stage}\n")
            log_file.flush()
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(0.5)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            raise exc

    if proc.returncode == 0:
        _emit_progress(
            progress_file,
            "done",
            "thermal_ortho" if "thermal" in channel_stage.lower() else "rgb_odm",
            channel_stage,
            channel_stage=channel_stage,
            step_n=step_total,
            step_total=step_total,
        )

    return proc.returncode


def write_zenith_flight_quality(
    manifests: dict,
    output_dir: Path,
    flight_path_json: Optional[Path] = None,
    thermal_params: Optional[dict] = None,
) -> Path:
    """Genera output_dir / flight_quality.json con métricas consolidadas del vuelo y ortorrectificación 2.5D."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dst_file = output_dir / "flight_quality.json"

    if flight_path_json is None:
        cand_fp = output_dir / "flight_path.geojson"
        if cand_fp.exists():
            flight_path_json = cand_fp

    thermal_m = manifests.get("thermal", {})
    rgb_m = manifests.get("rgb", {})

    has_thermal = bool(thermal_m)
    has_rgb = bool(rgb_m)
    status = "ok" if (has_thermal or has_rgb) else "degraded"

    gsds = []
    if "gsd_m" in thermal_m and thermal_m["gsd_m"]:
        try:
            gsds.append(float(thermal_m["gsd_m"]))
        except (ValueError, TypeError):
            pass
    if "gsd_m" in rgb_m and rgb_m["gsd_m"]:
        try:
            gsds.append(float(rgb_m["gsd_m"]))
        except (ValueError, TypeError):
            pass

    effective_gsd_cm = round(min(gsds) * 100.0, 2) if gsds else None
    crs = thermal_m.get("crs") or rgb_m.get("crs") or "EPSG:4326"

    quality_report = {
        "schema_version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "engine": "Zenith 2.5D Direct Ortho Engine",
        "crs": crs,
        "gsd_estimated_cm": effective_gsd_cm,
        "thermal_params": thermal_params,
        "sensors": {
            "thermal": {
                "processed": has_thermal,
                "photos_count": thermal_m.get("images_count", 0),
                "manifest": thermal_m,
            },
            "rgb": {
                "processed": has_rgb,
                "photos_count": rgb_m.get("images_count", 0),
                "manifest": rgb_m,
            },
        },
        "flight_path_available": flight_path_json is not None and Path(flight_path_json).exists(),
    }

    with tempfile.NamedTemporaryFile("w", dir=str(output_dir), delete=False, encoding="utf-8") as tmp:
        json.dump(quality_report, tmp, indent=2, ensure_ascii=False)
        tmp_name = tmp.name

    os.replace(tmp_name, dst_file)
    return dst_file


def _extract_one_raw(
    src_jpg: str,
    dst_raw: str,
    env: dict,
    emissivity: float = EMISSIVITY,
    humidity: float = HUMIDITY,
    distance: float = DISTANCE,
    reflection: float = REFLECTION,
) -> Tuple[bool, float]:
    try:
        cmd = _build_dji_irp_cmd(src_jpg, dst_raw, emissivity, humidity, distance, reflection)
        res = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        eff_humidity = float(humidity)
        # Sensores R-JPEG v1 (ej. Zenmuse H20T) imponen rango [20, 100] en dirp_set_measurement_params (rc -3 / 253)
        if (
            res.returncode != 0
            and float(humidity) < 20.0
            and (res.returncode == 253 or "dirp_set_measurement_params" in (res.stdout + res.stderr))
        ):
            eff_humidity = 20.0
            cmd_fallback = _build_dji_irp_cmd(src_jpg, dst_raw, emissivity, 20.0, distance, reflection)
            res = subprocess.run(
                cmd_fallback,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        ok = res.returncode == 0 and os.path.exists(dst_raw) and os.path.getsize(dst_raw) > 0
        return ok, eff_humidity
    except Exception as e:
        print(f"  ⚠ Fallo al extraer {os.path.basename(src_jpg)}: {e}", file=sys.stderr)
        return False, float(humidity)


def extract_thermal_raw_batch(
    thermal_images: List[str],
    raw_dir: Path,
    max_workers: int = 8,
    emissivity: Optional[float] = None,
    humidity: Optional[float] = None,
    distance: Optional[float] = None,
    reflection: Optional[float] = None,
) -> Tuple[int, int]:
    """Extrae datos float32 crudos (°C) usando dji_irp en paralelo y guarda sidecar thermal_params.json."""
    cfg = resolve_thermal_params(emissivity, humidity, distance, reflection)
    eff_emissivity = cfg["emissivity"]
    eff_humidity = cfg["humidity"]
    eff_distance = cfg["distance"]
    eff_reflection = cfg["reflection"]
    source_label = cfg["source"]

    raw_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(SDK_LIB_DIR) + ":" + env.get("LD_LIBRARY_PATH", "")

    ok_count = 0
    fail_count = 0
    clamped_count = 0
    eff_humidity_val = float(eff_humidity)
    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for img in thermal_images:
            raw_name = Path(img).stem + ".raw"
            dst_raw = str(raw_dir / raw_name)
            if os.path.exists(dst_raw) and os.path.getsize(dst_raw) > 0:
                ok_count += 1
                continue
            tasks.append(
                pool.submit(
                    _extract_one_raw,
                    img,
                    dst_raw,
                    env,
                    eff_emissivity,
                    eff_humidity,
                    eff_distance,
                    eff_reflection,
                )
            )

        for fut in as_completed(tasks):
            res_val = fut.result()
            if isinstance(res_val, tuple):
                ok, eff_h = res_val
            else:
                ok, eff_h = bool(res_val), float(eff_humidity)
            if ok:
                ok_count += 1
                if eff_h != float(eff_humidity):
                    clamped_count += 1
                    eff_humidity_val = eff_h
            else:
                fail_count += 1

    if clamped_count > 0:
        print(
            f"  ⚠ Aviso: sensor R-JPEG v1 detectado en {clamped_count} imágenes; humedad ajustada ({eff_humidity}% -> {eff_humidity_val}%)."
        )

    sidecar = {
        "sdk": "DJI Thermal SDK v1.8 dji_irp (DIRP API 0x13)",
        "format": "Float32 apparent temperature (°C)",
        "emissivity": float(eff_emissivity),
        "humidity_requested_pct": float(eff_humidity),
        "humidity_effective_pct": eff_humidity_val,
        "distance_m": float(eff_distance),
        "reflection_c": float(eff_reflection),
        "parameter_source": source_label,
        "rjpeg_v1_humidity_clamped": clamped_count > 0,
        "clamped_images_count": clamped_count,
    }
    try:
        (raw_dir / "thermal_params.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"  ⚠ Aviso al escribir thermal_params.json: {exc}")

    return ok_count, fail_count


def run_zenith_25d(
    source_dir: Path,
    output_dir: Path,
    progress_file: Optional[Path] = None,
    dem_source: str = "auto",
    external_dem: Optional[Path] = None,
    flat_elevation: Optional[float] = None,
    emissivity: Optional[float] = None,
    humidity: Optional[float] = None,
    distance: Optional[float] = None,
    reflection: Optional[float] = None,
    crs: Optional[str] = None,
) -> Dict:
    """Ejecuta el pipeline completo de ortorrectificación directa 2.5D con Zenith."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    t0 = time.time()
    source_dir = Path(source_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prep_dir = output_dir.parent / "preprocessing"
    prep_dir.mkdir(parents=True, exist_ok=True)

    thermal_cfg = resolve_thermal_params(emissivity, humidity, distance, reflection)

    # 1. Clasificación de imágenes
    all_jpgs = sorted(glob.glob(str(source_dir / "*.jpg")) + glob.glob(str(source_dir / "*.JPG")))
    thermal_jpgs = [p for p in all_jpgs if Path(p).stem.endswith("_T") or "_T_" in Path(p).stem]
    rgb_jpgs = [p for p in all_jpgs if p not in thermal_jpgs]

    print("════════════════════════════════════════════════════════════")
    print("  RAPTOR MODO 2.5D (ZENITH DIRECT ORTHO ENGINE)")
    print("════════════════════════════════════════════════════════════")
    print(f"  Fuente: {source_dir}")
    print(f"  Total JPG: {len(all_jpgs)} | RGB: {len(rgb_jpgs)} | Térmicas: {len(thermal_jpgs)}")
    print(f"  Política DEM: {dem_source} (DEM externo: {external_dem})")
    print(
        f"  Parámetros radiométricos: emisividad={thermal_cfg['emissivity']}, "
        f"humedad={thermal_cfg['humidity']}%, dist={thermal_cfg['distance']}m, "
        f"refl={thermal_cfg['reflection']}°C ({thermal_cfg['source']})"
    )

    if not rgb_jpgs and not thermal_jpgs:
        raise ValueError(
            f"No se encontraron imágenes RGB ni térmicas válidas en {source_dir} para procesar en 2.5D. "
            "Si este vuelo es multiespectral (DJI M3M), debe procesarse en Modo 3D (ODM)."
        )

    _emit_progress(progress_file, "start", "preprocessing", "Preparación y telemetría de vuelo")

    # Exportar ruta de vuelo preliminar si está disponible
    flight_path_script = _BASE_DIR / "scripts" / "export_flight_path.py"
    if flight_path_script.exists():
        try:
            subprocess.run([sys.executable, str(flight_path_script)], cwd=str(_BASE_DIR), check=False)
        except Exception as e:
            print(f"  ⚠ Aviso en flight_path: {e}")

    # Encontrar binario de zenith (1. $ZENITH_BIN, 2. PATH, 3. ../zenith/target/release/zenith, 4. ./zenith/...)
    zenith_bin = os.environ.get("ZENITH_BIN", "zenith")
    if zenith_bin == "zenith" and subprocess.run(["which", "zenith"], capture_output=True).returncode != 0:
        sibling_target = _BASE_DIR.parent / "zenith" / "target" / "release" / "zenith"
        local_target = _BASE_DIR / "zenith" / "target" / "release" / "zenith"
        if sibling_target.exists():
            zenith_bin = str(sibling_target)
        elif local_target.exists():
            zenith_bin = str(local_target)
        else:
            raise FileNotFoundError(
                "Binario 'zenith' no encontrado en PATH ni en /home/fdgmz/zenith/target/release/zenith"
            )

    zenith_timeout = int(os.environ.get("ZENITH_TIMEOUT", 900))
    zenith_log = output_dir / "logs" / "zenith.log"

    dem_args = []
    used_local_va = False
    if external_dem and Path(external_dem).exists():
        dem_args.extend(["--dem", str(external_dem)])
    elif flat_elevation is not None:
        dem_args.extend(["--flat-elevation", str(flat_elevation)])
    elif dem_source == "auto":
        local_va_candidates = [
            _BASE_DIR / "DEM_VA" / "dem_VA.tif",
            Path("/home/fdgmz/raptor/DEM_VA/dem_VA.tif"),
            Path("/app/DEM_VA/dem_VA.tif"),
            Path("/mnt/f/onedrive/OneDrive - Universidad EAFIT/2025_2_drones_incendios/DEM_VA/dem_VA.tif"),
        ]
        local_va_dem = None
        for cand in local_va_candidates:
            if cand.exists():
                local_va_dem = cand
                break
        if local_va_dem is not None:
            dem_args.extend(["--dem", str(local_va_dem)])
            used_local_va = True
            print(f"  DEM local regional detectado: {local_va_dem}")
        else:
            dem_args.extend(["--dem-source", "auto"])
    else:
        dem_args.extend(["--dem-source", dem_source])

    if crs:
        dem_args.extend(["--crs", str(crs)])

    products_generated = []
    zenith_manifests = {}
    thermal_ok = False
    rgb_ok = False

    _emit_progress(progress_file, "done", "preprocessing", "Preparación y telemetría de vuelo")

    # 2. Ortorrectificación Térmica (si hay fotos térmicas)
    if thermal_jpgs:
        _emit_progress(
            progress_file,
            "start",
            "thermal_ortho",
            "Ortorrectificación térmica 2.5D",
            channel_stage=ZENITH_THERMAL_STAGE,
            step_n=1,
            step_total=100,
        )
        print("\n── [2/4] Procesando sensor térmico con Zenith ──")
        raw_dir = prep_dir / "thermal_raw"
        ok_raw, fail_raw = extract_thermal_raw_batch(
            thermal_jpgs,
            raw_dir,
            emissivity=thermal_cfg["emissivity"],
            humidity=thermal_cfg["humidity"],
            distance=thermal_cfg["distance"],
            reflection=thermal_cfg["reflection"],
        )
        print(f"  Extracción radiométrica (°C): {ok_raw} OK, {fail_raw} fallos")

        thermal_raw_out = output_dir / "thermal_orthomosaic.raw"
        thermal_manifest = output_dir / "manifest_thermal_zenith.json"
        feather_args = []
        try:
            help_check = subprocess.run([zenith_bin, "ortho-thermal", "--help"], capture_output=True, text=True, timeout=5)
            if "--feather-px" in help_check.stdout:
                feather_args = ["--feather-px", "10.0", "--max-blend-temp-diff", "3.5"]
        except Exception as exc:
            print(f"  ⚠ Aviso: {exc}")

        cmd_thermal = [
            zenith_bin,
            "ortho-thermal",
            "--images",
            str(source_dir),
            "--raw-dir",
            str(raw_dir),
            "--output",
            str(thermal_raw_out),
            "--manifest",
            str(thermal_manifest),
        ] + feather_args + dem_args

        print(f"  Invocando: {' '.join(cmd_thermal)}")
        try:
            ret = _run_zenith_streaming(cmd_thermal, zenith_log, progress_file, ZENITH_THERMAL_STAGE, timeout=zenith_timeout)
            if ret != 0 and used_local_va:
                print("  ⚠ Falló la ejecución con DEM regional local. Reintentando con DEM Copernicus GLO-30 auto...")
                cmd_thermal_fb = [
                    zenith_bin,
                    "ortho-thermal",
                    "--images",
                    str(source_dir),
                    "--raw-dir",
                    str(raw_dir),
                    "--output",
                    str(thermal_raw_out),
                    "--manifest",
                    str(thermal_manifest),
                ] + feather_args + ["--dem-source", "auto"]
                if crs:
                    cmd_thermal_fb.extend(["--crs", str(crs)])
                ret = _run_zenith_streaming(cmd_thermal_fb, zenith_log, progress_file, ZENITH_THERMAL_STAGE, timeout=zenith_timeout)

            if ret == 0:
                thermal_tif = output_dir / "thermal_orthomosaic.tif"
                if thermal_tif.exists():
                    products_generated.append(str(thermal_tif))
                    thermal_ok = True
                    if thermal_manifest.exists():
                        try:
                            zenith_manifests["thermal"] = json.loads(thermal_manifest.read_text())
                        except Exception as exc:
                            print(f"  ⚠ Aviso: {exc}")
            else:
                print("  ❌ Falló la generación del ortomosaico térmico con Zenith")
        except subprocess.TimeoutExpired as exc:
            print(f"  ❌ Timeout ({zenith_timeout}s) en ortorrectificación térmica con Zenith: {exc}")

        # Limpiar raw_dir temporal y archivos intermedios tras generación
        shutil.rmtree(raw_dir, ignore_errors=True)
        for intermediate in [thermal_raw_out, thermal_raw_out.with_suffix(".hdr")]:
            if intermediate.exists():
                try:
                    intermediate.unlink()
                except Exception as exc:
                    print(f"  ⚠ Aviso: {exc}")

        _emit_progress(
            progress_file,
            "done",
            "thermal_ortho",
            "Ortorrectificación térmica 2.5D",
            channel_stage=ZENITH_THERMAL_STAGE,
            step_n=100,
            step_total=100,
        )

    # 3. Ortorrectificación RGB (si hay fotos visuales)
    if rgb_jpgs:
        _emit_progress(
            progress_file,
            "start",
            "rgb_odm",
            "Ortorrectificación visual RGB 2.5D",
            channel_stage=ZENITH_RGB_STAGE,
            step_n=1,
            step_total=100,
        )
        print("\n── [3/4] Procesando sensor visual RGB con Zenith ──")
        rgb_raw_out = output_dir / "rgb_orthomosaic.raw"
        rgb_manifest = output_dir / "manifest_rgb_zenith.json"

        # Aislar fotos visuales RGB si hay imágenes térmicas en el mismo directorio
        rgb_input_dir = source_dir
        if thermal_jpgs:
            rgb_filtered_dir = prep_dir / "rgb_images"
            rgb_filtered_dir.mkdir(parents=True, exist_ok=True)
            for p in rgb_jpgs:
                target_link = rgb_filtered_dir / Path(p).name
                if not target_link.exists():
                    try:
                        target_link.symlink_to(Path(p).resolve())
                    except OSError:
                        shutil.copy2(p, target_link)
            rgb_input_dir = rgb_filtered_dir

        rgb_blend_args = []
        try:
            help_check_rgb = subprocess.run([zenith_bin, "ortho-rgb", "--help"], capture_output=True, text=True, timeout=5)
            if "--blend-mode" in help_check_rgb.stdout:
                blend_mode = os.environ.get("ZENITH_BLEND_MODE", "micro-feather")
                feather_px = os.environ.get("ZENITH_FEATHER_PX", "25.0")
                rgb_blend_args = ["--blend-mode", blend_mode, "--feather-px", feather_px]
        except Exception as exc:
            print(f"  ⚠ Aviso: {exc}")

        cmd_rgb = [
            zenith_bin,
            "ortho-rgb",
            "--images",
            str(rgb_input_dir),
            "--output",
            str(rgb_raw_out),
            "--manifest",
            str(rgb_manifest),
        ] + rgb_blend_args + dem_args

        print(f"  Invocando: {' '.join(cmd_rgb)}")
        try:
            ret = _run_zenith_streaming(cmd_rgb, zenith_log, progress_file, ZENITH_RGB_STAGE, timeout=zenith_timeout)
            if ret != 0 and used_local_va:
                print("  ⚠ Falló la ejecución RGB con DEM regional local. Reintentando con DEM Copernicus GLO-30 auto...")
                cmd_rgb_fb = [
                    zenith_bin,
                    "ortho-rgb",
                    "--images",
                    str(rgb_input_dir),
                    "--output",
                    str(rgb_raw_out),
                    "--manifest",
                    str(rgb_manifest),
                ] + rgb_blend_args + ["--dem-source", "auto"]
                if crs:
                    cmd_rgb_fb.extend(["--crs", str(crs)])
                ret = _run_zenith_streaming(cmd_rgb_fb, zenith_log, progress_file, ZENITH_RGB_STAGE, timeout=zenith_timeout)

            if ret == 0:
                rgb_tif = output_dir / "rgb_orthomosaic.tif"
                if rgb_tif.exists():
                    products_generated.append(str(rgb_tif))
                    rgb_ok = True
                    if rgb_manifest.exists():
                        try:
                            zenith_manifests["rgb"] = json.loads(rgb_manifest.read_text())
                        except Exception as exc:
                            print(f"  ⚠ Aviso: {exc}")
            else:
                print("  ❌ Falló la generación del ortomosaico RGB con Zenith")
        except subprocess.TimeoutExpired as exc:
            print(f"  ❌ Timeout ({zenith_timeout}s) en ortorrectificación RGB con Zenith: {exc}")

        # Limpiar archivos .raw/.hdr intermedios de RGB
        for intermediate in [rgb_raw_out, rgb_raw_out.with_suffix(".hdr")]:
            if intermediate.exists():
                try:
                    intermediate.unlink()
                except Exception as exc:
                    print(f"  ⚠ Aviso: {exc}")

        # Limpieza condicional y segura de imágenes RGB aisladas
        keep_prep_rgb = os.environ.get("RAPTOR_KEEP_PREP_RGB", "0") in ("1", "true", "True")
        rgb_tif_path = output_dir / "rgb_orthomosaic.tif"
        if rgb_ok and thermal_jpgs and not keep_prep_rgb and rgb_tif_path.exists() and rgb_tif_path.stat().st_size > 0:
            rgb_filtered_dir = prep_dir / "rgb_images"
            if (
                rgb_filtered_dir.exists()
                and not rgb_filtered_dir.is_symlink()
                and os.path.realpath(rgb_filtered_dir).startswith(str(prep_dir.resolve()))
            ):
                try:
                    freed_bytes = sum(f.stat().st_size for f in rgb_filtered_dir.glob("*") if f.is_file() and not f.is_symlink())
                    shutil.rmtree(rgb_filtered_dir, ignore_errors=True)
                    with open(zenith_log, "a", encoding="utf-8") as lf:
                        lf.write(f"  ✓ Limpieza segura de imágenes RGB intermedias (prep_dir/rgb_images, {freed_bytes / (1024*1024):.1f} MB liberados)\n")
                except Exception as exc:
                    print(f"  ⚠ Aviso al limpiar rgb_images: {exc}")
        elif keep_prep_rgb:
            with open(zenith_log, "a", encoding="utf-8") as lf:
                lf.write("  ℹ RAPTOR_KEEP_PREP_RGB activo: se preservan imágenes intermedias en prep_dir/rgb_images\n")

        _emit_progress(
            progress_file,
            "done",
            "rgb_odm",
            "Ortorrectificación visual RGB 2.5D",
            channel_stage=ZENITH_RGB_STAGE,
            step_n=100,
            step_total=100,
        )

    if not thermal_ok and not rgb_ok:
        raise RuntimeError("Zenith no pudo generar ningún ortomosaico (RGB ni térmico).")

    # 4. Post-procesamiento estándar y generación de teselas
    _emit_progress(progress_file, "start", "hotspots", "Detección de focos de calor y análisis situacional")
    print("\n── [4/4] Ejecutando análisis cruzado y generación de teselas ──")

    # Hotspots térmicos
    th_script = _BASE_DIR / "scripts" / "compute_thermal_hotspot.py"
    if thermal_ok and th_script.exists():
        subprocess.run([sys.executable, str(th_script)], cwd=str(_BASE_DIR), check=False)

    # Resumen de situación
    sit_script = _BASE_DIR / "scripts" / "compute_situation_summary.py"
    if thermal_ok and sit_script.exists():
        subprocess.run([sys.executable, str(sit_script)], cwd=str(_BASE_DIR), check=False)

    _emit_progress(progress_file, "done", "hotspots", "Detección de focos de calor y análisis situacional")

    _emit_progress(progress_file, "start", "tiles", "Generación de teselas cartográficas")

    # Teselas para geovisor Leaflet
    tiles_script = _BASE_DIR / "scripts" / "generate_tiles.py"
    if tiles_script.exists():
        subprocess.run([sys.executable, str(tiles_script)], cwd=str(_BASE_DIR), check=False)

    # Exportación COG
    cog_script = _BASE_DIR / "scripts" / "export_cog.py"
    if cog_script.exists() and products_generated:
        subprocess.run([sys.executable, str(cog_script)] + products_generated, cwd=str(_BASE_DIR), check=False)

    _emit_progress(progress_file, "done", "tiles", "Generación de teselas cartográficas")

    # Sincronizar outputs secundarios si output_dir no es /app/outputs
    std_out = _BASE_DIR / "outputs"
    if output_dir.resolve() != std_out.resolve() and std_out.exists():
        for extra in ["situation.json", "hotspots.geojson", "termico_hotspot_class.tif", "flight_path.geojson"]:
            src_f = std_out / extra
            if src_f.exists():
                try:
                    shutil.copy2(src_f, output_dir / extra)
                except Exception as exc:
                    print(f"  ⚠ Aviso: {exc}")

    # Sincronizar tiles a mission_dir/tiles si es distinto a geovisor/tiles
    mission_tiles = output_dir.parent / "tiles"
    std_tiles = _BASE_DIR / "geovisor" / "tiles"
    if mission_tiles.exists() and std_tiles.exists() and mission_tiles.resolve() != std_tiles.resolve():
        try:
            for item in std_tiles.iterdir():
                dst = mission_tiles / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)
        except Exception as exc:
            print(f"  ⚠ Aviso: {exc}")

    # Generar reporte de calidad de vuelo y cobertura
    write_zenith_flight_quality(zenith_manifests, output_dir, thermal_params=thermal_cfg)

    _emit_progress(progress_file, "done", "pipeline", "Misión 2.5D completada")

    elapsed = time.time() - t0
    run_summary = {
        "ok": True,
        "mode": "2.5d",
        "engine": "Zenith 2.5D Direct Photogrammetry",
        "elapsed_seconds": round(elapsed, 2),
        "products": products_generated,
        "thermal_processed": thermal_ok,
        "rgb_processed": rgb_ok,
        "zenith_manifests": zenith_manifests,
        "thermal_params": thermal_cfg,
    }

    summary_path = output_dir / "run_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(run_summary, f, indent=2, ensure_ascii=False)

    print("\n════════════════════════════════════════════════════════════")
    print(f"  ✅ RAPTOR MODO 2.5D COMPLETADO EN {elapsed:.1f} s")
    print(f"  Productos: {products_generated}")
    print("════════════════════════════════════════════════════════════\n")
    return run_summary
