#!/usr/bin/env python3
"""
Prepara las bandas multiespectrales (DJI M3M) para ODM.

A diferencia del térmico, las 4 bandas MS ya traen GPS EXIF nativo (RTK) —
no hace falta inyectar nada, solo copiar y generar geo.txt como fallback
(igual que generate_geo.py hace para RGB, generalizado a las 4 bandas).

Produce:
  1. processing/multispectral_odm/images/*_MS_{G,R,RE,NIR}.TIF (copia)
  2. processing/multispectral_odm/geo.txt (fallback para ODM)

Uso:
  python3 scripts/prepare_multispectral_odm.py
"""

import os, sys, glob, json, shutil, subprocess

MS_TIF_DIR  = "data/multiespectral_mosaico"
OUTPUT_DIR  = "processing/multispectral_odm"
IMAGES_DIR  = os.path.join(OUTPUT_DIR, "images")
GEO_TXT     = os.path.join(OUTPUT_DIR, "geo.txt")
BAND_SUFFIXES = ["_MS_G.TIF", "_MS_R.TIF", "_MS_RE.TIF", "_MS_NIR.TIF"]

if not os.path.isdir(MS_TIF_DIR):
    print(f"❌ ERROR: Directorio fuente no encontrado: {MS_TIF_DIR}")
    sys.exit(1)

os.makedirs(IMAGES_DIR, exist_ok=True)

print("=" * 60)
print("  Preparación de bandas multiespectrales para ODM")
print("=" * 60)
print()

ms_files = sorted(
    f for f in os.listdir(MS_TIF_DIR)
    if any(f.upper().endswith(suf) for suf in BAND_SUFFIXES)
)
if not ms_files:
    print(f"❌ No se encontraron bandas *_MS_*.TIF en {MS_TIF_DIR}")
    sys.exit(1)

print(f"  Bandas encontradas: {len(ms_files)}")

# ── Copiar imágenes (limpiar copias previas primero) ───────────────
old = [f for f in os.listdir(IMAGES_DIR) if any(f.upper().endswith(s) for s in BAND_SUFFIXES)]
if old:
    print(f"🧹 Limpiando {len(old)} copias anteriores …")
    for f in old:
        os.remove(os.path.join(IMAGES_DIR, f))

print(f"📋 Copiando {len(ms_files)} bandas …")
for fname in ms_files:
    shutil.copy2(os.path.join(MS_TIF_DIR, fname), os.path.join(IMAGES_DIR, fname))

# ── geo.txt: GPS EXIF nativo (RTK), ya viene en las bandas ─────────
# Se agregan las columnas opcionales 8-9 de ODM (horizontal_accuracy,
# vertical_accuracy, ver opendm/geo.py) desde los tags RtkStdLon/Lat/Hgt del
# propio EXIF. Sin esto, ODM (opendm/photo.py update_with_geo_entry) pisa con
# None la precision RTK que el mismo ODM sabe auto-detectar del EXIF cuando
# NO hay geo.txt, forzando un DOP generico (~metros) en el bundle adjustment
# en vez de confiar en la precision RTK real (~1-3cm) - mismo bug
# diagnosticado y corregido para el termico esta sesion.
print(f"🔍 Extrayendo GPS de {len(ms_files)} bandas con exiftool …")
paths = [os.path.join(IMAGES_DIR, f) for f in ms_files]
result = subprocess.run(
    ["exiftool", "-j", "-n",
     "-GPSLatitude", "-GPSLongitude", "-GPSAltitude",
     "-GimbalYawDegree", "-GimbalPitchDegree", "-GimbalRollDegree",
     "-RtkStdLon", "-RtkStdLat", "-RtkStdHgt"] + paths,
    capture_output=True, text=True, timeout=180
)
if result.returncode != 0:
    print(f"❌ ERROR: exiftool falló:\n{result.stderr}")
    sys.exit(1)

try:
    all_data = json.loads(result.stdout)
except json.JSONDecodeError:
    print("❌ ERROR: no se pudo decodificar la salida de exiftool")
    sys.exit(1)

geo_lines = ["EPSG:4326"]
missing = 0
n_rtk = 0
for i, exif in enumerate(all_data):
    fname = ms_files[i] if i < len(ms_files) else f"unknown_{i}"
    lat = exif.get("GPSLatitude")
    lon = exif.get("GPSLongitude")
    if lat is None or lon is None:
        missing += 1
        continue
    alt = exif.get("GPSAltitude", 0)
    yaw = exif.get("GimbalYawDegree", 0)
    pitch = exif.get("GimbalPitchDegree", 0)
    roll = exif.get("GimbalRollDegree", 0)
    std_lon, std_lat, std_hgt = exif.get("RtkStdLon"), exif.get("RtkStdLat"), exif.get("RtkStdHgt")
    if std_lon is not None and std_lat is not None:
        # mismo margen de seguridad x2 que ODM aplica internamente al auto-detectar estos tags
        h_acc = max(std_lon, std_lat) * 2.0
        v_acc = (std_hgt if std_hgt is not None else std_lon) * 2.0
        n_rtk += 1
    else:
        h_acc, v_acc = 3.0, 5.0  # fallback conservador si esta captura puntual no tiene fix RTK
    geo_lines.append(f"{fname}\t{lon}\t{lat}\t{alt}\t{yaw}\t{pitch}\t{roll}\t{h_acc:.5f}\t{v_acc:.5f}")

if missing:
    print(f"  ⚠ {missing} bandas sin GPS (omitidas de geo.txt)")
print(f"  precision RTK disponible: {n_rtk}/{len(ms_files)-missing} bandas")

with open(GEO_TXT, "w") as f:
    f.write("\n".join(geo_lines) + "\n")

print(f"\n✅ {len(geo_lines)-1}/{len(ms_files)} bandas con GPS")
print(f"   Directorio ODM: {OUTPUT_DIR}/")
print(f"   geo.txt: {GEO_TXT}")
print(f"\nSiguiente paso: ODM multiespectral (invocado por docker/entrypoint.sh)")
