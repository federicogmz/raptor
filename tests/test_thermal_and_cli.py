"""Resolución del sensor térmico + validación de argumentos del lanzador.

Dos superficies chicas pero de borde: la primera decide cómo se ordena el
arreglo radiométrico (equivocarse produce un raster corrido, no un error), y
la segunda es la puerta de entrada de CI, donde un argumento mal validado se
traduce en un contenedor lanzado al pedo o en un cuelgue.
"""
import os
import subprocess
import sys

import numpy as np
import pytest
from osgeo import gdal

from convert_thermal_tiff import _jpeg_size, thermal_size

gdal.UseExceptions()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAPTOR = os.path.join(REPO, "raptor")


def _jpeg(path, w, h):
    ds = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
    ds.GetRasterBand(1).WriteArray(np.zeros((h, w), np.uint8))
    gdal.GetDriverByName("JPEG").CreateCopy(path, ds)
    ds = None
    return path


class TestResolucionTermica:
    @pytest.mark.parametrize("w,h", [
        (640, 512),     # H20T / M3T / M30T
        (1280, 1024),   # sensores más nuevos
        (336, 256),     # sensores chicos
    ])
    def test_infiere_la_resolucion_de_la_foto(self, tmp_path, w, h):
        """En los R-JPEG de DJI el stream JPEG está a la resolución del sensor
        térmico, así que fijar 640x512 en el código obligaba a tocarlo por cada
        cámara nueva."""
        p = _jpeg(str(tmp_path / f"t_{w}x{h}.jpg"), w, h)
        assert _jpeg_size(p) == (w, h)
        assert thermal_size(p, w * h) == (w, h)

    def test_rechaza_un_conteo_de_floats_que_no_cuadra(self, tmp_path):
        """Sin esta comprobación, el reshape produce un raster corrido o
        revienta con un error incomprensible."""
        p = _jpeg(str(tmp_path / "t.jpg"), 640, 512)
        assert thermal_size(p, 640 * 512 + 7) is None
        assert thermal_size(p, 1280 * 1024) is None, "floats de otro sensor"
        assert thermal_size(p, 0) is None


class TestConvertOneToleraTimeout:
    """Bug real, reportado en vivo: un solo dji_irp colgado (SDK externo,
    30s de timeout) tumbaba TODA la conversión con un traceback — la
    excepción se escapaba sin atajar hasta fut.result() en main(), en vez
    de contarse como una falla más (que es lo que ya hace el resto de
    convert_one() para cualquier OTRO tipo de error de dji_irp: SDK
    devuelve código != 0, no produce salida, tamaño no coincide)."""

    def test_timeout_devuelve_false_en_vez_de_propagar(self, tmp_path, monkeypatch):
        import convert_thermal_tiff as C
        src = _jpeg(str(tmp_path / "t.jpg"), 640, 512)
        dst = str(tmp_path / "t.tif")

        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="dji_irp", timeout=30)
        monkeypatch.setattr(C.subprocess, "run", _timeout)

        assert C.convert_one(src, dst) is False


class TestCLI:
    """Solo los caminos que fallan ANTES de invocar docker — el resto necesita
    un demonio Docker que no hay dentro del contenedor de tests."""

    def _run(self, *args):
        return subprocess.run(["bash", RAPTOR, *args], capture_output=True,
                              text=True, timeout=30)

    def test_sin_entradas_falla(self):
        r = self._run("run", "--export", "/tmp/x")
        assert r.returncode == 2
        assert "--input" in r.stderr

    def test_modo_invalido_falla_nombrando_los_validos(self):
        r = self._run("run", "--input", "/tmp", "--mode", "xyz")
        assert r.returncode == 2
        assert "rgb+thermal" in r.stderr

    def test_input_inexistente_falla(self):
        r = self._run("run", "--input", "/no/existe/seguro")
        assert r.returncode == 2
        assert "no existe" in r.stderr

    def test_opcion_desconocida_falla(self):
        r = self._run("run", "--input", "/tmp", "--parametro-inventado")
        assert r.returncode == 2
        assert "desconocida" in r.stderr

    def test_entrega_no_escribible_falla_con_mensaje_propio(self, tmp_path):
        r = self._run("run", "--input", str(tmp_path), "--export", "/proc/imposible")
        assert r.returncode == 2
        assert "carpeta de entrega" in r.stderr

    def test_crea_la_carpeta_de_entrega_en_el_host(self, tmp_path):
        """Se crea desde el host a propósito: si la crea el contenedor queda
        de root y el usuario no puede tocarla."""
        entrega = tmp_path / "entregas" / "la_clara"
        # Falla al llegar a docker (no hay demonio acá), pero la carpeta ya
        # tiene que estar creada para ese momento.
        self._run("run", "--input", str(tmp_path), "--export", str(entrega))
        assert entrega.is_dir()
        assert os.access(entrega, os.W_OK)

    def test_help_no_falla(self):
        for args in (["--help"], ["run", "--help"], ["webapp", "--help"]):
            r = self._run(*args)
            assert r.returncode == 0, f"{args}: {r.stderr}"
            assert r.stdout.strip()

    def test_comando_desconocido_falla(self):
        r = self._run("inventado")
        assert r.returncode == 2 and "desconocido" in r.stderr
