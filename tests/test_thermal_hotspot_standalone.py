"""Hotspot térmico independiente del multiespectral.

Antes solo se generaba como subproducto de compute_severity_classes.py, que
exigía detect_area_afectada.py, que a su vez EXIGÍA multiespectral (su señal
primaria era NDVI). Una misión RGB+térmico sin M3M se quedaba sin la capa de
hotspot sin necesidad real: nada en su cálculo depende de NDVI ni de un
polígono de área afectada. Ambos módulos (detect_area_afectada.py,
compute_severity_classes.py) fueron eliminados; compute_thermal_hotspot.py
es ahora la ÚNICA fuente de clasificación de hotspot, para toda misión con
térmico, con o sin multiespectral.
"""
import os

import numpy as np
import pytest
from osgeo import gdal

import compute_thermal_hotspot as H

gdal.UseExceptions()

GT = (466000.0, 0.2, 0.0, 708900.0, 0.0, -0.2)


@pytest.fixture
def mision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("outputs")
    return tmp_path


def _thermal(path, temps):
    n = len(temps)
    ds = gdal.GetDriverByName("GTiff").Create(path, n, 1, 1, gdal.GDT_Float32)
    ds.SetGeoTransform(GT)
    ds.GetRasterBand(1).WriteArray(np.array([temps], dtype=np.float32))
    ds.GetRasterBand(1).SetNoDataValue(float("nan"))
    ds = None


class TestHotspotSinMultiespectral:
    def test_clasifica_sin_ms_ni_poligono(self, mision, monkeypatch):
        """No debe pedir MS_PATH, POLY_PATH ni el cache de
        detect_area_afectada.py — ninguno de esos archivos existe acá."""
        monkeypatch.setattr(H, "TH_PATH", str(mision / "outputs" / "thermal_orthomosaic.tif"))
        monkeypatch.setattr(H, "OUT_PATH", str(mision / "outputs" / "termico_hotspot_class.tif"))
        _thermal(H.TH_PATH, [20, 45, 65, 95])  # normal, elevado, caliente, foco activo
        assert H.main() is None  # no sys.exit
        ds = gdal.Open(H.OUT_PATH)
        vals = ds.GetRasterBand(1).ReadAsArray()[0]
        ds = None
        assert vals.tolist() == [1, 2, 3, 4]

    def test_cubre_toda_la_cobertura_sin_recortar(self, mision, monkeypatch):
        """Sin polígono de área afectada (no hay multiespectral), el hotspot
        se clasifica en TODO el territorio con dato válido, no solo dentro de
        un perímetro — que acá no existe."""
        monkeypatch.setattr(H, "TH_PATH", str(mision / "outputs" / "thermal_orthomosaic.tif"))
        monkeypatch.setattr(H, "OUT_PATH", str(mision / "outputs" / "termico_hotspot_class.tif"))
        _thermal(H.TH_PATH, [95, 95, 95, 95])
        H.main()
        ds = gdal.Open(H.OUT_PATH)
        vals = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        assert (vals == 4).all(), "sin recorte, focos activos deben aparecer en toda la extensión"

    def test_falla_con_mensaje_claro_sin_termico(self, mision, monkeypatch, capsys):
        monkeypatch.setattr(H, "TH_PATH", str(mision / "outputs" / "no_existe.tif"))
        with pytest.raises(SystemExit) as exc:
            H.main()
        assert exc.value.code == 1
        assert "no encontrado" in capsys.readouterr().out

    def test_cortes_de_clasificacion(self):
        """Los cortes 40/60/88°C viven directamente en este módulo — ya no
        hay un segundo módulo (compute_severity_classes.py, eliminado) del
        que importarlos para comparar por identidad."""
        assert H.TERM_BREAKS == [40.0, 60.0, 88.0]


class TestEntrypointSiempreLoLlama:
    """El único código de hotspot térmico ahora es este script — corre
    incondicionalmente siempre que haya térmico, sin importar si hay
    multiespectral (el segundo camino recortado al área afectada ya no
    existe)."""

    def test_se_invoca_incondicionalmente(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docker", "entrypoint.sh"), encoding="utf-8").read()
        # Se ancla al comentario de "Calidad del LEVANTAMIENTO", único en el
        # archivo — es el bloque real de flight-quality/hotspot/situation-
        # summary (hoy corre en un subshell de fondo, parte del análisis
        # cruzado en paralelo). El `fi` de cierre buscado es el que arranca
        # en columna 0.
        ini = src.index('if [[ "$DO_THERMAL" -eq 1 ]]; then\n  # Calidad del LEVANTAMIENTO')
        bloque = src[ini:src.index("\nfi\n", ini) + 4]
        assert "compute-thermal-hotspot" in bloque
        assert "make situation-summary" in bloque
        # El viejo gate interno "solo si no hay multiespectral" ya no existe
        # — ambas líneas corren siempre.
        assert 'if [[ "$DO_MS" -eq 0 ]]; then' not in bloque
