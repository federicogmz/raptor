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

    def test_export_host_dir_con_punto_sin_resolver_no_rompe_la_comparacion(self, tmp_path, monkeypatch):
        """Reportado en vivo: ./raptor webapp --export ./entregas arma
        EXPORT_HOST_DIR anteponiendo $PWD sin normalizar — quedaba como
        ".../raptor/./entregas", con un "./" literal en el medio. Como
        host_a_contenedor() SÍ normaliza la ruta ENTRANTE con
        os.path.normpath() antes de comparar, la comparación de prefijo
        contra el EXPORT_HOST_DIR sin normalizar fallaba siempre — el
        usuario veía "«/ruta/correcta/» está fuera de la carpeta montada
        (esa misma ruta/correcta)", con la carpeta correcta señalada como
        si fuera otra. _normalize_host_dir() lo arregla en la raíz."""
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        montaje = tmp_path / "entregas"
        montaje.mkdir()
        monkeypatch.setenv("EXPORT_HOST_DIR", f"{tmp_path}/./entregas")
        monkeypatch.setenv("EXPORT_MOUNT", str(montaje))
        from webapp import main as M
        importlib.reload(M)
        assert M.EXPORT_HOST_DIR == str(tmp_path / "entregas"), \
            "EXPORT_HOST_DIR tiene que quedar normalizado, sin el './' literal"
        # La ruta por defecto que la webapp le ofrece al usuario para SU
        # propia misión tiene que resolver bien, no rebotar como "fuera
        # de la carpeta montada".
        assert M.host_a_contenedor(f"{tmp_path}/entregas/mi_mision") == str(montaje / "mi_mision")

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


class TestExportacionADemanda:
    """Export ya no se pide antes de arrancar la corrida (ver
    TestValidacionPrevia, que perdió esos dos tests) — se pide después,
    contra una misión que YA tiene productos reales en disco."""

    def _con_rgb_generado(self, runs, mision="m1"):
        outputs = runs / mision / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "rgb_orthomosaic.tif").write_bytes(b"")
        return outputs

    def test_export_options_lista_solo_lo_que_existe(self, app):
        c, M, runs, montaje = app
        self._con_rgb_generado(runs, "m1")
        r = c.get("/api/missions/m1/export-options").json()
        assert "rgb" in r["available_products"]
        assert "multispectral" not in r["available_products"]
        assert r["export_enabled"] is True
        assert r["default_export_dir"] == "/home/usuario/entregas/m1"

    def test_rechaza_producto_no_generado(self, app):
        c, M, runs, montaje = app
        self._con_rgb_generado(runs, "m1")
        r = c.post("/api/missions/m1/export", data={
            "export_dir": "/home/usuario/entregas/m1",
            "export_products": "rgb,multispectral"})
        assert r.status_code == 400 and "multispectral" in r.json()["detail"]

    def test_rechaza_carpeta_fuera_del_montaje(self, app):
        c, M, runs, montaje = app
        self._con_rgb_generado(runs, "m1")
        r = c.post("/api/missions/m1/export", data={
            "export_dir": "/etc", "export_products": "rgb"})
        assert r.status_code == 400

    def test_rechaza_mientras_la_mision_esta_corriendo(self, app):
        c, M, runs, montaje = app
        self._con_rgb_generado(runs, "m1")

        class _Falsa:
            mission_name, done = "m1", False
        M._state = _Falsa()
        try:
            r = c.post("/api/missions/m1/export", data={
                "export_dir": "/home/usuario/entregas/m1", "export_products": "rgb"})
            assert r.status_code == 409
        finally:
            M._state = None

    def test_rechaza_sin_productos_marcados(self, app):
        c, M, runs, montaje = app
        self._con_rgb_generado(runs, "m1")
        # " " (no vacío del todo) en vez de "": un value="" en form-encoded
        # puede llegar como campo AUSENTE del lado del server según el
        # cliente, lo que da 422 (Form(...) required) en vez de ejercitar la
        # validación real de "sin productos" que se busca probar acá.
        r = c.post("/api/missions/m1/export", data={
            "export_dir": "/home/usuario/entregas/m1", "export_products": " "})
        assert r.status_code == 400


class TestValidacionPrevia:
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


class TestOdmProjects:
    """_odm_projects() es puramente informativo (status de la misión) desde
    que se sacó el toggle "reusar ODM" de la webapp — ver el docstring de
    _odm_projects en webapp/main.py sobre por qué ese interruptor se sacó
    (era global, no por sensor, y se ofrecía en base a una señal que no
    garantizaba una reconstrucción completa: reportado en vivo, una misión
    con multiespectral que llegó a SfM/malla pero se cortó en texturizado
    igual se hubiera ofrecido como 'reusable')."""

    def _con_processing(self, runs, mision, *sensores):
        d = runs / mision / "processing"
        for s, sub in (("rgb", "rgb_odm"), ("thermal", "thermal_native_odm"),
                      ("multispectral", "multispectral_odm"), ("dband", "dband_odm")):
            if s in sensores:
                p = d / sub / "opensfm"
                p.mkdir(parents=True)
                (p / "reconstruction.json").write_text("[]")

    def test_odm_projects_incluye_dband(self, app):
        c, M, runs, montaje = app
        self._con_processing(runs, "m14", "dband")
        found = M._odm_projects(runs / "m14")
        assert found["dband"] is True
        assert found["rgb"] is False


class TestCancelarMision:
    """Reportado en vivo: una corrida sin el multiespectral que hacía falta,
    y ninguna forma de pararla desde la webapp — hubo que matarla a mano
    con docker exec. El endpoint solo llama a run.kill(); la garantía real
    de que eso mata el árbol de procesos completo (no solo entrypoint.sh)
    se prueba aparte en test_runner_kill.py."""

    def test_cancela_la_que_esta_corriendo(self, app):
        c, M, runs, montaje = app

        class _RunFalso:
            killed = False
            def kill(self):
                self.killed = True
        run_falso = _RunFalso()

        class _Falsa:
            mission_name, done, run = "corriendo", False, run_falso
        M._state = _Falsa()
        try:
            r = c.post("/api/missions/corriendo/cancel")
            assert r.status_code == 200
            assert run_falso.killed is True
        finally:
            M._state = None

    def test_404_si_no_es_la_mision_activa(self, app):
        c, M, runs, montaje = app
        assert c.post("/api/missions/otra/cancel").status_code == 404

    def test_409_si_ya_termino(self, app):
        c, M, runs, montaje = app

        class _Falsa:
            mission_name, done = "corriendo", True
        M._state = _Falsa()
        try:
            r = c.post("/api/missions/corriendo/cancel")
            assert r.status_code == 409
        finally:
            M._state = None


class TestEstadoDeLaUltimaCorridaSobreviveElReinicio:
    """Bug real, reportado en vivo: una misión que falló, redesplegar el
    contenedor (rebuild + restart, algo que pasa seguido en esta app), y la
    tarjeta de esa misión volvía a mostrar "Geovisor" como si nunca hubiera
    corrido — porque el resultado vivía SOLO en _state (memoria del
    proceso, se pierde en cada arranque nuevo). outputs/run_summary.json
    (docker/entrypoint.sh lo escribe siempre, hasta si falla) es la fuente
    que sí sobrevive."""

    def _run_summary(self, runs, mision, ok):
        d = runs / mision / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        import json
        (d / "run_summary.json").write_text(json.dumps({"ok": ok, "exit_code": 0 if ok else 2}))

    def test_mision_fallida_sin_estado_en_memoria_sale_ok_false(self, app):
        """El caso real: contenedor recién arrancado (M._state es None,
        como después de cualquier redeploy), la misión ya había fallado
        antes."""
        c, M, runs, montaje = app
        (runs / "m1").mkdir(parents=True)
        self._run_summary(runs, "m1", ok=False)
        assert M._state is None
        r = c.get("/api/missions")
        assert r.status_code == 200
        m = next(x for x in r.json()["missions"] if x["name"] == "m1")
        assert m["ok"] is False

    def test_mision_exitosa_sin_estado_en_memoria_sale_ok_true(self, app):
        c, M, runs, montaje = app
        (runs / "m2").mkdir(parents=True)
        self._run_summary(runs, "m2", ok=True)
        r = c.get("/api/missions")
        m = next(x for x in r.json()["missions"] if x["name"] == "m2")
        assert m["ok"] is True

    def test_mision_que_nunca_corrio_sale_ok_none(self, app):
        c, M, runs, montaje = app
        (runs / "m3").mkdir(parents=True)
        r = c.get("/api/missions")
        m = next(x for x in r.json()["missions"] if x["name"] == "m3")
        assert m["ok"] is None

    def test_el_estado_en_memoria_gana_si_es_mas_reciente(self, app):
        """_state manda cuando de verdad hay uno para esta misión — no hay
        que esperar a que run_summary.py termine de escribir para verlo."""
        c, M, runs, montaje = app
        (runs / "m4").mkdir(parents=True)
        self._run_summary(runs, "m4", ok=False)  # archivo viejo: falló

        class _Falsa:
            mission_name, done, returncode = "m4", True, 0  # memoria: tuvo éxito
        M._state = _Falsa()
        try:
            r = c.get("/api/missions")
            m = next(x for x in r.json()["missions"] if x["name"] == "m4")
            assert m["ok"] is True
        finally:
            M._state = None


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


class TestImportarLocal:
    """Copiar fotos ya presentes en el disco del servidor (./raptor webapp
    --import DIR) en vez de subirlas por el navegador — mismo patrón que la
    exportación, pero de solo lectura y en la otra dirección. Reportado:
    "la subida es muy lenta ¿podría no ser necesaria si ya están locales?"."""

    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        monkeypatch.delenv("EXPORT_HOST_DIR", raising=False)
        fuente = tmp_path / "fotos_del_dron"
        fuente.mkdir()
        monkeypatch.setenv("IMPORT_HOST_DIR", str(fuente))
        monkeypatch.setenv("IMPORT_MOUNT", str(fuente))
        from fastapi.testclient import TestClient
        from core import scan
        importlib.reload(scan)
        from webapp import main as M
        importlib.reload(M)
        M._state = None
        return TestClient(M.app), M, tmp_path / "runs", fuente

    def test_import_enabled_en_status(self, app):
        c, M, runs, fuente = app
        s = c.get("/api/missions/m1/status").json()
        assert s["import_enabled"] is True
        assert s["import_host_root"] == str(fuente)

    def test_sin_montaje_import_no_se_ofrece(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        monkeypatch.delenv("IMPORT_HOST_DIR", raising=False)
        from fastapi.testclient import TestClient
        from webapp import main as M
        importlib.reload(M)
        M._state = None
        s = TestClient(M.app).get("/api/missions/m1/status").json()
        assert s["import_enabled"] is False

    def test_primera_importacion_es_un_symlink_sin_copiar_nada(self, app):
        """Reportado en vivo: la primera versión copiaba/hardlinkeaba
        archivo por archivo — "¿por qué se duplican, si ya están acá?",
        con razón (un hardlink sigue siendo una segunda ruta real).
        raw/<kind>/ es un directorio real que contiene un symlink por
        cada carpeta agregada — cero archivos nuevos en el disco de
        runs/, incluso con una sola carpeta agregada."""
        c, M, runs, fuente = app
        (fuente / "DJI_0001_V.JPG").write_text("rgb")
        (fuente / "DJI_0001_T.JPG").write_text("termica")
        r = c.post("/api/missions/mi_mision/import-local",
                   data={"kind": "rgb_thermal", "host_path": str(fuente)})
        assert r.status_code == 200
        j = r.json()
        assert j["uploads"]["rgb"] == 1 and j["uploads"]["thermal"] == 1
        dest = runs / "mi_mision" / "raw" / "rgb_thermal"
        assert dest.is_dir() and not dest.is_symlink(), \
            "raw/rgb_thermal tiene que ser un directorio real (contenedor de symlinks)"
        entrada = dest / j["entry"]
        assert entrada.is_symlink(), "la carpeta agregada tiene que ser un symlink, no una copia"
        assert os.readlink(entrada) == str(fuente)
        assert (entrada / "DJI_0001_V.JPG").read_text() == "rgb"  # se lee A TRAVÉS del symlink

    def test_busca_recursivo_en_subcarpetas_a_traves_del_symlink(self, app):
        """El dron suele entregar las fotos en subcarpetas (100MEDIA/) —
        _listdir_names() (lo que cuenta "qué hay subido" para calidad,
        validación, la lista de misiones) tiene que verlas A TRAVÉS del
        symlink, aunque esté ANIDADO adentro de raw/<kind>/ (no en el
        nivel superior directo)."""
        c, M, runs, fuente = app
        sub = fuente / "100MEDIA"
        sub.mkdir()
        (sub / "DJI_0001_V.JPG").write_text("x")
        r = c.post("/api/missions/m/import-local",
                   data={"kind": "rgb_thermal", "host_path": str(fuente)}).json()
        assert r["uploads"]["rgb"] == 1
        dest = runs / "m" / "raw" / "rgb_thermal"
        entrada = dest / r["entry"]
        assert entrada.is_symlink()
        assert (entrada / "100MEDIA" / "DJI_0001_V.JPG").exists()

    def test_repetir_la_importacion_no_duplica(self, app):
        """Reimportar la MISMA carpeta no agrega una segunda entrada —
        _link_one_folder() detecta que ya apunta al mismo destino real y
        devuelve la entrada existente tal cual."""
        c, M, runs, fuente = app
        (fuente / "a_V.JPG").write_text("x")
        r1 = c.post("/api/missions/m/import-local",
                    data={"kind": "rgb_thermal", "host_path": str(fuente)}).json()
        r2 = c.post("/api/missions/m/import-local",
                    data={"kind": "rgb_thermal", "host_path": str(fuente)}).json()
        assert r2["entry"] == r1["entry"], "reimportar la misma carpeta no debería crear una entrada nueva"
        dest = runs / "m" / "raw" / "rgb_thermal"
        assert len(list(dest.iterdir())) == 1, "no debería haber quedado una segunda entrada duplicada"
        assert r2["uploads"]["rgb"] == 1, "el conteo no debería duplicarse"

    def test_agregar_varias_carpetas_suma_symlinks_sin_reemplazar(self, app):
        """Caso real: dos tarjetas del mismo vuelo (100MEDIA/, 101MEDIA/)
        como carpetas separadas — cada una se agrega como un symlink más,
        ninguna reemplaza a la otra."""
        c, M, runs, fuente = app
        (fuente / "a_V.JPG").write_text("x")
        otra = fuente.parent / "otra_tarjeta"
        otra.mkdir()
        (otra / "b_V.JPG").write_text("y")
        M.IMPORT_HOST_DIR = str(fuente.parent)
        M.IMPORT_MOUNT = str(fuente.parent)

        r1 = c.post("/api/missions/m/import-local",
                    data={"kind": "rgb_thermal", "host_path": str(fuente)}).json()
        r2 = c.post("/api/missions/m/import-local",
                    data={"kind": "rgb_thermal", "host_path": str(otra)}).json()
        assert r1["entry"] != r2["entry"]
        assert r2["uploads"]["rgb"] == 2, "las dos carpetas tienen que sumar, no reemplazarse"
        dest = runs / "m" / "raw" / "rgb_thermal"
        assert len(list(dest.iterdir())) == 2

    def test_migra_sola_el_symlink_de_tope_del_diseno_anterior(self, app):
        """Si raw/<kind> ya es un symlink de tope (diseño anterior, una
        sola carpeta), agregar una carpeta nueva lo migra: la vieja queda
        preservada como una entrada más, no se pierde."""
        c, M, runs, fuente = app
        (fuente / "vieja_V.JPG").write_text("v")
        dest = runs / "m" / "raw" / "rgb_thermal"
        dest.parent.mkdir(parents=True)
        os.symlink(str(fuente), dest)  # diseño viejo, a mano

        nueva = fuente.parent / "nueva"
        nueva.mkdir()
        (nueva / "nueva_V.JPG").write_text("n")
        M.IMPORT_HOST_DIR = str(fuente.parent)
        M.IMPORT_MOUNT = str(fuente.parent)
        r = c.post("/api/missions/m/import-local",
                   data={"kind": "rgb_thermal", "host_path": str(nueva)}).json()

        assert dest.is_dir() and not dest.is_symlink(), "se migró a directorio real"
        assert r["uploads"]["rgb"] == 2, "las fotos viejas no se perdieron en la migración"

    def test_import_remove_saca_una_sola_carpeta(self, app):
        """Sacar UNA carpeta agregada (entre varias) sin tener que vaciar
        todo el lote."""
        c, M, runs, fuente = app
        (fuente / "a_V.JPG").write_text("x")
        otra = fuente.parent / "otra"
        otra.mkdir()
        (otra / "b_V.JPG").write_text("y")
        M.IMPORT_HOST_DIR = str(fuente.parent)
        M.IMPORT_MOUNT = str(fuente.parent)
        r1 = c.post("/api/missions/m/import-local",
                    data={"kind": "rgb_thermal", "host_path": str(fuente)}).json()
        c.post("/api/missions/m/import-local",
               data={"kind": "rgb_thermal", "host_path": str(otra)})

        r = c.post("/api/missions/m/import-remove",
                   data={"kind": "rgb_thermal", "name": r1["entry"]})
        assert r.status_code == 200
        assert r.json()["uploads"]["rgb"] == 1, "solo debería quedar la carpeta que no se sacó"
        assert (otra / "b_V.JPG").exists(), "la carpeta que sigue agregada no se toca"
        assert (fuente / "a_V.JPG").read_text() == "x", "la carpeta sacada tampoco se toca del otro lado"

    def test_clear_uploads_de_un_symlink_no_toca_la_carpeta_original(self, app):
        """clear-uploads ("vaciar", para cuando se eligió la carpeta
        equivocada) tiene que sacar los symlinks — nunca tocar un byte
        de la carpeta real del usuario del otro lado."""
        c, M, runs, fuente = app
        (fuente / "a_V.JPG").write_text("x")
        c.post("/api/missions/m/import-local",
               data={"kind": "rgb_thermal", "host_path": str(fuente)})
        dest = runs / "m" / "raw" / "rgb_thermal"
        assert dest.is_dir()
        r = c.post("/api/missions/m/clear-uploads", data={"kind": "rgb_thermal"})
        assert r.status_code == 200
        assert not dest.exists()
        assert (fuente / "a_V.JPG").read_text() == "x", \
            "la carpeta original del usuario no debería haberse tocado"

    def test_ruta_fuera_del_montaje_se_rechaza(self, app):
        c, M, runs, fuente = app
        r = c.post("/api/missions/m/import-local",
                   data={"kind": "rgb_thermal", "host_path": "/etc"})
        assert r.status_code == 400
        assert "fuera de la carpeta montada" in r.json()["detail"]

    def test_kind_invalido_se_rechaza(self, app):
        c, M, runs, fuente = app
        r = c.post("/api/missions/m/import-local",
                   data={"kind": "lo_que_sea", "host_path": str(fuente)})
        assert r.status_code == 400

    def test_carpeta_inexistente_se_rechaza(self, app):
        c, M, runs, fuente = app
        r = c.post("/api/missions/m/import-local",
                   data={"kind": "rgb_thermal", "host_path": str(fuente / "no_existe")})
        assert r.status_code == 400
        assert "no existe" in r.json()["detail"]


class TestExplorarCarpetas:
    """/api/import/browse — el explorador de carpetas del lado del servidor
    que reemplaza escribir la ruta a mano. Ningún navegador expone la ruta
    real de una carpeta elegida con <input webkitdirectory> ni arrastrada
    desde el escritorio (restricción de seguridad del propio navegador, no
    de esta app) — por eso el picker vive acá, listando lo que ya está
    montado en IMPORT_MOUNT."""

    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        monkeypatch.delenv("EXPORT_HOST_DIR", raising=False)
        raiz = tmp_path / "fotos"
        raiz.mkdir()
        monkeypatch.setenv("IMPORT_HOST_DIR", str(raiz))
        monkeypatch.setenv("IMPORT_MOUNT", str(raiz))
        from fastapi.testclient import TestClient
        from core import scan
        importlib.reload(scan)
        from webapp import main as M
        importlib.reload(M)
        M._state = None
        return TestClient(M.app), M, raiz

    def test_lista_subcarpetas_de_la_raiz(self, app):
        c, M, raiz = app
        (raiz / "vuelo_M3T").mkdir()
        (raiz / "vuelo_M3M").mkdir()
        r = c.get("/api/import/browse")
        assert r.status_code == 200
        j = r.json()
        assert j["parent"] is None, "la raíz montada no tiene 'subir un nivel'"
        nombres = {e["name"] for e in j["entries"]}
        assert nombres == {"vuelo_M3T", "vuelo_M3M"}

    def test_pista_de_contenido_por_subcarpeta(self, app):
        """Cada entrada trae un conteo rápido para distinguir de un vistazo
        cuál es la carpeta RGB/térmico y cuál la multiespectral, sin tener
        que entrar a cada una."""
        c, M, raiz = app
        vuelo = raiz / "vuelo_M3T"
        vuelo.mkdir()
        (vuelo / "a_V.JPG").write_text("x")
        (vuelo / "a_T.JPG").write_text("x")
        r = c.get("/api/import/browse").json()
        entrada = next(e for e in r["entries"] if e["name"] == "vuelo_M3T")
        assert entrada["hint"]["rgb"] == 1

    def test_pista_de_contenido_ve_dentro_de_subcarpetas(self, app):
        """Reportado en vivo: una carpeta real (rgb_mosaico/ + termica/
        adentro, patrón típico de una entrega o SD del dron) mostraba
        "vacía" con un hint que solo miraba el nivel superior — las fotos
        casi nunca están sueltas justo ahí. El hint tiene que ver adentro,
        aunque esté acotado (no recorrer un árbol entero sin límite)."""
        c, M, raiz = app
        vuelo = raiz / "barbosa"
        (vuelo / "rgb_mosaico").mkdir(parents=True)
        (vuelo / "termica").mkdir()
        (vuelo / "rgb_mosaico" / "a_V.JPG").write_text("x")
        (vuelo / "termica" / "a_T.JPG").write_text("x")
        r = c.get("/api/import/browse").json()
        entrada = next(e for e in r["entries"] if e["name"] == "barbosa")
        assert entrada["hint"]["rgb"] == 1, "no debería mostrar 'vacía' con fotos un nivel más abajo"
        assert entrada["hint"]["thermal"] == 1
        assert entrada["hint"]["thermal"] == 1

    def test_navega_a_una_subcarpeta_y_puede_subir_de_nuevo(self, app):
        c, M, raiz = app
        sub = raiz / "vuelo_M3T"
        sub.mkdir()
        (sub / "100MEDIA").mkdir()
        r = c.get("/api/import/browse", params={"path": str(sub)})
        assert r.status_code == 200
        j = r.json()
        assert j["host_path"] == str(sub)
        assert j["parent"] == str(raiz)
        assert [e["name"] for e in j["entries"]] == ["100MEDIA"]

    def test_no_se_puede_salir_de_la_carpeta_montada(self, app):
        c, M, raiz = app
        r = c.get("/api/import/browse", params={"path": "/etc"})
        assert r.status_code == 400

    def test_sin_montaje_devuelve_400(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        monkeypatch.delenv("IMPORT_HOST_DIR", raising=False)
        from fastapi.testclient import TestClient
        from webapp import main as M
        importlib.reload(M)
        M._state = None
        r = TestClient(M.app).get("/api/import/browse")
        assert r.status_code == 400


class TestClasificacionGeneral:
    def test_reconoce_fotos_dji_estandar_y_varios_formatos(self, app):
        c, M, runs, montaje = app
        archivos = [
            "DJI_0001.JPG",
            "DJI_0002.jpeg",
            "IMG_0003.PNG",
            "orto.TIF",
            "DJI_0004_T.JPG",
            "DJI_0005_MS_NIR.TIF",
            "DJI_0006_D.JPG",
            "DJI_0001.SRT",
            "DJI_0001.MRK",
        ]
        counts = M.classify_files(archivos)
        assert counts["rgb"] == 4, f"Esperaba 4 RGB, obtuvo {counts}"
        assert counts["thermal"] == 1
        assert counts["ms"] == 1
        assert counts["dband"] == 1
        assert counts["otros"] == 2
        assert counts["total"] == 9

    def test_validacion_pasa_con_fotos_rgb_estandar(self, app):
        c, M, runs, montaje = app
        uploads = {
            "rgb_thermal": M.classify_files(["DJI_0001.JPG", "DJI_0002.JPG"]),
            "multispectral": M.classify_files([]),
        }
        errs = M._validate("rgb", False, uploads)
        assert errs == [], f"No debería haber errores con fotos RGB estándar: {errs}"

