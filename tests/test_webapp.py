"""Webapp: validación previa, entrega al host y guardas de misión activa.

El principio que protege este archivo está en el docstring de webapp/main.py:
una misión que no puede terminar bien tiene que fallar EN EL FORMULARIO, en
segundos y corregible ahí mismo, y no 40 minutos después adentro de ODM.
"""
import importlib
import os

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Webapp con RUNS_ROOT temporal y una carpeta de entrega montada."""
    monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("EXPORT_HOST_DIR", "/home/usuario/entregas")
    montaje = tmp_path / "export"
    montaje.mkdir()
    monkeypatch.setenv("EXPORT_MOUNT", str(montaje))
    from fastapi.testclient import TestClient
    # core.scan fija RUNS_ROOT al importarse, así que hay que recargarlo ANTES
    # que webapp.main — si no, la webapp sigue apuntando a /app/runs y los
    # tests pasan sin tocar el directorio temporal.
    from core import scan
    importlib.reload(scan)
    from webapp import main as M
    importlib.reload(M)
    M._state = None
    assert M.RUNS_ROOT == tmp_path / "runs", "el RUNS_ROOT temporal no tomó efecto"
    return TestClient(M.app), M, tmp_path / "runs", str(montaje)


def _subir(runs, mision, kind, nombres):
    d = runs / mision / "raw" / kind
    d.mkdir(parents=True, exist_ok=True)
    for n in nombres:
        (d / n).write_text("x")


def _bandas(prefijos, bandas=("G", "R", "RE", "NIR")):
    return [f"{p}_MS_{b}.TIF" for p in prefijos for b in bandas]


class TestBandasMultiespectrales:
    """ODM empareja las 4 bandas de cada captura y aborta si a alguna le falta
    su compañera — pero recién dentro de compute_band_maps(), después del SfM
    completo y con un mensaje genérico sobre CaptureUUID."""

    def test_capturas_completas_pasan(self, app):
        c, M, runs, montaje = app
        _subir(runs, "m", "multispectral", _bandas(["DJI_0001", "DJI_0002"]))
        uploads = {"rgb_thermal": M.classify_files([]),
                   "multispectral": M.classify_files(_bandas(["DJI_0001", "DJI_0002"]))}
        assert M._validate("none", True, uploads) == []

    def test_captura_incompleta_se_rechaza_nombrandola(self, app):
        c, M, runs, montaje = app
        archivos = _bandas(["DJI_0001"]) + _bandas(["DJI_0002"], ("G", "R", "RE"))
        uploads = {"rgb_thermal": M.classify_files([]),
                   "multispectral": M.classify_files(archivos)}
        errs = M._validate("none", True, uploads)
        assert errs, "una captura sin su banda NIR tiene que rechazarse"
        assert "DJI_0002" in errs[0], f"el error debe nombrar la captura: {errs[0]}"
        assert "NIR" not in errs[0].split("solo")[1].split(")")[0], \
            "debe listar las bandas PRESENTES, no la que falta"

    def test_banda_entera_faltante_se_distingue_de_captura_incompleta(self, app):
        c, M, runs, montaje = app
        # Ninguna captura tiene NIR: es una banda entera ausente, no capturas sueltas
        archivos = _bandas(["DJI_0001", "DJI_0002"], ("G", "R", "RE"))
        errs = M._validate("none", True, {"rgb_thermal": M.classify_files([]),
                                          "multispectral": M.classify_files(archivos)})
        assert errs and "bandas enteras" in errs[0], errs
        assert "NIR" in errs[0]

    def test_cuenta_por_banda(self, app):
        c, M, runs, montaje = app
        counts = M.classify_files(_bandas(["A", "B"]) + ["A_MS_G.TIF"])
        assert counts["ms"] == 9
        assert counts["ms_bands"] == {"G": 3, "R": 2, "RE": 2, "NIR": 2}

    def test_lista_capturas_incompletas_ordenadas(self, app):
        c, M, runs, montaje = app
        archivos = (_bandas(["C"]) + _bandas(["A"], ("G",)) + _bandas(["B"], ("G", "R")))
        inc = M._incomplete_captures(archivos)
        assert [p for p, _ in inc] == ["A", "B"]
        assert inc[0][1] == ["G"] and inc[1][1] == ["G", "R"]

    def test_muchas_incompletas_se_resumen(self, app):
        c, M, runs, montaje = app
        # Una captura completa, para que ninguna BANDA falte entera y el error
        # sea el de capturas incompletas y no el de banda ausente.
        archivos = (_bandas(["DJI_9999"])
                    + _bandas([f"DJI_{i:04d}" for i in range(10)], ("G", "R")))
        errs = M._validate("none", True, {"rgb_thermal": M.classify_files([]),
                                          "multispectral": M.classify_files(archivos)})
        assert "10 captura" in errs[0] and "más" in errs[0], errs[0]


class TestEntregaAlHost:
    def test_traduce_host_a_contenedor(self, app):
        c, M, runs, montaje = app
        assert M.host_a_contenedor("/home/usuario/entregas/la_clara") == f"{montaje}/la_clara"
        assert M.host_a_contenedor("/home/usuario/entregas") == montaje

    def test_rechaza_rutas_fuera_del_montaje(self, app):
        c, M, runs, montaje = app
        assert M.host_a_contenedor("/etc/passwd") is None
        # Prefijo parcial: "entregas_otro" NO está dentro de "entregas"
        assert M.host_a_contenedor("/home/usuario/entregas_otro") is None

    def test_status_expone_la_raiz_del_host(self, app):
        c, M, runs, montaje = app
        s = c.get("/api/missions/m1/status").json()
        assert s["export_enabled"] is True
        assert s["export_host_root"] == "/home/usuario/entregas"
        assert s["default_export_dir"] == "/home/usuario/entregas/m1"

    def test_check_export_confirma_la_ruta_del_host(self, app):
        c, M, runs, montaje = app
        r = c.post("/api/missions/m1/check-export",
                   data={"export_dir": "/home/usuario/entregas/la_clara",
                         "export_epsg": "9377"}).json()
        assert r["ok"], r["errors"]
        assert r["resolved"] == "/home/usuario/entregas/la_clara"
        assert r["crs_name"] == "MAGNA-SIRGAS 2018 / Origen-Nacional"

    def test_check_export_rechaza_fuera_del_montaje(self, app):
        c, M, runs, montaje = app
        r = c.post("/api/missions/m1/check-export",
                   data={"export_dir": "/etc", "export_epsg": "9377"}).json()
        assert not r["ok"]
        assert "fuera de la carpeta montada" in " ".join(r["errors"])

    def test_epsg_inexistente_se_rechaza(self, app):
        c, M, runs, montaje = app
        r = c.post("/api/missions/m1/check-export",
                   data={"export_dir": "/home/usuario/entregas/x",
                         "export_epsg": "999999"}).json()
        assert not r["ok"] and "no existe" in " ".join(r["errors"])

    def test_sin_montaje_la_exportacion_no_se_ofrece(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        monkeypatch.delenv("EXPORT_HOST_DIR", raising=False)
        from fastapi.testclient import TestClient
        from webapp import main as M
        importlib.reload(M)
        M._state = None
        s = TestClient(M.app).get("/api/missions/m1/status").json()
        assert s["export_enabled"] is False
        errs, env = M._validate_export(None, "/lo/que/sea", ["rgb"], "cog", "geojson", "9377")
        assert errs and "raptor webapp --export" in errs[0]
        assert env == {}


class TestValidacionPrevia:
    def test_start_rechaza_config_de_entrega_invalida_sin_lanzar_nada(self, app):
        c, M, runs, montaje = app
        _subir(runs, "m2", "rgb_thermal", ["DJI_0001_V.JPG"])
        r = c.post("/api/missions/m2/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "export_dir": "/etc", "export_products": "rgb", "export_epsg": "9377"})
        assert r.status_code == 400
        assert M._state is None, "no puede haber quedado un pipeline lanzado"

    def test_start_rechaza_producto_desconocido_nombrandolo(self, app):
        c, M, runs, montaje = app
        _subir(runs, "m2", "rgb_thermal", ["DJI_0001_V.JPG"])
        r = c.post("/api/missions/m2/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "export_dir": "/home/usuario/entregas/m2",
            "export_products": "rgb,inventado", "export_epsg": "9377"})
        assert r.status_code == 400 and "inventado" in r.json()["detail"]

    def test_un_get_no_crea_directorios(self, app):
        """Un sondeo a un nombre inexistente no debe dejar una misión fantasma
        vacía en el selector."""
        c, M, runs, montaje = app
        c.get("/api/missions/inventada/status")
        assert not (runs / "inventada").exists()


class TestLogEnDisco:
    def test_devuelve_el_log_de_una_corrida_anterior(self, app):
        """El estado en memoria solo cubre la última corrida de ESTE proceso;
        tras reiniciar el servidor el motivo del fallo sigue en disco."""
        c, M, runs, montaje = app
        d = runs / "vieja" / "outputs" / "logs"
        d.mkdir(parents=True)
        (d / "odm_rgb.log").write_text("linea1\nERROR: se acabó la memoria\n")
        r = c.get("/api/missions/vieja/log")
        assert r.status_code == 200
        j = r.json()
        assert "se acabó la memoria" in j["lines"][-1]
        assert j["source"] == "outputs/logs/odm_rgb.log"

    def test_mision_sin_logs_da_404(self, app):
        c, M, runs, montaje = app
        assert c.get("/api/missions/inexistente/log").status_code == 404


class TestGuardasDeMisionActiva:
    def test_no_se_reapunta_una_mision_mientras_otra_corre(self, app):
        """activate_mission() mueve symlinks GLOBALES del contenedor: abrir
        otra misión mientras una corre dejaría al pipeline escribiendo en el
        directorio equivocado."""
        c, M, runs, montaje = app

        class _Falsa:
            mission_name, done = "corriendo", False
        M._state = _Falsa()
        try:
            r = c.post("/api/missions/otra/activate")
            assert r.status_code == 409
            assert "corriendo" in r.json()["detail"]
        finally:
            M._state = None
