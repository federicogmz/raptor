#!/usr/bin/env python3
"""
Backend web de RAPTOR: subida de fotos crudas + selección de sensores/modo
+ progreso en vivo + geovisor, todo en un solo proceso.

No reimplementa nada del pipeline — reusa tal cual core/scan.py y
core/runner.py (activate_mission, PipelineRun, parse_progress_events), que ya
corren docker/entrypoint.sh como subprocess y exponen progreso estructurado
(ver PROGRESS_FILE en scripts/progress.py). core/ es el orquestador del
pipeline y no tiene UI propia.

Los tres sensores son OPCIONALES e independientes: se puede volar solo el
M3T/H20T (RGB+térmico), solo el M3M (multiespectral) o los dos. El modo se
deriva de qué se subió + qué eligió el usuario, y se VALIDA acá antes de
lanzar el pipeline: una misión que no puede terminar bien tiene que fallar
en el formulario (en segundos, corregible ahí mismo) y no 40 minutos después
adentro de ODM.

Solo UNA misión corre a la vez (activate_mission() redirige
processing/outputs/preprocessing/geovisor/tiles vía symlinks — dos corridas
simultáneas se pisarían).

Uso:
  python3 -m webapp.main [puerto]
"""
import asyncio
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from osgeo import gdal, osr
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

gdal.UseExceptions()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.runner import PipelineRun, activate_mission, parse_progress_events
from core.scan import RUNS_ROOT, sanitize_mission_name, scan_existing_runs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from hardware import estimate_message  # noqa: E402 — mensaje de calidad del formulario
from hardware import preset as hw_preset  # noqa: E402 — valida el nombre del preset

APP_DIR = Path(os.environ.get("RAPTOR_APP_DIR", "/app"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
GEOVISOR_DIR = APP_DIR / "geovisor"

UPLOAD_KINDS = ("rgb_thermal", "multispectral")
VALID_MODES = ("rgb", "rgb+thermal", "thermal", "none")

# ── Exportación de la entrega ───────────────────────────────────────────
# Catálogo y formatos los define scripts/export_products.py; acá solo se
# valida lo que llega del formulario para poder devolver un error corregible
# ANTES de arrancar (mismo principio que _validate() para los sensores).
EXPORT_PRODUCTS = ("rgb", "thermal", "dsm", "multispectral", "indices", "classes",
                   "confidence", "area", "flight_path", "situation", "pointclouds")
EXPORT_RASTER_FORMATS = ("cog", "gtiff")
EXPORT_VECTOR_FORMATS = ("geojson", "gpkg", "shp", "kml")
# La entrega va al DISCO DEL HOST, no a una ruta interna del contenedor: un
# producto que muere con el contenedor no le sirve a nadie. El lanzador
# (./raptor webapp --export DIR) monta esa carpeta del host en EXPORT_MOUNT y
# pasa su ruta real en EXPORT_HOST_DIR, para que el formulario pueda mostrar y
# validar rutas que el usuario reconoce en su propio disco.
# EXPORT_MOUNT es configurable solo para poder probarlo sin escribir en /: en
# producción siempre es /export, que es donde monta el lanzador.
def _normalize_host_dir(v):
    """os.path.normpath, no solo rstrip("/") — sin esto, un valor con "./"
    o ".." sin resolver (p.ej. EXPORT_HOST_DIR=".../raptor/./entregas", que
    podía pasar antes por cómo el lanzador `raptor` armaba la ruta) nunca
    hace match de prefijo contra una ruta que SÍ llega normalizada
    (host_a_contenedor() normaliza la ruta entrante) — el resultado es un
    "está fuera de la carpeta montada" apuntando, de forma confusa, a la
    carpeta correcta. El lanzador ya normaliza de su lado (ver abs() en
    ./raptor); esto es la misma garantía por si la variable llega de otro
    lado (un `docker run -e` a mano, por ejemplo)."""
    return os.path.normpath(v) if v else v


EXPORT_MOUNT = os.environ.get("EXPORT_MOUNT", "/export").rstrip("/")
EXPORT_HOST_DIR = _normalize_host_dir(os.environ.get("EXPORT_HOST_DIR", "").rstrip("/"))


def export_disponible():
    """La exportación solo se ofrece si hay una carpeta del host montada."""
    return bool(EXPORT_HOST_DIR) and os.path.isdir(EXPORT_MOUNT)


# ── Importar fotos ya presentes en el disco del servidor ────────────────
# Cuando la webapp corre en la MISMA máquina donde están las fotos (el caso
# de campo típico: un solo equipo, sin red de por medio), subir por el
# navegador es leer todo el archivo a memoria del cliente + mandarlo por
# HTTP para volver a escribirlo en el mismo disco — con cientos de fotos de
# varios GB en total, eso es minutos de tráfico local para copiar algo que
# ya está ahí. Mismo patrón que la exportación (./raptor webapp --import
# DIR monta esa carpeta del host, de solo lectura, y expone su ruta real
# para que el formulario la muestre): en vez de subir, el usuario apunta a
# una ruta del host y el servidor copia directo en el filesystem, sin pasar
# por el navegador.
IMPORT_MOUNT = os.environ.get("IMPORT_MOUNT", "/import").rstrip("/")
IMPORT_HOST_DIR = _normalize_host_dir(os.environ.get("IMPORT_HOST_DIR", "").rstrip("/"))


def import_disponible():
    return bool(IMPORT_HOST_DIR) and os.path.isdir(IMPORT_MOUNT)


def import_host_a_contenedor(ruta_host):
    """Traduce una ruta del host (dentro de IMPORT_HOST_DIR) a su ruta
    dentro del contenedor. None si no hay carpeta montada o la ruta cae
    fuera de ella — mismo criterio que host_a_contenedor() para export,
    pero de SOLO LECTURA (import nunca escribe ahí)."""
    if not import_disponible():
        return None
    ruta = os.path.normpath(ruta_host.rstrip("/") or IMPORT_HOST_DIR)
    if ruta == IMPORT_HOST_DIR:
        return IMPORT_MOUNT
    prefijo = IMPORT_HOST_DIR + os.sep
    if not ruta.startswith(prefijo):
        return None
    return os.path.join(IMPORT_MOUNT, ruta[len(prefijo):])


def import_contenedor_a_host(ruta_cont):
    """Inversa de import_host_a_contenedor() — para mostrarle al usuario una
    ruta que reconoce en su propio disco (el explorador de carpetas de
    /api/import/browse, y la lista de "ya agregadas" del status) en vez de
    la ruta interna /import/... del contenedor."""
    if not IMPORT_HOST_DIR:
        return ruta_cont
    ruta_cont = os.path.normpath(ruta_cont)
    if ruta_cont == IMPORT_MOUNT:
        return IMPORT_HOST_DIR
    prefijo = IMPORT_MOUNT + os.sep
    if ruta_cont.startswith(prefijo):
        return os.path.join(IMPORT_HOST_DIR, ruta_cont[len(prefijo):])
    return ruta_cont


def host_a_contenedor(ruta_host):
    """Traduce una ruta del host a la ruta equivalente dentro del contenedor.

    Devuelve None si cae fuera de la carpeta montada — que es el único
    subárbol del host que este proceso puede escribir.
    """
    if not export_disponible():
        return None
    ruta = os.path.normpath(ruta_host.rstrip("/") or EXPORT_HOST_DIR)
    if ruta == EXPORT_HOST_DIR:
        return EXPORT_MOUNT
    prefijo = EXPORT_HOST_DIR + os.sep
    if not ruta.startswith(prefijo):
        return None
    return os.path.join(EXPORT_MOUNT, ruta[len(prefijo):])


def contenedor_a_host(ruta_cont):
    """Inversa de host_a_contenedor, para mostrarle al usuario dónde quedó."""
    if not EXPORT_HOST_DIR:
        return ruta_cont
    if ruta_cont == EXPORT_MOUNT:
        return EXPORT_HOST_DIR
    prefijo = EXPORT_MOUNT + os.sep
    if ruta_cont.startswith(prefijo):
        return os.path.join(EXPORT_HOST_DIR, ruta_cont[len(prefijo):])
    return ruta_cont

app = FastAPI(title="Pipeline UAV de respuesta rápida")
RUNS_ROOT.mkdir(parents=True, exist_ok=True)


# StaticFiles no manda Cache-Control — sin eso, el navegador puede quedarse
# con una copia vieja de app.js/index.html/style.css sin revalidar (pasó en
# la práctica: cambios reales en el servidor, "no veo cambios" del lado del
# usuario). "no-cache" NO es "sin caché" — el navegador SIGUE cacheando,
# pero tiene que revalidar con ETag/Last-Modified en cada carga; si el
# archivo no cambió, el servidor responde 304 y no reenvía el contenido, así
# que no cuesta ancho de banda de más. Los tiles PNG de geovisor/tiles NO
# cambian una vez generados (cada corrida los regenera desde cero, nunca los
# pisa in-place), así que a esos sí les sirve un cache "fuerte" — se
# distinguen por el path.
class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith(("/geovisor", "/static")):
            # Solo los PNG de los tiles son de verdad inmutables. tiles/bounds.json
            # vive en la MISMA carpeta pero cambia por misión y hasta durante una
            # misma corrida (pollBoundsForChanges lo sondea justamente porque
            # cambia) — con el año de caché de acá, el navegador quedaba pegado al
            # bounds.json de la primera misión que abrió (centro/zoom viejos) sin
            # volver a pedirlo nunca más, aunque la URL fuera la misma para
            # cualquier misión que se activara después.
            # status_code==200 además de .png: un tile pedido ANTES de que
            # existiera (típico durante una corrida en curso) responde 404,
            # y sin este chequeo ese 404 se cacheaba "para siempre" igual —
            # el navegador nunca lo volvía a pedir ni cuando el archivo real
            # ya estaba escrito. Bug real, reportado en vivo: el térmico
            # nunca aparecía aunque bounds.json y el archivo en disco ya
            # estuvieran bien.
            if "/tiles/" in path and path.endswith(".png") and response.status_code == 200:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response


app.add_middleware(NoCacheStaticMiddleware)

# Compresión HTTP. Sin esto se mandaban SIN comprimir, en cada carga, el
# app.js del geovisor (176 KB), su hoja de estilos (42 KB) y la página de la
# webapp (93 KB) — texto que comprime ~4x. En una laptop sobre localhost eso
# no se nota; en la tablet del puesto de mando, contra el hotspot del celular
# de alguien, sí.
#
# minimum_size deja pasar sin comprimir lo chico (donde el gasto de CPU no se
# paga) y, sobre todo, no toca los tiles: son PNG ya comprimidos, y el
# middleware los saltea solo porque no están en el rango de tipos de texto.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ── Clasificación de archivos crudos ────────────────────────────────────
# Los mismos sufijos que busca docker/setup-data.sh recursivamente. Se
# clasifica acá para poder decirle al usuario QUÉ subió realmente (y qué le
# falta) antes de arrancar, en vez de que se entere por un error de ODM.
MS_BANDS = ("G", "R", "RE", "NIR")


def classify_files(names):
    counts = {"rgb": 0, "thermal": 0, "ms": 0, "dband": 0, "otros": 0}
    # Por banda además del total: ODM necesita las 4 de CADA captura, así que
    # el total por sí solo no dice si el vuelo está completo (ver
    # _incomplete_captures).
    counts["ms_bands"] = {b: 0 for b in MS_BANDS}
    for n in names:
        u = n.upper()
        if u.endswith("_V.JPG") or u.endswith("_W.JPG"):
            counts["rgb"] += 1
        elif u.endswith("_T.JPG"):
            counts["thermal"] += 1
        elif "_MS_" in u and u.endswith(".TIF"):
            counts["ms"] += 1
            banda = u.rsplit("_MS_", 1)[1][:-4]
            if banda in counts["ms_bands"]:
                counts["ms_bands"][banda] += 1
        elif u.endswith("_D.JPG"):
            counts["dband"] += 1
        else:
            counts["otros"] += 1
    counts["total"] = sum(counts[k] for k in ("rgb", "thermal", "ms", "dband", "otros"))
    counts["incompletas"] = _incomplete_captures(names) if counts["ms"] else []
    return counts


def _incomplete_captures(names):
    """Capturas multiespectrales a las que les falta alguna banda.

    Devuelve [(prefijo, bandas_presentes)] ordenado. ODM empareja las 4 bandas
    de cada captura por nombre y aborta si a alguna le falta su compañera —
    pero recién dentro de compute_band_maps(), o sea después del SfM completo y
    con un mensaje genérico sobre CaptureUUID. Detectarlo acá cuesta
    milisegundos y evita perder una hora de procesamiento.
    """
    capturas = {}
    for n in names:
        u = n.upper()
        if "_MS_" not in u or not u.endswith(".TIF"):
            continue
        prefijo, banda = u.rsplit("_MS_", 1)
        banda = banda[:-4]
        if banda in MS_BANDS:
            capturas.setdefault(prefijo, set()).add(banda)
    return sorted((p, sorted(b)) for p, b in capturas.items() if len(b) != len(MS_BANDS))


def _listdir_names(d: Path):
    """Nombres de archivo (sin ruta) bajo `d`, RECURSIVO — classify_files()
    solo mira sufijos/nombres, nunca la ruta completa, así que aplanar acá
    es seguro. Antes era solo el nivel superior: alcanzaba para raw/<kind>/
    (la subida por navegador ya llega aplanada ahí, ver /api/upload), pero
    import-local() (ver ese endpoint) puede dejar raw/<kind> como un
    SYMLINK directo a una carpeta del usuario con la estructura original
    del dron (p.ej. 100MEDIA/) — sin recursividad, esas fotos quedaban
    invisibles para el conteo de "qué hay subido" (calidad, validación,
    lista de misiones), aunque el pipeline (docker/setup-data.sh, que sí
    busca recursivo) las procesara igual.

    os.walk(followlinks=True), NO Path.rglob(): confirmado en vivo (Python
    3.12.3, el mismo de esta imagen) que rglob('*') NO desciende adentro de
    un symlink que encuentra A MITAD de la recorrida — solo lo lista como
    una entrada más, sin entrar. Eso pasaba desapercibido mientras raw/<kind>
    era EL symlink de partida (ahí sí lo sigue, mismo motivo que find sin -L
    vs find -L), pero deja de alcanzar apenas raw/<kind> es un directorio
    real que contiene VARIOS symlinks adentro (una carpeta por fuente
    elegida, ver import-local()) — exactamente el mismo bug de fondo que el
    de `find` en docker/setup-data.sh, esta vez del lado de pathlib."""
    if not d.is_dir():
        return []
    names = []
    for _root, _dirs, files in os.walk(d, followlinks=True):
        names.extend(files)
    return names


def _link_one_folder(dest_dir: Path, src_root: str) -> str:
    """Agrega UNA carpeta fuente como symlink adentro de dest_dir, con
    nombre libre de colisión. Si src_root ya estaba agregada (mismo destino
    real, aunque bajo otro nombre), no duplica — devuelve la entrada
    existente tal cual."""
    real_src = os.path.realpath(src_root)
    for entry in dest_dir.iterdir():
        if entry.is_symlink() and os.path.realpath(entry) == real_src:
            return entry.name
    base = os.path.basename(src_root.rstrip("/")) or "carpeta"
    name, i = base, 2
    while (dest_dir / name).exists():
        name = f"{base}-{i}"
        i += 1
    os.symlink(src_root, dest_dir / name)
    return name


def _add_import_folder(dest_dir: Path, src_root: str) -> str:
    """Agrega src_root como una carpeta más adentro de dest_dir (raw/<kind>/),
    que a partir de ahora SIEMPRE es un directorio real que contiene un
    symlink por cada carpeta fuente elegida — nunca copia ni un byte.

    dest_dir puede llegar siendo un symlink de TOPE directo a una sola
    carpeta (el diseño anterior, de una sola fuente por kind): se migra acá
    mismo antes de agregar la nueva, preservando la que ya estaba como la
    primera entrada, sin perder nada."""
    if dest_dir.is_symlink():
        old_target = os.readlink(dest_dir)
        dest_dir.unlink()
        dest_dir.mkdir(parents=True)
        _link_one_folder(dest_dir, old_target)
    dest_dir.mkdir(parents=True, exist_ok=True)
    return _link_one_folder(dest_dir, src_root)


def _imported_folders(dest_dir: Path):
    """Carpetas fuente ya agregadas a raw/<kind>/ (para que la UI las
    liste con opción de sacar una sola, sin tener que vaciar todo)."""
    if not dest_dir.is_dir() or dest_dir.is_symlink():
        return []
    out = []
    try:
        for entry in dest_dir.iterdir():
            if entry.is_symlink():
                out.append({"name": entry.name,
                            "host_path": import_contenedor_a_host(os.path.realpath(entry))})
    except OSError:
        pass
    return sorted(out, key=lambda e: e["name"].lower())


# ── Estado de la corrida activa (una sola a la vez, ver docstring) ─────────
class RunState:
    # Con las barras de progreso partidas por `\r` (ver runner.read_output)
    # el log son miles de redibujos: se guarda una ventana, no todo.
    MAX_LOG = 4000

    def __init__(self, mission_name, mission_dir, run_obj, mode, has_ms, dband=False):
        self.mission_name = mission_name
        self.mission_dir = mission_dir
        self.run = run_obj
        self.mode = mode
        self.has_ms = has_ms
        self.dband = dband
        self.log_lines = []
        self.log_dropped = 0        # cuántas se descartaron por la ventana
        self.done = False
        self.returncode = None
        self.error = None
        # Reloj del SERVIDOR: el cronómetro de la UI se calcula contra esto,
        # así recargar la página no lo reinicia desde cero.
        self.started_at = time.time()
        self.finished_at = None

    @property
    def log_total(self):
        """Índice absoluto de líneas emitidas (incluidas las descartadas)."""
        return self.log_dropped + len(self.log_lines)

    def _append(self, line):
        self.log_lines.append(line)
        excess = len(self.log_lines) - self.MAX_LOG
        if excess > 0:
            del self.log_lines[:excess]
            self.log_dropped += excess

    async def consume(self):
        # try/finally: pase lo que pase acá adentro, `done` TIENE que quedar
        # marcado. Cuando una excepción escapaba de este método (ver el
        # ValueError de readline en runner.py) el proceso terminaba bien pero
        # la UI se quedaba esperando un `done` que ya nunca iba a llegar.
        try:
            async for line in self.run.read_output():
                self._append(line)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._append(f"[webapp] error leyendo la salida del pipeline: {self.error}")
        finally:
            # wait() explícito: si read_output() abortó, nadie cosechó el
            # proceso todavía y `returncode` seguiría en None (la UI no
            # podría distinguir éxito de fallo).
            try:
                self.returncode = await self.run.wait()
            except Exception:
                self.returncode = self.run.returncode
            self.finished_at = time.time()
            self.done = True

    def elapsed(self):
        return (self.finished_at or time.time()) - self.started_at


# Se conserva DESPUÉS de terminar (no se limpia): es lo que permite volver a
# la pantalla de progreso —o al log de error— tras recargar el navegador, en
# vez de perder el motivo de la falla y tener que rearrancar a ciegas.
_state: RunState | None = None


def _mission_dir(name, create=False):
    """(nombre saneado, directorio) de una misión.

    `create` SOLO en los endpoints que realmente reciben datos (upload/start).
    Un GET de lectura —/status, /sample, /view— sobre un nombre inexistente no
    debe crear nada: el directorio quedaría como una misión fantasma vacía en
    la lista del selector."""
    safe = sanitize_mission_name(name)
    d = RUNS_ROOT / safe
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return safe, d


def _running_mission():
    """Nombre de la misión que está procesándose AHORA, o None."""
    return _state.mission_name if (_state and not _state.done) else None


def _refuse_if_other_mission_running(safe):
    """Rechaza activar una misión DISTINTA de la que está procesándose.

    activate_mission() redirige processing/outputs/preprocessing/geovisor/tiles
    por symlink (ver core/runner.py) — es un estado GLOBAL del contenedor, no
    algo por pestaña. Sin esta guarda, abrir el geovisor de una misión vieja
    mientras otra está corriendo mueve esos symlinks debajo del pipeline en
    curso, que sigue escribiendo tan tranquilo en el directorio equivocado: los
    productos de la misión que corre terminan mezclados dentro de la misión que
    se abrió para mirar, con un simple clic en el selector.

    Todos los endpoints que tocan los symlinks o los archivos de una misión
    (/activate, /view, /clear-uploads, DELETE) tienen que protegerse igual."""
    running = _running_mission()
    if running is not None and running != safe:
        raise HTTPException(
            409, f"«{running}» está procesándose ahora mismo. Abrir otra misión "
                 f"redirigiría los directorios de trabajo del pipeline en curso "
                 f"y mezclaría los productos de las dos. Espera a que termine.")


def _sample_raster(path: Path, lat: float, lon: float, band_idx: int = 1):
    """Lee UN valor de un GeoTIFF en (lat, lon) WGS84 — para la ficha 'qué
    significa este punto' del geovisor. None si el archivo no existe, el
    punto cae fuera del raster o es nodata — nunca se inventa un valor."""
    if not path.is_file():
        return None
    ds = gdal.Open(str(path))
    gt = ds.GetGeoTransform()
    inv = gdal.InvGeoTransform(gt)
    if inv is None:
        return None
    raster_srs = osr.SpatialReference(wkt=ds.GetProjection())
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    mx, my, _ = osr.CoordinateTransformation(wgs84, raster_srs).TransformPoint(lon, lat)
    px, py = (int(v) for v in gdal.ApplyGeoTransform(inv, mx, my))
    if px < 0 or py < 0 or px >= ds.RasterXSize or py >= ds.RasterYSize:
        return None
    band = ds.GetRasterBand(band_idx)
    arr = band.ReadAsArray(px, py, 1, 1)
    if arr is None:
        return None
    v = float(arr[0, 0])
    nodata = band.GetNoDataValue()
    if (nodata is not None and abs(v - nodata) < 1e-6) or not math.isfinite(v):
        return None
    return v


# Umbral de "misma zona": dos misiones cuyo centro cae a menos de esta
# distancia se consideran el mismo incidente/predio capturado en fechas
# distintas — es lo único que habilita el comparador Antes/Después en el
# geovisor (con una sola captura no hay nada que comparar). 400m cubre el
# tamaño típico de un vuelo de esta escala (decenas de hectáreas) sin
# confundir dos incendios cercanos pero distintos.
SAME_LOCATION_M = 400


def _mission_center(mission_dir: Path):
    bounds_path = mission_dir / "tiles" / "bounds.json"
    if not bounds_path.is_file():
        return None
    try:
        b = json.loads(bounds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    c = b.get("center")
    return tuple(c) if c and len(c) == 2 else None


def _haversine_m(p1, p2):
    r = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (p1[0], p1[1], p2[0], p2[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _related_missions(safe_name: str, mission_dir: Path):
    """Otras misiones (terminadas, con bounds.json) cuyo centro cae dentro
    de SAME_LOCATION_M de esta — candidatas para el comparador temporal."""
    center = _mission_center(mission_dir)
    if center is None:
        return []
    out = []
    for run in scan_existing_runs():
        if run.name == safe_name or not run.has_tiles:
            continue
        other_center = _mission_center(run.path)
        if other_center is None or _haversine_m(center, other_center) > SAME_LOCATION_M:
            continue
        captura = None
        situation_path = run.path / "outputs" / "situation.json"
        if situation_path.is_file():
            try:
                captura = json.loads(situation_path.read_text(encoding="utf-8")).get("captura")
            except (OSError, json.JSONDecodeError):
                pass
        out.append({"name": run.name, "captura": captura})
    out.sort(key=lambda m: m["captura"] or "")
    return out


def _odm_projects(mission_dir: Path):
    """Proyectos ODM ya reconstruidos de esta misión (lo caro: SfM+MVS).
    Su presencia es lo que habilita ofrecer 'reusar' en vez del checkbox
    SKIP_ODM que el usuario tenía que entender y marcar a mano."""
    proc = mission_dir / "processing"
    found = {}
    for key, sub in (("rgb", "rgb_odm"), ("thermal", "thermal_native_odm"),
                     ("multispectral", "multispectral_odm"), ("dband", "dband_odm")):
        d = proc / sub
        found[key] = (d / "opensfm" / "reconstruction.json").exists()
    return found


def _validate_reuse_odm(mission_dir, mode, has_multispectral, reuse_odm, dband=False):
    """"Reusar lo ya reconstruido" (SKIP_ODM=1) es un interruptor GLOBAL en
    docker/entrypoint.sh: salta las TRES reconstrucciones ODM a la vez, no
    sensor por sensor. Si se pide reusar pero algún sensor de ESTA corrida
    nunca se reconstruyó en esta misión —el caso típico: agregar un vuelo
    multiespectral a una misión que ya tenía RGB+térmico— esa reconstrucción
    nueva quedaría salteada igual, y la misión sigue con procesamiento
    incompleto sin ningún error claro (detect_area_afectada.py recién
    fallaría más adelante, con un mensaje que no menciona el sensor real)."""
    if not reuse_odm:
        return []
    odm_prev = _odm_projects(mission_dir)
    faltantes = []
    if mode in ("rgb", "rgb+thermal") and not odm_prev["rgb"]:
        faltantes.append("RGB")
    if mode in ("rgb+thermal", "thermal") and not odm_prev["thermal"]:
        faltantes.append("térmico")
    if has_multispectral and not odm_prev["multispectral"]:
        faltantes.append("multiespectral")
    if dband and not odm_prev["dband"]:
        faltantes.append("banda D")
    if not faltantes:
        return []
    return [f"Pediste reusar reconstrucciones ODM, pero {' y '.join(faltantes)} "
            f"nunca se reconstruyó en esta misión — no hay nada que reusar ahí. "
            "Elige «Empezar de cero» (reconstruye lo nuevo; lo que ya existe, "
            "ODM lo retoma solo y no lo rehace de cero)."]


# ── Landing + páginas estáticas de la webapp (no confundir con geovisor/) ──
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _run_ok_from_disk(mission_dir):
    """True/False según outputs/run_summary.json — docker/entrypoint.sh lo
    escribe SIEMPRE al terminar una corrida, incluso si falló (trap
    resumen_al_salir). Es la fuente que sobrevive a que el contenedor se
    reinicie (redeploy): _state (en memoria) se pierde en cada arranque
    nuevo del proceso, así que sin esto una misión que falló justo antes de
    un redeploy volvía a mostrarse como si nunca hubiera corrido — el botón
    de la tarjeta (webapp/static/index.html::loadMissions) mandaba al
    geovisor en vez de a "corregir y reintentar". None = nunca corrió."""
    p = mission_dir / "outputs" / "run_summary.json"
    if not p.is_file():
        return None
    try:
        with open(p) as f:
            return bool(json.load(f).get("ok"))
    except Exception:
        return None


@app.get("/api/missions")
def list_missions():
    out = []
    for r in scan_existing_runs():
        ok = _run_ok_from_disk(r.path)
        # _state manda si es la misión que corrió en ESTA sesión del
        # proceso — más al día que el archivo en el instante entre que
        # termina la corrida y run_summary.py alcanza a escribirlo.
        if _state and _state.done and _state.mission_name == r.name:
            ok = _state.returncode == 0
        out.append({"name": r.name, "has_outputs": r.has_outputs,
                    "has_processing": r.has_processing, "has_tiles": r.has_tiles,
                    "ok": ok})
    running = _running_mission()
    last = None
    if _state and _state.done:
        last = {"name": _state.mission_name, "ok": _state.returncode == 0}
    return {"missions": out, "active": running, "last_run": last}


@app.get("/api/missions/{mission}/status")
def mission_status(mission: str):
    """Todo lo que la UI necesita para decidir qué ofrecer: qué se subió (por
    tipo real de archivo), qué reconstrucciones ODM ya existen (para ofrecer
    reusarlas) y en qué quedó la última corrida."""
    safe, mission_dir = _mission_dir(mission)
    uploads = {k: classify_files(_listdir_names(mission_dir / "raw" / k))
               for k in UPLOAD_KINDS}
    tiles_ready = (mission_dir / "tiles" / "bounds.json").exists()
    running = bool(_state and not _state.done and _state.mission_name == safe)
    last_run = None
    if _state and _state.mission_name == safe and _state.done:
        last_run = {"ok": _state.returncode == 0, "returncode": _state.returncode,
                    "mode": _state.mode, "elapsed": round(_state.elapsed())}
    # elapsed del SERVIDOR: la UI lo usa para que el cronómetro sobreviva a
    # un F5 en vez de arrancar de nuevo en 00:00.
    elapsed = round(_state.elapsed()) if (_state and _state.mission_name == safe) else None
    return {"name": safe, "uploads": uploads, "odm": _odm_projects(mission_dir),
            "export_enabled": export_disponible(),
            "export_host_root": EXPORT_HOST_DIR,
            "default_export_dir": (os.path.join(EXPORT_HOST_DIR, safe)
                                   if export_disponible() else ""),
            "import_enabled": import_disponible(),
            "import_host_root": IMPORT_HOST_DIR,
            "imported": {k: _imported_folders(mission_dir / "raw" / k) for k in UPLOAD_KINDS},
            "has_tiles": tiles_ready, "running": running, "last_run": last_run,
            "elapsed": elapsed, "mode": _state.mode if (_state and _state.mission_name == safe) else None,
            "has_multispectral": _state.has_ms if (_state and _state.mission_name == safe) else None,
            # Otras misiones en la misma zona: es lo único que habilita el
            # comparador Antes/Después en el geovisor (ver _related_missions).
            "related_missions": _related_missions(safe, mission_dir) if tiles_ready else []}


# ── Subida de archivos: un POST por archivo (carpeta completa vía
# <input webkitdirectory multiple> en el navegador). Solo el nombre base del
# archivo se conserva (sin subcarpetas) — el pipeline busca por SUFIJO de
# nombre en TODO SOURCE_DIR recursivamente (ver docker/setup-data.sh), no
# importa la estructura de carpetas original. ──
@app.post("/api/upload")
async def upload(mission: str = Form(...), kind: str = Form(...), file: UploadFile = None):
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"kind debe ser uno de {UPLOAD_KINDS}")
    if file is None:
        raise HTTPException(400, "falta el archivo")
    safe, mission_dir = _mission_dir(mission, create=True)
    dest_dir = mission_dir / "raw" / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = os.path.basename(file.filename or "archivo")
    dest = dest_dir / fname
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"ok": True, "mission": safe, "file": fname, "size": dest.stat().st_size}


@app.get("/api/import/browse")
def import_browse(path: str = ""):
    """Explorador de carpetas del lado del SERVIDOR para elegir la fuente de
    import-local() a golpe de clic, sin escribir ninguna ruta a mano.

    No hay otra forma de hacer esto sin subir nada: ningún navegador le da a
    JS la ruta absoluta real de una carpeta del disco, ni con
    <input webkitdirectory> ni arrastrándola desde el escritorio — es una
    restricción de seguridad deliberada del navegador, no algo que dependa
    de esta implementación. Por eso el picker vive acá: lista lo que ya está
    montado en IMPORT_MOUNT (./raptor webapp --import DIR), y el navegador
    solo necesita mostrar nombres y dejar click."""
    if not import_disponible():
        raise HTTPException(400, "no hay carpeta de importación montada — arrancá con --import DIR")
    src = import_host_a_contenedor(path) if path else IMPORT_MOUNT
    if src is None or not os.path.isdir(src):
        raise HTTPException(400, f"«{path}» no existe o está fuera de la carpeta montada.")

    parent = None
    if os.path.normpath(src) != IMPORT_MOUNT:
        parent = import_contenedor_a_host(os.path.dirname(src.rstrip("/")))

    entries = []
    try:
        subdirs = sorted(
            (e for e in os.scandir(src) if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name.lower())
    except OSError as exc:
        raise HTTPException(400, f"no se pudo leer «{path}»: {exc}")
    for e in subdirs:
        hint = classify_files(_quick_hint_names(e.path))
        entries.append({"name": e.name, "host_path": import_contenedor_a_host(e.path),
                        "hint": {"rgb": hint["rgb"], "thermal": hint["thermal"],
                                 "ms": hint["ms"], "dband": hint["dband"], "total": hint["total"]}})
    return {"host_path": import_contenedor_a_host(src), "parent": parent, "entries": entries}


def _quick_hint_names(path, max_files=800, max_dirs=60):
    """Pista de contenido para el explorador: sufijos de archivo bajo `path`,
    RECURSIVO pero acotado. Probado en vivo con "solo el nivel superior": una
    carpeta de vuelo real (drone SD card / export típico) casi nunca tiene
    las fotos sueltas ahí mismo — vienen en subcarpetas (rgb_mosaico/,
    termica/, 100MEDIA/) — así que un hint no-recursivo mostraba "vacía"
    para carpetas con miles de fotos adentro. Los topes (800 archivos / 60
    subcarpetas) evitan que listar una carpeta con muchas hijas (donde ESTE
    hint corre una vez por hija) se vuelva lento — alcanzan de sobra para
    reconocer un vuelo real, que rara vez pasa de un puñado de subcarpetas."""
    names = []
    dirs_seen = 0
    for _root, _dirs, files in os.walk(path):
        dirs_seen += 1
        names.extend(files)
        if len(names) >= max_files or dirs_seen >= max_dirs:
            break
    return names


@app.post("/api/missions/{mission}/import-local")
def import_local(mission: str, kind: str = Form(...), host_path: str = Form(...)):
    """Agrega una carpeta ya presente en el disco del servidor como fuente
    para `kind`, sin pasar por el navegador — ver el comentario de
    IMPORT_MOUNT/IMPORT_HOST_DIR más arriba sobre por qué esto existe.
    host_path es una ruta del HOST (normalmente la devuelve /api/import/browse,
    no se escribe a mano); se resuelve a la ruta real del contenedor y se
    valida que caiga DENTRO de la carpeta montada antes de tocar nada.

    raw/<kind>/ es SIEMPRE un directorio real que contiene un symlink por
    cada carpeta agregada — nunca copia ni un byte, así que elegir varias
    carpetas (p.ej. dos tarjetas de un mismo vuelo, 100MEDIA/ + 101MEDIA/)
    solo agrega symlinks nuevos, no reemplaza lo que ya había. Reportado en
    vivo: una versión anterior de esto copiaba/hardlinkeaba archivo por
    archivo al reimportar — "¿por qué se duplican, si ya están acá?", con
    razón (un hardlink sigue siendo una segunda ruta real). _add_import_folder()
    también migra sola el diseño viejo (raw/<kind> como symlink de tope
    directo a una única carpeta), si lo encuentra."""
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"kind debe ser uno de {UPLOAD_KINDS}")
    src_root = import_host_a_contenedor(host_path)
    if src_root is None:
        raise HTTPException(400, f"«{host_path}» está fuera de la carpeta montada "
                                  f"({IMPORT_HOST_DIR or 'ninguna — arrancá con --import DIR'}).")
    if not os.path.isdir(src_root):
        raise HTTPException(400, f"«{host_path}» no existe o no es una carpeta.")

    safe, mission_dir = _mission_dir(mission, create=True)
    dest_dir = mission_dir / "raw" / kind
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    entry = _add_import_folder(dest_dir, src_root)

    uploads = classify_files(_listdir_names(dest_dir))
    return {"ok": True, "mission": safe, "kind": kind, "entry": entry, "uploads": uploads}


@app.post("/api/missions/{mission}/import-remove")
def import_remove(mission: str, kind: str = Form(...), name: str = Form(...)):
    """Saca UNA carpeta ya agregada (no todo el lote) — para cuando se
    eligió una de más entre varias, sin tener que vaciar y volver a
    agregar el resto."""
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"kind debe ser uno de {UPLOAD_KINDS}")
    if _state and not _state.done and _state.mission_name == sanitize_mission_name(mission):
        raise HTTPException(409, "no se pueden borrar archivos de una misión que está procesándose")
    _safe, mission_dir = _mission_dir(mission)
    dest_dir = mission_dir / "raw" / kind
    entry = dest_dir / os.path.basename(name)
    if not entry.is_symlink():
        raise HTTPException(404, "esa carpeta no está agregada")
    entry.unlink()
    return {"ok": True, "uploads": classify_files(_listdir_names(dest_dir))}


@app.post("/api/missions/{mission}/clear-uploads")
def clear_uploads(mission: str, kind: str = Form(...)):
    """Vaciar un lote ya subido — para cuando se eligió la carpeta equivocada,
    sin tener que inventar otro nombre de misión."""
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"kind debe ser uno de {UPLOAD_KINDS}")
    if _state and not _state.done and _state.mission_name == sanitize_mission_name(mission):
        raise HTTPException(409, "no se pueden borrar archivos de una misión que está procesándose")
    _safe, mission_dir = _mission_dir(mission)
    d = mission_dir / "raw" / kind
    if d.is_symlink():
        # import-local (ver ese endpoint) puede haber dejado esto como un
        # symlink directo a una carpeta del usuario, no una copia — hay que
        # sacar SOLO el symlink. shutil.rmtree sobre un symlink de nivel
        # superior tira OSError a propósito (no sigue el link para borrar
        # del otro lado), así que ni hace falta el chequeo: unlink() es lo
        # correcto acá y no toca un solo byte de la carpeta original.
        d.unlink()
    elif d.is_dir():
        shutil.rmtree(d)
    return {"ok": True}


# ── Validación previa: la misma decisión que toma docker/entrypoint.sh, pero
# ANTES de lanzarlo, para poder devolver un mensaje corregible en el
# formulario en vez de un fallo a mitad de una corrida larga. ──
def _validate(mode, has_ms, uploads):
    rt, ms = uploads["rgb_thermal"], uploads["multispectral"]
    errors = []
    if mode not in VALID_MODES:
        errors.append(f"Modo inválido: {mode}")
        return errors
    if mode != "none":
        if rt["rgb"] == 0:
            errors.append(
                "Elegiste procesar el vuelo RGB/térmico pero no hay fotos RGB "
                "(*_V.JPG / *_W.JPG) entre los archivos subidos"
                + (f" ({rt['total']} archivos)" if rt["total"] else "")
                + ". Sube la carpeta del vuelo M3T/H20T, o desactiva ese sensor "
                  "si solo vas a procesar el multiespectral.")
        if mode in ("rgb+thermal", "thermal") and rt["thermal"] == 0:
            errors.append(
                "Elegiste un producto con térmico pero no hay fotos térmicas "
                f"(*_T.JPG) entre los {rt['total']} archivos subidos. Agrega "
                "las fotos térmicas del vuelo, o cambia el producto a «Solo RGB».")
    if has_ms and ms["ms"] == 0:
        errors.append(
            "Activaste el multiespectral pero no hay bandas (*_MS_*.TIF) entre "
            f"los {ms['total']} archivos subidos en ese sensor. Sube la carpeta "
            "del vuelo M3M, o desactiva ese sensor.")
    elif has_ms:
        por_banda = ms.get("ms_bands", {})
        faltantes = [b for b in MS_BANDS if not por_banda.get(b)]
        if faltantes:
            errors.append(
                f"Al vuelo multiespectral le faltan bandas enteras: {', '.join(faltantes)}. "
                f"ODM necesita las 4 (G, R, RE, NIR) para agrupar cada captura. "
                "Revisa que hayas subido la carpeta completa del M3M.")
        elif ms.get("incompletas"):
            incompletas = ms["incompletas"]
            muestra = "; ".join(f"{p} (solo {', '.join(b)})" for p, b in incompletas[:3])
            errors.append(
                f"{len(incompletas)} captura(s) multiespectral(es) están incompletas: {muestra}"
                + (f"; y {len(incompletas) - 3} más" if len(incompletas) > 3 else "")
                + ". Cada captura del M3M son 4 archivos y ODM las empareja por nombre: "
                  "si a una le falta una banda, la reconstrucción aborta después de "
                  "una hora de procesamiento. Sube las bandas que faltan o quita "
                  "esas capturas.")
    if mode == "none" and not has_ms:
        errors.append("No seleccionaste ningún sensor para procesar.")
    return errors


def _validate_export(mission_dir, export_dir, products, raster_fmt, vector_fmt, epsg):
    """Valida la configuración de entrega y devuelve (errores, dict EXPORT_*).

    `export_dir` es una ruta del HOST. Se traduce a la ruta equivalente dentro
    del contenedor y se comprueba acá —no adentro del pipeline— que se pueda
    crear y escribir: descubrirlo al final, con la misión ya procesada, sería
    exactamente lo que la validación previa existe para evitar."""
    errors = []
    if not export_dir:
        return errors, {}

    if not export_disponible():
        return ([f"Para exportar hay que arrancar con una carpeta del host montada: "
                 f"«./raptor webapp --export /ruta/de/entregas». Sin eso los productos "
                 f"solo quedan dentro del contenedor y se pierden al cerrarlo."], {})

    host_dir = export_dir
    if not os.path.isabs(host_dir):
        host_dir = os.path.join(EXPORT_HOST_DIR, host_dir)
    export_dir = host_a_contenedor(host_dir)
    if export_dir is None:
        return ([f"«{host_dir}» está fuera de la carpeta montada ({EXPORT_HOST_DIR}). "
                 f"Elige una ruta adentro de esa carpeta, o reinicia con "
                 f"«./raptor webapp --export» apuntando a otro lado."], {})

    desconocidos = [p for p in products if p not in EXPORT_PRODUCTS]
    if desconocidos:
        errors.append(f"Productos de exportación desconocidos: {', '.join(desconocidos)}.")
    if not products:
        errors.append("Elegiste exportar pero no marcaste ningún producto.")
    if raster_fmt not in EXPORT_RASTER_FORMATS:
        errors.append(f"Formato ráster inválido: {raster_fmt}.")
    if vector_fmt not in EXPORT_VECTOR_FORMATS:
        errors.append(f"Formato vectorial inválido: {vector_fmt}.")

    epsg = (epsg or "").strip().lower()
    if epsg and epsg != "source":
        # RuntimeError incluido: con gdal.UseExceptions() activo (arriba de este
        # módulo), ImportFromEPSG LANZA en vez de devolver un código de error.
        try:
            osr.SpatialReference().ImportFromEPSG(int(epsg))
        except (ValueError, TypeError, RuntimeError):
            errors.append(f"EPSG «{epsg}» no existe o no lo reconoce PROJ. "
                          "Usa un código numérico válido, p. ej. 9377.")

    try:
        os.makedirs(export_dir, exist_ok=True)
        probe = os.path.join(export_dir, ".raptor_write_test")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
    except OSError as exc:
        errors.append(f"No se puede escribir en «{host_dir}»: {exc}.")

    if errors:
        return errors, {}
    return [], {"EXPORT_DIR": export_dir,
                # La ruta del host viaja al pipeline para que el manifiesto y
                # el resumen JSON digan dónde quedaron los archivos EN EL DISCO
                # DEL USUARIO, no la ruta interna que no le sirve a nadie.
                "EXPORT_HOST_DIR": host_dir,
                "EXPORT_PRODUCTS": ",".join(products),
                "EXPORT_RASTER_FORMAT": raster_fmt,
                "EXPORT_VECTOR_FORMAT": vector_fmt,
                "EXPORT_EPSG": epsg or "source"}


def _parse_products(raw):
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


@app.post("/api/missions/{mission}/check-export")
def check_export(mission: str, export_dir: str = Form(""), export_epsg: str = Form("")):
    """Comprueba carpeta destino y EPSG sin arrancar nada, para poder avisar
    mientras el usuario escribe. El error típico es una ruta del host que no
    está montada dentro del contenedor: acá se ve al instante en vez de al
    final de una corrida de una hora."""
    _safe, mission_dir = _mission_dir(mission)
    errors, env = _validate_export(mission_dir, export_dir.strip(), ["rgb"],
                                   "cog", "geojson", export_epsg)
    resolved = env.get("EXPORT_HOST_DIR", "")
    nombre = None
    epsg = (export_epsg or "").strip().lower()
    if epsg and epsg != "source" and not errors:
        srs = osr.SpatialReference()
        try:
            srs.ImportFromEPSG(int(epsg))
            nombre = srs.GetName()
        except (ValueError, TypeError, RuntimeError):
            pass
    return {"ok": not errors, "errors": errors, "resolved": resolved, "crs_name": nombre}


@app.get("/api/missions/{mission}/quality-estimate")
def quality_estimate(mission: str, preset: str = "", quality: int | None = None,
                     mode: str = "rgb+thermal", terreno: str = "plano",
                     has_multispectral: bool = False, n_photos: int | None = None):
    """A qué resolución van a salir los productos y cuánto se espera que
    tarde — mismo cálculo que ve `./raptor run` antes de arrancar
    (scripts/hardware.py, fuente única) y que corre después de verdad en
    docker/entrypoint.sh.

    La respuesta trae `opciones`: los CINCO presets con su tiempo estimado
    para esta misión concreta. Es lo que hace que la elección sea informada
    — antes era un slider de 0 a 100 y el usuario no tenía forma de saber
    qué compraba cada tramo.

    `quality` (0-100) se sigue aceptando: el mapeo a preset lo hace
    scripts/hardware.py.

    n_photos: el navegador ya lo sabe apenas el usuario elige la carpeta
    (classify() sobre el FileList, sin esperar nada del servidor) y lo manda
    directo — contar desde disco daba "0 fotos" mientras la subida seguía en
    curso, porque el archivo recién llega a raw/ cuando termina de subirse,
    no cuando se elige. Si no llega (llamada vieja a la API, o sin JS), se
    cae al conteo de lo que ya está en disco como antes."""
    elegido = preset.strip() or (str(quality) if quality is not None else "estandar")
    if n_photos is None:
        _safe, mission_dir = _mission_dir(mission)
        uploads = {k: classify_files(_listdir_names(mission_dir / "raw" / k))
                   for k in UPLOAD_KINDS}
        n_photos = 0
        if mode != "none":
            if mode != "thermal":
                n_photos += uploads["rgb_thermal"]["rgb"]
            if mode in ("rgb+thermal", "thermal"):
                n_photos += uploads["rgb_thermal"]["thermal"]
        if has_multispectral:
            # classify_files ya cuenta TIFs individuales (una banda = un
            # archivo), así que esto ya es "4 por captura" sin multiplicar
            # de nuevo.
            n_photos += uploads["multispectral"]["ms"]
    try:
        return estimate_message(elegido, n_photos, terreno=terreno)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/missions/{mission}/validate")
def validate_mission(mission: str, mode: str = Form(...), has_multispectral: bool = Form(False)):
    _safe, mission_dir = _mission_dir(mission)
    uploads = {k: classify_files(_listdir_names(mission_dir / "raw" / k))
               for k in UPLOAD_KINDS}
    errors = _validate(mode, has_multispectral, uploads)
    return {"ok": not errors, "errors": errors}


# ── Iniciar procesamiento ───────────────────────────────────────────────
@app.post("/api/missions/{mission}/start")
async def start_mission(mission: str, mode: str = Form(...),
                        has_multispectral: bool = Form(False),
                        dband: bool = Form(False),
                        reuse_odm: bool = Form(False),
                        preset: str = Form("estandar"),
                        terreno: str = Form("plano"),
                        export_dir: str = Form(""),
                        export_products: str = Form(""),
                        export_raster_format: str = Form("cog"),
                        export_vector_format: str = Form("geojson"),
                        export_epsg: str = Form("9377")):
    global _state
    if _state is not None and not _state.done:
        raise HTTPException(409, f"Ya hay una misión procesándose: {_state.mission_name}")

    safe, mission_dir = _mission_dir(mission, create=True)
    uploads = {k: classify_files(_listdir_names(mission_dir / "raw" / k))
               for k in UPLOAD_KINDS}
    errors = _validate(mode, has_multispectral, uploads)
    errors += _validate_reuse_odm(mission_dir, mode, has_multispectral, reuse_odm, dband)
    export_errors, export_env = _validate_export(
        mission_dir, export_dir.strip(), _parse_products(export_products),
        export_raster_format.strip().lower(), export_vector_format.strip().lower(),
        export_epsg)
    errors += export_errors
    try:
        # Valida preset Y terreno contra la tabla real (scripts/hardware.py)
        # en vez de repetir la lista acá, que sería una segunda copia para
        # desincronizar.
        hw_preset(preset, terreno=terreno)
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        raise HTTPException(400, " ".join(errors))

    # MODE=none no toca SOURCE_DIR (ver entrypoint.sh), pero igual se pasa un
    # directorio existente para no depender de ese detalle.
    source_dir = mission_dir / "raw" / "rgb_thermal"
    source_dir.mkdir(parents=True, exist_ok=True)
    ms_source_dir = mission_dir / "raw" / "multispectral" if has_multispectral else None

    activate_mission(mission_dir)
    progress_file = mission_dir / "progress.ndjson"
    run_obj = PipelineRun(
        mode=mode, source_dir=source_dir, ms_source_dir=ms_source_dir,
        skip_odm=reuse_odm, port=8080, progress_file=progress_file,
        export=export_env, preset=preset, dband=dband, terreno=terreno,
    )
    await run_obj.start()
    _state = RunState(safe, mission_dir, run_obj, mode, has_multispectral, dband)
    asyncio.create_task(_state.consume())
    return {"ok": True, "mission": safe}


# ── Progreso en vivo (SSE): lee el mismo PROGRESS_FILE que escribe
# core/runner.py (PipelineRun._env) y reempaqueta cada línea como
# Server-Sent Events para el HUD del geovisor. ──
@app.get("/api/missions/{mission}/events")
async def events(mission: str):
    safe = sanitize_mission_name(mission)
    if _state is None or _state.mission_name != safe:
        raise HTTPException(404, "esa misión no está corriendo ni corrió en esta sesión")
    state = _state

    async def gen():
        pos = 0
        log_pos = 0   # índice ABSOLUTO (la ventana de log descarta las viejas)
        # Primer evento: el reloj del servidor y el plan real de la corrida,
        # para que una pestaña que se reengancha reconstruya la vista completa
        # (cronómetro incluido) sin depender de lo que tenía en memoria.
        yield ("data: " + json.dumps({
            "kind": "hello", "elapsed": round(state.elapsed()),
            "mode": state.mode, "has_multispectral": state.has_ms, "dband": state.dband,
            "done": state.done}) + "\n\n")
        while True:
            pos, evs = parse_progress_events(str(state.run.progress_file), pos)
            for ev in evs:
                yield f"data: {json.dumps({'kind': 'progress', **ev})}\n\n"
            if log_pos < state.log_total:
                start = max(log_pos, state.log_dropped)
                for line in state.log_lines[start - state.log_dropped:]:
                    yield f"data: {json.dumps({'kind': 'log', 'line': line})}\n\n"
                log_pos = state.log_total
            if state.done:
                yield ("data: " + json.dumps({
                    "kind": "done", "returncode": state.returncode,
                    "elapsed": round(state.elapsed()), "error": state.error}) + "\n\n")
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/missions/{mission}/cancel")
def cancel_mission(mission: str):
    """Cancela la corrida en curso — ODM y el resto de las herramientas
    (opensfm, exiftool, gdal2tiles...) mueren de verdad, no solo el bash de
    entrypoint.sh (ver el comentario de PipelineRun.kill() en
    core/runner.py sobre por qué eso NO alcanzaba). El estado queda "done"
    apenas la tarea que lee la salida del proceso lo detecta (consume(),
    en su bloque finally) — no hace falta marcarlo acá."""
    safe = sanitize_mission_name(mission)
    if _state is None or _state.mission_name != safe:
        raise HTTPException(404, "esa misión no está procesándose ahora")
    if _state.done:
        raise HTTPException(409, "esa misión ya terminó")
    _state.run.kill()
    return {"ok": True}


@app.delete("/api/missions/{mission}")
def delete_mission(mission: str):
    """Borra por completo una misión (raw/, processing/, outputs/, tiles/) —
    para deshacerse de pruebas o corridas fallidas sin salir al shell del
    contenedor. Bloqueada mientras la misión está procesándose: borrar el
    directorio debajo de un pipeline corriendo (activate_mission() ya la
    symlinkeó a processing/outputs/...) lo dejaría escribiendo a la nada."""
    safe = sanitize_mission_name(mission)
    if _state is not None and _state.mission_name == safe and not _state.done:
        raise HTTPException(409, "no se puede eliminar una misión que está procesándose")
    mission_dir = RUNS_ROOT / safe
    if not mission_dir.is_dir():
        raise HTTPException(404, "esa misión no existe")
    shutil.rmtree(mission_dir)
    return {"ok": True}


@app.get("/api/missions/{mission}/log")
def run_log(mission: str, tail: int = 200):
    """Cola del log de la última corrida — para mostrar el motivo real de una
    falla al volver a la misión (o tras recargar), sin dejar al usuario
    adivinando qué pasó."""
    safe = sanitize_mission_name(mission)
    if _state is not None and _state.mission_name == safe:
        return {"lines": _state.log_lines[-tail:], "done": _state.done,
                "returncode": _state.returncode, "source": "memoria"}

    # Respaldo en DISCO: el estado en memoria solo cubre la última corrida de
    # ESTE proceso, así que tras reiniciar el servidor —o al mirar cualquier
    # misión que no sea la última— se perdía el motivo de la falla aunque el
    # log siguiera ahí. outputs/logs/ lo tiene: es el mismo destino al que
    # escribe run_odm() y run_quiet del Makefile.
    _safe, mission_dir = _mission_dir(safe)
    logs = mission_dir / "outputs" / "logs"
    if not logs.is_dir():
        raise HTTPException(404, "esa misión no tiene logs guardados")
    archivos = sorted(logs.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not archivos:
        raise HTTPException(404, "esa misión no tiene logs guardados")
    ultimo = archivos[-1]
    try:
        lineas = ultimo.read_text(errors="replace").splitlines()
    except OSError as exc:
        raise HTTPException(500, f"no se pudo leer {ultimo.name}: {exc}")
    return {"lines": lineas[-tail:], "done": True, "returncode": None,
            "source": f"outputs/logs/{ultimo.name}"}


SEVERITY_LABELS = {1: "leve", 2: "leve", 3: "moderado", 4: "severo"}
SEVERITY_RECS = {
    "leve": "Sin anomalía relevante en este punto — no requiere acción inmediata.",
    "moderado": "Daño moderado — sumar a la ronda de verificación cuando se pueda.",
    "severo": "Daño alto — priorizar verificación en terreno antes de dar la zona por controlada.",
}


@app.get("/api/missions/{mission}/sample")
def sample_point(mission: str, lat: float, lon: float):
    """Ficha 'qué significa este punto' del geovisor: valores REALES leídos
    de los rasters de la misión en (lat, lon), no aproximaciones calculadas
    en el navegador. Cada campo queda en null si ese producto no existe
    para la misión (p.ej. sin multiespectral) o el punto cae fuera de su
    cobertura — el frontend decide cómo mostrar esa ausencia, nunca se
    rellena acá con un valor inventado."""
    _safe, mission_dir = _mission_dir(mission)
    outputs = mission_dir / "outputs"
    sev_val = _sample_raster(outputs / "severidad_class.tif", lat, lon)
    temp_val = _sample_raster(outputs / "thermal_orthomosaic.tif", lat, lon)
    ndvi_val = _sample_raster(outputs / "indices" / "ndvi.tif", lat, lon)
    sev_class = int(sev_val) if sev_val is not None else None
    sev_label = SEVERITY_LABELS.get(sev_class)

    confianza = captura = None
    situation_path = outputs / "situation.json"
    if situation_path.is_file():
        try:
            s = json.loads(situation_path.read_text(encoding="utf-8"))
            confianza, captura = s.get("confianza"), s.get("captura")
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "dentro_del_area": sev_class is not None,
        "severidad": sev_label,
        "temperatura_c": round(temp_val, 1) if temp_val is not None else None,
        "ndvi": round(ndvi_val, 2) if ndvi_val is not None else None,
        "confianza": confianza,
        "captura": captura,
        "recomendacion": SEVERITY_RECS.get(sev_label),
    }


# ── Activar misión (symlinks) sin redirigir — la llama el propio geovisor
# ANTES de leer tiles/bounds.json. /view/{mission} activa y redirige, pero
# eso solo pasa al ENTRAR desde la webapp: un F5 sobre la URL ya redirigida
# (/geovisor/index.html?mission=X) es un GET directo a un archivo estático,
# nunca pasa por /view/ de nuevo — si mientras tanto se activó OTRA misión
# (o ninguna), el geovisor quedaba mostrando bounds.json de lo que sea que
# esté activo en ese momento, no el de la URL. Idempotente: activar la
# misión que ya está activa no hace nada raro.
@app.post("/api/missions/{mission}/activate")
def activate_mission_endpoint(mission: str):
    safe, mission_dir = _mission_dir(mission)
    _refuse_if_other_mission_running(safe)
    if not (mission_dir / "tiles" / "bounds.json").exists():
        raise HTTPException(404, "esta misión todavía no tiene tiles generados")
    activate_mission(mission_dir)
    return {"ok": True, "mission": safe}


# ── Geovisor: activa la misión (symlinks) y redirige a la MISMA página que
# usa el flujo "en vivo" (?mission=<nombre>) — un solo punto de entrada al
# geovisor para misión recién arrancada o ya terminada. El geovisor decide
# solo si hay algo en curso: conecta a /api/missions/<mision>/events y, si el
# servidor no la tiene activa (404), simplemente no muestra el HUD de
# progreso. Sirve los estáticos ya existentes de geovisor/ (index.html/
# app.js/style.css/tiles/outputs) — cero cambios ahí. El guardado del polígono
# va como endpoint POST aparte porque StaticFiles es de solo lectura. ──
@app.get("/view/{mission}")
def view_mission(mission: str):
    safe, mission_dir = _mission_dir(mission)
    _refuse_if_other_mission_running(safe)
    running = _running_mission() == safe
    if not running and not (mission_dir / "tiles" / "bounds.json").exists():
        raise HTTPException(404, "esta misión todavía no tiene tiles generados")
    activate_mission(mission_dir)
    return RedirectResponse(url=f"/geovisor/index.html?mission={safe}")


@app.post("/geovisor/api/save-area-afectada")
async def save_area_afectada(request: Request):
    """Persiste el polígono de área afectada editado a mano en el geovisor."""
    body = await request.json()
    if body.get("type") != "FeatureCollection":
        raise HTTPException(400, "se esperaba un GeoJSON FeatureCollection")
    path = GEOVISOR_DIR / "outputs" / "area_afectada.geojson"
    # geovisor/outputs es un SYMLINK a /app/outputs, que puede no existir
    # todavía si el servidor arrancó antes de procesar nada. Se crea el destino
    # real: Path.mkdir(exist_ok=True) sobre un symlink colgante falla con
    # FileExistsError, porque comprueba is_dir() —que sigue el enlace y da
    # False— y después choca contra el enlace en sí.
    try:
        os.makedirs(os.path.realpath(path.parent), exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, f"no se pudo guardar el polígono: {exc}")
    # El proceso corre como root; sin esto el archivo queda root:root y bloquea
    # ediciones o lecturas posteriores desde el host.
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass
    return {"ok": True}


app.mount("/geovisor", StaticFiles(directory=str(GEOVISOR_DIR), html=True, follow_symlink=True), name="geovisor")
# follow_symlink=True: webapp/static/fonts es un symlink a geovisor/fonts
# (una sola copia de Rubik para las dos UIs) — sin esto Starlette no lo sigue
# y las fuentes devuelven 404.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), follow_symlink=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"""
╔══════════════════════════════════════════════════╗
║  🛰️  Pipeline UAV de respuesta rápida            ║
║                                                  ║
║  Abrir en el navegador:                          ║
║  → http://localhost:{port}                          ║
╚══════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=port)
