#!/usr/bin/env python3
"""Resumen legible por máquina de la corrida, para automatización y CI.

Un pipeline encadenado necesita algo más que el código de salida: qué
productos salieron, con qué cobertura y en qué CRS quedó la entrega. Se
escribe siempre en outputs/run_summary.json (y una copia en la carpeta de
entrega, si la hay) — barato de generar y lo consume `./raptor run --json`.

Uso: python3 scripts/run_summary.py [codigo_de_salida]
"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone

import numpy as np
from osgeo import gdal, ogr

gdal.UseExceptions()

OUTPUTS = "outputs"
DESTINO = os.path.join(OUTPUTS, "run_summary.json")

RASTERS = {
    "rgb": f"{OUTPUTS}/rgb_orthomosaic.tif",
    "thermal": f"{OUTPUTS}/thermal_orthomosaic.tif",
    "dsm": f"{OUTPUTS}/dsm.tif",
    "multispectral": f"{OUTPUTS}/multispectral_orthomosaic.tif",
    "confidence": f"{OUTPUTS}/confidence_mask.tif",
    "severidad": f"{OUTPUTS}/severidad_class.tif",
    **{f"indice_{n}": f"{OUTPUTS}/indices/{n}.tif"
       for n in ("ndvi", "gndvi", "ndre", "msavi2")},
}
VECTORES = {
    "area_afectada": f"{OUTPUTS}/area_afectada.geojson",
    "flight_path": f"{OUTPUTS}/flight_path.geojson",
}


def _info_raster(path):
    ds = gdal.Open(path)
    try:
        gt = ds.GetGeoTransform()
        srs = ds.GetSpatialRef()
        info = {
            "archivo": os.path.basename(path),
            "ancho_px": ds.RasterXSize,
            "alto_px": ds.RasterYSize,
            "bandas": ds.RasterCount,
            "gsd_m": round(abs(gt[1]), 4),
            "crs": (f"EPSG:{srs.GetAuthorityCode(None)}"
                    if srs is not None and srs.GetAuthorityCode(None) else None),
            "mb": round(os.path.getsize(path) / 1e6, 1),
        }
        # Cobertura real: qué fracción del recuadro tiene dato. Es el número
        # que dice si el recorte de bordes dejó algo utilizable. Se mide sobre
        # la última banda (el alpha, donde lo hay) submuestreada a 512 px, que
        # alcanza para un porcentaje y no obliga a leer el ráster entero.
        band = ds.GetRasterBand(ds.RasterCount)
        arr = band.ReadAsArray(buf_xsize=min(512, ds.RasterXSize),
                               buf_ysize=min(512, ds.RasterYSize))
        if arr is not None:
            con_dato = np.isfinite(arr) & (arr > 0)
            info["cobertura_pct"] = round(100.0 * float(con_dato.sum()) / arr.size, 1)
        return info
    finally:
        ds = None


def _info_vector(path):
    ds = ogr.Open(path)
    try:
        lyr = ds.GetLayer()
        return {"archivo": os.path.basename(path), "entidades": lyr.GetFeatureCount()}
    finally:
        ds = None


def main():
    codigo = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    resumen = {
        "ok": codigo == 0,
        "exit_code": codigo,
        "terminado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modo": os.environ.get("MODE", "rgb+thermal"),
        "productos": {},
        "vectores": {},
    }

    for clave, path in RASTERS.items():
        if os.path.isfile(path):
            try:
                resumen["productos"][clave] = _info_raster(path)
            except Exception as exc:
                resumen["productos"][clave] = {"error": str(exc)}
    for clave, path in VECTORES.items():
        if os.path.isfile(path):
            try:
                resumen["vectores"][clave] = _info_vector(path)
            except Exception as exc:
                resumen["vectores"][clave] = {"error": str(exc)}

    sit = os.path.join(OUTPUTS, "situation.json")
    if os.path.isfile(sit):
        try:
            with open(sit) as f:
                resumen["situacion"] = json.load(f)
        except Exception:
            pass

    # Cobertura vs. área volada (scripts/compute_coverage.py): la métrica
    # que dice si el mosaico cubre lo que el dron realmente recorrió —
    # para CI, "cobertura_pct" del ráster no alcanza.
    cov = os.path.join(OUTPUTS, "coverage.json")
    if os.path.isfile(cov):
        try:
            with open(cov) as f:
                resumen["cobertura_vs_vuelo"] = json.load(f)
        except Exception:
            pass

    export_dir = os.environ.get("EXPORT_DIR", "").strip()
    if export_dir:
        manifiesto = os.path.join(export_dir, "export_manifest.json")
        resumen["entrega"] = {
            # La ruta del HOST, no la de adentro del contenedor: es la única
            # que le sirve a quien lee esto desde afuera.
            "carpeta": os.environ.get("EXPORT_HOST_DIR", export_dir),
            "crs": os.environ.get("EXPORT_EPSG", "source"),
        }
        if os.path.isfile(manifiesto):
            try:
                with open(manifiesto) as f:
                    resumen["entrega"]["archivos"] = len(json.load(f).get("archivos", []))
            except Exception:
                pass

    os.makedirs(OUTPUTS, exist_ok=True)
    with open(DESTINO, "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(DESTINO, 0o666)
    except OSError:
        pass

    # Copia en la carpeta de entrega: quien recibe los productos ve ahí mismo
    # con qué corrida salieron, sin acceso al directorio de trabajo.
    if export_dir and os.path.isdir(export_dir):
        try:
            copia = os.path.join(export_dir, "run_summary.json")
            shutil.copy2(DESTINO, copia)
            os.chmod(copia, 0o666)
        except OSError:
            pass

    print(f"✅ {DESTINO} ({len(resumen['productos'])} productos, "
          f"{len(resumen['vectores'])} vectores)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
