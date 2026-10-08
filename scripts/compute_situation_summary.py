#!/usr/bin/env python3
"""Resumen ejecutivo de la situación (outputs/situation.json) para el modo
simple del geovisor — números reales calculados acá, nunca inventados en
el navegador.

Puramente térmico: focos activos (temperatura ABSOLUTA, no depende de NDVI
ni de multiespectral — ver compute_thermal_hotspot.py), su ubicación real y
la confianza del dato (fracción del ortomosaico térmico con dato válido).

  1. Focos térmicos DISCRETOS: el hotspot es un raster continuo de clases —
     acá se etiquetan componentes conectados de la clase "foco activo"
     (>=88°C, ver compute_thermal_hotspot.py) y se calcula el centroide real
     en lat/lon de cada uno, más su temperatura pico.

  2. Confianza del dato: no es un número inventado para verse bien — es la
     fracción de la cobertura térmica que tiene dato válido (no nodata). Un
     área con muchos huecos de cobertura es, literalmente, menos confiable.

captura (fecha de vuelo) sale de flight_path.geojson: ese archivo lo escribe
export_flight_path.py del GPS/EXIF de las fotos ANTES de invocar a ODM, así
que existe para cualquier misión con al menos un sensor.

Uso: python3 scripts/compute_situation_summary.py
Lee: outputs/termico_hotspot_class.tif, outputs/thermal_orthomosaic.tif,
     outputs/flight_path.geojson
Escribe: outputs/situation.json (se omite si no hay térmico)
"""
import json
import os
import sys

import numpy as np
from osgeo import gdal, osr
from scipy import ndimage

gdal.UseExceptions()

HOT_PATH = "outputs/termico_hotspot_class.tif"
FLIGHT_PATH = "outputs/flight_path.geojson"
THERMAL_PATH = "outputs/thermal_orthomosaic.tif"
OUT_PATH = "outputs/situation.json"

# Componentes más chicos que esto son ruido de reconstrucción de un puñado
# de píxeles sueltos, no un foco real — a ~10cm/px (ortofoto térmica nativa
# de ODM), 9px ~ 0.09 m², un umbral conservador, no ajustado a mano contra
# ninguna misión particular.
MIN_HOTSPOT_PX = 9


def _hotspots_desde_clase(hot_path, gt, proj, temp_abs):
    """Componentes conectados de la clase 4 (foco activo)."""
    hot_ds = gdal.Open(hot_path)
    hot = hot_ds.GetRasterBand(1).ReadAsArray()
    hot_ds = None
    lbl, n_components = ndimage.label(hot == 4)
    raster_srs = osr.SpatialReference(wkt=proj)
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_wgs84 = osr.CoordinateTransformation(raster_srs, wgs84)
    hotspots = []
    for i in range(1, n_components + 1):
        ys, xs = np.where(lbl == i)
        if len(xs) < MIN_HOTSPOT_PX:
            continue
        cx, cy = float(xs.mean()), float(ys.mean())
        mx = gt[0] + cx * gt[1] + cy * gt[2]
        my = gt[3] + cx * gt[4] + cy * gt[5]
        lon, lat, _ = to_wgs84.TransformPoint(mx, my)
        peak_temp = float(temp_abs[ys, xs].max()) if temp_abs is not None else None
        hotspots.append({
            "lat": round(lat, 6), "lon": round(lon, 6), "px": int(len(xs)),
            "temp_c": round(peak_temp, 1) if peak_temp is not None else None,
        })
    hotspots.sort(key=lambda h: -(h["temp_c"] or 0))
    return hotspots


def _captura():
    """Fecha de vuelo real, del GPS/EXIF (no el mtime del archivo, que
    refleja cuándo CORRIÓ el pipeline)."""
    if not os.path.isfile(FLIGHT_PATH):
        return None
    with open(FLIGHT_PATH) as f:
        fp = json.load(f)
    times = [ft["properties"]["time"] for ft in fp.get("features", [])
             if ft["properties"].get("kind") == "capture" and ft["properties"].get("time")]
    return max(times) if times else None


def _resumen():
    ds = gdal.Open(THERMAL_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    temp_abs = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    valid = np.isfinite(temp_abs)

    hotspots = _hotspots_desde_clase(HOT_PATH, gt, proj, temp_abs)

    n_valid = int(valid.sum())
    confianza = "alta"
    cobertura_pct = 100.0
    if n_valid:
        # Qué fracción del ortomosaico térmico es dato real, no relleno. Se
        # guarda el número (no solo alta/media/baja) para que la UI pueda
        # explicar el motivo en vez de dejar la etiqueta sin contexto.
        valid_frac = 100 * n_valid / valid.size
        cobertura_pct = round(valid_frac, 0)
        confianza = "alta" if valid_frac >= 90 else ("media" if valid_frac >= 70 else "baja")

    temp_max = round(float(temp_abs[valid].max()), 1) if n_valid else None
    temp_promedio = round(float(temp_abs[valid].mean()), 1) if n_valid else None

    return {
        "hotspots_activos": len(hotspots),
        "hotspots": hotspots[:20],
        "confianza": confianza,
        "cobertura_pct": cobertura_pct,
        "temp_max": temp_max,
        "temp_promedio": temp_promedio,
    }


def main():
    if not (os.path.isfile(HOT_PATH) and os.path.isfile(THERMAL_PATH)):
        print("⚠ no hay hotspot térmico — omitiendo situation.json")
        return 0
    situation = _resumen()
    situation["captura"] = _captura()

    # Cobertura vs. área volada (scripts/compute_coverage.py): qué fracción
    # del área que el dron recorrió quedó cubierta por cada mosaico y si
    # disparó la alerta de cobertura baja — para que el panel de situación
    # del geovisor lo muestre sin pedir otro archivo.
    if os.path.isfile("outputs/coverage.json"):
        try:
            with open("outputs/coverage.json") as f:
                c = json.load(f)
            situation["cobertura_vs_area_volada"] = c.get("productos")
            situation["area_volada_km2"] = c.get("area_volada_km2")
            situation["alerta_cobertura_baja"] = bool(c.get("alerta"))
        except Exception:
            pass

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(situation, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(OUT_PATH, 0o666)
    except OSError:
        pass

    print(f"✅ {OUT_PATH}")
    print(f"  focos activos: {situation['hotspots_activos']} | "
          f"temp. máx: {situation['temp_max']}°C | confianza: {situation['confianza']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
