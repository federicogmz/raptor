"""export_cog.py / export_copc.py: exportación cloud-optimized por sensor,
apenas termina el post-procesamiento de CADA uno (ver _odm_post/_odm_export
en docker/entrypoint.sh), más un pase de seguridad final sin argumentos que
recorre todo lo que exista en outputs/.

Los dos scripts resuelven la MISMA pregunta desde dos modos:
  - archivos/sensores EXPLÍCITOS (modo por sensor): sin salteo por
    idempotencia — si se llama así es porque se sabe que el archivo recién
    se escribió.
  - SIN argumentos (pase de seguridad): con salteo de lo que ya esté al día,
    para no reconvertir de gratis lo que el pase por sensor ya dejó listo.
"""
import os
import subprocess

import numpy as np
import pytest
from osgeo import gdal

import export_cog
import export_copc

gdal.UseExceptions()

GT = (466000.0, 0.2, 0.0, 708900.0, 0.0, -0.2)


def _raster(path, w=20, h=20, dtype=gdal.GDT_Float32):
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, dtype)
    ds.SetGeoTransform(GT)
    arr = np.full((h, w), 1, dtype=np.float32 if dtype == gdal.GDT_Float32 else np.uint8)
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None


@pytest.fixture
def mision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("outputs")
    return tmp_path


class TestYaEsCog:
    def test_raster_normal_no_es_cog(self, mision):
        _raster("outputs/dsm.tif")
        assert export_cog._ya_es_cog("outputs/dsm.tif") is False

    def test_despues_de_to_cog_si_lo_es(self, mision):
        _raster("outputs/dsm.tif")
        assert export_cog.to_cog("outputs/dsm.tif") is True
        assert export_cog._ya_es_cog("outputs/dsm.tif") is True

    def test_archivo_inexistente_no_es_cog(self, mision):
        assert export_cog._ya_es_cog("outputs/no_existe.tif") is False


class TestToCog:
    def test_convierte_preservando_valores(self, mision):
        _raster("outputs/dsm.tif")
        ds_antes = gdal.Open("outputs/dsm.tif")
        antes = ds_antes.GetRasterBand(1).ReadAsArray()
        ds_antes = None
        assert export_cog.to_cog("outputs/dsm.tif") is True
        ds = gdal.Open("outputs/dsm.tif")
        despues = ds.GetRasterBand(1).ReadAsArray()
        assert ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG"
        ds = None
        assert np.array_equal(antes, despues)

    def test_predictor_segun_tipo_de_dato(self, mision):
        """3 = float (DSM/térmico/multiespectral/índices), 2 = entero
        (RGB byte, máscara de confianza)."""
        _raster("outputs/float.tif", dtype=gdal.GDT_Float32)
        assert export_cog._predictor_for("outputs/float.tif") == "3"
        _raster("outputs/byte.tif", dtype=gdal.GDT_Byte)
        assert export_cog._predictor_for("outputs/byte.tif") == "2"


class TestModoPorSensorSinSalteo:
    def test_convierte_aunque_ya_sea_cog(self, mision, monkeypatch):
        """El modo explícito no consulta _ya_es_cog: si se llama así es
        porque se sabe que el archivo recién se escribió."""
        _raster("outputs/rgb_orthomosaic.tif")
        export_cog.to_cog("outputs/rgb_orthomosaic.tif")  # ya es COG ahora
        llamadas = []
        monkeypatch.setattr(export_cog, "to_cog", lambda p: (llamadas.append(p), True)[1])
        monkeypatch.setattr("sys.argv", ["export_cog.py", "outputs/rgb_orthomosaic.tif"])
        export_cog.main()
        assert llamadas == ["outputs/rgb_orthomosaic.tif"]

    def test_avisa_de_faltantes_y_sigue_con_el_resto(self, mision, monkeypatch, capsys):
        _raster("outputs/dsm.tif")
        monkeypatch.setattr("sys.argv", ["export_cog.py", "outputs/no_existe.tif", "outputs/dsm.tif"])
        export_cog.main()
        salida = capsys.readouterr().out
        assert "no_existe.tif no existe, omitiendo" in salida
        assert export_cog._ya_es_cog("outputs/dsm.tif")

    def test_sin_nada_para_convertir_no_falla(self, mision, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["export_cog.py", "outputs/no_existe.tif"])
        export_cog.main()  # no debe lanzar SystemExit
        assert "0 raster(es) convertido" in capsys.readouterr().out


class TestModoDeSeguridadConSalteo:
    def test_saltea_lo_que_ya_es_cog(self, mision, monkeypatch, capsys):
        _raster("outputs/rgb_orthomosaic.tif")
        export_cog.to_cog("outputs/rgb_orthomosaic.tif")
        monkeypatch.setattr(export_cog, "PRODUCTS", ["outputs/rgb_orthomosaic.tif"])
        llamado = []
        monkeypatch.setattr(export_cog, "to_cog", lambda p: llamado.append(p) or True)
        monkeypatch.setattr("sys.argv", ["export_cog.py"])
        export_cog.main()
        assert llamado == []
        assert "ya es COG, omitiendo" in capsys.readouterr().out

    def test_convierte_lo_que_no_es_cog_todavia(self, mision, monkeypatch):
        _raster("outputs/dsm.tif")
        monkeypatch.setattr(export_cog, "PRODUCTS", ["outputs/dsm.tif"])
        monkeypatch.setattr("sys.argv", ["export_cog.py"])
        export_cog.main()
        assert export_cog._ya_es_cog("outputs/dsm.tif")

    def test_dband_orthomosaic_esta_en_productos(self):
        """dband_orthomosaic.tif se agregó a PRODUCTS — antes faltaba,
        mientras los otros tres ortomosaicos crudos por sensor ya estaban."""
        assert any("dband_orthomosaic.tif" in p for p in export_cog.PRODUCTS)


class TestFalloEnConversion:
    def test_un_fallo_reporta_cual_y_sale_con_error(self, mision, monkeypatch, capsys):
        """COG y COPC son productos independientes: acá se prueba que un
        fallo puntual no se trague en silencio ni tumbe todo sin decir cuál."""
        _raster("outputs/a.tif")
        _raster("outputs/b.tif")
        monkeypatch.setattr(export_cog, "to_cog", lambda p: "a.tif" not in p)
        monkeypatch.setattr("sys.argv", ["export_cog.py", "outputs/a.tif", "outputs/b.tif"])
        with pytest.raises(SystemExit) as exc:
            export_cog.main()
        assert exc.value.code == 1
        assert "outputs/a.tif" in capsys.readouterr().out


class TestAlDia:
    def test_destino_mas_nuevo_esta_al_dia(self, mision):
        open("a", "wb").write(b"x")
        open("b", "wb").write(b"x")
        os.utime("b", (os.path.getmtime("a") + 10, os.path.getmtime("a") + 10))
        assert export_copc._al_dia("a", "b") is True

    def test_destino_mas_viejo_no_esta_al_dia(self, mision):
        open("a", "wb").write(b"x")
        open("b", "wb").write(b"x")
        os.utime("b", (os.path.getmtime("a") - 10, os.path.getmtime("a") - 10))
        assert export_copc._al_dia("a", "b") is False

    def test_sin_destino_no_esta_al_dia(self, mision):
        open("a", "wb").write(b"x")
        assert export_copc._al_dia("a", "no-existe") is False


class TestExportCopc:
    @pytest.fixture
    def proyecto(self, mision):
        """Crea el .laz georreferenciado falso de un sensor en la carpeta
        que le corresponde según PROJECTS."""
        def _crear(sensor, proj_dir=None):
            proj_dir = proj_dir or export_copc.PROJECTS[sensor][0]
            geo = os.path.join(proj_dir, "odm_georeferencing")
            os.makedirs(geo, exist_ok=True)
            src = os.path.join(geo, "odm_georeferenced_model.laz")
            open(src, "wb").write(b"fake-laz")
            return src
        return _crear

    def _fake_pdal(self, monkeypatch):
        monkeypatch.setattr(export_copc, "_pdal", lambda: "pdal")
        llamadas = []

        def fake_run(cmd, **kw):
            llamadas.append(cmd)
            open(cmd[3], "wb").write(b"fake-copc")  # cmd[3] = dst

            class R:
                returncode = 0
                stderr = ""
            return R()
        monkeypatch.setattr(subprocess, "run", fake_run)
        return llamadas

    def test_sensor_desconocido_falla_claro(self, mision, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["export_copc.py", "no-existe"])
        with pytest.raises(SystemExit) as exc:
            export_copc.main()
        assert exc.value.code == 1
        assert "no-existe" in capsys.readouterr().out

    def test_pdal_no_encontrado_falla_claro(self, mision, monkeypatch, capsys):
        monkeypatch.setattr(export_copc, "_pdal", lambda: None)
        monkeypatch.setattr("sys.argv", ["export_copc.py"])
        with pytest.raises(SystemExit) as exc:
            export_copc.main()
        assert exc.value.code == 1
        assert "pdal no encontrado" in capsys.readouterr().out

    def test_dband_incluido_en_projects(self):
        """Antes esta lista solo tenía 3 sensores — banda D quedaba afuera
        sin ninguna razón real (su .laz georreferenciado existe igual, más
        chico porque --fast-orthophoto usa la nube dispersa de OpenSfM)."""
        assert "dband" in export_copc.PROJECTS

    def test_modo_por_sensor_no_saltea_aunque_este_al_dia(self, mision, monkeypatch, proyecto):
        src = proyecto("rgb")
        dst = os.path.join("outputs", export_copc.PROJECTS["rgb"][1])
        open(dst, "wb").write(b"copc-viejo")
        os.utime(dst, (os.path.getmtime(src) + 1000,) * 2)  # dst YA está al día
        llamadas = self._fake_pdal(monkeypatch)
        monkeypatch.setattr("sys.argv", ["export_copc.py", "rgb"])
        export_copc.main()
        assert len(llamadas) == 1, "el modo explícito no debe saltear por idempotencia"

    def test_modo_de_seguridad_saltea_lo_que_esta_al_dia(self, mision, monkeypatch, proyecto, capsys):
        src = proyecto("rgb")
        dst = os.path.join("outputs", export_copc.PROJECTS["rgb"][1])
        open(dst, "wb").write(b"copc-viejo")
        os.utime(dst, (os.path.getmtime(src) + 1000,) * 2)
        llamadas = self._fake_pdal(monkeypatch)
        monkeypatch.setattr("sys.argv", ["export_copc.py"])
        export_copc.main()
        assert llamadas == []
        assert "ya al día, omitiendo" in capsys.readouterr().out

    def test_modo_de_seguridad_convierte_lo_desactualizado(self, mision, monkeypatch, proyecto):
        src = proyecto("thermal")
        dst = os.path.join("outputs", export_copc.PROJECTS["thermal"][1])
        open(dst, "wb").write(b"copc-viejo")
        os.utime(dst, (os.path.getmtime(src) - 1000,) * 2)  # dst quedó VIEJO
        llamadas = self._fake_pdal(monkeypatch)
        monkeypatch.setattr("sys.argv", ["export_copc.py"])
        export_copc.main()
        assert len(llamadas) == 1

    def test_sin_reconstruccion_georeferenciada_no_hace_nada(self, mision, monkeypatch, capsys):
        """Sin el .laz de ODM (reconstrucción no terminada / falló antes de
        georeferenciar) no hay nada que convertir — no debe ser un error."""
        llamadas = self._fake_pdal(monkeypatch)
        monkeypatch.setattr("sys.argv", ["export_copc.py", "multispectral"])
        export_copc.main()
        assert llamadas == []
        assert "0 nube(s)" in capsys.readouterr().out

    def test_usa_el_directorio_configurado_en_projects(self, mision, monkeypatch, proyecto, tmp_path):
        """El origen de cada sensor sale de PROJECTS (env ODM_*_DIR en la
        corrida real, ver docker/entrypoint.sh) — no de una ruta fija."""
        alterno = str(tmp_path / "otro_lado" / "multispectral_odm")
        monkeypatch.setattr(export_copc, "PROJECTS", {
            **export_copc.PROJECTS,
            "multispectral": (alterno, export_copc.PROJECTS["multispectral"][1]),
        })
        src = proyecto("multispectral", proj_dir=alterno)
        llamadas = self._fake_pdal(monkeypatch)
        monkeypatch.setattr("sys.argv", ["export_copc.py", "multispectral"])
        export_copc.main()
        assert llamadas[0][2] == src
