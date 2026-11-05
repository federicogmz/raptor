"""Tests para el motor de ejecución 2.5D Zenith (core/zenith_runner.py).

Verifica la normalización de modos, el formato TSV de eventos de progreso,
y la ejecución integral de run_zenith_25d con un binario mock de Zenith.
"""

import json
from pathlib import Path
import pytest

from core.runner import STAGE_ORDER, normalize_mode, parse_progress_events
from core.thermal_params import (
    SIATA_DISTANCE,
    SIATA_EMISSIVITY,
    SIATA_HUMIDITY,
    SIATA_REFLECTION,
    resolve_thermal_params,
)
from core.zenith_runner import (
    DISTANCE,
    EMISSIVITY,
    HUMIDITY,
    REFLECTION,
    ZENITH_RGB_STAGE,
    ZENITH_THERMAL_STAGE,
    _emit_progress,
    extract_thermal_raw_batch,
    is_flexglue_available,
    resolve_pose_refinement,
    run_zenith_25d,
    write_zenith_flight_quality,
)


class TestZenithRunnerModeAndProgress:
    def test_normalize_mode_contract(self):
        """Verifica que normalize_mode distinga 2.5d, 3d, y preserve thermal-convert."""
        assert normalize_mode("2.5d") == "2.5d"
        assert normalize_mode("zenith") == "2.5d"
        assert normalize_mode(" 2.5D ") == "2.5d"

        assert normalize_mode("3d") == "3d"
        assert normalize_mode("rgb") == "3d"
        assert normalize_mode("rgb+thermal") == "3d"
        assert normalize_mode("thermal") == "3d"
        assert normalize_mode("none") == "3d"

        assert normalize_mode("thermal-convert") == "thermal-convert"
        assert normalize_mode("THERMAL-CONVERT") == "thermal-convert"

        # Modos desconocidos se preservan para que _validate los rechace
        assert normalize_mode("invalido_xyz") == "invalido_xyz"

    def test_emit_progress_tsv_and_parse_events(self, tmp_path):
        """Verifica que _emit_progress genere formato TSV y parse_progress_events calcule pct > 0."""
        pf = tmp_path / "progress.tsv"

        _emit_progress(pf, "start", "preprocessing", "Preparación y telemetría")
        _emit_progress(pf, "done", "preprocessing", "Preparación y telemetría")
        _emit_progress(pf, "start", "thermal_ortho", "Ortorrectificación térmica 2.5D")
        _emit_progress(pf, "done", "thermal_ortho", "Ortorrectificación térmica 2.5D")
        _emit_progress(pf, "start", "rgb_odm", "Ortorrectificación visual RGB 2.5D")
        _emit_progress(pf, "done", "rgb_odm", "Ortorrectificación visual RGB 2.5D")
        _emit_progress(pf, "start", "hotspots", "Detección de focos de calor")
        _emit_progress(pf, "done", "hotspots", "Detección de focos de calor")
        _emit_progress(pf, "start", "tiles", "Generación de teselas cartográficas")
        _emit_progress(pf, "done", "tiles", "Generación de teselas cartográficas")
        _emit_progress(pf, "done", "pipeline", "Misión 2.5D completada")

        pos, events = parse_progress_events(str(pf), 0)
        assert len(events) == 11
        assert pos > 0

        # Todos los eventos deben tener pct > 0
        for ev in events:
            assert "pct" in ev, f"Evento sin pct: {ev}"
            assert ev["pct"] > 0, f"pct debe ser > 0: {ev}"
            assert ev["stage"] in STAGE_ORDER

        # El evento final done de pipeline debe llegar al 100%
        assert events[-1]["pct"] == 100
        assert events[-1]["stage"] == "pipeline"
        assert events[-1]["event"] == "done"


class TestZenithRunnerExecution:
    def test_run_zenith_25d_with_mock_zenith(self, tmp_path, monkeypatch):
        """Ejecuta run_zenith_25d con un binario mock de Zenith y comprueba limpieza y resumen."""
        # 1. Crear mock ejecutable de zenith
        mock_zenith_path = tmp_path / "zenith"
        mock_script = """#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
log_file = Path("/tmp/mock_zenith_invocations.log")
with open(log_file, "a") as f:
    f.write(" ".join(args) + "\\n")

if "--help" in args:
    print("Zenith CLI --feather-px --blend-mode supported")
    sys.exit(0)

print("[1/2] Mock processing images...")
print("[2/2] Done.")

if "ortho-thermal" in args or "ortho-rgb" in args:
    out_idx = args.index("--output") if "--output" in args else -1
    if out_idx != -1 and out_idx + 1 < len(args):
        raw_out = Path(args[out_idx + 1])
        raw_out.parent.mkdir(parents=True, exist_ok=True)
        raw_out.write_bytes(b"RAW_DATA_MOCK")
        hdr_out = raw_out.with_suffix(".hdr")
        hdr_out.write_text("ENVI")
        tif_out = raw_out.with_suffix(".tif")
        tif_out.write_bytes(b"TIFF_DATA_MOCK")
    manifest_idx = args.index("--manifest") if "--manifest" in args else -1
    if manifest_idx != -1 and manifest_idx + 1 < len(args):
        m_out = Path(args[manifest_idx + 1])
        m_out.write_text('{"gsd_m": 0.15, "crs": "EPSG:32618", "images_count": 1}')
    sys.exit(0)

sys.exit(0)
"""
        mock_zenith_path.write_text(mock_script)
        mock_zenith_path.chmod(0o755)
        monkeypatch.setenv("ZENITH_BIN", str(mock_zenith_path))

        # 2. Mockear extracción de DJI Thermal SDK para no requerir el binario nativo
        def fake_extract_one(src_jpg: str, dst_raw: str, env: dict, *args) -> bool:
            p = Path(dst_raw)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"FLOAT32_MOCK")
            return True

        monkeypatch.setattr("core.zenith_runner._extract_one_raw", fake_extract_one)

        # 3. Crear dataset de entrada (1 térmico y 1 visual)
        source_dir = tmp_path / "input_photos"
        source_dir.mkdir()
        (source_dir / "DJI_0001_T.JPG").write_bytes(b"FAKE_THERMAL_JPG")
        (source_dir / "DJI_0001_W.JPG").write_bytes(b"FAKE_RGB_JPG")

        output_dir = tmp_path / "outputs"
        progress_file = tmp_path / "progress.tsv"

        summary = run_zenith_25d(
            source_dir=source_dir,
            output_dir=output_dir,
            progress_file=progress_file,
            dem_source="copernicus30",
        )

        # 4. Verificaciones sobre el resumen devuelto
        assert summary["ok"] is True
        assert summary["mode"] == "2.5d"
        assert summary["thermal_processed"] is True
        assert summary["rgb_processed"] is True
        assert len(summary["products"]) == 2
        assert "thermal_params" in summary
        assert summary["thermal_params"]["source"] == "default_siata"

        # 5. Verificación de limpieza de archivos intermedios y temporales
        prep_dir = tmp_path / "preprocessing"
        thermal_raw_dir = prep_dir / "thermal_raw"
        assert not thermal_raw_dir.exists(), "thermal_raw debe eliminarse tras el paso térmico"
        assert not (prep_dir / "rgb_images").exists(), "rgb_images filtrado debe eliminarse tras éxito"

        assert not (output_dir / "thermal_orthomosaic.raw").exists(), ".raw intermedio debe eliminarse"
        assert not (output_dir / "thermal_orthomosaic.hdr").exists(), ".hdr intermedio debe eliminarse"
        assert not (output_dir / "rgb_orthomosaic.raw").exists(), ".raw intermedio de RGB debe eliminarse"
        assert not (output_dir / "rgb_orthomosaic.hdr").exists(), ".hdr intermedio de RGB debe eliminarse"

        # Los productos finales TIFF deben existir
        assert (output_dir / "thermal_orthomosaic.tif").exists()
        assert (output_dir / "rgb_orthomosaic.tif").exists()
        assert (output_dir / "run_summary.json").exists()
        assert (output_dir / "flight_quality.json").exists()
        assert (output_dir / "logs" / "zenith.log").exists()

        # 6. Verificación de eventos en progress_file
        assert progress_file.exists()
        _, events = parse_progress_events(str(progress_file), 0)
        standard_events = [ev for ev in events if "pct" in ev]
        channel_events = [ev for ev in events if ev.get("event") == "stage"]
        assert len(standard_events) >= 6
        for ev in standard_events:
            assert ev["pct"] > 0
        assert standard_events[-1]["pct"] == 100
        assert len(channel_events) >= 2

    def test_run_zenith_25d_dem_local_and_blend_mode(self, tmp_path, monkeypatch):
        """Verifica que run_zenith_25d use DEM_VA cuando exista y pase --blend-mode."""
        mock_zenith_path = tmp_path / "zenith_dem"
        invocations_log = tmp_path / "invocations.log"
        mock_script = f"""#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
log_file = Path("{invocations_log}")
with open(log_file, "a") as f:
    f.write(" ".join(args) + "\\n")

if "--help" in args:
    print("Zenith CLI --feather-px --blend-mode supported")
    sys.exit(0)

# Simular que si se pasa un DEM inexistente o invalido falla con 1, sino genera tif
if "--dem" in args:
    dem_idx = args.index("--dem")
    dem_val = args[dem_idx + 1]
    if "failing" in dem_val:
        sys.exit(1)

out_idx = args.index("--output") if "--output" in args else -1
if out_idx != -1 and out_idx + 1 < len(args):
    raw_out = Path(args[out_idx + 1])
    raw_out.parent.mkdir(parents=True, exist_ok=True)
    raw_out.write_bytes(b"RAW")
    tif_out = raw_out.with_suffix(".tif")
    tif_out.write_bytes(b"TIF")
manifest_idx = args.index("--manifest") if "--manifest" in args else -1
if manifest_idx != -1 and manifest_idx + 1 < len(args):
    m_out = Path(args[manifest_idx + 1])
    m_out.write_text('{{"gsd_m": 0.15, "crs": "EPSG:32618", "images_count": 1}}')
sys.exit(0)
"""
        mock_zenith_path.write_text(mock_script)
        mock_zenith_path.chmod(0o755)
        monkeypatch.setenv("ZENITH_BIN", str(mock_zenith_path))
        monkeypatch.setenv("ZENITH_BLEND_MODE", "micro-feather")
        monkeypatch.setenv("ZENITH_FEATHER_PX", "25.0")

        source_dir = tmp_path / "photos"
        source_dir.mkdir()
        (source_dir / "DJI_0001_W.JPG").write_bytes(b"RGB")
        output_dir = tmp_path / "out"

        # Simular DEM_VA/dem_VA.tif
        fake_va_dem = tmp_path / "dem_VA.tif"
        fake_va_dem.write_bytes(b"DEM")
        monkeypatch.setattr("core.zenith_runner._BASE_DIR", tmp_path)
        (tmp_path / "DEM_VA").mkdir()
        (tmp_path / "DEM_VA" / "dem_VA.tif").write_bytes(b"DEM")

        res = run_zenith_25d(source_dir=source_dir, output_dir=output_dir, dem_source="auto")
        assert res["ok"] is True
        log_content = invocations_log.read_text()
        assert "--blend-mode micro-feather" in log_content
        assert "--feather-px 25.0" in log_content
        assert f"--dem {tmp_path / 'DEM_VA' / 'dem_VA.tif'}" in log_content

    def test_extract_thermal_raw_batch_writes_sidecar(self, tmp_path, monkeypatch):
        """Verifica los parámetros SIATA por defecto (0.96, 15, 25, 19) y generación de thermal_params.json."""

        assert EMISSIVITY == 0.96
        assert HUMIDITY == 15.0
        assert DISTANCE == 25.0
        assert REFLECTION == 19.0

        def fake_extract(src_jpg, dst_raw, env, *args):
            p = Path(dst_raw)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00" * 16)
            return True, 20.0

        monkeypatch.setattr("core.zenith_runner._extract_one_raw", fake_extract)
        raw_dir = tmp_path / "raw_out"
        ok, fail = extract_thermal_raw_batch([str(tmp_path / "DJI_0001_T.JPG")], raw_dir)
        assert ok == 1 and fail == 0
        sidecar = json.loads((raw_dir / "thermal_params.json").read_text())
        assert sidecar["emissivity"] == 0.96
        assert sidecar["humidity_requested_pct"] == 15.0
        assert sidecar["humidity_effective_pct"] == 20.0
        assert sidecar["distance_m"] == 25.0
        assert sidecar["reflection_c"] == 19.0
        assert sidecar["rjpeg_v1_humidity_clamped"] is True
        assert sidecar["clamped_images_count"] == 1

    def test_extract_one_raw_rjpeg_v1_fallback_253(self, tmp_path, monkeypatch):
        """Verifica que _extract_one_raw reintente con humidity=20 cuando dji_irp devuelve rc=253."""
        from types import SimpleNamespace
        from core.zenith_runner import _extract_one_raw

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            h_idx = cmd.index("--humidity")
            h_val = float(cmd[h_idx + 1])
            out_idx = cmd.index("-o")
            out_p = Path(cmd[out_idx + 1])
            if h_val < 20.0:
                return SimpleNamespace(
                    returncode=253,
                    stdout="ERROR: call dirp_set_measurement_params failed\n",
                    stderr="",
                )
            out_p.write_bytes(b"\x00" * 16)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.zenith_runner.subprocess.run", fake_run)
        dst_raw = str(tmp_path / "out.raw")
        ok, eff_h = _extract_one_raw(str(tmp_path / "in.JPG"), dst_raw, {})
        assert ok is True
        assert eff_h == 20.0
        assert len(calls) == 2


class TestThermalParamsResolution:
    def test_resolve_default_siata(self):
        """Sin argumentos ni variables de entorno, devuelve exactamente los estándares SIATA."""
        res = resolve_thermal_params()
        assert res["emissivity"] == SIATA_EMISSIVITY
        assert res["humidity"] == SIATA_HUMIDITY
        assert res["distance"] == SIATA_DISTANCE
        assert res["reflection"] == SIATA_REFLECTION
        assert res["source"] == "default_siata"

    def test_resolve_explicit_valid(self):
        """Argumentos válidos tienen precedencia y marcan source=custom."""
        res = resolve_thermal_params(emissivity=0.92, humidity=30.0, distance=15.0, reflection=22.5)
        assert res["emissivity"] == 0.92
        assert res["humidity"] == 30.0
        assert res["distance"] == 15.0
        assert res["reflection"] == 22.5
        assert res["source"] == "custom"

    def test_resolve_env_vars(self, monkeypatch):
        """Variables de entorno se leen si no hay argumentos explícitos."""
        monkeypatch.setenv("THERMAL_EMISSIVITY", "0.95")
        monkeypatch.setenv("THERMAL_HUMIDITY", "45.0")
        monkeypatch.setenv("THERMAL_DISTANCE", "18.0")
        monkeypatch.setenv("THERMAL_REFLECTION", "25.0")

        res = resolve_thermal_params()
        assert res["emissivity"] == 0.95
        assert res["humidity"] == 45.0
        assert res["distance"] == 18.0
        assert res["reflection"] == 25.0
        assert res["source"] == "custom"

    def test_resolve_precedence_explicit_over_env(self, monkeypatch):
        """Argumento explícito > Variable de entorno > SIATA."""
        monkeypatch.setenv("THERMAL_EMISSIVITY", "0.90")
        res = resolve_thermal_params(emissivity=0.98)
        assert res["emissivity"] == 0.98

    def test_resolve_empty_string_and_none(self, monkeypatch):
        """Cadenas vacías y None se tratan como 'no provisto' y caen a SIATA."""
        monkeypatch.delenv("THERMAL_EMISSIVITY", raising=False)
        res = resolve_thermal_params(emissivity="", humidity=None)
        assert res["emissivity"] == SIATA_EMISSIVITY
        assert res["humidity"] == SIATA_HUMIDITY

    def test_resolve_invalid_range_raises_value_error(self):
        """Valores fuera de rango físico DJI SDK lanzan ValueError descriptivo."""
        with pytest.raises(ValueError, match="rango permitido"):
            resolve_thermal_params(emissivity=0.05)

        with pytest.raises(ValueError, match="rango permitido"):
            resolve_thermal_params(emissivity=1.05)

        with pytest.raises(ValueError, match="rango permitido"):
            resolve_thermal_params(humidity=0.5)

        with pytest.raises(ValueError, match="rango permitido"):
            resolve_thermal_params(humidity=105.0)

        with pytest.raises(ValueError, match="rango permitido"):
            resolve_thermal_params(distance=0.0)

        with pytest.raises(ValueError, match="rango permitido"):
            resolve_thermal_params(distance=30.0)

        with pytest.raises(ValueError, match="rango permitido"):
            resolve_thermal_params(reflection=-50.0)

    def test_resolve_invalid_types_and_nan(self):
        """Rechaza booleanos, NaN, inf y textos no numéricos."""
        with pytest.raises(ValueError, match="no puede ser booleano"):
            resolve_thermal_params(emissivity=True)

        with pytest.raises(ValueError, match="no puede ser NaN ni infinito"):
            resolve_thermal_params(emissivity=float("nan"))

        with pytest.raises(ValueError, match="no puede ser NaN ni infinito"):
            resolve_thermal_params(emissivity=float("inf"))

        with pytest.raises(ValueError, match="Valor numérico inválido"):
            resolve_thermal_params(emissivity="invalido_num")

    def test_resolve_siata_defaults_pass_strict_validation(self):
        """Valores SIATA por defecto pasan la validación estricta de rangos."""
        explicit_siata = resolve_thermal_params(
            emissivity=SIATA_EMISSIVITY,
            humidity=SIATA_HUMIDITY,
            distance=SIATA_DISTANCE,
            reflection=SIATA_REFLECTION,
        )
        assert explicit_siata["emissivity"] == SIATA_EMISSIVITY
        assert explicit_siata["humidity"] == SIATA_HUMIDITY
        assert explicit_siata["distance"] == SIATA_DISTANCE
        assert explicit_siata["reflection"] == SIATA_REFLECTION
        assert explicit_siata["source"] == "custom"


class TestFlightQualityAndParity:
    def test_write_zenith_flight_quality_normal(self, tmp_path):
        manifests = {
            "thermal": {"gsd_m": 0.18, "crs": "EPSG:32618", "images_count": 50},
            "rgb": {"gsd_m": 0.08, "crs": "EPSG:32618", "images_count": 50},
        }
        thermal_cfg = {"emissivity": 0.96, "source": "default_siata"}
        fq = write_zenith_flight_quality(manifests, tmp_path, thermal_params=thermal_cfg)
        assert fq.exists()
        data = json.loads(fq.read_text())
        assert data["schema_version"] == "1.0"
        assert data["status"] == "ok"
        assert data["gsd_estimated_cm"] == 8.0
        assert data["crs"] == "EPSG:32618"
        assert data["thermal_params"]["emissivity"] == 0.96
        assert data["sensors"]["thermal"]["photos_count"] == 50
        assert data["sensors"]["rgb"]["photos_count"] == 50

    def test_write_zenith_flight_quality_empty(self, tmp_path):
        """Manifiestos vacíos producen reporte degraded sin lanzar excepción."""
        fq = write_zenith_flight_quality({}, tmp_path)
        assert fq.exists()
        data = json.loads(fq.read_text())
        assert data["status"] == "degraded"
        assert data["gsd_estimated_cm"] is None

    def test_emit_progress_dual_stage_format(self, tmp_path):
        """_emit_progress genera la línea estándar y la línea de canal 'stage\\tt=...'."""
        pf = tmp_path / "progress.tsv"
        _emit_progress(
            pf,
            "start",
            "thermal_ortho",
            "Ortorrectificación térmica 2.5D",
            channel_stage=ZENITH_THERMAL_STAGE,
            step_n=10,
            step_total=100,
        )
        content = pf.read_text()
        lines = content.strip().splitlines()
        assert len(lines) == 2
        assert "thermal_ortho" in lines[0]
        assert lines[1].startswith("stage\tt=")
        assert f"name={ZENITH_THERMAL_STAGE}" in lines[1]
        assert "n=10" in lines[1]
        assert "total=100" in lines[1]

    def test_app_js_channel_steps_parity(self):
        """Verifica que las constantes ZENITH_RGB_STAGE y ZENITH_THERMAL_STAGE coincidan con CHANNEL_STEPS en geovisor/app.js."""
        app_js = Path(__file__).resolve().parent.parent / "geovisor" / "app.js"
        assert app_js.exists(), "geovisor/app.js debe existir"
        js_text = app_js.read_text(encoding="utf-8")
        assert f"'{ZENITH_RGB_STAGE}'" in js_text or f'"{ZENITH_RGB_STAGE}"' in js_text
        assert f"'{ZENITH_THERMAL_STAGE}'" in js_text or f'"{ZENITH_THERMAL_STAGE}"' in js_text


class TestZenithRunnerPoseRefinement:
    def test_pose_refinement_invalid_value(self, tmp_path):
        """Verifica que valores desconocidos de pose_refinement lancen ValueError."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "DJI_0001.JPG").write_bytes(b"FAKE")
        out = tmp_path / "out"
        with pytest.raises(ValueError, match="pose_refinement inválido"):
            run_zenith_25d(source_dir=src, output_dir=out, pose_refinement="invalido_xyz")

    def test_pose_refinement_explicit_error_when_zenith_lacks_support(self, tmp_path, monkeypatch):
        """Invariante B1: Falla explícitamente si pose_refinement != 'none' y Zenith no soporta --tie-points."""
        mock_zenith = tmp_path / "zenith_no_tie_points"
        mock_zenith.write_text("""#!/usr/bin/env python3
import sys
if "--help" in sys.argv:
    print("Zenith legacy without tie points")
sys.exit(0)
""")
        mock_zenith.chmod(0o755)
        monkeypatch.setenv("ZENITH_BIN", str(mock_zenith))

        src = tmp_path / "src"
        src.mkdir()
        (src / "DJI_0001.JPG").write_bytes(b"RGB")
        out = tmp_path / "out"

        with pytest.raises(RuntimeError, match="no soporta --tie-points en ortho-rgb"):
            run_zenith_25d(source_dir=src, output_dir=out, pose_refinement="sift")

    def test_pose_refinement_none_passes_no_new_flags(self, tmp_path, monkeypatch):
        """Invariante I6: pose_refinement='none' preserva exactamente las banderas clásicas sin --tie-points."""
        log_file = tmp_path / "zenith_calls.log"
        mock_zenith = tmp_path / "zenith_mock"
        mock_zenith.write_text(f"""#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
with open("{log_file}", "a") as f:
    f.write(" ".join(args) + "\\n")

if "--help" in args:
    print("--feather-px --blend-mode --tie-points --export-refined-poses")
    sys.exit(0)

if "ortho-rgb" in args or "ortho-thermal" in args:
    out_idx = args.index("--output") if "--output" in args else -1
    if out_idx != -1:
        p = Path(args[out_idx + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RAW")
        p.with_suffix(".tif").write_bytes(b"TIF")
    m_idx = args.index("--manifest") if "--manifest" in args else -1
    if m_idx != -1:
        Path(args[m_idx + 1]).write_text('{{"gsd_m": 0.1}}')
sys.exit(0)
""")
        mock_zenith.chmod(0o755)
        monkeypatch.setenv("ZENITH_BIN", str(mock_zenith))
        monkeypatch.setattr("core.zenith_runner._extract_one_raw", lambda *args, **kwargs: True)

        src = tmp_path / "src"
        src.mkdir()
        (src / "DJI_0001_V.JPG").write_bytes(b"RGB")
        (src / "DJI_0001_T.JPG").write_bytes(b"TH")
        out = tmp_path / "out"

        summary = run_zenith_25d(source_dir=src, output_dir=out, pose_refinement="none")
        assert summary["ok"] is True
        assert summary["pose_refinement"] == "none"

        calls = log_file.read_text().strip().splitlines()
        for call in calls:
            assert "--tie-points" not in call
            assert "--export-refined-poses" not in call

    def test_pose_refinement_sift_orchestration(self, tmp_path, monkeypatch):
        """Verifica la orquestación completa de refinamiento SIFT: RGB primero, transferencia a térmico y banderas."""
        log_file = tmp_path / "zenith_calls_sift.log"
        mock_zenith = tmp_path / "zenith_mock_sift"
        mock_zenith.write_text(f"""#!/usr/bin/env python3
import sys
import json
from pathlib import Path

args = sys.argv[1:]
with open("{log_file}", "a") as f:
    f.write(" ".join(args) + "\\n")

if "--help" in args:
    print("--feather-px --blend-mode --tie-points --export-refined-poses")
    sys.exit(0)

if "ortho-rgb" in args:
    if "--export-refined-poses" in args:
        rp_idx = args.index("--export-refined-poses")
        poses_path = Path(args[rp_idx + 1])
        poses_path.parent.mkdir(parents=True, exist_ok=True)
        poses_data = {{
            "enu_origin": [6.25, -75.58, 1450.0],
            "poses": [
                {{"image": "DJI_0001_V.JPG", "position": [10.0, 20.0, 80.0], "yaw_deg": 0.0, "pitch_deg": -90.0, "roll_deg": 0.0}}
            ]
        }}
        poses_path.write_text(json.dumps(poses_data))
    out_idx = args.index("--output") if "--output" in args else -1
    if out_idx != -1:
        p = Path(args[out_idx + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RAW")
        p.with_suffix(".tif").write_bytes(b"TIF")
    m_idx = args.index("--manifest") if "--manifest" in args else -1
    if m_idx != -1:
        Path(args[m_idx + 1]).write_text('{{"gsd_m": 0.05}}')
    sys.exit(0)

if "ortho-thermal" in args:
    out_idx = args.index("--output") if "--output" in args else -1
    if out_idx != -1:
        p = Path(args[out_idx + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RAW")
        p.with_suffix(".tif").write_bytes(b"TIF")
    m_idx = args.index("--manifest") if "--manifest" in args else -1
    if m_idx != -1:
        Path(args[m_idx + 1]).write_text('{{"gsd_m": 0.15}}')
    sys.exit(0)

sys.exit(0)
""")
        mock_zenith.chmod(0o755)
        monkeypatch.setenv("ZENITH_BIN", str(mock_zenith))
        monkeypatch.setattr("core.zenith_runner._extract_one_raw", lambda *args, **kwargs: True)

        src = tmp_path / "src"
        src.mkdir()
        cv2 = pytest.importorskip("cv2")
        import numpy as np

        # Crear 3 imágenes con solape y textura rica para generar pares y matches no triviales
        np.random.seed(42)
        cv2.setRNGSeed(42)
        base = np.random.randint(50, 200, (400, 400), dtype=np.uint8)
        for _ in range(30):
            pt = tuple(np.random.randint(40, 360, size=2))
            cv2.circle(base, (int(pt[0]), int(pt[1])), 8, int(np.random.randint(0, 255)), -1)
        base_bgr = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)

        cv2.imwrite(str(src / "DJI_0001_V.JPG"), base_bgr)
        (src / "DJI_0001_T.JPG").write_bytes(b"THERMAL_DATA")

        m1 = np.float32([[1, 0, 15.0], [0, 1, 10.0]])
        img2 = cv2.warpAffine(base_bgr, m1, (400, 400))
        cv2.imwrite(str(src / "DJI_0002_V.JPG"), img2)
        (src / "DJI_0002_T.JPG").write_bytes(b"THERMAL_DATA")

        m2 = np.float32([[1, 0, 30.0], [0, 1, 20.0]])
        img3 = cv2.warpAffine(base_bgr, m2, (400, 400))
        cv2.imwrite(str(src / "DJI_0003_V.JPG"), img3)
        (src / "DJI_0003_T.JPG").write_bytes(b"THERMAL_DATA")

        out = tmp_path / "out"

        summary = run_zenith_25d(source_dir=src, output_dir=out, pose_refinement="sift")
        assert summary["ok"] is True
        assert summary["pose_refinement"] == "sift"
        assert summary["pose_refinement_applied"] is True
        assert summary["rgb_processed"] is True
        assert summary["thermal_processed"] is True

        calls = log_file.read_text().strip().splitlines()
        ortho_calls = [c for c in calls if "ortho-" in c and "--help" not in c]
        assert len(ortho_calls) >= 2
        # Orden invertido obligatorio: RGB primero
        assert "ortho-rgb" in ortho_calls[0]
        assert "--tie-points" in ortho_calls[0]
        assert "--export-refined-poses" in ortho_calls[0]

        # Térmico segundo con las poses transferidas
        assert "ortho-thermal" in ortho_calls[1]
        assert "--tie-points" in ortho_calls[1]

        # Validar que los archivos de tie points no estén vacíos
        prep_dir = tmp_path / "preprocessing"
        rgb_tp = json.loads((prep_dir / "rgb_tie_points.json").read_text())
        assert rgb_tp["matcher"] == "sift"
        assert len(rgb_tp["matches"]) >= 1

        th_tp = json.loads((prep_dir / "thermal_tie_points.json").read_text())
        assert len(th_tp["pose_overrides"]) >= 1
        assert th_tp["pose_overrides"][0]["image"] == "DJI_0001_T.JPG"

    def test_pose_refinement_prevents_stale_poses_transfer(self, tmp_path, monkeypatch):
        """Invariante B-B: Asegura que un refined_rgb_poses.json previo se borre y no se transfiera a térmicas si RGB falla."""
        pytest.importorskip("cv2")
        monkeypatch.setattr("core.zenith_runner.is_flexglue_available", lambda: True)
        # Pre-crear archivo de poses obsoleto con coordenadas absurdas en preprocessing
        prep_dir = tmp_path / "preprocessing"
        prep_dir.mkdir(parents=True, exist_ok=True)
        stale_file = prep_dir / "refined_rgb_poses.json"
        stale_file.write_text(
            json.dumps(
                {
                    "enu_origin": [0.0, 0.0, 0.0],
                    "poses": [{"image": "DJI_0001_V.JPG", "position": [99999.0, 99999.0, 99999.0]}],
                }
            )
        )

        mock_zenith = tmp_path / "zenith_mock_fail_rgb"
        mock_zenith.write_text("""#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv[1:]
if "--help" in args:
    print("--feather-px --blend-mode --tie-points --export-refined-poses")
    sys.exit(0)
if "ortho-rgb" in args:
    # Simular fallo en RGB
    sys.exit(1)
if "ortho-thermal" in args:
    out_idx = args.index("--output") if "--output" in args else -1
    if out_idx != -1:
        p = Path(args[out_idx + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RAW")
        p.with_suffix(".tif").write_bytes(b"TIF")
    m_idx = args.index("--manifest") if "--manifest" in args else -1
    if m_idx != -1:
        Path(args[m_idx + 1]).write_text('{"gsd_m": 0.15}')
    sys.exit(0)
""")
        mock_zenith.chmod(0o755)
        monkeypatch.setenv("ZENITH_BIN", str(mock_zenith))
        monkeypatch.setattr("core.zenith_runner._extract_one_raw", lambda *args, **kwargs: True)

        src = tmp_path / "src"
        src.mkdir()
        (src / "DJI_0001_V.JPG").write_bytes(b"RGB")
        (src / "DJI_0001_T.JPG").write_bytes(b"TH")
        out = tmp_path / "out"

        summary = run_zenith_25d(source_dir=src, output_dir=out, pose_refinement="sift")
        assert summary["ok"] is True
        assert summary["pose_refinement_applied"] is False
        assert summary["rgb_processed"] is False
        assert summary["thermal_processed"] is True

        # Verificar que el thermal_tie_points.json generado NO contenga las poses obsoletas [99999.0, ...]
        th_tp_path = prep_dir / "thermal_tie_points.json"
        assert th_tp_path.exists()
        th_data = json.loads(th_tp_path.read_text())
        assert th_data["pose_overrides"] == []

    def test_pose_refinement_auto_thermal_only_focus(self, tmp_path, monkeypatch):
        """Verifica que pose_refinement='auto' y 'flexglue' mantengan RGB en proyección RTK directa por defecto y refinen el térmico."""
        log_file = tmp_path / "zenith_calls_auto.log"
        mock_zenith = tmp_path / "zenith_mock_auto"
        mock_zenith.write_text(f"""#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
with open("{log_file}", "a") as f:
    f.write(" ".join(args) + "\\n")

if "--help" in args:
    print("--feather-px --blend-mode --tie-points --export-refined-poses")
    sys.exit(0)

if "ortho-rgb" in args or "ortho-thermal" in args:
    out_idx = args.index("--output") if "--output" in args else -1
    if out_idx != -1:
        p = Path(args[out_idx + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RAW")
        p.with_suffix(".tif").write_bytes(b"TIF")
    m_idx = args.index("--manifest") if "--manifest" in args else -1
    if m_idx != -1:
        Path(args[m_idx + 1]).write_text('{{"gsd_m": 0.15}}')
    sys.exit(0)

sys.exit(0)
""")
        mock_zenith.chmod(0o755)
        monkeypatch.setenv("ZENITH_BIN", str(mock_zenith))
        monkeypatch.setattr("core.zenith_runner._extract_one_raw", lambda *args, **kwargs: True)

        # Mock feature matcher module so it doesn't try to load heavy ONNX models during unit tests
        class MockMatcherMod:
            @staticmethod
            def run_feature_matching_pipeline(
                images_dir, output_tie_points_path, matcher_type, is_thermal=False, raw_dir=None
            ):
                Path(output_tie_points_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_tie_points_path).write_text(
                    json.dumps({"matcher": matcher_type, "matches": [], "pose_overrides": []})
                )

        monkeypatch.setattr("core.zenith_runner._get_feature_matcher_module", lambda: MockMatcherMod)
        monkeypatch.setattr("core.zenith_runner.is_flexglue_available", lambda: True)

        src = tmp_path / "src"
        src.mkdir()
        (src / "DJI_0001_V.JPG").write_bytes(b"RGB")
        (src / "DJI_0001_T.JPG").write_bytes(b"TH")
        out = tmp_path / "out"

        # 1. Caso default: auto sin forzar RGB
        summary = run_zenith_25d(source_dir=src, output_dir=out, pose_refinement="auto", force_rgb_refinement=False)
        assert summary["ok"] is True
        assert summary["pose_refinement"] == "auto"
        assert summary["pose_refinement_applied"] is True
        assert summary["force_rgb_refinement"] is False

        calls = log_file.read_text().strip().splitlines()
        ortho_calls = [c for c in calls if "ortho-" in c and "--help" not in c]
        # Con auto: RGB debe ser directo RTK (sin --tie-points), Térmico sí debe tener --tie-points
        rgb_call = [c for c in ortho_calls if "ortho-rgb" in c][0]
        th_call = [c for c in ortho_calls if "ortho-thermal" in c][0]
        assert "--tie-points" not in rgb_call
        assert "--tie-points" in th_call

        # 2. Caso con force_rgb_refinement=True
        log_file.unlink()
        summary_forced = run_zenith_25d(
            source_dir=src, output_dir=out, pose_refinement="flexglue", force_rgb_refinement=True
        )
        assert summary_forced["ok"] is True
        assert summary_forced["force_rgb_refinement"] is True
        calls_forced = log_file.read_text().strip().splitlines()
        ortho_calls_forced = [c for c in calls_forced if "ortho-" in c and "--help" not in c]
        rgb_call_forced = [c for c in ortho_calls_forced if "ortho-rgb" in c][0]
        assert "--tie-points" in rgb_call_forced


@pytest.mark.parametrize(
    "mode,modality,is_rtk,force_rgb,expected",
    [
        ("none", "thermal", True, False, "none"),
        ("none", "rgb", True, False, "none"),
        ("sift", "thermal", True, False, "sift"),
        ("sift", "rgb", True, False, "sift"),
        ("flexglue", "thermal", True, False, "flexglue"),
        ("flexglue", "rgb", True, False, "none"),
        ("flexglue", "rgb", True, True, "flexglue"),
        ("flexglue", "rgb", False, False, "flexglue"),
        ("auto", "thermal", True, False, "flexglue"),
        ("auto", "rgb", True, False, "none"),
        ("auto", "rgb", True, True, "flexglue"),
        ("auto", "rgb", False, False, "flexglue"),
        (None, "thermal", True, False, "none"),
    ],
)
def test_resolve_pose_refinement_matrix(monkeypatch, mode, modality, is_rtk, force_rgb, expected):
    """Verifica la resolución per-modalidad bajo RTK, non-RTK, y force_rgb_refinement."""
    monkeypatch.setattr("core.zenith_runner.is_flexglue_available", lambda: True)
    assert resolve_pose_refinement(mode, modality, is_rtk=is_rtk, force_rgb_refinement=force_rgb) == expected


def test_resolve_pose_refinement_fallback(monkeypatch):
    """Verifica que el modo auto haga fallback a none cuando FlexGlue no esté disponible."""
    assert isinstance(is_flexglue_available(), bool)
    monkeypatch.setattr("core.zenith_runner.is_flexglue_available", lambda: False)
    assert resolve_pose_refinement("auto", "thermal") == "none"
    assert resolve_pose_refinement("auto", "rgb", is_rtk=False) == "none"


def test_resolve_pose_refinement_invalid():
    """Verifica que modos o modalidades inválidas se manejen adecuadamente."""
    with pytest.raises(ValueError, match="pose_refinement inválido"):
        resolve_pose_refinement("invalid_mode", "thermal")
    assert resolve_pose_refinement("auto", "unknown_sensor") == "none"
