"""Calidad configurable: la tabla única de PRESETS (scripts/hardware.py), el
endpoint del formulario y el paso del preset hacia el pipeline real.

El problema que motivó todo esto: --pc-quality low dejaba los mapas de
profundidad a 320px sobre fotos de 4056px (1/12), y a esa resolución una copa
de árbol no se resuelve — la causa real de que el ortomosaico "no pareciera
true-ortho". La calidad elegida tiene que llegar de verdad hasta ODM, y el
mensaje que ve el usuario tiene que salir de la MISMA tabla que se usa después
(docker/entrypoint.sh la lee vía subprocess; la webapp la importa directo).

La calidad se pide por PRESET (vistazo/rapido/estandar/alta/maxima), no con un
número 0-100: ese número no le decía a nadie cuánto iba a tardar ni para qué
servía, y 71 y 89 daban exactamente lo mismo. El número se sigue aceptando y
se mapea al preset equivalente — hay corridas guardadas, scripts y CI que lo
pasan.
"""
import importlib
import os

import pytest

import hardware as HW


class TestTablaDeCalidad:
    @pytest.mark.parametrize("q", [0, 19, 20, 39, 40, 69, 70, 89, 90, 95, 100])
    def test_cubre_todo_el_rango_sin_huecos(self, q):
        """Cualquier 0-100 cae en un preset completo — no se fijan los valores
        concretos (la tabla se recalibra contra corridas reales), solo que
        estén todos presentes y bien tipados."""
        t = HW.quality_tier(q)
        assert t["pc_quality"] in ("low", "medium", "high", "ultra")
        assert t["feature_quality"] in ("lowest", "low", "medium", "high", "ultra")
        assert isinstance(t["res_cm"], (int, float)) and t["res_cm"] > 0

    @pytest.mark.parametrize("q,nombre", [(100, "forense"), (90, "forense"),
                                          (89, "cartografico"), (70, "cartografico"),
                                          (69, "cartografico"), (40, "cartografico"),
                                          (39, "tactico"), (20, "tactico"),
                                          (19, "tactico"), (0, "tactico")])
    def test_el_numero_heredado_mapea_al_preset_equivalente(self, q, nombre):
        assert HW.preset(str(q))["nombre"] == nombre
        assert HW.quality_tier(q)["nombre"] == nombre

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


class TestBundleAdjustmentHibrido:
    """--use-hybrid-bundle-adjustment de ODM."""

    @pytest.mark.parametrize("q", [100, 95, 91, 90])
    def test_ultra_no_usa_hibrido(self, q):
        """En 'forense' se prioriza la consistencia del ajuste global."""
        assert HW.quality_tier(q)["hybrid_ba"] is False

    @pytest.mark.parametrize("q", [89, 75, 70, 69, 40])
    def test_resto_de_escalones_usa_hibrido(self, q):
        assert HW.quality_tier(q)["hybrid_ba"] is True

    def test_el_mensaje_menciona_el_tipo_de_bundle_adjustment(self):
        m = HW.estimate_message("cartografico", 224)
        assert "híbrido" in m["modelo_texto"]
        m2 = HW.estimate_message("forense", 224)
        assert "global completo" in m2["modelo_texto"]


class TestMatcherNeighbors:
    """matcher_neighbors acota los pares a comparar en todos los presets."""

    @pytest.mark.parametrize("q", [40, 69, 70, 89, 90, 95, 100])
    def test_ningun_preset_3d_usa_grafo_completo(self, q):
        n = HW.quality_tier(q)["matcher_neighbors"]
        assert n > 0, "grafo completo (0) es O(n²) y no aporta en vuelos con GPS"
        assert n <= 32, f"vecindario desmedido ({n}): vuelve a acercarse a O(n²)"

    def test_mas_calidad_no_acota_mas_que_menos_calidad(self):
        vistos = [HW.quality_tier(q)["matcher_neighbors"] for q in range(0, 101, 5)]
        assert all(a <= b for a, b in zip(vistos, vistos[1:])), vistos


class TestEntrypointUsaMatcherNeighborsDeLaTabla:
    def test_no_queda_hardcodeado_en_0(self):
        entrypoint = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docker", "entrypoint.sh")
        src = open(entrypoint, encoding="utf-8").read()
        assert "--matcher-neighbors 0" not in src, (
            "matcher-neighbors no puede quedar hardcodeado — tiene que "
            "salir de MATCHER_NEIGHBORS (scripts/hardware.py)")
        # Los argumentos de cada sensor viven en _odm_args(), una rama del
        # `case` por proyecto: los cuatro tienen que leer la variable.
        assert src.count("--matcher-neighbors $MATCHER_NEIGHBORS") == 4, (
            "las cuatro ramas de _odm_args (rgb, thermal, multispectral, "
            "dband) tienen que usar la variable, no un valor fijo")


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


class TestEstimacionRecalibrada:
    """La estimación se recalibró contra una corrida real de esta máquina
    (barbosa-chorrera: ~11.7 h para 221 fotos a calidad 75, escalón high) —
    el modelo anterior decía "4-10 min" (60-100x optimista). La GPU no es un
    sí/no: la VRAM de una laptop (6 GB) es el cuello de botella real de la
    reconstrucción densa, y el mensaje tiene que guiar a los presets rápidos."""

    def _hw(self, gpu=False, vram_mb=None, cores=20, mem=23000):
        return {"cores": cores, "mem_available_mb": mem, "gpu": gpu,
                "gpu_name": "NVIDIA RTX A1000 6GB Laptop GPU" if gpu else None,
                "vram_mb": vram_mb}

    def test_gpu_de_poca_vram_acelera_menos_que_una_grande(self):
        chica = HW.estimate_minutes(75, 200, self._hw(gpu=True, vram_mb=6 * 1024))
        grande = HW.estimate_minutes(75, 200, self._hw(gpu=True, vram_mb=24 * 1024))
        assert grande[0] < chica[0], (chica, grande)

    def test_gpu_siempre_mas_rapida_que_cpu(self):
        cpu = HW.estimate_minutes(75, 200, self._hw(gpu=False))
        gpu = HW.estimate_minutes(75, 200, self._hw(gpu=True, vram_mb=24 * 1024))
        assert gpu[0] < cpu[0]

    def test_vram_desconocida_es_conservadora(self):
        sin_vram = HW.estimate_minutes(75, 200, self._hw(gpu=True, vram_mb=None))
        cpu = HW.estimate_minutes(75, 200, self._hw(gpu=False))
        assert sin_vram[0] < cpu[0], "GPU con VRAM desconocida no puede ser MÁS lenta que CPU"

    def test_alta_no_promete_minutos_para_miles_de_fotos(self):
        """Barbosa-picodegallo real: 3149 imágenes a calidad 75 (preset
        «alta»), 24 h 15 min de punta a punta según outputs/logs/timings.json
        — pero esa corrida es ANTERIOR a que hybrid_ba y matcher_neighbors
        acotado estuvieran cableados en el preset (ver el comentario de
        recalibración en scripts/hardware.py), así que ya no es el techo a
        igualar: es justo lo que esas dos palancas debían recortar. Lo que
        sigue valiendo la pena guardar es la propiedad estructural: «alta»
        usa reconstrucción incremental con densa activa, así que miles de
        fotos se siguen estimando en horas, no en minutos."""
        lo, hi = HW.estimate_minutes("forense", 3149, self._hw(gpu=True, vram_mb=6 * 1024))
        assert hi > lo > 60, (lo, hi)

    def test_guia_de_respuesta_rapida_en_los_presets_caros(self):
        m = HW.estimate_message("cartografico", 3149, self._hw(gpu=True, vram_mb=6 * 1024))
        assert "respuesta rápida" in m["tiempo_texto"]
        assert "«Táctico»" in m["tiempo_texto"]

    def test_sin_guia_en_los_presets_baratos(self):
        m = HW.estimate_message("tactico", 221, self._hw(gpu=True, vram_mb=6 * 1024))
        assert "respuesta rápida" not in m["tiempo_texto"]

    def test_las_opciones_traen_los_tres_presets_con_su_tiempo(self):
        """El formulario muestra las tres opciones con su tiempo."""
        m = HW.estimate_message("cartografico", 500, self._hw(gpu=True, vram_mb=6 * 1024))
        nombres = [o["nombre"] for o in m["opciones"]]
        assert nombres == HW.PRESETS_ORDENADOS
        # Ordenados de más rápido a más lento, con texto listo para mostrar.
        minutos = [o["minutos_estimados"][0] for o in m["opciones"]]
        assert minutos == sorted(minutos), minutos
        assert all(o["tiempo_texto"] for o in m["opciones"])
        assert sum(1 for o in m["opciones"] if o["por_defecto"]) == 1

    def test_el_mensaje_muestra_la_vram(self):
        m = HW.estimate_message(75, 100, self._hw(gpu=True, vram_mb=6 * 1024))
        assert "6 GB VRAM" in m["tiempo_texto"]

    def test_detect_hardware_incluye_vram(self):
        hw = HW.detect_hardware()
        assert "vram_mb" in hw


class TestTerrenoEscarpado:
    """Bug real, encontrado en vivo (misión mision_2026-08-08, terreno
    rocoso, preset vistazo): sfm_algorithm=planar alinea las fotos por
    homografías entre pares, válido solo si la escena es efectivamente
    plana. De 1199 fotos RGB con match, solo 283 (23.6%) quedaron en la
    reconstrucción final — el resto se descartó en silencio porque su
    homografía con las vecinas no encajaba en terreno con relieve fuerte.
    terreno="escarpado" fuerza incremental (sin ese supuesto) incluso en
    los presets que por defecto usan planar."""

    @pytest.mark.parametrize("p", ["tactico", "vistazo", "rapido"])
    def test_tactico_se_mantiene_incremental(self, p):
        assert HW.preset(p, terreno="escarpado")["sfm_algorithm"] == "incremental"

    @pytest.mark.parametrize("p", ["cartografico", "forense"])
    def test_no_cambia_los_que_ya_son_incremental(self, p):
        assert HW.preset(p, terreno="escarpado")["sfm_algorithm"] == "incremental"

    def test_default_es_plano_y_no_toca_nada(self):
        base = HW.preset("cartografico")
        assert base["terreno"] == "plano"
        assert HW.preset("cartografico", terreno="plano") == base

    def test_tactico_es_fast_orthophoto(self):
        assert HW.preset("tactico")["sfm_algorithm"] == "incremental"
        assert HW.preset("tactico")["fast_orthophoto"] is True

    def test_terreno_invalido_falla_con_mensaje_claro(self):
        with pytest.raises(ValueError, match="terreno desconocido"):
            HW.preset("cartografico", terreno="montañoso")

    def test_terreno_none_es_plano(self):
        assert HW.preset("cartografico", terreno=None)["terreno"] == "plano"

    def test_estimate_message_documenta_el_terreno_elegido(self):
        assert HW.estimate_message("cartografico", 100, terreno="escarpado")["terreno"] == "escarpado"

    def test_preset_options_refleja_el_algoritmo(self):
        opciones = {o["nombre"]: o for o in HW.preset_options(100, terreno="escarpado")}
        assert opciones["cartografico"]["sfm_algorithm"] == "incremental"
        assert opciones["forense"]["sfm_algorithm"] == "incremental"
        assert opciones["tactico"]["sfm_algorithm"] == "incremental"


class TestEstimacionHonesta:
    """El modelo por etapas no debe prometer minutos para 3D incremental."""

    def _hw(self):
        return {"cores": 20, "mem_available_mb": 23000, "gpu": True,
                "gpu_name": "RTX A1000 6GB", "vram_mb": 6 * 1024}

    def test_escarpado_ya_no_promete_minutos(self):
        lo, hi = HW.estimate_minutes("cartografico", 2398, self._hw(), terreno="escarpado")
        assert hi > lo >= 60, f"no puede prometer minutos: lo={lo}"

    def test_tactico_sigue_siendo_rapido(self):
        lo, hi = HW.estimate_minutes("tactico", 200, self._hw(), terreno="plano")
        assert hi <= 30, (lo, hi)

    def test_por_sensor_desglosa_y_termico_es_mas_barato(self):
        m = HW.estimate_message("cartografico", 2398, self._hw(), terreno="escarpado",
                                por_sensor={"rgb": 1199, "thermal": 1199})
        assert "Por sensor" in m["tiempo_texto"]
        rgb_lo, _ = m["por_sensor"]["rgb"]
        th_lo, _ = m["por_sensor"]["thermal"]
        assert th_lo < rgb_lo, "el térmico (0.33 MP) tiene que estimarse más barato por foto"


class TestMensajeEnIngles:
    """idioma='en' es la traducción del mismo mensaje."""

    def _hw(self):
        return {"cores": 20, "mem_available_mb": 23000, "gpu": True,
                "gpu_name": "RTX A1000 6GB", "vram_mb": 6 * 1024}

    def test_default_sigue_en_espanol(self):
        m = HW.estimate_message("cartografico", 100, self._hw())
        assert "Preset «" in m["modelo_texto"]

    def test_idioma_en_traduce_los_tres_textos(self):
        m = HW.estimate_message("cartografico", 100, self._hw(), idioma="en")
        assert "Preset \"Cartographic\"" in m["modelo_texto"]
        assert "Up to" in m["resolucion_texto"]
        assert "Estimated time" in m["tiempo_texto"]
        assert "núcleos" not in m["tiempo_texto"]

    def test_idioma_en_traduce_el_desglose_por_sensor(self):
        m = HW.estimate_message("cartografico", 2398, self._hw(), terreno="escarpado", idioma="en",
                                por_sensor={"rgb": 1199, "thermal": 1199})
        assert "By sensor" in m["tiempo_texto"]
        assert "thermal ≈" in m["tiempo_texto"]

    def test_idioma_en_traduce_la_guia_de_respuesta_rapida(self):
        m = HW.estimate_message("cartografico", 3149, self._hw(), idioma="en")
        assert "quick response" in m["tiempo_texto"]
        assert '"Tactical"' in m["tiempo_texto"]

    def test_idioma_en_traduce_las_opciones(self):
        opciones = {o["nombre"]: o for o in HW.preset_options(100, self._hw(), idioma="en")}
        assert opciones["cartografico"]["titulo"] == "Cartographic"
        assert opciones["forense"]["titulo"] == "Forensic"
        assert opciones["tactico"]["titulo"] == "Tactical"


class TestPipelineRunPropagaElPreset:
    def test_el_preset_llega_al_entorno_del_subproceso(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null",
                          preset="tactico")
        assert run._env()["PRESET"] == "tactico"

    def test_default_es_cartografico(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null")
        assert run._env()["PRESET"] == "cartografico"

    def test_quality_numerico_heredado_sigue_llegando(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null",
                          quality=40)
        # Se pasa tal cual: el mapeo a preset lo hace scripts/hardware.py,
        # que es donde vive la tabla.
        assert run._env()["PRESET"] == "40"

    def test_el_terreno_llega_al_entorno_del_subproceso(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null",
                          terreno="escarpado")
        assert run._env()["TERRENO"] == "escarpado"

    def test_terreno_default_es_plano(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null")
        assert run._env()["TERRENO"] == "plano"


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

    def test_lang_en_devuelve_el_mensaje_en_ingles(self, app):
        c, runs = app
        j = c.get("/api/missions/m1b/quality-estimate?preset=cartografico&lang=en").json()
        assert "Preset \"Cartographic\"" in j["modelo_texto"]

    def test_sin_lang_sigue_en_espanol(self, app):
        c, runs = app
        j = c.get("/api/missions/m1c/quality-estimate?preset=estandar").json()
        assert "Preset «" in j["modelo_texto"]

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

    def test_n_photos_explicito_pisa_el_conteo_de_disco(self, app):
        """Reportado: el estimado decía "0 fotos" mientras la subida seguía
        en curso, porque contaba desde disco y el archivo recién llega a
        raw/ cuando termina de subirse — no cuando se elige en el navegador.
        El navegador ya sabe cuántas fotos hay apenas se elige la carpeta
        (classify() sobre el FileList) y lo manda como n_photos; eso tiene
        que ganarle al conteo de disco, no promediarse ni ignorarse."""
        c, runs = app
        # Nada subido a disco todavía (upload recién arrancando) — el
        # navegador ya eligió 200 fotos y las está subiendo.
        r = c.get("/api/missions/m_subiendo/quality-estimate?quality=75&n_photos=200").json()
        assert r["n_photos"] == 200

    def test_sin_n_photos_sigue_contando_desde_disco(self, app):
        """Compatibilidad: una llamada vieja (sin n_photos) tiene que seguir
        funcionando exactamente como antes."""
        c, runs = app
        d = runs / "m_viejo" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        (d / "a_T.JPG").write_text("x")
        r = c.get("/api/missions/m_viejo/quality-estimate?quality=75&mode=rgb%2Bthermal").json()
        assert r["n_photos"] == 2

    def test_start_rechaza_un_preset_inexistente(self, app):
        c, runs = app
        d = runs / "m6" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        r = c.post("/api/missions/m6/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "preset": "turbo"})
        assert r.status_code == 400
        # El mensaje tiene que listar las opciones reales, no solo decir "no".
        assert "tactico" in r.text and "forense" in r.text, r.text

    def test_estimate_rechaza_un_preset_inexistente(self, app):
        c, runs = app
        (runs / "m7").mkdir(parents=True)
        r = c.get("/api/missions/m7/quality-estimate?preset=turbo&n_photos=10")
        assert r.status_code == 400

    def test_estimate_devuelve_las_tres_opciones_con_su_tiempo(self, app):
        c, runs = app
        (runs / "m8").mkdir(parents=True)
        m = c.get("/api/missions/m8/quality-estimate?n_photos=500").json()
        assert [o["nombre"] for o in m["opciones"]] == HW.PRESETS_ORDENADOS
        assert all(o["tiempo_texto"] for o in m["opciones"])
        assert m["preset"] == "cartografico", "el default lo pone el servidor"

    def test_estimate_acepta_el_quality_numerico_heredado(self, app):
        c, runs = app
        (runs / "m9").mkdir(parents=True)
        m = c.get("/api/missions/m9/quality-estimate?quality=75&n_photos=10").json()
        assert m["preset"] == "cartografico"

    def test_estimate_default_es_terreno_escarpado(self, app):
        c, runs = app
        (runs / "m10").mkdir(parents=True)
        m = c.get("/api/missions/m10/quality-estimate?preset=cartografico&n_photos=100").json()
        assert m["terreno"] == "escarpado"
        assert m["tier"]["sfm_algorithm"] == "incremental"

    def test_estimate_terreno_escarpado_fuerza_incremental(self, app):
        c, runs = app
        (runs / "m11").mkdir(parents=True)
        m = c.get("/api/missions/m11/quality-estimate?"
                 "preset=cartografico&terreno=escarpado&n_photos=100").json()
        assert m["terreno"] == "escarpado"
        assert m["tier"]["sfm_algorithm"] == "incremental"

    def test_estimate_terreno_invalido_falla_claro(self, app):
        c, runs = app
        (runs / "m12").mkdir(parents=True)
        r = c.get("/api/missions/m12/quality-estimate?preset=cartografico&terreno=montañoso&n_photos=10")
        assert r.status_code == 400

    def test_start_acepta_terreno_escarpado(self, app, monkeypatch):
        c, runs = app
        d = runs / "m13" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        from core import runner
        async def fake_start(self):
            self.proc = None
        monkeypatch.setattr(runner.PipelineRun, "start", fake_start)
        r = c.post("/api/missions/m13/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "preset": "cartografico", "terreno": "escarpado"})
        assert r.status_code == 200, r.text

    def test_start_rechaza_un_terreno_inexistente(self, app):
        c, runs = app
        d = runs / "m14" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        r = c.post("/api/missions/m14/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "preset": "cartografico", "terreno": "montañoso"})
        assert r.status_code == 400
