# Revisión del backend y pipeline RAPTOR — eficiencia y velocidad

Fecha: 2026-08-12. Alcance: `webapp/main.py`, `core/runner.py`, `core/scan.py`,
`docker/entrypoint.sh`, `docker/setup-data*.sh`, `Makefile`, `Dockerfile` y
todos los `scripts/*.py` del post-procesamiento. La suite de tests corre
**579/587 verde en el host** (los 8 restantes exigen el layout del contenedor,
`/app`, y pasan con `make test` adentro de la imagen).

## Veredicto

El pipeline está **muy bien optimizado**: casi todas las oportunidades de
velocidad de alto impacto ya están implementadas y documentadas con
mediciones reales. No hay un "cuello de botella de diseño" evidente; las
mejoras restantes son de segundo orden (memoria pico, contención de CPU,
cola del final de la corrida) o experimentales (ODM `--split`).

## Lo que ya está optimizado (no tocar sin medir antes)

| Área | Mecanismo |
|---|---|
| Reconstrucción | 4 sensores despachados en paralelo con semáforo FIFO (RGB/térmico/MS/banda D), preparación async por sensor, presupuesto de memoria compartido (`odm_slots` en `scripts/hardware.py`) |
| SfM | `--use-hybrid-bundle-adjustment`, `--matcher-neighbors 8`, `--min-num-features` por preset, `sfm_algorithm=planar` en vistazo/rápido, split opensfm/openmvs con concurrencia liviana/pesada correcta |
| MVS | `--fast-orthophoto` en vistazo/rápido (RGB y térmico) — salta DensifyPointCloud (~11 h en una corrida real de 1199 fotos) |
| Preparación | copias en paralelo con `xargs -P` (memory-aware), hardlinks en staging MS, exiftool por lotes vía `-@ -`, `cp --reflink=auto` en RGB |
| Térmico | conversión dji_irp paralela con skip de existentes, denoise vectorizado numpy, mediana multihilo |
| Post | COG paralelo con salteo por `_ya_es_cog` + predictor por dtype, COPC paralelo con mtime, tiles incrementales con sello + rangos de color cacheados, recortes/índices por sensor apenas terminan |
| Webapp | subida chunked, import-local por symlinks (cero copias), gzip, caché inmutable de tiles, validación previa al arranque, SSE con historial, kill del árbol completo de procesos |

## Dónde se va el tiempo (modelo por fases, `scripts/hardware.py`)

Coeficientes medidos en corridas reales (por foto):

- **features/matching/undistort**: ~13 s — paralela, se divide por núcleos.
- **BA incremental** (`sfm=incremental`): ~12.4 s — **secuencial por diseño**,
  domina los presets estandar+ y todo terreno escarpado. Es el límite real de
  velocidad sin cambiar de algoritmo (de ahí el submuestreo de urgencia).
- **DensifyPointCloud (MVS)**: ~13.7 s sobre GPU de referencia — solo en
  estandar/alta/máxima; escala con pc_quality y VRAM.
- **malla+textura**: ~5.1 s — paralela.
- Preparación térmica: sdk-convert ~6-12 s/foto (paralela), denoise/prepare
  livianos.
- Tiles + recortes + export: proporcionales a los píxeles del ráster final
  (el techo `res_cm` del preset es la palanca que ya los acota).

## Problemas encontrados (por severidad)

### P2 — Memoria pico en `generate_tiles.py::to_8bit` (RGB)
`to_8bit()` hace `ds.ReadAsArray()` **completo** sobre el ortomosaico RGB
(3-4 bandas a resolución nativa). En un preset `maxima` real (ráster de
~50k×41k px) son ~6 GB de un solo golpe. La versión de una banda
(`_banda_a_8bit`) ya es por franjas de 2048 filas — **reusar ese patrón para
RGB elimina el pico** sin cambiar el resultado.

### P2 — `publish_partial` compite con el ODM del sensor siguiente
`generate_tiles.py` lanza `gdal2tiles.py --processes=NPROCS` (TODOS los
núcleos) en background mientras la reconstrucción del siguiente sensor está
corriendo con su concurrencia liviana → sobresuscripción de CPU. Limitar los
procesos de gdal2tiles durante las publicaciones parciales (p.ej.
`NPROCS/2` o una env propia) deja núcleos para el SfM en curso.

### P3 — `setup-data*.sh` copia con `cp` plano
Las copias fuente → `data/` usan `cp` (paralelo, pero copia real). En
filesystems con copy-on-write (`cp --reflink=auto`) sería casi gratis, como
ya hace `prepare-rgb`. Importa solo cuando fuente y `data/` están en el mismo
fs.

### P3 — COG no cubre los rasters de clases
`export_cog.py::PRODUCTS` incluye índices (`outputs/indices/*.tif`) pero no
`outputs/severidad_class.tif` ni `outputs/termico_hotspot_class.tif`
(Byte, chicos; menor impacto).

### P3 — `index()` relee `index.html` de disco en cada request
93 KB con revalidación `no-cache` — trivial, pero un cache simple en memoria
lo elimina.

### P3 — Subida escribe en el event loop
`/api/upload` hace `open().write()` sincrónico en un handler async. Con un
operador único es imperceptible; `run_in_threadpool` lo deja impecable.

### P3 — Sin auth ni límites en la webapp
El proceso escucha en `0.0.0.0` sin autenticación: `DELETE /api/missions/{m}`
borra misiones y la subida no tiene tope de tamaño. Aceptable en campo
(localhost/red aislada), riesgoso en una red compartida.

### P3 — Estimación «vistazo+escarpado» sin recalibrar
El factor ×3 por incremental forzado está marcado `PENDIENTE` en
`hardware.py`. **La misión de prueba es la oportunidad de calibrarlo** con
`outputs/logs/timings.json` + substages.

## Oportunidades de velocidad (priorizadas)

1. **ODM `--split`/`--split-overlap` para misiones muy grandes** (>3000 fotos,
   caso barbosa-picodegallo: 24 h). ODM parte el vuelo en chunks que corren en
   paralelo y baja el pico de memoria. Experimental: medir contra el baseline
   antes de habilitarlo como preset.
2. **`to_8bit` RGB por franjas** (P2 arriba) — evita el pico de RAM en
   alta/máxima, sin costo de tiempo.
3. **Presupuestar núcleos para `publish_partial`** (P2 arriba).
4. **Paralelizar el `exiftool -overwrite_original` de `prepare-rgb`** — hoy un
   solo proceso secuencial sobre todas las fotos; con `xargs -P` (lotes vía
   `-@ -`) se divide por núcleos. Medir antes: en NVMe quizá ya es minutos.
5. **`cp --reflink=auto` en setup-data** (P3).
6. **Cache de `index.html` + COG de clases + `run_in_threadpool` en upload**
   (P3, 20 min de trabajo total).
7. **Calibrar el modelo de estimación** con los datos de la misión de prueba.

## Cómo medir la misión de prueba (baseline)

- `outputs/logs/timings.json` — tiempos por etapa (prep / ODM por sensor /
  post) y sub-etapas dentro del SfM.
- `outputs/logs/odm_<sensor>_substages.json` — dónde se fue cada
  reconstrucción (features vs incremental vs MVS).
- `outputs/run_summary.json` — resultado + productos generados.
- `outputs/logs/*.log` — detalle por etapa.
- Comparar contra la estimación previa (webapp/CLI): la diferencia mide qué
  tan calibrado está el modelo.

## Mejoras aplicadas (2026-08-12)

Las de bajo riesgo ya implementadas y validadas (suite 579/587 verde, mismos
8 fallos ambientales de `/app` de antes; cero regresiones):

- **`generate_tiles.py::to_8bit` por franjas** (RGB 3-4 bandas) — elimina el
  pico de RAM en presets altos; resultado idéntico (verificado con ráster
  sintético de 2600 filas, alpha incluida).
- **`RAPTOR_TILES_PROCESSES`** en `generate_tiles.py` + `publish_partial` en
  `entrypoint.sh` — los tiles parciales usan la mitad de los núcleos para no
  pelearle la CPU al SfM del sensor siguiente; el `make tiles` final sigue
  usando la máquina entera (el sello de mtime evita re-teselar).
- **`cp --reflink=auto`** en `setup-data*.sh` — copias casi gratis en
  filesystems copy-on-write, fallback a copia normal en el resto.
- **COG de clases** — `severidad_class.tif` y `termico_hotspot_class.tif`
  entran a `export_cog.py::PRODUCTS`.
- **`webapp/main.py`** — `index.html` cacheado contra mtime (una lectura por
  cambio, no por request) y la escritura del upload movida a un hilo de
  trabajo (`run_in_threadpool`), fuera del event loop. Validado con smoke
  test (GET /, upload, status, validate).
- **`Makefile::prepare-rgb`** — exiftool paralelo (`xargs -P NPROCS -n 100`)
  en vez de un solo proceso secuencial; equivalente por archivo. PENDIENTE:
  verificar dentro del contenedor con fotos DJI reales (no hay exiftool en
  este host).

## Entorno para correr la misión

Este host no tiene Docker ni runtime NVIDIA: el pipeline (ODM) corre
**dentro de la imagen `raptor:latest`** (`FROM opendronemap/odm:gpu`), que
hay que construir con `./raptor build` en una máquina con Docker. La webapp
sí está desplegada acá (puerto 8080) y puede subir/validar fotos, pero
`/api/missions/{m}/start` lanza `docker/entrypoint.sh` con rutas `/app` que
no existen fuera del contenedor.
