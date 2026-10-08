#!/usr/bin/env python3
"""outputs/flight_quality.json — calidad del LEVANTAMIENTO (cómo se voló),
no del dato de impacto (eso es situation.json). Reemplaza el viejo campo
"confianza" del geovisor: ese número solo decía qué fracción del
ortomosaico térmico tenía dato real, algo que no le dice nada útil a quien
decide si repetir el vuelo — un reporte de Terra/Agisoft/Pix4D habla de
solape, velocidad e imágenes reconstruidas, que es lo que de verdad
predice si el resultado es confiable.

Cuatro señales, todas derivadas de lo que ODM y el GPS del dron ya
registraron (nada inventado):

  1. Imágenes reconstruidas: cuántas de las capturadas terminaron con una
     pose válida en la reconstrucción SfM (opensfm/reconstruction.json)
     contra cuántas se capturaron (img_list.txt). Una foto que no se
     reconstruye casi siempre es blur de movimiento, mala luz o muy poco
     solape con sus vecinas — la señal más directa de "algo salió mal en
     el vuelo", por sensor.

  2. Solape de cámaras: reusa _rgb_camera_overlap() de
     trim_low_overlap_edges.py (la misma huella de cámara que ya gobierna
     el recorte de bordes de bajo solape) para contar, en la zona
     realmente cubierta, cuántas fotos ven cada punto del terreno. La
     literatura de fotogrametría cita 3+ como el mínimo para una
     reconstrucción confiable (ambigüedad geométrica por debajo de eso).

  3. Velocidad de vuelo: de outputs/flight_path.geojson (GPS+tiempo de
     cada captura, ya lo escribe export_flight_path.py). Volar rápido para
     el intervalo de disparo configurado es la causa más común de solape
     insuficiente y de foto borrosa — Terra/Pix4D la reportan por la misma
     razón.

  4. GSD real logrado: tamaño de píxel del ortomosaico, no una promesa de
     antemano.

"calidad" es un resumen cualitativo (buena/regular/baja) de las mismas
señales — no una cifra aparte inventada para verse bien.

Uso: python3 scripts/compute_flight_quality.py
Lee: processing/{rgb,thermal_native,multispectral}_odm/{img_list.txt,
     opensfm/reconstruction.json, images.json}, outputs/flight_path.geojson,
     outputs/rgb_orthomosaic.tif (o el primer ortomosaico disponible)
Escribe: outputs/flight_quality.json (omite el archivo si no hay ni un solo
     reconstruction.json — misión que no llegó a esa etapa)
"""
import json
import math
import os
import sys
from datetime import datetime

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trim_low_overlap_edges import _rgb_camera_overlap  # noqa: E402  (reusa la huella de cámara real)
from gdal_open_retry import gdal_open_retry  # noqa: E402

FLIGHT_PATH = "outputs/flight_path.geojson"
OUT_PATH = "outputs/flight_quality.json"

# (nombre visible, dir de ODM, ortomosaico de referencia para GSD)
SENSORES = [
    ("rgb", "processing/rgb_odm", "outputs/rgb_orthomosaic.tif"),
    ("termico", "processing/thermal_native_odm", "outputs/thermal_orthomosaic.tif"),
    ("multiespectral", "processing/multispectral_odm", "outputs/multispectral_orthomosaic.tif"),
]

# EXIF Model -> nombre de equipo legible. "ZH20T" es el Model REAL de un
# Zenmuse H20T (Make=DJI, Model=ZH20T) — nunca se lee del proyecto térmico,
# porque prepare_thermal_native_odm.py fuerza ese mismo string en CUALQUIER
# misión para disparar la calibración nativa de ODM (ver ese script); la
# fuente confiable es siempre el proyecto RGB, que no se toca.
EQUIPO_POR_MODEL = {
    "ZH20T": "DJI Zenmuse H20T",
    "H20T": "DJI Zenmuse H20T",
    "M3T": "DJI Mavic 3T",
    "M3M": "DJI Mavic 3M",
}


def _reconstruccion_por_sensor():
    """Imágenes capturadas vs reconstruidas, por sensor que sí corrió ODM."""
    out = {}
    for nombre, odm_dir, _ in SENSORES:
        img_list = os.path.join(odm_dir, "img_list.txt")
        recon_path = os.path.join(odm_dir, "opensfm", "reconstruction.json")
        if not os.path.isfile(img_list):
            continue
        with open(img_list) as f:
            capturadas = sum(1 for line in f if line.strip())
        reconstruidas = 0
        if os.path.isfile(recon_path):
            with open(recon_path) as f:
                data = json.load(f)
            rec = data[0] if isinstance(data, list) else data
            reconstruidas = len(rec.get("shots", {}))
        if capturadas:
            out[nombre] = {
                "capturadas": capturadas,
                "reconstruidas": reconstruidas,
                "pct": round(100 * reconstruidas / capturadas, 0),
            }
    return out


def _equipo():
    """Make/Model EXIF real, leído SIEMPRE del proyecto RGB (nunca del
    térmico, forzado a Model=ZH20T sin importar la cámara real — ver
    docstring del módulo)."""
    images_json = "processing/rgb_odm/images.json"
    if not os.path.isfile(images_json):
        return None
    with open(images_json) as f:
        imgs = json.load(f)
    if not imgs:
        return None
    make = (imgs[0].get("camera_make") or "").strip()
    model = (imgs[0].get("camera_model") or "").strip()
    if not make and not model:
        return None
    return EQUIPO_POR_MODEL.get(model, f"{make} {model}".strip())


def _haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _velocidad_vuelo():
    """Velocidad entre capturas CONSECUTIVAS de un mismo sensor (mezclar
    sensores que disparan al mismo tiempo desde la misma posición daría
    distancia ~0 entre "capturas" que en realidad son simultáneas)."""
    if not os.path.isfile(FLIGHT_PATH):
        return None, None
    with open(FLIGHT_PATH) as f:
        fp = json.load(f)
    caps = [ft for ft in fp.get("features", [])
            if ft["properties"].get("kind") == "capture" and ft["properties"].get("time")]
    if not caps:
        return None, None
    # Un solo sensor alcanza: la trayectoria física es la misma para todos
    # los que disparan juntos. "rgb" es el que casi siempre existe.
    sensores_presentes = {c["properties"].get("sensor") for c in caps}
    sensor = "rgb" if "rgb" in sensores_presentes else sorted(sensores_presentes)[0]
    caps = sorted((c for c in caps if c["properties"].get("sensor") == sensor),
                  key=lambda c: c["properties"]["time"])
    if len(caps) < 2:
        return None, None
    speeds = []
    for a, b in zip(caps, caps[1:]):
        try:
            ta = datetime.fromisoformat(a["properties"]["time"])
            tb = datetime.fromisoformat(b["properties"]["time"])
        except ValueError:
            continue
        dt = (tb - ta).total_seconds()
        if dt <= 0:
            continue
        lon1, lat1 = a["geometry"]["coordinates"]
        lon2, lat2 = b["geometry"]["coordinates"]
        speeds.append(_haversine_m(lon1, lat1, lon2, lat2) / dt)
    if not speeds:
        return None, None
    return round(float(np.mean(speeds)), 1), round(float(np.max(speeds)), 1)


def _solape():
    """Reusa la huella de cámara real (misma que gobierna el recorte de
    bordes) para contar, en la zona cubierta, cuántas fotos ven cada punto
    del terreno."""
    ref = next((path for _, _, path in SENSORES if os.path.isfile(path)), None)
    recon = next((os.path.join(odm_dir, "opensfm", "reconstruction.json")
                  for _, odm_dir, path in SENSORES
                  if path == ref and os.path.isfile(os.path.join(odm_dir, "opensfm", "reconstruction.json"))),
                 None)
    if ref is None or recon is None:
        return None, None
    ds = gdal_open_retry(ref)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize
    ds = None
    ov_full, _, _, _ = _rgb_camera_overlap(gt, W, H, recon_path=recon, proj_wkt=proj)
    if ov_full is None:
        return None, None
    cubierto = ov_full[ov_full > 0]
    if not cubierto.size:
        return None, None
    p50 = float(np.median(cubierto))
    pct_ok = 100 * float((cubierto >= 3).sum()) / cubierto.size
    return round(p50, 1), round(pct_ok, 0)


def _gsd_cm():
    out = {}
    for nombre, _, path in SENSORES:
        if os.path.isfile(path):
            ds = gdal_open_retry(path)
            out[nombre] = round(abs(ds.GetGeoTransform()[1]) * 100, 1)
            ds = None
    return out


def _calidad(recon_pct_min, solape_p50, velocidad_media):
    """Rótulo cualitativo único, para quien solo quiere el semáforo — el
    detalle completo sigue disponible en cada campo. Umbrales de
    fotogrametría estándar: 3+ solape es el mínimo citado para
    reconstrucción confiable; por debajo de 70% de imágenes reconstruidas
    algo sistemático salió mal (no ruido aislado)."""
    señales_malas = 0
    señales_regulares = 0
    if recon_pct_min is not None:
        if recon_pct_min < 70:
            señales_malas += 1
        elif recon_pct_min < 90:
            señales_regulares += 1
    if solape_p50 is not None:
        if solape_p50 < 3:
            señales_malas += 1
        elif solape_p50 < 4:
            señales_regulares += 1
    if señales_malas:
        return "baja"
    if señales_regulares:
        return "regular"
    return "buena"


def main():
    reconstruccion = _reconstruccion_por_sensor()
    if not reconstruccion:
        print("⚠ no hay reconstruction.json de ningún sensor — omitiendo flight_quality.json")
        return 0

    velocidad_media, velocidad_max = _velocidad_vuelo()
    solape_p50, solape_pct_ok = _solape()
    recon_pct_min = min((v["pct"] for v in reconstruccion.values()), default=None)

    resultado = {
        "equipo": _equipo(),
        "reconstruccion": reconstruccion,
        "solape_p50": solape_p50,
        "solape_pct_area_ok": solape_pct_ok,
        "velocidad_media_ms": velocidad_media,
        "velocidad_max_ms": velocidad_max,
        "gsd_cm": _gsd_cm(),
        "calidad": _calidad(recon_pct_min, solape_p50, velocidad_media),
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(OUT_PATH, 0o666)
    except OSError:
        pass

    print(f"✅ {OUT_PATH}")
    print(f"  calidad: {resultado['calidad']} | solape p50: {solape_p50} | "
          f"velocidad media: {velocidad_media} m/s | reconstrucción mínima: {recon_pct_min}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
