#!/usr/bin/env python3
"""Detecta automáticamente el polígono del área afectada por el incendio,
a partir del ortomosaico multiespectral + térmico ya generados por ODM.

Metodología (validada cruzadamente en La Clara Caldas y Envigado, jul 2026
— ver "Validación cruzada" más abajo — contra un polígono de referencia
digitalizado a mano en cada misión):
  1. Población de referencia = píxeles con NDVI>0.5 en la misma misión
     (vegetación claramente sana, sin necesidad de dibujar nada a mano).
  2. z-score robusto (mediana/MAD) del brillo promedio de las 4 bandas
     multiespectrales, relativo a esa referencia — NO un umbral absoluto:
     así el criterio se autocalibra a la luz/vegetación de cada misión en
     vez de depender de un número fijo ajustado a una sola corrida.
     El brillo crudo separó mejor que NDVI/GNDVI/NDRE/SAVI/MSAVI/GEMI en
     las pruebas de esta sesión (AUC 0.880 vs 0.57-0.85) — normalizar en
     razón (como hace cualquier índice) tira la información de reflectancia
     absoluta, que es justo la señal que distingue ceniza de suelo desnudo.
     Corte: z>=2 — regla empírica 68-95-99.7 de control estadístico de
     procesos (2 sigma = desviación "notable"), NO un número tanteado
     contra este dataset.
  3. Temperatura ABSOLUTA (no anomalía relativa) como segunda confirmación
     — ver paper/CALIBRACION_TERMICA_CAMPO.md y compute_severity_classes.py
     para la justificación: la anomalía relativa da falsos positivos en
     suelo/cultivo calentado por el sol en días despejados (visto en las
     pruebas de esta sesión). Se usa un umbral bajo (40°C) solo para
     confirmar "más caliente que el entorno típico", no el umbral
     operacional de fuego activo (~88°C/190°F, ver compute_severity_classes.py)
     que sería demasiado estricto para detectar el área ya enfriada días
     después del incendio.
  4. Umbral doble por histéresis (semilla + crecimiento), NO un corte
     único fijo — validado contra dos polígonos de referencia digitalizados
     a mano (La Clara Caldas y Envigado) que resultaron necesitar cortes
     óptimos opuestos (z>=2 vs z>=1) con un solo umbral: la severidad
     espectral real de un incendio varía demasiado entre misiones para que
     un número fijo generalice.
       - "semilla" (alta confianza): Z_SEVERIDAD_MIN=2 (2-sigma) [+confirmación
         térmica si la misión tiene señal absoluta confiable].
       - "débil" (Z_SEVERIDAD_WEAK=1, el mismo corte que ya usa
         compute_severity_classes.py para la clase "leve"): NO se usa solo
         — solo se agrega si está conectado por vecindad-8 a una semilla.
  5. Filtro de vegetación viva (NDVI), para el problema que la histéresis
     por sí sola NO resuelve: un lote de cultivo cercano en La Clara Caldas
     tiene sus propios píxeles semilla (z>=2 por sombra/orientación), y la
     histéresis los hace crecer igual que al incendio real. Diagnóstico
     (esta sesión, ambas misiones): dentro del polígono de referencia
     digitalizado a mano, la ceniza real tiene NDVI mediana 0.25-0.31 (sin
     clorofila viva); los falsos positivos (cultivo/vegetación con sombra)
     están en 0.40-0.68 (vegetación viva). Dos filtros, calibrados juntos
     y validados por separado contra los dos polígonos de referencia
     (La Clara Caldas y Envigado — no solo contra uno):
       - techo NDVI_CEILING=0.6 en semilla Y en débil: mismo corte "densa y
         sana" que ya usa compute_severity_classes.py para clasificar
         índices, no un número nuevo.
       - SEED_NDVI_GROWTH_MAX=0.33: un componente semilla solo puede CRECER
         por histéresis si el NDVI PROMEDIO de sus propios píxeles semilla
         es <0.33 — si es más alto (posible cultivo/vegetación estresada,
         ambiguo), se queda solo con sus píxeles semilla, sin extenderse.
         Este valor sí es una calibración de datos (no una constante de la
         literatura como sigma/°C). Se subió de 0.30 a 0.33 a propósito
         (decisión explícita del usuario, no un óptimo matemático) para
         priorizar el recall de Envigado — su lóbulo norte real tenía NDVI
         semilla 0.310, justo debajo del corte anterior — aceptando que
         esto vuelve a colar más del cultivo de La Clara Caldas, que ahí se
         corrige a mano en el geovisor (capa vectorial editable) en vez de
         perseguir un óptimo espectral automático para ese caso.
  6. Limpieza morfológica: componente conexa principal + fragmentos cercanos
     (siguen siendo la misma mancha, solo separados por una franja delgada
     sin quemar) + relleno de huecos internos (copas sobrevivientes dentro
     de la mancha) + cierre leve del borde (sin inflar el contorno real).

Validación cruzada (IoU/recall/precisión contra polígono de referencia
digitalizado a mano, jul 2026 — con SEED_NDVI_GROWTH_MAX=0.33):
  - Envigado: IoU=0.89 (recall=0.94, precisión=0.95).
  - La Clara Caldas: IoU=0.64 (recall=0.78, precisión=0.78) — el corte más
    laxo casi no le costó nada (era 0.65 con el corte anterior de 0.30),
    pero el cultivo sigue coloreado en parte; se corrige a mano ahí en vez
    de perseguir un óptimo automático (ver arriba).

Intento descartado — filtro de TEXTURA en vez de NDVI: se probó reemplazar
la compuerta por componente por coherencia de orientación del gradiente
(banda roja: hileras de cultivo deberían dar bordes alineados en una
dirección, incendio real no). En una muestra pequeña (los 10 componentes
semilla más grandes) separaba de forma perfecta — pero al aplicarlo
completo, varios componentes de cultivo (más chicos, con zonas de suelo
desnudo/sombra dentro del lote) también dieron coherencia baja, e igual de
grave, muchos fragmentos reales de incendio con caja delimitadora chica
dieron coherencia alta por puro ruido estadístico (pocos píxeles para
estimar el tensor de gradiente). Resultado neto en La Clara Caldas: IoU
0.647→0.565, empeoró. Se descartó y se volvió a NDVI. Queda como
antecedente para no repetir el mismo experimento sin datos nuevos.

Limitación conocida: el gate de NDVI por componente reduce pero no elimina
el falso positivo de cultivo (algunos componentes de cultivo SÍ promedian
NDVI<0.30) — sigue siendo un filtro espectral, no uno de forma/textura
verdadero. Un filtro de textura que funcione tendría que operar sobre la
imagen completa en una ventana regular (no sobre la caja irregular que ya
definió el propio umbral, que es donde el intento de arriba falló) —
pendiente si se justifica la inversión.

Salida en EPSG:4326 (lon/lat, RFC 7946 — GeoJSON no tiene "crs" propio;
Leaflet en particular IGNORA cualquier miembro crs y asume siempre 4326,
así que escribir el polígono en la proyección UTM de la misión lo deja
invisible en el geovisor aunque el archivo sea válido). Quien necesite
rasterizarlo contra la grilla UTM de la misión (compute_severity_classes.py)
reproyecta al leerlo, no al revés.

Uso: python3 scripts/detect_area_afectada.py
Lee: outputs/multispectral_orthomosaic.tif, outputs/thermal_orthomosaic.tif
Escribe: outputs/area_afectada.geojson, outputs/_deteccion_data.npz
         (este último lo reusa compute_severity_classes.py, evita
         re-alinear térmico+multiespectral dos veces)
"""
import os
import sys
import numpy as np
from osgeo import gdal, ogr, osr
from scipy import ndimage

# Identificación de bandas por NOMBRE: se reusa tal cual la de
# compute_vegetation_indices.py en vez de duplicar la tabla de alias (las dos
# leen el MISMO multispectral_orthomosaic.tif — tenían que coincidir y no
# coincidían, ver _read_bands()). Mismo patrón de import que
# odm_progress_filter.py (sys.path del propio directorio), para que funcione
# tanto corriendo este script directo como importándolo desde /app.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compute_vegetation_indices import _find_band  # noqa: E402

gdal.UseExceptions()

MS_PATH = "outputs/multispectral_orthomosaic.tif"
TH_PATH = "outputs/thermal_orthomosaic.tif"
OUT_GEOJSON = "outputs/area_afectada.geojson"
CACHE_PATH = "outputs/_deteccion_data.npz"

Z_SEVERIDAD_MIN = 2.0    # semilla — 2 sigma, regla empírica 68-95-99.7, no tanteado
Z_SEVERIDAD_WEAK = 1.0   # crecimiento por histéresis — mismo corte de "leve"
                         # que usa compute_severity_classes.py, no un número nuevo
NDVI_CEILING = 0.6       # techo semilla+débil — mismo corte "densa y sana" de
                         # compute_severity_classes.py, no un número nuevo
SEED_NDVI_GROWTH_MAX = 0.33  # un componente semilla solo crece por histéresis
                             # si su NDVI semilla promedio es <esto (ver docstring:
                             # calibrado priorizando recall en Envigado — el lóbulo
                             # norte real quedaba en 0.310, justo debajo de este corte;
                             # sube el falso positivo de cultivo en La Clara Caldas,
                             # aceptado a propósito porque ahí se corrige a mano en
                             # el geovisor, ver docstring)
TEMP_MIN_ABS_C = 40.0    # confirmación mínima de calor, ver docstring
MERGE_DIST_M = 50.0
MIN_COMPONENT_M2 = 5.0
CLOSE_M = 1.0
SIMPLIFY_TOLERANCE_M = 1.5  # ver nota junto a SimplifyPreserveTopology() más abajo


def _align_to_grid(src_path, ref_gt, ref_proj, W, H, resample="bilinear"):
    opts = gdal.WarpOptions(
        format="MEM", xRes=ref_gt[1], yRes=abs(ref_gt[5]),
        outputBounds=(ref_gt[0], ref_gt[3] + H * ref_gt[5], ref_gt[0] + W * ref_gt[1], ref_gt[3]),
        dstSRS=ref_proj, resampleAlg=resample, width=W, height=H,
    )
    return gdal.Warp("", src_path, options=opts)


def _read_bands(ms_ds):
    """Bandas espectrales por NOMBRE + máscara alpha del ortomosaico MS.

    BUG REAL corregido acá: esto asumía posición fija (banda 1=Red, 2=Green,
    3=NIR, 4=RedEdge, 5=alpha), pero compute_vegetation_indices.py —que lee
    ESTE MISMO archivo— documenta explícitamente que "el orden de
    reconstruction.multi_camera de ODM no está garantizado" y por eso resuelve
    por GetDescription() desde siempre. Los dos scripts leían el mismo raster
    con criterios opuestos.

    Con un orden distinto al asumido, `brightness` (promedio de las 4) seguía
    saliendo bien —es simétrico— así que nada fallaba a la vista; pero el NDVI
    quedaba calculado con dos bandas cambiadas, y ese NDVI gobierna el techo
    NDVI_CEILING (semilla y débil) y la compuerta SEED_NDVI_GROWTH_MAX por
    componente. Es decir: el polígono de área afectada y toda la severidad que
    se calcula sobre él salían mal, en silencio y sin ningún error.

    Se exigen las 4 bandas: el z-score de brillo está calibrado sobre el
    promedio de las 4 (ver docstring del módulo), no sobre las que haya.
    """
    names = {}
    for logical in ("nir", "red", "green", "rededge"):
        idx = _find_band(ms_ds, logical)
        if idx is None:
            descs = [ms_ds.GetRasterBand(i + 1).GetDescription() or f"(sin nombre, banda {i+1})"
                     for i in range(ms_ds.RasterCount)]
            print(f"❌ ERROR: no se encontró la banda '{logical}' en {MS_PATH}.")
            print(f"   Bandas presentes: {descs}")
            print("   ODM las nombra desde el tag XMP Camera:BandName de cada TIFF del M3M.")
            sys.exit(1)
        names[logical] = idx
    bands = {k: ms_ds.GetRasterBand(v).ReadAsArray().astype(np.float32)
             for k, v in names.items()}

    # Alpha por interpretación de color, no por índice fijo — mismo criterio
    # que trim_multispectral() y compute_vegetation_indices.py.
    n = ms_ds.RasterCount
    if n > 1 and ms_ds.GetRasterBand(n).GetColorInterpretation() == gdal.GCI_AlphaBand:
        alpha = ms_ds.GetRasterBand(n).ReadAsArray()
    else:
        alpha = np.full(bands["nir"].shape, 255, dtype=np.uint8)
    return bands, alpha


def compute_z_scores():
    """Devuelve (z_severidad, temp_abs, valid, ndvi, gt, proj, W, H) — todo
    alineado a la grilla del multiespectral. Cachea en CACHE_PATH."""
    ms_ds = gdal.Open(MS_PATH)
    gt, proj = ms_ds.GetGeoTransform(), ms_ds.GetProjection()
    W, H = ms_ds.RasterXSize, ms_ds.RasterYSize

    bands, ms_alpha = _read_bands(ms_ds)
    red, green = bands["red"], bands["green"]
    nir, rededge = bands["nir"], bands["rededge"]
    brightness = (red + green + nir + rededge) / 4

    th_w = _align_to_grid(TH_PATH, gt, proj, W, H)
    th = th_w.GetRasterBand(1).ReadAsArray().astype(np.float32)
    th_alpha = th_w.GetRasterBand(2).ReadAsArray() if th_w.RasterCount >= 2 else np.full_like(th, 255)

    valid = (ms_alpha > 0) & (th_alpha > 0) & np.isfinite(nir) & np.isfinite(th) & (th < 1000)

    with np.errstate(invalid="ignore", divide="ignore"):
        ndvi = (nir - red) / (nir + red)
    ref_pop = valid & (ndvi > 0.5)
    if ref_pop.sum() < 1000:
        print(f"  ⚠ Población de referencia (NDVI>0.5) muy chica ({ref_pop.sum()} px) — "
              f"resultado puede no ser confiable en esta misión.")

    med_bright = np.median(brightness[ref_pop])
    mad_bright = np.median(np.abs(brightness[ref_pop] - med_bright))

    z_severidad = (med_bright - brightness) / (1.4826 * mad_bright)

    np.savez(CACHE_PATH, z_severidad=z_severidad, temp_abs=th, valid=valid, ndvi=ndvi, ref_pop_size=ref_pop.sum())
    return z_severidad, th, valid, ndvi, gt, proj, W, H


def detect_polygon(z_severidad, temp_abs, valid, ndvi, gt, proj, W, H):
    px = abs(gt[1])
    px_area = px ** 2

    # El umbral térmico absoluto (40°C) asume un incendio todavía caliente
    # al momento del vuelo. Si la misión se voló con el fuego ya frío (visto
    # en Envigado: máximo de toda la misión 40.8°C, prácticamente ningún
    # píxel cruza el corte), exigirlo bloquea la detección por completo aun
    # cuando la señal espectral (z_severidad) sí muestra una mancha real.
    # "Alcanza el umbral" exige una cantidad mínima de píxeles, no solo que
    # exista alguno — unos pocos píxeles sueltos calientes (ruido/artefacto
    # puntual) no son lo mismo que una región real todavía caliente.
    #
    # Respaldo cuando NO hay señal térmica confiable: se probó un percentil
    # de temperatura de la misma misión como sustituto y fue peor que no usar
    # térmico — con temperatura casi plana, ese percentil separa por RUIDO,
    # no por señal real, y termina rechazando la mayoría del área quemada
    # real (visto en Envigado: detección fragmentada y muy por debajo del
    # área visible a simple vista en el RGB). También se probó exigir
    # temp_abs>=40°C siempre (LCC lo cumple de sobra): no ayuda a discriminar
    # cultivo/suelo soleado de incendio real porque AMBOS llegan a esa
    # temperatura en un día despejado — es NDVI, no temperatura, lo que los
    # separa (ver punto 5). Por eso el térmico es una confirmación opcional
    # cuando existe, nunca el discriminador principal.
    #
    # Techo de NDVI en semilla y débil: rechaza vegetación viva (cultivo,
    # árboles con sombra) que casualmente cruza el corte de brillo — ver
    # docstring, calibrado y validado contra los dos polígonos de referencia.
    ndvi_ok = ndvi < NDVI_CEILING

    MIN_PX_ABS_THRESHOLD = 1000
    temp_valid = temp_abs[valid]
    if temp_valid.size and (temp_valid >= TEMP_MIN_ABS_C).sum() >= MIN_PX_ABS_THRESHOLD:
        seed = valid & (z_severidad >= Z_SEVERIDAD_MIN) & (temp_abs >= TEMP_MIN_ABS_C) & ndvi_ok
        metodo_desc = (f"semilla z_severidad>={Z_SEVERIDAD_MIN} (2-sigma) AND temp_abs>={TEMP_MIN_ABS_C}C "
                        f"AND ndvi<{NDVI_CEILING}")
    else:
        print(f"  ⚠ Menos de {MIN_PX_ABS_THRESHOLD}px llegan a {TEMP_MIN_ABS_C}°C (incendio ya frío al momento del "
              f"vuelo) — sin confirmación térmica confiable, se usa z_severidad solo.")
        seed = valid & (z_severidad >= Z_SEVERIDAD_MIN) & ndvi_ok
        metodo_desc = (f"semilla z_severidad>={Z_SEVERIDAD_MIN} (2-sigma) AND ndvi<{NDVI_CEILING} — sin "
                        f"confirmación térmica confiable en esta misión (<{MIN_PX_ABS_THRESHOLD}px sobre "
                        f"{TEMP_MIN_ABS_C}C)")

    # Histéresis: crece la semilla de alta confianza sobre píxeles del corte
    # débil (z>=1, "leve") SOLO si están conectados (vecindad-8) a una
    # semilla — evita que z_severidad solo (sin núcleo fuerte cerca) cuele
    # ruido disperso, pero sí extiende sobre gradiente real de severidad
    # espectral contiguo a una detección confirmada (ver docstring: la
    # mitad del área real de Envigado quedaba en z=[1,2), justo bajo el
    # corte único anterior, pero conectada a la mancha confirmada).
    weak = valid & (z_severidad >= Z_SEVERIDAD_WEAK) & ndvi_ok
    weak_lbl, weak_n = ndimage.label(weak, structure=np.ones((3, 3)))
    if weak_n == 0 or not seed.any():
        print("  ⚠ Ningún píxel superó el umbral — no se detectó área afectada.")
        return None
    seed_ids = np.unique(weak_lbl[seed])
    seed_ids = seed_ids[seed_ids != 0]

    # Compuerta por componente: un componente semilla+débil solo se deja
    # CRECER más allá de sus propios píxeles semilla si el NDVI PROMEDIO de
    # sus propios píxeles semilla es de ceniza inequívoca (<SEED_NDVI_GROWTH_MAX).
    # Se probó reemplazar esto por coherencia de orientación de textura (ver
    # docstring, sección "intento descartado") y no generalizó — se queda
    # con NDVI. Si el componente no pasa la compuerta, conserva sus píxeles
    # semilla originales (no desaparece del polígono, solo no se expande).
    seed_lbl = np.where(seed, weak_lbl, 0)
    ndvi_means = np.atleast_1d(ndimage.mean(ndvi, seed_lbl, seed_ids))
    grow_ids = seed_ids[ndvi_means < SEED_NDVI_GROWTH_MAX]
    no_grow_ids = seed_ids[ndvi_means >= SEED_NDVI_GROWTH_MAX]
    cand = np.isin(weak_lbl, grow_ids) | (seed & np.isin(weak_lbl, no_grow_ids))
    metodo_desc += (f", crecida por histeresis a z_severidad>={Z_SEVERIDAD_WEAK} AND ndvi<{NDVI_CEILING} "
                     f"conectado (vecindad-8), solo en componentes con NDVI semilla promedio <"
                     f"{SEED_NDVI_GROWTH_MAX}")

    lbl, n = ndimage.label(cand, structure=np.ones((3, 3)))
    if n == 0:
        print("  ⚠ Ningún píxel superó el umbral — no se detectó área afectada.")
        return None
    sizes = ndimage.sum(cand, lbl, range(1, n + 1))
    biggest_id = int(np.argmax(sizes)) + 1
    main = lbl == biggest_id
    dist_to_main = ndimage.distance_transform_edt(~main) * px

    merged = main.copy()
    for comp_id in range(1, n + 1):
        if comp_id == biggest_id:
            continue
        comp_mask = lbl == comp_id
        area = sizes[comp_id - 1] * px_area
        if area < MIN_COMPONENT_M2:
            continue
        if dist_to_main[comp_mask].min() > MERGE_DIST_M:
            continue
        merged |= comp_mask

    filled = ndimage.binary_fill_holes(merged)
    r_px = CLOSE_M / px
    dilated = ndimage.distance_transform_edt(~filled) <= r_px
    closed_out = ndimage.distance_transform_edt(~dilated) > r_px
    closed = filled | (dilated & ~closed_out)
    final_mask = ndimage.binary_fill_holes(closed).astype(np.uint8)

    mask_ds = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    mask_ds.SetGeoTransform(gt)
    mask_ds.SetProjection(proj)
    band = mask_ds.GetRasterBand(1)
    band.WriteArray(final_mask)

    tmp_ds = ogr.GetDriverByName("MEM").CreateDataSource("tmp")
    srs = osr.SpatialReference(wkt=proj)
    tmp_layer = tmp_ds.CreateLayer("tmp", srs, ogr.wkbPolygon)
    tmp_layer.CreateField(ogr.FieldDefn("val", ogr.OFTInteger))
    gdal.Polygonize(band, band, tmp_layer, 0, [], callback=None)

    union_geom = ogr.Geometry(ogr.wkbMultiPolygon)
    for feat in tmp_layer:
        union_geom = union_geom.Union(feat.GetGeometryRef())
    # gdal.Polygonize() traza un vértice en CADA transición de píxel del
    # contorno — un perímetro real sale con miles de vértices (7103 vistos
    # en Barbosa/la_clara), la escalera propia de un raster, no la forma del
    # incendio. La tolerancia anterior (px*0.5, media resolución de píxel:
    # ~4cm) era demasiado fina para limarla — apenas si quitaba puntos
    # colineales exactos. SIMPLIFY_TOLERANCE_M (1.5m) sí borra esa escalera
    # sin deformar el perímetro a la escala en que importa para una decisión
    # de respuesta a incendios, y de paso deja un polígono editable a mano
    # en el geovisor sin generar miles de marcadores de vértice (antes,
    # entrar a modo edición tardaba muchos segundos en construir un manejador
    # de arrastre POR CADA vértice — ver toggleAreaEdit()/buildEditHandles()
    # en geovisor/app.js).
    union_geom = union_geom.SimplifyPreserveTopology(SIMPLIFY_TOLERANCE_M)
    area_m2 = union_geom.Area()  # calculada en la SRS proyectada (metros), antes de reproyectar

    # Salida en EPSG:4326 (lon/lat) — ver docstring: Leaflet ignora "crs" y
    # asume siempre WGS84, así que escribir en la UTM de la misión deja el
    # polígono invisible en el geovisor. area_m2 ya quedó calculada arriba
    # en la proyección métrica original, no se recalcula en grados.
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_wgs84 = osr.CoordinateTransformation(srs, wgs84)
    out_geom = union_geom.Clone()
    out_geom.Transform(to_wgs84)

    out_path = OUT_GEOJSON
    if os.path.exists(out_path):
        os.remove(out_path)
    out_ds = ogr.GetDriverByName("GeoJSON").CreateDataSource(out_path)
    out_layer = out_ds.CreateLayer("area_afectada", wgs84, ogr.wkbMultiPolygon)
    out_layer.CreateField(ogr.FieldDefn("area_m2", ogr.OFTReal))
    out_layer.CreateField(ogr.FieldDefn("metodo", ogr.OFTString))
    feat = ogr.Feature(out_layer.GetLayerDefn())
    feat.SetGeometry(out_geom)
    feat.SetField("area_m2", area_m2)
    feat.SetField("metodo", f"{metodo_desc}, comp.principal+cercanas(<={MERGE_DIST_M}m), "
                             f"fill_holes+cierre {CLOSE_M}m")
    out_layer.CreateFeature(feat)
    out_ds = None
    return out_path, area_m2


def main():
    if not os.path.isfile(MS_PATH) or not os.path.isfile(TH_PATH):
        print(f"❌ ERROR: hacen falta {MS_PATH} y {TH_PATH} (¿corrió compute-indices?)")
        sys.exit(1)
    z_severidad, temp_abs, valid, ndvi, gt, proj, W, H = compute_z_scores()
    result = detect_polygon(z_severidad, temp_abs, valid, ndvi, gt, proj, W, H)
    if result is None:
        sys.exit(1)
    out_path, area = result
    print(f"✅ {out_path}: área detectada {area:.0f} m²")


if __name__ == "__main__":
    main()
