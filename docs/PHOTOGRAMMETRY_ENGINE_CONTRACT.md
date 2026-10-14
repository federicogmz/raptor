# Contrato I/O del Motor Fotogramétrico (Photogrammetry Engine Contract)

**Versión:** 1.0  
**Fecha:** Octubre 2026  
**Propósito:** Definir la interfaz estándar y agnóstica entre RAPTOR (sistema de analítica y respuesta táctica ante incendios forestales) y los motores de fotogrametría (OpenDroneMap actual, o motores de reconstrucción acelerada 2.5D en C++/CUDA/Rust futuros).

---

## 1. Principio Arquitectónico

RAPTOR separa estrictamente:
1. **El Motor Fotogramétrico**: Responsable de la calibración de cámaras (SfM), triangulación, estimación de superficie (DSM) y ortorrectificación de mosaicos.
2. **El Sistema Táctico RAPTOR**: Responsable de la analítica operacional contra incendios (triage de hotspots, propagación de calor, clasificación espectral de daño en cobertura vegetal, máscaras de confiabilidad y resúmenes de mando).

Cualquier motor compatible con RAPTOR debe adherirse a las especificaciones de entrada, salida y reglas radiométricas descritas a continuación.

---

## 2. Niveles de Entrega Operacional (Tiers de Latencia)

| Nivel | Responsable | Latencia Objetivo | Entregables Principales | Precisión Típica |
| :--- | :--- | :--- | :--- | :--- |
| **Nivel 0 (Triage Táctico)** | RAPTOR | **< 2 minutos** | `triage_thermal_level0.geojson`, `situation_level0.json` | $\pm 5\text{--}15\text{ m}$ (con DEM 2m) / $\pm 30\text{--}50\text{ m}$ (plano) |
| **Nivel 1 (Reconstrucción Rápida 2.5D)** | Motor Fotogramétrico | **5 – 15 minutos** | Ortomosaicos rápidos preliminares (RGB, Térmica), DSM disperso | $\pm 1\text{--}2\text{ m}$ |
| **Nivel 2 (MVS Completo)** | Motor Fotogramétrico | **30 – 60+ minutos** | Ortomosaicos definitivos a máxima resolución, DSM denso, Índices multiespectrales (NDVI, NDRE) | Submétrica / centimétrica ($\pm 0.05\text{--}0.2\text{ m}$) |

---

## 3. Especificación de Entradas del Motor

El motor fotogramétrico debe aceptar los siguientes insumos en el sistema de archivos o monturas de contenedor:

### 3.1. Imágenes de Entrada (`images/`)
- **RGB**: JPEG o GeoTIFF con metadatos EXIF estándar (GPSLat, GPSLon, GPSAlt, GimbalYaw, GimbalPitch, GimbalRoll).
- **Térmica**: GeoTIFF Float32 en grados Celsius absolutos (°C) o TIFF uint16 en convención Kelvin×100 (`round((°C + 273.15) * 100)`).
- **Multiespectral**: TIFF calibrados por banda (e.g. Green, Red, RedEdge, NIR) con metadatos de radiancia/reflectancia.

### 3.2. Georreferenciación y Poses (`geo.txt` o EXIF)
Formato tabular estándar tab-separated con encabezado de proyección (ej. `EPSG:4326` o CRS proyectado):
```text
EPSG:4326
filename    longitude    latitude    altitude    yaw    pitch    roll    horiz_acc    vert_acc
IMG_0001.tif -75.58921   6.16843     2150.4      34.2   -89.5    0.1     0.05         0.08
```

### 3.3. DEM Externo Opcional (`--external-dem <path.tif>`)
- Formato: GeoTIFF (Float32 o Int16) con sistema de referencia de coordenadas (CRS) explícito.
- Tipos soportados: DSM (Superficie) o DTM (Terreno).
- Si se proporciona, el motor lo utiliza para:
  1. Acelerar y restringir el emparejamiento de correspondencias (matching guiado por altura).
  2. Proyectar ortofotos directas en modo 2.5D sin requerir densificación MVS costosa.
  3. Fijar la cota vertical absoluta contra un modelo de elevación oficial verificado.

---

## 4. Especificación de Salidas del Motor

El motor debe depositar en el directorio `outputs/` los siguientes artefactos:

### 4.1. Ortomosaicos COG (Cloud Optimized GeoTIFF)
- `rgb_orthomosaic.tif`: COG RGB de 3 bandas (Byte, 0-255), compresión DEFLATE/LZW o JPEG con sobremuestreos (*overviews*) internos.
- `thermal_orthomosaic.tif`: COG Float32 de 1 banda en unidades de **°C reales**.
- `dsm.tif`: Modelo Digital de Superficie en Float32 (metros sobre el datum).

### 4.2. REGLA INQUEBRANTABLE: Preservación Radiométrica
> [!CRITICAL]
> **Prohibido el blending fotométrico destructivo en datos científicos.**
> Para las bandas térmicas y multiespectrales, el remuestreo espacial del mosaico DEBE ser **Nearest-Neighbor (NEAREST)** en los bordes de celda.
> Queda estrictamente prohibida cualquier interpolación bilineal, bicúbica, corrección de ganancia o mezcla de coloración de bordes (*feathering*) que altere los valores físicos absolutos de temperatura (°C) o reflectancia.

### 4.3. Cámaras Calibradas y Poses (`reconstruction.json`)
Estructura JSON estandarizada (compatible con OpenSfM / Bundler) conteniendo:
- Modelo de cámara con parámetros intrínsecos calibrados ($f_x, f_y, c_x, c_y, k_1, k_2$).
- Poses extrínsecas por fotograma ($R$ rotación axis-angle o matriz $3\times3$, $t$ vector de traslación).
- Origen geodésico local (`reference_lla`: latitud, longitud, altitud).

### 4.4. Telemetría y Registro de Progreso
- Eventos de progreso estructurados emitidos en stdout o archivo de log con prefijo estándar (`[STAGE_START]`, `[STAGE_PROGRESS: XX%]`, `[STAGE_END]`).
- Tiempos de ejecución por etapa registrados en segundos reales de reloj.
