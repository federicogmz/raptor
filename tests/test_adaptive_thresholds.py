"""Umbrales adaptativos de trim_low_overlap_edges.py.

Son el corazón calibrado del recorte: deciden cuánto ortomosaico sobrevive.
Cada test nombra el comportamiento documentado que protege.
"""
import numpy as np

from trim_low_overlap_edges import (_adaptive_ceiling, _adaptive_floor,
                                    _adaptive_floor_joint)


class TestAdaptiveFloor:
    def test_no_toca_el_piso_calibrado_si_la_mision_lo_soporta(self):
        # 90% de la población por encima de 8 y se pide conservar 85%
        vals = np.array([10] * 90 + [2] * 10, float)
        assert _adaptive_floor(vals, 8, min_keep_frac=0.85) == 8

    def test_relaja_cuando_el_vuelo_tiene_menos_solape_que_la_calibracion(self):
        # Un piso calibrado para un vuelo de solape denso, aplicado a uno con
        # menos solape real, descartaría casi todo.
        vals = np.array([3] * 100, float)
        out = _adaptive_floor(vals, 20, min_keep_frac=0.85, fallback_percentile=15)
        assert out < 20, "un piso que deja pasar 0% tiene que relajarse"
        assert out >= 1

    def test_nunca_sube_el_piso(self):
        vals = np.array([50] * 100, float)
        assert _adaptive_floor(vals, 8) == 8

    def test_el_cero_es_centinela_de_fuera_de_huella_por_defecto(self):
        # exclude_zero=True: los 0 son "fuera del footprint", no una medición.
        # Con ellos excluidos la población real (todos 10) supera el piso.
        vals = np.array([0] * 80 + [10] * 20, float)
        assert _adaptive_floor(vals, 8, exclude_zero=True) == 8

    def test_con_exclude_zero_false_el_filtro_puede_desactivarse(self):
        # Con una señal de mediana 0, forzar el piso a 1 descartaría a la
        # mayoría de la población. Con 0 como medición legítima el piso puede
        # bajar hasta 0 = señal desactivada.
        vals = np.array([0] * 80 + [5] * 20, float)
        out = _adaptive_floor(vals, 4, min_keep_frac=0.85, exclude_zero=False)
        assert out == 0.0, "una señal sin poder discriminante debe desactivarse, no forzarse a 1"

    def test_poblacion_vacia_devuelve_lo_pedido(self):
        assert _adaptive_floor(np.array([]), 8) == 8
        # todo ceros + exclude_zero -> población vacía tras filtrar
        assert _adaptive_floor(np.zeros(50), 8, exclude_zero=True) == 8

    def test_respeta_la_mascara_de_poblacion(self):
        vals = np.array([100] * 50 + [1] * 50, float)
        mask = np.array([False] * 50 + [True] * 50)
        # Mirando SOLO la mitad mala, el piso 8 no se sostiene
        assert _adaptive_floor(vals, 8, population_mask=mask) < 8
        # Mirando la mitad buena, sí
        assert _adaptive_floor(vals, 8, population_mask=~mask) == 8


class TestAdaptiveFloorJoint:
    def test_detecta_el_colapso_de_la_interseccion(self):
        """El caso que justifica la función: cada piso por separado parece
        dejar pasar suficiente, pero la intersección de los dos colapsa la
        cobertura."""
        n = 1000
        rng = np.random.default_rng(0)
        # Anticorrelacionadas: donde una es alta la otra es baja
        a = rng.permutation(np.linspace(0, 40, n))
        b = 40 - a
        req_a, req_b = 20.0, 20.0
        assert (a >= req_a).mean() > 0.4, "cada piso por separado parece razonable"
        assert (b >= req_b).mean() > 0.4
        assert ((a >= req_a) & (b >= req_b)).mean() < 0.1, "pero juntos colapsan"

        out = _adaptive_floor_joint([(a, req_a), (b, req_b)], min_keep_frac=0.5)
        assert all(o < req_a for o in out), "ambos pisos tienen que relajarse"
        kept = ((a >= out[0]) & (b >= out[1])).mean()
        assert kept >= 0.5 - 1e-6, f"la intersección relajada debe alcanzar min_keep_frac (dio {kept})"

    def test_conserva_la_relacion_entre_pisos(self):
        """Se relajan TODOS con el mismo factor de escala, en vez de
        privilegiar uno — es lo que dice hacer el docstring."""
        n = 500
        a = np.linspace(0, 10, n)
        b = np.linspace(0, 20, n)
        out = _adaptive_floor_joint([(a, 8.0), (b, 16.0)], min_keep_frac=0.9)
        assert out[1] > out[0]
        # 16/8 = 2 -> la proporción se mantiene tras el escalado conjunto
        assert abs(out[1] / out[0] - 2.0) < 0.05

    def test_no_toca_nada_si_ya_alcanza(self):
        a = np.array([30.0] * 100)
        b = np.array([30.0] * 100)
        assert _adaptive_floor_joint([(a, 8.0), (b, 4.0)], min_keep_frac=0.85) == [8.0, 4.0]

    def test_desactiva_la_senal_saturada_de_ceros(self):
        """Una señal que ni con piso 1 deja pasar min_keep_frac se DESACTIVA
        (piso 0), en vez de forzarla a 1 y excluir a esa mayoría."""
        buena = np.array([30.0] * 100)
        ceros = np.array([0.0] * 90 + [30.0] * 10)   # 90% en cero
        out = _adaptive_floor_joint([(buena, 8.0), (ceros, 4.0)], min_keep_frac=0.85)
        assert out[1] == 0.0, "la señal sin poder discriminante debe quedar desactivada"
        assert out[0] > 0.0, "la que sí discrimina se conserva"


class TestAdaptiveCeiling:
    def test_no_toca_el_umbral_si_descarta_poco(self):
        vals = np.array([0.1] * 95 + [0.9] * 5)
        assert _adaptive_ceiling(vals, 0.5, max_remove_frac=0.15) == 0.5

    def test_sube_el_umbral_si_descartaria_demasiado(self):
        vals = np.array([0.9] * 100)
        out = _adaptive_ceiling(vals, 0.5, max_remove_frac=0.15, fallback_percentile=90)
        assert out > 0.5

    def test_nunca_baja_el_umbral(self):
        """Simétrico de _adaptive_floor: este solo sube."""
        vals = np.array([0.01] * 100)
        assert _adaptive_ceiling(vals, 0.5) == 0.5
