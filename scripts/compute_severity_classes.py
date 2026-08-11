#!/usr/bin/env python3
"""Clasifica severidad (multiespectral), hotspot térmico e índices de
vegetación dentro del polígono de área afectada ya detectado
(detect_area_afectada.py) — NO sobre la misión completa, para no mostrar
ruido disperso (vías, cultivos, suelo al sol) como si fuera parte del
incendio.

Todos los cortes de clase están tomados de literatura/convención estándar,
no ajustados a mano contra este dataset:

  SEVERIDAD (z-score robusto de brillo multiespectral vs. vegetación sana
  de la misma misión): cortes en 1/2/3 sigma — regla empírica 68-95-99.7
  de control estadístico de procesos (anomaly detection estándar), NO un
  número tanteado.
    1=isla no quemada(z<1) 2=leve(1-2) 3=moderado(2-3) 4=severo(z>=3)
  La clase 1 SOLO aparece dentro del polígono (fuera se recorta a nodata,
  ver más abajo) — no es "no afectado" en sentido amplio, es terreno SIN
  anomalía espectral encerrado por el perímetro del incendio: islas reales
  sin quemar (roca, claro, vegetación muy húmeda) o huecos topológicos que
  detect_area_afectada.py rellena al cerrar el polígono. Llamarla "no
  afectado" es engañoso para un tomador de decisiones — dentro del
  perímetro, por definición, todo es "área afectada" en algún grado.

  HOTSPOT TÉRMICO (temperatura ABSOLUTA, no anomalía relativa — ver
  detect_area_afectada.py para por qué): la literatura operacional de
  detección de hotspots con drones en incendios usa 190-250°F (~88-121°C)
  como umbral de "fuego activo bajo superficie" (predetermined threshold
  para mop-up/residual heat monitoring). Se usa el extremo bajo de ese
  rango (88°C) como corte de "foco activo".
    1=normal(<40°C) 2=elevado(40-60°C) 3=caliente(60-88°C) 4=foco activo(>=88°C, umbral operacional citado)
  Fuente: literatura de UAV thermal imaging para wildfire hotspot/mop-up
  detection (predetermined threshold 190-250°F).

  ÍNDICES DE VEGETACIÓN (NDVI/GNDVI/NDRE/MSAVI2): cortes estándar de
  teledetección agrícola/forestal, no inventados:
    NDVI  (USGS):  1=sin vegetación(<0.1) 2=escasa/estresada(0.1-0.6) 3=densa y sana(>=0.6)
    GNDVI:         1=estrés severo(<0.3) 2=moderada/estresada(0.3-0.5) 3=sana(>=0.5)
    NDRE:          1=deficiencia N(<0.2) 2=transición(0.2-0.3) 3=saludable(0.3-0.6) 4=óptimo/maduro(>=0.6)
    MSAVI2:        mismos cortes que NDVI (0.1/0.6) — a diferencia de NDVI/
                   GNDVI/NDRE, MSAVI2 no tiene una convención de cortes tan
                   establecida en la literatura; se reusan los de NDVI como
                   punto de partida razonable (MSAVI2 se diseñó para leer en
                   la misma escala 0-1 que NDVI, solo corregido por brillo
                   de suelo — converge con NDVI en dosel denso, diverge
                   justo en dosel disperso, que es donde importa). Ver
                   compute_vegetation_indices.py para la justificación de
                   por qué se agrega.

Uso: python3 scripts/compute_severity_classes.py
Lee: outputs/_deteccion_data.npz (de detect_area_afectada.py — correr ese primero)
     outputs/area_afectada.geojson, outputs/indices/{ndvi,gndvi,ndre,msavi2}.tif
Escribe: outputs/severidad_class.tif, outputs/termico_hotspot_class.tif,
         outputs/indices/{ndvi,gndvi,ndre,msavi2}_class.tif
"""
import os
import sys
import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()

MS_PATH = "outputs/multispectral_orthomosaic.tif"
CACHE_PATH = "outputs/_deteccion_data.npz"
POLY_PATH = "outputs/area_afectada.geojson"
OUT_DIR = "outputs"
INDICES_DIR = "outputs/indices"

SEV_BREAKS = [1.0, 2.0, 3.0]      # sigma — regla 68-95-99.7
TERM_BREAKS = [40.0, 60.0, 88.0]  # °C absolutos — ver docstring

# (breaks, nombres) por índice — límites de literatura, ver docstring
INDEX_SCHEMES = {
    "ndvi": ([0.1, 0.6], ["sin vegetación", "escasa/estresada", "densa y sana"]),
    "gndvi": ([0.3, 0.5], ["estrés severo", "moderada/estresada", "sana"]),
    "ndre": ([0.2, 0.3, 0.6], ["deficiencia N", "transición", "saludable", "óptimo/maduro"]),
    "msavi2": ([0.1, 0.6], ["sin vegetación", "escasa/estresada", "densa y sana"]),
}


def classify(values, breaks, valid):
    cls = np.zeros(values.shape, dtype=np.uint8)
    cls[valid & (values < breaks[0])] = 1
    for i in range(len(breaks) - 1):
        cls[valid & (values >= breaks[i]) & (values < breaks[i + 1])] = i + 2
    cls[valid & (values >= breaks[-1])] = len(breaks) + 1
    return cls


def write_class_tif(path, arr, gt, proj):
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Byte, ["COMPRESS=LZW", "TILED=YES"])
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    out.GetRasterBand(1).WriteArray(arr)
    out.GetRasterBand(1).SetNoDataValue(0)
    out = None
    print(f"✅ {path}")


def main():
    if not os.path.isfile(CACHE_PATH):
        print(f"❌ ERROR: corre scripts/detect_area_afectada.py primero")
        sys.exit(1)
    if not os.path.isfile(POLY_PATH):
        # CACHE_PATH existe pero POLY_PATH no: detect_area_afectada.py SÍ
        # corrió, solo que no encontró ningún píxel que superara el umbral
        # — resultado válido (misión sin área afectada detectable), no un
        # prerrequisito faltante. Nada que clasificar.
        print("  (sin área afectada detectada — nada que clasificar)")
        return

    c = np.load(CACHE_PATH)
    z_severidad, temp_abs, valid = c["z_severidad"], c["temp_abs"], c["valid"]

    ms_ds = gdal.Open(MS_PATH)
    gt, proj = ms_ds.GetGeoTransform(), ms_ds.GetProjection()
    W, H = ms_ds.RasterXSize, ms_ds.RasterYSize

    # area_afectada.geojson se escribe en EPSG:4326 (ver detect_area_afectada.py
    # — Leaflet ignora "crs" y asume siempre WGS84) pero la grilla del
    # multiespectral está en la UTM de la misión: hay que reproyectar cada
    # feature antes de rasterizar, RasterizeLayer no transforma coordenadas
    # solo, asume que ya están en la SRS del raster destino.
    fire_ds = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    fire_ds.SetGeoTransform(gt)
    fire_ds.SetProjection(proj)
    poly_ds = ogr.Open(POLY_PATH)
    poly_layer = poly_ds.GetLayer()
    raster_srs = osr.SpatialReference(wkt=proj)
    to_raster_srs = osr.CoordinateTransformation(poly_layer.GetSpatialRef(), raster_srs)
    mem_ds = ogr.GetDriverByName("MEM").CreateDataSource("fire_mem")
    mem_layer = mem_ds.CreateLayer("fire", raster_srs, ogr.wkbMultiPolygon)
    for feat in poly_layer:
        g = feat.GetGeometryRef().Clone()
        g.Transform(to_raster_srs)
        out_feat = ogr.Feature(mem_layer.GetLayerDefn())
        out_feat.SetGeometry(g)
        mem_layer.CreateFeature(out_feat)
    gdal.RasterizeLayer(fire_ds, [1], mem_layer, burn_values=[1])
    fire_mask = fire_ds.GetRasterBand(1).ReadAsArray().astype(bool)
    n_fire = fire_mask.sum()
    print(f"Área del polígono: {n_fire} px")

    sev = classify(z_severidad, SEV_BREAKS, valid)
    thc = classify(temp_abs, TERM_BREAKS, valid)
    sev[~fire_mask] = 0
    thc[~fire_mask] = 0

    for name, cls, labels in [("severidad", sev, ["isla no quemada", "leve", "moderado", "severo"]),
                               ("térmico", thc, ["normal", "elevado", "caliente", "foco activo"])]:
        for i, lbl in enumerate(labels, 1):
            n = (cls == i).sum()
            pct = 100 * n / n_fire if n_fire else 0
            print(f"  {name} {lbl}: {n} px ({pct:.1f}%)")

    write_class_tif(os.path.join(OUT_DIR, "severidad_class.tif"), sev, gt, proj)
    write_class_tif(os.path.join(OUT_DIR, "termico_hotspot_class.tif"), thc, gt, proj)

    # Índices de vegetación: clasificados con cortes estándar, sobre TODA
    # la misión (no recortados al polígono — el sentido es ver el estado
    # de vegetación en general, no solo dentro del incendio).
    for name, (breaks, labels) in INDEX_SCHEMES.items():
        idx_path = os.path.join(INDICES_DIR, f"{name}.tif")
        if not os.path.isfile(idx_path):
            print(f"  ⚠ {idx_path} no existe, omitiendo clasificación de {name}")
            continue
        idx_ds = gdal.Open(idx_path)
        idx_gt = idx_ds.GetGeoTransform()
        vals = idx_ds.GetRasterBand(1).ReadAsArray()
        idx_valid = np.isfinite(vals)
        idx_cls = classify(vals, breaks, idx_valid)
        for i, lbl in enumerate(labels, 1):
            n = (idx_cls == i).sum()
            print(f"  {name} {lbl}: {n} px")
        write_class_tif(os.path.join(INDICES_DIR, f"{name}_class.tif"), idx_cls, idx_gt, idx_ds.GetProjection())


if __name__ == "__main__":
    main()
