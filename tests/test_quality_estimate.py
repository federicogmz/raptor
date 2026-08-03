"""Calidad configurable: la tabla única (scripts/hardware.py), el endpoint del
formulario y el paso de QUALITY hacia el pipeline real.

El problema que motivó todo esto: --pc-quality low dejaba los mapas de
profundidad a 320px sobre fotos de 4056px (1/12), y a esa resolución una copa
de árbol no se resuelve — la causa real de que el ortomosaico "no pareciera
true-ortho". El slider de calidad tiene que llegar de verdad hasta ODM, y el
mensaje que ve el usuario tiene que salir de la MISMA tabla que se usa después
(docker/entrypoint.sh la lee vía subprocess; la webapp la importa directo).
"""
import importlib
import os

import pytest

import hardware as HW


class TestTablaDeCalidad:
    @pytest.mark.parametrize("q,pc,feat,res", [
        (100, "ultra", "ultra", 1),
        (95, "ultra", "ultra", 1),
        (90, "ultra", "ultra", 1),
        (89, "high", "high", 2),
        (70, "high", "high", 2),
        (69, "medium", "high", 4),
        (40, "medium", "high", 4),
        (39, "low", "medium", 8),
        (20, "low", "medium", 8),
        (19, "lowest", "medium", 15),
        (0, "lowest", "medium", 15),
    ])
    def test_cubre_todo_el_rango_sin_huecos(self, q, pc, feat, res):
        t = HW.quality_tier(q)
        assert (t["pc_quality"], t["feature_quality"], t["res_cm"]) == (pc, feat, res)

    def test_a_mas_calidad_nunca_pide_una_resolucion_mas_gruesa(self):
        """Subir la calidad no puede empeorar el techo de resolución pedido."""
        vistos = [HW.quality_tier(q)["res_cm"] for q in range(0, 101, 5)]
        assert all(a >= b for a, b in zip(vistos, vistos[1:])), vistos

    def test_a_mas_calidad_nunca_baja_el_tiempo_relativo(self):
        vistos = [HW.quality_tier(q)["tiempo_relativo"] for q in range(0, 101, 5)]
        assert all(a <= b for a, b in zip(vistos, vistos[1:])), vistos

    def test_fuera_de_rango_se_recorta_no_revienta(self):
        assert HW.quality_tier(150) == HW.quality_tier(100)
        assert HW.quality_tier(-10) == HW.quality_tier(0)


class TestConcurrencia:
    def test_override_manual_gana_siempre(self):
        assert HW.safe_concurrency(12.3, override=7) == 7

    def test_nunca_supera_los_nucleos(self):
        n = HW.safe_concurrency(0.01)  # megapíxeles ínfimos -> RAM no limita
        assert n <= HW.cpu_count()

    def test_nunca_baja_de_uno(self):
        assert HW.safe_concurrency(10_000) >= 1

    def test_mas_megapixeles_pide_menos_hilos(self):
        """Fotos más grandes -> más memoria por hilo -> menos hilos seguros."""
        chico = HW.safe_concurrency(0.33)   # sensor térmico
        grande = HW.safe_concurrency(12.3)  # RGB
        assert grande <= chico


class TestEstimacion:
    def test_mas_fotos_da_mas_tiempo(self):
        hw = {"cores": 8, "mem_available_mb": 16000, "gpu": False, "gpu_name": None}
        lo1, hi1 = HW.estimate_minutes(75, 100, hw)
        lo2, hi2 = HW.estimate_minutes(75, 1000, hw)
        assert lo2 > lo1 and hi2 > hi1

    def test_mas_calidad_da_mas_tiempo(self):
        hw = {"cores": 8, "mem_available_mb": 16000, "gpu": False, "gpu_name": None}
        _, hi_baja = HW.estimate_minutes(0, 500, hw)
        _, hi_alta = HW.estimate_minutes(100, 500, hw)
        assert hi_alta > hi_baja

    def test_mas_nucleos_da_menos_tiempo(self):
        base = {"mem_available_mb": 64000, "gpu": False, "gpu_name": None}
        _, hi_pocos = HW.estimate_minutes(75, 1000, {**base, "cores": 2})
        _, hi_muchos = HW.estimate_minutes(75, 1000, {**base, "cores": 32})
        assert hi_muchos < hi_pocos, "más núcleos en mejor hardware tiene que correr más rápido"

    def test_cero_fotos_no_revienta(self):
        assert HW.estimate_minutes(75, 0) == (0.0, 0.0)

    def test_el_mensaje_no_promete_mas_finura_que_el_gsd(self):
        m = HW.estimate_message(100, 500)
        assert "nunca más fino" in m["resolucion_texto"]

    def test_el_mensaje_incluye_el_hardware_detectado(self):
        m = HW.estimate_message(75, 500)
        assert str(m["hardware"]["cores"]) in m["tiempo_texto"]


class TestPipelineRunPropagaQuality:
    def test_quality_llega_al_entorno_del_subproceso(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null",
                          quality=40)
        assert run._env()["QUALITY"] == "40"

    def test_default_es_75(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null")
        assert run._env()["QUALITY"] == "75"


class TestEndpointDeLaWebapp:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        monkeypatch.delenv("EXPORT_HOST_DIR", raising=False)
        from fastapi.testclient import TestClient
        from core import scan
        importlib.reload(scan)
        from webapp import main as M
        importlib.reload(M)
        M._state = None
        return TestClient(M.app), tmp_path / "runs"

    def test_devuelve_estimacion_para_una_mision_sin_subidas(self, app):
        c, runs = app
        r = c.get("/api/missions/m1/quality-estimate?quality=75")
        assert r.status_code == 200
        j = r.json()
        assert j["n_photos"] == 0
        assert "resolucion_texto" in j and "tiempo_texto" in j

    def test_cuenta_las_fotos_ya_subidas(self, app):
        c, runs = app
        d = runs / "m2" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        for i in range(3):
            (d / f"DJI_{i:04d}_V.JPG").write_text("x")
            (d / f"DJI_{i:04d}_T.JPG").write_text("x")
        r = c.get("/api/missions/m2/quality-estimate?quality=75&mode=rgb%2Bthermal").json()
        assert r["n_photos"] == 6

    def test_modo_rgb_no_cuenta_termicas(self, app):
        c, runs = app
        d = runs / "m3" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        (d / "a_T.JPG").write_text("x")
        r = c.get("/api/missions/m3/quality-estimate?quality=75&mode=rgb").json()
        assert r["n_photos"] == 1

    def test_cuenta_bandas_multiespectrales_sin_multiplicar_de_mas(self, app):
        c, runs = app
        d = runs / "m4" / "raw" / "multispectral"
        d.mkdir(parents=True)
        for b in ("G", "R", "RE", "NIR"):
            (d / f"a_MS_{b}.TIF").write_text("x")
        r = c.get("/api/missions/m4/quality-estimate?quality=75&mode=none"
                 "&has_multispectral=true").json()
        assert r["n_photos"] == 4

    def test_quality_fuera_de_rango_se_recorta_no_falla(self, app):
        c, runs = app
        assert c.get("/api/missions/m5/quality-estimate?quality=500").status_code == 200

    def test_start_rechaza_quality_invalida(self, app):
        c, runs = app
        d = runs / "m6" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        r = c.post("/api/missions/m6/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "quality": "500"})
        assert r.status_code == 400
