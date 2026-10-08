"""Caracterización de los cuatro trim_*: RED DE SEGURIDAD DEL REFACTOR, y
prueba del comportamiento que reemplazó al recorte por solape (reportado en
vivo: el mosaico térmico se estaba recortando mucho más de lo que la
cobertura real del vuelo justificaba).

Estos tests fijan el COMPORTAMIENTO OBSERVABLE de las cuatro funciones sobre
una misión sintética: cuántos píxeles sobreviven y con qué forma exacta
(hash de la máscara), más un par de invariantes estructurales del casco
convexo (recorta MENOS que el piso de solape viejo, nunca agrega área que
el alpha de ODM no tenía). Si un refactor cambia un solo píxel del golden,
falla — no pretende que el número sea "correcto" en sentido absoluto, es lo
que el código hace HOY, que es justo lo que un refactor debe preservar.
"""
import hashlib
import json
import os

import numpy as np
import pytest
from osgeo import gdal

import trim_low_overlap_edges as T
from synthetic import escribir_raster, extent_para, reconstruccion

gdal.UseExceptions()

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden",
                      "trim_masks.json")


def _huella(mask):
    """(nº de píxeles, hash de la forma) — sensible a un solo píxel movido."""
    b = np.ascontiguousarray(mask.astype(np.uint8))
    return {"n": int(mask.sum()), "sha1": hashlib.sha1(b.tobytes()).hexdigest()[:16]}


@pytest.fixture
def mision(tmp_path, monkeypatch):
    """Misión sintética completa con los cuatro productos y su
    reconstruction.json compartido, con las constantes del módulo apuntadas
    ahí."""
    d = tmp_path
    gt, W, H = extent_para()
    rng = np.random.default_rng(42)

    # Alpha de ODM: un disco central (lo "bien reconstruido") con un fleco
    # irregular alrededor — antes era lo que los pisos de solape recortaban;
    # ahora sirve para comprobar que el casco convexo NO lo recorta tan
    # agresivo (alcanza con que una sola foto haya cubierto la celda).
    yy, xx = np.mgrid[0:H, 0:W]
    r = np.sqrt((yy - H / 2) ** 2 + (xx - W / 2) ** 2)
    cuerpo = r < min(W, H) * 0.42
    fleco = (r < min(W, H) * 0.48) & (rng.random((H, W)) < 0.7)
    alpha_bool = cuerpo | fleco

    rgb = np.zeros((4, H, W), np.uint8)
    for b in range(3):
        rgb[b] = (rng.random((H, W)) * 200 + 30).astype(np.uint8)
    rgb[3] = np.where(alpha_bool, 255, 0)
    rgb_src = escribir_raster(str(d / "rgb_odm.tif"), rgb, gt,
                              dtype=gdal.GDT_Byte, alpha_last=True)

    # Cotas BAJAS a propósito: las poses de OpenSfM son topocéntricas con
    # origen en reference_lla, así que la Z de las cámaras (50 m) y la del DSM
    # tienen que estar en el mismo marco. Con un DSM en cotas absolutas (~1900)
    # y cámaras a Z=50, las huellas se proyectarían con una Z de referencia
    # totalmente distinta a la de las cámaras.
    dsm = np.where(alpha_bool, 2 + rng.random((H, W)) * 3, np.nan).astype(np.float32)
    dsm_p = escribir_raster(str(d / "dsm.tif"), dsm, gt, nodata=float("nan"))

    th = np.stack([np.where(alpha_bool, 25 + rng.random((H, W)) * 15, np.nan),
                   np.where(alpha_bool, 255.0, 0.0)]).astype(np.float32)
    th_src = escribir_raster(str(d / "th_odm.tif"), th, gt, alpha_last=True,
                             nodata=float("nan"))

    ms = np.concatenate([
        np.stack([np.where(alpha_bool, 0.1 + rng.random((H, W)) * 0.6, np.nan)
                  for _ in range(4)]),
        np.where(alpha_bool, 255.0, 0.0)[np.newaxis, ...]]).astype(np.float32)
    ms_src = escribir_raster(str(d / "ms_odm.tif"), ms, gt, alpha_last=True,
                             band_names=["Green", "Red", "RedEdge", "NIR", "alpha"])

    recon = reconstruccion(str(d / "opensfm" / "reconstruction.json"))

    monkeypatch.setattr(T, "RGB_ODM_SRC", rgb_src)
    monkeypatch.setattr(T, "RGB_PATH", str(d / "out_rgb.tif"))
    monkeypatch.setattr(T, "DSM_PATH", dsm_p)
    monkeypatch.setattr(T, "THNAT_ODM_SRC", th_src)
    monkeypatch.setattr(T, "TH_PATH", str(d / "out_th.tif"))
    monkeypatch.setattr(T, "MS_ODM_SRC", ms_src)
    monkeypatch.setattr(T, "MS_PATH", str(d / "out_ms.tif"))
    for c in ("RGB_RECON", "MS_RECON", "THNAT_RECON"):
        monkeypatch.setattr(T, c, recon)
    # HULL_*_PATH por defecto son relativos ("outputs/hull_rgb.geojson") —
    # sandboxeados igual que el resto, si no _footprint_hull_mask crea un
    # "outputs/" de verdad en el cwd de donde corra pytest.
    monkeypatch.setattr(T, "HULL_RGB_PATH", str(d / "hull_rgb.geojson"))
    monkeypatch.setattr(T, "HULL_THERMAL_PATH", str(d / "hull_thermal.geojson"))
    monkeypatch.setattr(T, "HULL_MS_PATH", str(d / "hull_multispectral.geojson"))
    return d


def _mask_de(path, banda_alpha):
    ds = gdal.Open(path)
    a = ds.GetRasterBand(banda_alpha).ReadAsArray()
    ds = None
    return a > 0 if not np.issubdtype(a.dtype, np.floating) else np.isfinite(a) & (a > 0)


def _mask_finita(path):
    ds = gdal.Open(path)
    a = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return np.isfinite(a)


def _correr_todo(d):
    """Los cuatro trim_*, en el orden real del Makefile, y sus máscaras."""
    T.trim_rgb()
    T.trim_dsm()
    T.trim_thermal_native()
    T.trim_multispectral()
    return {
        "rgb": _huella(_mask_de(str(d / "out_rgb.tif"), 4)),
        "dsm": _huella(_mask_finita(str(d / "dsm.tif"))),
        "thermal": _huella(_mask_finita(str(d / "out_th.tif"))),
        "multispectral": _huella(_mask_de(str(d / "out_ms.tif"), 5)),
    }


class TestCaracterizacion:
    def test_las_cuatro_mascaras_no_cambian(self, mision):
        """El test que blinda el refactor. Si no existe el golden, lo escribe
        (primera corrida); si existe, exige coincidencia exacta."""
        got = _correr_todo(mision)
        if not os.path.isfile(GOLDEN):
            os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
            with open(GOLDEN, "w") as f:
                json.dump(got, f, indent=2, sort_keys=True)
            pytest.skip(f"golden creado en {GOLDEN} — volvé a correr para comparar")
        with open(GOLDEN) as f:
            esperado = json.load(f)
        assert got == esperado, (
            "el recorte cambió respecto del golden.\n"
            f"esperado: {json.dumps(esperado, indent=2, sort_keys=True)}\n"
            f"obtenido: {json.dumps(got, indent=2, sort_keys=True)}\n"
            "Si el cambio es intencional, borrá tests/golden/trim_masks.json y regeneralo.")

    def test_el_recorte_solo_quita_area_nunca_agrega(self, mision):
        """Invariante estructural, independiente de los números concretos:
        ninguno de los cuatro puede marcar como válido un píxel que no lo era
        en el alpha original de ODM."""
        ds = gdal.Open(T.RGB_ODM_SRC)
        alpha_orig = ds.GetRasterBand(4).ReadAsArray() == 255
        ds = None
        T.trim_rgb()
        assert not (_mask_de(T.RGB_PATH, 4) & ~alpha_orig).any()

    def test_el_casco_convexo_conserva_la_mayor_parte_del_alpha(self, mision):
        """La razón de ser del cambio: el casco convexo exige UNA sola foto
        cubriendo la celda (no varias superpuestas como el piso de solape
        viejo), así que sobre esta misión sintética (vuelo denso, huellas que
        cubren de sobra el disco+fleco del alpha) el recorte final tiene que
        conservar la mayoría del alpha original — el disco+fleco de la
        fixture es un patrón aleatorio independiente de las huellas reales
        de cámara, así que no coincide pixel a pixel con el casco, pero
        tiene que quedar bien por encima del ~40-60% típico que dejaba el
        piso de solape + recorte de confiabilidad de antes."""
        ds = gdal.Open(T.RGB_ODM_SRC)
        alpha_orig = ds.GetRasterBand(4).ReadAsArray() == 255
        ds = None
        T.trim_rgb()
        final = _mask_de(T.RGB_PATH, 4)
        assert final.sum() / alpha_orig.sum() > 0.75, (
            "el casco convexo debería conservar bastante más del alpha "
            "original en un vuelo en grilla denso — si conserva mucho "
            "menos, algo volvió a exigir solape entre fotos en vez de una "
            "sola huella")

    def test_area_lejos_del_vuelo_queda_afuera(self, mision):
        """El casco convexo SÍ tiene un límite real: un alpha "bien
        reconstruido" muy por fuera de donde volaron las cámaras (imposible
        en una misión real, pero deliberado acá para poner a prueba el
        límite) tiene que quedar recortado igual — el casco no es "conservar
        todo"."""
        ds = gdal.Open(T.RGB_ODM_SRC)
        gt = ds.GetGeoTransform()
        full = ds.ReadAsArray()
        ds = None
        # Una franja de alpha=255 pegada al borde superior del raster, a
        # varios cientos de metros del cuerpo del vuelo (extent_para ya deja
        # 50 m de margen — esto va bien por fuera de eso).
        full[3, :5, :] = 255
        rgb_lejos = str(mision / "rgb_odm_lejos.tif")
        escribir_raster(rgb_lejos, full, gt, dtype=gdal.GDT_Byte, alpha_last=True)
        T.RGB_ODM_SRC = rgb_lejos
        T.trim_rgb()
        final = _mask_de(T.RGB_PATH, 4)
        assert not final[:5, :].any(), \
            "una franja lejos de toda huella de cámara no debería sobrevivir al casco convexo"

    def test_franja_de_una_sola_foto_sin_solape_se_conserva(self, mision):
        """El bug real que motivó el cambio: una franja cubierta por UNA sola
        foto (sin ninguna otra superpuesta) tenía que sobrevivir al casco
        convexo — con el piso de solape viejo, esa franja quedaba SIEMPRE
        afuera (exigía varias fotos por celda, ver REL_MIN_OVERLAP_RGB=15).
        Vuelo de una sola fila (todas las cámaras alineadas): en los bordes
        angostos del corredor fotografiado el solape entre pasadas vecinas es
        cero por construcción — es exactamente la franja de una sola foto
        que había que dejar de recortar."""
        gt, W, H = extent_para(n_lado=1)
        recon_fila = reconstruccion(str(mision / "opensfm_fila" / "reconstruction.json"),
                                    n_lado=1)
        # Alpha=255 en TODO el raster: lo único que decide qué sobrevive acá
        # es el casco convexo, no el alpha.
        rgb = np.zeros((4, H, W), np.uint8)
        rgb[:3] = 100
        rgb[3] = 255
        rgb_src = escribir_raster(str(mision / "rgb_fila.tif"), rgb, gt,
                                  dtype=gdal.GDT_Byte, alpha_last=True)
        T.RGB_ODM_SRC = rgb_src
        T.RGB_PATH = str(mision / "out_fila.tif")
        T.RGB_RECON = recon_fila
        T.trim_rgb()
        final = _mask_de(T.RGB_PATH, 4)
        assert final.sum() > 0, \
            "la única foto de este vuelo sintético tiene que dejar ALGO adentro del casco"

    def test_sin_reconstruction_json_no_falla_y_avisa(self, mision, capsys):
        """Degradación explícita: sin poses no hay huellas que calcular, pero
        el recorte tiene que correr igual (se queda con el alpha crudo de
        ODM, sin el recorte extra por casco convexo)."""
        for c in ("RGB_RECON", "MS_RECON", "THNAT_RECON"):
            setattr(T, c, str(mision / "no_existe.json"))
        T.trim_rgb()
        assert "sin recorte por casco convexo" in capsys.readouterr().out
        assert os.path.isfile(T.RGB_PATH)

    def test_el_casco_se_alinea_con_la_crs_del_raster(self, mision):
        """Regresión del bug P0 #3 original (piso de solape con EPSG fijo):
        con una zona UTM fija, un raster fuera de esa zona ponía las huellas
        a cientos de kilómetros de distancia sin lanzar ningún error. Acá el
        equivalente es que la máscara del casco NO puede terminar vacía."""
        ds = gdal.Open(T.RGB_ODM_SRC)
        gt, proj = ds.GetGeoTransform(), ds.GetProjection()
        W, H = ds.RasterXSize, ds.RasterYSize
        ds = None
        mask = T._footprint_hull_mask(T.RGB_RECON, gt, W, H, proj)
        assert mask is not None and mask.any(), \
            "las huellas de cámara tienen que caer sobre el raster"
