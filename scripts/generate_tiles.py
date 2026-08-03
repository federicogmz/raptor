#!/usr/bin/env python3
"""Generar tiles XYZ para geovisor Leaflet: RGB, térmico y hillshade del DSM."""
import os, sys, json, math, subprocess, shutil, tempfile, glob, numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()

def _first(*paths):
    """Primer archivo que exista. Se usa para que este script pueda correrse
    a MITAD del pipeline y publique lo mejor disponible: si el producto final
    (recortado) todavía no existe pero ODM ya escribió su ortofoto cruda, se
    tesela esa como vista previa. Cuando después aparece el recortado, su
    mtime distinto invalida el sello y la capa se regenera sola (ver
    tile_layer/up_to_date). Así el geovisor muestra el mosaico apenas termina
    la reconstrucción, no al final de toda la corrida."""
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]


RGB_IN      = _first("outputs/rgb_orthomosaic.tif",
                     "processing/rgb_odm/odm_orthophoto/odm_orthophoto.tif")
DSM_IN      = _first("outputs/dsm.tif",
                     "processing/rgb_odm/odm_dem/dsm.tif",
                     "processing/multispectral_odm/odm_dem/dsm.tif")
MS_IN       = _first("outputs/multispectral_orthomosaic.tif",
                     "processing/multispectral_odm/odm_orthophoto/odm_orthophoto.tif")
# El térmico NO tiene vista previa desde la ortofoto cruda de ODM a
# propósito: su leyenda son grados reales y la calibración Kelvin→°C se
# aplica recién en el recorte. Publicar la cruda mostraría una escala de
# temperatura falsa — en una herramienta de respuesta a incendios eso es
# peor que no mostrar nada todavía.
THERMAL_IN  = "outputs/thermal_orthomosaic.tif"
INDICES_DIR = "outputs/indices"
INDEX_NAMES = ["ndvi", "gndvi", "ndre", "msavi2"]
TILES_DIR   = "geovisor/tiles"
ZOOM_MIN    = 14
# El zoom MÁXIMO se deriva de la resolución real de cada ráster (ver
# zoom_range): fijarlo desaprovecha el dato. Con un tope fijo de 20, que a 6°N
# son ~15 cm/px, un ortomosaico de 8 cm/px se veía a la mitad de la resolución
# que ya se había calculado. El tope duro evita generar pirámides absurdas si
# alguna vez llega un ráster de resolución milimétrica.
ZOOM_MAX_HARD = 23
# Núcleos detectados (scripts/hardware.py, misma fuente que la concurrencia de
# ODM en el entrypoint) — antes era un 4 fijo que no aprovechaba máquinas más
# grandes ni se cuidaba en las más chicas. gdal2tiles.py reparte por zoom, no
# por RAM por hilo como ODM, así que acá alcanza con los núcleos sin acotar
# por memoria. NPROCS sigue pudiéndose forzar a mano si hace falta.
if os.environ.get("NPROCS"):
    NPROCS = int(os.environ["NPROCS"])
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hardware import cpu_count
    NPROCS = cpu_count()
# Nombre de banda (GetDescription(), lo pone ODM vía XMP Camera:BandName) ->
# id corto de capa/tile. El compositor client-side (app.js) arma composites
# RGB en el navegador combinando estas capas de a 3 — no se generan
# combinaciones fijas en disco, así el usuario elige/cambia la combinación
# sin tener que re-generar tiles.
MS_BAND_LAYER_IDS = {"Red": "ms_red", "Green": "ms_green", "NIR": "ms_nir", "RedEdge": "ms_rededge"}


def _index_clip_range(path):
    """Rango de color para un índice de vegetación (NDVI/GNDVI/NDRE): la
    fórmula (a-b)/(a+b) está matemáticamente acotada a [-1,1], pero
    reflectancias ruidosas (a+b≈0) pueden producir artefactos fuera de ese
    rango — se usa percentil 1-99 del dato real (mismo criterio que el
    térmico, ver _thermal_clip_range) recortado siempre a [-1,1]."""
    ds = gdal.Open(path)
    a = ds.GetRasterBand(1).ReadAsArray()
    mask = np.isfinite(a)
    if ds.RasterCount >= 2:
        # Banda 2 = alpha: ODM rellena el área fuera de cobertura con un
        # valor arbitrario en la banda de dato (no necesariamente NaN — se
        # vio literalmente 2**32 en una corrida real), pero SÍ marca alpha=0
        # ahí de forma confiable. isfinite() sola no alcanza para excluirlo.
        mask &= ds.GetRasterBand(2).ReadAsArray() > 0
    ds = None
    valid = a[mask]
    if valid.size == 0:
        return (-1.0, 1.0)
    lo, hi = np.percentile(valid, [1, 99])
    lo, hi = max(-1.0, float(lo)), min(1.0, float(hi))
    if hi - lo < 0.1:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.05, mid + 0.05
    return (lo, hi)


def _band_clip_range(path, band_idx):
    """Rango de color (percentil 1-99) para una banda espectral CRUDA
    (reflectancia/radiancia, no acotada a [-1,1] como los índices) — mismo
    criterio percentil que _index_clip_range, sin el clamp específico de
    índice."""
    ds = gdal.Open(path)
    a = ds.GetRasterBand(band_idx).ReadAsArray()
    mask = np.isfinite(a)
    if ds.RasterCount >= 2:
        alpha_band = ds.RasterCount
        mask &= ds.GetRasterBand(alpha_band).ReadAsArray() > 0
    ds = None
    valid = a[mask]
    if valid.size == 0:
        return (0.0, 1.0)
    lo, hi = np.percentile(valid, [1, 99])
    lo, hi = float(lo), float(hi)
    if hi - lo < 1e-6:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.5, mid + 0.5
    return (lo, hi)


def _thermal_clip_range():
    """Rango de color para el 8-bit térmico, calculado del dato REAL de esta
    misión (no un valor fijo): un rango fijo calibrado para un vuelo con
    variación térmica amplia (p.ej. 14-60°C) comprime a una franja angosta
    de color cualquier misión con rango real más chico (p.ej. 30-36°C en
    zonas de vegetación uniforme), y el mosaico se ve como un bloque sólido
    sin detalle. Se usa percentil 0.5-99.9 (no min/max crudo, sensible a
    spikes residuales de 1-2 píxeles) con un piso de 5°C de span para no
    reventar el contraste si la escena es EXTREMADAMENTE uniforme.
    El extremo superior es 99.9, no 99: en misiones de incendio los puntos
    calientes reales (>1% del área en focos activos) son justo el dato de
    interés: recortar en el percentil 99 los satura todos al mismo blanco
    plano y se pierde el detalle de intensidad entre focos — con ~1% del área
    repartida entre 33°C y 45°C, todo ese rango colapsaría a un único tono."""
    if not os.path.exists(THERMAL_IN):
        return (15.0, 55.0)
    ds = gdal.Open(THERMAL_IN)
    a = ds.GetRasterBand(1).ReadAsArray()
    mask = np.isfinite(a)
    if ds.RasterCount >= 2:
        # Ver misma nota en _index_clip_range: el relleno fuera de cobertura
        # de ODM no siempre es NaN (visto: 2**32 exacto), alpha=0 es la
        # única señal de validez confiable.
        mask &= ds.GetRasterBand(2).ReadAsArray() > 0
    ds = None
    valid = a[mask]
    if valid.size == 0:
        return (15.0, 55.0)
    lo, hi = np.percentile(valid, [0.5, 99.9])
    if hi - lo < 5.0:
        mid = (hi + lo) / 2
        lo, hi = mid - 2.5, mid + 2.5
    return (float(lo), float(hi))


THERMAL_CLIP = _thermal_clip_range()
print(f"Rango de color térmico (percentil 1-99 del dato real): {THERMAL_CLIP[0]:.1f}-{THERMAL_CLIP[1]:.1f}°C")

os.makedirs(TILES_DIR, exist_ok=True)

if shutil.which("gdal2tiles.py") is None:
    print("❌ gdal2tiles.py no encontrado en el PATH.")
    sys.exit(1)


def to_8bit(src, dst, bands=3, clip_range=None):
    """Convertir a 8-bit para tiles."""
    ds = gdal.Open(src)
    arr = ds.ReadAsArray()
    if bands == 3:
        has_alpha = arr.shape[0] >= 4
        rgb = arr[:4] if has_alpha else (arr[:3] if arr.shape[0] >= 3 else np.stack([arr[0]]*3))
        if rgb.dtype != np.uint8:
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        nb = 4 if has_alpha else 3
        drv = gdal.GetDriverByName("GTiff")
        out = drv.Create(dst, ds.RasterXSize, ds.RasterYSize, nb, gdal.GDT_Byte,
                         ["COMPRESS=LZW", "TILED=YES"])
        out.SetGeoTransform(ds.GetGeoTransform())
        out.SetProjection(ds.GetProjection())
        for i in range(nb):
            out.GetRasterBand(i+1).WriteArray(rgb[i])
        if has_alpha:
            out.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
        out = None
    else:
        a = ds.GetRasterBand(1).ReadAsArray()
        # Máscara de validez real: alpha=0 (si existe banda 2) es la única
        # señal confiable de "fuera de cobertura" — el relleno de ODM en la
        # banda de dato no siempre es NaN (visto: 2**32 exacto en una
        # corrida real), así que isfinite() sola deja pasar basura.
        invalid = ~np.isfinite(a)
        if ds.RasterCount >= 2:
            invalid |= (ds.GetRasterBand(2).ReadAsArray() <= 0)
        a = np.nan_to_num(a, nan=0.0)
        if clip_range:
            a = np.clip(a, clip_range[0], clip_range[1])
            a = ((a - clip_range[0]) / (clip_range[1] - clip_range[0]) * 255).astype(np.uint8)
        else:
            a = np.clip(a, 0, 255).astype(np.uint8)
        a[invalid] = 0
        drv = gdal.GetDriverByName("GTiff")
        out = drv.Create(dst, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte,
                         ["COMPRESS=LZW", "TILED=YES"])
        out.SetGeoTransform(ds.GetGeoTransform())
        out.SetProjection(ds.GetProjection())
        out.GetRasterBand(1).WriteArray(a)
        out.GetRasterBand(1).SetNoDataValue(0)
        out = None
    ds = None
    print(f"  8-bit: {dst} ({os.path.getsize(dst)/(1024*1024):.0f} MB)")


def band_to_8bit(src, dst, band_idx, clip_range):
    """Como to_8bit(bands=1) pero para un índice de banda específico de un
    raster MULTIbanda (p.ej. una banda espectral individual del ortomosaico
    MS de 5 bandas: Red/Green/NIR/RedEdge/alpha) — to_8bit() solo sabía leer
    la banda 1."""
    ds = gdal.Open(src)
    a = ds.GetRasterBand(band_idx).ReadAsArray()
    invalid = ~np.isfinite(a)
    if ds.RasterCount >= 2:
        invalid |= (ds.GetRasterBand(ds.RasterCount).ReadAsArray() <= 0)
    a = np.nan_to_num(a, nan=0.0)
    a = np.clip(a, clip_range[0], clip_range[1])
    a = ((a - clip_range[0]) / (clip_range[1] - clip_range[0]) * 255).astype(np.uint8)
    a[invalid] = 0
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(dst, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte,
                     ["COMPRESS=LZW", "TILED=YES"])
    out.SetGeoTransform(ds.GetGeoTransform())
    out.SetProjection(ds.GetProjection())
    out.GetRasterBand(1).WriteArray(a)
    out.GetRasterBand(1).SetNoDataValue(0)
    out = None; ds = None
    print(f"  8-bit: {dst} ({os.path.getsize(dst)/(1024*1024):.0f} MB)")


def zoom_range(src):
    """Rango de zoom XYZ que cubre la resolución REAL del ráster.

    El nivel z de la pirámide web tiene 156543.03 * cos(lat) / 2^z metros por
    píxel. Se elige el primer z cuya resolución es igual o más fina que la del
    ráster: teselar más allá solo interpola, y quedarse corto tira detalle ya
    calculado.
    """
    ds = gdal.Open(src)
    gt = ds.GetGeoTransform()
    px_m = abs(gt[1])
    # Centro del ráster en lat/lon, para el cos(lat) de la fórmula
    srs = ds.GetSpatialRef()
    lat = 0.0
    if srs is not None:
        wgs = osr.SpatialReference()
        wgs.ImportFromEPSG(4326)
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        try:
            tr = osr.CoordinateTransformation(srs, wgs)
            cx = gt[0] + gt[1] * ds.RasterXSize / 2
            cy = gt[3] + gt[5] * ds.RasterYSize / 2
            lat = tr.TransformPoint(cx, cy)[1]
        except RuntimeError:
            lat = 0.0
    ds = None
    z = ZOOM_MIN
    while z < ZOOM_MAX_HARD:
        res = 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)
        if res <= px_m:
            break
        z += 1
    return z


def generate(src_8bit, out_dir, resampling="average"):
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    zmax = zoom_range(src_8bit)
    print(f"  zoom {ZOOM_MIN}-{zmax} (resolución del ráster: "
          f"{abs(gdal.Open(src_8bit).GetGeoTransform()[1])*100:.1f} cm/px)")
    subprocess.run([
        "gdal2tiles.py", "--xyz", "--no-kml", "--resampling", resampling,
        f"--processes={NPROCS}", "-z", f"{ZOOM_MIN}-{zmax}",
        src_8bit, out_dir,
    ], check=True)
    # gdal2tiles.py con --processes reparte el trabajo en subprocesos: un OOM
    # kill de UNO de ellos no siempre hace que el proceso padre salga con
    # código de error (`check=True` no lo detecta) — deja carpetas de zoom a
    # medias en out_dir, indistinguibles de una corrida completa para
    # cualquiera que sirva esos tiles después. Reportado como "hay un nivel
    # de zoom en el que la capa no renderiza" — un hueco real de tiles, no un
    # bug del geovisor. Se verifica acá que TODOS los niveles pedidos
    # existan y tengan al menos un PNG antes de darlo por bueno; tile_layer()
    # no llama a mark_done() si esto revienta, así que la próxima pasada de
    # `make tiles` reintenta desde cero en vez de quedar con el hueco para
    # siempre.
    faltantes = [z for z in range(ZOOM_MIN, zmax + 1)
                 if not glob.glob(os.path.join(out_dir, str(z), "*", "*.png"))]
    if faltantes:
        raise RuntimeError(
            f"{out_dir}: gdal2tiles.py terminó pero faltan tiles en zoom "
            f"{faltantes} (rango pedido {ZOOM_MIN}-{zmax}) — probable OOM kill "
            f"de un subproceso de --processes={NPROCS}")
    n = sum(1 for _ in os.walk(out_dir) for f in _[2] if f.endswith(".png"))
    print(f"  {n} tiles PNG")


# ── Teselado incremental ────────────────────────────────────────────────
# El entrypoint invoca `make tiles` VARIAS veces durante una corrida (apenas
# termina cada producto) para que el geovisor los vaya publicando en vez de
# aparecer todo junto al final. Sin este salteo, cada pasada volvería a
# teselar lo ya hecho — minutos por capa — y el costo se comería la ventaja.
# Se compara contra un sello con el mtime del raster fuente: si no cambió, la
# capa está al día. FORCE_TILES=1 rehace todo.
STAMP = ".src_mtime"


def up_to_date(src_ref, out_dir):
    if os.environ.get("FORCE_TILES") == "1":
        return False
    stamp = os.path.join(out_dir, STAMP)
    if not os.path.isdir(out_dir) or not os.path.exists(stamp):
        return False
    try:
        with open(stamp) as f:
            return abs(float(f.read().strip()) - os.path.getmtime(src_ref)) < 1e-6
    except (OSError, ValueError):
        return False


def mark_done(src_ref, out_dir):
    try:
        with open(os.path.join(out_dir, STAMP), "w") as f:
            f.write(repr(os.path.getmtime(src_ref)))
    except OSError:
        pass


def tile_layer(src_ref, out_dir, build=None, resampling="average"):
    """Tesela una capa si hace falta. `build(tmp)` escribe el TIFF 8-bit a
    teselar; si es None se tesela `src_ref` directamente (ya viene en Byte)."""
    name = os.path.basename(out_dir)
    if up_to_date(src_ref, out_dir):
        print(f"  ✓ {name}: ya al día, omitiendo")
        return False
    if build is None:
        generate(src_ref, out_dir, resampling=resampling)
        mark_done(src_ref, out_dir)
        return True
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tmp = tf.name
    try:
        build(tmp)
        generate(tmp, out_dir, resampling=resampling)
        mark_done(src_ref, out_dir)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def make_hillshade(src_dsm, dst_hs):
    """Genera un hillshade 8-bit desde el DSM usando gdaldem."""
    print("  Calculando hillshade...")
    subprocess.run([
        "gdaldem", "hillshade", src_dsm, dst_hs,
        "-z", "2", "-az", "315", "-alt", "45",
        "-compute_edges", "-co", "COMPRESS=LZW", "-co", "TILED=YES"
    ], check=True)
    # Convertir a 8-bit para tiles
    ds = gdal.Open(dst_hs)
    arr = ds.GetRasterBand(1).ReadAsArray()
    arr = np.nan_to_num(arr, nan=0)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(dst_hs, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte,
                     ["COMPRESS=LZW", "TILED=YES"])
    out.SetGeoTransform(ds.GetGeoTransform())
    out.SetProjection(ds.GetProjection())
    out.GetRasterBand(1).WriteArray(arr)
    out.GetRasterBand(1).SetNoDataValue(0)
    out = None
    ds = None
    print(f"  Hillshade: {dst_hs} ({os.path.getsize(dst_hs)/(1024*1024):.0f} MB)")


# Capas cuyo LAYER_REGISTRY del geovisor las registra SIN condición propia
# (a diferencia de los índices continuos, las bandas MS o el área afectada,
# que ya se gatean solos por bounds.json/fetch): antes de esto, una misión
# sin multiespectral igual ofrecía en el panel "Severidad", "Hotspot" y los 4
# índices clasificados, con tiles que no existen — confuso, y en el caso de
# rgb/thermal, un panel que promete un sensor que la misión ni siquiera voló.
# Se registra acá cuáles existen DE VERDAD para que app.js registre solo esas.
capas_disponibles = []

# RGB
print("=== RGB Tiles ===")
if os.path.exists(RGB_IN):
    tile_layer(RGB_IN, os.path.join(TILES_DIR, "rgb"),
               lambda t: to_8bit(RGB_IN, t, bands=3))
    capas_disponibles.append("rgb")
else:
    print(f"  ⚠ {RGB_IN} no existe, omitiendo")

# Thermal
print("\n=== Thermal Tiles ===")
if os.path.exists(THERMAL_IN):
    tile_layer(THERMAL_IN, os.path.join(TILES_DIR, "thermal"),
               lambda t: to_8bit(THERMAL_IN, t, bands=1, clip_range=THERMAL_CLIP))
    capas_disponibles.append("thermal")
else:
    print(f"  ⚠ {THERMAL_IN} no existe, omitiendo")

# Hillshade (DSM)
print("\n=== Hillshade Tiles (DSM) ===")
if os.path.exists(DSM_IN):
    tile_layer(DSM_IN, os.path.join(TILES_DIR, "hillshade"),
               lambda t: make_hillshade(DSM_IN, t))
    capas_disponibles.append("hillshade")
else:
    print(f"  ⚠ {DSM_IN} no existe, omitiendo")

# Índices de vegetación (NDVI/GNDVI/NDRE)
index_ranges = {}
print("\n=== Índices de vegetación (Tiles) ===")
for name in INDEX_NAMES:
    idx_in = os.path.join(INDICES_DIR, f"{name}.tif")
    if not os.path.exists(idx_in):
        print(f"  ⚠ {idx_in} no existe, omitiendo")
        continue
    clip = _index_clip_range(idx_in)
    index_ranges[name] = clip
    print(f"  {name.upper()} rango de color: {clip[0]:.2f}..{clip[1]:.2f}")
    tile_layer(idx_in, os.path.join(TILES_DIR, name),
               lambda t, p=idx_in, c=clip: to_8bit(p, t, bands=1, clip_range=c))

# Bandas espectrales individuales del multiespectral (Red/Green/NIR/RedEdge)
# — se sirven CRUDAS, sin componer, para que el geovisor arme composites RGB
# en el navegador (presets tipo falso-color-IR, RedEdge, o combinación
# personalizada) sin tener que pre-generar cada combinación posible.
ms_band_ranges = {}
print("\n=== Bandas multiespectrales individuales (Tiles) ===")
if os.path.exists(MS_IN):
    ds = gdal.Open(MS_IN)
    n_bands = ds.RasterCount
    has_alpha = n_bands >= 2 and ds.GetRasterBand(n_bands).GetColorInterpretation() == gdal.GCI_AlphaBand
    n_spectral = n_bands - 1 if has_alpha else n_bands
    band_desc = {i + 1: (ds.GetRasterBand(i + 1).GetDescription() or f"band{i+1}") for i in range(n_spectral)}
    ds = None
    for band_idx, desc in band_desc.items():
        layer_id = MS_BAND_LAYER_IDS.get(desc, f"ms_band{band_idx}")
        clip = _band_clip_range(MS_IN, band_idx)
        ms_band_ranges[layer_id] = clip
        print(f"  {desc} ({layer_id}) rango: {clip[0]:.3f}..{clip[1]:.3f}")
        tile_layer(MS_IN, os.path.join(TILES_DIR, layer_id),
                   lambda t, b=band_idx, c=clip: band_to_8bit(MS_IN, t, b, c))
else:
    print(f"  ⚠ {MS_IN} no existe, omitiendo")

# Área afectada: severidad + hotspot térmico (clases 0-4, ya en Byte con
# nodata=0 desde compute_severity_classes.py — sin escalar). resampling
# "near", NO "average": son clases discretas, promediar valores vecinos
# inventaría una clase intermedia que no existe (p.ej. 2.5 entre leve y
# moderado no significa nada).
print("\n=== Severidad / Hotspot térmico / Índices clasificados (Tiles) ===")
for name, path in [("severidad", "outputs/severidad_class.tif"),
                    ("hotspot_termico", "outputs/termico_hotspot_class.tif"),
                    ("ndvi_class", "outputs/indices/ndvi_class.tif"),
                    ("gndvi_class", "outputs/indices/gndvi_class.tif"),
                    ("ndre_class", "outputs/indices/ndre_class.tif"),
                    ("msavi2_class", "outputs/indices/msavi2_class.tif")]:
    if not os.path.exists(path):
        print(f"  ⚠ {path} no existe, omitiendo")
        continue
    tile_layer(path, os.path.join(TILES_DIR, name), resampling="near")
    capas_disponibles.append(name)

# Centro real (lat/lon WGS84) para que el geovisor abra sobre los datos de
# ESTA misión en vez de un centro hardcodeado de una misión anterior — cada
# corrida procesa un sitio distinto (ver index.html, que lee este archivo).
# La cadena de respaldo importa: una misión puede NO tener vuelo RGB/térmico
# (solo multiespectral M3M) — sin bounds.json el geovisor no abre en ningún
# lado, así que se toma el centro del primer producto que exista.
src_for_center = next((p for p in (RGB_IN, THERMAL_IN, MS_IN, DSM_IN)
                        if os.path.exists(p)), RGB_IN)
if os.path.exists(src_for_center):
    ds = gdal.Open(src_for_center)
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    cx = gt[0] + gt[1] * w / 2
    cy = gt[3] + gt[5] * h / 2
    srs = osr.SpatialReference(); srs.ImportFromWkt(ds.GetProjection())
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tgt = osr.SpatialReference(); tgt.ImportFromEPSG(4326)
    tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lon, lat, _ = osr.CoordinateTransformation(srs, tgt).TransformPoint(cx, cy)
    # Resolución REAL de cada producto, para que el geovisor la muestre medida
    # en vez de escrita a mano. La fija el GSD del vuelo (altura y sensor), no
    # una constante: las leyendas tenían números fijos que no correspondían a
    # ninguna misión concreta.
    resoluciones = {}
    for clave, ruta in (("rgb", RGB_IN), ("thermal", THERMAL_IN),
                        ("dsm", DSM_IN), ("multispectral", MS_IN)):
        if os.path.exists(ruta):
            d = gdal.Open(ruta)
            resoluciones[clave] = round(abs(d.GetGeoTransform()[1]) * 100, 1)
            d = None
    with open(os.path.join(TILES_DIR, "bounds.json"), "w") as f:
        json.dump({"center": [lat, lon], "zoom": 17,
                    "thermal_range": [THERMAL_CLIP[0], THERMAL_CLIP[1]],
                    "resolucion_cm": resoluciones,
                    # Qué capas se tesela DE VERDAD en esta misión — de acá lee
                    # el geovisor para no ofrecer en el panel "Severidad",
                    # "Hotspot" o los índices clasificados cuando no hay
                    # multiespectral (o "Térmico" cuando no hubo vuelo H20T):
                    # antes esas entradas del panel eran incondicionales y
                    # prometían una capa sin tiles detrás.
                    "capas_disponibles": capas_disponibles,
                    "index_ranges": {k: [v[0], v[1]] for k, v in index_ranges.items()},
                    "ms_band_ranges": {k: [v[0], v[1]] for k, v in ms_band_ranges.items()}}, f)
    print(f"  centro: [{lat:.5f}, {lon:.5f}] -> {TILES_DIR}/bounds.json")
    ds = None

print(f"\n✅ Tiles en {TILES_DIR}/")
