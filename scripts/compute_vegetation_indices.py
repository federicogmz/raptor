#!/usr/bin/env python3
"""Calcula índices de vegetación desde el ortomosaico multiespectral (M3M).

Lee outputs/multispectral_orthomosaic.tif (multibanda, una banda por canal
espectral con su nombre en GetDescription() — ver trim_multispectral()) e
identifica las bandas Green/Red/RedEdge/NIR por nombre (case-insensitive, no
por posición fija: el orden de reconstruction.multi_camera de ODM no está
garantizado). Cada índice sale a su propio GeoTIFF Float32 en outputs/indices/.

Fórmulas en un dict → agregar un índice nuevo no toca la lógica de cómputo.

MSAVI2 (además de NDVI/GNDVI/NDRE): distinto propósito de NDVI, no
redundante. NDVI se satura y se vuelve sensible al brillo del SUELO cuando
la cobertura de dosel es baja (<~30%) — exactamente el escenario de
regeneración post-incendio (dosel disperso sobre ceniza/suelo expuesto).
MSAVI2 (Qi et al. 1994) agrega un factor de ajuste de suelo autocalibrado
por píxel para corregir eso — literatura estándar para monitorear
regeneración de vegetación en terreno de cobertura baja, un caso de uso
DISTINTO de "detectar la mancha quemada" (donde ya se probó en esta sesión
que el brillo crudo separa mejor que cualquier índice, incluidos SAVI/
MSAVI — ver detect_area_afectada.py). Con cobertura de dosel alta MSAVI2
converge con NDVI (mismo valor, sin pérdida); diverge justo donde NDVI es
menos confiable, que es donde aporta.
"""
import os, sys
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

MS_PATH   = "outputs/multispectral_orthomosaic.tif"
OUT_DIR   = "outputs/indices"

# nombre_lógico → substrings que identifican la banda en GetDescription()
# (RedEdge antes que Red: "red" es substring de "rededge")
BAND_ALIASES = {
    "nir":     ["nir"],
    "rededge": ["rededge", "red edge", "red_edge"],
    "red":     ["red"],
    "green":   ["green"],
}

# índice → (banda_a, banda_b) calculado como (a-b)/(a+b)
INDICES = {
    "ndvi":  ("nir", "red"),
    "gndvi": ("nir", "green"),
    "ndre":  ("nir", "rededge"),
}

# MSAVI2 (Modified Soil-Adjusted Vegetation Index, Qi et al. 1994) — no es
# un ratio (a-b)/(a+b) simple, se calcula aparte. Se prefiere sobre SAVI
# (Huete 1988, mismo objetivo con un factor L=0.5 fijo a mano) porque
# MSAVI2 se autocalibra por píxel sin ese parámetro libre — mismo principio,
# una fuente menos de número "tanteado". Ver docstring de main() para la
# justificación de por qué se agrega junto a NDVI/GNDVI/NDRE.
def _msavi2(nir, red):
    with np.errstate(invalid="ignore"):
        return (2 * nir + 1 - np.sqrt(np.maximum((2 * nir + 1) ** 2 - 8 * (nir - red), 0))) / 2


def _find_band(ds, logical_name):
    for i in range(1, ds.RasterCount + 1):
        desc = (ds.GetRasterBand(i).GetDescription() or "").strip().lower()
        for alias in BAND_ALIASES[logical_name]:
            if alias == desc or desc.endswith(alias):
                return i
    return None


def main():
    if not os.path.isfile(MS_PATH):
        print(f"❌ ERROR: {MS_PATH} no encontrado (¿corrió trim-edges-multispectral?)")
        sys.exit(1)

    ds = gdal.Open(MS_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize

    band_idx = {}
    for logical in ("nir", "red", "green", "rededge"):
        idx = _find_band(ds, logical)
        if idx is not None:
            band_idx[logical] = idx
    print(f"Bandas identificadas: { {k: ds.GetRasterBand(v).GetDescription() for k, v in band_idx.items()} }")

    # Máscara de validez real: alpha=0 (última banda) es la única señal
    # confiable de "fuera de cobertura" — ODM rellena ahí con un valor
    # arbitrario en las bandas de dato (visto: 2**32 exacto e IDÉNTICO en
    # las 4 bandas espectrales de una corrida real), lo que hace que
    # (a-b)/(a+b) dé 0.0 "válido" en vez de NaN — un piso artificial en
    # el índice, no vegetación real. abs(a+b)>1e-4 solo no alcanza porque
    # 2**32+2**32 es un número enorme, no cercano a cero.
    footprint_valid = None
    if ds.RasterCount >= max(band_idx.values(), default=0) + 1:
        alpha_idx = ds.RasterCount
        if ds.GetRasterBand(alpha_idx).GetColorInterpretation() == gdal.GCI_AlphaBand:
            footprint_valid = ds.GetRasterBand(alpha_idx).ReadAsArray() > 0

    os.makedirs(OUT_DIR, exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")

    def write_index(name, idx_arr):
        out_path = os.path.join(OUT_DIR, f"{name}.tif")
        out = drv.Create(out_path, W, H, 1, gdal.GDT_Float32,
                          ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
        out.SetGeoTransform(gt)
        out.SetProjection(proj)
        out.GetRasterBand(1).WriteArray(idx_arr)
        out.GetRasterBand(1).SetNoDataValue(float("nan"))
        out = None
        vals = idx_arr[np.isfinite(idx_arr)]
        if vals.size:
            print(f"  ✅ {out_path}: {name.upper()} {vals.min():.2f}..{vals.max():.2f} "
                  f"(mediana {np.median(vals):.2f})")
        else:
            print(f"  ⚠ {out_path}: sin píxeles válidos")

    for name, (a_name, b_name) in INDICES.items():
        if a_name not in band_idx or b_name not in band_idx:
            print(f"  ⚠ {name.upper()}: falta banda '{a_name}' o '{b_name}' — omitido")
            continue
        a = ds.GetRasterBand(band_idx[a_name]).ReadAsArray().astype(np.float32)
        b = ds.GetRasterBand(band_idx[b_name]).ReadAsArray().astype(np.float32)
        # Reflectancias (camera+sun) rondan 0-0.15 en este sensor — un
        # denominador (a+b) apenas distinto de 0 (ruido de calibración, no
        # "!= 0" exacto) ya dispara valores absurdos (verificado: sin este
        # piso, NDVI salía hasta ±14000 en <0.01% de los píxeles). 1e-4 es
        # dos órdenes de magnitud menor que el p1 real de cualquier banda.
        valid = np.isfinite(a) & np.isfinite(b) & (np.abs(a + b) > 1e-4)
        if footprint_valid is not None:
            valid &= footprint_valid
        with np.errstate(invalid="ignore", divide="ignore"):
            idx_arr = np.where(valid, (a - b) / (a + b), np.nan)
        write_index(name, idx_arr)

    if "nir" in band_idx and "red" in band_idx:
        nir = ds.GetRasterBand(band_idx["nir"]).ReadAsArray().astype(np.float32)
        red = ds.GetRasterBand(band_idx["red"]).ReadAsArray().astype(np.float32)
        valid = np.isfinite(nir) & np.isfinite(red) & (np.abs(nir + red) > 1e-4)
        if footprint_valid is not None:
            valid &= footprint_valid
        msavi2_arr = np.where(valid, _msavi2(nir, red), np.nan)
        write_index("msavi2", msavi2_arr)
    else:
        print("  ⚠ MSAVI2: falta banda 'nir' o 'red' — omitido")

    ds = None
    print(f"\n✅ Índices en {OUT_DIR}/")


if __name__ == "__main__":
    main()
