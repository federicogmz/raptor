# Herramientas de diagnóstico

Scripts auxiliares para depurar y analizar resultados del pipeline.
No forman parte del flujo principal.

- `thermal_diag.py` — Diagnóstico rápido del mosaico térmico a baja
  resolución. Acumula count/sum/sumsq de temperaturas proyectadas sin
  pesos ni bias, exporta count/mean/std como PNG. Útil para distinguir
  si los artefactos vienen de la geometría (count) o del desacuerdo
  entre frames (std).

Uso:
  python3 scripts/debug/thermal_diag.py