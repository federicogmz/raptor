# RAPTOR — Documentación Técnica

Procesamiento fotogramétrico de vuelos DJI (RGB, térmico Zenmuse H20T/Mavic
3T, multiespectral Mavic 3M) para producir ortomosaicos georreferenciados.

## Arquitectura del pipeline

El pipeline se compone de 7 etapas, orquestadas por `docker/entrypoint.sh`
dentro de un único contenedor (`Dockerfile`, `FROM opendronemap/odm:gpu`).
**ODM hace TODA la reconstrucción 3D y el renderizado de ortofoto para los
tres sensores** (RGB, térmico, multiespectral) — no hay ningún blending
heurístico propio; los scripts Python solo preparan las imágenes de entrada,
recortan bordes de baja confianza y postprocesan (índices, tiles, export).

```
data/rgb_mosaico/*_V.JPG    data/termica_mosaico/*_T.JPG   data/multiespectral_mosaico/*_MS_*.TIF
       │                            │                              │
       ├─ 1. Organización ──────────┼──────────────────────────────┤
       ├─ 2. Preparación GPS/geo.txt│                              ├─ 2. Preparación
       │                            ▼                              │
       │                    DJI Thermal SDK (R-JPEG→°C)             │
       │                    + denoise bilateral                     │
       │                            │                              │
       │                    re-encode °C→Kelvin×100 uint16          │
       │                    + tags Make=DJI/Model=ZH20T/            │
       │                      XMP Camera:BandName=LWIR              │
       │                            │                              │
       ▼                            ▼                              ▼
  3. ODM RGB                 3. ODM Térmico nativo           3. ODM Multiespectral
  (SfM+MVS+DSM+orto)         (SfM+MVS+malla+textura+orto     (SfM+calibración+orto
  processing/rgb_odm/         calibrada — ODM detecta y       multibanda)
       │                      convierte Kelvin→°C solo)      processing/multispectral_odm/
       ▼                     processing/thermal_native_odm/         │
  dsm_clean.py                      │                              │
       │                            ▼                              ▼
       ▼                    trim_low_overlap_edges.py       trim_low_overlap_edges.py
  trim_low_overlap_edges.py  (trim_thermal_native)           (trim_multispectral)
  (trim_rgb)                        │                              │
       │                            ▼                              ▼
       │                    confidence_mask.py           compute_vegetation_indices.py
       │                            │                              │
       └────────────┬───────────────┴──────────────────────────────┘
                    ▼
     6. generate_tiles.py → geovisor/tiles/ → serve.py :8080
                    ▼
     7. export_cog.py + export_copc.py → outputs/*.tif (COG) + *.copc.laz
```

## Etapas en detalle

### 1. Organización de imágenes (`docker/setup-data.sh`)

Busca recursivamente en `/input` los sufijos de cada sensor (`*_V.JPG`,
`*_W.JPG` RGB; `*_T.JPG` térmico) y los copia a `data/rgb_mosaico/` y
`data/termica_mosaico/`; `setup-data-multispectral.sh` hace lo propio con las 4
bandas del M3M desde `/input_ms`, y aborta con un mensaje claro si falta alguna
(ODM necesita las 4 para agrupar cada captura). No se extrae ningún CSV de
metadatos: el GPS/EXIF lo leen directamente los scripts que lo necesitan
(`export_flight_path.py`, `prepare_*_odm.py`) y ODM lo lee del EXIF de cada foto.

### 2. Preparación de imágenes

- **RGB** (target `prepare-rgb` del `Makefile`): copia las imágenes a
  `processing/rgb_odm/images/` y las re-etiqueta con `exiftool` preservando
  EXIF completo + XMP de DJI. **No se genera `geo.txt` para RGB a propósito** —
  ODM lee el GPS del EXIF y detecta solo la precisión RTK; ver el comentario
  extenso del target, que documenta por qué `-exif:all` (y no `-gps:all`) y por
  qué hace falta `-api Compact=Shorthand`.
- **Multiespectral** (`prepare_multispectral_odm.py`): organiza las 4 bandas
  del M3M (ya traen GPS EXIF nativo) y genera `geo.txt`.
- **Térmico nativo** (`prepare_thermal_native_odm.py`): re-encodea los TIFF
  Float32 °C (de `convert_thermal_tiff.py`) a uint16 Kelvin×100 y les agrega
  EXIF `Make=DJI`/`Model=ZH20T` + XMP `Camera:BandName=LWIR` — el formato y
  tags exactos que `opendm/thermal.py` de ODM reconoce para aplicar su propia
  calibración radiométrica nativa (Kelvin→Celsius) durante la reconstrucción.
  Ver `scripts/camera_ns.exiftool.config` (namespace XMP necesario para poder
  *escribir* `Camera:BandName`, no solo leerlo).

### 3. OpenDroneMap (los tres proyectos son independientes y completos)

- **ODM RGB**: SfM + MVS + DSM + ortofoto. `--feature-quality high
  --orthophoto-resolution 2 --dsm --dem-resolution 8 --pc-quality low`.
  ~20-30 min con GPU.

- **ODM Térmico nativo**: SfM + MVS + malla + textura + ortofoto — pipeline
  COMPLETO igual que RGB, no solo SfM. `--radiometric-calibration camera`
  dispara la calibración Kelvin×100→°C nativa de ODM (ver
  `opendm/thermal.py::dn_to_temperature`, rama `DJI ZH20T`) durante el
  render de textura, así que el ortofoto final ya sale en °C reales.
  `--orthophoto-resolution 10 --pc-quality medium`. ~15-20 min.
  Reemplaza (jul 2026) el pipeline heurístico anterior (proyección inversa +
  winner-take-all propio) tras compararse contra una entrega de referencia
  de Agisoft para la misma misión: el pipeline propio daba 16.9% de
  cobertura con artefactos de fragmentación en los bordes; el render nativo
  de ODM da 68.6% sin artefactos, a mayor resolución.

- **ODM Multiespectral**: SfM + calibración a reflectancia (sensor de sol,
  sin panel físico) + ortofoto multibanda. `--radiometric-calibration
  camera+sun --dsm --pc-quality low`. Solo corre si se montó `/input_ms`.

- **Limpieza DSM** (`dsm_clean.py`): detecta y elimina relleno sintético de
  ODM (mesetas planas con varianza local ~0) y outliers del DSM de RGB.
  Rellena huecos reales (agua, sombra) con la tendencia del terreno
  circundante. Salida: `outputs/dsm.tif`.

### 4. Conversión radiométrica térmica (antes de invocar ODM)

- **DJI Thermal SDK** (`convert_thermal_tiff.py`): convierte R-JPEG a
  GeoTIFF Float32 con temperatura en °C usando `dji_irp` (`distance=25`,
  máximo del sensor). Copia GPS/gimbal EXIF del R-JPEG fuente (para
  `prepare_thermal_native_odm.py` y para exportar a Agisoft/Pix4D si hace
  falta).

- **Denoise** (`denoise_thermal_frames.py`): filtro bilateral 3×3 que reduce
  el ruido amplificado por la corrección de distancia sin recortar picos
  reales de temperatura. Copia GPS/gimbal a la salida (mismo patrón).

### 5. Recorte de bordes y post-procesamiento

- **Recorte de bordes** (`trim_low_overlap_edges.py`): `trim_rgb`/
  `trim_thermal_native`/`trim_multispectral` comparten la misma lógica
  genérica — proyectan la huella de cada foto al suelo
  (`_rgb_camera_overlap`, usando el `reconstruction.json` de CADA
  proyecto), calculan un piso de solape adaptativo
  (`_adaptive_floor_joint`) y aplican un recorte de confiabilidad
  (`_reliability_crop`: núcleo conexo, contorno regular, margen de
  seguridad — estilo Agisoft/Pix4D/DJI Terra). El térmico nativo además
  hace un despike puntual y recorta extremos físicamente implausibles.

- **Máscara de confianza** (`confidence_mask.py`): limpia motas/contorno de
  RGB y térmico, genera `outputs/confidence_mask.tif` con los píxeles
  confiables en ambos productos.

- **Índices de vegetación** (`compute_vegetation_indices.py`): NDVI/GNDVI/NDRE
  y MSAVI2 desde el ortofoto multibanda multiespectral, si la misión lo trae.
  Las bandas se identifican por NOMBRE (`GetDescription()`, que ODM escribe
  desde el XMP `Camera:BandName`), no por posición: el orden de
  `reconstruction.multi_camera` de ODM no está garantizado.

- **Área afectada y severidad** (`detect_area_afectada.py`,
  `compute_severity_classes.py`, `compute_situation_summary.py`): solo si la
  misión tiene multiespectral **y** térmico. Detecta el polígono del incendio
  por z-score robusto de brillo multiespectral con histéresis y compuerta de
  NDVI, clasifica severidad y hotspot térmico dentro de ese perímetro, y resume
  todo en `outputs/situation.json` para el modo simple del geovisor.

### 6. Tiles y geovisor

- **Tiles XYZ** (`generate_tiles.py`): genera tiles PNG en niveles 14-20
  para RGB (8-bit color), térmico y multiespectral (escala de grises con
  rango de color calculado del percentil real de cada misión) e índices
  (paleta divergente RdYlGn).

- **Servidor** (`serve.py`): HTTP server con CORS que sirve el dashboard
  Leaflet y los tiles.

### 7. Exportación cloud-optimized

- **COG** (`export_cog.py`): convierte todos los rasters finales de
  `outputs/` a Cloud Optimized GeoTIFF (overviews embebidos, legible por
  rangos HTTP).
- **COPC** (`export_copc.py`): exporta las nubes de puntos densas
  georreferenciadas de los tres proyectos ODM (RGB, térmico, multiespectral)
  a Cloud Optimized Point Cloud (`.copc.laz`) vía `pdal`.

## Productos finales

| Archivo | Descripción | Resolución aprox. |
|---|---|---|
| `outputs/dsm.tif` | Modelo digital de superficie limpio (COG) | 20 cm/px |
| `outputs/rgb_orthomosaic.tif` | Ortofoto RGB (COG) | 2 cm/px |
| `outputs/thermal_orthomosaic.tif` | Ortomosaico térmico en °C (COG) | ~10 cm/px |
| `outputs/multispectral_orthomosaic.tif` | Ortofoto multibanda (COG) | ~10 cm/px |
| `outputs/indices/{ndvi,gndvi,ndre}.tif` | Índices de vegetación (COG) | igual que MS |
| `outputs/confidence_mask.tif` | Máscara RGB∩térmico confiable | grilla térmica |
| `outputs/point_cloud_{rgb,thermal,multispectral}.copc.laz` | Nubes densas (COPC) | — |

## Sistema de referencia

El de cada misión: ODM elige la zona UTM según el GPS del vuelo y la escribe en
todos sus productos. Los scripts que necesitan proyectar (el conteo de solape de
cámaras en `trim_low_overlap_edges.py`) **derivan la CRS del propio raster que
están procesando**, así que no hay nada que ajustar para volar en otra zona.

`UTM_EPSG = 32618` sigue en ese script como respaldo para un raster sin
proyección legible — antes era el valor fijo que se usaba siempre, lo que hacía
que fuera de la zona 18N las huellas de cámara cayeran a cientos de kilómetros
del raster y el recorte de bordes se degradara en silencio.

Los productos vectoriales (`area_afectada.geojson`) van en EPSG:4326 por RFC
7946 — Leaflet ignora cualquier miembro `crs` y asume siempre WGS84.

## Dependencias

- **Docker** + NVIDIA Container Toolkit (para GPU)
- **DJI Thermal SDK** (incluido en `dji_thermal_sdk/`)
- **Python 3** con: `gdal`, `numpy`, `scipy`, `pyproj` (en el contenedor)
- **exiftool** (en el contenedor, con `scripts/camera_ns.exiftool.config`
  para poder escribir el tag XMP `Camera:BandName`)
- **pdal** (incluido en la imagen base de ODM, en `/code/SuperBuild/install/bin/pdal`)
- **ODM** (imágenes oficiales `opendronemap/odm`, se descargan automáticamente)
