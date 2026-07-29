#!/usr/bin/env python3
"""Recorta los bordes de bajo solape de los mosaicos (RGB, térmico nativo,
multiespectral) — todos productos de un render de malla 3D real de ODM.

En el borde real de la cobertura de vuelo el solape entre fotos cae a 1 o 0
imágenes → la reconstrucción se degrada (texturas duplicadas/borrosas). Se
recorta solo un margen pegado a esa frontera real (no se toca nada en el
interior con buen solape), para no perder área de cobertura significativa.

El ortofoto crudo de ODM trae su canal alpha (qué triángulo de la malla 3D
se renderizó), que se usa como base. PERO el alpha es BINARIO y no codifica la
CALIDAD de reconstrucción: ODM rasteriza la malla también en el borde del vuelo
donde solo hubo vistas oblicuas/escasas, y ahí el ortofoto sale BORROSO / EN
BLOQUES (triángulos grandes mal resueltos). Por eso, además del alpha, se aplica
un PISO DE SOLAPE DE CÁMARAS (`_rgb_camera_overlap` proyecta la huella de
cada foto al suelo y cuenta cuántas cubren cada celda; se descarta <
REL_MIN_OVERLAP_*) — mismo principio para cualquier sensor. Eso quita justo el
fleco borroso/en bloques. (Se descartó usar nitidez local —Laplaciano— como
señal del borroso: no discrimina entre triángulos mal resueltos y tierra
desnuda/sendero lisos —ambos de nitidez baja— y borraba datos reales. El solape
de cámaras es la señal correcta y robusta.) La limpieza fina de motas/contorno
(en todos los productos) corre después, en confidence_mask.py.

RECORTE DE CONFIABILIDAD (estilo Agisoft/Pix4D/Terra), `_reliability_crop`:
conserva SOLO el núcleo contiguo bien-solapado, con contorno regular y margen
de seguridad. Descarta toda isla, sliver y península de bajo solape de una
sola vez — "las zonas sin datos válidos / poco solape no llevan información".

trim_rgb/trim_multispectral/trim_thermal_native comparten esta misma lógica
genérica (_rgb_camera_overlap + _adaptive_floor_joint + _reliability_crop),
apuntando cada uno a su propio reconstruction.json — el principio físico
(huella de cámara/oblicuidad) no depende del sensor ni del blending final.
"""
import os, json
import numpy as np
from osgeo import gdal
from scipy import ndimage

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


# ── RECORTE DE CONFIABILIDAD estilo Agisoft / Pix4D / DJI Terra ──────────────
# Cómo cortan el borde las herramientas profesionales (y por qué su salida no
# tiene motas flotantes ni slivers como tenía la nuestra):
#   • Pix4D: solo reconstruye/ortorectifica donde hay SUFICIENTE SOLAPE de
#     imágenes; su guía recomienda ≥5 imágenes por punto para calidad fiable.
#     El resto se reporta como baja calidad / sin dato.
#   • Agisoft Metashape: la nube densa lleva un "confidence" por punto (nº de
#     mapas de profundidad que coinciden); se filtra por confianza, y la
#     malla/orto resultante es una SUPERFICIE CONEXA — no deja puntos sueltos.
#   • DJI Terra: igual, reconstruye dentro de la región bien cubierta y emite
#     un contorno de ortomosaico limpio.
# El principio común = (1) piso de solape, (2) superficie CONEXA (un solo
# cuerpo, sin islas), (3) contorno regular, (4) margen de seguridad hacia
# adentro. _reliability_crop implementa exactamente eso. Reemplaza el parche de
# filtros de borde previos (no_xcheck / fleco / islas / alisado por separado)
# por una sola operación coherente y decisiva.
# El RGB NO trae solape por píxel — el alpha de ODM es binario y NO codifica la
# CALIDAD de reconstrucción: ODM rasteriza la malla 3D también donde se
# reconstruyó con vistas oblicuas/escasas (borde del vuelo), y ahí el ortofoto
# sale BORROSO/EN BLOQUES (la malla tiene triángulos grandes mal resueltos). Por
# eso se calcula un mapa de solape de cámaras RGB propio (proyectando la huella
# de cada foto al suelo, `_rgb_camera_overlap`) y se aplica el mismo piso.
# Mismo principio aplica a cualquier otro sensor (multiespectral, térmico
# nativo) — solo cambia el umbral de partida según su FOV/densidad de vuelo.
REL_MIN_OVERLAP_RGB = 15  # piso de solape de cámaras RGB (huella COMPLETA), estricto → fleco borroso
REL_MIN_CENTRAL_RGB = 4   # piso de solape de cámaras RGB con huella CENTRAL (análogo n_contrib)
                          # → quita el "peine"/fingers (bordes de footprints, no cobertura central)
REL_MIN_NADIR_RGB   = 2   # piso de solape NADIR (fotos con inclinación ≤NADIR_MAX_TILT_DEG): exige
                          # al menos un par de vistas casi-nadir por celda, no solo oblicuas — quita
                          # la textura "fantasma"/duplicada de zonas reconstruidas solo con oblicuas.
RGB_OV_COARSE       = 16  # factor de submuestreo para el conteo de solape (rápido; ~1.8m/celda)
REL_OPEN_M      = 2.5   # m: opening RGB — elimina penínsulas/slivers más finos que 2× esto
                        #    (2.5m quita los spikes finos del borde; <2m los dejaba)
REL_CLOSE_M     = 2.7   # m: closing RGB — regulariza el contorno, rellena muescas finas
REL_ERODE_M     = 0.4   # m: erosión final — margen de seguridad (el borde de cualquier
                        #    huella de foto es poco fiable, igual que el buffer de Agisoft)

# Multiespectral (M3M) y térmico nativo (ODM mesh, ver trim_thermal_native):
# mismos pisos RGB como valor de ARRANQUE (misma física — solape/oblicuidad de
# huella de cámara — solo cambia el sensor), sin calibración de campo propia
# todavía (ver paper/CALIBRACION_TERMICA_CAMPO.md para el precedente de por qué
# esto importa). A diferencia de RGB, donde el piso NADIR ya está validado con
# datos reales y se aplica DURO (sin auto-desactivar), acá los TRES pisos
# (full/central/nadir) van por la vía adaptativa — sin evidencia de campo
# propia, forzar uno sin poder auto-relajarse podría colapsar toda la
# cobertura de una misión con menos solape.
REL_MIN_OVERLAP_MS = REL_MIN_OVERLAP_RGB
REL_MIN_CENTRAL_MS = REL_MIN_CENTRAL_RGB
REL_MIN_NADIR_MS   = REL_MIN_NADIR_RGB
REL_OPEN_M_MS      = REL_OPEN_M
REL_CLOSE_M_MS     = REL_CLOSE_M

REL_MIN_OVERLAP_THNAT = REL_MIN_OVERLAP_RGB
REL_MIN_CENTRAL_THNAT = REL_MIN_CENTRAL_RGB
REL_MIN_NADIR_THNAT   = REL_MIN_NADIR_RGB
REL_OPEN_M_THNAT      = REL_OPEN_M
REL_CLOSE_M_THNAT     = REL_CLOSE_M
# Cotas físicas del sensor térmico (°C) — descarta valores fuera de rango
# plausible como artefacto de blending/textura, no superficie real. Rango
# amplio a propósito: en misiones de incendio los focos reales superan 60°C.
THNAT_HARD_LO, THNAT_HARD_HI = -20.0, 150.0
THNAT_DESPIKE_WIN = 9      # px: ventana de mediana para detectar spikes puntuales
THNAT_DESPIKE_DEV = 10.0   # °C: desviación vs mediana local = spike (reemplaza por mediana)


def _adaptive_floor(values, requested, population_mask=None, min_keep_frac=0.85,
                     fallback_percentile=15, label="", exclude_zero=True):
    """Los pisos de solape (REL_MIN_OVERLAP*) se calibraron para el vuelo
    RGB+H20T original (M300, solape denso). Un dron/vuelo distinto (p.ej. M3T,
    otra altura/patrón) puede tener MENOS solape real en TODA la escena — el
    piso fijo entonces descarta el 100% en vez de solo el borde. Se detecta
    comparando cuánto sobreviviría con el piso fijo vs la población real de
    solape observada; si sobrevive menos de min_keep_frac, se relaja al
    percentil fallback_percentile de esa población (nunca sube el piso, solo
    lo baja cuando el vuelo real no da para el valor calibrado).

    exclude_zero: por defecto True — para señales de solape (ov_full,
    n_overlap) el valor 0 es un CENTINELA de "fuera del footprint", no una
    medición real, y hay que ignorarlo al evaluar la población. Pasar False
    cuando 0 es una medición legítima y mala (p.ej. n_contrib=0 = ningún
    frame cubre ese píxel con peso fuerte — justo lo que se quiere detectar;
    excluirlo esconde el problema y el umbral fijo nunca se relaja aunque la
    mayoría del área sea 0)."""
    pop = values[population_mask] if population_mask is not None else values
    if exclude_zero:
        pop = pop[pop > 0]
    if pop.size == 0:
        return requested
    kept_frac = float((pop >= requested).mean())
    if kept_frac >= min_keep_frac:
        return requested
    # Con exclude_zero=True el piso mínimo útil es 1 (0 no tiene sentido —
    # el 0 ya se excluyó de pop). Con exclude_zero=False (0 es medición real)
    # el piso puede bajar hasta 0 = filtro completamente desactivado — pasa
    # cuando la distribución está tan cargada de ceros (>fallback_percentile%)
    # que ningún piso entero >=1 alcanza min_keep_frac; forzar piso=1 en ese
    # caso seguiría descartando la mayoría (el bug real que motivó esto: para
    # El Cano, n_contrib mediana=0 → percentil15=0 → clamp a 1 igual quitaba
    # el 72% del área en vez de desactivar la señal que no discrimina acá).
    min_floor = 1.0 if exclude_zero else 0.0
    adaptive = max(min_floor, float(np.percentile(pop, fallback_percentile)))
    if adaptive <= 0:
        print(f"  ⚠ {label}: ni el piso más laxo discrimina en esta misión "
              f"(mediana={np.median(pop):.1f}) — filtro desactivado")
    else:
        print(f"  ⚠ {label}: solape real más bajo que la calibración original "
              f"(mediana={np.median(pop):.1f}, piso {requested:.0f} solo dejaría "
              f"{100*kept_frac:.1f}%) — piso ajustado a {adaptive:.1f}")
    return adaptive


def _adaptive_floor_joint(specs, population_mask=None, min_keep_frac=0.85, label=""):
    """Como _adaptive_floor pero para VARIOS pisos combinados con AND (p.ej.
    solape RGB completo Y central). Evaluar cada piso por separado no alcanza:
    dos pisos que individualmente 'parecen' dejar suficiente (cada uno por
    encima de min_keep_frac) pueden, combinados, colapsar la cobertura igual
    — caso real (El Cano): full≥20 deja pasar 61.6%, central≥4 deja pasar
    76.2%, pero la INTERSECCIÓN de los dos deja solo ~25%. Se relajan TODOS
    los pisos a la vez con un mismo factor de escala (búsqueda binaria) hasta
    que la intersección conjunta alcance min_keep_frac — conserva la relación
    relativa entre pisos en vez de privilegiar uno sobre otro.
    Si una señal está tan cargada de ceros que NINGÚN piso positivo la deja
    pasar sin excluir casi toda esa mayoría (p.ej. solape central≈0 en la
    mayor parte de la escena — vuelo con poca huella central, no un defecto),
    esa señal en particular se DESACTIVA (piso 0 = sin efecto) en vez de
    forzarla a 1, que igual excluiría a esa mayoría de ceros (mismo
    principio que exclude_zero=False en _adaptive_floor).

    specs: lista de (values_array, requested_threshold). Devuelve la lista de
    pisos ajustados, mismo orden que specs."""
    pop_specs = [v[population_mask] if population_mask is not None else v for v, _ in specs]
    requested = [r for _, r in specs]
    n = len(specs)
    # Señal "activa" = incluso en su piso mínimo útil (1) deja pasar
    # min_keep_frac; si no, ningún escalado positivo la salva (ver arriba).
    active = [float((pop_specs[i] >= 1).mean()) >= min_keep_frac for i in range(n)]

    def kept_frac(scale):
        m = np.ones(pop_specs[0].shape, dtype=bool)
        for i in range(n):
            if not active[i]:
                continue
            m &= (pop_specs[i] >= requested[i] * scale)
        return float(m.mean())

    base_frac = kept_frac(1.0)
    if base_frac >= min_keep_frac and all(active):
        return requested

    lo, hi = 0.0, 1.0
    for _ in range(20):
        mid = (lo + hi) / 2
        if kept_frac(mid) >= min_keep_frac:
            lo = mid
        else:
            hi = mid
    adjusted = [max(1.0, r * lo) if active[i] else 0.0 for i, r in enumerate(requested)]
    disabled = [i for i in range(n) if not active[i]]
    extra = f" (señal #{disabled} desactivada, no discrimina en esta misión)" if disabled else ""
    print(f"  ⚠ {label}: pisos combinados dejarían solo {100*base_frac:.1f}% "
          f"(cada uno por separado parecía suficiente) — ajustados de "
          f"{[f'{r:.0f}' for r in requested]} a {[f'{a:.1f}' for a in adjusted]}{extra}")
    return adjusted


def _adaptive_ceiling(values, requested, population_mask=None, max_remove_frac=0.15,
                       fallback_percentile=90, label=""):
    """Simétrico de _adaptive_floor para filtros de EXCLUSIÓN (se descarta
    donde values > requested — p.ej. densidad de huecos, fragmentación).
    Estos umbrales también se calibraron contra el vuelo original (solape
    denso, huecos/fragmentación raros con buena geometría); en un vuelo con
    MENOS solape en general estos "defectos" son más comunes AUNQUE la
    geometría esté sana, y el umbral fijo descarta mucho más de lo previsto.
    Si el umbral fijo descartaría más de max_remove_frac, se SUBE (nunca se
    baja) al percentil fallback_percentile de la población real."""
    pop = values[population_mask] if population_mask is not None else values
    if pop.size == 0:
        return requested
    removed_frac = float((pop > requested).mean())
    if removed_frac <= max_remove_frac:
        return requested
    adaptive = max(requested, float(np.percentile(pop, fallback_percentile)))
    print(f"  ⚠ {label}: umbral calibrado ({requested:.2f}) descartaría "
          f"{100*removed_frac:.1f}% del área — subido a {adaptive:.2f}")
    return adaptive


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


RGB_OV_ZBANDS = 5  # nº de bandas de elevación para el solape de cámaras RGB.
                   # Terreno con relieve real (montañoso) invalida el supuesto de
                   # "una sola Z de terreno" (ver _rgb_camera_overlap) — proyectar
                   # con una Z plana única desalinea la huella de cámara en zonas
                   # de elevación distinta a la mediana, marcando ov=0 en área
                   # BIEN reconstruida solo porque está más alto/bajo que la
                   # mediana. 5 bandas por percentil de Z real alcanza para
                   # terrenos con cientos de metros de relieve sin volverse
                   # costoso (5× las proyecciones, no por-celda).
NADIR_MAX_TILT_DEG = 30  # inclinación máxima (respecto a mirar derecho hacia
                         # abajo) para considerar una foto "nadir". Vuelos
                         # mixtos nadir+oblicua (común en M3T, ej. El Cano:
                         # 40 nadir ~0-2° + 94 oblicuas ~45°) pueden reconstruir
                         # zonas ENTERAS solo con oblicuas si el nadir no llega
                         # ahí — eso produce textura "fantasma"/duplicada por
                         # error de correspondencia estéreo entre vistas muy
                         # oblicuas, un defecto que NO se ve en el tamaño de
                         # malla ni en el solape total (ver docs/memoria jul
                         # 2026: 6 señales geométricas probadas, ninguna lo
                         # detectaba). 30° separa con margen holgado los dos
                         # grupos típicos (~0-2° vs ~45°).


def _target_crs(proj_wkt):
    """CRS a la que se proyectan las huellas de cámara: la del PROPIO raster
    que se está recortando, no una zona UTM fija.

    BUG REAL corregido acá: esto era EPSG:32618 (UTM 18N) hardcodeado, mientras
    que el geotransform contra el que se comparan esas huellas lo escribe ODM en
    la UTM real de la misión. En la zona 18N coincidían por casualidad — en 17N
    o 19N las huellas caen a cientos de KILÓMETROS del raster y el conteo de
    solape da 0 en todas partes.

    Lo grave es que no falla: _adaptive_floor_joint lee esos ceros como "esta
    misión tiene poco solape real", relaja o directamente desactiva los pisos, y
    el recorte de confiabilidad deja de recortar el fleco borroso que existe
    para recortar. Salida plausible a la vista, geométricamente sin sentido.
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


def _rgb_camera_overlap(gt, W, H, recon_path=None, proj_wkt=None):
    """Mapas de solape de cámaras (RGB u otro sensor con poses SfM propias,
    p.ej. multiespectral): nº de fotos cuya huella en el suelo cubre cada
    celda. Proyecta las 4 esquinas de cada foto al plano del terreno usando
    las poses SfM (mismas que el térmico) y rasteriza el cuadrilátero. Devuelve
    (solape_full, solape_central, solape_nadir, factor): el COMPLETO (para el
    fleco borroso de bajo solape), el CENTRAL (esquinas hacia adentro, análogo
    RGB de n_contrib → para el "peine"/fingers) y el NADIR (solo fotos con
    inclinación ≤NADIR_MAX_TILT_DEG respecto a mirar derecho hacia abajo → para
    zonas reconstruidas solo con vistas oblicuas, ver NADIR_MAX_TILT_DEG). Si
    falta reconstruction.json, devuelve (None, None, None, None).

    recon_path: reconstruction.json a usar (default RGB_RECON) — genérico para
    poder apuntarlo al proyecto ODM de otro sensor (p.ej. MS_RECON), ya que la
    proyección de huella no depende de nada RGB-específico.

    proj_wkt: proyección del raster que se está recortando (el mismo del que
    salió `gt`) — las huellas TIENEN que caer en esa CRS para que el conteo de
    solape se alinee con la grilla. Ver _target_crs.

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
    # Modelo de cámara OpenSfM: "brown" (RGB, con focal_x/focal_y y
    # descentrado c_x/c_y) vs "perspective"/"simple_pinhole" (multiespectral
    # M3M: una sola "focal", sin descentrado — c_x/c_y quedan en 0).
    if "focal_x" in cam:
        fx = cam["focal_x"] * sc; fy = cam.get("focal_y", cam["focal_x"]) * sc
    else:
        fx = fy = cam["focal"] * sc
    cx = cam.get("c_x", 0.0) * sc + w/2; cy = cam.get("c_y", 0.0) * sc + h/2
    tr = Transformer.from_crs("EPSG:4326", _target_crs(proj_wkt), always_xy=True)
    ref = rec["reference_lla"]; ref_e, ref_n = tr.transform(ref["longitude"], ref["latitude"])

    cf = RGB_OV_COARSE; gw, gh = W//cf, H//cf; res = gt[1]*cf; x0, y0 = gt[0], gt[3]
    gx = x0 + (np.arange(gw)+0.5)*res; gy = y0 - (np.arange(gh)+0.5)*res

    # Z LOCAL por celda de la grilla coarse (no un escalar global) — resamplea
    # el DSM real a la resolución de la grilla de solape.
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

    # Dos juegos de esquinas: la huella COMPLETA (detecta el fleco borroso de
    # bajo solape) y la huella CENTRAL (esquinas hacia adentro 0.55× → análogo
    # RGB de n_contrib: cuenta solo cámaras que cubren el píxel CENTRALMENTE, no
    # en su borde → detecta el "peine"/fingers, que son bordes de footprints).
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

    # Inclinación real de cada shot (derivada de la rotación ya resuelta por
    # SfM, no del EXIF crudo — coincide con el pitch de vuelo pero no depende
    # de un CSV externo): ángulo entre la dirección de vista y "derecho hacia
    # abajo" en el mundo. d = R.T@[0,0,1] es la dirección de vista; nadir
    # perfecto → d=[0,0,-1] → -d[2]=1 → tilt=0.
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

def _iter_open(m, r):  # opening por erosión+dilatación iteradas (rápido y separable)
    return ndimage.binary_dilation(ndimage.binary_erosion(m, iterations=r), iterations=r) if r else m

def _iter_close(m, r):
    return ndimage.binary_erosion(ndimage.binary_dilation(m, iterations=r), iterations=r) if r else m

def _reliability_crop(mask, px_size_m, reference_population=None,
                       open_m=None, close_m=None):
    """Recorte de confiabilidad (Agisoft/Pix4D/Terra): conserva SOLO el núcleo
    contiguo bien-solapado, con contorno regular y margen de seguridad. El
    piso de solape por sí mismo lo aplica el caller (_adaptive_floor_joint
    sobre _rgb_camera_overlap) — esta función solo impone conectividad/contorno
    sobre lo que ya pasó ese piso.

    reference_population: reservado para pisos adicionales específicos del
    caller (no usado internamente hoy).
    open_m/close_m: radios de opening/closing en metros, default REL_OPEN_M/
    REL_CLOSE_M si no se pasan — mismo radio en metros erosiona igual sin
    importar el GSD del sensor.
    Implementación con erosión/dilatación ITERADA (kernel cruz 1px ×r): scipy lo
    resuelve como BFS de distancia — un disco explícito de r≈50px sería O(n·r²)
    sin separar, intratable en el RGB de 19288×16487.
    """
    r_open  = max(1, int(round((open_m  if open_m  is not None else REL_OPEN_M)  / px_size_m)))
    r_close = max(1, int(round((close_m if close_m is not None else REL_CLOSE_M) / px_size_m)))
    r_erode = max(1, int(round(REL_ERODE_M / px_size_m)))
    quality_mask = mask
    m = quality_mask.copy()
    m = _iter_open(m, r_open)                      # quita slivers/penínsulas finas
    lbl, n = ndimage.label(m)                      # SOLO el componente principal
    if n > 0:
        sz = np.bincount(lbl.ravel()); sz[0] = 0
        m = lbl == int(sz.argmax())
    m = _iter_close(m, r_close)                    # contorno regular
    m = ndimage.binary_erosion(m, iterations=r_erode)  # margen de seguridad
    # OJO: re-intersectar con `quality_mask` (lo que pasó el piso de solape),
    # NO con `mask` (el original sin filtrar) — el closing DILATA el
    # componente limpio, y re-ANDear contra el mask original sin filtrar
    # reintroduce exactamente las zonas de bajo solape que el piso ya había
    # descartado (el "peine"/tendrils que closing hace crecer de vuelta desde
    # el borde). Bug real, no solo cuestión de umbral: se notaba poco con el
    # piso estricto original (8) porque esas zonas eran finas; con el piso
    # relajado (adaptativo, misiones de bajo solape) se vuelve muy visible.
    return m & quality_mask


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
        valid = full[3] == 255   # alpha real de ODM: triángulo de malla renderizado
    else:
        valid = a.mean(axis=0) >= 10  # respaldo si no hay alpha disponible
    before = int(valid.sum())

    # NOTA: el criterio de nitidez (Laplaciano) que documentaba esta función
    # quedó MAL CALIBRADO para la corrida actual de ODM — se verificó que no
    # discrimina nada: mediana de nitidez cerca del borde (<30m) vs lejos
    # (>=30m) son del mismo orden, y a CUALQUIER umbral razonable se excluye
    # un % similar en ambas zonas (ej. con SHARP_THRESH=700, 83.7% cerca vs
    # 86.2% lejos — sin separación real). Con el umbral documentado (700)
    # esto borraba ~85% de TODO el RGB válido cerca de cualquier hueco chico,
    # produciendo los huecos ramificados (siguiendo senderos/tierra desnuda,
    # que tienen textura lisa = nitidez baja pero SON datos reales nítidos,
    # no fantasmas). Se elimina el filtro de nitidez — el alpha real de la
    # malla 3D de ODM (ya verificado como fuente de verdad) es suficiente.
    # Piso de solape de cámaras RGB: el alpha de ODM incluye el fleco BORROSO/EN
    # BLOQUES del borde (malla reconstruida con vistas oblicuas/escasas). Se
    # calcula el solape real proyectando huellas de foto y se descarta <REL_MIN_
    # OVERLAP_RGB — igual que el piso de solape del térmico. Ver el comentario de
    # REL_MIN_OVERLAP_RGB y _rgb_camera_overlap.
    px_size_m = abs(gt[1])
    ov_full, ov_central, ov_nadir, cf = _rgb_camera_overlap(gt, W, H, proj_wkt=proj)
    if ov_full is not None:
        gh, gw = ov_full.shape
        ys = np.minimum(np.arange(H) * gh // H, gh - 1)
        xs = np.minimum(np.arange(W) * gw // W, gw - 1)
        ys_c = np.minimum(np.arange(gh) * H // gh, H - 1)
        xs_c = np.minimum(np.arange(gw) * W // gw, W - 1)
        valid_coarse = valid[np.ix_(ys_c, xs_c)]
        # (1) piso de solape COMPLETO → quita el fleco borroso/en bloques.
        # (2) piso de solape CENTRAL (análogo de n_contrib) → quita el "peine"/
        #     fingers (bordes de footprints). fill_holes en grilla gruesa rellena
        #     los dips interiores encerrados y deja fuera solo las fingers del borde.
        # (3) piso NADIR: zonas reconstruidas SOLO con fotos oblicuas (sin
        #     ningún nadir de respaldo) — textura "fantasma"/duplicada por mal
        #     emparejamiento estéreo entre vistas muy oblicuas. Este defecto NO
        #     se ve en tamaño de malla, solape total, pendiente del DSM ni
        #     textura (6 señales probadas, jul 2026) — solo en si hay AL MENOS
        #     una foto casi-nadir cubriendo la celda. Ver NADIR_MAX_TILT_DEG.
        # min_keep_frac bajo (no 0.85): el criterio ya NO es "maximizar
        # cobertura mientras algo pase" — es "cortar de verdad lo distorsionado
        # /de bajo solape, aunque quede poca área, mejor que dato malo" (pedido
        # explícito del usuario tras ver casas/laderas borrosas incluidas).
        # Confirmado con datos reales (El Cano): en zona distorsionada por bajo
        # solape, ov_central≈0 en 97% de los píxeles (vs mediana 9.5-10 en
        # bosque bueno); en zona distorsionada por vuelo oblicuo, ov_nadir
        # mediana=1 con 45% en cero (vs mediana 9, 15% en cero en zona buena).
        floor_full, floor_central = _adaptive_floor_joint(
            [(ov_full, REL_MIN_OVERLAP_RGB), (ov_central, REL_MIN_CENTRAL_RGB)],
            valid_coarse, min_keep_frac=0.2, label="piso de solape RGB (completo+central)")
        keep_coarse = ov_full >= floor_full
        central_ok = ndimage.binary_fill_holes(ov_central >= floor_central)
        keep_coarse = keep_coarse & central_ok
        # Piso NADIR aparte, NO adaptativo/auto-desactivable: es una señal ya
        # VALIDADA con datos reales (zona mala mediana=1, 45% en cero; zona
        # buena mediana=9, 15% en cero — ver notas jul 2026), a diferencia de
        # n_contrib térmico (ruido sin estructura espacial, confirmado
        # correcto desactivarlo). El mecanismo de auto-desactivar por "no
        # alcanza min_keep_frac" no distingue "señal sin poder discriminante"
        # de "señal que discrimina bien pero la MAYORÍA de esta misión es
        # oblicua-only" — este caso es lo segundo, así que se exige de
        # verdad aunque dejar pase menos del 20% objetivo.
        before_nadir = int(keep_coarse[valid_coarse].sum())
        nadir_ok = ndimage.binary_fill_holes(ov_nadir >= REL_MIN_NADIR_RGB)
        keep_coarse = keep_coarse & nadir_ok
        after_nadir = int(keep_coarse[valid_coarse].sum())
        print(f"  piso NADIR (≥{REL_MIN_NADIR_RGB} fotos casi-nadir, no solo oblicuas): "
              f"−{before_nadir - after_nadir:,} celdas coarse "
              f"({100*(before_nadir-after_nadir)/max(before_nadir,1):.1f}% de lo que quedaba)")
        well_covered = keep_coarse[np.ix_(ys, xs)]
        before_ov = int(valid.sum())
        valid = valid & well_covered
        print(f"  piso de solape RGB (full≥{floor_full:.0f} y central≥{floor_central:.0f}): "
              f"−{before_ov - int(valid.sum()):,} px (fleco borroso + peine)")
    else:
        print("  ⚠ reconstruction.json no encontrado — sin piso de solape RGB")

    # Recorte de confiabilidad (mismo criterio Agisoft/Pix4D/Terra que el
    # térmico): conserva solo el cuerpo conexo con contorno regular y margen.
    reliable = _reliability_crop(valid, px_size_m)
    after = int(reliable.sum())
    print(f"RGB: válidos antes={before:,} ({100*before/valid.size:.1f}%) "
          f"después={after:,} ({100*after/valid.size:.1f}%)  "
          f"recortado={100*(before-after)/before:.2f}%")

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
    out.GetRasterBand(4).WriteArray((reliable * 255).astype(np.uint8))
    out = None
    print(f"✅ {RGB_PATH} (RGBA)")


def trim_multispectral():
    """Recorta bordes de bajo solape del ortomosaico multiespectral (M3M),
    mismo criterio calidad-sobre-cobertura que trim_rgb() — reusa TAL CUAL
    _rgb_camera_overlap/_adaptive_floor_joint/_reliability_crop apuntando al
    reconstruction.json del proyecto ODM multiespectral (mismo principio
    físico: huella de cámara / oblicuidad, no depende de si el sensor es RGB
    o multiespectral). Ver REL_MIN_*_MS arriba para por qué los tres pisos van
    por la vía adaptativa acá (a diferencia del piso nadir RGB, ya validado y
    aplicado duro).

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
    if has_alpha:
        spectral = full[:-1]
        alpha = full[-1]
        amax = float(np.nanmax(alpha)) if np.isfinite(alpha).any() else 0.0
        valid = alpha > (amax / 2 if amax > 0 else 0)
    else:
        spectral = full
        valid = np.isfinite(spectral).all(axis=0) & (np.nanmean(spectral, axis=0) > 0)
    band_names = [ds.GetRasterBand(i + 1).GetDescription() or f"band{i+1}"
                  for i in range(spectral.shape[0])]
    ds = None
    before = int(valid.sum())

    px_size_m = abs(gt[1])
    ov_full, ov_central, ov_nadir, cf = _rgb_camera_overlap(gt, W, H, recon_path=MS_RECON,
                                                            proj_wkt=proj)
    if ov_full is not None:
        gh, gw = ov_full.shape
        ys = np.minimum(np.arange(H) * gh // H, gh - 1)
        xs = np.minimum(np.arange(W) * gw // W, gw - 1)
        ys_c = np.minimum(np.arange(gh) * H // gh, H - 1)
        xs_c = np.minimum(np.arange(gw) * W // gw, W - 1)
        valid_coarse = valid[np.ix_(ys_c, xs_c)]
        floor_full, floor_central, floor_nadir = _adaptive_floor_joint(
            [(ov_full, REL_MIN_OVERLAP_MS), (ov_central, REL_MIN_CENTRAL_MS),
             (ov_nadir, REL_MIN_NADIR_MS)],
            valid_coarse, min_keep_frac=0.2,
            label="piso de solape MS (completo+central+nadir)")
        keep_coarse = ov_full >= floor_full
        keep_coarse &= ndimage.binary_fill_holes(ov_central >= floor_central)
        keep_coarse &= ndimage.binary_fill_holes(ov_nadir >= floor_nadir)
        well_covered = keep_coarse[np.ix_(ys, xs)]
        before_ov = int(valid.sum())
        valid = valid & well_covered
        print(f"  piso de solape MS (full≥{floor_full:.0f}, central≥{floor_central:.0f}, "
              f"nadir≥{floor_nadir:.0f}): −{before_ov - int(valid.sum()):,} px")
    else:
        print("  ⚠ reconstruction.json multiespectral no encontrado — sin piso de solape")

    reliable = _reliability_crop(valid, px_size_m,
                                  open_m=REL_OPEN_M_MS, close_m=REL_CLOSE_M_MS)
    after = int(reliable.sum())
    print(f"Multiespectral: válidos antes={before:,} ({100*before/valid.size:.1f}%) "
          f"después={after:,} ({100*after/valid.size:.1f}%)  "
          f"recortado={100*(before-after)/max(before,1):.2f}%")

    drv = gdal.GetDriverByName("GTiff")
    nb = spectral.shape[0]
    out = drv.Create(MS_PATH, W, H, nb + 1, gdal.GDT_Float32,
                      ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    # SetColorInterpretation(alpha) tiene que ir ANTES de cualquier
    # WriteArray: libtiff congela tags baseline como ExtraSamples apenas se
    # escribe la primera strip/tile del archivo (root cause del
    # RuntimeError "Cannot modify tag ExtraSamples while writing" — un
    # GeoTIFF Float32 de 5 bandas no tiene la heurística RGBA implícita que
    # sí aplica GDAL a un GeoTIFF Byte de 4 bandas, ver trim_rgb()).
    for b in range(nb):
        out.GetRasterBand(b + 1).SetDescription(band_names[b])
        out.GetRasterBand(b + 1).SetNoDataValue(float("nan"))
    alpha_band = out.GetRasterBand(nb + 1)
    alpha_band.SetColorInterpretation(gdal.GCI_AlphaBand)
    for b in range(nb):
        out.GetRasterBand(b + 1).WriteArray(np.where(reliable, spectral[b], np.nan))
    alpha_band.WriteArray((reliable * 255).astype(np.float32))
    out = None
    print(f"✅ {MS_PATH} ({nb} bandas + alpha: {', '.join(band_names)})")


def trim_thermal_native():
    """Recorta bordes de bajo solape del ortomosaico térmico NATIVO (ODM
    render de malla 3D real, ver docker/entrypoint.sh + prepare_thermal_native_odm.py
    — reemplaza el pipeline heurístico anterior, winner-take-all sobre
    proyección propia). Mismo criterio calidad-sobre-cobertura que
    trim_rgb()/trim_multispectral(): reusa TAL CUAL _rgb_camera_overlap/
    _adaptive_floor_joint/_reliability_crop apuntando al reconstruction.json
    del proyecto ODM térmico nativo — el principio físico (huella de cámara/
    oblicuidad) no depende del sensor.

    A diferencia del pipeline viejo (que necesitaba despike+deband+cotas
    duras+blandas+fragmentación+"smears" porque el blending propio producía
    esos artefactos), el render de malla de ODM no los genera — validado
    visualmente contra la referencia Agisoft (misma escena, sin fragmentación,
    68.6% cobertura vs 16.9% del pipeline viejo). Solo queda limpieza de
    valores puntual (despike + cota física dura), no geometría de blending.
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

    px_size_m = abs(gt[1])
    ov_full, ov_central, ov_nadir, cf = _rgb_camera_overlap(gt, W, H, recon_path=THNAT_RECON,
                                                            proj_wkt=proj)
    if ov_full is not None:
        gh, gw = ov_full.shape
        ys = np.minimum(np.arange(H) * gh // H, gh - 1)
        xs = np.minimum(np.arange(W) * gw // W, gw - 1)
        ys_c = np.minimum(np.arange(gh) * H // gh, H - 1)
        xs_c = np.minimum(np.arange(gw) * W // gw, W - 1)
        valid_coarse = valid[np.ix_(ys_c, xs_c)]
        floor_full, floor_central, floor_nadir = _adaptive_floor_joint(
            [(ov_full, REL_MIN_OVERLAP_THNAT), (ov_central, REL_MIN_CENTRAL_THNAT),
             (ov_nadir, REL_MIN_NADIR_THNAT)],
            valid_coarse, min_keep_frac=0.2,
            label="piso de solape térmico nativo (completo+central+nadir)")
        keep_coarse = ov_full >= floor_full
        keep_coarse &= ndimage.binary_fill_holes(ov_central >= floor_central)
        keep_coarse &= ndimage.binary_fill_holes(ov_nadir >= floor_nadir)
        well_covered = keep_coarse[np.ix_(ys, xs)]
        before_ov = int(valid.sum())
        valid = valid & well_covered
        print(f"  piso de solape térmico (full≥{floor_full:.0f}, central≥{floor_central:.0f}, "
              f"nadir≥{floor_nadir:.0f}): −{before_ov - int(valid.sum()):,} px")
    else:
        print("  ⚠ reconstruction.json térmico nativo no encontrado — sin piso de solape")

    reliable = _reliability_crop(valid, px_size_m,
                                  open_m=REL_OPEN_M_THNAT, close_m=REL_CLOSE_M_THNAT)
    after = int(reliable.sum())
    print(f"Térmico: válidos antes={before:,} ({100*before/valid.size:.1f}%) "
          f"después={after:,} ({100*after/valid.size:.1f}%)  "
          f"recortado={100*(before-after)/max(before,1):.2f}%")

    out = np.where(reliable, a, np.nan)
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
    """Recorta bordes de bajo solape del DSM — mismo criterio calidad-sobre-
    cobertura que trim_rgb()/trim_multispectral()/trim_thermal_native(), que
    ya lo aplican. El DSM quedaba afuera de este recorte (bug real, no
    intencional): dsm_clean.py solo filtra relleno sintético (varianza local
    exactamente 0, ver ese script) y outliers puntuales (mediana+MAD) — pero
    en el borde de bajo solape ODM interpola la grilla del DEM con valores
    CON textura/variación real (no planos, no outliers puntuales), así que
    ninguno de los dos filtros los atrapa. Es la misma extrapolación borrosa/
    en bloques que ya se ve en el alpha crudo de RGB — mismo piso de solape
    de cámaras (_rgb_camera_overlap sobre RGB_RECON, la reconstrucción de la
    que sale el DSM) resuelve el problema en el DSM igual que en RGB.

    Corre DESPUÉS de clean-dsm (necesita outputs/dsm.tif ya filtrado de
    relleno sintético/outliers como entrada) — el propio _rgb_camera_overlap
    también LEE ese mismo outputs/dsm.tif para el Z-banding de la huella de
    cámara, pero eso es solo una referencia de elevación gruesa (percentiles),
    no le afecta usar la versión sin recortar todavía.
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

    px_size_m = abs(gt[1])
    ov_full, ov_central, ov_nadir, cf = _rgb_camera_overlap(gt, W, H, recon_path=RGB_RECON,
                                                            proj_wkt=proj)
    if ov_full is not None:
        gh, gw = ov_full.shape
        ys = np.minimum(np.arange(H) * gh // H, gh - 1)
        xs = np.minimum(np.arange(W) * gw // W, gw - 1)
        ys_c = np.minimum(np.arange(gh) * H // gh, H - 1)
        xs_c = np.minimum(np.arange(gw) * W // gw, W - 1)
        valid_coarse = valid[np.ix_(ys_c, xs_c)]
        floor_full, floor_central, floor_nadir = _adaptive_floor_joint(
            [(ov_full, REL_MIN_OVERLAP_RGB), (ov_central, REL_MIN_CENTRAL_RGB),
             (ov_nadir, REL_MIN_NADIR_RGB)],
            valid_coarse, min_keep_frac=0.2,
            label="piso de solape DSM (completo+central+nadir)")
        keep_coarse = ov_full >= floor_full
        keep_coarse &= ndimage.binary_fill_holes(ov_central >= floor_central)
        keep_coarse &= ndimage.binary_fill_holes(ov_nadir >= floor_nadir)

        # BUG REAL corregido acá (visto en Barbosa: dsm_clean detectó 72.5%
        # de relleno sintético en esta misión, dejando `valid` fino
        # legítimamente perforado — huecos reales dispersos, no una "zona
        # mala" contigua). El recorte de confiabilidad (núcleo contiguo,
        # _reliability_crop) NO puede aplicarse sobre esa máscara fina ya
        # perforada: el radio de apertura calibrado para el fleco de BORDE
        # de vuelo (metros) erosiona cualquier salpicado fino a casi nada —
        # visto en Barbosa: 10.6% tras el piso de solape → 0.03% tras
        # _reliability_crop sobre la máscara fina (1.8M componentes,
        # mediana 2px — ruido disperso, no una región recortable).
        # Fix: el núcleo contiguo se busca en la grilla GRUESA de solape
        # geométrico (`keep_coarse`, un blob sólido — análogo al alpha de
        # RGB, NO perforado por dsm_clean), con el radio físico convertido a
        # celdas gruesas (cf px por celda). Eso recorta el fleco de borde de
        # vuelo real sin exigirle contigüidad fina a los huecos legítimos de
        # dsm_clean, que quedan tal cual dentro de la región geométricamente
        # buena.
        coarse_px_size_m = px_size_m * cf
        keep_coarse = _reliability_crop(keep_coarse, coarse_px_size_m)

        well_covered = keep_coarse[np.ix_(ys, xs)]
        before_ov = int(valid.sum())
        valid = valid & well_covered
        print(f"  piso de solape DSM (full≥{floor_full:.0f}, central≥{floor_central:.0f}, "
              f"nadir≥{floor_nadir:.0f}) + núcleo contiguo en grilla gruesa ({coarse_px_size_m:.2f}m/celda): "
              f"−{before_ov - int(valid.sum()):,} px")
    else:
        print("  ⚠ reconstruction.json RGB no encontrado — sin piso de solape DSM")

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
    trim_rgb()
    trim_dsm()
    trim_thermal_native()
