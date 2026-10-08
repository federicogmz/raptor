#!/usr/bin/env python3
"""
Prepara las térmicas para el pipeline ODM NATIVO: malla 3D + textura +
ortofoto real vía el propio renderizador de ODM, sin blending heurístico
propio.

ODM tiene soporte NATIVO para calibración radiométrica DJI H20T
(opendm/thermal.py) pero exige un formato específico que nuestra fuente (DJI
M3T, no "ZH20T") no calza sola:
  - TIFF uint16 = round((°C + 273.15) * 100)  ("calibrated grayscale tif", el
    mismo convenio de encoding que usa DJI para el H20T)
  - EXIF Make=DJI, Model=ZH20T (dispara la rama de calibración correcta en
    opendm/thermal.py::dn_to_temperature — la rama genérica alternativa,
    pensada para cámaras FLIR, crashea con un TypeError/NoneType en nuestros
    datos porque no hay tag RawThermalImage que extraer)
  - XMP Camera:BandName=LWIR (dispara photo.is_thermal()==True; namespace
    http://pix4d.com/camera/1.0, mismo que usa el M3M — ver camera_ns.exiftool.config)

Partimos de los TIFF Float32 °C YA calibrados por convert_thermal_tiff.py (SDK
oficial DJI, con GPS/gimbal ya embebido) — este script SOLO cambia el
ENCODING, no la calibración: round-trip verificado exacto (21.55°C → K100 →
21.55°C tras pasar por opendm.thermal.dn_to_temperature real).

Produce:
  1. processing/thermal_native_odm/images/*.tif (uint16 K100, EXIF/XMP completo)
  2. processing/thermal_native_odm/geo.txt

Uso:
  python3 scripts/prepare_thermal_native_odm.py
"""
import os, sys, glob, json, subprocess
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

SRC_DENOISED = "preprocessing/thermal_dji_sdk_denoised"
SRC_RAW      = "preprocessing/thermal_dji_sdk"
OUTPUT_DIR   = "processing/thermal_native_odm"
IMAGES_DIR   = os.path.join(OUTPUT_DIR, "images")
GEO_TXT      = os.path.join(OUTPUT_DIR, "geo.txt")
_BASE = os.path.dirname(os.path.abspath(__file__))
EXIFTOOL_CONFIG = os.path.join(_BASE, "camera_ns.exiftool.config")


def _source_dir():
    if os.path.isdir(SRC_DENOISED) and glob.glob(os.path.join(SRC_DENOISED, "*.tif")):
        return SRC_DENOISED
    return SRC_RAW


def main():
    src_dir = _source_dir()
    src_files = sorted(glob.glob(os.path.join(src_dir, "*.tif")))
    if not src_files:
        print(f"❌ ERROR: no se encontraron TIFF térmicos en {src_dir}")
        print("   Corré primero: make sdk-convert [denoise-thermal]")
        sys.exit(1)

    print("=" * 60)
    print("  Preparación térmica NATIVA (ODM malla 3D)")
    print("=" * 60)
    print(f"  Fuente: {src_dir} ({len(src_files)} imágenes)")

    os.makedirs(IMAGES_DIR, exist_ok=True)

    # Limpieza selectiva: solo lo que ya NO corresponde a la fuente actual
    # (otra misión/corrida reutilizando el mismo processing/). Lo que sí
    # corresponde a un frame de la fuente actual se evalúa uno por uno más
    # abajo (existe + abre con GDAL → se reusa; si no, se re-encodea) — un
    # retry sobre una misión ya preparada no debería re-encodear TODO desde
    # cero, solo lo que de verdad falta.
    src_basenames = {os.path.basename(f) for f in src_files}
    obsoletas = [f for f in glob.glob(os.path.join(IMAGES_DIR, "*.tif"))
                 if os.path.basename(f) not in src_basenames]
    if obsoletas:
        print(f"🧹 Limpiando {len(obsoletas)} imágenes obsoletas (otra misión/corrida) …")
        for f in obsoletas:
            os.remove(f)

    # ── 1. Re-encodear Float32 °C → uint16 Kelvin×100 ──────────────────────
    drv = gdal.GetDriverByName("GTiff")
    dst_files = []
    nuevas = 0
    for src in src_files:
        dst = os.path.join(IMAGES_DIR, os.path.basename(src))
        if os.path.exists(dst):
            ds = gdal.Open(dst)
            if ds is not None:
                ds = None
                dst_files.append(dst)
                continue
            os.remove(dst)  # corrupto: se regenera

        ds = gdal.Open(src)
        a = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        k100 = np.round(np.clip((a + 273.15) * 100, 0, 65535)).astype(np.uint16)
        out = drv.Create(dst, k100.shape[1], k100.shape[0], 1, gdal.GDT_UInt16)
        out.GetRasterBand(1).WriteArray(k100)
        out = None
        dst_files.append(dst)
        nuevas += 1
        if nuevas % 100 == 0:
            print(f"  {nuevas} re-encodeadas …")
    print(f"  ✅ {nuevas} TIFF re-encodeados nuevos, {len(dst_files) - nuevas} ya existentes "
          f"reutilizados (°C → Kelvin×100 uint16)")

    # ── 2. EXIF/XMP: copiar GPS/gimbal de la fuente + forzar tags de banda ──
    # -api Compact=Shorthand + -xmp-drone-dji:all: preserva los tags de
    # precision RTK (RtkStdLon/Lat/Hgt) en formato ATRIBUTO XML (el que
    # escribe DJI de fabrica). Sin -api Compact=Shorthand, exiftool los
    # reserializa como elementos XML anidados y el parser XMP propio de ODM
    # (busca '@drone-dji:RtkStdLon' con prefijo @ = atributo) no los
    # reconoce - verificado esta sesion con el mismo bug en RGB (ver
    # Makefile:prepare-rgb).
    print("  Etiquetando EXIF/XMP (GPS + Make=DJI/Model=ZH20T + Camera:BandName=LWIR) …")
    src_pattern = os.path.join(src_dir, "%f.tif")
    # Lista de archivos por STDIN (`-@ -`): una misión térmica son cientos o
    # miles de frames y la línea de comandos tiene un tope duro (ARG_MAX).
    # Mismo patrón que export_flight_path.py.
    result = subprocess.run(
        ["exiftool", "-api", "Compact=Shorthand", "-config", EXIFTOOL_CONFIG, "-overwrite_original",
         "-tagsfromfile", src_pattern,
         "-gps:all", "-xmp-drone-dji:all", "-GimbalYawDegree", "-GimbalPitchDegree", "-GimbalRollDegree",
         "-FlightYawDegree", "-FlightPitchDegree", "-FlightRollDegree",
         "-Make=DJI", "-Model=ZH20T", "-XMP-Camera:BandName=LWIR", "-@", "-"],
        input="\n".join(dst_files),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"❌ ERROR etiquetando EXIF/XMP: {result.stderr[:500]}")
        sys.exit(1)

    # ── 3. geo.txt desde el GPS ya embebido ────────────────────────────────
    # Columnas 8-9 (horizontal_accuracy, vertical_accuracy, opcionales en el
    # formato de ODM - ver opendm/geo.py): sin esto, ODM (opendm/photo.py
    # update_with_geo_entry) pisa con None la precision RTK que el mismo ODM
    # sabe auto-detectar del EXIF cuando NO hay geo.txt, forzando un DOP
    # generico (~metros) en el bundle adjustment en vez de confiar en la
    # precision RTK real (~1-3cm) del propio termico.
    print("  Generando geo.txt …")
    r = subprocess.run(
        ["exiftool", "-j", "-n", "-GPSLatitude", "-GPSLongitude", "-GPSAltitude",
         "-GimbalYawDegree", "-GimbalPitchDegree", "-GimbalRollDegree",
         "-RtkStdLon", "-RtkStdLat", "-RtkStdHgt", "-@", "-"],
        input="\n".join(dst_files),
        capture_output=True, text=True, timeout=180
    )
    data = json.loads(r.stdout)
    lines = ["EPSG:4326"]
    missing = 0
    n_rtk = 0
    for d in data:
        # Nombre desde SourceFile, no por posición: emparejar la fila i de la
        # salida con el archivo i de la entrada asume orden y conteo exactos, y
        # si fallan, geo.txt queda con coordenadas del archivo equivocado — una
        # reconstrucción mal georreferenciada, sin ningún error visible.
        fname = os.path.basename(d.get("SourceFile", ""))
        if not fname:
            missing += 1
            continue
        lat, lon = d.get("GPSLatitude"), d.get("GPSLongitude")
        if lat is None or lon is None:
            missing += 1
            continue
        alt = d.get("GPSAltitude", 0)
        yaw, pitch, roll = d.get("GimbalYawDegree", 0), d.get("GimbalPitchDegree", 0), d.get("GimbalRollDegree", 0)
        std_lon, std_lat, std_hgt = d.get("RtkStdLon"), d.get("RtkStdLat"), d.get("RtkStdHgt")
        if std_lon is not None and std_lat is not None:
            h_acc = max(std_lon, std_lat) * 2.0  # mismo margen de seguridad x2 que ODM aplica internamente
            v_acc = (std_hgt if std_hgt is not None else std_lon) * 2.0
            n_rtk += 1
        else:
            h_acc, v_acc = 3.0, 5.0  # fallback conservador si esta captura puntual no tiene fix RTK
        lines.append(f"{fname}\t{lon}\t{lat}\t{alt}\t{yaw}\t{pitch}\t{roll}\t{h_acc:.5f}\t{v_acc:.5f}")
    if missing:
        print(f"  ⚠ {missing} imágenes sin GPS (omitidas de geo.txt)")
    print(f"  precision RTK disponible: {n_rtk}/{len(dst_files)-missing} imágenes")
    with open(GEO_TXT, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n✅ {len(lines)-1}/{len(dst_files)} imágenes con GPS")
    print(f"   Directorio ODM: {OUTPUT_DIR}/")
    print(f"   geo.txt: {GEO_TXT}")
    print("\nSiguiente paso: ODM térmico nativo (invocado por docker/entrypoint.sh)")


if __name__ == "__main__":
    main()
