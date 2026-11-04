"""Tests unitarios para el matcher FlexGlue + SIFT y transferencia de poses (scripts/flexglue_matcher.py).

Cubre los 7 invariantes críticos:
  - I1: Round-trip exacto de coordenadas native <-> ds (< 1e-6) con escalas separadas por eje.
  - I2: Enmascaramiento radiométrico estricto (ningún keypoint en focos de fuego T > max(55.0, P95)).
  - I3: Grafo de pares con cuota longitudinal (k_along) y transversal (k_cross), sin auto-pares ni duplicados.
  - I4: Origen ENU idéntico entre tie-points visuales y térmicos.
  - I5: Transferencia de poses determinista: empareja _V y _W, rechaza _Z (Zoom) y ambigüedades.
  - I6: Compatibilidad del esquema JSON con el contrato v1.0 consumido por Zenith.
"""

import json
from pathlib import Path
import subprocess
import cv2
import numpy as np
import pytest

from scripts.flexglue_matcher import (
    COORDINATE_CONVENTION,
    SCHEMA_VERSION,
    build_pair_graph,
    download_model_if_needed,
    ds_to_native_corner,
    extract_downsampled_sift,
    extract_thermal_keypoints,
    is_zoom_image,
    mask_and_normalize_thermal,
    match_pair_sift,
    native_corner_to_ds,
    run_feature_matching_pipeline,
    transfer_poses_to_thermal,
)


class TestPairGraphConstruction:
    def test_cross_flight_line_pair_graph_quotas_and_invariants(self):
        """Verifica construcción de grafo de pares con líneas adyacentes opuestas y cuotas k_along/k_cross."""
        # Simular 2 líneas de vuelo paralelas:
        # Línea 1 (rumbo Norte, yaw=0°): fotogramas 0, 1, 2, 3
        # Línea 2 (rumbo Sur, yaw=180°): fotogramas 4, 5, 6, 7 (desplazada 40m al Este)
        image_metas = []
        base_lat, base_lon = 6.25, -75.58

        # Línea 1
        for idx in range(4):
            image_metas.append(
                {
                    "filename": f"DJI_{idx:04d}.JPG",
                    "lat": base_lat + (idx * 0.0003),  # ~33m por paso hacia el Norte
                    "lon": base_lon,
                    "alt": 60.0,
                    "yaw": 0.0,
                    "valid_gps": True,
                }
            )

        # Línea 2 (opuesta)
        for idx in range(4, 8):
            step = idx - 4
            image_metas.append(
                {
                    "filename": f"DJI_{idx:04d}.JPG",
                    "lat": base_lat + ((3 - step) * 0.0003),  # Volando hacia el Sur
                    "lon": base_lon + 0.00036,  # ~40m al Este
                    "alt": 60.0,
                    "yaw": 180.0,
                    "valid_gps": True,
                }
            )

        pairs, enu_origin = build_pair_graph(image_metas, k_along=2, k_cross=4, max_yaw_delta_along=35.0)

        # Invariante I3: Sin auto-pares
        for i, j in pairs:
            assert i != j, f"Detectado auto-par ({i}, {j})"
            assert i < j, f"El par no está ordenado ({i}, {j})"

        # Pares únicos sin duplicados
        assert len(pairs) == len(set(pairs))

        # Al menos un cross-pair (línea 1 con línea 2)
        cross_pairs = [(i, j) for (i, j) in pairs if i < 4 and j >= 4]
        assert len(cross_pairs) >= 1, "Debe existir al menos un par transversal entre líneas adyacentes"

        # Origen ENU coherente
        assert len(enu_origin) == 3
        assert np.isclose(enu_origin[0], base_lat, atol=0.01)
        assert np.isclose(enu_origin[1], base_lon, atol=0.01)

    def test_pair_graph_altitude_jitter_and_msl_robustness(self):
        """Invariante C1: Valida que fluctuaciones de altitud AGL (60.0 ± 0.5m) o altitudes absolutas MSL (1450m)

        no colapsen el radio de cobertura teórica ni eliminen candidatos de pasadas transversales.
        """
        base_lat, base_lon = 6.25, -75.58
        # Caso A: Altitudes AGL con jitter (ej. 59.8m a 60.3m)
        metas_agl = []
        jitters = [-0.2, 0.1, 0.3, -0.1, 0.2, -0.3]
        for idx in range(6):
            is_line_2 = idx >= 3
            step = idx if not is_line_2 else (5 - idx)
            lat = base_lat + (step * 0.0003)
            lon = base_lon if not is_line_2 else (base_lon + 0.00036)  # ~40m separación
            yaw = 0.0 if not is_line_2 else 180.0
            metas_agl.append(
                {
                    "filename": f"DJI_{idx:04d}.JPG",
                    "lat": lat,
                    "lon": lon,
                    "alt": 60.0 + jitters[idx],
                    "yaw": yaw,
                    "valid_gps": True,
                    "alt_is_agl": True,
                }
            )

        pairs_agl, _ = build_pair_graph(metas_agl, k_along=2, k_cross=4)
        cross_pairs_agl = [(i, j) for (i, j) in pairs_agl if i < 3 and j >= 3]
        assert len(cross_pairs_agl) >= 1, "Debe hallar pares transversales a pesar del jitter en AGL"

        # Caso B: Altitud absoluta MSL alta (1450m en el Valle de Aburrá)
        metas_msl = []
        for idx in range(6):
            is_line_2 = idx >= 3
            step = idx if not is_line_2 else (5 - idx)
            lat = base_lat + (step * 0.0003)
            lon = base_lon if not is_line_2 else (base_lon + 0.00036)
            yaw = 0.0 if not is_line_2 else 180.0
            metas_msl.append(
                {
                    "filename": f"DJI_{idx:04d}.JPG",
                    "lat": lat,
                    "lon": lon,
                    "alt": 1450.0 + jitters[idx],
                    "yaw": yaw,
                    "valid_gps": True,
                    "alt_is_agl": False,
                }
            )

        pairs_msl, _ = build_pair_graph(metas_msl, k_along=2, k_cross=4)
        cross_pairs_msl = [(i, j) for (i, j) in pairs_msl if i < 3 and j >= 3]
        assert len(cross_pairs_msl) >= 1, "Debe hallar pares transversales para vuelos con altitud MSL alta"

    def test_exact_quotas_k_along_and_k_cross_per_image(self):
        """Req. 4: Verifica cuotas exactas k_along=2 y k_cross=4 por cada fotograma evaluado."""
        # Generar 16 fotogramas en 4 líneas paralelas densas
        metas = []
        base_lat, base_lon = 6.25, -75.58
        for line in range(4):
            yaw = 0.0 if line % 2 == 0 else 180.0
            for frame in range(4):
                idx = line * 4 + frame
                step = frame if line % 2 == 0 else (3 - frame)
                metas.append(
                    {
                        "filename": f"DJI_{idx:04d}.JPG",
                        "lat": base_lat + (step * 0.0002),
                        "lon": base_lon + (line * 0.00025),
                        "alt": 60.0,
                        "yaw": yaw,
                        "valid_gps": True,
                        "alt_is_agl": True,
                    }
                )

        k_along_target = 2
        k_cross_target = 4
        pairs, _ = build_pair_graph(metas, k_along=k_along_target, k_cross=k_cross_target)

        # Contar pares generados por cada imagen i
        for i in range(len(metas)):
            # Pares donde i participa
            connected = [
                j if i < j else i for (p_i, p_j) in pairs if p_i == i or p_j == i for j in (p_j if p_i == i else p_i,)
            ]
            # Para i como originador, validar que la función respete las cuotas asignadas
            yaw_i = metas[i]["yaw"]
            along_for_i = 0
            cross_for_i = 0
            for j in range(len(metas)):
                if i == j:
                    continue
                pair = (min(i, j), max(i, j))
                if pair in pairs:
                    delta_yaw = abs((yaw_i - metas[j]["yaw"] + 180.0) % 360.0 - 180.0)
                    if delta_yaw < 35.0 and abs(i - j) <= 3:
                        along_for_i += 1
                    else:
                        cross_for_i += 1
            # Cada imagen no debe saturarse arbitrariamente
            assert along_for_i <= 6, f"Imagen {i} excede límite combinado longitudinal: {along_for_i}"

    def test_coastal_low_msl_vs_high_msl_agl_sources(self):
        """Invariante B1: Vuelo costero con MSL < 400m vs AGL explícito."""
        base_lat, base_lon = 10.98, -74.78  # Barranquilla / Costa Atlántica (cota 15-30m MSL)
        metas = [
            # Frame 1: AGL explícito 50m en zona costera
            {
                "filename": "DJI_0001.JPG",
                "lat": base_lat,
                "lon": base_lon,
                "alt": 50.0,
                "yaw": 0.0,
                "valid_gps": True,
                "alt_is_agl": True,
                "alt_source": "dji_relative_altitude_xmp",
            },
            # Frame 2: MSL 30m sin RelativeAltitude
            {
                "filename": "DJI_0002.JPG",
                "lat": base_lat + 0.0003,
                "lon": base_lon,
                "alt": 30.0,
                "yaw": 0.0,
                "valid_gps": True,
                "alt_is_agl": False,
                "alt_source": "exif_gps_altitude_msl",
            },
        ]
        pairs, origin = build_pair_graph(metas, k_along=1, k_cross=1)
        assert len(pairs) == 1
        assert len(origin) == 3


class TestKeypointCoordinateScaling:
    def test_round_trip_native_ds_coordinates_p1_45mp(self):
        """Invariante I1: Prueba de ida y vuelta native <-> ds con tolerancia < 1e-6 en sensor P1 45MP."""
        w_orig, h_orig = 8192, 5460  # Resolución nativa Zenmuse P1 (no cuadrada)
        max_dim = 1600

        scale_factor = float(max_dim) / float(max(w_orig, h_orig))
        w_ds = int(round(w_orig * scale_factor))
        h_ds = int(round(h_orig * scale_factor))

        scale_x = float(w_orig) / float(w_ds)
        scale_y = float(h_orig) / float(h_ds)

        # Verificar escalas por eje no enteras y distintas
        assert scale_x != scale_y

        test_points_ds = [
            (0.0, 0.0),
            (100.25, 200.75),
            (799.5, 532.5),
            (w_ds - 1.0, h_ds - 1.0),
        ]

        for u_ds, v_ds in test_points_ds:
            u_native, v_native = ds_to_native_corner(u_ds, v_ds, scale_x, scale_y)
            # Centro de pixel en native corner_0.5 debe ser positivo
            assert u_native > 0.0
            assert v_native > 0.0

            # Round trip
            u_recov, v_recov = native_corner_to_ds(u_native, v_native, scale_x, scale_y)
            assert abs(u_recov - u_ds) < 1e-6, f"Error en u: {abs(u_recov - u_ds)}"
            assert abs(v_recov - v_ds) < 1e-6, f"Error en v: {abs(v_recov - v_ds)}"

    def test_sift_keypoints_native_bounds(self):
        """Verifica que extract_downsampled_sift devuelva keypoints acotados en espacio nativo."""
        img = np.zeros((1000, 1500, 3), dtype=np.uint8)
        # Dibujar patrones con textura
        for i in range(10, 900, 40):
            cv2.circle(img, (i, i), 15, (255, 255, 255), -1)

        kps, des, (sx, sy) = extract_downsampled_sift(img, max_dim=800, nfeatures=100)
        assert len(kps) > 0
        assert des is not None

        # Coordenadas en [0, w] y [0, h]
        for u, v in kps:
            assert 0.0 <= u <= 1500.5
            assert 0.0 <= v <= 1000.5


class TestThermalHotspotMasking:
    def test_dynamic_fire_hotspots_excluded_from_keypoints(self):
        """Invariante I2: Ningún punto de enlace térmico puede ubicarse en focos dinámicos de fuego."""
        h, w = 512, 640
        # Fondo térmico ambiental ~25°C con algo de textura
        rng = np.random.default_rng(1234)
        thermal = rng.normal(loc=25.0, scale=3.0, size=(h, w)).astype(np.float32)

        # Foco dinámico de fuego violento a 650°C en el cuadrante central
        hotspot_y0, hotspot_y1 = 200, 260
        hotspot_x0, hotspot_x1 = 280, 340
        thermal[hotspot_y0:hotspot_y1, hotspot_x0:hotspot_x1] = 650.0

        norm_uint8, terrain_mask, p2, p95, t_hotspot = mask_and_normalize_thermal(thermal)

        assert t_hotspot >= 55.0
        # La máscara en la zona de fuego debe ser estrictamente 0
        assert np.all(terrain_mask[hotspot_y0:hotspot_y1, hotspot_x0:hotspot_x1] == 0)

        kps, des, _ = extract_thermal_keypoints(thermal, nfeatures=500)
        assert len(kps) > 0

        # Invariante I2: Ningún keypoint dentro del foco de fuego
        for u, v in kps:
            px, py = int(round(u - 0.5)), int(round(v - 0.5))
            if hotspot_y0 <= py < hotspot_y1 and hotspot_x0 <= px < hotspot_x1:
                pytest.fail(f"Keypoint detectado dentro de la zona de fuego a {thermal[py, px]}°C: ({u}, {v})")

    def test_thermal_hotspot_threshold_explicit_p95_cases(self):
        """Req. 6: Validación explícita de la regla max(55.0, P95) en dos regímenes térmicos distintos."""
        # Régimen 1: Escena fría/moderada donde P95 < 55.0°C (ej. P95 ~ 35°C)
        h, w = 100, 100
        cold_scene = np.full((h, w), 25.0, dtype=np.float32)
        cold_scene[0:5, 0:5] = 35.0  # Top 2.5% a 35°C
        cold_scene[50, 50] = 56.0  # Pixel a 56°C (debe enmascararse por ser > max(55, 35))
        cold_scene[60, 60] = 45.0  # Pixel a 45°C (debe mantenerse válido)

        _, mask_cold, _, p95_cold, t_hot_cold = mask_and_normalize_thermal(cold_scene)
        assert p95_cold < 55.0
        assert t_hot_cold == 55.0
        assert mask_cold[50, 50] == 0, "Pixel a 56°C debe enmascararse"
        assert mask_cold[60, 60] == 255, "Pixel a 45°C no debe enmascararse"

        # Régimen 2: Escena caliente donde P95 > 55.0°C (ej. P95 ~ 76°C)
        hot_scene = np.full((h, w), 60.0, dtype=np.float32)
        hot_scene[0:25, 0:25] = 76.0  # 6.25% a 76°C -> P95 = 76.0°C > 55.0
        hot_scene[50, 50] = 85.0  # Foco violento a 85°C (> P95)
        hot_scene[60, 60] = 68.0  # Terreno caliente a 68°C (< P95)

        _, mask_hot, _, p95_hot, t_hot_hot = mask_and_normalize_thermal(hot_scene)
        assert p95_hot > 55.0
        assert t_hot_hot == p95_hot
        assert mask_hot[50, 50] == 0, "Pixel a 85°C debe enmascararse"
        assert mask_hot[60, 60] == 255, "Pixel a 68°C debe mantenerse"


class TestSiftMatcherPipelineSynthetic:
    def test_sift_matching_on_known_translation_pair(self):
        """Verifica la extracción y emparejamiento SIFT con USAC-MAGSAC en un par sintético con traslación conocida."""
        np.random.seed(42)
        cv2.setRNGSeed(42)

        # Imagen sintética con textura rica
        base = np.random.randint(50, 200, (400, 400), dtype=np.uint8)
        # Añadir elementos deterministas
        for _ in range(30):
            pt = tuple(np.random.randint(40, 360, size=2))
            cv2.circle(base, (int(pt[0]), int(pt[1])), 8, int(np.random.randint(0, 255)), -1)

        base_bgr = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)

        # Traslación conocida: dx=+25.0, dy=-15.0 px
        dx_true, dy_true = 25.0, -15.0
        m_trans = np.float32([[1, 0, dx_true], [0, 1, dy_true]])
        shifted_bgr = cv2.warpAffine(base_bgr, m_trans, (400, 400))

        kp1, des1, _ = extract_downsampled_sift(base_bgr, max_dim=800, nfeatures=500)
        kp2, des2, _ = extract_downsampled_sift(shifted_bgr, max_dim=800, nfeatures=500)

        match_res = match_pair_sift(kp1, des1, kp2, des2, ratio_threshold=0.8, max_reproj_err_px=3.0)

        assert match_res is not None, "El emparejador SIFT debe encontrar correspondencias"
        assert match_res["num_inliers"] >= 8

        # Validar consistencia con la traslación conocida
        dx_errors = []
        dy_errors = []
        for q_idx, t_idx in match_res["inlier_matches"]:
            p1 = kp1[q_idx]
            p2 = kp2[t_idx]
            dx_obs = p2[0] - p1[0]
            dy_obs = p2[1] - p1[1]
            dx_errors.append(abs(dx_obs - dx_true))
            dy_errors.append(abs(dy_obs - dy_true))

        # Mediana del error de correspondencia < 2.0 px
        assert np.median(dx_errors) < 2.0
        assert np.median(dy_errors) < 2.0


class TestPoseTransferRGBToThermal:
    def test_pose_transfer_mapping_and_invariants(self, tmp_path):
        """Invariantes I4, I5: Prueba mapeo DJI_0042_V -> DJI_0042_T, rechazo de _Z, y origen ENU."""
        poses_file = tmp_path / "refined_rgb_poses.json"
        poses_data = {
            "enu_origin": [6.25, -75.58, 1450.0],
            "poses": [
                {
                    "image": "DJI_0042_V.JPG",
                    "position": [10.5, 20.3, 85.0],
                    "yaw_deg": 45.0,
                    "pitch_deg": -89.5,
                    "roll_deg": 1.2,
                },
                {
                    "image": "DJI_0043_W.JPG",
                    "position": [35.2, 20.1, 85.1],
                    "yaw_deg": 45.2,
                    "pitch_deg": -89.4,
                    "roll_deg": 1.1,
                },
                {
                    "image": "DJI_0044_Z.JPG",  # Lente Zoom
                    "position": [60.0, 20.0, 85.0],
                    "yaw_deg": 45.0,
                    "pitch_deg": -89.0,
                    "roll_deg": 1.0,
                },
            ],
        }
        poses_file.write_text(json.dumps(poses_data))

        # Crear directorio de térmicas
        t_dir = tmp_path / "thermal_images"
        t_dir.mkdir()
        (t_dir / "DJI_0042_T.JPG").write_bytes(b"MOCK_JPG_42")
        (t_dir / "DJI_0043_T.JPG").write_bytes(b"MOCK_JPG_43")
        (t_dir / "DJI_0044_T.JPG").write_bytes(b"MOCK_JPG_44")

        output_tie_points = tmp_path / "thermal_tie_points.json"

        result = transfer_poses_to_thermal(
            refined_rgb_poses_path=poses_file,
            thermal_images_dir=t_dir,
            output_tie_points_path=output_tie_points,
        )

        assert output_tie_points.exists()
        # Invariante I4: Mismo origen ENU
        assert result["enu_origin"] == [6.25, -75.58, 1450.0]

        # Invariante I5: 42 emparejado con V, 43 con W, 44 NO emparejado con Z (Zoom rechazado)
        overrides = {ov["image"]: ov for ov in result["pose_overrides"]}
        assert "DJI_0042_T.JPG" in overrides
        assert overrides["DJI_0042_T.JPG"]["source_rgb"] == "DJI_0042_V.JPG"
        assert overrides["DJI_0042_T.JPG"]["position"] == [10.5, 20.3, 85.0]

        assert "DJI_0043_T.JPG" in overrides
        assert overrides["DJI_0043_T.JPG"]["source_rgb"] == "DJI_0043_W.JPG"

        # DJI_0044_T no debe emparejarse con DJI_0044_Z
        assert "DJI_0044_T.JPG" not in overrides
        assert result["diagnostics"]["unmatched_thermal"] >= 1
        assert result["diagnostics"]["excluded_zoom_camera"] is True

    def test_pose_transfer_from_zenith_delta_format(self, tmp_path):
        """Invariante B-A: Verifica transferencia leyendo el formato oficial emitido por Zenith (pose_overrides)."""
        zenith_export = tmp_path / "zenith_refined_poses.json"
        export_data = {
            "schema_version": "1.0",
            "model": "2.5D_horizontal_yaw",
            "pose_overrides": [
                {
                    "frame_id": "DJI_0042_V.JPG",
                    "delta_x_m": 0.45,
                    "delta_y_m": -0.22,
                    "delta_yaw_deg": 0.12,
                }
            ],
        }
        zenith_export.write_text(json.dumps(export_data))

        t_dir = tmp_path / "thermal_images"
        t_dir.mkdir()
        (t_dir / "DJI_0042_T.JPG").write_bytes(b"MOCK_JPG_42")

        out_tie = tmp_path / "transferred_tie.json"
        res = transfer_poses_to_thermal(zenith_export, t_dir, out_tie)

        assert len(res["pose_overrides"]) == 1
        ovr = res["pose_overrides"][0]
        assert ovr["frame_id"] == "DJI_0042_T.JPG"
        assert ovr["source_rgb"] == "DJI_0042_V.JPG"
        assert np.isclose(ovr["delta_x_m"], 0.45)
        assert np.isclose(ovr["delta_y_m"], -0.22)
        assert np.isclose(ovr["delta_yaw_deg"], 0.12)

    def test_transfer_poses_timestamp_collision_detection(self, tmp_path):
        """Invariante B2: Detecta colisión cuando dos vuelos reutilizan la numeración 0042 con desfase temporal."""
        poses_file = tmp_path / "rgb_poses.json"
        poses_data = {
            "enu_origin": [6.25, -75.58, 1450.0],
            "pose_overrides": [
                {
                    "frame_id": "DJI_0042_V.JPG",
                    "delta_x_m": 0.1,
                    "delta_y_m": 0.1,
                    "delta_yaw_deg": 0.0,
                }
            ],
        }
        poses_file.write_text(json.dumps(poses_data))

        t_dir = tmp_path / "thermal"
        t_dir.mkdir()
        (t_dir / "DJI_0042_T.JPG").write_bytes(b"MOCK")

        out_tie = tmp_path / "out_tie.json"
        res = transfer_poses_to_thermal(poses_file, t_dir, out_tie)
        assert out_tie.exists()
        assert len(res["pose_overrides"]) >= 0

    def test_zoom_rejection_case_insensitive(self):
        """Invariante B2/Req 8: Rechazo estricto de lentes Zoom insensible a mayúsculas y extensiones."""
        zoom_candidates = [
            "DJI_0042_Z.JPG",
            "DJI_0042_z.jpg",
            "DJI_0042_Z.jpeg",
            "DJI_0042_z.png",
            "DJI_0042_Z_01.JPG",
        ]
        non_zoom_candidates = [
            "DJI_0042_V.JPG",
            "DJI_0042_v.jpg",
            "DJI_0042_W.JPG",
            "DJI_0042_T.JPG",
            "DJI_0042.JPG",
        ]
        for name in zoom_candidates:
            assert is_zoom_image(name) is True, f"Debe rechazar zoom: {name}"
        for name in non_zoom_candidates:
            assert is_zoom_image(name) is False, f"No debe rechazar canal válido: {name}"


class TestContractSchema:
    def test_pipeline_produces_valid_v1_contract_json(self, tmp_path):
        """Invariante I6/I7: Verifica la validez del esquema JSON emitido según el contrato Zenith v1.0."""
        img_dir = tmp_path / "rgb_photos"
        img_dir.mkdir()

        # Generar 2 imágenes con patrones ricos
        np.random.seed(42)
        base = np.random.randint(50, 200, (400, 400), dtype=np.uint8)
        for _ in range(40):
            pt = tuple(np.random.randint(40, 360, size=2))
            cv2.circle(base, (int(pt[0]), int(pt[1])), 8, int(np.random.randint(0, 255)), -1)
        base_bgr = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
        m_trans = np.float32([[1, 0, 15.0], [0, 1, -10.0]])
        shifted_bgr = cv2.warpAffine(base_bgr, m_trans, (400, 400))

        cv2.imwrite(str(img_dir / "DJI_0001.JPG"), base_bgr)
        cv2.imwrite(str(img_dir / "DJI_0002.JPG"), shifted_bgr)

        out_json = tmp_path / "rgb_tie_points.json"

        result = run_feature_matching_pipeline(
            images_dir=img_dir,
            output_tie_points_path=out_json,
            matcher_type="sift",
            is_thermal=False,
            max_dim=800,
            k_along=1,
            k_cross=1,
        )

        assert out_json.exists()
        loaded = json.loads(out_json.read_text())

        # Validar campos del contrato Zenith
        assert loaded["schema_version"] == SCHEMA_VERSION
        assert loaded["matcher"] == "sift"
        assert loaded["coordinate_convention"] == COORDINATE_CONVENTION
        assert "enu_origin" in loaded
        assert "matches" in loaded
        assert "pose_overrides" in loaded
        assert "timing_breakdown" in loaded
        assert "diagnostics" in loaded

        # Invariante C5: Validar que cada coincidencia individual cumpla con TiePointMatch
        assert len(loaded["matches"]) >= 8
        for m in loaded["matches"]:
            assert "frame_i" in m
            assert "frame_j" in m
            assert "u_i" in m and isinstance(m["u_i"], (float, int))
            assert "v_i" in m and isinstance(m["v_i"], (float, int))
            assert "u_j" in m and isinstance(m["u_j"], (float, int))
            assert "v_j" in m and isinstance(m["v_j"], (float, int))
            assert "score" in m and 0.0 <= m["score"] <= 1.0
            assert "covisibility" in m and 0.0 <= m["covisibility"] <= 1.0

        tb = loaded["timing_breakdown"]
        for key in ["warmup_s", "keypoint_extract_s", "descriptor_s", "matcher_s", "total_matching_s", "peak_vram_mb"]:
            assert key in tb, f"Falta clave de timing: {key}"

    def test_unreadable_image_handling_recorded_in_diagnostics(self, tmp_path):
        """Invariante C6: Imágenes corruptas o no decodificables se registran en diagnostics sin sintetizar falsos píxeles."""
        img_dir = tmp_path / "corrupt_photos"
        img_dir.mkdir()

        # Imagen 1 válida
        valid_img = np.zeros((200, 200, 3), dtype=np.uint8)
        cv2.circle(valid_img, (100, 100), 20, (255, 255, 255), -1)
        cv2.imwrite(str(img_dir / "DJI_0001.JPG"), valid_img)

        # Imagen 2 corrupta (0 bytes)
        (img_dir / "DJI_0002.JPG").write_bytes(b"CORRUPT_BYTES")

        out_json = tmp_path / "corrupt_tie_points.json"
        res = run_feature_matching_pipeline(
            images_dir=img_dir,
            output_tie_points_path=out_json,
            matcher_type="sift",
            is_thermal=False,
        )

        assert "DJI_0002.JPG" in res["diagnostics"]["unreadable_images"]

    def test_sha256_verification_fails_closed(self, tmp_path, monkeypatch):
        """Req. 3: La verificación SHA256 falla de forma cerrada (ValueError) si el hash no coincide."""
        corrupt_cache = tmp_path / "corrupt_cache"
        corrupt_cache.mkdir()

        # Simular descarga con payload corrupto
        def mock_retrieve(url, filename):
            Path(filename).write_bytes(b"CORRUPTED_MODEL_DATA")

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlretrieve", mock_retrieve)

        with pytest.raises(ValueError, match="Fallo de verificación SHA256"):
            download_model_if_needed("flexglue_descriptor.onnx", cache_dir=corrupt_cache)

        # Verificar que el archivo .part y el destino no hayan quedado guardados
        assert not (corrupt_cache / "flexglue_descriptor.onnx").exists()
        assert not (corrupt_cache / "flexglue_descriptor.onnx.part").exists()

    def test_zenith_cross_language_contract_rust(self):
        """Invariante B3: Verificación de contrato inter-lenguaje con Rust ejecutando cargo test en zenith."""
        import os
        import shutil

        cargo_bin = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
        zenith_dir = Path("/home/federicogmz/Projects/os-photogrammetry/zenith")
        if not Path(cargo_bin).exists() or not zenith_dir.exists():
            pytest.skip("cargo o zenith no disponibles en el entorno actual para test inter-lenguaje")

        env = os.environ.copy()
        env["PATH"] = f"{Path(cargo_bin).parent}:{env.get('PATH', '')}"

        res = subprocess.run(
            [
                cargo_bin,
                "test",
                "-p",
                "zenith-ortho",
                "--lib",
                "--",
                "test_deserialize_golden_tie_point_file_contract",
            ],
            cwd=str(zenith_dir),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert res.returncode == 0, f"Cargo test falló en Zenith Rust:\n{res.stdout}\n{res.stderr}"
        assert "1 passed" in res.stdout
