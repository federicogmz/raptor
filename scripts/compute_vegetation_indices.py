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
regeneración de vegetación en terreno de cobertura baja. Con cobertura de dosel alta MSAVI2
converge con NDVI (mismo valor, sin pérdida); diverge justo donde NDVI es
menos confiable, que es donde aporta.
"""

import os, sys
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

STRIP_ROWS = int(os.environ.get("RAPTOR_STRIP_ROWS", 1024))
MS_PATH = "outputs/multispectral_orthomosaic.tif"
OUT_DIR = "outputs/indices"

# nombre_lógico → substrings que identifican la banda en GetDescription()
# (RedEdge antes que Red: "red" es substring de "rededge")
BAND_ALIASES = {
    "nir": ["nir"],
    "rededge": ["rededge", "red edge", "red_edge"],
    "red": ["red"],
    "green": ["green"],
}

# índice → (banda_a, banda_b) calculado como (a-b)/(a+b)
INDICES = {
    "ndvi": ("nir", "red"),
    "gndvi": ("nir", "green"),
    "ndre": ("nir", "rededge"),
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

    has_alpha = False
    alpha_idx = None
    if ds.RasterCount >= max(band_idx.values(), default=0) + 1:
        cand_idx = ds.RasterCount
        if ds.GetRasterBand(cand_idx).GetColorInterpretation() == gdal.GCI_AlphaBand:
            has_alpha = True
            alpha_idx = cand_idx

    os.makedirs(OUT_DIR, exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")

    def compute_and_write_index(name, a_idx, b_idx, is_msavi2=False):
        out_path = os.path.join(OUT_DIR, f"{name}.tif")
        out = drv.Create(out_path, W, H, 1, gdal.GDT_Float32, ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
        out.SetGeoTransform(gt)
        out.SetProjection(proj)
        out_band = out.GetRasterBand(1)
        out_band.SetNoDataValue(float("nan"))

        band_a = ds.GetRasterBand(a_idx)
        band_b = ds.GetRasterBand(b_idx)
        alpha_band = ds.GetRasterBand(alpha_idx) if has_alpha else None

        min_val = float("inf")
        max_val = float("-inf")
        sample_vals = []
        MAX_SAMPLES = 1_000_000
        total_valid = 0

        for yoff in range(0, H, STRIP_ROWS):
            num_rows = min(STRIP_ROWS, H - yoff)
            a = band_a.ReadAsArray(0, yoff, W, num_rows).astype(np.float32)
            b = band_b.ReadAsArray(0, yoff, W, num_rows).astype(np.float32)

            valid = np.isfinite(a) & np.isfinite(b) & (np.abs(a + b) > 1e-4)
            if alpha_band is not None:
                footprint_strip = alpha_band.ReadAsArray(0, yoff, W, num_rows) > 0
                valid &= footprint_strip

            with np.errstate(invalid="ignore", divide="ignore"):
                if is_msavi2:
                    idx_strip = np.where(valid, _msavi2(a, b), np.nan)
                else:
                    idx_strip = np.where(valid, (a - b) / (a + b), np.nan)

            out_band.WriteArray(idx_strip, 0, yoff)

            fin = idx_strip[np.isfinite(idx_strip)]
            if fin.size:
                c_min = float(np.min(fin))
                c_max = float(np.max(fin))
                if c_min < min_val:
                    min_val = c_min
                if c_max > max_val:
                    max_val = c_max
                total_valid += fin.size
                if len(sample_vals) < MAX_SAMPLES:
                    sample_vals.extend(fin[:: max(1, fin.size // 10000)].tolist())

        out_band = None
        out = None

        if total_valid > 0:
            med = float(np.median(sample_vals))
            print(f"  ✅ {out_path}: {name.upper()} {min_val:.2f}..{max_val:.2f} (mediana {med:.2f})")
        else:
            print(f"  ⚠ {out_path}: sin píxeles válidos")

    for name, (a_name, b_name) in INDICES.items():
        if a_name not in band_idx or b_name not in band_idx:
            print(f"  ⚠ {name.upper()}: falta banda '{a_name}' o '{b_name}' — omitido")
            continue
        compute_and_write_index(name, band_idx[a_name], band_idx[b_name], is_msavi2=False)

    if "nir" in band_idx and "red" in band_idx:
        compute_and_write_index("msavi2", band_idx["nir"], band_idx["red"], is_msavi2=True)
    else:
        print("  ⚠ MSAVI2: falta banda 'nir' o 'red' — omitido")

    ds = None
    print(f"\n✅ Índices en {OUT_DIR}/")


if __name__ == "__main__":
    main()
