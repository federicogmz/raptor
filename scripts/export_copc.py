#!/usr/bin/env python3
"""Exporta las nubes de puntos densas georreferenciadas de ODM a formato
COPC (Cloud Optimized Point Cloud): mismo LAZ pero organizado en octree
interno, para que Potree/QGIS/CloudCompare puedan streamearlo por partes
en vez de descargar el archivo completo.

Usa el `pdal` que trae el propio SuperBuild de ODM (no está en PATH por
defecto en la imagen base).

Uso:
  python3 scripts/export_copc.py              # las 4 nubes que existan
  python3 scripts/export_copc.py rgb thermal   # solo las que se piden

La forma con nombres de sensor la usa docker/entrypoint.sh apenas termina
CADA reconstrucción — el archivo georreferenciado
(odm_georeferencing/odm_georeferenced_model.laz) ya existe ahí, mucho antes
de que el DSM/ortofoto/post-procesamiento de ese mismo sensor terminen, así
que no hace falta esperar ni a los otros sensores ni al resto de esta
misión para tener la nube de puntos lista para abrir en QGIS/Potree.
"""
import os, sys, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402

PDAL_BIN = "/code/SuperBuild/install/bin/pdal"
OUTPUTS = "outputs"

# Como mucho cuatro nubes y cada `pdal translate` es un proceso aparte sobre
# su propio archivo — se convierten a la vez en lugar de una atrás de otra.
# PDAL sí carga la nube en memoria, así que se pasa por la misma cuenta
# memory-aware que el resto del pipeline con un perfil holgado.
_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(12.3, override=int(_override) if _override else None)

# sensor -> (directorio del proyecto ODM, nombre de salida). Los cuatro
# proyectos escriben un georreferenciado propio en odm_georeferencing/ — RGB,
# multiespectral y térmico nativo con nube DENSA (post-MVS); banda D con la
# nube DISPERSA de OpenSfM nomás (--fast-orthophoto salta MVS, no georef).
# Antes esta lista solo tenía 3: banda D quedaba afuera sin ninguna razón
# real — su .laz georreferenciado existe igual, más chico pero real.
PROJECTS = {
    "rgb": (os.environ.get("ODM_RGB_DIR", "processing/rgb_odm"), "point_cloud_rgb.copc.laz"),
    "multispectral": (os.environ.get("ODM_MS_DIR", "processing/multispectral_odm"), "point_cloud_multispectral.copc.laz"),
    "thermal": (os.environ.get("ODM_THNAT_DIR", "processing/thermal_native_odm"), "point_cloud_thermal.copc.laz"),
    "dband": (os.environ.get("ODM_DBAND_DIR", "processing/dband_odm"), "point_cloud_dband.copc.laz"),
}


def _pdal():
    if shutil.which("pdal"):
        return "pdal"
    if os.path.isfile(PDAL_BIN):
        return PDAL_BIN
    return None


def _al_dia(src, dst):
    """True si `dst` ya existe y es al menos tan nuevo como `src` — evita
    reconvertir en el pase de seguridad final lo que la exportación por
    sensor ya dejó listo apenas terminó SU reconstrucción."""
    return os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)


def main():
    pdal = _pdal()
    if pdal is None:
        print("❌ pdal no encontrado (ni en PATH ni en SuperBuild)")
        sys.exit(1)

    os.makedirs(OUTPUTS, exist_ok=True)

    # Argumentos = nombres de sensor (modo por sensor, sin salteo por
    # idempotencia — si se lo llama explícitamente es porque se sabe que la
    # nube recién quedó lista). Sin argumentos = las 4, saltando las que ya
    # estén al día (pase de seguridad).
    pedidos = sys.argv[1:] or list(PROJECTS)
    desconocidos = [s for s in pedidos if s not in PROJECTS]
    if desconocidos:
        print(f"❌ sensor(es) desconocido(s): {', '.join(desconocidos)} "
              f"(válidos: {', '.join(PROJECTS)})")
        sys.exit(1)

    pendientes = []
    for sensor in pedidos:
        proj_dir, out_name = PROJECTS[sensor]
        src = os.path.join(proj_dir, "odm_georeferencing", "odm_georeferenced_model.laz")
        dst = os.path.join(OUTPUTS, out_name)
        if not os.path.isfile(src):
            continue
        if not sys.argv[1:] and _al_dia(src, dst):
            print(f"  ✓ {dst}: ya al día, omitiendo")
            continue
        pendientes.append((src, dst))

    if not pendientes:
        print("  ⚠ No se encontró ninguna nube georreferenciada nueva para exportar")
        print("\n✅ 0 nube(s) de puntos exportada(s) en formato COPC")
        return

    def _convertir(par):
        src, dst = par
        r = subprocess.run([pdal, "translate", src, dst, "--writer", "copc"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ❌ {src} → {dst}: {r.stderr[:400]}")
            return False
        print(f"  ✅ {dst} ({os.path.getsize(dst)/(1024*1024):.0f} MB, desde {src})")
        return True

    with ThreadPoolExecutor(max_workers=min(NPROCS, len(pendientes))) as pool:
        resultados = list(pool.map(_convertir, pendientes))

    if not all(resultados):
        sys.exit(1)
    print(f"\n✅ {len(resultados)} nube(s) de puntos exportada(s) en formato COPC")


if __name__ == "__main__":
    main()
