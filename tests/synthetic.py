"""Misión sintética mínima: ortomosaicos + reconstruction.json de ODM.

Sirve para ejercitar los cuatro `trim_*` de punta a punta sin datos de vuelo.
La geometría es deliberadamente sencilla (vuelo en grilla, cámaras nadir salvo
las que se pidan oblicuas) pero pasa por el MISMO camino que una misión real:
proyección de huellas con las poses SfM, pisos adaptativos y recorte de núcleo.
"""
import json
import os

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()

# Antioquia, zona UTM 18N — mismas coordenadas de orden de magnitud que las
# misiones reales del proyecto.
REF_LAT, REF_LON = 6.4, -75.5
UTM18N = 32618


def _wkt(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s.ExportToWkt()


def escribir_raster(path, arr, gt, epsg=UTM18N, dtype=gdal.GDT_Float32,
                    alpha_last=False, nodata=None, band_names=None):
    """arr: (bandas, H, W) o (H, W)."""
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    n, H, W = arr.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, W, H, n, dtype)
    ds.SetGeoTransform(gt)
    ds.SetProjection(_wkt(epsg))
    if alpha_last:
        # SIEMPRE antes del primer WriteArray: libtiff congela ExtraSamples al
        # escribir la primera tile (mismo motivo documentado en trim_rgb).
        ds.GetRasterBand(n).SetColorInterpretation(gdal.GCI_AlphaBand)
    for i in range(n):
        b = ds.GetRasterBand(i + 1)
        if band_names:
            b.SetDescription(band_names[i])
        if nodata is not None:
            b.SetNoDataValue(nodata)
        b.WriteArray(arr[i])
    ds = None
    return path


def reconstruccion(path, n_lado=10, altura=50.0, paso_m=10.0, w=1024, h=768,
                   focal=0.85, oblicuas=()):
    """reconstruction.json de OpenSfM con un vuelo en grilla n_lado x n_lado.

    El sistema local de OpenSfM tiene origen en reference_lla, X al este, Y al
    norte y Z arriba; la pose guardada es (rotación axis-angle mundo->cámara,
    traslación) con C = -R^T t como centro de cámara. Una cámara nadir mira
    -Z, o sea R = diag(1,-1,-1) (rotación de 180° sobre X).

    oblicuas: índices de shot a los que se les aplica un tilt de 35°, para
    poder ejercitar el piso NADIR.
    """
    # 180° sobre X en axis-angle = (pi, 0, 0)
    nadir_aa = [np.pi, 0.0, 0.0]

    def aa_tilt(deg):
        """Nadir compuesto con una rotación de `deg` sobre Y, en axis-angle."""
        a = np.radians(deg)
        R_nadir = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]], float)
        R_y = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
        R = R_y @ R_nadir
        ang = np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1))
        if abs(ang) < 1e-9:
            return [0.0, 0.0, 0.0]
        ax = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
        ax = ax / (2 * np.sin(ang))
        return list(ax * ang)

    shots = {}
    k = 0
    off = (n_lado - 1) * paso_m / 2
    for iy in range(n_lado):
        for ix in range(n_lado):
            C = np.array([ix * paso_m - off, iy * paso_m - off, altura])
            aa = aa_tilt(35.0) if k in oblicuas else nadir_aa
            R = _aa_to_R_local(aa)
            t = -R @ C
            shots[f"IMG_{k:04d}.JPG"] = {"rotation": list(aa), "translation": list(t)}
            k += 1
    rec = {
        "cameras": {"cam": {"projection_type": "brown", "width": w, "height": h,
                            "focal_x": focal, "focal_y": focal, "c_x": 0.0, "c_y": 0.0}},
        "shots": shots,
        "reference_lla": {"latitude": REF_LAT, "longitude": REF_LON, "altitude": 0.0},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump([rec], f)
    return path


def _aa_to_R_local(aa):
    """Rodrigues — misma fórmula que _aa_to_R del script bajo prueba."""
    x, y, z = aa
    ang = np.sqrt(x * x + y * y + z * z)
    if ang < 1e-12:
        return np.eye(3)
    k = np.array([x, y, z]) / ang
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(ang) * K + (1 - np.cos(ang)) * (K @ K)


def extent_para(n_lado=10, paso_m=10.0, px=0.5, margen_m=50.0):
    """Geotransform y tamaño que cubren el vuelo con margen, centrados en el
    origen local (que es donde cae reference_lla proyectado).

    Los valores por defecto están CALIBRADOS para que el solape tenga un
    gradiente real (máx ~26 fotos en el centro, mediana ~5, ~21% por encima del
    piso de 15): sin eso las huellas cubren el raster entero por igual, los
    pisos de solape no descartan un solo píxel y el test de caracterización
    dejaría de proteger justamente el bloque que se refactoriza."""
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{UTM18N}", always_xy=True)
    e0, n0 = tr.transform(REF_LON, REF_LAT)
    medio = (n_lado - 1) * paso_m / 2 + margen_m
    W = H = int(2 * medio / px)
    gt = (e0 - medio, px, 0.0, n0 + medio, 0.0, -px)
    return gt, W, H
