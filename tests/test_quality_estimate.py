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
    @pytest.mark.parametrize("q,pc,feat,res", [
        (100, "ultra", "ultra", 1),
        (95, "ultra", "ultra", 1),
        (90, "ultra", "ultra", 1),
        (89, "high", "high", 2),
        (70, "high", "high", 2),
        (69, "medium", "high", 4),
        (40, "medium", "high", 4),
        (39, "medium", "medium", 8),
        (20, "medium", "medium", 8),
        (19, "low", "medium", 15),
        (0, "low", "medium", 15),
    ])
    def test_cubre_todo_el_rango_sin_huecos(self, q, pc, feat, res):
        t = HW.quality_tier(q)
        assert (t["pc_quality"], t["feature_quality"], t["res_cm"]) == (pc, feat, res)

    @pytest.mark.parametrize("q,nombre", [(100, "maxima"), (90, "maxima"),
                                          (89, "alta"), (70, "alta"),
                                          (69, "estandar"), (40, "estandar"),
                                          (39, "rapido"), (20, "rapido"),
                                          (19, "vistazo"), (0, "vistazo")])
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
    """--use-hybrid-bundle-adjustment de ODM: reportado en vivo — una
    reconstrucción de 224 fotos quedó ~40 min en bundle adjustment con la
    CPU casi ociosa (2 núcleos, nada para paralelizar: es secuencial por
    diseño). Sin esto ODM hace bundle adjustment GLOBAL completo en CADA
    foto agregada, que se pone más caro a medida que crece la
    reconstrucción — con esto, local por foto y global cada 100."""

    @pytest.mark.parametrize("q", [100, 95, 91, 90])
    def test_ultra_no_usa_hibrido(self, q):
        """En 'ultra' se prioriza la consistencia del ajuste global — el
        usuario ya eligió pagar 64x el tiempo base por el máximo detalle."""
        assert HW.quality_tier(q)["hybrid_ba"] is False

    @pytest.mark.parametrize("q", [89, 75, 70, 69, 40, 39, 20, 19, 0])
    def test_resto_de_escalones_usa_hibrido(self, q):
        assert HW.quality_tier(q)["hybrid_ba"] is True

    def test_el_mensaje_menciona_el_tipo_de_bundle_adjustment(self):
        m = HW.estimate_message(75, 224)
        assert "híbrido" in m["modelo_texto"]
        m2 = HW.estimate_message(95, 224)
        assert "global completo" in m2["modelo_texto"]


class TestMatcherNeighbors:
    """Bug real, reportado en vivo: docker/entrypoint.sh tenía
    `--matcher-neighbors 0` (grafo completo: cada foto contra TODAS las
    demás) hardcodeado en las cuatro llamadas a run_odm, SIN importar la
    calidad elegida — "calidad mínima" seguía pagando el matching más caro
    posible. Confirmado en vivo: una banda D de 584 fotos tardó ~5h en
    bundle adjustment con QUALITY=0. En ultra/high se mantiene el grafo
    completo (importa no perderse pares que podrían cerrar un loop); en
    medium/low/lowest se acota a los 8 vecinos más cercanos."""

    @pytest.mark.parametrize("q", [100, 95, 91, 90, 89, 75, 70])
    def test_ultra_y_high_usan_grafo_completo(self, q):
        assert HW.quality_tier(q)["matcher_neighbors"] == 0

    @pytest.mark.parametrize("q", [69, 40, 39, 20, 19, 0])
    def test_medium_low_lowest_acotan_a_8_vecinos(self, q):
        assert HW.quality_tier(q)["matcher_neighbors"] == 8


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

    def test_el_rango_cubre_la_corrida_real(self):
        """Barbosa-picodegallo real: 3149 imágenes a calidad 75 (preset
        «alta»), 24 h 15 min de punta a punta según
        outputs/logs/timings.json. Es la corrida contra la que está
        calibrado _SEG_POR_FOTO_BASE, así que la estimación TIENE que
        contener ese número."""
        lo, hi = HW.estimate_minutes("alta", 3149, self._hw(gpu=True, vram_mb=6 * 1024))
        assert lo <= 24.25 * 60 <= hi, (lo, hi)

    def test_guia_de_respuesta_rapida_en_los_presets_caros(self):
        m = HW.estimate_message("alta", 3149, self._hw(gpu=True, vram_mb=6 * 1024))
        assert "respuesta rápida" in m["tiempo_texto"]
        assert "«Rápido»" in m["tiempo_texto"]
        assert "«Vistazo»" in m["tiempo_texto"]

    def test_sin_guia_en_los_presets_baratos(self):
        m = HW.estimate_message("estandar", 221, self._hw(gpu=True, vram_mb=6 * 1024))
        assert "respuesta rápida" not in m["tiempo_texto"]

    def test_las_opciones_traen_los_cinco_presets_con_su_tiempo(self):
        """Lo que hace que la elección sea informada: el formulario muestra
        los cinco con su número al lado, calculado para ESTAS fotos."""
        m = HW.estimate_message("estandar", 500, self._hw(gpu=True, vram_mb=6 * 1024))
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

    @pytest.mark.parametrize("p", ["vistazo", "rapido"])
    def test_fuerza_incremental_en_los_planares(self, p):
        assert HW.preset(p, terreno="escarpado")["sfm_algorithm"] == "incremental"

    @pytest.mark.parametrize("p", ["estandar", "alta", "maxima"])
    def test_no_cambia_los_que_ya_son_incremental(self, p):
        assert HW.preset(p, terreno="escarpado")["sfm_algorithm"] == "incremental"

    def test_default_es_plano_y_no_toca_nada(self):
        base = HW.preset("vistazo")
        assert base["sfm_algorithm"] == "planar"
        assert base["terreno"] == "plano"
        assert HW.preset("vistazo", terreno="plano") == base

    def test_no_afecta_fast_orthophoto(self):
        """Cobertura (terreno) y velocidad de MVS (fast_orthophoto) son
        palancas independientes — una no debe apagar la otra."""
        assert HW.preset("vistazo", terreno="escarpado")["fast_orthophoto"] is True

    def test_el_tiempo_estimado_sube_al_forzar_incremental(self):
        """La estimación no puede seguir prometiendo la velocidad de
        planar en un modo que ya no lo usa."""
        plano = HW.preset("vistazo", terreno="plano")["tiempo_relativo"]
        escarpado = HW.preset("vistazo", terreno="escarpado")["tiempo_relativo"]
        assert escarpado > plano

    def test_terreno_invalido_falla_con_mensaje_claro(self):
        with pytest.raises(ValueError, match="terreno desconocido"):
            HW.preset("vistazo", terreno="montañoso")

    def test_terreno_none_es_plano(self):
        assert HW.preset("vistazo", terreno=None)["terreno"] == "plano"

    def test_estimate_message_documenta_el_terreno_elegido(self):
        assert HW.estimate_message("vistazo", 100, terreno="escarpado")["terreno"] == "escarpado"

    def test_preset_options_refleja_el_algoritmo_forzado(self):
        opciones = {o["nombre"]: o for o in HW.preset_options(100, terreno="escarpado")}
        assert opciones["vistazo"]["sfm_algorithm"] == "incremental"
        assert opciones["estandar"]["sfm_algorithm"] == "incremental"


class TestEstimacionHonesta:
    """El modelo recalibrado (por etapas, ver el comentario en
    scripts/hardware.py) tiene que cubrir las corridas reales — el modelo
    anterior prometía "31 min – 2.1 h" para la misión que terminó en
    17.7 h, y esa promesa rota era la fuente de la insatisfacción."""

    def _hw(self):
        return {"cores": 20, "mem_available_mb": 23000, "gpu": True,
                "gpu_name": "RTX A1000 6GB", "vram_mb": 6 * 1024}

    def test_escarpado_vistazo_cubre_la_corrida_real(self):
        """mision_2026-08-08: 2398 fotos (1199 RGB + 1199 térmicas) en
        vistazo+escarpado = 17 h 42 min reales (outputs/logs/timings.json).
        El rango estimado TIENE que contener ese número."""
        lo, hi = HW.estimate_minutes("vistazo", 2398, self._hw(), terreno="escarpado")
        assert lo <= 17.7 * 60 <= hi, (lo, hi)

    def test_escarpado_ya_no_promete_minutos(self):
        lo, _ = HW.estimate_minutes("vistazo", 2398, self._hw(), terreno="escarpado")
        assert lo >= 120, f"no puede prometer minutos: lo={lo}"

    def test_planar_sigue_siendo_rapido(self):
        lo, hi = HW.estimate_minutes("vistazo", 2398, self._hw(), terreno="plano")
        assert hi <= 4 * 60, (lo, hi)

    def test_por_sensor_desglosa_y_termico_es_mas_barato(self):
        m = HW.estimate_message("vistazo", 2398, self._hw(), terreno="escarpado",
                                por_sensor={"rgb": 1199, "thermal": 1199})
        assert "Por sensor" in m["tiempo_texto"]
        rgb_lo, _ = m["por_sensor"]["rgb"]
        th_lo, _ = m["por_sensor"]["thermal"]
        assert th_lo < rgb_lo, "el térmico (0.33 MP) tiene que estimarse más barato por foto"

    def test_aviso_escarpado_solo_cuando_se_fuerza(self):
        m = HW.estimate_message("vistazo", 100, self._hw(), terreno="escarpado")
        assert m["aviso_escarpado"] is True
        assert "secuencial" in m["modelo_texto"]
        m2 = HW.estimate_message("vistazo", 100, self._hw(), terreno="plano")
        assert m2["aviso_escarpado"] is False
        # En estandar (ya incremental por diseño) no se "fuerza" nada.
        m3 = HW.estimate_message("estandar", 100, self._hw(), terreno="escarpado")
        assert m3["aviso_escarpado"] is False


class TestPipelineRunPropagaElPreset:
    def test_el_preset_llega_al_entorno_del_subproceso(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null",
                          preset="rapido")
        assert run._env()["PRESET"] == "rapido"

    def test_default_es_estandar(self):
        from core.runner import PipelineRun
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null")
        assert run._env()["PRESET"] == "estandar"

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
        assert "vistazo" in r.text and "maxima" in r.text, r.text

    def test_estimate_rechaza_un_preset_inexistente(self, app):
        c, runs = app
        (runs / "m7").mkdir(parents=True)
        r = c.get("/api/missions/m7/quality-estimate?preset=turbo&n_photos=10")
        assert r.status_code == 400

    def test_estimate_devuelve_las_cinco_opciones_con_su_tiempo(self, app):
        c, runs = app
        (runs / "m8").mkdir(parents=True)
        m = c.get("/api/missions/m8/quality-estimate?n_photos=500").json()
        assert [o["nombre"] for o in m["opciones"]] == HW.PRESETS_ORDENADOS
        assert all(o["tiempo_texto"] for o in m["opciones"])
        assert m["preset"] == "estandar", "el default lo pone el servidor"

    def test_estimate_acepta_el_quality_numerico_heredado(self, app):
        c, runs = app
        (runs / "m9").mkdir(parents=True)
        m = c.get("/api/missions/m9/quality-estimate?quality=75&n_photos=10").json()
        assert m["preset"] == "alta"

    def test_estimate_default_es_terreno_escarpado(self, app):
        """Default de ESTE endpoint (webapp/main.py) — no confundir con
        HW.preset(terreno=None), que sigue en "plano" (ver
        test_terreno_none_es_plano más arriba): la mayoría de las misiones
        de emergencia real no son planas, y "plano" por defecto venía
        perdiendo cobertura en silencio salvo que el operador supiera tildar
        "escarpado" a mano — ver el comentario del formulario en
        webapp/static/index.html."""
        c, runs = app
        (runs / "m10").mkdir(parents=True)
        m = c.get("/api/missions/m10/quality-estimate?preset=vistazo&n_photos=100").json()
        assert m["terreno"] == "escarpado"
        assert m["tier"]["sfm_algorithm"] == "incremental"

    def test_estimate_terreno_escarpado_fuerza_incremental(self, app):
        c, runs = app
        (runs / "m11").mkdir(parents=True)
        m = c.get("/api/missions/m11/quality-estimate?"
                 "preset=vistazo&terreno=escarpado&n_photos=100").json()
        assert m["terreno"] == "escarpado"
        assert m["tier"]["sfm_algorithm"] == "incremental"

    def test_estimate_terreno_invalido_falla_claro(self, app):
        c, runs = app
        (runs / "m12").mkdir(parents=True)
        r = c.get("/api/missions/m12/quality-estimate?preset=vistazo&terreno=montañoso&n_photos=10")
        assert r.status_code == 400

    def test_start_acepta_terreno_escarpado(self, app):
        c, runs = app
        d = runs / "m13" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        r = c.post("/api/missions/m13/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "preset": "vistazo", "terreno": "escarpado"})
        assert r.status_code == 200, r.text

    def test_start_rechaza_un_terreno_inexistente(self, app):
        c, runs = app
        d = runs / "m14" / "raw" / "rgb_thermal"
        d.mkdir(parents=True)
        (d / "a_V.JPG").write_text("x")
        r = c.post("/api/missions/m14/start", data={
            "mode": "rgb", "has_multispectral": "false", "reuse_odm": "false",
            "preset": "vistazo", "terreno": "montañoso"})
        assert r.status_code == 400
