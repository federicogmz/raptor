# RAPTOR — Documentación Técnica

Procesamiento fotogramétrico de vuelos DJI (RGB, térmico Zenmuse H20T/Mavic
3T, multiespectral + banda D Mavic 3M) para producir ortomosaicos
georreferenciados.

## Arquitectura del pipeline

Orquestado por `docker/entrypoint.sh` dentro de un único contenedor
(`Dockerfile`, `FROM opendronemap/odm:gpu`). **ODM hace TODA la
reconstrucción 3D y el renderizado de ortofoto para los cuatro sensores**
(RGB, térmico, multiespectral, banda D) — no hay ningún blending heurístico
propio; los scripts Python preparan las imágenes de entrada, recortan bordes
de baja confianza y postprocesan (índices, área afectada, tiles, export).

Los cuatro proyectos ODM (`rgb_odm`, `thermal_native_odm`,
`multispectral_odm`, `dband_odm`) son **independientes**: distinta carpeta
fuente, distinta carpeta destino, ningún archivo compartido. El pipeline lo
aprovecha en dos niveles:

1. **Cada sensor prepara sus imágenes de forma independiente** — no hay una
   barrera global que espere a que los cuatro terminen de preparar antes de
   que cualquiera pueda empezar a reconstruir (el térmico, que invoca el SDK
   de DJI foto por foto, puede tardar 20-40 min cada 200 fotos; RGB/MS/banda D
   preparan en segundos a minutos).
2. **La reconstrucción de cada sensor arranca apenas SU preparación
   termina**, compitiendo por un cupo memory-aware contra los demás sensores
   que ya estén listos (`odm_slots()` — ver «Reparto de la máquina entre
   proyectos ODM» más abajo).

```
data/rgb_mosaico/       data/termica_mosaico/     data/multiespectral_mosaico/   data/dband_mosaico/
*_V.JPG, *_W.JPG         *_T.JPG                   *_MS_{G,R,RE,NIR}.TIF          *_D.JPG
       │                       │                          │                          │
       └──────────┬────────────┘                          └────────────┬─────────────┘
                  │  0. Organización (docker/setup-data*.sh, EN PARALELO)
                  ▼
       1. Ruta de vuelo (export_flight_path.py) — antes de ODM, da algo real
          que mirar en el geovisor desde el minuto uno
                  │
                  ▼
       2. Preparación POR SENSOR, cada uno independiente:
            RGB: copia + exiftool (prepare-rgb)
            Térmico: DJI SDK R-JPEG→°C + denoise + re-encode Kelvin×100
            MS/banda D: hardlink a images/ + geo.txt (scripts/odm_staging.py)
                  │
                  ▼
       3. SEMÁFORO de reconstrucción (odm_slots(), memory-aware) — cada
          sensor entra apenas SU preparación terminó, no cuando terminan
          las de los demás:
   ┌──────────────┬──────────────────┬───────────────────┬──────────────┐
   │  ODM RGB     │  ODM Térmico     │  ODM Multiespectral│  ODM Banda D │
   │  (SfM+[MVS]  │  nativo (SfM+MVS │  (SfM+calibración  │  (SfM+orto,  │
   │  +DSM+orto)  │  +malla+textura  │  +orto multibanda) │  siempre     │
   │  processing/ │  +orto en °C)    │  processing/       │  --fast-orth │
   │  rgb_odm/    │  processing/     │  multispectral_odm/│  ophoto)     │
   │              │  thermal_native_ │                    │  processing/ │
   │              │  odm/            │                    │  dband_odm/  │
   └──────┬───────┴────────┬─────────┴─────────┬──────────┴──────┬───────┘
          │  4. Apenas CADA sensor termina SU reconstrucción (no espera a
          │     los demás): limpieza DSM (solo RGB o MS sin RGB) + recorte
          │     de bordes de bajo solape + tiles parciales + export COG/COPC
          ▼          ▼                   ▼                     ▼
    dsm_clean.py  trim_low_       trim_low_             trim_low_
    (solo RGB/MS) overlap_edges.py overlap_edges.py      overlap_edges.py
          │            │                   │                     │
          └──────┬─────┴───────────────────┴─────────────────────┘
                 │  5. Análisis cruzado, EN PARALELO entre sí, sobre lo ya
                 │     recortado (no se leen ni escriben entre sí):
                 ▼
   ┌─────────────────────┬──────────────────────┬───────────────────────┐
   │ confidence_mask.py   │ compute_flight_       │ compute_vegetation_   │
   │ (RGB∩térmico)        │ quality.py (solape,   │ indices.py +          │
   │                      │ velocidad, % reconst.)│ detect_area_afectada  │
   │                      │                       │ + compute_severity_   │
   │                      │                       │ classes / compute_    │
   │                      │                       │ thermal_hotspot       │
   └──────────┬───────────┴───────────┬───────────┴────────────┬──────────┘
              └─────────────┬─────────┴─────────────────────────┘
                            ▼
             6. generate_tiles.py → geovisor/tiles/ → webapp/main.py :8080
                            ▼
             7. Pase final: export_cog.py + export_copc.py (idempotentes —
                saltean lo que cada sensor ya exportó en el paso 4)
                            ▼
             8. export_products.py → EXPORT_DIR (opcional)
```

## Etapas en detalle

### 0. Organización de imágenes (`docker/setup-data.sh`, `setup-data-multispectral.sh`)

Buscan recursivamente los sufijos de cada sensor y copian a
`data/{rgb,termica,multiespectral,dband}_mosaico/`: `*_V.JPG`/`*_W.JPG`
(RGB), `*_T.JPG` (térmico) desde `/input`; `*_MS_{G,R,RE,NIR}.TIF` y
`*_D.JPG` desde `/input_ms`. Los dos scripts corren **en paralelo** (leen de
carpetas fuente distintas, escriben a destinos distintos — no comparten
nada). `setup-data-multispectral.sh` aborta con un mensaje claro si falta
alguna de las 4 bandas espectrales (ODM necesita las 4 para agrupar cada
captura); banda D es opcional, su ausencia no es error. No se extrae ningún
CSV de metadatos: el GPS/EXIF lo leen directamente los scripts que lo
necesitan y ODM lo lee del EXIF de cada foto.

### 1. Ruta de vuelo (`export_flight_path.py`)

Corre apenas termina la organización, ANTES de tocar ODM: lee GPS/tiempo
directo del EXIF de `data/*_mosaico/` (ya completos en este punto), no
necesita esperar a la preparación específica de cada sensor (que para
térmico puede tardar 20-40 min). Le da al geovisor algo real que mostrar
desde el primer minuto, en vez de una pantalla vacía durante toda la
reconstrucción.

### 2. Preparación de imágenes (por sensor, independiente)

- **RGB** (target `prepare-rgb` del `Makefile`): copia las imágenes a
  `processing/rgb_odm/images/` y las re-etiqueta con `exiftool` preservando
  EXIF completo + XMP de DJI. **No se genera `geo.txt` para RGB a propósito** —
  ODM lee el GPS del EXIF y detecta solo la precisión RTK; ver el comentario
  extenso del target, que documenta por qué `-exif:all` (y no `-gps:all`) y por
  qué hace falta `-api Compact=Shorthand`.
- **Multiespectral y banda D** (`prepare_multispectral_odm.py`,
  `prepare_dband_odm.py`, ambos sobre `scripts/odm_staging.py`): pueblan
  `images/` con **hardlinks** en vez de copias (ODM solo LEE esos archivos,
  y en misiones grandes copiarlos duplicaba varios GB de escritura por
  nada) y generan `geo.txt` desde el GPS EXIF nativo del M3M. Si el hardlink
  no se puede hacer (fuente y destino en filesystems distintos) cae a copia
  normal sin avisar — mismo resultado, solo más lento.
- **Térmico nativo** (`prepare_thermal_native_odm.py`): re-encodea los TIFF
  Float32 °C (de `convert_thermal_tiff.py`) a uint16 Kelvin×100 y les agrega
  EXIF `Make=DJI`/`Model=ZH20T` + XMP `Camera:BandName=LWIR` — el formato y
  tags exactos que `opendm/thermal.py` de ODM reconoce para aplicar su propia
  calibración radiométrica nativa (Kelvin→Celsius) durante la reconstrucción.
  Ver `scripts/camera_ns.exiftool.config` (namespace XMP necesario para poder
  *escribir* `Camera:BandName`, no solo leerlo).

**Presupuesto de preparación compartido**: multiespectral, térmico y banda D
paralelizan CADA UNO puertas adentro (copia+exiftool / `dji_irp` por
imagen). Si cada uno pidiera su propia concurrencia memory-aware sin saber
de los demás, correrlos todos a la vez podría sumar más hilos de los que la
RAM disponible banca. `entrypoint.sh` calcula un presupuesto único
(`PREP_BUDGET`, calibrado contra el perfil más pesado, 5 MP) y lo reparte
entre los streams que de verdad van a correr y paralelizan puertas adentro
(`PREP_SPLIT`) — el térmico queda afuera de este reparto porque `dji_irp`
mide ~9 MB RSS por proceso, un perfil de memoria completamente distinto.

### 3. Reconstrucción ODM (semáforo memory-aware)

Todos los parámetros de calidad (`--pc-quality`, `--feature-quality`,
`--orthophoto-resolution`/`--dem-resolution`, `--min-num-features`,
`--matcher-neighbors`, `--sfm-algorithm`, bundle adjustment,
`--fast-orthophoto`) salen del **preset** elegido (`vistazo` / `rapido` /
`estandar` / `alta` / `maxima`) y del **terreno** (`plano`/`escarpado`) vía
`scripts/hardware.py::preset()` — única fuente que también lee la webapp y
`./raptor run` para mostrar el mismo mensaje de resolución/tiempo antes de
arrancar. Ver «Calidad, terreno y hardware» en el README para el porqué de
cada palanca.

- **`sfm_algorithm`**: `planar` en `vistazo`/`rapido` por defecto — alinea
  las fotos por homografías entre pares, mucho más rápido que la
  reconstrucción incremental, pero **solo válido si la escena es
  efectivamente plana** (vuelo nadir, altura fija). Con terreno de relieve
  fuerte, `planar` puede descartar la mayoría de las fotos en silencio: en
  una misión de terreno rocoso, `vistazo` con `planar` solo incorporó 283 de
  1199 fotos RGB (23.6%) y 224 de 1199 térmicas (18.7%) a la reconstrucción
  final — sin ningún error, el pipeline "terminó bien" con un ortomosaico
  muy recortado. `TERRENO=escarpado` fuerza `incremental` (reconstruye foto
  por foto vía bundle adjustment, sin asumir un plano) incluso en los
  presets rápidos — confirmado en la misma misión: 99.2% y 98.7% de
  cobertura respectivamente, a costa de ser bastante más lento (la misma
  misión: térmico ~6h, RGB ~10h en vez de minutos — incremental es
  secuencial por diseño, no paralelizable entre fotos).

- **`--fast-orthophoto` en RGB** (`vistazo`/`rapido`): salta
  `DensifyPointCloud` (MVS) — la etapa de reconstrucción densa, que en un
  vuelo de ~1200 fotos con GPU de laptop midió ~11h de las ~15h totales de
  esa reconstrucción. El DSM sale entonces de la nube DISPERSA de SfM en vez
  de la densa (más pobre, más huecos). `estandar` en adelante mantienen la
  nube densa completa porque RGB es la fuente del DSM de la misión. Banda D
  usa esta bandera SIEMPRE (nunca pide `--dsm`, no tiene sentido pagar la
  etapa más cara de toda la misión para un producto que no la necesita).

- **`run_odm()`** (en `entrypoint.sh`) parte cada reconstrucción en dos
  invocaciones de ODM con concurrencias distintas — `--end-with opensfm`
  con concurrencia LIVIANA (todo el SfM: detectar/emparejar features,
  reconstruir, undistort — ODM reduce las fotos a `feature_process_size`
  antes de tocarlas, así que el footprint por hilo no depende de la
  resolución nativa) y `--rerun-from openmvs` con concurrencia PESADA (la
  reconstrucción densa, que si corre, sí depende de la resolución nativa del
  sensor). Cuando el proyecto usa `--fast-orthophoto` no hay etapa densa que
  proteger, así que `run_odm()` lo detecta (revisa los argumentos, no el
  nombre del sensor) y hace **una sola invocación** con la concurrencia
  liviana para todo el run.

- **ODM RGB**: SfM + [MVS salvo `--fast-orthophoto`] + DSM + ortofoto.
- **ODM Térmico nativo**: SfM + MVS + malla + textura + ortofoto — pipeline
  COMPLETO igual que RGB, no solo SfM. `--radiometric-calibration camera`
  dispara la calibración Kelvin×100→°C nativa de ODM (ver
  `opendm/thermal.py::dn_to_temperature`, rama `DJI ZH20T`) durante el
  render de textura, así que el ortofoto final ya sale en °C reales. Se usa
  el render nativo y no un blending heurístico propio: contra una entrega
  de referencia de Agisoft sobre la misma misión, una proyección inversa
  con winner-take-all propio da 16.9% de cobertura con artefactos de
  fragmentación en los bordes; el render nativo de ODM da 68.6% sin
  artefactos, a mayor resolución.
- **ODM Multiespectral**: SfM + calibración a reflectancia (sensor de sol,
  sin panel físico) + ortofoto multibanda. Solo corre si se montó
  `/input_ms`. Si no hay vuelo RGB, su propio DSM (también corre con
  `--dsm`) pasa a ser la fuente del modelo de superficie de la misión.
- **ODM Banda D**: SfM + ortofoto rápida (`--fast-orthophoto` siempre, sin
  `--dsm`). Solo corre si `DBAND=1` y hay archivos `*_D.JPG`.

### Reparto de la máquina entre proyectos ODM (`odm_slots()`)

Los cuatro proyectos son independientes, así que correrlos estrictamente en
serie desperdicia la máquina de forma medible: la reconstrucción incremental
de un sensor puede usar ~1 núcleo durante horas mientras los demás no hacen
nada, y la siguiente reconstrucción espera su turno sin necesidad real.

`scripts/hardware.py::odm_slots(perfiles_mp)` decide, contra la RAM
disponible REAL (no la total) y los núcleos, cuántos proyectos pendientes
pueden reconstruir a la vez: se prueba el N más grande permitido y se baja
hasta que TODOS los de la tanda reciban al menos `MIN_HILOS_POR_PROYECTO`
tanto en la fase liviana como en la pesada — si ni siquiera dos entran, se
corre de a uno (el comportamiento de siempre, ahora como piso y no como
techo). El perfil de megapíxeles de cada sensor (`_odm_mp()` en
`entrypoint.sh`) es el proxy de cuánta RAM necesita en su etapa más pesada:
12.3 MP para RGB (fotos nativas, lo que pesa `DensifyPointCloud`), salvo que
ese RGB use `--fast-orthophoto` — ahí su etapa más pesada real es
texturizar sobre las imágenes "undistorted" (casi resolución nativa, pero
lejos del costo de MVS), así que su perfil baja a la mitad. Sin este ajuste,
`odm_slots()` seguía reservando la máquina entera para un RGB que ya no
paga el costo que ese perfil asumía.

`RAPTOR_ODM_PARALELO=1` fuerza el modo secuencial de siempre.
`MAX_CONCURRENCY` sigue siendo la perilla manual de hilos por proyecto (no
de cuántos proyectos corren juntos — respetarlo lanzando varios proyectos
con ese valor cada uno sería lo contrario de lo que pide).

### El chequeo de CUDA

El binario de reconstrucción densa (`DensifyPointCloud`) está enlazado
contra `libcuda.so.1` y ni siquiera carga sin el runtime de NVIDIA montado
— la corrida moriría en la etapa `openmvs`, después de haber pagado la
preparación y todo el SfM. `entrypoint.sh` lo comprueba en el primer
segundo, pero **solo para los sensores que de verdad van a tocar esa
etapa**: con `SKIP_ODM=1` no hay reconstrucción ninguna, y ni banda D
(`--fast-orthophoto` siempre) ni RGB en `vistazo`/`rapido`
(`--fast-orthophoto` también) la tocan — si esos son los únicos sensores
activos, el chequeo no bloquea aunque falte CUDA.

### 4. Post-procesamiento por sensor (apenas termina SU reconstrucción)

- **Limpieza DSM** (`dsm_clean.py`): detecta y elimina relleno sintético de
  ODM (mesetas planas con varianza local ~0) y outliers. Rellena huecos
  reales (agua, sombra) con la tendencia del terreno circundante. Corre
  sobre el DSM de RGB, o el de multiespectral si no hay vuelo RGB. Salida:
  `outputs/dsm.tif`.
- **Recorte de bordes** (`trim_low_overlap_edges.py`): `trim_rgb`/
  `trim_thermal_native`/`trim_multispectral`/`trim_dband` comparten la misma
  lógica genérica — proyectan la huella de cada foto al suelo
  (`_rgb_camera_overlap`, usando el `reconstruction.json` de CADA
  proyecto), calculan un piso de solape adaptativo
  (`_adaptive_floor_joint`) y aplican un recorte de confiabilidad
  (`_reliability_crop`: núcleo conexo, contorno regular, margen de
  seguridad — estilo Agisoft/Pix4D/DJI Terra). El térmico nativo además
  hace un despike puntual y recorta extremos físicamente implausibles.
- **Exportación cloud-optimized de ESE sensor** (`export_cog.py`,
  `export_copc.py`, llamados con argumentos explícitos): apenas termina el
  post-procesamiento de un sensor, sus productos ya salen en COG/COPC — no
  hace falta esperar a que los demás sensores terminen. Es idempotente
  aposta: un pase final sin argumentos (ver etapa 7) vuelve a recorrer todo
  `outputs/` pero saltea lo que ya esté al día.
- **`publish_partial()`**: tiles del geovisor regenerados apenas hay algo
  nuevo que mostrar — no una etapa nueva en la barra de progreso (el
  usuario sigue viendo la fase real en curso), solo una actualización de
  lo que ya hay.

### 5. Análisis cruzado (en paralelo entre sí)

Corren en paralelo porque **no se leen ni se escriben entre sí** — cada uno
lee solo ortomosaicos/índices ya recortados:

- **Máscara de confianza** (`confidence_mask.py`): lee
  `outputs/{rgb,thermal}_orthomosaic.tif`, genera
  `outputs/confidence_mask.tif` con los píxeles confiables en ambos
  productos. Requiere RGB Y térmico (si falta cualquiera de los dos, no
  corre — `confidence_mask.py` abriría un archivo inexistente).
- **Calidad del levantamiento** (`compute_flight_quality.py`): solape de
  cámaras, velocidad de vuelo, % de imágenes reconstruidas — independiente
  de si hay multiespectral, corre para cualquier combinación de sensores
  que incluya térmico.
- **Índices de vegetación** (`compute_vegetation_indices.py`): NDVI/GNDVI/
  NDRE/MSAVI2 desde el ortofoto multibanda multiespectral, si la misión lo
  trae. Las bandas se identifican por NOMBRE (`GetDescription()`, que ODM
  escribe desde el XMP `Camera:BandName`), no por posición.
- **Área afectada y severidad** (`detect_area_afectada.py`,
  `compute_severity_classes.py`, `compute_situation_summary.py`): solo si la
  misión tiene multiespectral **y** térmico. Detecta el polígono del incendio
  por z-score robusto de brillo multiespectral con histéresis y compuerta de
  NDVI, clasifica severidad y hotspot térmico dentro de ese perímetro.
- **Hotspot térmico standalone** (`compute_thermal_hotspot.py`): cuando hay
  térmico pero NO multiespectral, genera el hotspot sobre temperatura
  absoluta directamente (sin el recorte al polígono de área afectada, que
  necesita NDVI) — antes una misión RGB+térmico sin M3M se quedaba sin esta
  capa sin necesidad real.
- **`compute_situation_summary.py`**: resume todo en `outputs/situation.json`
  para el resumen gerencial del geovisor — corre tanto con área afectada
  completa como con el caso solo-térmico.

### 6. Tiles y geovisor

- **Tiles XYZ** (`generate_tiles.py`): genera tiles PNG desde zoom 14 hasta el
  que corresponda a la resolución REAL de cada ráster (`zoom_range()`, deriva
  el zoom máximo del geotransform). RGB (8-bit color), térmico, multiespectral
  y banda D (escala de grises con rango de color calculado del percentil real
  de cada misión) e índices (paleta divergente RdYlGn). Se invoca
  incrementalmente (`publish_partial()`) durante todo el pipeline, no solo al
  final — saltea lo que no cambió (`up_to_date()`, mtime).
- **Servidor** (`webapp/main.py`): sirve el dashboard Leaflet (vendorizado,
  funciona sin internet) y los tiles bajo `/geovisor/`, más el HUD de
  progreso en vivo (SSE), el muestreo por punto y el guardado del polígono
  de área afectada editado a mano. Es el mismo proceso en los modos `webapp`
  y `serve`.

### 7. Pase final de exportación cloud-optimized

- **COG** (`export_cog.py`, sin argumentos): recorre `PRODUCTS` (todos los
  rasters finales posibles, incluido `dband_orthomosaic.tif`) y convierte
  solo lo que exista y **todavía no sea COG** (`_ya_es_cog()` chequea el
  metadato real `LAYOUT=COG` de GDAL) — lo que cada sensor ya exportó en la
  etapa 4 se saltea sin costo.
- **COPC** (`export_copc.py`, sin argumentos): las 4 nubes de puntos
  densas georreferenciadas (`odm_georeferencing/odm_georeferenced_model.laz`
  de cada proyecto) a Cloud Optimized Point Cloud vía `pdal`, salteando las
  que ya estén al día (`_al_dia()`, compara mtime contra la fuente).

### 8. Entrega al usuario (opcional)

- **`export_products.py`**: copia los productos elegidos a `EXPORT_DIR`, en el
  formato (`cog`/`gtiff`, `geojson`/`gpkg`/`shp`/`kml`) y el EPSG que se pidan.
  Sin `EXPORT_DIR` es un no-op. Los rásters de clases se remuestrean con vecino
  más cercano (mismo criterio que `generate_tiles.py`); los continuos, con
  cúbico. Escribe un `export_manifest.json` con el detalle de la entrega.

  La reproyección se hace en **dos pasos** (warp a GTiff temporal, después
  `Translate` al formato final) porque `gdal.Warp` no puede escribir directo a
  COG: ese driver solo implementa `CreateCopy()`, y el binding de Python
  —a diferencia del ejecutable `gdalwarp`— devuelve un GeoTIFF con el
  geotransform destruido sin lanzar ningún error. Ver el comentario en
  `_export_raster()`.

## Productos finales

| Archivo | Descripción | Resolución aprox. |
|---|---|---|
| `outputs/dsm.tif` | Modelo digital de superficie limpio (COG) | según preset |
| `outputs/rgb_orthomosaic.tif` | Ortofoto RGB (COG) | según preset |
| `outputs/thermal_orthomosaic.tif` | Ortomosaico térmico en °C (COG) | según preset |
| `outputs/multispectral_orthomosaic.tif` | Ortofoto multibanda (COG) | según preset |
| `outputs/dband_orthomosaic.tif` | Ortofoto rápida de la cámara RGB del M3M (COG) | según preset |
| `outputs/indices/{ndvi,gndvi,ndre,msavi2}.tif` | Índices de vegetación (COG) | igual que MS |
| `outputs/confidence_mask.tif` | Máscara RGB∩térmico confiable | grilla térmica |
| `outputs/area_afectada.geojson` | Polígono del área afectada detectada | — |
| `outputs/severidad_class.tif`, `termico_hotspot_class.tif` | Clasificaciones (COG) | grilla térmica |
| `outputs/situation.json` | Resumen ejecutivo para el geovisor | — |
| `outputs/point_cloud_{rgb,thermal,multispectral,dband}.copc.laz` | Nubes densas georreferenciadas (COPC) | — |
| `outputs/logs/timings.json` | Desglose de tiempos por etapa | — |

## Sistema de referencia

El de cada misión: ODM elige la zona UTM según el GPS del vuelo y la escribe en
todos sus productos. Los scripts que necesitan proyectar (el conteo de solape de
cámaras en `trim_low_overlap_edges.py`) **derivan la CRS del propio raster que
están procesando**, así que no hay nada que ajustar para volar en otra zona.

`UTM_EPSG = 32618` queda en ese script solo como respaldo para un raster sin
proyección legible. No se usa como valor fijo: fuera de la zona 18N eso pondría
las huellas de cámara a cientos de kilómetros del raster y degradaría el
recorte de bordes en silencio.

Los productos vectoriales (`area_afectada.geojson`) van en EPSG:4326 por RFC
7946 — Leaflet ignora cualquier miembro `crs` y asume siempre WGS84.

## Dependencias

- **Docker** + NVIDIA Container Toolkit (recomendado — ver «El chequeo de
  CUDA» arriba para cuándo es realmente indispensable)
- **DJI Thermal SDK** (incluido en `dji_thermal_sdk/`)
- **Python 3** con: `gdal`, `numpy`, `scipy`, `pyproj` (en el contenedor)
- **exiftool** (en el contenedor, con `scripts/camera_ns.exiftool.config`
  para poder escribir el tag XMP `Camera:BandName`)
- **pdal** (incluido en la imagen base de ODM, en `/code/SuperBuild/install/bin/pdal`)
- **ODM** (heredado de la imagen base `opendronemap/odm:gpu`)
