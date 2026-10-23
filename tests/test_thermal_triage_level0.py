"""
tests/test_thermal_triage_level0.py
===================================
Pruebas unitarias completas para el módulo de Triage Térmico Nivel 0.
"""

import json
import math
import os
import tempfile
import numpy as np
import pytest

pytest.importorskip("osgeo")
from osgeo import gdal, osr

from scripts.thermal_triage_level0 import (
    build_camera_basis_enu,
    pixel_to_ray_vector,
    intersect_ray_plane,
    intersect_ray_dem,
    compute_uncertainty_radius,
    deduplicate_hotspots,
    run_thermal_triage,
    DEMReaderInMemory,
    DEFAULT_FOCAL_PX,
)

gdal.UseExceptions()


def test_build_camera_basis_nadir():
    """Verifica que a nadir (-90°) el eje óptico apunte verticalmente hacia abajo."""
    x_c, u_top, z_c = build_camera_basis_enu(yaw_deg=0.0, pitch_deg=-90.0, roll_deg=0.0)

    # z_c debe ser (0, 0, -1)
    np.testing.assert_allclose(z_c, [0.0, 0.0, -1.0], atol=1e-6)
    # x_c (derecha de la imagen) apunta al Este (1, 0, 0)
    np.testing.assert_allclose(x_c, [1.0, 0.0, 0.0], atol=1e-6)
    # u_top (arriba de la imagen) apunta al Norte (0, 1, 0)
    np.testing.assert_allclose(u_top, [0.0, 1.0, 0.0], atol=1e-6)


def test_build_camera_basis_horizontal_north():
    """Verifica que apuntando horizontalmente al Norte (pitch=0, yaw=0) los ejes sean correctos."""
    x_c, u_top, z_c = build_camera_basis_enu(yaw_deg=0.0, pitch_deg=0.0, roll_deg=0.0)

    # z_c apunta al Norte (0, 1, 0)
    np.testing.assert_allclose(z_c, [0.0, 1.0, 0.0], atol=1e-6)
    # x_c apunta al Este (1, 0, 0)
    np.testing.assert_allclose(x_c, [1.0, 0.0, 0.0], atol=1e-6)
    # u_top apunta hacia arriba (0, 0, 1)
    np.testing.assert_allclose(u_top, [0.0, 0.0, 1.0], atol=1e-6)


def test_dji_m3t_intrinsics():
    """Verifica que los intrínsecos de DJI M3T correspondan a la focal calibrada de 9.1mm / 12um (~758.3 px)."""
    assert DEFAULT_FOCAL_PX == pytest.approx(758.33, abs=0.5)


def test_pixel_to_ray_center_pixel():
    """El píxel central debe producir exactamente el eje óptico z_c."""
    w, h = 640, 512
    fx = fy = 758.33
    yaw, pitch = 45.0, -60.0

    _, _, z_c = build_camera_basis_enu(yaw_deg=yaw, pitch_deg=pitch)
    ray = pixel_to_ray_vector(w / 2.0, h / 2.0, w, h, fx, fy, yaw_deg=yaw, pitch_deg=pitch)

    np.testing.assert_allclose(ray, z_c, atol=1e-6)


def test_round_trip_projection_and_ray():
    """
    Test de round-trip: Proyectar un punto de terreno conocido (X, Y, Z) a píxel
    y reconstruir el rayo. El rayo debe apuntar exactamente hacia el punto original.
    """
    w, h = 640, 512
    fx = fy = 600.0
    c_xyz = np.array([1000.0, 2000.0, 500.0])  # Cámara a 500m
    target_xyz = np.array([1050.0, 2080.0, 150.0])  # Terreno a 150m

    # Vector desde la cámara al blanco
    v_target = target_xyz - c_xyz
    dist_target = np.linalg.norm(v_target)
    dir_target = v_target / dist_target

    # Orientar la cámara aproximadamente hacia el blanco
    yaw = math.degrees(math.atan2(dir_target[0], dir_target[1]))
    pitch = math.degrees(math.asin(dir_target[2]))

    x_c, u_top, z_c = build_camera_basis_enu(yaw, pitch)

    # Proyección inversa analítica del punto al plano focal
    # v_target = s * (z_c + ((u - cx)/fx)*x_c - ((v - cy)/fy)*u_top)
    proj_z = np.dot(v_target, z_c)
    proj_x = np.dot(v_target, x_c)
    proj_y = np.dot(v_target, u_top)

    u = (w / 2.0) + (proj_x / proj_z) * fx
    v = (h / 2.0) - (proj_y / proj_z) * fy

    # Convertir (u, v) de vuelta a rayo
    ray_reconstructed = pixel_to_ray_vector(u, v, w, h, fx, fy, yaw, pitch)

    # Verificar que coincidan
    cosine_sim = np.dot(ray_reconstructed, dir_target)
    assert cosine_sim > 0.999999


def test_intersect_ray_plane_nadir():
    """Verifica la intersección analítica con un plano horizontal a nadir."""
    c_xyz = np.array([500.0, 1000.0, 300.0])
    ray_dir = np.array([0.0, 0.0, -1.0])
    ground_z = 100.0

    impact, method, s = intersect_ray_plane(c_xyz, ray_dir, ground_z)
    assert method == "plane_fallback"
    assert s == pytest.approx(200.0)
    np.testing.assert_allclose(impact, [500.0, 1000.0, 100.0])


def test_intersect_ray_dem_synthetic():
    """
    Crea un DEM sintético con una pendiente conocida y prueba la intersección
    por ray-marching y bisección.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem_test.tif")
        w, h = 100, 100
        res = 2.0  # 2m por píxel

        # Terreno con pendiente: Z = 100 + 0.1 * X
        x0, y0 = 500000.0, 700000.0
        elev = np.zeros((h, w), dtype=np.float32)
        for r in range(h):
            for c in range(w):
                elev[r, c] = 100.0 + 0.1 * (c * res)

        # Crear GeoTIFF
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(dem_path, w, h, 1, gdal.GDT_Float32)
        ds.SetGeoTransform([x0, res, 0.0, y0, 0.0, -res])

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32618)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(elev)
        ds = None

        # Cargar con DEMReaderInMemory
        bbox = (x0, y0 - h * res, x0 + w * res, y0)
        reader = DEMReaderInMemory(dem_path, srs.ExportToWkt(), bbox_utm=bbox, buffer_m=50.0)

        # Cámara sobre el centro del DEM a 300m de altura, mirando hacia abajo
        c_xyz = np.array([x0 + 50.0, y0 - 50.0, 300.0])
        ray_dir = np.array([0.0, 0.0, -1.0])

        impact, method, s = intersect_ray_dem(c_xyz, ray_dir, reader)
        assert method == "external_dem"
        assert impact is not None

        # La cota esperada en x = x0 + 50 es 100 + 0.1 * 50 = 105.0 m
        expected_z = 105.0
        assert impact[2] == pytest.approx(expected_z, abs=0.1)
        assert impact[0] == pytest.approx(c_xyz[0], abs=0.1)
        assert impact[1] == pytest.approx(c_xyz[1], abs=0.1)

        # Ahora probamos con ondulación geoidal de -24.0 m
        reader_geoid = DEMReaderInMemory(dem_path, srs.ExportToWkt(), bbox_utm=bbox, buffer_m=50.0, geoid_undulation_m=-24.0)
        impact_g, method_g, s_g = intersect_ray_dem(c_xyz, ray_dir, reader_geoid)
        assert method_g == "external_dem"
        # La elevación ajustada debe ser 105.0 m
        assert impact_g[2] == pytest.approx(105.0, abs=0.1)


def test_multi_crossing_ridge_protection():
    """
    Verifica que el ray marching detecte el PRIMER impacto y no salte
    sobre una cresta topográfica hacia un valle trasero.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "ridge_dem.tif")
        w, h = 200, 50
        res = 2.0
        x0, y0 = 0.0, 100.0

        # Crear perfil con cresta alta en x=100 (columna 50)
        elev = np.zeros((h, w), dtype=np.float32) + 50.0
        for c in range(w):
            x = c * res
            # Cresta gaussiana de 100m de altura en x = 80m
            elev[:, c] += 100.0 * np.exp(-0.5 * ((x - 80.0) / 10.0) ** 2)

        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(dem_path, w, h, 1, gdal.GDT_Float32)
        ds.SetGeoTransform([x0, res, 0.0, y0, 0.0, -res])
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32618)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(elev)
        ds = None

        bbox = (0.0, 0.0, 400.0, 100.0)
        reader = DEMReaderInMemory(dem_path, srs.ExportToWkt(), bbox_utm=bbox, buffer_m=50.0)

        # Cámara a la izquierda (x=10, z=120) disparando oblicua hacia adelante (dx=0.8, dz=-0.6)
        c_xyz = np.array([10.0, 50.0, 120.0])
        ray_dir = np.array([0.8, 0.0, -0.6])
        ray_dir /= np.linalg.norm(ray_dir)

        impact, method, s = intersect_ray_dem(c_xyz, ray_dir, reader)
        assert method == "external_dem"
        assert impact is not None
        # Debe impactar en la ladera de barlovento de la cresta (x < 80)
        assert impact[0] < 80.0
        assert impact[0] > 60.0


def test_uncertainty_propagation():
    """Verifica que la incertidumbre aumente con la inclinación y sea menor con DEM."""
    # Nadir con DEM
    unc_dem, comp_dem = compute_uncertainty_radius(
        slant_range_m=300.0, off_nadir_deg=0.0, gnss_acc_m=1.0, dem_used=True, dem_vertical_acc_m=2.0
    )
    # Nadir sin DEM (fallback)
    unc_flat, comp_flat = compute_uncertainty_radius(
        slant_range_m=300.0, off_nadir_deg=0.0, gnss_acc_m=1.0, dem_used=False
    )
    # Oblicua 45° con DEM
    unc_oblique, comp_oblique = compute_uncertainty_radius(
        slant_range_m=424.0, off_nadir_deg=45.0, gnss_acc_m=1.0, dem_used=True, dem_vertical_acc_m=2.0
    )

    # A 45° el error de proyección vertical aumenta fuertemente
    assert unc_oblique > unc_dem
    assert comp_oblique["e_vertical_proj_m"] > comp_dem["e_vertical_proj_m"]


def test_deduplication():
    """Verifica que dos observaciones cercanas del mismo foco se consoliden en una sola."""
    obs = [
        {
            "utm_x": 1000.0,
            "utm_y": 2000.0,
            "lat": 6.2001,
            "lon": -75.5001,
            "elevation_m": 1500.0,
            "temp_max_c": 95.0,
            "pixel_count": 12,
            "uncertainty_radius_m": 10.0,
            "geometry_source": "external_dem",
            "image_file": "IMG_0001.tif",
        },
        {
            "utm_x": 1003.0,
            "utm_y": 2002.0,
            "lat": 6.20012,
            "lon": -75.50008,
            "elevation_m": 1501.0,
            "temp_max_c": 112.0,  # Pico más caliente en la segunda foto
            "pixel_count": 15,
            "uncertainty_radius_m": 8.0,
            "geometry_source": "external_dem",
            "image_file": "IMG_0002.tif",
        },
    ]

    consolidated = deduplicate_hotspots(obs)
    assert len(consolidated) == 1
    c = consolidated[0]
    assert c["temp_max_c"] == 112.0
    assert c["n_observations"] == 2
    assert len(c["images"]) == 2
    # La incertidumbre combinada debe respetar el piso sistémico
    assert c["uncertainty_radius_m"] <= 8.0


def test_end_to_end_synthetic_triage():
    """Prueba de extremo a extremo: genera un frame térmico sintético y corre el triage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = os.path.join(tmpdir, "thermal_in")
        os.makedirs(input_dir)
        tif_path = os.path.join(input_dir, "DJI_0001_T.tif")

        # Imagen térmica 640x512 con foco a 130°C en el centro
        w, h = 640, 512
        thermal_data = np.full((h, w), 25.0, dtype=np.float32)
        # Cluster caliente de 5x5 píxeles = 25 píxeles (> 9 px)
        thermal_data[250:255, 315:320] = 130.5

        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(tif_path, w, h, 1, gdal.GDT_Float32)
        ds.GetRasterBand(1).WriteArray(thermal_data)

        # Embeber metadatos EXIF simulados
        ds.SetMetadata(
            {
                "GPSLatitude": "6.2442",
                "GPSLongitude": "-75.5812",
                "GPSAltitude": "1800.0",
                "RelativeAltitude": "120.0",
                "GimbalYawDegree": "10.0",
                "GimbalPitchDegree": "-89.0",
                "GimbalRollDegree": "0.0",
                "RtkStdLon": "0.05",
                "RtkStdLat": "0.05",
            }
        )
        ds = None

        out_geojson = os.path.join(tmpdir, "triage.geojson")
        out_json = os.path.join(tmpdir, "situation.json")

        summary = run_thermal_triage(
            input_dir=input_dir,
            output_geojson=out_geojson,
            output_json=out_json,
            temp_threshold=88.0,
            min_pixels=9,
        )

        assert summary["hotspots_activos"] == 1
        assert summary["temp_max_c"] == pytest.approx(130.5, abs=0.1)

        # Verificar GeoJSON generado
        with open(out_geojson) as f:
            gj = json.load(f)
        assert gj["type"] == "FeatureCollection"
        assert len(gj["features"]) == 1
        feat = gj["features"][0]
        assert feat["properties"]["temp_max_apparent_c"] == pytest.approx(130.5, abs=0.1)
        assert feat["properties"]["pixel_count_total"] == 25
        assert feat["geometry"]["type"] == "Point"
