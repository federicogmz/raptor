"""Tabla de presets (scripts/hardware.py::PRESETS): los tres presets, sus
alias históricos y el orden de costo que promete la estimación."""

import hardware as HW


class TestPresets:
    def test_presets_principales_existen(self):
        assert "tactico" in HW.PRESETS
        assert "cartografico" in HW.PRESETS
        assert "forense" in HW.PRESETS
        assert HW.PRESETS_ORDENADOS == ["tactico", "cartografico", "forense"]

    def test_tactico_es_fast_orthophoto(self):
        p = HW.preset("tactico")
        assert p["sfm_algorithm"] == "incremental"
        assert p["fast_orthophoto"] is True
        assert p["feature_quality"] == "lowest"
        assert p["min_features"] == 3000

    def test_cartografico_y_forense_son_3d(self):
        c = HW.preset("cartografico")
        assert c["sfm_algorithm"] == "incremental"

        f = HW.preset("forense")
        assert f["sfm_algorithm"] == "incremental"

    def test_alias_historicos_mapean_correctamente(self):
        assert HW.preset("vistazo")["nombre"] == "tactico"
        assert HW.preset("rapido")["nombre"] == "tactico"
        assert HW.preset("estandar")["nombre"] == "cartografico"
        assert HW.preset("alta")["nombre"] == "cartografico"
        assert HW.preset("maxima")["nombre"] == "forense"

    def test_estimacion_tactico_es_mas_rapida_que_cartografico(self):
        hw = {"cores": 16, "mem_available_mb": 32000, "gpu": True,
              "gpu_name": "RTX 3080", "vram_mb": 10240}
        t_lo, t_hi = HW.estimate_minutes("tactico", 500, hw)
        c_lo, c_hi = HW.estimate_minutes("cartografico", 500, hw)
        assert t_lo < c_lo, f"tactico ({t_lo}) debe ser más rápido que cartográfico ({c_lo})"
        assert t_hi < c_hi, f"tactico ({t_hi}) debe ser más rápido que cartográfico ({c_hi})"
