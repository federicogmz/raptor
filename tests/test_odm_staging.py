"""scripts/odm_staging.py — poblar images/ de un proyecto ODM y armar geo.txt.

Lo importante que se verifica acá es que se ENLAZA en vez de copiar. Hasta
este cambio cada foto se escribía dos veces (fuente → data/ → processing/):
en barbosa-picodegallo, ~23 GB duplicados y 20 min de etapa de preparación.
ODM solo lee processing/<proyecto>/images/, así que el hardlink alcanza.

El contraejemplo importa igual: `Makefile::prepare-rgb` corre exiftool con
-overwrite_original sobre images/, así que ahí un hardlink modificaría
también el original de data/ — por eso RGB usa `cp --reflink=auto` y no este
módulo.
"""
import json
import os
import subprocess
import sys

import pytest

from odm_staging import enlazar_o_copiar, escribir_geo_txt, poblar_images


def _tocar(p, contenido=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(contenido)
    return p


class TestPoblarImages:
    def test_enlaza_en_vez_de_copiar(self, tmp_path):
        src = tmp_path / "data"
        dst = tmp_path / "images"
        for n in ("a_MS_G.TIF", "a_MS_R.TIF"):
            _tocar(src / n)

        nombres = poblar_images(str(src), str(dst), ("_MS_G.TIF", "_MS_R.TIF"), 2)

        assert nombres == ["a_MS_G.TIF", "a_MS_R.TIF"]
        for n in nombres:
            # Mismo inodo = un solo archivo en disco, no dos.
            assert (dst / n).stat().st_ino == (src / n).stat().st_ino, n

    def test_ignora_lo_que_no_coincide_con_los_sufijos(self, tmp_path):
        src, dst = tmp_path / "data", tmp_path / "images"
        _tocar(src / "a_MS_G.TIF")
        _tocar(src / "a_D.JPG")
        _tocar(src / "notas.txt")

        nombres = poblar_images(str(src), str(dst), ("_MS_G.TIF",), 2)
        assert nombres == ["a_MS_G.TIF"]
        assert sorted(os.listdir(dst)) == ["a_MS_G.TIF"]

    def test_sufijos_sin_distinguir_mayusculas(self, tmp_path):
        src, dst = tmp_path / "data", tmp_path / "images"
        _tocar(src / "a_ms_nir.tif")
        assert poblar_images(str(src), str(dst), ("_MS_NIR.TIF",), 2) == ["a_ms_nir.tif"]

    def test_limpia_la_corrida_anterior(self, tmp_path):
        """Sin esto, una misión nueva sobre el mismo contenedor mezcla dos
        vuelos — el bug que ya costó una corrida entera."""
        src, dst = tmp_path / "data", tmp_path / "images"
        _tocar(src / "nueva_MS_G.TIF")
        _tocar(dst / "vieja_MS_G.TIF")

        poblar_images(str(src), str(dst), ("_MS_G.TIF",), 2)
        assert sorted(os.listdir(dst)) == ["nueva_MS_G.TIF"]

    def test_sin_archivos_devuelve_lista_vacia(self, tmp_path):
        src, dst = tmp_path / "data", tmp_path / "images"
        src.mkdir()
        assert poblar_images(str(src), str(dst), ("_MS_G.TIF",), 2) == []

    def test_cae_a_copia_si_el_hardlink_no_se_puede(self, tmp_path, monkeypatch):
        """data/ y processing/ pueden ser volúmenes distintos: ahí os.link
        falla con EXDEV y hay que copiar igual, sin romper la corrida."""
        origen, destino = tmp_path / "o.TIF", tmp_path / "d.TIF"
        _tocar(origen, b"contenido")

        def _falla(*_a, **_k):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(os, "link", _falla)
        enlazar_o_copiar(str(origen), str(destino))

        assert destino.read_bytes() == b"contenido"
        assert destino.stat().st_ino != origen.stat().st_ino


class TestGeoTxt:
    @pytest.fixture
    def exiftool_falso(self, tmp_path, monkeypatch):
        """exiftool de mentira que devuelve el JSON que devolvería el real."""
        registros = {}

        def _run(cmd, **kwargs):
            rutas = kwargs["input"].strip().splitlines()
            salida = [dict(registros.get(os.path.basename(p), {}), SourceFile=p)
                      for p in rutas]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(salida), "")

        monkeypatch.setattr(subprocess, "run", _run)
        return registros

    def test_usa_la_precision_rtk_cuando_esta(self, tmp_path, exiftool_falso):
        exiftool_falso["a.TIF"] = {"GPSLatitude": 6.4, "GPSLongitude": -75.3,
                                    "GPSAltitude": 1500, "RtkStdLon": 0.01,
                                    "RtkStdLat": 0.02, "RtkStdHgt": 0.03}
        geo = tmp_path / "geo.txt"
        con_gps, sin_gps = escribir_geo_txt(["/x/a.TIF"], str(geo), 1)

        assert (con_gps, sin_gps) == (1, 0)
        lineas = geo.read_text().strip().splitlines()
        assert lineas[0] == "EPSG:4326"
        campos = lineas[1].split("\t")
        assert campos[0] == "a.TIF"
        # Margen ×2, el mismo que ODM aplica al auto-detectar estos tags.
        assert float(campos[7]) == pytest.approx(0.04)
        assert float(campos[8]) == pytest.approx(0.06)

    def test_sin_rtk_cae_al_dop_conservador(self, tmp_path, exiftool_falso):
        exiftool_falso["a.TIF"] = {"GPSLatitude": 6.4, "GPSLongitude": -75.3}
        geo = tmp_path / "geo.txt"
        escribir_geo_txt(["/x/a.TIF"], str(geo), 1)
        campos = geo.read_text().strip().splitlines()[1].split("\t")
        assert (float(campos[7]), float(campos[8])) == (3.0, 5.0)

    def test_omite_los_que_no_tienen_gps(self, tmp_path, exiftool_falso):
        exiftool_falso["con.TIF"] = {"GPSLatitude": 6.4, "GPSLongitude": -75.3}
        exiftool_falso["sin.TIF"] = {}
        geo = tmp_path / "geo.txt"
        con_gps, sin_gps = escribir_geo_txt(["/x/con.TIF", "/x/sin.TIF"], str(geo), 1)

        assert (con_gps, sin_gps) == (1, 1)
        assert "sin.TIF" not in geo.read_text()

    def test_liga_por_sourcefile_no_por_posicion(self, tmp_path, monkeypatch):
        """Emparejar la fila i de exiftool con el archivo i de la entrada da
        por sentado un orden que no está garantizado. Si alguna vez no se
        cumple, geo.txt asigna coordenadas al archivo equivocado — eso no
        falla, produce una reconstrucción mal georreferenciada."""
        def _run(cmd, **kwargs):
            rutas = kwargs["input"].strip().splitlines()
            # A propósito, al revés del orden de entrada.
            salida = [{"SourceFile": p, "GPSLatitude": 6.0 + i, "GPSLongitude": -75.0}
                      for i, p in reversed(list(enumerate(rutas)))]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(salida), "")

        monkeypatch.setattr(subprocess, "run", _run)
        geo = tmp_path / "geo.txt"
        escribir_geo_txt(["/x/primero.TIF", "/x/segundo.TIF"], str(geo), 1)

        filas = {l.split("\t")[0]: l.split("\t") for l in
                 geo.read_text().strip().splitlines()[1:]}
        assert float(filas["primero.TIF"][2]) == pytest.approx(6.0)
        assert float(filas["segundo.TIF"][2]) == pytest.approx(7.0)
