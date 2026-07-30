#!/usr/bin/env python3
"""Exporta las nubes de puntos densas georreferenciadas de ODM a formato
COPC (Cloud Optimized Point Cloud): mismo LAZ pero organizado en octree
interno, para que Potree/QGIS/CloudCompare puedan streamearlo por partes
en vez de descargar el archivo completo.

Usa el `pdal` que trae el propio SuperBuild de ODM (no está en PATH por
defecto en la imagen base).
"""
import os, sys, shutil, subprocess

PDAL_BIN = "/code/SuperBuild/install/bin/pdal"
OUTPUTS = "outputs"

# (directorio del proyecto ODM, nombre de salida) — los tres proyectos con
# reconstrucción 3D propia completa (RGB siempre; MS y térmico nativo si la
# misión los trae). El térmico usa el renderizador nativo de ODM (malla +
# textura + ortofoto real, ver prepare_thermal_native_odm.py), así que su nube
# densa es un producto por derecho propio y no un subproducto de las poses.
PROJECTS = [
    (os.environ.get("ODM_RGB_DIR", "processing/rgb_odm"), "point_cloud_rgb.copc.laz"),
    (os.environ.get("ODM_MS_DIR", "processing/multispectral_odm"), "point_cloud_multispectral.copc.laz"),
    (os.environ.get("ODM_THNAT_DIR", "processing/thermal_native_odm"), "point_cloud_thermal.copc.laz"),
]


def _pdal():
    if shutil.which("pdal"):
        return "pdal"
    if os.path.isfile(PDAL_BIN):
        return PDAL_BIN
    return None


def main():
    pdal = _pdal()
    if pdal is None:
        print("❌ pdal no encontrado (ni en PATH ni en SuperBuild)")
        sys.exit(1)

    os.makedirs(OUTPUTS, exist_ok=True)
    exported = 0
    for proj_dir, out_name in PROJECTS:
        src = os.path.join(proj_dir, "odm_georeferencing", "odm_georeferenced_model.laz")
        if not os.path.isfile(src):
            continue
        dst = os.path.join(OUTPUTS, out_name)
        result = subprocess.run(
            [pdal, "translate", src, dst, "--writer", "copc"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  ❌ {src} → {dst}: {result.stderr[:400]}")
            sys.exit(1)
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        print(f"  ✅ {dst} ({size_mb:.0f} MB, desde {src})")
        exported += 1

    if exported == 0:
        print("  ⚠ No se encontró ninguna nube georreferenciada "
              "(odm_georeferencing/odm_georeferenced_model.laz) en ningún proyecto")
    print(f"\n✅ {exported} nube(s) de puntos exportada(s) en formato COPC")


if __name__ == "__main__":
    main()
