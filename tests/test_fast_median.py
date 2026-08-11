"""scripts/fast_median.py: mediana rápida para las pasadas de outliers del DSM
(dsm_clean.py). Acelera repartiendo el array en bloques de filas entre hilos
(scipy.ndimage.median_filter libera el GIL), no cambiando de algoritmo — así
que el resultado tiene que ser BIT A BIT el de
scipy.ndimage.median_filter(mode='nearest'), que es lo que reemplaza.

Cuidado con el camino que ejercita cada test: por debajo de
MIN_PX_PARA_PARALELIZAR la función delega directo en scipy y nunca toca el
código de bloques. Los arrays chicos de las pruebas de abajo no alcanzan para
cubrir el reparto por hilos — de ahí TestCaminoParalelo, que baja el umbral.

Mantener siempre alguna prueba con ventana 9 sobre float32: esa combinación
es la que usa dsm_clean.py de verdad, y es exactamente la que reventaba con
la implementación anterior (cv2.medianBlur exige 8 bits para ventanas
mayores a 5×5).
"""
import numpy as np
import pytest
from scipy import ndimage

import fast_median
from fast_median import median_filter_fast


class TestEquivalenciaConScipy:
    @pytest.mark.parametrize("shape", [(17, 23), (64, 64), (129, 97), (300, 400)])
    def test_mismo_resultado_que_scipy(self, shape):
        rng = np.random.default_rng(7)
        a = rng.random(shape).astype(np.float32) * 300
        mine = median_filter_fast(a, 9)
        theirs = ndimage.median_filter(a, size=9, mode="nearest")
        assert mine.shape == a.shape
        assert np.array_equal(mine, theirs)

    def test_tamano_de_ventana_distinto(self):
        rng = np.random.default_rng(3)
        a = rng.random((50, 60)).astype(np.float32)
        for k in (3, 5, 9):
            assert np.array_equal(median_filter_fast(a, k),
                                  ndimage.median_filter(a, size=k, mode="nearest"))

    def test_imagen_constante(self):
        a = np.full((40, 40), 123.0, dtype=np.float32)
        assert np.array_equal(median_filter_fast(a, 9), a)

    def test_entrada_no_float32_se_castea(self):
        rng = np.random.default_rng(5)
        a = rng.random((30, 30))  # float64
        out = median_filter_fast(a, 9)
        assert out.dtype == np.float32

    def test_entrada_no_contigua(self):
        rng = np.random.default_rng(9)
        big = rng.random((60, 60)).astype(np.float32)
        a = big[::2, ::2]  # vista no contigua
        assert np.array_equal(median_filter_fast(a, 9),
                              ndimage.median_filter(np.ascontiguousarray(a),
                                                    size=9, mode="nearest"))

    def test_array_chico(self):
        a = np.arange(6, dtype=np.float32).reshape(2, 3)
        out = median_filter_fast(a, 3)
        assert out.shape == a.shape


class TestCaminoParalelo:
    """El reparto en bloques con halo es donde puede aparecer una costura.

    Se baja el umbral en vez de usar un array de verdad grande: el punto es
    ejercitar el código de bloques, y un DSM real de 65 Mpx en un test
    tardaría minutos sin comprobar nada más.
    """

    @pytest.fixture
    def paralelo_siempre(self, monkeypatch):
        monkeypatch.setattr(fast_median, "MIN_PX_PARA_PARALELIZAR", 1)

    @pytest.mark.parametrize("shape", [(200, 137), (61, 400), (129, 97)])
    def test_identico_a_scipy_repartido_en_bloques(self, paralelo_siempre, shape):
        rng = np.random.default_rng(11)
        # Offset de elevación real (~2000 m): con float32 es donde una cuenta
        # numéricamente descuidada se notaría.
        a = rng.random(shape).astype(np.float32) * 300 + 2000.0
        assert np.array_equal(median_filter_fast(a, 9),
                              ndimage.median_filter(a, size=9, mode="nearest"))

    def test_mas_hilos_que_filas_no_rompe(self, paralelo_siempre, monkeypatch):
        monkeypatch.setattr(fast_median, "_hilos", lambda: 64)
        a = np.random.default_rng(1).random((5, 40)).astype(np.float32)
        assert np.array_equal(median_filter_fast(a, 9),
                              ndimage.median_filter(a, size=9, mode="nearest"))

    def test_respeta_max_concurrency(self, paralelo_siempre, monkeypatch):
        """MAX_CONCURRENCY es la misma perilla en todo el pipeline; acá tiene
        que valer también, y con 1 hilo el resultado no puede cambiar."""
        monkeypatch.setenv("MAX_CONCURRENCY", "1")
        assert fast_median._hilos() == 1
        a = np.random.default_rng(2).random((80, 90)).astype(np.float32)
        assert np.array_equal(median_filter_fast(a, 9),
                              ndimage.median_filter(a, size=9, mode="nearest"))
