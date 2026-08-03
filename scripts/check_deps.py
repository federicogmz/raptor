#!/usr/bin/env python3
"""Verifica en runtime las dependencias que se HEREDAN de la imagen base.

requirements.txt fija lo que RAPTOR instala encima, pero GDAL, pyproj y scipy
vienen del venv de `opendronemap/odm:gpu`, que es un tag móvil. Pinearlas en
pip forzaría recompilaciones y peleas con el SuperBuild de ODM, así que en vez
de eso se comprueban acá: si la base cambia por debajo, esto lo dice de una
con un mensaje accionable, en vez de que aparezca como un resultado raro en
una misión.

Uso: python3 scripts/check_deps.py   (exit 1 si algo no cumple)
"""
import importlib.metadata as md
import sys

# paquete -> (mínima soportada, máxima soportada exclusiva o None, por qué)
HEREDADAS = {
    "GDAL":   ("3.8.0", None,
               "lectura/escritura de todos los rásters y la exportación COG"),
    "pyproj": ("3.6.0", None,
               "proyección de huellas de cámara y reproyección de la entrega"),
    "scipy":  ("1.10.0", None,
               "morfología del recorte de confiabilidad (ndimage)"),
}

# La ABI de numpy es un caso aparte: no es "conviene", es que el _gdal_array.so
# de ODM está compilado contra 1.x y con 2.x revienta con ImportError.
NUMPY_MAX = 2


def _tupla(v):
    out = []
    for parte in v.split(".")[:3]:
        num = "".join(c for c in parte if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def main():
    fallos = []

    try:
        import numpy as np
        if _tupla(np.__version__)[0] >= NUMPY_MAX:
            fallos.append(
                f"numpy {np.__version__}: el _gdal_array.so de ODM está compilado "
                f"contra la ABI de numpy 1.x y rompe con 2.x apenas se usa gdal "
                f"desde Python. Fija numpy<2 en requirements.txt.")
        else:
            # La prueba que de verdad importa: que el binding funcione.
            from osgeo import gdal
            gdal.UseExceptions()
            ds = gdal.GetDriverByName("MEM").Create("", 2, 2, 1)
            ds.GetRasterBand(1).ReadAsArray()
            ds = None
    except Exception as exc:
        fallos.append(f"el binding Python de GDAL no funciona: {exc}")

    for paquete, (minima, maxima, para_que) in HEREDADAS.items():
        try:
            v = md.version(paquete)
        except md.PackageNotFoundError:
            fallos.append(f"{paquete} no está instalado (hace falta para {para_que})")
            continue
        if _tupla(v) < _tupla(minima):
            fallos.append(f"{paquete} {v} < {minima} mínimo (se usa para {para_que})")
        elif maxima and _tupla(v) >= _tupla(maxima):
            fallos.append(f"{paquete} {v} >= {maxima} no soportado (se usa para {para_que})")
        else:
            print(f"  ✅ {paquete} {v}")

    if fallos:
        print("\n❌ La imagen base cambió por debajo de lo que RAPTOR soporta:")
        for f in fallos:
            print(f"   • {f}")
        print("\n   La base opendronemap/odm:gpu es un tag móvil. Si el cambio es")
        print("   aceptable, actualiza los rangos en scripts/check_deps.py y corre")
        print("   `make test` para confirmar que el recorte no cambió.")
        return 1
    print("✅ dependencias heredadas de la imagen base: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
