#!/usr/bin/env python3
"""Recorta los mosaicos (RGB, térmico nativo, multiespectral) y el DSM al
área REALMENTE fotografiada por el vuelo: el casco convexo de las huellas en
el suelo de TODAS las fotos que entraron a la reconstrucción SfM — no la
línea de vuelo ni las posiciones de cámara solas (el casco de esos CENTROS
subestimaría el área real cubierta, que se extiende medio ancho de foto más
allá de cada centro), la huella COMPLETA de cada foto proyectada al suelo.

Reemplaza el recorte anterior (piso de solape de cámaras + recorte de
"confiabilidad" con opening/closing morfológico, ~500 líneas de heurísticas
calibradas). Ese enfoque exigía VARIAS fotos superpuestas cubriendo cada
celda (REL_MIN_OVERLAP_*) para conservarla — así que el borde de CADA pasada
del vuelo, donde una franja angosta solo tiene la cobertura de UNA foto (sin
solape con la pasada vecina, típico en los bordes del patrón de vuelo o en
vuelos con poco solape lateral), quedaba recortado aunque esa foto la haya
fotografiado de verdad. Reportado en vivo: el mosaico térmico se estaba
recortando mucho más de lo que la cobertura real del vuelo justificaba.

El casco convexo no exige solape entre fotos: alcanza con que UNA sola foto
haya cubierto el punto para que quede adentro. Sigue partiendo del alpha
real de ODM (qué triángulo de la malla 3D se renderizó) como base — el casco
solo agrega el límite de EXTENSIÓN real del vuelo, no reemplaza esa señal.

trim_rgb/trim_multispectral/trim_thermal_native/trim_dsm comparten el mismo
cálculo de casco (_footprint_hull_mask), apuntando cada uno a su propio
reconstruction.json — el principio (huella real de cámara) no depende del
sensor.
"""
import os, json
import numpy as np
from osgeo import gdal, ogr, osr
from scipy import ndimage
from scipy.spatial import ConvexHull

gdal.UseExceptions()

RGB_PATH = os.environ.get("RGB_PATH", "outputs/rgb_orthomosaic.tif")
ODM_RGB_DIR = os.environ.get("ODM_RGB_DIR", "processing/rgb_odm")
RGB_ODM_SRC = os.path.join(ODM_RGB_DIR, "odm_orthophoto/odm_orthophoto.tif")  # alpha real de ODM
RGB_RECON   = os.path.join(ODM_RGB_DIR, "opensfm/reconstruction.json")        # poses + intrínsecas
DSM_PATH    = os.environ.get("DSM_PATH", "outputs/dsm.tif")
TH_PATH  = "outputs/thermal_orthomosaic.tif"
ODM_MS_DIR = os.environ.get("ODM_MS_DIR", "processing/multispectral_odm")
MS_ODM_SRC = os.path.join(ODM_MS_DIR, "odm_orthophoto/odm_orthophoto.tif")
MS_RECON   = os.path.join(ODM_MS_DIR, "opensfm/reconstruction.json")
MS_PATH    = "outputs/multispectral_orthomosaic.tif"
ODM_THNAT_DIR = os.environ.get("ODM_THNAT_DIR", "processing/thermal_native_odm")
THNAT_ODM_SRC = os.path.join(ODM_THNAT_DIR, "odm_orthophoto/odm_orthophoto.tif")
THNAT_RECON   = os.path.join(ODM_THNAT_DIR, "opensfm/reconstruction.json")
# Solo RESPALDO, para el caso de un raster sin proyección legible: la CRS real
# se deriva del propio raster que se está recortando (ver _target_crs).
UTM_EPSG = 32618

# Cascos convexos exportados como GeoJSON (ver _footprint_hull_mask), una
# capa más del geovisor (grupo "🛩️ Vuelo") — permite validar a simple vista
# si el recorte real coincide con el casco esperado, en vez de inferirlo de
# la forma del mosaico. DSM reusa el casco de RGB (mismo RGB_RECON, ver
# trim_dsm), así que no tiene un archivo propio.
HULL_RGB_PATH = "outputs/hull_rgb.geojson"
HULL_THERMAL_PATH = "outputs/hull_thermal.geojson"
HULL_MS_PATH = "outputs/hull_multispectral.geojson"

# Cotas físicas del sensor térmico (°C) — descarta valores fuera de rango
# plausible como artefacto de blending/textura, no superficie real. Rango
# amplio a propósito: en misiones de incendio los focos reales superan 60°C.
# Sin relación con el recorte espacial (casco convexo, más abajo): esto es
# limpieza de VALORES, no de extensión.
THNAT_HARD_LO, THNAT_HARD_HI = -20.0, 150.0
THNAT_DESPIKE_WIN = 9      # px: ventana de mediana para detectar spikes puntuales
THNAT_DESPIKE_DEV = 10.0   # °C: desviación vs mediana local = spike (reemplaza por mediana)


def _target_crs(proj_wkt):
    """CRS a la que se proyectan las huellas de cámara: la del PROPIO raster
    que se está recortando, no una zona UTM fija.

    Tiene que ser la del raster porque las huellas se comparan contra su
    geotransform, que ODM escribe en la UTM real de la misión. Con una zona
    fija, cualquier misión fuera de esa zona pone las huellas a cientos de
    kilómetros del raster: la máscara del casco daría 0 en todas partes sin
    lanzar ninguna excepción, y el recorte dejaría el mosaico entero vacío
    sin ningún aviso claro del motivo.
    """
    from pyproj import CRS
    if proj_wkt:
        try:
            return CRS.from_wkt(proj_wkt)
        except Exception as exc:
            print(f"  ⚠ no se pudo leer la proyección del raster ({exc}) — "
                  f"se cae al respaldo EPSG:{UTM_EPSG}")
    else:
        print(f"  ⚠ el raster no trae proyección — se usa el respaldo EPSG:{UTM_EPSG}")
    return CRS.from_epsg(UTM_EPSG)


def _aa_to_R(aa):
    x, y, z = aa
    ang = np.sqrt(x*x + y*y + z*z)
    if ang < 1e-12:
        return np.eye(3)
    ax = np.array([x, y, z]) / ang
    c, s = np.cos(ang), np.sin(ang); t = 1 - c; ux, uy, uz = ax
    return np.array([[c+ux*ux*t, ux*uy*t-uz*s, ux*uz*t+uy*s],
                     [uy*ux*t+uz*s, c+uy*uy*t, uy*uz*t-ux*s],
                     [uz*ux*t-uy*s, uz*uy*t+ux*s, c+uz*uz*t]])


RGB_OV_ZBANDS = 5  # nº de bandas de elevación para el solape de cámaras.
                   # Terreno con relieve real (montañoso) invalida el supuesto
                   # de "una sola Z de terreno" — proyectar con una Z plana
                   # única desalinea la huella de cámara en zonas de elevación
                   # distinta a la mediana, marcando ov=0 en área BIEN
                   # reconstruida solo porque está más alto/bajo que la
                   # mediana. 5 bandas por percentil de Z real alcanza para
                   # terrenos con cientos de metros de relieve sin volverse
                   # costoso (5× las proyecciones, no por-celda).
RGB_OV_COARSE = 16  # factor de submuestreo para el conteo de solape (rápido; ~1.8m/celda)
NADIR_MAX_TILT_DEG = 30  # inclinación máxima (respecto a mirar derecho hacia
                         # abajo) para considerar una foto "nadir".


def _rgb_camera_overlap(gt, W, H, recon_path=None, proj_wkt=None):
    """Mapas de solape de cámaras (nº de fotos cuya huella en el suelo cubre
    cada celda) — YA NO lo usa el recorte de bordes (ver _footprint_hull_mask
    más abajo, que reemplazó el piso de solape por el casco convexo), pero
    scripts/compute_flight_quality.py SÍ sigue necesitando esto: la mediana
    de fotos por celda (solape_p50) es una métrica real de calidad del
    levantamiento, distinta de "hasta dónde recortar" — sigue viviendo acá
    porque es la misma huella de cámara/poses SfM que ya se parsea en este
    módulo, no tiene sentido duplicarlo en otro archivo.

    Proyecta las 4 esquinas de cada foto al plano del terreno usando las
    poses SfM y rasteriza el cuadrilátero. Devuelve
    (solape_full, solape_central, solape_nadir, factor): el COMPLETO, el
    CENTRAL (esquinas hacia adentro, análogo a n_contrib) y el NADIR (solo
    fotos con inclinación ≤NADIR_MAX_TILT_DEG). Si falta reconstruction.json,
    devuelve (None, None, None, None).

    Con terreno de relieve real (no plano) una sola Z global desalinea la
    huella de cámara lejos de esa Z — se proyecta por BANDAS de elevación
    (percentiles de la Z real del DSM) y cada celda usa el resultado de la
    banda a la que pertenece su propia Z local (ver RGB_OV_ZBANDS).
    """
    recon_path = recon_path if recon_path is not None else RGB_RECON
    if not os.path.isfile(recon_path):
        return None, None, None, None
    from pyproj import Transformer
    with open(recon_path) as f:
        data = json.load(f)
    rec = data[0] if isinstance(data, list) else data
    cam = list(rec["cameras"].values())[0]
    w, h = cam["width"], cam["height"]; sc = max(w, h)
    if "focal_x" in cam:
        fx = cam["focal_x"] * sc; fy = cam.get("focal_y", cam["focal_x"]) * sc
    else:
        fx = fy = cam["focal"] * sc
    cx = cam.get("c_x", 0.0) * sc + w/2; cy = cam.get("c_y", 0.0) * sc + h/2
    tr = Transformer.from_crs("EPSG:4326", _target_crs(proj_wkt), always_xy=True)
    ref = rec["reference_lla"]; ref_e, ref_n = tr.transform(ref["longitude"], ref["latitude"])

    cf = RGB_OV_COARSE; gw, gh = W//cf, H//cf; res = gt[1]*cf; x0, y0 = gt[0], gt[3]
    gx = x0 + (np.arange(gw)+0.5)*res; gy = y0 - (np.arange(gh)+0.5)*res

    z_local = np.zeros((gh, gw), np.float32)
    z_bands = np.array([0.0])
    if os.path.isfile(DSM_PATH):
        dd = gdal.Open(DSM_PATH)
        zsrc = dd.GetRasterBand(1).ReadAsArray(buf_xsize=gw, buf_ysize=gh).astype(np.float32)
        dd = None
        bad = ~np.isfinite(zsrc) | (zsrc < -500)
        if bad.any() and (~bad).any():
            zsrc[bad] = np.median(zsrc[~bad])
        z_local = zsrc
        zvalid = z_local[np.isfinite(z_local)]
        if zvalid.size:
            z_bands = np.unique(np.percentile(zvalid, np.linspace(0, 100, RGB_OV_ZBANDS + 1)))
    if z_bands.size < 2:
        z_bands = np.array([float(z_local.mean()) - 1, float(z_local.mean()) + 1])
    band_idx = np.clip(np.digitize(z_local, z_bands[1:-1]), 0, len(z_bands) - 2)
    band_reps = [(z_bands[b] + z_bands[b+1]) / 2 for b in range(len(z_bands) - 1)]

    ctr = np.array([w/2.0, h/2.0])
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], float)
    corners_c = ctr + 0.55 * (corners - ctr)

    def _project(R, Cu, corner_set, Zg):
        q = []
        for (u, v) in corner_set:
            d = R.T @ np.array([(u-cx)/fx, (v-cy)/fy, 1.0])
            if abs(d[2]) < 1e-9:
                return None
            g = Cu + (Zg - Cu[2]) / d[2] * d
            q.append((g[0], g[1]))
        return np.array(q)

    def _accum(acc, q):
        c0 = max(0, int((q[:,0].min()-x0)/res)); c1 = min(gw, int((q[:,0].max()-x0)/res)+1)
        r0 = max(0, int((y0-q[:,1].max())/res)); r1 = min(gh, int((y0-q[:,1].min())/res)+1)
        if c0 >= c1 or r0 >= r1:
            return
        XX, YY = np.meshgrid(gx[c0:c1], gy[r0:r1])
        for sign in (1, -1):  # winding-agnostic point-in-quad
            inside = np.ones(XX.shape, bool)
            for k in range(4):
                x1, y1 = q[k]; x2, y2 = q[(k+1) % 4]
                inside &= sign*((x2-x1)*(YY-y1) - (y2-y1)*(XX-x1)) >= 0
            if inside.any():
                acc[r0:r1, c0:c1] += inside
                return

    def _tilt_deg(R):
        d = R.T @ np.array([0.0, 0.0, 1.0])
        return np.degrees(np.arccos(np.clip(-d[2], -1.0, 1.0)))

    shots = list(rec["shots"].values())
    ov = np.zeros((gh, gw), np.int32); ovc = np.zeros((gh, gw), np.int32)
    ovn = np.zeros((gh, gw), np.int32)
    for b, Zg in enumerate(band_reps):
        band_mask = band_idx == b
        if not band_mask.any():
            continue
        ov_b = np.zeros((gh, gw), np.int32); ovc_b = np.zeros((gh, gw), np.int32)
        ovn_b = np.zeros((gh, gw), np.int32)
        for shot in shots:
            R = _aa_to_R(shot["rotation"]); t = np.array(shot["translation"]); C = -R.T @ t
            Cu = np.array([ref_e + C[0], ref_n + C[1], C[2]])
            qf = _project(R, Cu, corners, Zg)
            if qf is not None:
                _accum(ov_b, qf)
                if _tilt_deg(R) <= NADIR_MAX_TILT_DEG:
                    _accum(ovn_b, qf)
            qc = _project(R, Cu, corners_c, Zg)
            if qc is not None:
                _accum(ovc_b, qc)
        ov[band_mask] = ov_b[band_mask]
        ovc[band_mask] = ovc_b[band_mask]
        ovn[band_mask] = ovn_b[band_mask]
    g = lambda x: ndimage.gaussian_filter(x.astype(np.float32), 2)
    return g(ov), g(ovc), g(ovn), cf


def _reference_agl_m(dsm_path=DSM_PATH):
    """AGL (altura real de vuelo sobre el terreno) de referencia para
    sensores cuyo PROPIO reconstruction.json no está alineado con la
    elevación real del DSM (ver z_ref en _footprint_hull_mask) — reportado
    en vivo: térmico y RGB son el MISMO dron a la MISMA altura real en el
    MISMO vuelo (cámara dual H20T), pero solo RGB traía --dsm y por lo tanto
    su reconstruction.json quedaba en la elevación real; térmico usaba un
    valor fijo (80 m) inventado sin relación con la altura real (~219 m en
    esa misión), y eso desalineaba su casco frente al de RGB en vez de
    achicarlo correctamente.

    Se deriva de RGB_RECON (o MS_RECON si no hay RGB) — el que SÍ suele
    alinear con el DSM — y se reusa para cualquier otro sensor de la MISMA
    misión, ya que todos vuelan a la vez. Devuelve None si ninguno de los
    dos alinea (misión sin RGB ni multiespectral, o sin DSM)."""
    for recon_path in (RGB_RECON, MS_RECON):
        if not os.path.isfile(recon_path) or not os.path.isfile(dsm_path):
            continue
        try:
            with open(recon_path) as f:
                data = json.load(f)
            rec = data[0] if isinstance(data, list) else data
            if not rec.get("shots"):
                continue
            all_Cz = []
            for shot in rec["shots"].values():
                R = _aa_to_R(shot["rotation"]); t = np.array(shot["translation"])
                all_Cz.append(float((-R.T @ t)[2]))
            cam_z_median = float(np.median(all_Cz))
            dd = gdal.Open(dsm_path)
            zarr = dd.GetRasterBand(1).ReadAsArray()
            dd = None
            zvalid = zarr[np.isfinite(zarr) & (zarr > -500)]
            if not zvalid.size:
                continue
            dsm_lo, dsm_hi = float(zvalid.min()), float(zvalid.max())
            if dsm_lo - 500.0 <= cam_z_median <= dsm_hi + 2000.0:
                return cam_z_median - float(np.median(zvalid))
        except Exception:
            continue
    return None


def _footprint_hull_mask(recon_path, gt, W, H, proj_wkt, dsm_path=DSM_PATH, buffer_m=None,
                          geojson_out=None, agl_hint_m=None):
    """Máscara booleana (H,W): True donde cae DENTRO del casco convexo de las
    huellas en el suelo de TODAS las fotos que entraron a la reconstrucción
    SfM (recon_path) — el área REALMENTE fotografiada, no una aproximación
    por densidad de solape entre fotos vecinas.

    Usa la huella COMPLETA de cada foto (las 4 esquinas del sensor
    proyectadas al suelo, no una versión achicada hacia el centro) para que
    el casco cubra la zona que la foto de verdad capturó — incluida la
    franja de borde de cada pasada que solo tiene UNA foto encima, sin
    solape con la pasada vecina, que el criterio anterior (piso de solape de
    cámaras) descartaba aunque estuviera bien fotografiada.

    buffer_m: margen agregado al casco antes de rasterizar, además de la
    huella completa (no una reducción) — evita perder borde real fotografiado
    tanto por el redondeo de rasterizar un polígono a grilla como por el
    error de la Z de referencia única (mediana de la altura de cámara menos
    un AGL típico, ver AGL_TIPICO_M más abajo — no la Z real bajo cada
    esquina) con la que se proyectan las huellas: en terreno con relieve,
    esa aproximación corre el contorno del casco unos metros. Reportado en
    vivo: con un margen de apenas 2 px el mosaico térmico se seguía viendo
    recortado. Default 3 m o 10 px del raster destino (lo mayor de los
    dos) — generoso a propósito:
    un margen de más solo agrega el borde real que la huella ya cubrió, no
    reintroduce el fleco de baja calidad que el recorte anterior sacaba
    (ese lo sigue filtrando el alpha real de ODM, no este casco).

    geojson_out: si se pasa, además escribe el polígono del casco (el mismo
    que se rasteriza, con el buffer ya aplicado) en EPSG:4326 — para que el
    geovisor lo pueda mostrar como capa y así VALIDAR a simple vista si el
    recorte real coincide con el casco esperado, en vez de inferirlo de la
    forma del mosaico. Reportado en vivo: sin esto, un mosaico que se ve mal
    recortado no dice si el problema es el casco en sí (mal calculado) o
    algo aguas abajo (alpha crudo de ODM, filtro de parches sueltos).

    Devuelve None si no hay reconstruction.json (sin SfM no hay huellas que
    calcular — el caller cae al alpha crudo de ODM sin este recorte extra).
    """
    if not os.path.isfile(recon_path):
        return None
    from pyproj import Transformer
    with open(recon_path) as f:
        data = json.load(f)
    rec = data[0] if isinstance(data, list) else data
    if not rec.get("shots"):
        return None
    cam = list(rec["cameras"].values())[0]
    w, h = cam["width"], cam["height"]; sc = max(w, h)
    # Modelo de cámara OpenSfM: "brown" (RGB, con focal_x/focal_y y
    # descentrado c_x/c_y) vs "perspective"/"simple_pinhole" (multiespectral
    # M3M: una sola "focal", sin descentrado — c_x/c_y quedan en 0).
    if "focal_x" in cam:
        fx = cam["focal_x"] * sc; fy = cam.get("focal_y", cam["focal_x"]) * sc
    else:
        fx = fy = cam["focal"] * sc
    cx = cam.get("c_x", 0.0) * sc + w / 2; cy = cam.get("c_y", 0.0) * sc + h / 2
    target_wkt = _target_crs(proj_wkt).to_wkt()
    tr = Transformer.from_crs("EPSG:4326", target_wkt, always_xy=True)
    ref = rec["reference_lla"]; ref_e, ref_n = tr.transform(ref["longitude"], ref["latitude"])

    # Z de referencia para proyectar las esquinas al suelo. Reportado en vivo
    # (bug real): en una misión real, RGB y multiespectral tenían Cu[2]
    # (altura de cámara en el frame topocéntrico de CADA reconstruction.json)
    # ~1988 m, cerca de la elevación real del DSM (~1769-1963 m, AGL
    # ~220-240 m, plausible) — pero el térmico tenía Cu[2] entre -4 y 133 m
    # (mediana ~2 m), un frame COMPLETAMENTE distinto, aunque los tres
    # reconstruction.json declaran el mismo reference_lla.altitude=0.0. Usar
    # la mediana del DSM (~1769 m) como z_ref para el térmico desalineaba la
    # Z casi 1770 m — cada esquina se proyectaba lejísimos, e inflaba el
    # casco a ~19.400 ha (para un incendio de pocas hectáreas).
    #
    # Cada sensor reconstruye su Z en SU PROPIO frame — a veces alineado con
    # la elevación real del DSM (post-georreferenciación de ODM), a veces
    # no. Se DETECTA la alineación en vez de asumirla: si la mediana de
    # Cu[2] cae dentro de un margen generoso sobre el techo del DSM (hasta
    # 2 km — cualquier AGL real de un vuelo de mapeo), se usa la mediana del
    # DSM (terreno real, más preciso). Si no, el DSM no está en el mismo
    # frame que este sensor — se cae a un z_ref autoconsistente derivado de
    # las propias posiciones de cámara (mediana de Cu[2] menos un AGL típico
    # de vuelo de mapeo). Ninguno de los dos necesita ser exacto: un error
    # de Z solo corre el CONTORNO del casco unos metros (ver buffer_m), no
    # cambia sistemáticamente si un punto cae adentro o afuera.
    AGL_TIPICO_M = agl_hint_m if agl_hint_m is not None else 80.0

    # Las posiciones de cámara son confiables TAL CUAL — OpenSfM las ancla con
    # el GPS del EXIF como prior, y confirmado en vivo sobre una misión real:
    # el bbox de posiciones de RGB y del térmico (los dos sensores de esta
    # misión) cae en el mismo rango, sin ninguna pose perdida a kilómetros de
    # las demás. El intento anterior de "robustecer" esto con estadística
    # tipo MAD (asumiendo una distribución ~normal) resultó CONTRAPRODUCENTE:
    # las posiciones de un vuelo en grilla son casi UNIFORMES a lo largo de
    # cada pasada, no concentradas alrededor de una mediana como una normal,
    # así que 1.4826×MAD sobreestima muchísimo el desvío real — reportado en
    # vivo con capas de validación: un bbox real de ~550×650 m terminaba con
    # una región "robusta" de ~1660×1980 m, casi 3× más ancha en cada eje.
    # Se usa directo el bbox real (min/max) de las posiciones de cámara, sin
    # ensanchar con ninguna fórmula estadística.
    all_C = []
    all_Cz = []
    for shot in rec["shots"].values():
        R = _aa_to_R(shot["rotation"]); t = np.array(shot["translation"])
        C = -R.T @ t
        all_C.append((ref_e + C[0], ref_n + C[1]))
        all_Cz.append(C[2])
    all_C = np.array(all_C)
    cam_z_median = float(np.median(all_Cz))
    z_ref = cam_z_median - AGL_TIPICO_M  # respaldo: frame propio de este sensor
    if os.path.isfile(dsm_path):
        dd = gdal.Open(dsm_path)
        zarr = dd.GetRasterBand(1).ReadAsArray()
        dd = None
        zvalid = zarr[np.isfinite(zarr) & (zarr > -500)]
        if zvalid.size:
            dsm_lo, dsm_hi = float(zvalid.min()), float(zvalid.max())
            # Alineado = la cámara está razonablemente ENCIMA del terreno
            # real, no muy por debajo (eso es justo la señal de un frame
            # distinto, como el térmico con Cu[2]~2m contra un DSM de
            # ~1769m — muy por debajo del terreno más bajo, imposible para
            # una cámara real) ni absurdamente por encima.
            if dsm_lo - 500.0 <= cam_z_median <= dsm_hi + 2000.0:
                z_ref = float(np.median(zvalid))
    # Bbox real de cámaras, SIN margen: ya son posiciones confiables (ver el
    # comentario de arriba) — un colchón "por las dudas" solo vuelve a
    # inflar el casco. Solo se usa para el guard de POSICIÓN de cámara (una
    # pose con traslación garbage), no para las esquinas.
    lo_cam, hi_cam = all_C.min(axis=0), all_C.max(axis=0)

    # Las esquinas NO se validan contra un margen fijo estimado a mano (eso
    # es justo lo que salió mal: una fórmula que mezclaba GSD del raster de
    # SALIDA con la cuenta de píxeles del SENSOR sobreestimaba mucho la
    # huella real de RGB —sensor de 4056×3040— frente a la del térmico
    # —640×512—, aunque las dos cámaras vuelan la MISMA altura real.
    # Reportado en vivo: con esa fórmula el casco de RGB quedaba visiblemente
    # más ancho que el del térmico sobre la MISMA misión). Cada esquina YA SE
    # PROYECTA con el modelo de cámara real (foco, rotación, altura) — su
    # propia distancia a SU cámara, medida después de proyectar, es la huella
    # real de ESA esquina. Se valida esa distancia contra un múltiplo
    # generoso del AGL de esa cámara (agl = altura real sobre el terreno):
    # con FOV ancho + oblicuidad realista la esquina más lejana de una foto
    # real no pasa de un par de veces el AGL; CORNER_AGL_MULT=5 da margen de
    # sobra para cualquier geometría real sin dejar pasar una proyección
    # degenerada (que en la práctica salía a cientos de AGL de distancia).
    CORNER_AGL_MULT = 2.5

    # Achicamos la huella proyectada (ej. 85% del tamaño real) para ignorar
    # los bordes muy oblicuos de cada foto que causan "smears" en el borde del mosaico.
    SHRINK = 0.60
    ctr = np.array([w / 2.0, h / 2.0])
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], float)
    corners = ctr + SHRINK * (corners - ctr)
    pts = []
    for shot in rec["shots"].values():
        R = _aa_to_R(shot["rotation"]); t = np.array(shot["translation"]); C = -R.T @ t
        Cu = np.array([ref_e + C[0], ref_n + C[1], C[2]])
        if not (lo_cam[0] <= Cu[0] <= hi_cam[0] and lo_cam[1] <= Cu[1] <= hi_cam[1]):
            continue  # la cámara misma cae fuera de la región robusta de vuelo
        agl = max(1.0, abs(Cu[2] - z_ref))
        max_corner_dist = CORNER_AGL_MULT * agl
        for (u, v) in corners:
            d = R.T @ np.array([(u - cx) / fx, (v - cy) / fy, 1.0])
            if abs(d[2]) < 0.05:  # rayo casi horizontal: sin intersección de suelo confiable
                continue
            g = Cu + (z_ref - Cu[2]) / d[2] * d
            if np.hypot(g[0] - Cu[0], g[1] - Cu[1]) > max_corner_dist:
                continue  # esquina proyectada más lejos de lo que cualquier huella real llega
            pts.append((g[0], g[1]))
    if len(pts) < 3:
        return None
    pts = np.array(pts)

    hull = ConvexHull(pts)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for i in hull.vertices:  # scipy ya los da en orden (antihorario en 2D)
        ring.AddPoint(float(pts[i, 0]), float(pts[i, 1]))
    ring.CloseRings()
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    if buffer_m is None:
        buffer_m = max(3.0, 10 * abs(gt[1]))
    if buffer_m:
        poly = poly.Buffer(buffer_m)

    if geojson_out:
        os.makedirs(os.path.dirname(geojson_out) or ".", exist_ok=True)
        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        to_wgs84 = osr.CoordinateTransformation(osr.SpatialReference(wkt=target_wkt), wgs84)
        out_geom = poly.Clone()
        out_geom.Transform(to_wgs84)
        if os.path.exists(geojson_out):
            os.remove(geojson_out)
        out_ds = ogr.GetDriverByName("GeoJSON").CreateDataSource(geojson_out)
        out_layer = out_ds.CreateLayer("hull", wgs84, ogr.wkbPolygon)
        out_layer.CreateField(ogr.FieldDefn("area_m2", ogr.OFTReal))
        feat = ogr.Feature(out_layer.GetLayerDefn())
        feat.SetGeometry(out_geom)
        feat.SetField("area_m2", poly.Area())
        out_layer.CreateFeature(feat)
        out_ds = None
        try:
            os.chmod(geojson_out, 0o666)
        except OSError:
            pass

    mem_ds = ogr.GetDriverByName("MEM").CreateDataSource("hull")
    srs = osr.SpatialReference(wkt=target_wkt)
    layer = mem_ds.CreateLayer("hull", srs, ogr.wkbPolygon)
    feat = ogr.Feature(layer.GetLayerDefn())
    feat.SetGeometry(poly)
    layer.CreateFeature(feat)

    mask_ds = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    mask_ds.SetGeoTransform(gt)
    mask_ds.SetProjection(proj_wkt or target_wkt)
    gdal.RasterizeLayer(mask_ds, [1], layer, burn_values=[1])
    mask = mask_ds.GetRasterBand(1).ReadAsArray().astype(bool)
    mask_ds = None
    return mask


def _apply_hull(valid, recon_path, gt, W, H, proj, label, geojson_out=None, agl_hint_m=None):
    """valid & (dentro del casco convexo de huellas) — el ÚNICO criterio
    espacial de recorte: nada de pisos de solape, nada de filtrado de
    parches sueltos por tamaño. El casco es la única autoridad sobre qué
    extensión es "área realmente fotografiada"; lo que quede alpha=255 de
    ODM adentro del casco se conserva tal cual, sea o no parte del mismo
    componente conexo (un incendio real casi nunca es un solo blob —
    cortafuegos, caminos, parches salteados — así que imponer conectividad
    descartaría área real).

    Si no hay reconstruction.json (sensor sin SfM propio, por ejemplo un
    producto reusado sin recorte pedido) se deja `valid` tal cual, con
    aviso — sin casco no hay con qué recortar."""
    mask = _footprint_hull_mask(recon_path, gt, W, H, proj, geojson_out=geojson_out,
                                agl_hint_m=agl_hint_m)
    if mask is None:
        print(f"  ⚠ {recon_path} no encontrado — sin recorte por casco convexo ({label})")
        return valid
    before = int(valid.sum())
    after_check = int((valid & mask).sum())
    if before > 0 and after_check == 0:
        print(f"  ⚠ El casco convexo ({label}) no se superpone con los píxeles válidos ({before:,} px) — conservando ortomosaico original")
        return valid
    valid = valid & mask
    after_hull = int(valid.sum())
    print(f"  casco convexo de huellas ({label}): −{before - after_hull:,} px "
          f"fuera del área realmente fotografiada")
    return valid


def trim_rgb():
    # Fuente: el ortofoto crudo de ODM con su alpha real de malla 3D (no el
    # producto ya procesado — siempre se recalcula desde la fuente original).
    src = RGB_ODM_SRC if os.path.isfile(RGB_ODM_SRC) else RGB_PATH
    ds = gdal.Open(src)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    full = ds.ReadAsArray()
    a = full[:3]
    W, H = ds.RasterXSize, ds.RasterYSize
    if full.shape[0] >= 4:
        valid = full[3] == 255
    else:
        valid = a.mean(axis=0) >= 10  # respaldo si no hay alpha disponible
    before = int(valid.sum())

    valid = _apply_hull(valid, RGB_RECON, gt, W, H, proj, "RGB", geojson_out=HULL_RGB_PATH)
    after = int(valid.sum())
    print(f"RGB: válidos antes={before:,} ({100*before/valid.size:.1f}%) "
          f"después={after:,} ({100*after/valid.size:.1f}%)  "
          f"recortado={100*(before-after)/max(before,1):.2f}%")

    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(RGB_PATH, W, H, 4, gdal.GDT_Byte,
                      ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    # libtiff congela tags baseline como ExtraSamples apenas se escribe la
    # primera strip/tile del archivo — SetColorInterpretation(alpha) tiene
    # que ir ANTES de cualquier WriteArray, no después (si no, falla con
    # "Cannot modify tag ... while writing" en formatos sin heurística RGBA
    # implícita; acá funciona por casualidad porque GDAL asume alpha en la
    # 4ta banda Byte de un GeoTIFF de 4 bandas, ver trim_multispectral()).
    out.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    for b in range(3):
        out.GetRasterBand(b + 1).WriteArray(a[b])
    out.GetRasterBand(4).WriteArray((valid * 255).astype(np.uint8))
    out = None
    print(f"✅ {RGB_PATH} (RGBA)")


def trim_multispectral():
    """Recorta al casco convexo de huellas el ortomosaico multiespectral
    (M3M), mismo criterio que trim_rgb() — reusa _apply_hull() apuntando al
    reconstruction.json del proyecto ODM multiespectral (el principio físico,
    huella de cámara, no depende de si el sensor es RGB o multiespectral).

    El ortofoto de ODM multiespectral es un GeoTIFF Float32 multibanda (una
    banda por canal espectral + alpha), con el nombre de banda en
    GetDescription() (p.ej. "Green"/"Red"/"RedEdge"/"NIR" — ODM los agrupa
    automáticamente vía el tag XMP Camera:BandName, nada que armar acá).
    """
    if not os.path.isfile(MS_ODM_SRC):
        print(f"  ⚠ {MS_ODM_SRC} no encontrado — sin recorte multiespectral")
        return
    ds = gdal.Open(MS_ODM_SRC)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize
    full = ds.ReadAsArray().astype(np.float32)
    if full.ndim == 2:
        full = full[np.newaxis, ...]
    n_total = full.shape[0]

    has_alpha = n_total > 1 and (
        ds.GetRasterBand(n_total).GetColorInterpretation() == gdal.GCI_AlphaBand)
    
    # Igual que en RGB, ignoramos el alpha estricto de ODM para evitar fragmentación
    # en modo urgencia, basando la validez puramente en que existan datos espectrales.
    if has_alpha:
        spectral = full[:-1]
    else:
        spectral = full
    valid = np.isfinite(spectral).all(axis=0) & (np.nanmean(spectral, axis=0) > 0)
    
    band_names = [ds.GetRasterBand(i + 1).GetDescription() or f"band{i+1}"
                  for i in range(spectral.shape[0])]
    ds = None
    before = int(valid.sum())

    valid = _apply_hull(valid, MS_RECON, gt, W, H, proj, "MS", geojson_out=HULL_MS_PATH,
                        agl_hint_m=_reference_agl_m())
    after = int(valid.sum())
    print(f"Multiespectral: válidos antes={before:,} ({100*before/valid.size:.1f}%) "
          f"después={after:,} ({100*after/valid.size:.1f}%)  "
          f"recortado={100*(before-after)/max(before,1):.2f}%")

    drv = gdal.GetDriverByName("GTiff")
    nb = spectral.shape[0]
    out = drv.Create(MS_PATH, W, H, nb + 1, gdal.GDT_Float32,
                      ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    # SetColorInterpretation(alpha) tiene que ir ANTES de cualquier WriteArray:
    # libtiff congela tags baseline como ExtraSamples apenas se escribe la
    # primera strip/tile, y después falla con "Cannot modify tag ExtraSamples
    # while writing". Un GeoTIFF Float32 de 5 bandas no tiene la heurística
    # RGBA implícita que GDAL sí aplica a uno Byte de 4 bandas, así que acá el
    # orden no perdona.
    for b in range(nb):
        out.GetRasterBand(b + 1).SetDescription(band_names[b])
        out.GetRasterBand(b + 1).SetNoDataValue(float("nan"))
    alpha_band = out.GetRasterBand(nb + 1)
    alpha_band.SetColorInterpretation(gdal.GCI_AlphaBand)
    for b in range(nb):
        out.GetRasterBand(b + 1).WriteArray(np.where(valid, spectral[b], np.nan))
    alpha_band.WriteArray((valid * 255).astype(np.float32))
    out = None
    print(f"✅ {MS_PATH} ({nb} bandas + alpha: {', '.join(band_names)})")


def trim_thermal_native():
    """Recorta al casco convexo de huellas el ortomosaico térmico NATIVO,
    que ODM produce renderizando la malla 3D (ver docker/entrypoint.sh +
    prepare_thermal_native_odm.py). Mismo criterio que trim_rgb()/
    trim_multispectral(): reusa _apply_hull() apuntando al reconstruction.json
    del proyecto ODM térmico nativo.

    El render de malla no produce "smears" (manchones borroneados por
    blending, que sí puede dejar un mosaico por costura tipo el RGB), pero SÍ
    puede salir fragmentado en islas desconectadas — confirmado en vivo con
    fast_orthophoto=True (vistazo/rápido): la malla de la nube DISPERSA de
    SfM deja huecos reales donde la textura es pobre (32 componentes conexos
    en una misión real, aunque las poses de cámara se reconstruyeron casi
    completas). Eso no es un recorte de este script — es la extensión real
    que ODM pudo mallar; el casco convexo de abajo solo puede ACHICARLA
    (nunca rellena huecos internos). Acá además hace falta limpieza de
    VALORES puntual (despike + cota física dura) — sin relación con el
    recorte espacial de arriba.
    """
    if not os.path.isfile(THNAT_ODM_SRC):
        print(f"  ⚠ {THNAT_ODM_SRC} no encontrado — sin recorte térmico nativo")
        return
    ds = gdal.Open(THNAT_ODM_SRC)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize
    full = ds.ReadAsArray().astype(np.float32)
    if full.ndim == 2:
        full = full[np.newaxis, ...]
    n_total = full.shape[0]
    has_alpha = n_total > 1 and (
        ds.GetRasterBand(n_total).GetColorInterpretation() == gdal.GCI_AlphaBand)
    a = full[0]
    if has_alpha:
        alpha = full[-1]
        amax = float(np.nanmax(alpha)) if np.isfinite(alpha).any() else 0.0
        valid = alpha > (amax / 2 if amax > 0 else 0)
    else:
        valid = np.isfinite(a)
    ds = None
    before = int(valid.sum())

    # Despike: spikes locales (ruido de sensor/textura) → mediana local.
    # Conserva cobertura; respeta bordes térmicos reales (más anchos que la
    # ventana de mediana).
    a_fill = np.where(valid, a, np.nanmedian(a[valid]) if valid.any() else 0.0)
    med = ndimage.median_filter(a_fill, size=THNAT_DESPIKE_WIN)
    spike = valid & (np.abs(a - med) > THNAT_DESPIKE_DEV)
    a[spike] = med[spike]
    print(f"  despike: {int(spike.sum()):,} px reemplazados por mediana local "
          f"(|ΔT|>{THNAT_DESPIKE_DEV}°C)")

    # Cota física dura: valores fuera de rango plausible del sensor → nodata.
    extreme = valid & ((a < THNAT_HARD_LO) | (a > THNAT_HARD_HI))
    valid = valid & ~extreme
    print(f"  extremos físicos (fuera [{THNAT_HARD_LO:.0f},{THNAT_HARD_HI:.0f}]°C): "
          f"{int(extreme.sum()):,} px")

    valid = _apply_hull(valid, THNAT_RECON, gt, W, H, proj, "térmico", geojson_out=HULL_THERMAL_PATH,
                        agl_hint_m=_reference_agl_m())
    after = int(valid.sum())
    print(f"Térmico: válidos antes={before:,} ({100*before/valid.size:.1f}%) "
          f"después={after:,} ({100*after/valid.size:.1f}%)  "
          f"recortado={100*(before-after)/max(before,1):.2f}%")

    out = np.where(valid, a, np.nan)
    drv = gdal.GetDriverByName("GTiff")
    o = drv.Create(TH_PATH, W, H, 1, gdal.GDT_Float32,
                    ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    o.SetGeoTransform(gt)
    o.SetProjection(proj)
    o.GetRasterBand(1).WriteArray(out)
    o.GetRasterBand(1).SetNoDataValue(float("nan"))
    o = None
    vals = out[np.isfinite(out)]
    print(f"✅ {TH_PATH}  T {vals.min():.1f}-{vals.max():.1f}°C")


def trim_dsm():
    """Recorta al casco convexo de huellas el DSM — mismo criterio que
    trim_rgb()/trim_multispectral()/trim_thermal_native(): la elevación
    también es fiable solo dentro del área realmente fotografiada.

    dsm_clean.py (que corre ANTES, ver docker/entrypoint.sh) ya filtra
    relleno sintético (varianza local exactamente 0) y outliers puntuales
    (mediana+MAD) — ninguno de los dos atrapa la extrapolación de borde del
    vuelo, que tiene textura y variación real, así que este recorte sigue
    haciendo falta encima de esos dos filtros.
    """
    if not os.path.isfile(DSM_PATH):
        print(f"  ⚠ {DSM_PATH} no encontrado — sin recorte de DSM")
        return
    ds = gdal.Open(DSM_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize
    z = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ds = None
    valid = np.isfinite(z)
    before = int(valid.sum())

    valid = _apply_hull(valid, RGB_RECON, gt, W, H, proj, "DSM")
    after = int(valid.sum())
    print(f"DSM: válidos antes={before:,} ({100*before/valid.size:.1f}%) "
          f"después={after:,} ({100*after/valid.size:.1f}%)  "
          f"recortado={100*(before-after)/max(before,1):.2f}%")

    out = np.where(valid, z, np.nan).astype(np.float32)
    drv = gdal.GetDriverByName("GTiff")
    o = drv.Create(DSM_PATH, W, H, 1, gdal.GDT_Float32,
                    ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    o.SetGeoTransform(gt)
    o.SetProjection(proj)
    o.GetRasterBand(1).WriteArray(out)
    o.GetRasterBand(1).SetNoDataValue(float("nan"))
    o = None
    print(f"✅ {DSM_PATH}")


if __name__ == "__main__":
    # Los cuatro productos, en el mismo orden que el entrypoint. trim_dsm() va
    # después de trim_rgb() porque comparten RGB_RECON; cada uno sale solo si
    # su ortomosaico no existe.
    trim_rgb()
    trim_dsm()
    trim_thermal_native()
    trim_multispectral()
