#!/usr/bin/env python3
"""Resumen ejecutivo de la situación (outputs/situation.json) para el modo
simple del geovisor — números reales calculados acá, nunca inventados en
el navegador.

Reusa lo que el pipeline ya calculó (severidad_class.tif,
termico_hotspot_class.tif, area_afectada.geojson, _deteccion_data.npz,
flight_path.geojson) y agrega dos cómputos que antes NO existían:

  1. Focos térmicos DISCRETOS: severidad/hotspot son rasters continuos —
     antes no había forma de decir "hay 3 focos" ni de darle a cada uno una
     coordenada para centrar el mapa o listarlo en la vista accesible. Acá
     se etiquetan componentes conectados de la clase "foco activo" (>=88°C,
     ver compute_severity_classes.py) y se calcula el centroide real en
     lat/lon de cada uno, más su temperatura pico.

  2. Confianza del dato: no es un número inventado para verse bien — es la
     fracción del PERÍMETRO DETECTADO que tiene dato multiespectral+térmico
     válido (no nodata). Un perímetro con muchos huecos de cobertura es,
     literalmente, menos confiable: cualquier severidad/hotspot calculado
     ahí se apoya en menos píxeles reales.

Uso: python3 scripts/compute_situation_summary.py
Lee: outputs/severidad_class.tif, outputs/termico_hotspot_class.tif,
     outputs/area_afectada.geojson, outputs/_deteccion_data.npz,
     outputs/indices/ndvi_class.tif, outputs/flight_path.geojson
Escribe: outputs/situation.json (omite el archivo si la misión no tiene
     multiespectral+térmico — no hay severidad/focos que resumir)
"""
import json
import os
import sys

import numpy as np
from osgeo import gdal, osr
from scipy import ndimage

gdal.UseExceptions()

CACHE_PATH = "outputs/_deteccion_data.npz"
AREA_PATH = "outputs/area_afectada.geojson"
SEV_PATH = "outputs/severidad_class.tif"
HOT_PATH = "outputs/termico_hotspot_class.tif"
NDVI_CLASS_PATH = "outputs/indices/ndvi_class.tif"
FLIGHT_PATH = "outputs/flight_path.geojson"
OUT_PATH = "outputs/situation.json"

# Componentes más chicos que esto son ruido de reconstrucción de un puñado
# de píxeles sueltos, no un foco real — a ~10cm/px (ortofoto térmica nativa
# de ODM), 9px ~ 0.09 m², un umbral conservador, no ajustado a mano contra
# ninguna misión particular.
MIN_HOTSPOT_PX = 9


def main():
    if not os.path.isfile(SEV_PATH) or not os.path.isfile(AREA_PATH):
        print("⚠ no hay severidad/área afectada (misión sin multiespectral+térmico) — "
              "omitiendo situation.json")
        return 0

    with open(AREA_PATH) as f:
        area_geo = json.load(f)
    feats = area_geo.get("features", [])
    area_m2 = feats[0]["properties"].get("area_m2", 0.0) if feats else 0.0
    area_ha = area_m2 / 10000

    sev_ds = gdal.Open(SEV_PATH)
    sev = sev_ds.GetRasterBand(1).ReadAsArray()
    gt, proj = sev_ds.GetGeoTransform(), sev_ds.GetProjection()
    fire_mask = sev > 0
    n_fire = int(fire_mask.sum())

    # Severidad: 1=isla no quemada 2=leve 3=moderado 4=severo (ver
    # compute_severity_classes.py). Para el resumen de 3 franjas del mockup,
    # "isla no quemada" se agrupa con "leve" — sigue siendo dentro del
    # perímetro del incendio, sin anomalía espectral detectada ahí.
    sev_counts = {c: int((sev == c).sum()) for c in (1, 2, 3, 4)}
    pct = lambda n: round(100 * n / n_fire, 0) if n_fire else 0.0
    leve_pct = pct(sev_counts[1] + sev_counts[2])
    moderado_pct = pct(sev_counts[3])
    severo_pct = pct(sev_counts[4])
    dominante = max((("leve", leve_pct), ("moderado", moderado_pct), ("severo", severo_pct)),
                    key=lambda x: x[1])[0]

    # Focos térmicos discretos: componentes conectados de clase 4.
    hotspots = []
    if os.path.isfile(HOT_PATH):
        hot_ds = gdal.Open(HOT_PATH)
        hot = hot_ds.GetRasterBand(1).ReadAsArray()
        lbl, n_components = ndimage.label(hot == 4)
        raster_srs = osr.SpatialReference(wkt=proj)
        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        to_wgs84 = osr.CoordinateTransformation(raster_srs, wgs84)
        temp_abs = np.load(CACHE_PATH)["temp_abs"] if os.path.isfile(CACHE_PATH) else None
        for i in range(1, n_components + 1):
            ys, xs = np.where(lbl == i)
            if len(xs) < MIN_HOTSPOT_PX:
                continue
            cx, cy = float(xs.mean()), float(ys.mean())
            mx = gt[0] + cx * gt[1] + cy * gt[2]
            my = gt[3] + cx * gt[4] + cy * gt[5]
            lon, lat, _ = to_wgs84.TransformPoint(mx, my)
            peak_temp = float(temp_abs[ys, xs].max()) if temp_abs is not None else None
            sev_here = int(sev[ys, xs].max())
            hotspots.append({
                "lat": round(lat, 6), "lon": round(lon, 6), "px": int(len(xs)),
                "temp_c": round(peak_temp, 1) if peak_temp is not None else None,
                "severidad": {1: "leve", 2: "leve", 3: "moderado", 4: "severo"}.get(sev_here, "leve"),
            })
        hotspots.sort(key=lambda h: -(h["temp_c"] or 0))

    # Vegetación comprometida: % del perímetro con NDVI clasificado como
    # "sin vegetación" o "escasa/estresada" (clases 1-2, ver
    # compute_severity_classes.py). Requiere la misma grilla que severidad
    # (ambos derivan del mismo ortomosaico multiespectral) — si no coincide,
    # se omite en vez de forzar una comparación píxel a píxel inválida.
    veg_pct = None
    if os.path.isfile(NDVI_CLASS_PATH):
        ndvi_ds = gdal.Open(NDVI_CLASS_PATH)  # variable propia: sin esto, el Dataset
        ndvi_cls = ndvi_ds.GetRasterBand(1).ReadAsArray()  # anónimo se recolecta como
        del ndvi_ds  # basura antes de leer la banda (Band depende del Dataset vivo)
        if ndvi_cls.shape == sev.shape:
            compromised = np.isin(ndvi_cls, [1, 2]) & fire_mask
            veg_pct = pct(int(compromised.sum()))

    # Confianza: fracción del perímetro con dato válido (no nodata) en la
    # fuente multiespectral+térmica usada para severidad — no una cifra de
    # relleno.
    confianza = "alta"
    if os.path.isfile(CACHE_PATH):
        valid = np.load(CACHE_PATH)["valid"]
        if valid.shape == fire_mask.shape and n_fire:
            valid_frac = 100 * (valid & fire_mask).sum() / n_fire
            confianza = "alta" if valid_frac >= 90 else ("media" if valid_frac >= 70 else "baja")

    # Fecha de captura real: la más reciente del GPS/EXIF de las fotos (no
    # el mtime del archivo, que refleja cuándo CORRIÓ el pipeline, no
    # cuándo se voló la misión).
    captura = None
    if os.path.isfile(FLIGHT_PATH):
        with open(FLIGHT_PATH) as f:
            fp = json.load(f)
        times = [ft["properties"]["time"] for ft in fp.get("features", [])
                 if ft["properties"].get("kind") == "capture" and ft["properties"].get("time")]
        if times:
            captura = max(times)

    situation = {
        "area_ha": round(area_ha, 1),
        "severidad": {"leve_pct": leve_pct, "moderado_pct": moderado_pct,
                      "severo_pct": severo_pct, "dominante": dominante},
        "hotspots_activos": len(hotspots),
        "hotspots": hotspots[:20],
        "vegetacion_comprometida_pct": veg_pct,
        "confianza": confianza,
        "captura": captura,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(situation, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(OUT_PATH, 0o666)
    except OSError:
        pass
    print(f"✅ {OUT_PATH}")
    print(f"  área: {situation['area_ha']} ha | severidad dominante: {dominante} | "
          f"focos activos: {len(hotspots)} | confianza: {confianza}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
