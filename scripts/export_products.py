#!/usr/bin/env python3
"""Exporta los productos finales a una carpeta elegida por el usuario, en el
formato y el sistema de referencia que pida.

Es la ÚLTIMA etapa del pipeline y es OPCIONAL: sin EXPORT_DIR no hace nada
(cero impacto en las corridas que no la usan). No reemplaza a outputs/ — copia
desde ahí, así que el geovisor y cualquier reproceso siguen leyendo lo de
siempre; esto es la entrega para el que se lleva los datos a QGIS/ArcGIS.

Variables de entorno (todas opcionales; sin EXPORT_DIR no corre):
  EXPORT_DIR             carpeta destino
  EXPORT_PRODUCTS        claves separadas por coma (ver CATALOG); "all" = todo
  EXPORT_RASTER_FORMAT   cog (default) | gtiff
  EXPORT_VECTOR_FORMAT   geojson (default) | gpkg | shp | kml
  EXPORT_EPSG            código EPSG destino, o "source" para no reproyectar
  EXPORT_VERTICAL_DATUM  ellipsoidal (default) | egm96 | geoid:<ruta> | offset:<m>

Uso: python3 scripts/export_products.py
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import numpy as np
    from osgeo import gdal, osr

    gdal.UseExceptions()
except ImportError:
    np = None
    gdal = None
    osr = None

OUTPUTS = os.environ.get("RAPTOR_EXPORT_SOURCE_DIR", "outputs")

CATALOG = {
    "rgb": ("Ortomosaico RGB", "raster", [f"{OUTPUTS}/rgb_orthomosaic.tif"]),
    "thermal": ("Ortomosaico térmico (°C)", "raster", [f"{OUTPUTS}/thermal_orthomosaic.tif"]),
    "dsm": ("Modelo digital de superficie", "raster", [f"{OUTPUTS}/dsm.tif"]),
    "multispectral": ("Ortomosaico multiespectral", "raster", [f"{OUTPUTS}/multispectral_orthomosaic.tif"]),
    "indices": (
        "Índices de vegetación",
        "raster",
        [f"{OUTPUTS}/indices/{n}.tif" for n in ("ndvi", "gndvi", "ndre", "msavi2")],
    ),
    "classes": (
        "Hotspot e índices clasificados",
        "raster_discreto",
        [f"{OUTPUTS}/termico_hotspot_class.tif"]
        + [f"{OUTPUTS}/indices/{n}_class.tif" for n in ("ndvi", "gndvi", "ndre", "msavi2")],
    ),
    "confidence": ("Máscara de confianza", "raster_discreto", [f"{OUTPUTS}/confidence_mask.tif"]),
    "area": (
        "Área afectada delimitada",
        "vector",
        [f"{OUTPUTS}/area_afectada_experto.geojson", f"{OUTPUTS}/area_afectada.geojson"],
    ),
    "flight_path": ("Ruta de vuelo", "vector", [f"{OUTPUTS}/flight_path.geojson"]),
    "situation": ("Resumen de situación (JSON)", "copia", [f"{OUTPUTS}/situation.json"]),
    "pointclouds": (
        "Nubes de puntos",
        "nube",
        [f"{OUTPUTS}/point_cloud_{n}.copc.laz" for n in ("rgb", "thermal", "multispectral", "dband")],
    ),
}

RASTER_FORMATS = {
    "cog": ("COG", ".tif", ["COMPRESS=DEFLATE", "OVERVIEW_RESAMPLING=AVERAGE", "BIGTIFF=IF_SAFER"]),
    "gtiff": ("GTiff", ".tif", ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]),
}
VECTOR_FORMATS = {
    "geojson": ("GeoJSON", ".geojson"),
    "gpkg": ("GPKG", ".gpkg"),
    "shp": ("ESRI Shapefile", ".shp"),
    "kml": ("KML", ".kml"),
}

PDAL_BIN = "/code/SuperBuild/install/bin/pdal"


def _srs_from_epsg(code):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(code))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _find_pdal():
    return shutil.which("pdal") or (PDAL_BIN if os.path.isfile(PDAL_BIN) else None)


def _find_egm96_grid():
    """Localiza una grilla EGM96 real instalada en el entorno."""
    candidatos = [
        os.environ.get("RAPTOR_EGM96_GRID", ""),
        "/usr/share/proj/us_nga_egm96_15.tif",
        "/usr/share/proj/egm96_15.gtx",
        "/code/SuperBuild/install/share/proj/us_nga_egm96_15.tif",
        "/code/SuperBuild/install/share/proj/egm96_15.gtx",
    ]
    try:
        import pyproj

        pdir = pyproj.datadir.get_data_dir()
        candidatos.extend([os.path.join(pdir, "us_nga_egm96_15.tif"), os.path.join(pdir, "egm96_15.gtx")])
    except Exception:
        pass
    for c in candidatos:
        if c and os.path.isfile(c):
            return c
    return None


def parse_vertical_datum(raw):
    """Valida y resuelve `EXPORT_VERTICAL_DATUM`.

    Acepta: `""` / `"ellipsoidal"` / `"source"`, `"egm96"`, `"geoid:<ruta>"`,
    `"offset:<metros>"`. Lanza ValueError si es inválido o falta la grilla.
    """
    val = (raw or "").strip()
    low = val.lower()
    if not low or low in ("ellipsoidal", "source", "wgs84"):
        return {"kind": "ellipsoidal", "label": "ellipsoidal", "grid": None, "offset": 0.0}
    if low == "egm96":
        grid = _find_egm96_grid()
        if not grid:
            raise ValueError(
                "EXPORT_VERTICAL_DATUM=egm96 requiere una grilla EGM96 (egm96_15.gtx / us_nga_egm96_15.tif) "
                "que no está disponible en /usr/share/proj ni en RAPTOR_EGM96_GRID."
            )
        return {"kind": "egm96", "label": "egm96", "grid": grid, "offset": 0.0}
    if low.startswith("geoid:"):
        grid_path = val[6:].strip()
        if not grid_path or not os.path.isfile(grid_path):
            raise ValueError(f"Grilla de geoide no encontrada: '{grid_path}'.")
        if gdal is not None:
            ds = gdal.Open(grid_path)
            if ds is None:
                raise ValueError(f"GDAL no pudo abrir la grilla de geoide: '{grid_path}'.")
            ds = None
        return {
            "kind": "geoid",
            "label": f"geoid:{os.path.basename(grid_path)}",
            "grid": grid_path,
            "offset": 0.0,
        }
    if low.startswith("offset:"):
        try:
            dz = float(val[7:].strip())
        except ValueError as exc:
            raise ValueError(f"Desplazamiento vertical inválido en '{val}': {exc}") from exc
        if not math.isfinite(dz):
            raise ValueError(f"Desplazamiento vertical no finito en '{val}'.")
        return {"kind": "offset", "label": f"offset:{dz:+.3f}m", "grid": None, "offset": dz}
    raise ValueError(
        f"EXPORT_VERTICAL_DATUM '{raw}' inválido. Válidos: ellipsoidal, egm96, geoid:<ruta>, offset:<metros>."
    )


def _resample_alg(src, discreto):
    """Remuestreo al reproyectar, según qué representa el ráster."""
    if discreto:
        return "near"
    nombre = os.path.basename(src)
    if nombre.startswith("thermal_orthomosaic"):
        return "near"
    if nombre.startswith(("rgb_orthomosaic", "dband_orthomosaic")):
        return "cubic"
    return "bilinear"


def _sample_geoid_at(grid_path, x, y, src_srs):
    """Muestrea bilinealmente la ondulación N (m) del geoide en un punto."""
    grid_ds = gdal.Open(grid_path)
    if grid_ds is None:
        raise RuntimeError(f"No se pudo abrir grilla de geoide: {grid_path}")
    try:
        grid_srs = grid_ds.GetSpatialRef()
        if grid_srs is None:
            grid_srs = _srs_from_epsg(4326)
        else:
            grid_srs = grid_srs.Clone()
        grid_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        in_srs = src_srs.Clone()
        in_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if not in_srs.IsSame(grid_srs):
            ct = osr.CoordinateTransformation(in_srs, grid_srs)
            gx, gy, _ = ct.TransformPoint(float(x), float(y))
        else:
            gx, gy = float(x), float(y)

        gt = grid_ds.GetGeoTransform()
        band = grid_ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        arr = band.ReadAsArray().astype(np.float64)
        if gt[0] >= -0.5 and gx < 0.0:
            gx = gx % 360.0
        px = (gx - gt[0]) / gt[1] - 0.5
        py = (gy - gt[3]) / gt[5] - 0.5
        cols, rows = grid_ds.RasterXSize, grid_ds.RasterYSize
        if px < 0 or py < 0 or px > cols - 1 or py > rows - 1:
            raise RuntimeError(f"Punto ({gx:.4f}, {gy:.4f}) fuera de la grilla de geoide {grid_path}")
        c0 = min(int(math.floor(px)), cols - 2)
        r0 = min(int(math.floor(py)), rows - 2)
        fx, fy = px - c0, py - r0
        patch = arr[r0 : r0 + 2, c0 : c0 + 2]
        if not np.isfinite(patch).all() or (nodata is not None and math.isfinite(nodata) and (patch == nodata).any()):
            raise RuntimeError(f"Celda NoData en grilla de geoide {grid_path} alrededor de ({gx:.4f}, {gy:.4f})")
        n_val = (
            patch[0, 0] * (1 - fx) * (1 - fy)
            + patch[0, 1] * fx * (1 - fy)
            + patch[1, 0] * (1 - fx) * fy
            + patch[1, 1] * fx * fy
        )
        if not math.isfinite(n_val) or abs(n_val) > 500.0:
            raise RuntimeError(f"Ondulación N fuera de rango en {grid_path}: {n_val}")
        return float(n_val)
    finally:
        grid_ds = None


def _apply_dsm_vertical_datum(tif_path, vdatum):
    """Aplica la conversión vertical al GeoTIFF del DSM y actualiza su CRS/meta."""
    ds = gdal.Open(tif_path, gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"No se pudo abrir DSM temporal {tif_path}")
    try:
        srs = ds.GetSpatialRef()
        if srs is None:
            raise RuntimeError("El DSM no tiene sistema de referencia espacial para aplicar datum vertical.")
        dst_srs = srs.Clone()
        dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        gt = ds.GetGeoTransform()
        w, h = ds.RasterXSize, ds.RasterYSize
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        arr = band.ReadAsArray().astype(np.float32)

        valid = np.isfinite(arr)
        if nodata is not None and math.isfinite(nodata):
            valid &= arr != np.float32(nodata)

        if vdatum["kind"] in ("egm96", "geoid"):
            xs = [gt[0], gt[0] + w * gt[1] + h * gt[2], gt[0] + w * gt[1], gt[0] + h * gt[2]]
            ys = [gt[3], gt[3] + w * gt[4] + h * gt[5], gt[3] + w * gt[4], gt[3] + h * gt[5]]
            bounds = (min(xs), min(ys), max(xs), max(ys))

            grid_ds = gdal.Open(vdatum["grid"])
            if grid_ds is None:
                raise RuntimeError(f"No se pudo abrir grilla de geoide: {vdatum['grid']}")
            grid_nd = grid_ds.GetRasterBand(1).GetNoDataValue()
            grid_srs = grid_ds.GetSpatialRef()
            grid_ds = None
            src_grid_srs = grid_srs.Clone() if grid_srs is not None else _srs_from_epsg(4326)
            src_grid_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

            # gdal.Warp reproyecta y remuestrea bilinealmente la grilla de
            # ondulación N (en EPSG:4326) al grid exacto del DSM en UTM/MAGNA.
            n_ds = gdal.Warp(
                "",
                vdatum["grid"],
                options=gdal.WarpOptions(
                    format="MEM",
                    srcSRS=src_grid_srs,
                    dstSRS=dst_srs,
                    outputBounds=bounds,
                    width=w,
                    height=h,
                    resampleAlg="bilinear",
                    dstNodata=float("nan"),
                    warpOptions=["INIT_DEST=NO_DATA"],
                ),
            )
            n_arr = n_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
            n_ds = None

            valid_n = np.isfinite(n_arr) & (np.abs(n_arr) < 500.0)
            if grid_nd is not None and math.isfinite(grid_nd):
                valid_n &= n_arr != np.float32(grid_nd)
            if valid.any() and not valid_n[valid].all():
                raise RuntimeError(f"La grilla de geoide {vdatum['grid']} no cubre el área completa del DSM.")
            arr[valid] = arr[valid] - n_arr[valid]
        elif vdatum["kind"] == "offset":
            arr[valid] = arr[valid] + np.float32(vdatum["offset"])

        band.WriteArray(arr)
        ds.SetMetadataItem("VERTICAL_DATUM", vdatum["label"])
        if vdatum["kind"] == "egm96":
            v_srs = _srs_from_epsg(5773)
            comp = osr.SpatialReference()
            if comp.SetCompoundCS(f"{srs.GetName()} + EGM96 height", srs, v_srs) == 0:
                ds.SetSpatialRef(comp)
    finally:
        ds = None


def _export_raster(src, dst_dir, fmt_key, epsg, discreto, vdatum, es_dsm=False):
    drv, ext, copts = RASTER_FORMATS[fmt_key]
    dst = os.path.join(dst_dir, os.path.splitext(os.path.basename(src))[0] + ext)
    resample = _resample_alg(src, discreto)
    ds = gdal.Open(src)
    dtype = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)
    ds = None
    predictor = "3" if dtype in ("Float32", "Float64") else "2"
    copts = copts + [f"PREDICTOR={predictor}"]

    # DOS PASOS a propósito: gdal.Warp NO puede escribir directo a COG (ver test
    # en test_export_products.py). Además escribimos el resultado final a un
    # temporal y hacemos os.replace para no dejar archivos a medias.
    tmp_warp = None
    tmp_out = None
    apply_vdatum = es_dsm and vdatum["kind"] != "ellipsoidal"
    try:
        source = src
        if epsg is not None or apply_vdatum:
            fd, tmp_warp = tempfile.mkstemp(suffix=".tif", dir=dst_dir)
            os.close(fd)
            if epsg is not None:
                warped = gdal.Warp(
                    tmp_warp,
                    src,
                    options=gdal.WarpOptions(
                        format="GTiff",
                        resampleAlg=resample,
                        multithread=True,
                        dstSRS=_srs_from_epsg(epsg),
                        creationOptions=["TILED=YES", "BIGTIFF=IF_SAFER"],
                    ),
                )
                warped = None
            else:
                copied = gdal.Translate(
                    tmp_warp,
                    src,
                    options=gdal.TranslateOptions(format="GTiff", creationOptions=["TILED=YES", "BIGTIFF=IF_SAFER"]),
                )
                copied = None
            if apply_vdatum:
                _apply_dsm_vertical_datum(tmp_warp, vdatum)
            source = tmp_warp

        fd_out, tmp_out = tempfile.mkstemp(suffix=ext, dir=dst_dir)
        os.close(fd_out)
        out = gdal.Translate(tmp_out, source, options=gdal.TranslateOptions(format=drv, creationOptions=copts))
        out = None
        os.replace(tmp_out, dst)
        tmp_out = None
    finally:
        for tmp in (tmp_warp, tmp_out):
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
    return dst


def _export_vector(src, dst_dir, fmt_key, epsg):
    drv, ext = VECTOR_FORMATS[fmt_key]
    dst = os.path.join(dst_dir, os.path.splitext(os.path.basename(src))[0] + ext)
    opts = dict(format=drv)
    if fmt_key == "kml":
        opts["dstSRS"] = _srs_from_epsg(4326)
    elif epsg is not None:
        opts["dstSRS"] = _srs_from_epsg(epsg)
    if os.path.exists(dst):
        os.remove(dst)
    gdal.VectorTranslate(dst, src, options=gdal.VectorTranslateOptions(**opts))
    return dst


def _export_pointcloud(src, dst_dir, epsg, vdatum):
    dst = os.path.join(dst_dir, os.path.basename(src))
    needs_vdatum = vdatum["kind"] != "ellipsoidal"
    if epsg is None and not needs_vdatum:
        shutil.copy2(src, dst)
        return dst

    pdal = _find_pdal()
    if pdal is None:
        if needs_vdatum:
            raise RuntimeError(
                f"No se puede aplicar datum vertical '{vdatum['label']}' a {os.path.basename(src)}: pdal no encontrado."
            )
        print(f"  ⚠ {os.path.basename(src)}: pdal no encontrado — se copia sin reproyectar")
        shutil.copy2(src, dst)
        return dst

    if not needs_vdatum:
        fd, tmp_dst = tempfile.mkstemp(suffix=".copc.laz", dir=dst_dir)
        os.close(fd)
        try:
            r = subprocess.run(
                [
                    pdal,
                    "translate",
                    src,
                    tmp_dst,
                    "reprojection",
                    f"--filters.reprojection.out_srs=EPSG:{epsg}",
                    "--writer",
                    "copc",
                ],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                print(
                    f"  ⚠ {os.path.basename(src)}: no se pudo reproyectar ({r.stderr.strip()[:160]}) "
                    "— se copia en la CRS original"
                )
                shutil.copy2(src, dst)
            else:
                os.replace(tmp_dst, dst)
                tmp_dst = None
        finally:
            if tmp_dst and os.path.exists(tmp_dst):
                os.unlink(tmp_dst)
        return dst

    # Datum vertical explícito en nube: falla en voz alta si info o pipeline fallan.
    info_r = subprocess.run([pdal, "info", "--summary", src], capture_output=True, text=True)
    if info_r.returncode != 0:
        raise RuntimeError(f"pdal info falló sobre {os.path.basename(src)}: {info_r.stderr.strip()[:200]}")
    summary = json.loads(info_r.stdout).get("summary", {})
    bounds = summary.get("bounds", {})
    srs_wkt = (summary.get("srs") or {}).get("wkt") or (summary.get("srs") or {}).get("proj4") or ""
    if not srs_wkt:
        raise RuntimeError(f"{os.path.basename(src)} no tiene SRS embebido para aplicar '{vdatum['label']}'.")

    src_srs = osr.SpatialReference()
    src_srs.SetFromUserInput(srs_wkt)
    if vdatum["kind"] in ("egm96", "geoid"):
        cx = (bounds["minx"] + bounds["maxx"]) / 2.0
        cy = (bounds["miny"] + bounds["maxy"]) / 2.0
        dz = -_sample_geoid_at(vdatum["grid"], cx, cy, src_srs)
    else:
        dz = vdatum["offset"]

    target_h = _srs_from_epsg(epsg) if epsg is not None else src_srs
    if vdatum["kind"] == "egm96":
        comp = osr.SpatialReference()
        v_srs = _srs_from_epsg(5773)
        a_srs = (
            comp.ExportToWkt()
            if comp.SetCompoundCS(f"{target_h.GetName()} + EGM96 height", target_h, v_srs) == 0
            else target_h.ExportToWkt()
        )
    else:
        a_srs = f"EPSG:{epsg}" if epsg is not None else target_h.ExportToWkt()

    stages = [{"type": "readers.las", "filename": src}]
    if epsg is not None:
        stages.append({"type": "filters.reprojection", "out_srs": f"EPSG:{epsg}"})
    stages.append({"type": "filters.transformation", "matrix": f"1 0 0 0 0 1 0 0 0 0 1 {dz:.6f} 0 0 0 1"})

    fd, tmp_dst = tempfile.mkstemp(suffix=".copc.laz", dir=dst_dir)
    os.close(fd)
    try:
        stages.append({"type": "writers.copc", "filename": tmp_dst, "forward": "all", "a_srs": a_srs})
        r = subprocess.run(
            [pdal, "pipeline", "--stdin"], input=json.dumps({"pipeline": stages}), capture_output=True, text=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"pdal pipeline falló aplicando '{vdatum['label']}': {r.stderr.strip()[:240]}")
        os.replace(tmp_dst, dst)
        tmp_dst = None
    finally:
        if tmp_dst and os.path.exists(tmp_dst):
            os.unlink(tmp_dst)
    return dst


def main():
    export_dir = os.environ.get("EXPORT_DIR", "").strip()
    if not export_dir:
        print("  (sin EXPORT_DIR — no se exporta nada)")
        return 0

    requested = os.environ.get("EXPORT_PRODUCTS", "all").strip()
    keys = list(CATALOG) if requested in ("", "all") else [k.strip() for k in requested.split(",") if k.strip()]
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
            _srs_from_epsg(epsg)
        except Exception as exc:
            print(f"❌ ERROR: EPSG '{epsg_raw}' inválido o desconocido: {exc}")
            return 1

    try:
        vdatum = parse_vertical_datum(os.environ.get("EXPORT_VERTICAL_DATUM", ""))
    except ValueError as exc:
        print(f"❌ ERROR: {exc}")
        return 1

    # Validación previa al bucle para no dejar entregas parciales sin manifiesto.
    presentes_por_clave = {}
    for key in keys:
        _label, _kind, sources = CATALOG[key]
        presentes = [s for s in sources if os.path.isfile(s)]
        if key == "area":
            presentes = presentes[:1]
        presentes_por_clave[key] = presentes

    if vdatum["kind"] != "ellipsoidal" and presentes_por_clave.get("pointclouds") and _find_pdal() is None:
        print(f"❌ ERROR: EXPORT_VERTICAL_DATUM={vdatum['label']} para nubes COPC requiere pdal y no se encontró.")
        return 1

    try:
        os.makedirs(export_dir, exist_ok=True)
    except OSError as exc:
        print(f"❌ ERROR: no se pudo crear la carpeta de exportación '{export_dir}': {exc}")
        return 1

    crs_label = f"EPSG:{epsg} ({_srs_from_epsg(epsg).GetName()})" if epsg else "la de la misión (sin reproyectar)"
    print(f"  destino : {export_dir}")
    print(f"  CRS     : {crs_label}")
    print(f"  vertical: {vdatum['label']}")
    print(f"  formatos: ráster={raster_fmt} · vectorial={vector_fmt}")

    manifest = {
        "crs": f"EPSG:{epsg}" if epsg else "origen",
        "vertical_datum": vdatum["label"],
        "raster_format": raster_fmt,
        "vector_format": vector_fmt,
        "archivos": [],
    }
    n_ok = n_skip = 0
    for key in keys:
        label, kind, _sources = CATALOG[key]
        presentes = presentes_por_clave[key]
        if not presentes:
            print(f"  – {label}: no generado en esta misión, omitido")
            n_skip += 1
            continue
        for src in presentes:
            try:
                if kind in ("raster", "raster_discreto"):
                    dst = _export_raster(
                        src,
                        export_dir,
                        raster_fmt,
                        epsg,
                        discreto=(kind == "raster_discreto"),
                        vdatum=vdatum,
                        es_dsm=(key == "dsm"),
                    )
                elif kind == "vector":
                    dst = _export_vector(src, export_dir, vector_fmt, epsg)
                elif kind == "nube":
                    dst = _export_pointcloud(src, export_dir, epsg, vdatum)
                else:
                    dst = os.path.join(export_dir, os.path.basename(src))
                    shutil.copy2(src, dst)
            except Exception as exc:
                print(f"  ❌ {os.path.basename(src)}: {exc}")
                return 1
            size_mb = os.path.getsize(dst) / 1e6
            print(f"  ✅ {os.path.basename(dst)}  ({size_mb:.1f} MB)  ← {label}")
            entry = {
                "producto": key,
                "etiqueta": label,
                "archivo": os.path.basename(dst),
                "origen": src,
                "mb": round(size_mb, 1),
            }
            if key in ("dsm", "pointclouds"):
                entry["vertical_datum"] = vdatum["label"]
            if key == "pointclouds" and vdatum["kind"] in ("egm96", "geoid"):
                entry["vertical_method"] = "constant_N_at_centroid"
            manifest["archivos"].append(entry)
            n_ok += 1

    manifest_path = os.path.join(export_dir, "export_manifest.json")
    fd_m, tmp_manifest = tempfile.mkstemp(prefix=".manifest_", suffix=".json", dir=export_dir)
    try:
        with os.fdopen(fd_m, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp_manifest, manifest_path)
    finally:
        if os.path.exists(tmp_manifest):
            os.unlink(tmp_manifest)

    for root, dirs, files in os.walk(export_dir):
        for p in dirs + files:
            try:
                os.chmod(os.path.join(root, p), 0o666 if p in files else 0o777)
            except OSError:
                pass

    print(
        f"\n✅ {n_ok} archivo(s) exportado(s) a {export_dir}"
        + (f" ({n_skip} producto(s) no disponibles en esta misión)" if n_skip else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
