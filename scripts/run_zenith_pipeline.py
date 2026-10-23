#!/usr/bin/env python3
"""CLI wrapper delgado para ejecutar el pipeline 2.5D con Zenith en RAPTOR."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.zenith_runner import run_zenith_25d


def main():
    parser = argparse.ArgumentParser(description="RAPTOR 2.5D Zenith Photogrammetry Pipeline")
    parser.add_argument("--input", "-i", "--source-dir", required=True, dest="input", help="Directorio con imágenes del vuelo (RGB y/o Térmicas)")
    parser.add_argument("--output", "-o", "--output-dir", default="outputs", dest="output", help="Directorio de salida para productos")
    parser.add_argument("--progress-file", help="Ruta al archivo progress.ndjson")
    parser.add_argument("--dem-source", default="auto", choices=["auto", "copernicus", "flat"], help="Fuente de DEM")
    parser.add_argument("--dem", help="Ruta a DEM local explícito")
    parser.add_argument("--flat-elevation", type=float, help="Cota constante en metros para plano de contingencia")
    parser.add_argument("--emissivity", type=float, help="Emisividad superficial [0.10, 1.00]")
    parser.add_argument("--humidity", type=float, help="Humedad relativa en % [1.0, 100.0]")
    parser.add_argument("--distance", type=float, help="Distancia al objetivo en metros [1.0, 25.0]")
    parser.add_argument("--reflection", type=float, help="Temperatura reflejada en °C [-40.0, 500.0]")
    parser.add_argument("--crs", help="Sistema de coordenadas de referencia (e.g. EPSG:32618)")

    args = parser.parse_args()

    # Si vienen variables de entorno del contenedor
    source_dir = Path(os.environ.get("SOURCE_DIR", args.input))
    output_dir = Path(os.environ.get("OUTPUT_DIR", args.output))
    progress_file = Path(os.environ["PROGRESS_FILE"]) if "PROGRESS_FILE" in os.environ else (Path(args.progress_file) if args.progress_file else None)
    external_dem = Path(os.environ["EXTERNAL_DEM"]) if os.environ.get("EXTERNAL_DEM") else (Path(args.dem) if args.dem else None)
    flat_elevation = float(os.environ["FLAT_ELEVATION"]) if os.environ.get("FLAT_ELEVATION") else args.flat_elevation

    emissivity = float(os.environ["THERMAL_EMISSIVITY"]) if "THERMAL_EMISSIVITY" in os.environ and os.environ["THERMAL_EMISSIVITY"].strip() else args.emissivity
    humidity = float(os.environ["THERMAL_HUMIDITY"]) if "THERMAL_HUMIDITY" in os.environ and os.environ["THERMAL_HUMIDITY"].strip() else args.humidity
    distance = float(os.environ["THERMAL_DISTANCE"]) if "THERMAL_DISTANCE" in os.environ and os.environ["THERMAL_DISTANCE"].strip() else args.distance
    reflection = float(os.environ["THERMAL_REFLECTION"]) if "THERMAL_REFLECTION" in os.environ and os.environ["THERMAL_REFLECTION"].strip() else args.reflection
    crs = os.environ.get("EXPORT_EPSG", args.crs)
    if crs and crs.lower() == "source":
        crs = None

    try:
        run_zenith_25d(
            source_dir=source_dir,
            output_dir=output_dir,
            progress_file=progress_file,
            dem_source=args.dem_source,
            external_dem=external_dem,
            flat_elevation=flat_elevation,
            emissivity=emissivity,
            humidity=humidity,
            distance=distance,
            reflection=reflection,
            crs=crs,
        )
    except Exception as e:
        print(f"❌ ERROR en pipeline Zenith 2.5D: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
