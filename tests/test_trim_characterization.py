"""Caracterización de los cuatro trim_*: RED DE SEGURIDAD DEL REFACTOR.

`trim_rgb`, `trim_multispectral`, `trim_thermal_native` y `trim_dsm` repiten el
mismo bloque de ~25 líneas (mapeo a grilla gruesa + pisos adaptativos +
fill_holes + vuelta a grilla fina). Deduplicarlo es la deuda #4 del plan de
mejora, y es exactamente el tipo de cambio que puede alterar el resultado sin
que nada falle.

Estos tests fijan el COMPORTAMIENTO OBSERVABLE de las cuatro funciones sobre
una misión sintética: cuántos píxeles sobreviven y con qué forma exacta
(hash de la máscara). Si el refactor cambia un solo píxel, fallan.

No pretenden que los números sean "correctos" en sentido absoluto — son lo que
el código hace HOY, que es justo lo que un refactor debe preservar.
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
    """Misión sintética completa con los cuatro productos y sus tres
    reconstruction.json, con las constantes del módulo apuntadas ahí."""
    d = tmp_path
    gt, W, H = extent_para()
    rng = np.random.default_rng(42)

    # Alpha de ODM: un disco central (lo "bien reconstruido") con un fleco
    # irregular alrededor, que es lo que los pisos de solape deben recortar.
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
    # y cámaras a Z=50, _rgb_camera_overlap proyecta los rayos HACIA ARRIBA y
    # las huellas caen fuera del raster — el solape da 0 y los pisos dejan de
    # recortar sin que nada falle.
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

    # Cámaras oblicuas en el borde del vuelo (primera y última fila) -> el piso
    # NADIR tiene qué recortar, que es su razón de ser.
    oblicuas = set(range(10)) | set(range(90, 100))
    recon = reconstruccion(str(d / "opensfm" / "reconstruction.json"),
                           oblicuas=oblicuas)

    monkeypatch.setattr(T, "RGB_ODM_SRC", rgb_src)
    monkeypatch.setattr(T, "RGB_PATH", str(d / "out_rgb.tif"))
    monkeypatch.setattr(T, "DSM_PATH", dsm_p)
    monkeypatch.setattr(T, "THNAT_ODM_SRC", th_src)
    monkeypatch.setattr(T, "TH_PATH", str(d / "out_th.tif"))
    monkeypatch.setattr(T, "MS_ODM_SRC", ms_src)
    monkeypatch.setattr(T, "MS_PATH", str(d / "out_ms.tif"))
    for c in ("RGB_RECON", "MS_RECON", "THNAT_RECON"):
        monkeypatch.setattr(T, c, recon)
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

    def test_los_pisos_de_solape_realmente_recortan(self, mision, capsys):
        """GUARDA DE LA GUARDA. Con un vuelo sintético de huella demasiado
        grande, las cámaras cubren el raster entero por igual, los pisos no
        descartan ni un píxel y el golden de arriba pasa a medir solo a
        _reliability_crop — dejando sin proteger justo el bloque que se
        deduplica. Pasó de verdad al escribir estos tests. Este test falla si
        el vuelo sintético se vuelve a desafinar."""
        T.trim_rgb()
        T.trim_thermal_native()
        T.trim_multispectral()
        salida = capsys.readouterr().out
        for etiqueta in ("piso de solape RGB", "piso de solape térmico", "piso de solape MS"):
            linea = next(l for l in salida.splitlines() if etiqueta in l)
            quitados = int(linea.split("−")[1].split()[0].replace(",", ""))
            assert quitados > 0, f"«{etiqueta}» no descartó nada: {linea}"
        # RGB no comparte camino con los otros tres: su piso nadir es DURO, no
        # adaptativo (señal ya validada con datos reales — ver el comentario de
        # REL_MIN_NADIR_RGB). Sobre esta misión sintética los cuatro dan el
        # mismo recorte, así que sin esto un refactor podría pasarlo a la vía
        # adaptativa y el golden no lo notaría.
        assert "piso NADIR (≥" in salida, \
            "el piso nadir duro de RGB tiene que seguir reportándose aparte"

    def test_el_recorte_solo_quita_area_nunca_agrega(self, mision):
        """Invariante estructural, independiente de los números concretos:
        ninguno de los cuatro puede marcar como válido un píxel que no lo era."""
        ds = gdal.Open(T.RGB_ODM_SRC)
        alpha_orig = ds.GetRasterBand(4).ReadAsArray() == 255
        ds = None
        T.trim_rgb()
        assert not (_mask_de(T.RGB_PATH, 4) & ~alpha_orig).any()

    def test_sin_reconstruction_json_no_falla_y_avisa(self, mision, capsys):
        """Degradación explícita: sin poses no hay piso de solape, pero el
        recorte de confiabilidad tiene que correr igual."""
        for c in ("RGB_RECON", "MS_RECON", "THNAT_RECON"):
            setattr(T, c, str(mision / "no_existe.json"))
        T.trim_rgb()
        assert "sin piso de solape" in capsys.readouterr().out
        assert os.path.isfile(T.RGB_PATH)

    def test_el_solape_se_alinea_con_la_crs_del_raster(self, mision):
        """Regresión del bug P0 #3: con el EPSG fijo, un raster fuera de la
        zona 18N daba solape 0 en todas partes sin lanzar error."""
        ds = gdal.Open(T.RGB_ODM_SRC)
        gt, proj = ds.GetGeoTransform(), ds.GetProjection()
        W, H = ds.RasterXSize, ds.RasterYSize
        ds = None
        ov, _, _, _ = T._rgb_camera_overlap(gt, W, H, recon_path=T.RGB_RECON,
                                            proj_wkt=proj)
        assert ov is not None and ov.max() > 0, \
            "las huellas de cámara tienen que caer sobre el raster"
