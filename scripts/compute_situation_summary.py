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
     fracción del área relevante (el perímetro detectado, o toda la
     cobertura térmica si no hay perímetro) que tiene dato válido (no
     nodata). Un área con muchos huecos de cobertura es, literalmente,
     menos confiable: cualquier severidad/hotspot calculado ahí se apoya en
     menos píxeles reales.

DOS MODOS, según qué haya generado el pipeline:
  - CON multiespectral+térmico (severidad_class.tif + area_afectada.geojson
    existen): el resumen completo — área afectada, severidad, focos,
    vegetación comprometida, confianza sobre el perímetro detectado.
  - SOLO térmico (sin multiespectral: no hay NDVI para detectar un
    perímetro, ver detect_area_afectada.py): el resumen se recorta a lo que
    el térmico solo puede decir — focos activos con su temperatura y
    ubicación, confianza sobre la cobertura térmica completa. área_ha,
    severidad y vegetación quedan en None — pedir esas cifras sin
    multiespectral sería inventarlas.
  Sin NINGUNO de los dos (ni siquiera hotspot térmico), no hay nada que
  resumir y se omite el archivo, igual que antes.

captura (fecha de vuelo) sale de flight_path.geojson en los dos modos: ese
archivo lo escribe export_flight_path.py del GPS/EXIF de las fotos ANTES de
invocar a ODM, así que existe para cualquier misión con al menos un sensor,
independientemente de si terminó teniendo área/severidad o no.

Uso: python3 scripts/compute_situation_summary.py
Lee: outputs/severidad_class.tif, outputs/termico_hotspot_class.tif,
     outputs/area_afectada.geojson, outputs/_deteccion_data.npz,
     outputs/indices/ndvi_class.tif, outputs/flight_path.geojson,
     outputs/thermal_orthomosaic.tif
Escribe: outputs/situation.json (omite el archivo si no hay ni
     severidad+área ni hotspot térmico que resumir)
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
THERMAL_PATH = "outputs/thermal_orthomosaic.tif"
OUT_PATH = "outputs/situation.json"

# Componentes más chicos que esto son ruido de reconstrucción de un puñado
# de píxeles sueltos, no un foco real — a ~10cm/px (ortofoto térmica nativa
# de ODM), 9px ~ 0.09 m², un umbral conservador, no ajustado a mano contra
# ninguna misión particular.
MIN_HOTSPOT_PX = 9


def _hotspots_desde_clase(hot_path, gt, proj, temp_abs, sev=None):
    """Componentes conectados de la clase 4 (foco activo) — mismo cómputo
    para los dos modos, solo cambia de dónde sale `temp_abs` y si hay `sev`
    (severidad) para anotar cada foco."""
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
        sev_here = int(sev[ys, xs].max()) if sev is not None else None
        hotspots.append({
            "lat": round(lat, 6), "lon": round(lon, 6), "px": int(len(xs)),
            "temp_c": round(peak_temp, 1) if peak_temp is not None else None,
            "severidad": ({1: "leve", 2: "leve", 3: "moderado", 4: "severo"}.get(sev_here, "leve")
                         if sev_here is not None else None),
        })
    hotspots.sort(key=lambda h: -(h["temp_c"] or 0))
    return hotspots


def _captura():
    """Fecha de vuelo real, del GPS/EXIF (no el mtime del archivo, que
    refleja cuándo CORRIÓ el pipeline). Existe con cualquier combinación de
    sensores — no depende de multiespectral."""
    if not os.path.isfile(FLIGHT_PATH):
        return None
    with open(FLIGHT_PATH) as f:
        fp = json.load(f)
    times = [ft["properties"]["time"] for ft in fp.get("features", [])
             if ft["properties"].get("kind") == "capture" and ft["properties"].get("time")]
    return max(times) if times else None


def _resumen_completo():
    """Misión CON multiespectral+térmico: área afectada, severidad,
    vegetación comprometida y focos, todo recortado al perímetro
    detectado."""
    with open(AREA_PATH) as f:
        area_geo = json.load(f)
    feats = area_geo.get("features", [])
    area_m2 = feats[0]["properties"].get("area_m2", 0.0) if feats else 0.0
    area_ha = area_m2 / 10000

    sev_ds = gdal.Open(SEV_PATH)
    sev = sev_ds.GetRasterBand(1).ReadAsArray()
    gt, proj = sev_ds.GetGeoTransform(), sev_ds.GetProjection()
    sev_ds = None
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

    hotspots = []
    if os.path.isfile(HOT_PATH):
        temp_abs = np.load(CACHE_PATH)["temp_abs"] if os.path.isfile(CACHE_PATH) else None
        hotspots = _hotspots_desde_clase(HOT_PATH, gt, proj, temp_abs, sev=sev)

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

    return {
        "area_ha": round(area_ha, 1),
        "severidad": {"leve_pct": leve_pct, "moderado_pct": moderado_pct,
                      "severo_pct": severo_pct, "dominante": dominante},
        "hotspots_activos": len(hotspots),
        "hotspots": hotspots[:20],
        "vegetacion_comprometida_pct": veg_pct,
        "confianza": confianza,
        "solo_termico": False,
    }


def _resumen_solo_termico():
    """Misión SIN multiespectral (o sin que el área/severidad haya podido
    calcularse): lo único que el térmico por sí solo puede decir — focos
    activos, temperatura y ubicación, y una confianza basada en cuánta
    cobertura térmica es dato real. área_ha/severidad/vegetación quedan en
    None: pedirlas sin NDVI sería inventarlas (ver detect_area_afectada.py,
    que exige multiespectral porque su señal primaria ES el NDVI)."""
    ds = gdal.Open(THERMAL_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    temp_abs = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    valid = np.isfinite(temp_abs)

    hotspots = _hotspots_desde_clase(HOT_PATH, gt, proj, temp_abs)

    n_valid = int(valid.sum())
    confianza = "alta"
    if n_valid:
        # Sin perímetro detectado (no hay multiespectral), la confianza se
        # mide sobre TODA la cobertura térmica en vez de sobre un perímetro:
        # qué fracción del ortomosaico térmico es dato real, no relleno.
        valid_frac = 100 * n_valid / valid.size
        confianza = "alta" if valid_frac >= 90 else ("media" if valid_frac >= 70 else "baja")

    return {
        "area_ha": None,
        "severidad": None,
        "hotspots_activos": len(hotspots),
        "hotspots": hotspots[:20],
        "vegetacion_comprometida_pct": None,
        "confianza": confianza,
        "solo_termico": True,
    }


def main():
    tiene_area = os.path.isfile(SEV_PATH) and os.path.isfile(AREA_PATH)
    tiene_hotspot_solo = os.path.isfile(HOT_PATH) and os.path.isfile(THERMAL_PATH)
    if not tiene_area and not tiene_hotspot_solo:
        print("⚠ no hay severidad/área afectada ni hotspot térmico — omitiendo situation.json")
        return 0

    situation = _resumen_completo() if tiene_area else _resumen_solo_termico()
    situation["captura"] = _captura()

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(situation, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(OUT_PATH, 0o666)
    except OSError:
        pass

    print(f"✅ {OUT_PATH}")
    if situation["solo_termico"]:
        print(f"  SOLO térmico (sin multiespectral) | focos activos: "
              f"{situation['hotspots_activos']} | confianza: {situation['confianza']}")
    else:
        print(f"  área: {situation['area_ha']} ha | severidad dominante: "
              f"{situation['severidad']['dominante']} | focos activos: "
              f"{situation['hotspots_activos']} | confianza: {situation['confianza']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
