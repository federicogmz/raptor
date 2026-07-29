#!/usr/bin/env python3
"""Ruta de vuelo (GeoJSON) a partir del GPS EXIF de las capturas.

Corre TEMPRANO en el pipeline —apenas las imágenes están organizadas, antes
de ODM— para que el geovisor tenga algo real que mostrar desde el primer
minuto en vez de una pantalla vacía durante la hora que tarda la
reconstrucción: dónde voló el dron, cuántas capturas hay y qué extensión
cubren. Los ortomosaicos se van sumando después, a medida que cada producto
termina (ver `make tiles` incremental en docker/entrypoint.sh).

Produce:
  outputs/flight_path.geojson  — LineString del recorrido + Point por captura
  geovisor/tiles/bounds.json   — SOLO si todavía no existe: centro/zoom
                                  provisorio para que el visor abra sobre la
                                  zona. generate_tiles.py lo pisa después con
                                  la versión completa (rangos de bandas, etc).
"""
import glob
import json
import os
import subprocess
import sys

SOURCES = [
    ("rgb", "data/rgb_mosaico", ("*_V.JPG", "*_W.JPG")),
    ("thermal", "data/termica_mosaico", ("*_T.JPG",)),
    ("multispectral", "data/multiespectral_mosaico", ("*_MS_G.TIF",)),
]
OUT = "outputs/flight_path.geojson"
BOUNDS = "geovisor/tiles/bounds.json"


def read_gps(paths):
    """GPS de un lote de imágenes en UNA sola llamada a exiftool (-j -n).
    Con cientos de fotos, invocarlo por archivo tardaría minutos; en lote es
    aproximadamente un segundo."""
    if not paths:
        return []
    out = []
    # -@ desde stdin: la lista de archivos puede superar el límite de ARG_MAX.
    r = subprocess.run(
        ["exiftool", "-j", "-n", "-GPSLatitude", "-GPSLongitude",
         "-GPSAltitude", "-DateTimeOriginal", "-FileName", "-@", "-"],
        input="\n".join(paths), capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout.strip():
        return []
    try:
        meta = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    for d in meta:
        lat, lon = d.get("GPSLatitude"), d.get("GPSLongitude")
        if lat is None or lon is None:
            continue
        # exiftool devuelve DateTimeOriginal en formato EXIF "YYYY:MM:DD
        # HH:MM:SS" (dos puntos también en la fecha) — new Date() de
        # JavaScript no lo reconoce como ISO 8601 y devuelve Invalid Date en
        # silencio. Se normaliza acá, en el único lugar que lee el EXIF crudo,
        # para que todo lo que consume esta hora despues (situation.json, el
        # geovisor) reciba un string ISO real sin tener que repetir el parche.
        raw_time = d.get("DateTimeOriginal", "")
        iso_time = raw_time
        if raw_time and len(raw_time) >= 19:
            iso_time = raw_time[:10].replace(":", "-") + "T" + raw_time[11:19]
        out.append({
            "lat": float(lat), "lon": float(lon),
            "alt": float(d.get("GPSAltitude") or 0.0),
            "name": d.get("FileName", ""),
            "time": iso_time,
        })
    # Orden temporal: es lo que hace que la LineString sea el recorrido real
    # y no un garabato en orden de listado del filesystem.
    out.sort(key=lambda p: (p["time"], p["name"]))
    return out


def main():
    os.makedirs("outputs", exist_ok=True)
    features, total = [], 0

    for sensor, folder, patterns in SOURCES:
        paths = []
        for pat in patterns:
            paths.extend(glob.glob(os.path.join(folder, pat)))
        pts = read_gps(sorted(paths))
        if not pts:
            continue
        total += len(pts)
        print(f"  {sensor}: {len(pts)} capturas con GPS")

        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                          "coordinates": [[p["lon"], p["lat"]] for p in pts]},
            "properties": {"sensor": sensor, "kind": "track", "captures": len(pts)},
        })
        for p in pts:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                "properties": {"sensor": sensor, "kind": "capture",
                                "name": p["name"], "alt": p["alt"], "time": p["time"]},
            })

    if not features:
        print("  ⚠ ninguna captura con GPS EXIF — sin ruta de vuelo")
        return 0

    with open(OUT, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    try:
        os.chmod(OUT, 0o666)
    except OSError:
        pass
    print(f"✅ {OUT} ({total} capturas)")

    # bounds.json provisorio: solo si generate_tiles.py todavía no escribió el
    # definitivo. Sin esto el geovisor no sabe dónde centrar y abre en el
    # océano; con esto abre sobre la zona del vuelo desde el minuto uno.
    if not os.path.exists(BOUNDS):
        lats = [c[1] for ft in features if ft["geometry"]["type"] == "LineString"
                for c in ft["geometry"]["coordinates"]]
        lons = [c[0] for ft in features if ft["geometry"]["type"] == "LineString"
                for c in ft["geometry"]["coordinates"]]
        if lats and lons:
            os.makedirs(os.path.dirname(BOUNDS), exist_ok=True)
            with open(BOUNDS, "w") as f:
                json.dump({"center": [sum(lats) / len(lats), sum(lons) / len(lons)],
                            "zoom": 16, "preliminary": True,
                            "thermal_range": [0, 60], "index_ranges": {},
                            "ms_band_ranges": {}}, f)
            try:
                os.chmod(BOUNDS, 0o666)
            except OSError:
                pass
            print(f"✅ {BOUNDS} (provisorio, centro del vuelo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
