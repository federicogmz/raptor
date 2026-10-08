#!/usr/bin/env python3
"""Exporta los productos finales a una carpeta elegida por el usuario, en el
formato y el sistema de referencia que pida.

Es la ÚLTIMA etapa del pipeline y es OPCIONAL: sin EXPORT_DIR no hace nada
(cero impacto en las corridas que no la usan). No reemplaza a outputs/ — copia
desde ahí, así que el geovisor y cualquier reproceso siguen leyendo lo de
siempre; esto es la entrega para el que se lleva los datos a QGIS/ArcGIS.

Por qué existe: outputs/ queda en la UTM WGS84 que eligió ODM según el GPS del
vuelo, que no es la CRS en la que trabajan las entidades colombianas. EPSG:9377
(MAGNA-SIRGAS 2018 / Origen-Nacional) es el sistema único nacional adoptado por
el IGAC — reproyectar a mano cada producto en QGIS después de cada misión es
justo el trabajo repetitivo que este paso elimina.

Variables de entorno (todas opcionales; sin EXPORT_DIR no corre):
  EXPORT_DIR            carpeta destino
  EXPORT_PRODUCTS       claves separadas por coma (ver CATALOG); "all" = todo
  EXPORT_RASTER_FORMAT  cog (default) | gtiff
  EXPORT_VECTOR_FORMAT  geojson (default) | gpkg | shp | kml
  EXPORT_EPSG           código EPSG destino, o "source" para no reproyectar

Uso: python3 scripts/export_products.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from osgeo import gdal, osr

gdal.UseExceptions()

# Configurable para exportar a demanda una misión que NO es la que está
# symlinkeada ahora mismo a /app/outputs (ver webapp/main.py::export_mission):
# apunta directo a la carpeta outputs/ real de esa misión, así exportar una
# misión vieja no le mueve el piso a otra que se esté procesando o mirando en
# el geovisor al mismo tiempo. El pipeline normal (docker/entrypoint.sh)
# nunca fija esta variable, así que sigue usando "outputs" relativo a cwd,
# que es donde activate_mission() la tiene symlinkeada.
OUTPUTS = os.environ.get("RAPTOR_EXPORT_SOURCE_DIR", "outputs")

# clave -> (etiqueta, tipo, [rutas fuente])
#
# `discreto` marca los rásters de CLASES (hotspot, índices clasificados,
# máscara de confianza): se remuestrean con vecino más cercano, nunca
# promediando. Es el mismo criterio que ya aplica generate_tiles.py al
# teselarlos — promediar clases vecinas inventa una clase intermedia que no
# existe (un 2.5 entre "elevado" y "caliente" no significa nada).
CATALOG = {
    "rgb":           ("Ortomosaico RGB", "raster", [f"{OUTPUTS}/rgb_orthomosaic.tif"]),
    "thermal":       ("Ortomosaico térmico (°C)", "raster", [f"{OUTPUTS}/thermal_orthomosaic.tif"]),
    "dsm":           ("Modelo digital de superficie", "raster", [f"{OUTPUTS}/dsm.tif"]),
    "multispectral": ("Ortomosaico multiespectral", "raster", [f"{OUTPUTS}/multispectral_orthomosaic.tif"]),
    "indices":       ("Índices de vegetación", "raster",
                      [f"{OUTPUTS}/indices/{n}.tif" for n in ("ndvi", "gndvi", "ndre", "msavi2")]),
    "classes":       ("Hotspot e índices clasificados", "raster_discreto",
                      [f"{OUTPUTS}/termico_hotspot_class.tif"]
                      + [f"{OUTPUTS}/indices/{n}_class.tif" for n in ("ndvi", "gndvi", "ndre", "msavi2")]),
    "confidence":    ("Máscara de confianza", "raster_discreto", [f"{OUTPUTS}/confidence_mask.tif"]),
    "flight_path":   ("Ruta de vuelo", "vector", [f"{OUTPUTS}/flight_path.geojson"]),
    "situation":     ("Resumen de situación (JSON)", "copia", [f"{OUTPUTS}/situation.json"]),
    "pointclouds":   ("Nubes de puntos", "nube",
                      [f"{OUTPUTS}/point_cloud_{n}.copc.laz" for n in ("rgb", "thermal", "multispectral", "dband")]),
}

RASTER_FORMATS = {
    "cog":   ("COG", ".tif", ["COMPRESS=DEFLATE", "OVERVIEW_RESAMPLING=AVERAGE", "BIGTIFF=IF_SAFER"]),
    "gtiff": ("GTiff", ".tif", ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]),
}
VECTOR_FORMATS = {
    "geojson": ("GeoJSON", ".geojson"),
    "gpkg":    ("GPKG", ".gpkg"),
    "shp":     ("ESRI Shapefile", ".shp"),
    "kml":     ("KML", ".kml"),
}

PDAL_BIN = "/code/SuperBuild/install/bin/pdal"


def _srs_from_epsg(code):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(code))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _export_raster(src, dst_dir, fmt_key, epsg, discreto):
    drv, ext, copts = RASTER_FORMATS[fmt_key]
    dst = os.path.join(dst_dir, os.path.splitext(os.path.basename(src))[0] + ext)
    # Vecino más cercano para clases; cúbico para continuo (RGB/térmico/DSM/
    # índices), que da mejor resultado que bilineal al cambiar de grilla.
    resample = "near" if discreto else "cubic"
    # PREDICTOR según el tipo real del dato, mismo criterio que export_cog.py:
    # 3 para punto flotante, 2 para entero.
    ds = gdal.Open(src)
    dtype = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)
    ds = None
    predictor = "3" if dtype in ("Float32", "Float64") else "2"
    copts = copts + [f"PREDICTOR={predictor}"]

    # DOS PASOS a propósito: gdal.Warp NO puede escribir directo a COG. Ese
    # driver solo implementa CreateCopy(), no Create(), y el binding de Python
    # —a diferencia del ejecutable gdalwarp, que hace el temporal por dentro—
    # no lo suple: devuelve un GeoTIFF con el GEOTRANSFORM DESTRUIDO (origen
    # 0,0 y píxel 1.0) sin lanzar ningún error ni advertencia. Verificado con
    # GDAL 3.11.1. En una entrega eso es lo peor posible: un archivo que abre
    # bien y está en cualquier parte del planeta menos donde corresponde.
    # Se reproyecta a un GTiff temporal y recién ahí se convierte al formato
    # final con Translate (CreateCopy) — el mismo camino de dos pasos que ya
    # usa export_cog.py.
    tmp = None
    try:
        source = src
        if epsg is not None:
            fd, tmp = tempfile.mkstemp(suffix=".tif", dir=dst_dir)
            os.close(fd)
            warped = gdal.Warp(tmp, src, options=gdal.WarpOptions(
                format="GTiff", resampleAlg=resample, multithread=True,
                dstSRS=_srs_from_epsg(epsg),
                creationOptions=["TILED=YES", "BIGTIFF=IF_SAFER"]))
            warped = None
            source = tmp
        out = gdal.Translate(dst, source, options=gdal.TranslateOptions(
            format=drv, creationOptions=copts))
        out = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    return dst


def _export_vector(src, dst_dir, fmt_key, epsg):
    drv, ext = VECTOR_FORMATS[fmt_key]
    dst = os.path.join(dst_dir, os.path.splitext(os.path.basename(src))[0] + ext)
    opts = dict(format=drv)
    if fmt_key == "kml":
        # KML es WGS84 por especificación (OGC KML 2.2): cualquier otra CRS
        # produce un archivo que Google Earth interpreta mal, no un error.
        opts["dstSRS"] = _srs_from_epsg(4326)
    elif epsg is not None:
        opts["dstSRS"] = _srs_from_epsg(epsg)
    if os.path.exists(dst):
        os.remove(dst)
    gdal.VectorTranslate(dst, src, options=gdal.VectorTranslateOptions(**opts))
    return dst


def _export_pointcloud(src, dst_dir, epsg):
    dst = os.path.join(dst_dir, os.path.basename(src))
    if epsg is None:
        shutil.copy2(src, dst)
        return dst
    pdal = shutil.which("pdal") or (PDAL_BIN if os.path.isfile(PDAL_BIN) else None)
    if pdal is None:
        print(f"  ⚠ {os.path.basename(src)}: pdal no encontrado — se copia sin reproyectar")
        shutil.copy2(src, dst)
        return dst
    r = subprocess.run([pdal, "translate", src, dst, "reprojection",
                        f"--filters.reprojection.out_srs=EPSG:{epsg}", "--writer", "copc"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ⚠ {os.path.basename(src)}: no se pudo reproyectar ({r.stderr.strip()[:160]}) "
              f"— se copia en la CRS original")
        shutil.copy2(src, dst)
    return dst


def main():
    export_dir = os.environ.get("EXPORT_DIR", "").strip()
    if not export_dir:
        print("  (sin EXPORT_DIR — no se exporta nada)")
        return 0

    requested = os.environ.get("EXPORT_PRODUCTS", "all").strip()
    keys = list(CATALOG) if requested in ("", "all") else [
        k.strip() for k in requested.split(",") if k.strip()]
    desconocidas = [k for k in keys if k not in CATALOG]
    if desconocidas:
        print(f"❌ ERROR: productos desconocidos: {desconocidas}")
        print(f"   Válidos: {list(CATALOG)}")
        return 1

    raster_fmt = os.environ.get("EXPORT_RASTER_FORMAT", "cog").strip().lower() or "cog"
    vector_fmt = os.environ.get("EXPORT_VECTOR_FORMAT", "geojson").strip().lower() or "geojson"
    if raster_fmt not in RASTER_FORMATS:
        print(f"❌ ERROR: formato ráster '{raster_fmt}' inválido. Válidos: {list(RASTER_FORMATS)}")
        return 1
    if vector_fmt not in VECTOR_FORMATS:
        print(f"❌ ERROR: formato vectorial '{vector_fmt}' inválido. Válidos: {list(VECTOR_FORMATS)}")
        return 1

    epsg_raw = os.environ.get("EXPORT_EPSG", "").strip().lower()
    epsg = None
    if epsg_raw and epsg_raw != "source":
        try:
            epsg = int(epsg_raw)
            _srs_from_epsg(epsg)          # falla acá si el código no existe
        except Exception as exc:
            print(f"❌ ERROR: EPSG '{epsg_raw}' inválido o desconocido: {exc}")
            return 1

    try:
        os.makedirs(export_dir, exist_ok=True)
    except OSError as exc:
        print(f"❌ ERROR: no se pudo crear la carpeta de exportación '{export_dir}': {exc}")
        return 1

    crs_label = f"EPSG:{epsg} ({_srs_from_epsg(epsg).GetName()})" if epsg else "la de la misión (sin reproyectar)"
    print(f"  destino : {export_dir}")
    print(f"  CRS     : {crs_label}")
    print(f"  formatos: ráster={raster_fmt} · vectorial={vector_fmt}")

    manifest = {"crs": f"EPSG:{epsg}" if epsg else "origen", "raster_format": raster_fmt,
                "vector_format": vector_fmt, "archivos": []}
    n_ok = n_skip = 0
    for key in keys:
        label, kind, sources = CATALOG[key]
        presentes = [s for s in sources if os.path.isfile(s)]
        if not presentes:
            print(f"  – {label}: no generado en esta misión, omitido")
            n_skip += 1
            continue
        for src in presentes:
            try:
                if kind in ("raster", "raster_discreto"):
                    dst = _export_raster(src, export_dir, raster_fmt, epsg,
                                         discreto=(kind == "raster_discreto"))
                elif kind == "vector":
                    dst = _export_vector(src, export_dir, vector_fmt, epsg)
                elif kind == "nube":
                    dst = _export_pointcloud(src, export_dir, epsg)
                else:
                    dst = os.path.join(export_dir, os.path.basename(src))
                    shutil.copy2(src, dst)
            except Exception as exc:
                print(f"  ❌ {os.path.basename(src)}: {exc}")
                return 1
            size_mb = os.path.getsize(dst) / 1e6
            print(f"  ✅ {os.path.basename(dst)}  ({size_mb:.1f} MB)  ← {label}")
            manifest["archivos"].append({"producto": key, "etiqueta": label,
                                         "archivo": os.path.basename(dst),
                                         "origen": src, "mb": round(size_mb, 1)})
            n_ok += 1

    with open(os.path.join(export_dir, "export_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # El contenedor corre como root: sin esto la entrega queda ilegible desde el
    # host (mismo motivo que el chmod final de docker/entrypoint.sh).
    for root, dirs, files in os.walk(export_dir):
        for p in dirs + files:
            try:
                os.chmod(os.path.join(root, p), 0o666 if p in files else 0o777)
            except OSError:
                pass

    print(f"\n✅ {n_ok} archivo(s) exportado(s) a {export_dir}"
          + (f" ({n_skip} producto(s) no disponibles en esta misión)" if n_skip else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
