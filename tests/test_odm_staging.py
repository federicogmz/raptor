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


@pytest.fixture
def exiftool_falso(monkeypatch):
    """exiftool de mentira que devuelve el JSON que devolvería el real."""
    registros = {}

    def _run(cmd, **kwargs):
        rutas = kwargs["input"].strip().splitlines()
        salida = [dict(registros.get(os.path.basename(p), {}), SourceFile=p) for p in rutas]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(salida), "")

    monkeypatch.setattr(subprocess, "run", _run)
    return registros


class TestGeoTxt:
    def test_usa_la_precision_rtk_cuando_esta(self, tmp_path, exiftool_falso):
        exiftool_falso["a.TIF"] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "GPSAltitude": 1500,
            "RtkStdLon": 0.01,
            "RtkStdLat": 0.02,
            "RtkStdHgt": 0.03,
        }
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
            salida = [
                {"SourceFile": p, "GPSLatitude": 6.0 + i, "GPSLongitude": -75.0}
                for i, p in reversed(list(enumerate(rutas)))
            ]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(salida), "")

        monkeypatch.setattr(subprocess, "run", _run)
        geo = tmp_path / "geo.txt"
        escribir_geo_txt(["/x/primero.TIF", "/x/segundo.TIF"], str(geo), 1)

        filas = {l.split("\t")[0]: l.split("\t") for l in geo.read_text().strip().splitlines()[1:]}
        assert float(filas["primero.TIF"][2]) == pytest.approx(6.0)
        assert float(filas["segundo.TIF"][2]) == pytest.approx(7.0)


class TestRtkFlagYMrk:
    def test_rtk_fix_prefiere_absolute_altitude_y_duplica_std(self, tmp_path, exiftool_falso):
        exiftool_falso["DJI_20260803120000_0001_D.JPG"] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "GPSAltitude": 2790.0,
            "AbsoluteAltitude": 2817.5,
            "RtkFlag": 50,
            "RtkStdLon": 0.012,
            "RtkStdLat": 0.008,
            "RtkStdHgt": 0.021,
        }
        geo = tmp_path / "geo.txt"
        escribir_geo_txt(["/x/DJI_20260803120000_0001_D.JPG"], str(geo), 1)
        campos = geo.read_text().strip().splitlines()[1].split("\t")
        assert float(campos[3]) == pytest.approx(2817.5)
        assert float(campos[7]) == pytest.approx(0.024)
        assert float(campos[8]) == pytest.approx(0.042)

    def test_rtk_float_aplica_piso_030_060(self, tmp_path, exiftool_falso):
        exiftool_falso["float_chico.JPG"] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 1800.0,
            "RtkFlag": 34,
            "RtkStdLon": 0.05,
            "RtkStdLat": 0.04,
            "RtkStdHgt": 0.10,
        }
        exiftool_falso["float_grande.JPG"] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 1800.0,
            "RtkFlag": 32,
            "RtkStdLon": 0.25,
            "RtkStdLat": 0.20,
            "RtkStdHgt": 0.45,
        }
        geo = tmp_path / "geo.txt"
        escribir_geo_txt(["/x/float_chico.JPG", "/x/float_grande.JPG"], str(geo), 1)
        filas = {l.split("\t")[0]: l.split("\t") for l in geo.read_text().strip().splitlines()[1:]}
        assert (float(filas["float_chico.JPG"][7]), float(filas["float_chico.JPG"][8])) == pytest.approx((0.30, 0.60))
        assert (float(filas["float_grande.JPG"][7]), float(filas["float_grande.JPG"][8])) == pytest.approx((0.50, 0.90))

    def test_bloque_mixto_downweightea_sin_rtk_y_unifica_altura(self, tmp_path, exiftool_falso):
        exiftool_falso["fix.JPG"] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "GPSAltitude": 2790.0,
            "AbsoluteAltitude": 2817.5,
            "RtkFlag": 50,
            "RtkStdLon": 0.01,
            "RtkStdLat": 0.01,
            "RtkStdHgt": 0.02,
        }
        exiftool_falso["single.JPG"] = {
            "GPSLatitude": 6.401,
            "GPSLongitude": -75.301,
            "GPSAltitude": 2790.0,
            "AbsoluteAltitude": 2818.0,
            "RtkFlag": 16,
            "RtkStdLon": 0.01,
            "RtkStdLat": 0.01,
            "RtkStdHgt": 0.02,
        }
        geo = tmp_path / "geo.txt"
        escribir_geo_txt(["/x/fix.JPG", "/x/single.JPG"], str(geo), 1)
        filas = {l.split("\t")[0]: l.split("\t") for l in geo.read_text().strip().splitlines()[1:]}
        assert float(filas["fix.JPG"][3]) == pytest.approx(2817.5)
        assert float(filas["single.JPG"][3]) == pytest.approx(2818.0)
        assert (float(filas["fix.JPG"][7]), float(filas["fix.JPG"][8])) == pytest.approx((0.02, 0.04))
        assert (float(filas["single.JPG"][7]), float(filas["single.JPG"][8])) == pytest.approx((10.0, 20.0))

    def test_cache_exif_recalcula_pesos_y_recupera_json_corrupto(self, tmp_path, monkeypatch, exiftool_falso):
        imgs = tmp_path / "imgs"
        imgs.mkdir()
        f1 = imgs / "DJI_20260803120000_0001_D.JPG"
        f2 = imgs / "DJI_20260803120005_0002_D.JPG"
        f1.write_bytes(b"foto1")
        f2.write_bytes(b"foto2")

        exiftool_falso[f1.name] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 2817.0,
            "RtkFlag": 16,
        }
        exiftool_falso[f2.name] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 2817.0,
            "RtkFlag": 50,
            "RtkStdLon": 0.01,
            "RtkStdLat": 0.01,
            "RtkStdHgt": 0.02,
        }
        geo = tmp_path / "geo.txt"
        escribir_geo_txt([str(f1), str(f2)], str(geo), 1)
        filas1 = {l.split("\t")[0]: l.split("\t") for l in geo.read_text().strip().splitlines()[1:]}
        assert (float(filas1[f1.name][7]), float(filas1[f1.name][8])) == pytest.approx((10.0, 20.0))

        def _prohibido(*_a, **_kw):
            raise AssertionError("no debe re-invocar exiftool con caché válida")

        monkeypatch.setattr(subprocess, "run", _prohibido)
        escribir_geo_txt([str(f1)], str(geo), 1)
        filas2 = {l.split("\t")[0]: l.split("\t") for l in geo.read_text().strip().splitlines()[1:]}
        assert (float(filas2[f1.name][7]), float(filas2[f1.name][8])) == pytest.approx((3.0, 5.0))

        # Si el JSON del caché está corrupto, debe recuperarse ejecutando exiftool.
        import json

        cache_path = tmp_path / "geo.txt.exif_cache.json"
        cache_path.write_text("{corrupto", encoding="utf-8")

        def _exiftool_f1(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps([{"SourceFile": str(f1), **exiftool_falso[f1.name]}]),
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", _exiftool_f1)
        escribir_geo_txt([str(f1)], str(geo), 1)
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert data["schema"] == 2
        assert f1.name in data["entries"]

    def test_mrk_ppk_actualiza_solo_primaria_con_secuencia_unica(self, tmp_path, exiftool_falso):
        from datetime import datetime, timezone

        cap_utc = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        gps_sec = cap_utc - 315964800.0 + 18.0
        week = int(gps_sec // 604800.0)
        tow = gps_sec - week * 604800.0

        mrk_dir = tmp_path / "mrk"
        mrk_dir.mkdir()
        (mrk_dir / "100_0001_Timestamp.MRK").write_text(
            f"1\t{tow:.6f}\t[{week}]\t0.0,N\t0.0,E\t0.0,V\t6.371860,Lat\t-75.561097,Lon\t2817.790,Ellh\t0.011,0.013,0.022\t50,Q\n"
            f"2\t{tow + 5:.6f}\t[{week}]\t0.0,N\t0.0,E\t0.0,V\t6.372000,Lat\t-75.561200,Lon\t2818.500,Ellh\t0.010,0.010,0.020\t50,Q\n"
            f"3\t{tow + 10:.6f}\t[{week}]\t0.0,N\t0.0,E\t0.0,V\t6.372100,Lat\t-75.561300,Lon\t2819.000,Ellh\t0.010,0.010,0.020\t50,Q\n"
            f"4\t{tow + 15:.6f}\t[{week}]\t0.0,N\t0.0,E\t0.0,V\t6.372200,Lat\t-75.561400,Lon\t2820.000,Ellh\t0.010,0.010,0.020\t50,Q\n",
            encoding="utf-8",
        )
        # Candidato duplicado en MRK para secuencia 4 -> debe rechazarse por ambigüedad.
        (mrk_dir / "100_0002_Timestamp.MRK").write_text(
            f"4\t{tow + 15:.6f}\t[{week}]\t0.0,N\t0.0,E\t0.0,V\t6.372205,Lat\t-75.561405,Lon\t2820.100,Ellh\t0.010,0.010,0.020\t50,Q\n",
            encoding="utf-8",
        )

        # seq 1: primaria D sin RTK -> aplica PPK
        p1 = "DJI_20260803120000_0001_D.JPG"
        # seq 2 duplicada en el bloque (dos vuelos) -> NO aplica PPK
        p2a = "DJI_20260803120005_0002_D.JPG"
        p2b = "DJI_20260804120005_0002_D.JPG"
        # seq 3: lente secundaria _MS_G -> NO aplica PPK
        p3 = "DJI_20260803120010_0003_MS_G.TIF"
        # seq 4: secuencia con dos candidatos MRK cercanos -> NO aplica PPK
        p4 = "DJI_20260803120015_0004_D.JPG"

        exiftool_falso[p1] = {
            "GPSLatitude": 6.371870,
            "GPSLongitude": -75.561100,
            "AbsoluteAltitude": 2810.0,
            "RtkFlag": 16,
            "DateTimeOriginal": "2026:08:03 12:00:00",
        }
        exiftool_falso[p2a] = {
            "GPSLatitude": 6.372005,
            "GPSLongitude": -75.561205,
            "AbsoluteAltitude": 2810.0,
            "RtkFlag": 16,
            "DateTimeOriginal": "2026:08:03 12:00:05",
        }
        exiftool_falso[p2b] = dict(exiftool_falso[p2a])
        exiftool_falso[p3] = {
            "GPSLatitude": 6.372105,
            "GPSLongitude": -75.561305,
            "AbsoluteAltitude": 2811.0,
            "RtkFlag": 16,
            "DateTimeOriginal": "2026:08:03 12:00:10",
        }
        exiftool_falso[p4] = {
            "GPSLatitude": 6.372202,
            "GPSLongitude": -75.561402,
            "AbsoluteAltitude": 2812.0,
            "RtkFlag": 16,
            "DateTimeOriginal": "2026:08:03 12:00:15",
        }

        geo = tmp_path / "geo.txt"
        escribir_geo_txt([f"/x/{n}" for n in (p1, p2a, p2b, p3, p4)], str(geo), 1, mrk_dir=str(mrk_dir))
        filas = {l.split("\t")[0]: l.split("\t") for l in geo.read_text().strip().splitlines()[1:]}

        assert float(filas[p1][3]) == pytest.approx(2817.790)
        assert (float(filas[p1][7]), float(filas[p1][8])) == pytest.approx((0.026, 0.044))

        for no_ppk, alt_orig in ((p2a, 2810.0), (p2b, 2810.0), (p3, 2811.0), (p4, 2812.0)):
            assert float(filas[no_ppk][3]) == pytest.approx(alt_orig)
            assert (float(filas[no_ppk][7]), float(filas[no_ppk][8])) == pytest.approx((10.0, 20.0))

    def test_prepare_rgb_odm_gestionar_geo_txt_auto_y_override(self, tmp_path, monkeypatch, exiftool_falso):
        import prepare_rgb_odm

        proj = tmp_path / "rgb_odm"
        images_dir = proj / "images"
        images_dir.mkdir(parents=True)
        mrk_dir = tmp_path / "mrk_rgb"
        mrk_dir.mkdir()
        monkeypatch.setattr(prepare_rgb_odm, "MRK_DIR", str(mrk_dir))

        f_fix = images_dir / "DJI_20260803120000_0001_W.JPG"
        f_single = images_dir / "DJI_20260803120005_0002_W.JPG"
        f_fix.write_bytes(b"a")
        f_single.write_bytes(b"b")
        exiftool_falso[f_fix.name] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 2817.0,
            "RtkFlag": 50,
            "RtkStdLon": 0.01,
            "RtkStdLat": 0.01,
            "RtkStdHgt": 0.02,
        }
        exiftool_falso[f_single.name] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 2817.0,
            "RtkFlag": 16,
        }

        geo_txt = proj / "geo.txt"
        geo_txt.write_text("obsoleto\n", encoding="utf-8")

        # Bloque limpio FIX=50 en modo auto: elimina geo.txt previo y no escribe uno nuevo.
        monkeypatch.setenv("RAPTOR_RGB_GEO_TXT", "auto")
        prepare_rgb_odm._gestionar_geo_txt(str(images_dir), [f_fix.name])
        assert not geo_txt.exists()

        # Bloque uniforme sin RTK (RtkFlag=16/0 sin FIX): tampoco escribe geo.txt en auto y limpia obsoleto.
        f_zero = images_dir / "DJI_20260803120010_0003_W.JPG"
        f_zero.write_bytes(b"z")
        exiftool_falso[f_zero.name] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 2817.0,
            "RtkFlag": 0,
        }
        geo_txt.write_text("obsoleto\n", encoding="utf-8")
        prepare_rgb_odm._gestionar_geo_txt(str(images_dir), [f_single.name, f_zero.name])
        assert not geo_txt.exists()

        # Bloque con RTK FLOAT=34 en modo auto: sí escribe geo.txt con piso (0.30, 0.60).
        f_float = images_dir / "DJI_20260803120015_0004_W.JPG"
        f_float.write_bytes(b"fl")
        exiftool_falso[f_float.name] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "AbsoluteAltitude": 2817.0,
            "RtkFlag": 34,
            "RtkStdLon": 0.05,
            "RtkStdLat": 0.05,
            "RtkStdHgt": 0.10,
        }
        prepare_rgb_odm._gestionar_geo_txt(str(images_dir), [f_float.name])
        assert geo_txt.exists()
        campos_fl = geo_txt.read_text(encoding="utf-8").strip().splitlines()[1].split("\t")
        assert (float(campos_fl[7]), float(campos_fl[8])) == pytest.approx((0.30, 0.60))

        # Bloque mixto en modo auto: escribe geo.txt ponderado.
        prepare_rgb_odm._gestionar_geo_txt(str(images_dir), [f_fix.name, f_single.name])
        assert geo_txt.exists()
        assert f_single.name in geo_txt.read_text(encoding="utf-8")

        # Override RAPTOR_RGB_GEO_TXT=0 elimina geo.txt incluso con bloque mixto.
        monkeypatch.setenv("RAPTOR_RGB_GEO_TXT", "0")
        prepare_rgb_odm._gestionar_geo_txt(str(images_dir), [f_fix.name, f_single.name])
        assert not geo_txt.exists()

    def test_altura_uniforme_en_bloque_parcial_absolute_altitude(self, tmp_path, exiftool_falso, capsys):
        exiftool_falso["con_ambas.TIF"] = {
            "GPSLatitude": 6.4,
            "GPSLongitude": -75.3,
            "GPSAltitude": 2790.0,
            "AbsoluteAltitude": 2817.5,
            "RtkFlag": 50,
            "RtkStdLon": 0.01,
            "RtkStdLat": 0.01,
            "RtkStdHgt": 0.02,
        }
        exiftool_falso["solo_gps.TIF"] = {
            "GPSLatitude": 6.401,
            "GPSLongitude": -75.301,
            "GPSAltitude": 2792.0,
            "RtkFlag": 16,
        }
        geo = tmp_path / "geo.txt"
        escribir_geo_txt(["/x/con_ambas.TIF", "/x/solo_gps.TIF"], str(geo), 1)
        filas = {l.split("\t")[0]: l.split("\t") for l in geo.read_text().strip().splitlines()[1:]}
        assert float(filas["con_ambas.TIF"][3]) == pytest.approx(2790.0)
        assert float(filas["solo_gps.TIF"][3]) == pytest.approx(2792.0)
        assert "AbsoluteAltitude parcial" in capsys.readouterr().out

    def test_prepare_rgb_odm_no_deja_imagenes_sin_limpiar_si_falla_exiftool_y_reintenta(self, tmp_path, monkeypatch):
        import shutil
        import prepare_rgb_odm

        data_rgb = tmp_path / "data_rgb"
        images_dir = tmp_path / "images"
        mrk_dir = tmp_path / "mrk"
        data_rgb.mkdir()
        mrk_dir.mkdir()
        (data_rgb / "DJI_20260803120000_0001_W.JPG").write_bytes(b"img1")
        (data_rgb / "DJI_20260803120005_0002_W.JPG").write_bytes(b"img2")

        monkeypatch.setattr(prepare_rgb_odm, "DATA_RGB", str(data_rgb))
        monkeypatch.setattr(prepare_rgb_odm, "IMAGES_DIR", str(images_dir))
        monkeypatch.setattr(prepare_rgb_odm, "MRK_DIR", str(mrk_dir))
        monkeypatch.setattr(prepare_rgb_odm, "NPROCS", 1)
        monkeypatch.setenv("RAPTOR_RGB_GEO_TXT", "0")

        intentos = {"exiftool": 0, "limpiados": []}

        def _fake_run(cmd, **kw):
            if cmd[0] == "cp":
                shutil.copy2(cmd[-2], cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "exiftool":
                intentos["exiftool"] += 1
                lote = kw["input"].strip().splitlines()
                if intentos["exiftool"] == 1:
                    return subprocess.CompletedProcess(cmd, 1, "", "simulated exiftool error")
                intentos["limpiados"].extend(os.path.basename(p) for p in lote)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"comando inesperado: {cmd}")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        with pytest.raises(RuntimeError, match="exiftool falló"):
            prepare_rgb_odm.main()
        assert os.listdir(images_dir) == []

        # Al reintentar, pendientes vuelve a contener las 2 imágenes y las limpia todas.
        prepare_rgb_odm.main()
        assert sorted(os.listdir(images_dir)) == [
            "DJI_20260803120000_0001_W.JPG",
            "DJI_20260803120005_0002_W.JPG",
        ]
        assert intentos["exiftool"] == 2
        assert len(intentos["limpiados"]) == 2
