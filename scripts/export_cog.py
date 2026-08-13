#!/usr/bin/env python3
"""Convierte los productos raster finales a COG (Cloud Optimized GeoTIFF):
mismo GeoTIFF de siempre pero con overviews embebidos y una organización
interna que permite lectura parcial por HTTP range-requests (QGIS, Potree,
servidores de tiles pueden leerlos directo sin bajar el archivo entero).

Sobrescribe en el mismo path que ya usan generate_tiles.py y el geovisor —
un COG es un GeoTIFF válido normal (GDAL lo lee exactamente igual), así que
nada corriente abajo se rompe con el cambio.

Uso:
  python3 scripts/export_cog.py                       # todos los productos existentes
  python3 scripts/export_cog.py outputs/dsm.tif ...    # solo los que se pasan

La forma con archivos explícitos la usa docker/entrypoint.sh apenas termina
el post-procesamiento de CADA sensor (no hace falta esperar a que los cuatro
terminen para que el ortomosaico de uno ya esté disponible como COG) — ver
_odm_post() en entrypoint.sh. La forma sin argumentos sigue siendo el pase
de seguridad final: recorre TODO lo que exista en outputs/, e IGNORA lo que
ya sea COG (ver _ya_es_cog) para no re-escribir de gratis lo que el pase por
sensor ya dejó listo.
"""
import os, sys, glob, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor
from osgeo import gdal

gdal.UseExceptions()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402

# Cada gdal_translate es un proceso aparte sobre su propio archivo: no hay
# estado compartido, así que paralelizar es directo. Antes corrían los 11
# rásters de una misión completa (~3.2 GB) uno atrás del otro y monohilo,
# con 19 núcleos ociosos al final de una corrida de horas.
#
# El perfil de memoria es el de gdal_translate leyendo por bloques, no el de
# cargar el ráster entero, así que 2 MP de "peso" por hilo es de sobra —
# mismo criterio que la concurrencia liviana de ODM. NUM_THREADS=ALL_CPUS
# además paraleliza la compresión DEFLATE puertas adentro de cada uno.
_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(2, override=int(_override) if _override else None)

OUTPUTS = "outputs"
# dband_orthomosaic.tif se agrega acá (antes faltaba: los otros tres
# ortomosaicos "crudos" por sensor —rgb/thermal/multispectral— sí estaban).
PRODUCTS = [
    os.path.join(OUTPUTS, "rgb_orthomosaic.tif"),
    os.path.join(OUTPUTS, "thermal_orthomosaic.tif"),
    os.path.join(OUTPUTS, "dband_orthomosaic.tif"),
    os.path.join(OUTPUTS, "dsm.tif"),
    os.path.join(OUTPUTS, "confidence_mask.tif"),
    os.path.join(OUTPUTS, "multispectral_orthomosaic.tif"),
    # Los rasters de CLASES (Byte, discretos) también salen en COG — antes
    # quedaban afuera y se exportaban como GeoTIFF plano pese a que el resto
    # de los productos de la misión ya era COG.
    os.path.join(OUTPUTS, "severidad_class.tif"),
    os.path.join(OUTPUTS, "termico_hotspot_class.tif"),
] + sorted(glob.glob(os.path.join(OUTPUTS, "indices", "*.tif")))


def _predictor_for(path):
    """PREDICTOR de compresión según el tipo de dato real (no asumido):
    3 = floating point (DSM, térmico, multiespectral, índices), 2 = entero
    (RGB byte, máscara de confianza), 1 = sin predictor (resto)."""
    ds = gdal.Open(path)
    dt = ds.GetRasterBand(1).DataType
    ds = None
    name = gdal.GetDataTypeName(dt)
    if name in ("Float32", "Float64"):
        return "3"
    if name in ("Byte", "Int16", "UInt16", "Int32", "UInt32"):
        return "2"
    return "1"


def _ya_es_cog(path):
    """True si `path` ya está en layout COG — GDAL lo marca en los metadatos
    de dominio IMAGE_STRUCTURE. Evita reconvertir en el pase de seguridad
    final lo que la exportación por sensor ya dejó listo minutos u horas
    antes (barato: un gdal.Info, no una relectura+reescritura del ráster
    completo)."""
    try:
        ds = gdal.Open(path)
        layout = ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE")
        ds = None
        return layout == "COG"
    except RuntimeError:
        return False


def to_cog(path):
    predictor = _predictor_for(path)
    before = os.path.getsize(path)
    fd, tmp = tempfile.mkstemp(suffix=".tif", dir=os.path.dirname(path) or ".")
    os.close(fd)
    try:
        result = subprocess.run(
            ["gdal_translate", "-of", "COG",
             "-co", "COMPRESS=DEFLATE", "-co", f"PREDICTOR={predictor}",
             "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", "BIGTIFF=IF_SAFER",
             "-co", "NUM_THREADS=ALL_CPUS",
             path, tmp],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  ❌ {path}: {result.stderr[:300]}")
            return False
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    after = os.path.getsize(path)
    print(f"  ✅ {path}: {before/1e6:.0f}MB → {after/1e6:.0f}MB (COG, predictor={predictor})")
    return True


def main():
    argfiles = sys.argv[1:]
    if argfiles:
        # Modo por sensor: SOLO lo que se pidió, sin el salteo por
        # "ya es COG" — si se lo llama explícitamente con un archivo es
        # porque se sabe que recién se escribió.
        pendientes = [p for p in argfiles if os.path.isfile(p)]
        faltantes = [p for p in argfiles if not os.path.isfile(p)]
        for p in faltantes:
            print(f"  ⚠ {p} no existe, omitiendo")
    else:
        # Pase de seguridad: todo lo que exista Y todavía no sea COG.
        pendientes = [p for p in PRODUCTS if os.path.isfile(p) and not _ya_es_cog(p)]
        ya_listos = [p for p in PRODUCTS if os.path.isfile(p) and p not in pendientes]
        for p in ya_listos:
            print(f"  ✓ {p}: ya es COG, omitiendo")

    if not pendientes:
        print("  ⚠ Nada para convertir a COG" if argfiles else
              "  ⚠ No se encontró ningún raster final en outputs/ para convertir")
        print("\n✅ 0 raster(es) convertido(s) a COG")
        return

    print(f"Convirtiendo {len(pendientes)} raster(es) a COG con {NPROCS} en paralelo …")
    with ThreadPoolExecutor(max_workers=NPROCS) as pool:
        resultados = list(pool.map(to_cog, pendientes))

    # Se espera a TODOS antes de decidir: cortar en el primer fallo dejaría
    # los demás a medio convertir y sin decir cuáles.
    if not all(resultados):
        fallidos = [p for p, ok in zip(pendientes, resultados) if not ok]
        print(f"\n❌ Falló la conversión de {len(fallidos)}: {', '.join(fallidos)}")
        sys.exit(1)
    print(f"\n✅ {len(resultados)} raster(es) convertido(s) a COG")


if __name__ == "__main__":
    main()
