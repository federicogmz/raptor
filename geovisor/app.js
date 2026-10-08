// ═══════════════════════════════════════════════════════════════════
// PALETTES
// ═══════════════════════════════════════════════════════════════════
// Única paleta térmica: Ironbow (negro→púrpura→rojo→naranja→amarillo→
// blanco = frío→caliente). Es el estándar de facto en cámaras térmicas
// FLIR: quien responde a incendios ya la reconoce de su propio equipo de
// mano, así que no hace falta leyenda para leerla. El pedido original era
// "hot/cold" en el sentido de UNA sola paleta clara (vs. picker de 6
// opciones, que era ruido para un caso de uso con una sola respuesta
// correcta), pero los colores que quedaron eran, sin querer, los mismos
// de un jet/arcoíris genérico (pasa por VERDE en la mitad), el problema de
// percepción más conocido de esa familia de paletas: un valor "medio" no
// tiene por qué leerse como verde, y genera bandas falsas donde no hay
// ningún cambio real de temperatura. Ironbow es monótona en luminancia
// (más caliente = más clara, SIEMPRE), sin ese artefacto.
const PALETTES = {
  ironbow: {name:'Ironbow',colors:['#000000','#1a0a3c','#4a0f6e','#8b1538','#c8291f','#f07d0a','#fce300','#ffffff'],
    stops:[0,.14,.28,.42,.56,.70,.85,1]}
};

function buildLUT(pal) {
  const lut = new Uint8Array(256*4);
  const {colors,stops}=pal;
  for(let i=0;i<256;i++){const t=i/255;let lo=0,hi=stops.length-1;
    while(hi-lo>1){const m=(lo+hi)>>1;if(stops[m]<=t)lo=m;else hi=m;}
    const f=(t-stops[lo])/(stops[hi]-stops[lo]||1);
    const c0=parseInt(colors[lo].slice(1),16),c1=parseInt(colors[hi].slice(1),16);
    lut[i*4]=((c0>>16)&255)*(1-f)+((c1>>16)&255)*f;
    lut[i*4+1]=((c0>>8)&255)*(1-f)+((c1>>8)&255)*f;
    lut[i*4+2]=(c0&255)*(1-f)+(c1&255)*f;
    lut[i*4+3]=255;
  }
  lut[3]=0; return lut;
}
const LUTS={}; for(const[k,v]of Object.entries(PALETTES))LUTS[k]=buildLUT(v);
let currentPalette='ironbow',currentLUT=LUTS[currentPalette];

// Paleta divergente para índices de vegetación (NDVI/GNDVI/NDRE, rango
// -1..1): rojo (vegetación pobre/estresada/suelo) → amarillo (neutro) →
// verde (vegetación sana), estándar en teledetección agrícola/forestal.
const INDEX_PALETTE = {name:'RdYlGn',colors:['#a50026','#d73027','#f46d43','#fdae61','#fee08b','#ffffbf','#d9ef8b','#a6d96a','#66bd63','#1a9850','#006837'],stops:[0,.1,.2,.3,.4,.5,.6,.7,.8,.9,1]};
const INDEX_LUT = buildLUT(INDEX_PALETTE);

// ═══════════════════════════════════════════════════════════════════
// IDIOMA (EN/ES)
// ═══════════════════════════════════════════════════════════════════
// El diccionario y t() se declaran ACÁ, al principio del archivo, no junto
// a applyLang()/toggleLang() más abajo (que sí quedan junto al tema, mismo
// lugar donde vive THEME_KEY — ver esa sección). LAYER_REGISTRY y los
// diccionarios de etiquetas (SIMPLE_HINTS, INDEX_LABELS, etc.), unas pocas
// líneas más abajo en este mismo archivo, ya llaman a t() al construirse.
// Con let/const declarados recién junto al tema, esa lectura temprana
// revienta con "Cannot access before initialization" (temporal dead zone):
// mismo bug real que liveMsBandIds/SITUATION, ver sus comentarios más abajo
// para el mismo patrón.
const LANG_KEY='raptor-geovisor-lang';
const I18N={
  en:{
    'doc.title':'Mission status',
    'topbar.skipLink':'Skip to the status panel',
    'topbar.back':'Back to missions',
    'topbar.loading':'Loading…',
    'topbar.dateTitle':'Date of the capture shown',
    'topbar.captureLabel':'Capture: ',
    'topbar.reportBtn':'Generate summary',
    'topbar.help':'Help',
    'help.howTo':'How to use',
    'help.rowSolo':'Solo: shows only this layer',
    'help.rowClick':'Click on the map: see what that point means',
    'help.rowMeasure':'Measure distance/area',
    'help.rowTheme':'Switch light/dark theme',
    'help.rowEsc':'Close whatever is open',
    'help.accessibility':'Accessibility',
    'help.textSize':'Text size',
    'help.textSmall':'Small text',
    'help.textNormal':'Normal text',
    'help.textLarge':'Large text',
    'help.highContrast':'High contrast',
    'help.reduceMotion':'Reduce motion',
    'help.theme':'Theme',
    'a11y.themeToggle':'Switch theme',
    'a11y.language':'Language',
    'rail.stationLabel':'Station identity',
    'rail.context':'FIELD<br>ANALYSIS',
    'rail.footer':'GIS<br>01',
    'map.areaLabel':'Area map',
    'map.fitBounds':'Fit whole mission',
    'point.cardAria':'Selected point detail',
    'measure.title':'Measurement',
    'measure.clear':'Clear',
    'measure.distance':'Distance',
    'measure.area':'Area',
    'measure.hint':'Click to add points · double click to close the area · Esc to exit',
    'panel.toggleAria':'Show status panel',
    'panel.kicker':'Mission report',
    'panel.heading':'Incident assessment',
    'panel.closeAria':'Hide panel',
    'panel.sub':'Priority findings to coordinate the response.',
    'summary.loading':'Loading mission data…',
    'ph.stageInit':'Starting…',
    'ph.collapseAria':'Collapse progress',
    'ph.expandAria':'Expand progress',
    'ph.viewLogWord':' View log',
    'ph.hideLog':' Hide log',
    'sig.kicker':'GIS',
    'sig.heading':'Layers and tools',
    'sig.sub':'Layer catalog, compare, and measure.',
    'sig.drawBtn':'Reshape',
    'sig.drawTitle':'Reshape & delineate burned area (D)',
    'draw.title':'Reshape & Delineation (QGIS)',
    'draw.modeReshape':'Reshape',
    'draw.modeCut':'Cut',
    'draw.modeAdd':'Add',
    'draw.modeNew':'New',
    'draw.freehandLabel':'Freehand active',
    'draw.assistBtn':'Suggestion',
    'draw.smoothBtn':'Smooth',
    'draw.undoBtn':'Undo',
    'draw.clearBtn':'Clear',
    'draw.saveBtn':'Save as Official Area',
    'draw.hint':'Cross the polygon perimeter with a stroke to reshape it (QGIS style)',
    'sig.compareBtn':'Compare',
    'sig.compareTitle':'Compare two layers side by side (C)',
    'sig.measureBtn':'Measure',
    'sig.measureTitle':'Measure distance and area (M)',
    'sig.exportBtn':'Export',
    'sig.exportTitle':'Download an image of the current map',
    'sig.hideAll':'Hide all',
    'sig.hideAllTitle':'Turn off all layers',
    'sig.reset':'Reset',
    'sig.resetTitle':'Return to the initial view',
    'timebar.title':'Compare over time',
    'report.title':'Situation summary',
    'report.closeAria':'Close',
    'report.download':'Download image',
    'report.loading':'Loading…',
    'report.loadError':'Could not load the mission data.',
    'report.noImpactCard':'No impact data, this mission has no thermal',
    'report.activeHotspots':'Active hotspots',
    'report.maxTemp':'Max. temperature',
    'report.avgTemp':'Avg. temperature',
    'report.generating':'Generating…',
    'report.imgAlt':'Mission situation summary',
    'report.imgError':'Could not generate the image. Turn on at least one layer on the map.',
    'report.urgentCritical':'⚠ Risk of reignition',
    'report.urgentGood':'✓ No critical anomalies',
    'report.urgentNone':'No impact data',
    'report.flightQualityLabel':'Flight quality: ',
    'report.noImpactDataYet':'No impact data yet',
    'reco.noThermal':'This mission has no thermal data to summarize.',
    'reco.label':'Recommendation',
    'val.buena':'Good',
    'val.regular':'Fair',
    'val.baja':'Low',
    'val.alta':'High',
    'val.media':'Medium',
    'report.defaultEquipment':'UAV drone',
    'summary.activeHotspots':'Active thermal hotspots',
    'summary.reignitionRisk':'Risk of reignition',
    'summary.noneDetected':'None detected',
    'summary.maxAbbr':'max',
    'summary.avgAbbr':'avg',
    'summary.lastCapture':'Last capture',
    'summary.surveyQuality':'Survey quality',
    'summary.noDataYet':'No data yet',
    'summary.overlap':'overlap ~',
    'summary.flightAt':'flight at',
    'summary.reconstructedPct':'% of photos reconstructed',
    'summary.noImpactTitle':'No impact data yet',
    'summary.noImpactSub':'This mission has no thermal, or the run has not reached that stage.',
    'freshness.updated':'Updated',
    'freshness.justNow':'just now',
    'freshness.minAgo':'min ago',
    'freshness.hAgo':'h ago',
    'freshness.lowCoverage':'⚠ low coverage vs. area flown',
    'freshness.noData':'No situation data yet',
    'hotspot.identifiedLabel':'Hotspots identified',
    'hotspot.rowLabel':'Hotspot',
    'point.looking':'Looking up…',
    'point.error':'Error',
    'point.errorMsg':'Could not look up this point.',
    'point.selected':'Selected point',
    'point.outsideCoverage':'This point is outside the orthomosaic coverage, there is no data to report here.',
    'point.temperature':'Temperature',
    'point.vegetation':'Vegetation (NDVI)',
    'point.date':'Date',
    'point.confidence':'Confidence',
    'timebar.thisCapture':'this capture',
    'timebar.compareWith':'compare with:',
    'timebar.compareNote':'Pixel-by-pixel visual comparison between different missions is not available yet. For now, open each mission separately from the list to compare their situation summaries.',
    'mission.none':'No active mission',
    'mission.processingWord':'processing…',
    'export.generating':'⏳ Generating…',
    'export.failMap':'Could not export the map: ',
    'export.noLayers':'There is no visible layer to export. Turn on at least one layer in the panel.',
    'offline.title':'No connection to the base map.',
    'offline.body':"This mission's layers (orthomosaic, thermal, indices) still work. What's missing is the streets and satellite background, which comes from the internet.",
    'base.streets':'🗺️ Streets',
    'base.satellite':'🛰️ Satellite',
    'layer.dragTitle':'Drag to reorder',
    'layer.soloTitle':'Show only this layer',
    'layer.zoomTitle':'Fit to this layer',
    'layer.legendTitle':'Show description and legend',
    'layer.opacity':'Opacity',
    'layer.whatItMeans':'What it means:',
    'addms.vegIndicesTitle':'🌿 Vegetation indices',
    'addms.includedPending':'Multispectral flight included. NDVI/GNDVI/NDRE/MSAVI2 will appear here once reconstruction finishes.',
    'addms.notYet':'This mission does not have a multispectral flight (M3M) yet. Without it there is no NDVI/GNDVI/NDRE/MSAVI2 to show.',
    'addms.thermalIncludedPending':'Thermal flight included. Hotspots will appear here once reconstruction finishes.',
    'addms.thermalNotYet':'This mission does not have a thermal flight yet. Without it there is no temperature to classify.',
    'addms.ctaTitle':'🌿 Add multispectral',
    'addms.ctaDetail':'Turns on automatic vegetation indices (NDVI/GNDVI/NDRE/MSAVI2).',
    'addms.ctaLink':'➕ Add multispectral flight',
    'layer.hillshade':'⛰️ Relief (DSM)',
    'layer.rgb':'📷 RGB',
    'layer.dband':'📷 Visible (D band)',
    'layer.msComposite':'🎨 Multispectral (composite)',
    'layer.thermal':'🌡️ Thermal',
    'layer.hotspot':'♨️ Thermal hotspot',
    'layer.flightPath':'🛩️ Flight path',
    'layer.hull':'🔷 Convex hull',
    'layer.ndviClass':'🌿 NDVI classified',
    'layer.gndviClass':'🌾 GNDVI classified',
    'layer.ndreClass':'🍃 NDRE classified',
    'layer.msavi2Class':'🌱 MSAVI2 classified',
    'layer.areaExperto':'📌 Burned area (Official / Expert)',
    'layer.areaDetectada':'💡 Suggested thermal mask',
    'sensor.rgb':'RGB',
    'sensor.thermal':'Thermal',
    'sensor.multispectral':'Multispectral',
    'group.impacto':'🔥 Fire impact',
    'group.indices':'🌿 Indices',
    'group.opticas':'📷 Optical',
    'group.termicas':'🌡️ Thermal',
    'group.terreno':'⛰️ Terrain',
    'group.vuelo':'🛩️ Flight',
    'channel.rgb':'RGB',
    'channel.thermal':'Thermal',
    'channel.ms':'Multispectral',
    'channel.waiting':'Waiting…',
    'channel.failed':'Failed',
    'channel.prep':'Preparation',
    'channel.recon3d':'3D reconstruction',
    'channel.trimExport':'Trim + export',
    'channel.trimIndices':'Trim + indices',
    'progress.cancel':'Cancel',
    'progress.cancelConfirm':"Cancel the processing that's running? What's been done so far in this run will be lost.",
    'progress.cancelling':'Cancelling…',
    'progress.cancelFailedGeneric':'could not cancel',
    'progress.cancelFailedAlert':'Could not cancel: ',
    'progress.processing':'Processing…',
    'progress.complete':'Processing complete',
    'progress.viewFullLog':'View full log',
    'progress.code':'code',
    'legend.resolution':'Resolution',
    'legend.hillshadeBody':'<p>Hillshade computed from the <b>DSM</b> (digital <i>surface</i> model): it includes vegetation and structures, it is not a bare terrain model (DTM). Visual reference only, with no units.</p>',
    'legend.channels':'Channels',
    'legend.custom':'Custom',
    'legend.msCompositeBody':'<p>RGB composite built in the browser by combining 3 raw spectral bands. There is no fixed file per combination, changing the selection recomposes it on the fly.</p>',
    'legend.range':'Range',
    'legend.to':'to',
    'legend.dbandSensorLine':'DJI M3M, D camera (RGB)',
    'legend.dbandBody':'<p>Fast visible mosaic, computed from the M3M own RGB camera, a separate sensor from the 4 spectral bands (G/R/RE/NIR), not co-aligned with them. Meant for a first visual look, it does not replace the RGB orthomosaic from the M3T/H20T flight if this mission also has one.</p>',
    'legend.hotspotBody1':'<p>ABSOLUTE temperature (not a relative anomaly: a relative threshold gives false positives on ground/crops heated by the sun). The "active hotspot" cutoff (88°C/190°F) is the operational threshold cited in drone hotspot detection literature for "active fire below the surface." Operational use: risk of reignition / mop-up.</p>',
    'legend.hotspotBody2':'<p>Shown over the <b>entire</b> thermal coverage, without clipping to any polygon.</p>',
    'hotspot.classNormal':'Normal',
    'hotspot.classElevated':'Elevated',
    'hotspot.classHot':'Hot',
    'hotspot.classActive':'Active hotspot',
    'idx.ndvi.label':'🌿 NDVI',
    'idx.ndvi.desc':'Vegetation health/vigor',
    'idx.gndvi.label':'🌾 GNDVI',
    'idx.gndvi.desc':'Sensitive to chlorophyll',
    'idx.ndre.label':'🍃 NDRE',
    'idx.ndre.desc':'Stress in dense canopy',
    'idx.msavi2.label':'🌱 MSAVI2',
    'idx.msavi2.desc':'NDVI corrected for soil brightness, more reliable than NDVI in sparse canopy/post-fire regrowth',
    'idxclass.ndviClass.desc':'Standard USGS cutoffs.',
    'idxclass.gndviClass.desc':'Standard agricultural remote sensing cutoffs.',
    'idxclass.ndreClass.desc':'Standard foliar nitrogen cutoffs (precision agriculture).',
    'idxclass.msavi2Class.desc':'NDVI corrected for soil brightness (Qi et al. 1994), more reliable than NDVI in sparse canopy (post-fire regrowth, cover &lt;30%). Same cutoffs as NDVI (see the classify_vegetation_indices.py docstring: MSAVI2 has no well-established convention of its own, so NDVI cutoffs are reused as a starting point).',
    'idxclass.noVeg':'No vegetation',
    'idxclass.sparseStressed':'Sparse/stressed',
    'idxclass.denseHealthy':'Dense and healthy',
    'idxclass.severeStress':'Severe stress',
    'idxclass.moderateStressed':'Moderate/stressed',
    'idxclass.healthy':'Healthy',
    'idxclass.nDeficiency':'N deficiency',
    'idxclass.transition':'Transition',
    'idxclass.healthy2':'Healthy',
    'idxclass.optimalMature':'Optimal/mature',
    'ms.presetCir':'False color IR (R=NIR G=Red B=Green)',
    'ms.presetRededge':'RedEdge (R=NIR G=RedEdge B=Red)',
    'ms.bandRed':'Red (spectral)',
    'ms.bandGreen':'Green (spectral)',
    'ms.bandNir':'NIR',
    'ms.bandRededge':'RedEdge',
    'ms.dbandRed':'Red (D band RGB)',
    'ms.dbandGreen':'Green (D band RGB)',
    'ms.dbandBlue':'Blue (D band RGB)',
    'hint.hotspot_termico':'Dark red: active hotspot (≥88°C), risk of reignition, needs attention. Orange/yellow: elevated temperature, monitor.',
    'hint.ndvi_class':'Green: dense and healthy vegetation. Yellow: sparse or stressed, monitor how it evolves. Brown: no vegetation cover.',
    'hint.gndvi_class':'Green: healthy vegetation. Yellow: moderate stress. Red: severe stress, possible damage from heat or lack of water.',
    'hint.ndre_class':'Dark green: optimal. Light green: healthy. Orange/red: deficiency, needs attention soon.',
    'hint.msavi2_class':'Green: dense and healthy vegetation. Yellow: sparse or in early regrowth. Brown: no cover.',
    'hint.ndvi':'Green = healthy, dense vegetation. Red/brown = bare soil or heavily stressed vegetation.',
    'hint.gndvi':'Green = healthy vegetation. Red = severe stress, possible damage.',
    'hint.ndre':'Green = healthy foliage. Red = deficiency, needs attention soon.',
    'hint.msavi2':'Green = dense vegetation. Brown = no cover or exposed soil.',
    'hint.rgb':'Real color image from the flight, a direct visual reference of the terrain.',
    'hint.ms_composite':'Composite of spectral bands, brings out vegetation contrasts not visible to the naked eye.',
    'hint.thermal':'Surface temperature scale. Hotter (warm colors) can indicate residual thermal activity.',
    'hint.hillshade':'Terrain relief, helps locate slopes and access routes, with no thermal meaning.',
    'hint.flight_path':'The drone real path during capture, useful for checking flight coverage.',
  },
  es:{
    'doc.title':'Situación de la misión',
    'topbar.skipLink':'Saltar al panel de situación',
    'topbar.back':'Volver a las misiones',
    'topbar.loading':'Cargando…',
    'topbar.dateTitle':'Fecha de la captura mostrada',
    'topbar.captureLabel':'Captura: ',
    'topbar.reportBtn':'Generar resumen',
    'topbar.help':'Ayuda',
    'help.howTo':'Cómo usar',
    'help.rowSolo':'Solo: muestra únicamente esta capa',
    'help.rowClick':'Clic en el mapa: ver qué significa ese punto',
    'help.rowMeasure':'Medir distancia/área',
    'help.rowTheme':'Cambiar tema claro/oscuro',
    'help.rowEsc':'Cerrar lo que esté abierto',
    'help.accessibility':'Accesibilidad',
    'help.textSize':'Tamaño de texto',
    'help.textSmall':'Texto pequeño',
    'help.textNormal':'Texto normal',
    'help.textLarge':'Texto grande',
    'help.highContrast':'Alto contraste',
    'help.reduceMotion':'Reducir movimiento',
    'help.theme':'Tema',
    'a11y.themeToggle':'Cambiar tema',
    'a11y.language':'Idioma',
    'rail.stationLabel':'Identidad de la estación',
    'rail.context':'ANÁLISIS<br>DE CAMPO',
    'rail.footer':'SIG<br>01',
    'map.areaLabel':'Mapa de la zona',
    'map.fitBounds':'Encuadrar toda la misión',
    'point.cardAria':'Detalle del punto seleccionado',
    'measure.title':'Medición',
    'measure.clear':'Limpiar',
    'measure.distance':'Distancia',
    'measure.area':'Área',
    'measure.hint':'Clic para agregar puntos · doble clic para cerrar el área · Esc para salir',
    'panel.toggleAria':'Mostrar panel de situación',
    'panel.kicker':'Informe de misión',
    'panel.heading':'Evaluación del incidente',
    'panel.closeAria':'Ocultar panel',
    'panel.sub':'Hallazgos priorizados para coordinar la respuesta.',
    'summary.loading':'Cargando datos de la misión…',
    'ph.stageInit':'Iniciando…',
    'ph.collapseAria':'Colapsar progreso',
    'ph.expandAria':'Expandir progreso',
    'ph.viewLogWord':' Ver log',
    'ph.hideLog':' Ocultar log',
    'sig.kicker':'SIG',
    'sig.heading':'Capas y herramientas',
    'sig.sub':'Catálogo de capas, comparar y medir.',
    'sig.drawBtn':'Remodelar',
    'sig.drawTitle':'Remodelar y delimitar área afectada (D)',
    'draw.title':'Remodelar y Delimitar (QGIS)',
    'draw.modeReshape':'Remodelar',
    'draw.modeCut':'Recortar',
    'draw.modeAdd':'Añadir',
    'draw.modeNew':'Nuevo',
    'draw.freehandLabel':'Mano alzada activa',
    'draw.assistBtn':'Sugerencia',
    'draw.smoothBtn':'Suavizar',
    'draw.undoBtn':'Deshacer',
    'draw.clearBtn':'Limpiar',
    'draw.saveBtn':'Guardar como Área Oficial',
    'draw.hint':'Traza una línea que cruce el polígono para remodelar su contorno (estilo QGIS)',
    'sig.compareBtn':'Comparar',
    'sig.compareTitle':'Comparar dos capas lado a lado (C)',
    'sig.measureBtn':'Medir',
    'sig.measureTitle':'Medir distancia y área (M)',
    'sig.exportBtn':'Exportar',
    'sig.exportTitle':'Descargar una imagen del mapa actual',
    'sig.hideAll':'Ocultar todo',
    'sig.hideAllTitle':'Apagar todas las capas',
    'sig.reset':'Restablecer',
    'sig.resetTitle':'Volver a la vista inicial',
    'timebar.title':'Comparar en el tiempo',
    'report.title':'Resumen de situación',
    'report.closeAria':'Cerrar',
    'report.download':'Descargar imagen',
    'report.loading':'Cargando…',
    'report.loadError':'No se pudieron cargar los datos de la misión.',
    'report.noImpactCard':'Sin datos de impacto, esta misión no tiene térmico',
    'report.activeHotspots':'Focos activos',
    'report.maxTemp':'Temp. máxima',
    'report.avgTemp':'Temp. promedio',
    'report.generating':'Generando…',
    'report.imgAlt':'Resumen de situación de la misión',
    'report.imgError':'No se pudo generar la imagen. Activa al menos una capa en el mapa.',
    'report.urgentCritical':'⚠ Riesgo de reactivación',
    'report.urgentGood':'✓ Sin anomalías críticas',
    'report.urgentNone':'Sin datos de impacto',
    'report.flightQualityLabel':'Calidad del vuelo: ',
    'report.noImpactDataYet':'Sin datos de impacto todavía',
    'reco.noThermal':'Esta misión no tiene datos térmicos para resumir.',
    'reco.label':'Recomendación',
    'val.buena':'Buena',
    'val.regular':'Regular',
    'val.baja':'Baja',
    'val.alta':'Alta',
    'val.media':'Media',
    'report.defaultEquipment':'Dron UAV',
    'summary.activeHotspots':'Focos térmicos activos',
    'summary.reignitionRisk':'Riesgo de reactivación',
    'summary.noneDetected':'Ninguno detectado',
    'summary.maxAbbr':'máx',
    'summary.avgAbbr':'prom',
    'summary.lastCapture':'Última captura',
    'summary.surveyQuality':'Calidad del levantamiento',
    'summary.noDataYet':'Sin datos todavía',
    'summary.overlap':'solape ~',
    'summary.flightAt':'vuelo a',
    'summary.reconstructedPct':'% de fotos reconstruidas',
    'summary.noImpactTitle':'Sin datos de impacto todavía',
    'summary.noImpactSub':'Esta misión no tiene térmico, o la corrida no llegó a esa etapa.',
    'freshness.updated':'Actualizado',
    'freshness.justNow':'recién',
    'freshness.minAgo':'min',
    'freshness.hAgo':'h',
    'freshness.lowCoverage':'⚠ cobertura baja vs. área volada',
    'freshness.noData':'Sin datos de situación todavía',
    'hotspot.identifiedLabel':'Focos identificados',
    'hotspot.rowLabel':'Foco',
    'point.looking':'Consultando…',
    'point.error':'Error',
    'point.errorMsg':'No se pudo consultar este punto.',
    'point.selected':'Punto seleccionado',
    'point.outsideCoverage':'Este punto está fuera de la cobertura de los ortomosaicos, sin dato que reportar acá.',
    'point.temperature':'Temperatura',
    'point.vegetation':'Vegetación (NDVI)',
    'point.date':'Fecha',
    'point.confidence':'Confianza',
    'timebar.thisCapture':'esta captura',
    'timebar.compareWith':'comparar con:',
    'timebar.compareNote':'La comparación visual pixel a pixel entre misiones distintas todavía no está disponible. Por ahora, abre cada misión por separado desde el listado para comparar sus resúmenes de situación.',
    'mission.none':'Sin misión activa',
    'mission.processingWord':'procesando…',
    'export.generating':'⏳ Generando…',
    'export.failMap':'No se pudo exportar el mapa: ',
    'export.noLayers':'No hay ninguna capa visible para exportar. Activa al menos una capa en el panel.',
    'offline.title':'Sin conexión al mapa base.',
    'offline.body':'Las capas de esta misión (ortomosaico, térmico, índices) se ven igual. Lo que falta es el fondo de calles y satelital, que viene de internet.',
    'base.streets':'🗺️ Calles',
    'base.satellite':'🛰️ Satélite',
    'layer.dragTitle':'Arrastrar para reordenar',
    'layer.soloTitle':'Ver solo esta capa',
    'layer.zoomTitle':'Encuadrar esta capa',
    'layer.legendTitle':'Ver descripción y leyenda',
    'layer.opacity':'Opacidad',
    'layer.whatItMeans':'Qué significa:',
    'addms.vegIndicesTitle':'🌿 Índices de vegetación',
    'addms.includedPending':'Vuelo multiespectral incluido. NDVI/GNDVI/NDRE/MSAVI2 van a aparecer acá cuando termine la reconstrucción.',
    'addms.notYet':'Esta misión no tiene vuelo multiespectral (M3M) todavía. Sin él no hay NDVI/GNDVI/NDRE/MSAVI2 que mostrar.',
    'addms.thermalIncludedPending':'Vuelo térmico incluido. Los focos de calor van a aparecer acá cuando termine la reconstrucción.',
    'addms.thermalNotYet':'Esta misión no tiene vuelo térmico todavía. Sin él no hay temperatura que clasificar.',
    'addms.ctaTitle':'🌿 Agregar multiespectral',
    'addms.ctaDetail':'Habilita índices de vegetación automáticos (NDVI/GNDVI/NDRE/MSAVI2).',
    'addms.ctaLink':'➕ Agregar vuelo multiespectral',
    'layer.hillshade':'⛰️ Relieve (DSM)',
    'layer.rgb':'📷 RGB',
    'layer.dband':'📷 Visible (banda D)',
    'layer.msComposite':'🎨 Multiespectral (compuesto)',
    'layer.thermal':'🌡️ Térmico',
    'layer.hotspot':'♨️ Hotspot térmico',
    'layer.flightPath':'🛩️ Ruta de vuelo',
    'layer.hull':'🔷 Casco convexo',
    'layer.ndviClass':'🌿 NDVI clasificado',
    'layer.gndviClass':'🌾 GNDVI clasificado',
    'layer.ndreClass':'🍃 NDRE clasificado',
    'layer.msavi2Class':'🌱 MSAVI2 clasificado',
    'layer.areaExperto':'📌 Área afectada (Oficial / Experto)',
    'layer.areaDetectada':'💡 Máscara térmica sugerida',
    'sensor.rgb':'RGB',
    'sensor.thermal':'Térmico',
    'sensor.multispectral':'Multiespectral',
    'group.impacto':'🔥 Impacto del incendio',
    'group.indices':'🌿 Índices',
    'group.opticas':'📷 Ópticas',
    'group.termicas':'🌡️ Térmicas',
    'group.terreno':'⛰️ Terreno',
    'group.vuelo':'🛩️ Vuelo',
    'channel.rgb':'RGB',
    'channel.thermal':'Térmico',
    'channel.ms':'Multiespectral',
    'channel.waiting':'Esperando…',
    'channel.failed':'Falló',
    'channel.prep':'Preparación',
    'channel.recon3d':'Reconstrucción 3D',
    'channel.trimExport':'Recorte + exportación',
    'channel.trimIndices':'Recorte + índices',
    'progress.cancel':'Cancelar',
    'progress.cancelConfirm':'¿Cancelar el procesamiento en curso? Lo hecho hasta ahora en esta corrida se pierde.',
    'progress.cancelling':'Cancelando…',
    'progress.cancelFailedGeneric':'no se pudo cancelar',
    'progress.cancelFailedAlert':'No se pudo cancelar: ',
    'progress.processing':'Procesando…',
    'progress.complete':'Procesamiento completo',
    'progress.viewFullLog':'Ver log completo',
    'progress.code':'código',
    'legend.resolution':'Resolución',
    'legend.hillshadeBody':'<p>Sombreado de relieve calculado sobre el <b>DSM</b> (modelo digital de <i>superficie</i>): incluye vegetación y construcciones, no es un modelo de terreno desnudo (DTM). Solo referencia visual, sin unidades.</p>',
    'legend.channels':'Canales',
    'legend.custom':'Personalizado',
    'legend.msCompositeBody':'<p>Composición RGB armada en el navegador combinando 3 bandas espectrales crudas. No hay un archivo fijo por combinación, cambiar la selección recompone al vuelo.</p>',
    'legend.range':'Rango',
    'legend.to':'a',
    'legend.dbandSensorLine':'DJI M3M, cámara D (RGB)',
    'legend.dbandBody':'<p>Mosaico visible rápido, calculado a partir de la cámara RGB propia del M3M, un sensor aparte de las 4 bandas espectrales (G/R/RE/NIR), no coalineado con ellas. Pensado para una primera mirada visual, no reemplaza al ortomosaico RGB del vuelo M3T/H20T si esta misión también lo tiene.</p>',
    'legend.hotspotBody1':'<p>Temperatura ABSOLUTA (no anomalía relativa: un umbral relativo da falsos positivos en suelo/cultivo calentado por el sol). El corte de "foco activo" (88°C/190°F) es el umbral operacional citado en literatura de detección de hotspots con drones para "fuego activo bajo superficie". Uso operacional: riesgo de reactivación / mop-up.</p>',
    'legend.hotspotBody2':'<p>Se muestra sobre <b>toda</b> la cobertura térmica, sin recortar a ningún polígono.</p>',
    'hotspot.classNormal':'Normal',
    'hotspot.classElevated':'Elevado',
    'hotspot.classHot':'Caliente',
    'hotspot.classActive':'Foco activo',
    'idx.ndvi.label':'🌿 NDVI',
    'idx.ndvi.desc':'Salud/vigor de vegetación',
    'idx.gndvi.label':'🌾 GNDVI',
    'idx.gndvi.desc':'Sensible a clorofila',
    'idx.ndre.label':'🍃 NDRE',
    'idx.ndre.desc':'Estrés en dosel denso',
    'idx.msavi2.label':'🌱 MSAVI2',
    'idx.msavi2.desc':'NDVI corregido por brillo de suelo, más confiable que NDVI en dosel disperso/regeneración post-incendio',
    'idxclass.ndviClass.desc':'Cortes estándar USGS.',
    'idxclass.gndviClass.desc':'Cortes estándar de teledetección agrícola.',
    'idxclass.ndreClass.desc':'Cortes estándar de nitrógeno foliar (agricultura de precisión).',
    'idxclass.msavi2Class.desc':'NDVI corregido por brillo de suelo (Qi et al. 1994), más confiable que NDVI en dosel disperso (regeneración post-incendio, cobertura &lt;30%). Mismos cortes que NDVI (ver docstring de classify_vegetation_indices.py: MSAVI2 no tiene convención propia tan establecida, se reusa la de NDVI como punto de partida).',
    'idxclass.noVeg':'Sin vegetación',
    'idxclass.sparseStressed':'Escasa/estresada',
    'idxclass.denseHealthy':'Densa y sana',
    'idxclass.severeStress':'Estrés severo',
    'idxclass.moderateStressed':'Moderada/estresada',
    'idxclass.healthy':'Sana',
    'idxclass.nDeficiency':'Deficiencia N',
    'idxclass.transition':'Transición',
    'idxclass.healthy2':'Saludable',
    'idxclass.optimalMature':'Óptimo/maduro',
    'ms.presetCir':'Falso color IR (R=NIR G=Red B=Green)',
    'ms.presetRededge':'RedEdge (R=NIR G=RedEdge B=Red)',
    'ms.bandRed':'Red (espectral)',
    'ms.bandGreen':'Green (espectral)',
    'ms.bandNir':'NIR',
    'ms.bandRededge':'RedEdge',
    'ms.dbandRed':'Red (RGB banda D)',
    'ms.dbandGreen':'Green (RGB banda D)',
    'ms.dbandBlue':'Blue (RGB banda D)',
    'hint.hotspot_termico':'Rojo oscuro: foco activo (≥88°C), riesgo de reactivación, requiere atención. Naranja/amarillo: temperatura elevada, monitorear.',
    'hint.ndvi_class':'Verde: vegetación densa y sana. Amarillo: escasa o estresada, vigilar evolución. Café: sin cobertura vegetal.',
    'hint.gndvi_class':'Verde: vegetación sana. Amarillo: estrés moderado. Rojo: estrés severo, posible daño por calor o falta de agua.',
    'hint.ndre_class':'Verde oscuro: óptimo. Verde claro: saludable. Naranja/rojo: deficiencia, atención en el corto plazo.',
    'hint.msavi2_class':'Verde: vegetación densa y sana. Amarillo: escasa o en regeneración temprana. Café: sin cobertura.',
    'hint.ndvi':'Verde = vegetación sana y densa. Rojo/café = suelo desnudo o vegetación muy estresada.',
    'hint.gndvi':'Verde = vegetación sana. Rojo = estrés severo, posible daño.',
    'hint.ndre':'Verde = follaje saludable. Rojo = deficiencia, atención en el corto plazo.',
    'hint.msavi2':'Verde = vegetación densa. Café = sin cobertura o suelo expuesto.',
    'hint.rgb':'Imagen a color real del vuelo, referencia visual directa del terreno.',
    'hint.ms_composite':'Composición de bandas espectrales, realza contrastes de vegetación no visibles a simple vista.',
    'hint.thermal':'Escala de temperatura de superficie, más caliente (colores cálidos) puede indicar actividad térmica residual.',
    'hint.hillshade':'Relieve del terreno, ayuda a ubicar pendientes y accesos, sin significado térmico.',
    'hint.flight_path':'Recorrido real del dron durante la captura, útil para verificar cobertura del vuelo.',
  },
};
let LANG='en';
function t(key){ return (I18N[LANG]&&I18N[LANG][key]) || I18N.en[key] || key; }
// Traduce un VALOR de dato (no una clave de UI): fq.calidad/d.confianza
// llegan del backend en español ('buena'/'regular'/'baja'/'alta'/'media',
// ver compute_flight_quality.py y el endpoint /sample) — esto los muestra
// en el idioma activo sin tocar el dato real ni el resto del código que
// compara contra esos mismos valores.
function localizeValue(raw){
  if(raw==null||raw==='')return raw;
  const key='val.'+String(raw).toLowerCase();
  return (I18N.en[key]!==undefined)?t(key):raw;
}

// ═══════════════════════════════════════════════════════════════════
// ACTIVAR LA MISIÓN DE LA URL: antes de leer nada más
// ═══════════════════════════════════════════════════════════════════
// /view/{mission} (la entrada normal desde la webapp) activa los symlinks
// y RECIÉN AHÍ redirige acá, pero un F5 sobre esta misma URL ya redirigida
// es un GET directo a un archivo estático, nunca vuelve a pasar por
// /view/. Si entre medio se activó otra misión (o el contenedor arrancó de
// cero), esta página leía bounds.json de lo que sea que estuviera activo
// en ESE momento, no necesariamente la de la URL. Con ?mission= presente
// se le pide al servidor reactivarla, sincrónico, ANTES del fetch de
// bounds.json de más abajo (si no hay ?mission=, no hay nada que activar:
// geovisor abierto suelto, se deja como estaba).
const urlMission=new URLSearchParams(location.search).get('mission');
if(urlMission){
  try{
    const act=new XMLHttpRequest();
    act.open('POST',`/api/missions/${encodeURIComponent(urlMission)}/activate`,false);
    act.send(null);
  }catch(e){}
}

// ═══════════════════════════════════════════════════════════════════
// MISIÓN ACTUAL — nombre resuelto una sola vez, todo lo demás lo espera
// ═══════════════════════════════════════════════════════════════════
// Si se llegó acá con ?mission= (arranque en vivo o "Ver geovisor" desde
// la webapp — el único flujo normal) se usa ese nombre directo, no depende
// de qué symlink esté activo en el servidor en este instante. Sin el
// parámetro (geovisor abierto suelto/recargado desde un bookmark viejo) se
// cae a la misión "activa" o, si no hay ninguna corriendo, la última con
// tiles — para no dejar la pantalla completamente huérfana.
// Declarado ACÁ (junto a urlMission), no más abajo donde se usa por primera
// vez (labelMission()): applyLang(), junto al tema más abajo, ya llama a
// labelMission()/checkRelatedMissions() (que hacen `await missionReady`) en
// su primera pasada — con missionReady declarado más abajo con `const`, esa
// lectura temprana revienta con "Cannot access before initialization",
// mismo bug real que liveMsBandIds/SITUATION/I18N, ver sus comentarios.
let CURRENT_MISSION=urlMission||null;
const missionReady=(async()=>{
  if(CURRENT_MISSION)return CURRENT_MISSION;
  try{
    const d=await (await fetch('/api/missions',{cache:'no-store'})).json();
    if(d.active){CURRENT_MISSION=d.active;return CURRENT_MISSION;}
    const conTiles=(d.missions||[]).filter(m=>m.has_tiles);
    if(conTiles.length){CURRENT_MISSION=conTiles[conTiles.length-1].name;return CURRENT_MISSION;}
  }catch(e){}
  return null;
})();

// ═══════════════════════════════════════════════════════════════════
// bounds.json (por misión)
// ═══════════════════════════════════════════════════════════════════
// Centro/zoom por defecto (fallback si tiles/bounds.json no existe todavía,
// p.ej. corridas viejas sin regenerar tiles, o el geovisor se abrió sin
// ?mission= y sin ninguna misión activada nunca en este contenedor). Cada
// misión real tiene su propia ubicación, bounds.json lo calcula
// generate_tiles.py desde el centro real del ortomosaico.
let CENTER=[6.3619,-75.5465],ZOOM=17;
let THERMAL_MIN=15,THERMAL_MAX=55;   // fallback (vuelo original, rango amplio)
let INDEX_RANGES={};   // {} si la misión no tiene datos multiespectrales (M3M)
let MS_BAND_RANGES={};  // {} si la misión no tiene datos multiespectrales (M3M)
let RESOLUCION_CM=null; // cm/px MEDIDOS por producto (bounds.json)
// Qué capas TIENEN tiles de verdad (generate_tiles.py, campo capas_disponibles
// de bounds.json): de acá salen hotspot/índices clasificados. Antes
// se registraban sin condición y el panel ofrecía capas de una misión sin
// multiespectral (o sin térmico) que no tenían ningún tile detrás.
let CAPAS_DISPONIBLES=new Set();
// `preliminary` lo escribe export_flight_path.py cuando la corrida TODAVÍA
// está en curso y lo único que hay es la ruta de vuelo: el visor se abre
// igual (sirve desde el minuto uno) pero sabe que faltan productos y avisa
// cuando aparecen. generate_tiles.py lo pisa sin la marca al terminar.
let PRELIMINARY=false;
// true si el vuelo INCLUYE fotos multiespectrales, aunque ODM todavía no haya
// calculado ningún índice. Se saca del tally de flight_path.geojson (arma la
// ruta de vuelo desde el EXIF de TODAS las fotos, incluidas las MS, antes de
// que arranque la reconstrucción) — es la señal más temprana posible de que
// hay M3M en esta misión. Sin esto, "¿tiene multiespectral?" se confundía con
// "¿YA terminó de calcular los índices?": la caja "Agregar multiespectral"
// (pensada para una misión que arrancó SIN M3M) aparecía igual en una misión
// que sí lo tiene, mientras la reconstrucción seguía en curso.
let hasMsInput=false;
let hasThermalInput=false;
// true si esta carga inicial YA encontró un centro real (bounds.json existía,
// aunque sea la versión "preliminary" de export_flight_path.py). Si queda en
// false, es que se abrió el geovisor en la ventana de pocos segundos ANTES de
// que ese archivo exista siquiera. pollBoundsForChanges() recentra una sola
// vez apenas aparezca, en vez de dejar el mapa pegado en el respaldo fijo.
let boundsWasReal=false;
// Token de caché de los tiles (generate_tiles.py lo escribe fresco en CADA
// corrida, incluida cada pasada de publish_partial) — se agrega como
// ?v=TILES_V a toda URL de tile (ver las capas más abajo y los Grid
// personalizados). "Corregir y reintentar" sobre la MISMA misión reescribe
// los tiles en el MISMO path; sin esto, un navegador que ya los haya
// pedido antes se queda con la respuesta vieja aunque Cache-Control ya no
// sea "immutable" — cambiar la URL entera es la única forma de garantizar
// el refetch sin depender de que el caché revalide bien. Bug real,
// reportado en vivo: tiles viejos (mosaico más recortado) después de un
// reintento, en algunos niveles de zoom.
let TILES_V=0;
try{
  const req=new XMLHttpRequest();
  req.open('GET','tiles/bounds.json?t='+Date.now(),false);
  req.send(null);
  if(req.status===200){
    const b=JSON.parse(req.responseText);
    if(b.center){CENTER=b.center;boundsWasReal=true;}
    if(b.zoom)ZOOM=b.zoom;
    if(b.thermal_range){THERMAL_MIN=b.thermal_range[0];THERMAL_MAX=b.thermal_range[1];}
    if(b.index_ranges)INDEX_RANGES=b.index_ranges;
    if(b.ms_band_ranges)MS_BAND_RANGES=b.ms_band_ranges;
    if(b.resolucion_cm)RESOLUCION_CM=b.resolucion_cm;
    if(b.capas_disponibles)CAPAS_DISPONIBLES=new Set(b.capas_disponibles);
    if(b.tiles_v)TILES_V=b.tiles_v;
    PRELIMINARY=!!b.preliminary;
  }
}catch(e){}
// Plantilla de URL de tile con el token de caché — usada tanto por las
// capas L.tileLayer estándar (rgb/dband/hillshade) como por el img.src de
// los Grid personalizados de más abajo (esos leen TILES_V directo en cada
// createTile(), así que ya quedan al día solos con cada redraw()).
function tileTpl(name){ return `tiles/${name}/{z}/{x}/{y}.png?v=${TILES_V}`; }

// ═══════════════════════════════════════════════════════════════════
// MAP + PANES
// ═══════════════════════════════════════════════════════════════════
const map=L.map('map',{center:CENTER,zoom:ZOOM,maxZoom:21,zoomControl:false,
  attributionControl:{position:'bottomleft',prefix:false}});
// Columna única y discreta (zoom + encuadrar), abajo a la derecha, no el
// default de Leaflet arriba a la izquierda, que en el diseño nuevo queda
// tapado por el selector Mapa/Lista.
L.control.zoom({position:'bottomright'}).addTo(map);
const FitBoundsControl=L.Control.extend({
  options:{position:'bottomright'},
  onAdd:function(){
    const el=L.DomUtil.create('div','leaflet-bar');
    const btn=L.DomUtil.create('a','map-extra-control',el);
    btn.href='#';btn.title=t('map.fitBounds');btn.setAttribute('aria-label',t('map.fitBounds'));
    btn.innerHTML='⤢';
    L.DomEvent.on(btn,'click',L.DomEvent.stop).on(btn,'click',()=>{
      if(MISSION_BOUNDS)map.fitBounds(MISSION_BOUNDS,{padding:[40,40]});else map.setView(CENTER,ZOOM);
    });
    return el;
  }
});
new FitBoundsControl().addTo(map);

// Cada capa de dato (no los mapas base) recibe su propio pane con un
// zIndex explícito: es lo que permite que el reordenamiento por arrastre
// del panel "Capas" cambie el orden VISUAL real en el mapa. Compartir el
// tilePane por defecto (como antes) solo permite z-order = orden de
// addTo(), que no se puede reordenar después de agregado.
const INDEX_NAMES=Object.keys(INDEX_RANGES);
const MS_BAND_IDS=Object.keys(MS_BAND_RANGES);
// Copia actualizable de MS_BAND_IDS: pollBoundsForChanges() la reasigna
// cuando el multiespectral de una misión EN CURSO aparece más tarde (ver
// registerMsComposite()/registerHotspot()). Declarada acá, junto a
// MS_BAND_IDS, y NO más abajo en el archivo. El registro inicial de capas
// (registerHotspot() etc.) llama a `def.legend()` de forma SÍNCRONA al armar
// el panel de Capas por primera vez (renderCapasPanel(), unas líneas antes de
// donde esto vivía), y con `let` en la zona muerta temporal esa lectura
// temprana tira "Cannot access before initialization": una excepción sin
// capturar que corta la ejecución del script ahí mismo y deja el resto de la
// inicialización (incluido renderSummaryCards()) sin correr nunca. El panel
// de "Situación actual" se queda pegado en "Cargando datos de la misión…"
// para siempre. Bug real, encontrado corriendo la página real en jsdom.
let liveMsBandIds=[...MS_BAND_IDS];
// flight_path: layersPanelHTML() filtra por
// layerOrder, no por LAYER_REGISTRY directo — una capa que se
// registra (sync o vía tryLoadFlightPath()) pero nunca entra a este array
// se dibuja en el mapa igual (el loop de defaultOn de más abajo no depende
// de layerOrder) pero no existía ni en el sidebar ni en la leyenda flotante.
// TOP → BOTTOM (coincide exactamente con el orden visual del panel de Capas).
// El elemento en el índice 0 está arriba de todo en el panel y tiene el zIndex
// más alto (se renderiza encima de todas las demás capas en Leaflet).
let layerOrder=[
  'flight_path',
  'hull_rgb','hull_thermal','hull_multispectral',
  'area_afectada_experto','area_afectada_detectada','hotspot_termico',
  'ndvi_class','gndvi_class','ndre_class','msavi2_class',
  ...INDEX_NAMES,
  'thermal',
  ...(MS_BAND_IDS.length?['ms_composite']:[]),'rgb','dband',
  'hillshade'
];
function applyLayerOrder(){
  const total=layerOrder.length;
  layerOrder.forEach((id,i)=>{
    let pane=map.getPane('pane-'+id);
    if(!pane)pane=map.createPane('pane-'+id);
    pane.style.zIndex=210+(total-1-i)*10;
  });
}
applyLayerOrder();

// "Calles": Reemplazado CARTO por OpenTopoMap (OSM Terrain) sin key
const osmBase=L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{
  maxNativeZoom: 17, maxZoom:21,subdomains:'abc',
  attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> · © <a href="https://opentopomap.org" target="_blank" rel="noopener">OpenTopoMap</a>',
}).addTo(map);
const satBase=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
  maxZoom:19,attribution:'© Esri, Maxar, Earthstar Geographics',
});
// La satelital de Esri viene SIN nombres de calles/veredas. Para orientarse
// (rutas de acceso, poblados cercanos) hace falta la capa de referencia
// (etiquetas + vías, transparente) encima. Se agrega/saca junto con la base,
// nunca sola — ver wireLayersPanel(), selector de mapa base.
const satLabels=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,pane:'overlayPane'});
let currentBase='osm';

// ── Sin conexión ────────────────────────────────────────────────────
// Leaflet y todo el geovisor se sirven desde la imagen (ver vendor/ en
// index.html), así que las capas de ESTA misión —que son las que importan—
// se ven igual sin internet. Los mapas base NO se pueden empaquetar (son
// tiles de todo el mundo, de terceros): sin conexión el fondo queda gris.
// Un fondo gris mudo se lee como "el geovisor está roto"; se dice qué pasa
// y qué sigue funcionando. Se avisa UNA vez, con varios tiles fallados —
// un 404 suelto en el borde del área es normal y no significa nada.
let _tilesBaseFallidos=0, _avisoOfflineDado=false;
function avisarSiSinMapaBase(){
  if(_avisoOfflineDado || ++_tilesBaseFallidos < 6) return;
  _avisoOfflineDado=true;
  const d=document.createElement('div');
  d.className='aviso-offline';
  d.setAttribute('role','status');
  d.innerHTML=`<b>${t('offline.title')}</b> ${t('offline.body')}`
            + `<button class="reset" aria-label="${t('report.closeAria')}"><svg class="ic" style="width:14px;height:14px" aria-hidden="true"><use href="#i-x"/></svg></button>`;
  d.querySelector('button').addEventListener('click',()=>d.remove());
  document.body.appendChild(d);
}
for(const capa of [osmBase,satBase,satLabels]) capa.on('tileerror',avisarSiSinMapaBase);

const rgbLayer=L.tileLayer(tileTpl('rgb'),{maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:1,pane:'pane-rgb'}).addTo(map);

// Banda D del M3M (mosaico visible rápido, opt-in) — a diferencia de
// rgbLayer, NO se agrega al mapa ni se registra de entrada: la mayoría de
// las misiones no la tienen. Se registra recién si aparece en
// capas_disponibles (ver registerDband(), mismo patrón que hotspot más
// abajo).
const dbandLayer=L.tileLayer(tileTpl('dband'),{maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:1,pane:'pane-dband'});

// ── RGB con orden de canales configurable ('normal' o personalizado
// r/g/b→cualquier canal fuente), mismo tile 'rgb' de siempre, remapeado en
// canvas client-side. channelOrder=['r','g','b'] es la identidad (igual que
// rgbLayer de arriba); se usa solo cuando el usuario elige un orden
// personalizado, para no pagar el costo de canvas en el caso normal. ──
const RgbSwizzleGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y,order=this.options.channelOrder||['r','g','b'];const CH={r:0,g:1,b:2};img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data;const src=new Uint8ClampedArray(d);for(let i=0;i<d.length;i+=4){d[i]=src[i+CH[order[0]]];d[i+1]=src[i+CH[order[1]]];d[i+2]=src[i+CH[order[2]]];}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/rgb/${z}/${x}/${y}.png?v=${TILES_V}`;return t;}});
let rgbCustomLayer=null; // instanciada bajo demanda, ver setRgbChannelOrder()
let rgbChannelOrder=['r','g','b'];
function setRgbChannelOrder(order){
  rgbChannelOrder=order;
  const isIdentity=order[0]==='r'&&order[1]==='g'&&order[2]==='b';
  const onMap=map.hasLayer(LAYER_REGISTRY.rgb.layer);
  const opacity=LAYER_REGISTRY.rgb.layer.options.opacity;
  if(onMap)map.removeLayer(LAYER_REGISTRY.rgb.layer);
  if(isIdentity){
    LAYER_REGISTRY.rgb.layer=rgbLayer;
  }else{
    if(!rgbCustomLayer)rgbCustomLayer=new RgbSwizzleGrid({channelOrder:order,maxZoom:21,maxNativeZoom:20,minZoom:14,pane:'pane-rgb'});
    else rgbCustomLayer.options.channelOrder=order;
    rgbCustomLayer.setOpacity(opacity);
    rgbCustomLayer.redraw();
    LAYER_REGISTRY.rgb.layer=rgbCustomLayer;
  }
  if(onMap)LAYER_REGISTRY.rgb.layer.addTo(map);
}

// ── Compositor multi-banda para el multiespectral: arma un RGB en canvas
// combinando 3 bandas espectrales crudas (una por canal), sin pre-generar
// cada combinación posible en disco: el usuario elige presets (falso color
// IR, RedEdge) o una combinación personalizada desde el panel de capas. ──
const BandCompositeGrid=L.GridLayer.extend({createTile:function(coords,done){
  const t=document.createElement('canvas');t.width=256;t.height=256;
  const ctx=t.getContext('2d');
  const z=coords.z,x=coords.x,y=coords.y;
  const bands=[this.options.bandR,this.options.bandG,this.options.bandB];
  const ranges=[MS_BAND_RANGES[bands[0]],MS_BAND_RANGES[bands[1]],MS_BAND_RANGES[bands[2]]];
  const imgs=[new Image(),new Image(),new Image()];
  let loaded=0,failed=false;
  function combine(){
    const datas=imgs.map(img=>{const c=document.createElement('canvas');c.width=256;c.height=256;const cx=c.getContext('2d');cx.drawImage(img,0,0);return cx.getImageData(0,0,256,256).data;});
    const out=ctx.createImageData(256,256);
    for(let i=0;i<out.data.length;i+=4){
      out.data[i]=datas[0][i];out.data[i+1]=datas[1][i];out.data[i+2]=datas[2][i];
      // alpha = valido solo donde las 3 bandas seleccionadas tienen dato
      out.data[i+3]=Math.min(datas[0][i+3],datas[1][i+3],datas[2][i+3]);
    }
    ctx.putImageData(out,0,0);
    done(null,t);
  }
  imgs.forEach((img,i)=>{
    img.crossOrigin='anonymous';
    img.onload=()=>{loaded++;if(loaded===3&&!failed)combine();};
    img.onerror=()=>{if(!failed){failed=true;done(null,t);}};
    img.src=`tiles/${bands[i]}/${z}/${x}/${y}.png?v=${TILES_V}`;
  });
  return t;
}});
// label como getter (no string fija): se relee con t() cada vez que algo
// accede a la propiedad, así una banda personalizada abierta antes de
// cambiar de idioma queda al día sin tocar quien la usa (legend(), acá
// abajo, es una closure que ya se re-ejecuta en cada render del panel).
const MS_COMPOSITE_PRESETS={
  cir:{get label(){return t('ms.presetCir');},bands:['ms_nir','ms_red','ms_green']},
  rededge:{get label(){return t('ms.presetRededge');},bands:['ms_nir','ms_rededge','ms_red']},
};
function msBandLabel(id){
  const KEYS={ms_red:'ms.bandRed',ms_green:'ms.bandGreen',ms_nir:'ms.bandNir',ms_rededge:'ms.bandRededge',
    dband_r:'ms.dbandRed',dband_g:'ms.dbandGreen',dband_b:'ms.dbandBlue'};
  return KEYS[id]?t(KEYS[id]):id;
}
let msCompositeBands=(MS_COMPOSITE_PRESETS.cir.bands.every(b=>MS_BAND_IDS.includes(b)))
  ? [...MS_COMPOSITE_PRESETS.cir.bands] : [MS_BAND_IDS[0],MS_BAND_IDS[1]||MS_BAND_IDS[0],MS_BAND_IDS[2]||MS_BAND_IDS[0]];
let msCompositeLayer=MS_BAND_IDS.length? new BandCompositeGrid({bandR:msCompositeBands[0],bandG:msCompositeBands[1],bandB:msCompositeBands[2],maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:1,pane:'pane-ms_composite'}) : null; // let: registerMsComposite() la crea después si la misión arrancó sin datos MS
function setMsCompositeBands(bands){
  msCompositeBands=bands;
  msCompositeLayer.options.bandR=bands[0];msCompositeLayer.options.bandG=bands[1];msCompositeLayer.options.bandB=bands[2];
  msCompositeLayer.redraw();
}

// Thermal canvas layer
const ThermalGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y;img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data,lut=currentLUT;for(let i=0;i<d.length;i+=4){const v=d[i],idx=v*4,origA=d[i+3];d[i]=lut[idx];d[i+1]=lut[idx+1];d[i+2]=lut[idx+2];d[i+3]=origA*lut[idx+3]/255;}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/thermal/${z}/${x}/${y}.png?v=${TILES_V}`;return t;}});
let thermalLayer=new ThermalGrid({maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.7,pane:'pane-thermal'}).addTo(map);

// Hillshade from DSM tiles (if available)
let hillshadeLayer=L.tileLayer(tileTpl('hillshade'),{maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.4,pane:'pane-hillshade'});

// Índices de vegetación (NDVI/GNDVI/NDRE) — solo existen si la misión trae
// datos multiespectrales (/input_ms montado). Mismo patrón canvas-remap que
// ThermalGrid, pero con la paleta divergente fija.
const IndexGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y,name=this.options.indexName;img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data,lut=INDEX_LUT;for(let i=0;i<d.length;i+=4){const v=d[i],idx=v*4,origA=d[i+3];d[i]=lut[idx];d[i+1]=lut[idx+1];d[i+2]=lut[idx+2];d[i+3]=origA*lut[idx+3]/255;}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/${name}/${z}/${x}/${y}.png?v=${TILES_V}`;return t;}});
function indexLabelInfo(name){
  const K={ndvi:'idx.ndvi',gndvi:'idx.gndvi',ndre:'idx.ndre',msavi2:'idx.msavi2'};
  return K[name]?[t(K[name]+'.label'),t(K[name]+'.desc')]:[name.toUpperCase(),''];
}
const indexLayers={};
INDEX_NAMES.forEach(name=>{
  indexLayers[name]=new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.8,pane:'pane-'+name});
});

// Hotspot térmico / índices clasificados: clases DISCRETAS (0=sin dato, 1-4),
// no un gradiente continuo — cada valor de píxel mapea a un color exacto,
// sin interpolar (interpolar inventaría una "clase 2.5" que no existe). Los
// tiles ya vienen con resampling "near" (generate_tiles.py), así que solo
// deberían aparecer los 5 valores exactos.
function buildDiscreteLUT(colorsByClass){
  const lut=new Uint8Array(256*4);
  for(let i=0;i<256;i++){
    const c=colorsByClass[i]||[0,0,0,0];
    lut[i*4]=c[0];lut[i*4+1]=c[1];lut[i*4+2]=c[2];lut[i*4+3]=c[3];
  }
  return lut;
}
const HOTSPOT_LUT=buildDiscreteLUT({0:[0,0,0,0],1:[33,150,243,80],2:[255,235,59,255],3:[255,152,0,255],4:[198,40,40,255]});
const ClassGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y,name=this.options.layerName,lut=this.options.lut;img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data;for(let i=0;i<d.length;i+=4){const v=d[i],idx=v*4;d[i]=lut[idx];d[i+1]=lut[idx+1];d[i+2]=lut[idx+2];d[i+3]=lut[idx+3];}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/${name}/${z}/${x}/${y}.png?v=${TILES_V}`;return t;}});
const hotspotLayer=new ClassGrid({layerName:'hotspot_termico',lut:HOTSPOT_LUT,maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.85,pane:'pane-hotspot_termico'});

L.control.scale({imperial:false,metric:true,position:'bottomleft'}).addTo(map);

// Norte + coordenadas: se habían sacado del rediseño por error (el mockup
// de referencia no las mostraba, pero el pedido original de mantenerlas
// seguía en pie). Va como control de Leaflet, no un div absoluto a mano,
// para que se apile automáticamente arriba de la escala en la misma
// esquina, sin pisarla ni tener que calcular offsets fijos.
function fmtCoord(v,pos,neg){
  const h=v>=0?pos:neg, a=Math.abs(v);
  const d=Math.floor(a), m=(a-d)*60;
  return `${d}°${m.toFixed(3)}'${h}`;
}
const CoordsControl=L.Control.extend({
  options:{position:'bottomleft'},
  onAdd:function(){
    const el=L.DomUtil.create('div','coords-control');
    el.innerHTML='<span class="compass" aria-hidden="true">▲N</span><span class="coords-text">—</span>';
    L.DomEvent.disableClickPropagation(el);
    this._text=el.querySelector('.coords-text');
    return el;
  }
});
const coordsControl=new CoordsControl().addTo(map);
map.on('mousemove',e=>{
  coordsControl._text.textContent=`${fmtCoord(e.latlng.lat,'N','S')}  ${fmtCoord(e.latlng.lng,'E','O')}`;
});
map.on('mouseout',()=>{ coordsControl._text.textContent='—'; });

// Resolución MEDIDA de cada producto (bounds.json, la escribe generate_tiles.py
// desde el geotransform real). Las leyendas la mostraban escrita a mano y no
// correspondía a ninguna misión concreta: el GSD lo fijan la altura de vuelo y
// el sensor, así que cambia con cada vuelo.
function gsdTxt(clave){
  const v=RESOLUCION_CM&&RESOLUCION_CM[clave];
  return v?`${v} cm/px`:'—';
}

// SITUATION y SIMPLE_HINTS se declaran ACÁ (no donde se usan más abajo,
// junto a loadSituation()) porque layerCardHTML() —
// llamada desde renderCapasPanel() en el INIT, línea siguiente al registro
// de capas — ya las lee (interpretación en lenguaje llano + focos
// identificados dentro de la tarjeta de Hotspot). Con un `let`/`const` más
// abajo en el mismo script, esa lectura temprana revienta con "Cannot
// access before initialization" (temporal dead zone) — mismo bug real que
// liveMsBandIds, ver su comentario más abajo.
let SITUATION=null;
// Función, no dict estático: se relee con t() en cada render (layerCardHTML,
// llamada desde renderCapasPanel() en cada cambio de idioma).
function simpleHint(id){
  const key='hint.'+id;
  return (I18N.en[key]!==undefined)?t(key):null;
}

// ── Registro de capas para el panel "Capas" (gestor unificado) ──
const LAYER_REGISTRY={
  hillshade:{label:t('layer.hillshade'),group:'terreno',layer:hillshadeLayer,defaultOn:false,defaultOpacity:.4,
    legend:()=>`<div class="stat-row"><span class="lbl">${t('legend.resolution')}</span><span class="val">${gsdTxt('dsm')}</span></div>
      ${t('legend.hillshadeBody')}`},
  rgb:{label:t('layer.rgb'),group:'opticas',layer:rgbLayer,defaultOn:true,defaultOpacity:1,
    legend:()=>`<div class="stat-row"><span class="lbl">GSD</span><span class="val cool">${gsdTxt('rgb')}</span></div><div class="stat-row"><span class="lbl">Sensor</span><span class="val">DJI Zenmuse H20T (wide)</span></div>
      <div class="band-picker" style="margin-top:8px">
        <label>${t('legend.channels')}<select class="rgb-channel-preset">
          <option value="normal"${rgbChannelOrder.join(',')==='r,g,b'?' selected':''}>Normal (R-G-B)</option>
          <option value="custom"${rgbChannelOrder.join(',')!=='r,g,b'?' selected':''}>${t('legend.custom')}</option>
        </select></label>
        <div class="rgb-channel-custom" style="${rgbChannelOrder.join(',')!=='r,g,b'?'':'display:none'}">
          ${['R','G','B'].map((lbl,i)=>`<label>${lbl}<select class="rgb-channel-sel" data-ch="${i}">
            ${['r','g','b'].map(c=>`<option value="${c}"${rgbChannelOrder[i]===c?' selected':''}>${c.toUpperCase()}</option>`).join('')}
          </select></label>`).join('')}
        </div>
      </div>`},
};
if(MS_BAND_IDS.length){
  LAYER_REGISTRY.ms_composite={label:t('layer.msComposite'),group:'opticas',layer:msCompositeLayer,defaultOn:false,defaultOpacity:1,
    legend:()=>{
      const presetKey=Object.entries(MS_COMPOSITE_PRESETS).find(([,p])=>p.bands.join(',')===msCompositeBands.join(','))?.[0]||'custom';
      return `${t('legend.msCompositeBody')}
      <div class="band-picker">
        <label>Preset<select class="ms-composite-preset">
          ${Object.entries(MS_COMPOSITE_PRESETS).map(([k,p])=>`<option value="${k}"${presetKey===k?' selected':''}>${p.label}</option>`).join('')}
          <option value="custom"${presetKey==='custom'?' selected':''}>${t('legend.custom')}</option>
        </select></label>
        <div class="ms-composite-custom" style="${presetKey==='custom'?'':'display:none'}">
          ${['R','G','B'].map((lbl,i)=>`<label>${lbl}<select class="ms-composite-sel" data-ch="${i}">
            ${MS_BAND_IDS.map(id=>`<option value="${id}"${msCompositeBands[i]===id?' selected':''}>${msBandLabel(id)}</option>`).join('')}
          </select></label>`).join('')}
        </div>
      </div>`;}};
}
LAYER_REGISTRY.thermal={label:t('layer.thermal'),group:'termicas',layer:thermalLayer,defaultOn:true,defaultOpacity:.7,
    legend:()=>{const pal=PALETTES[currentPalette],ramp=pal.colors.join(',');return `<div class="stat-row"><span class="lbl">GSD</span><span class="val warm">${gsdTxt('thermal')}</span></div>
      <div class="stat-row"><span class="lbl">${t('legend.range')}</span><span class="val warm">${THERMAL_MIN.toFixed(1)}-${THERMAL_MAX.toFixed(1)} °C</span></div>
      <div style="margin-top:6px"><div class="legend-bar" style="background:linear-gradient(to right,${ramp})"></div>
      <div class="legend-lbl"><span>${THERMAL_MIN.toFixed(1)}°C</span><span>${((THERMAL_MIN+THERMAL_MAX)/2).toFixed(1)}°C</span><span>${THERMAL_MAX.toFixed(1)}°C</span></div></div>`;}};
INDEX_NAMES.forEach(name=>{
  const [label]=indexLabelInfo(name);
  const [lo,hi]=INDEX_RANGES[name];
  LAYER_REGISTRY[name]={label,group:'indices',layer:indexLayers[name],defaultOn:false,defaultOpacity:.8,
    legend:()=>{const ramp=INDEX_PALETTE.colors.join(','),desc=indexLabelInfo(name)[1];return `<p>${desc}</p>
      <div class="stat-row"><span class="lbl">${t('legend.range')}</span><span class="val cool">${lo.toFixed(2)} ${t('legend.to')} ${hi.toFixed(2)}</span></div>
      <div style="margin-top:6px"><div class="legend-bar" style="background:linear-gradient(to right,${ramp})"></div>
      <div class="legend-lbl"><span>${lo.toFixed(2)}</span><span>${((lo+hi)/2).toFixed(2)}</span><span>${hi.toFixed(2)}</span></div></div>`;}};
});
function classLegend(classes){
  return `<div class="legend-classes">${classes.map(([color,label])=>
    `<div class="stat-row"><span style="display:inline-block;width:12px;height:12px;border-radius:2px;background:${color};margin-right:6px;vertical-align:middle"></span><span class="lbl">${label}</span></div>`
  ).join('')}</div>`;
}
// hotspot_termico/*_class: el objeto Layer de Leaflet se crea SIEMPRE
// (su pane ya existe desde el layerOrder.forEach de arriba, no cuesta nada),
// pero la entrada en LAYER_REGISTRY (lo que hace que aparezcan en el panel de
// Capas) se registra SOLO si bounds.json dice que hay tiles de verdad
// (CAPAS_DISPONIBLES, ver generate_tiles.py). Antes se registraban sin
// condición: una misión sin térmico igual ofrecía "Hotspot" y los 4 índices
// clasificados con tiles inexistentes.
// registerHotspot()/registerIndexClass() se llaman una vez al cargar la
// página (para lo que ya está listo) y de nuevo en pollBoundsForChanges()
// (para lo que aparece mientras la misión sigue procesándose), mismo patrón
// que registerIndexLayer()/registerMsComposite().
function registerDband(){
  if(LAYER_REGISTRY.dband||!CAPAS_DISPONIBLES.has('dband'))return false;
  LAYER_REGISTRY.dband={label:t('layer.dband'),group:'opticas',layer:dbandLayer,defaultOn:false,defaultOpacity:1,
    legend:()=>`<div class="stat-row"><span class="lbl">GSD</span><span class="val cool">${gsdTxt('dband')}</span></div><div class="stat-row"><span class="lbl">Sensor</span><span class="val">${t('legend.dbandSensorLine')}</span></div>
      ${t('legend.dbandBody')}`};
  return true;
}
// Clases del hotspot térmico: función, no array estático, para poder
// pedirla dos veces con distinto escape de HTML (&lt; en la leyenda inline
// del panel, < crudo en LEGEND_SWATCHES/canvas del reporte) y para que se
// releea con t() en cada idioma.
function hotspotClasses(useEntities){
  const lt=useEntities?'&lt;':'<';
  return [
    ['#2196F3',`${t('hotspot.classNormal')} (${lt}40°C)`],
    ['#FFEB3B',`${t('hotspot.classElevated')} (40-60°C)`],
    ['#FF9800',`${t('hotspot.classHot')} (60-88°C)`],
    ['#C62828',`${t('hotspot.classActive')} (≥88°C)`],
  ];
}
function registerHotspot(){
  if(LAYER_REGISTRY.hotspot_termico||!CAPAS_DISPONIBLES.has('hotspot_termico'))return false;
  // Única señal de impacto del geovisor — encendida de entrada, es el dato
  // principal, no algo que haya que ir a descubrir en el panel de Capas.
  LAYER_REGISTRY.hotspot_termico={label:t('layer.hotspot'),group:'impacto',layer:hotspotLayer,defaultOn:true,defaultOpacity:.85,
    legend:()=>`${t('legend.hotspotBody1')}
      ${t('legend.hotspotBody2')}`+
      classLegend(hotspotClasses(true))};
  return true;
}

const INDEX_CLASS_LUTS={
  ndvi_class:buildDiscreteLUT({0:[0,0,0,0],1:[141,110,99,255],2:[255,235,59,255],3:[76,175,80,255]}),
  gndvi_class:buildDiscreteLUT({0:[0,0,0,0],1:[211,47,47,255],2:[255,235,59,255],3:[76,175,80,255]}),
  ndre_class:buildDiscreteLUT({0:[0,0,0,0],1:[211,47,47,255],2:[255,152,0,255],3:[139,195,74,255],4:[27,94,32,255]}),
  msavi2_class:buildDiscreteLUT({0:[0,0,0,0],1:[141,110,99,255],2:[255,235,59,255],3:[76,175,80,255]}),
};
// Solo la ESTRUCTURA (colores, cortes numéricos) queda fija acá — el label,
// la descripción y el nombre de cada clase se resuelven con t() en
// indexClassLabel()/indexClassDesc()/indexClassSwatches(), no como strings
// congeladas, para que se releean solas en cada cambio de idioma.
const INDEX_CLASS_INFO={
  ndvi_class:{labelKey:'layer.ndviClass',descKey:'idxclass.ndviClass.desc',classes:[['#8D6E63','idxclass.noVeg','(&lt;0.1)'],['#FFEB3B','idxclass.sparseStressed','(0.1-0.6)'],['#4CAF50','idxclass.denseHealthy','(≥0.6)']]},
  gndvi_class:{labelKey:'layer.gndviClass',descKey:'idxclass.gndviClass.desc',classes:[['#D32F2F','idxclass.severeStress','(&lt;0.3)'],['#FFEB3B','idxclass.moderateStressed','(0.3-0.5)'],['#4CAF50','idxclass.healthy','(≥0.5)']]},
  ndre_class:{labelKey:'layer.ndreClass',descKey:'idxclass.ndreClass.desc',classes:[['#D32F2F','idxclass.nDeficiency','(&lt;0.2)'],['#FF9800','idxclass.transition','(0.2-0.3)'],['#8BC34A','idxclass.healthy2','(0.3-0.6)'],['#1B5E20','idxclass.optimalMature','(≥0.6)']]},
  msavi2_class:{labelKey:'layer.msavi2Class',descKey:'idxclass.msavi2Class.desc',classes:[['#8D6E63','idxclass.noVeg','(&lt;0.1)'],['#FFEB3B','idxclass.sparseStressed','(0.1-0.6)'],['#4CAF50','idxclass.denseHealthy','(≥0.6)']]},
};
function indexClassLabel(name){ return t(INDEX_CLASS_INFO[name].labelKey); }
function indexClassDesc(name){ return t(INDEX_CLASS_INFO[name].descKey); }
function indexClassSwatches(name){ return INDEX_CLASS_INFO[name].classes.map(([c,key,suffix])=>[c,`${t(key)} ${suffix}`]); }
const indexClassLayers={};
Object.keys(INDEX_CLASS_INFO).forEach(name=>{
  indexClassLayers[name]=new ClassGrid({layerName:name,lut:INDEX_CLASS_LUTS[name],maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.85,pane:'pane-'+name});
});
// Swatches [color,etiqueta] por capa, para la leyenda de la imagen exportada
// (ver buildReportCanvas() en la sección del reporte). hotspot_termico y los
// 4 índices clasificados arman los suyos con las funciones de arriba, no con
// un array estático — así se releen en el idioma activo en cada exportación.
function legendSwatchesFor(id){
  if(id==='hotspot_termico')return hotspotClasses(false);
  if(INDEX_CLASS_INFO[id])return indexClassSwatches(id).map(([c,l])=>[c,l.replace(/&lt;/g,'<').replace(/&gt;/g,'>')]);
  return null;
}
function registerIndexClass(name){
  if(LAYER_REGISTRY[name]||!CAPAS_DISPONIBLES.has(name))return false;
  LAYER_REGISTRY[name]={label:indexClassLabel(name),group:'indices',layer:indexClassLayers[name],defaultOn:false,defaultOpacity:.85,
    legend:()=>`<p>${indexClassDesc(name)}</p>`+classLegend(indexClassSwatches(name))};
  return true;
}
registerDband();
registerHotspot();
Object.keys(INDEX_CLASS_INFO).forEach(registerIndexClass);

// ── Ruta de vuelo ────────────────────────────────────────────────────
// La escribe scripts/export_flight_path.py ANTES de invocar a ODM, desde el
// GPS EXIF de las capturas. Es lo ÚNICO que existe durante la reconstrucción
// (~1 h), así que es lo que hace que abrir el geovisor temprano sirva de
// algo: se ve dónde voló el dron, cuántas capturas hay y qué área cubren,
// en vez de un mapa vacío.
// featureGroup (no layerGroup): getBounds() es lo que permite encuadrar la
// misión y sirve de respaldo de extensión para el botón ⤢ de cada capa.
const flightLayer=L.featureGroup();
flightLayer.setOpacity=function(v){
  flightLayer.eachLayer(l=>{if(l.setStyle)l.setStyle({opacity:v,fillOpacity:v*.85});});
};
// Nombre de sensor traducido (mismas 3 claves rgb/thermal/multispectral en
// todo el archivo: flight_path, cascos convexos, HUD de progreso).
function sensorLabel(sensor){
  const K={rgb:'sensor.rgb',thermal:'sensor.thermal',multispectral:'sensor.multispectral'};
  return K[sensor]?t(K[sensor]):sensor;
}
function sensorCountLabel(sensor){
  return sensor==='rgb'?'RGB':sensorLabel(sensor).toLowerCase();
}
// Cuerpo de la leyenda de "Ruta de vuelo": función (no template estático)
// para que se releea con t() en cada idioma, igual que el resto de las
// leyendas de este archivo.
function flightPathLegendBody(resumen){
  return LANG==='es'
    ? `<p>Recorrido y posición de cada captura, según el GPS embebido en
        las fotos (${resumen}). Se genera antes de la reconstrucción, así que está
        disponible mientras el procesamiento sigue en curso.</p>`
    : `<p>Path and position of each capture, from the GPS embedded in
        the photos (${resumen}). Generated before reconstruction, so it is
        available while processing is still running.</p>`;
}
try{
  const req=new XMLHttpRequest();
  req.open('GET','outputs/flight_path.geojson',false);
  req.send(null);
  if(req.status===200){
    const gj=JSON.parse(req.responseText);
    const SC={rgb:'#58a6ff',thermal:'#f0883e',multispectral:'#3fb950'};
    const tally={};
    gj.features.forEach(ft=>{
      const s=ft.properties.sensor,c=SC[s]||'#58a6ff';
      if(ft.properties.kind==='track'){
        tally[s]=ft.properties.captures;
        L.geoJSON(ft,{style:{color:c,weight:2,opacity:.9,dashArray:'5,4'}}).addTo(flightLayer);
      }else{
        L.circleMarker([ft.geometry.coordinates[1],ft.geometry.coordinates[0]],
          {radius:3,color:c,weight:1,fillColor:c,fillOpacity:.85})
         .bindTooltip(`${ft.properties.name}<br>${Number(ft.properties.alt||0).toFixed(0)} m`,
                      {direction:'top'})
         .addTo(flightLayer);
      }
    });
    if(tally.multispectral)hasMsInput=true;
    if(tally.thermal)hasThermalInput=true;
    // Encendida por defecto solo mientras la corrida está en curso: una vez
    // que hay ortomosaicos, el recorrido estorba más de lo que aporta.
    LAYER_REGISTRY.flight_path={label:t('layer.flightPath'),group:'vuelo',layer:flightLayer,
      defaultOn:PRELIMINARY,defaultOpacity:1,
      legend:()=>{
        const resumen=Object.entries(tally).map(([s,n])=>`${n} ${sensorCountLabel(s)}`).join(' · ');
        return flightPathLegendBody(resumen);
      }};
  }
}catch(e){}

// ── Cascos convexos (trim_low_overlap_edges.py::_footprint_hull_mask) ──
// El polígono REAL que se usa para recortar cada mosaico, no una
// aproximación. Reportado en vivo: sin poder ver el casco en sí, un mosaico
// que se veía mal recortado no decía si el problema era el casco (mal
// calculado) o algo aguas abajo (alpha crudo de ODM, filtro de parches
// sueltos) — esta capa lo separa a simple vista. Apagada por defecto (es
// una herramienta de validación, no algo para mirar en el uso normal); los
// tres sensores comparten el mismo color que ya usa Ruta de vuelo.
const HULL_SC={rgb:'#58a6ff',thermal:'#f0883e',multispectral:'#3fb950'};
function hullLegendBody(sensor){
  const name=sensorLabel(sensor).toLowerCase();
  return LANG==='es'
    ? `<p>Polígono REAL usado para recortar el mosaico ${name}: el
        casco convexo de las huellas en el suelo de todas las fotos de ese sensor que entraron a la
        reconstrucción. Si el mosaico se ve recortado por DENTRO de este contorno, el problema no es el
        casco; si el mosaico se sale de este contorno, sí lo es.</p>`
    : `<p>Actual polygon used to trim the ${name} mosaic: the
        convex hull of the ground footprints of every photo from that sensor that went into the
        reconstruction. If the mosaic looks trimmed INSIDE this outline, the hull is not the problem;
        if the mosaic extends past this outline, it is.</p>`;
}
function _loadHullSync(sensor){
  if(LAYER_REGISTRY['hull_'+sensor])return;
  try{
    const req=new XMLHttpRequest();
    req.open('GET',`outputs/hull_${sensor}.geojson`,false);
    req.send(null);
    if(req.status!==200)return;
    const gj=JSON.parse(req.responseText);
    const c=HULL_SC[sensor];
    const layer=L.geoJSON(gj,{style:{color:c,weight:2,opacity:.9,fill:false,dashArray:'2,6'}});
    layer.setOpacity=function(v){layer.setStyle({opacity:v});};
    LAYER_REGISTRY['hull_'+sensor]={label:`${t('layer.hull')} · ${sensorLabel(sensor)}`,group:'vuelo',
      layer,defaultOn:false,defaultOpacity:1,
      legend:()=>hullLegendBody(sensor)};
  }catch(e){}
}
['rgb','thermal','multispectral'].forEach(_loadHullSync);

// ── Capas de Área Quemada (Validación Experto vs Detección Algorítmica) ──
try{
  const req=new XMLHttpRequest();
  req.open('GET','outputs/area_afectada_experto.geojson',false);
  req.send(null);
  if(req.status===200){
    const gj=JSON.parse(req.responseText);
    const totalHa=gj.properties?.total_area_ha||0;
    const totalM2=gj.properties?.total_area_m2||0;
    const layer=L.geoJSON(gj,{
      pane:'pane-area_afectada_experto',
      style:{color:'#ff6d00',weight:3,opacity:.95,fillColor:'#ff9100',fillOpacity:.25,dashArray:'6,4'},
      onEachFeature:(ft,l)=>{
        l.bindPopup(`<strong>📌 Área Afectada Manual (Experto)</strong><br>`+
                    `Área: <b>${ft.properties.area_ha||totalHa} ha</b> (${ft.properties.area_m2||totalM2} m²)<br>`+
                    `Origen: ${ft.properties.origen||'Digitación experta en campo'}`);
      }
    });
    layer.setOpacity=function(v){layer.setStyle({opacity:v,fillOpacity:v*.25});};
    LAYER_REGISTRY.area_afectada_experto={
      label:t('layer.areaExperto')||'📌 Área afectada (Oficial / Experto)',
      group:'impacto',
      layer,
      defaultOn:true,
      defaultOpacity:1,
      legend:()=>LANG==='es'
        ? `<p><b>Delimitación oficial validada:</b> Polígono oficial de área afectada levantado por fotointerpretación experta o delimitación asistida en RAPTOR. Superficie: <b>${totalHa} ha</b> (${totalM2} m²).</p>`
        : `<p><b>Official validated area:</b> Official burned area polygon from expert photointerpretation or assisted delineation in RAPTOR. Area: <b>${totalHa} ha</b> (${totalM2} m²).</p>`
    };
  }
}catch(e){}

try{
  const req=new XMLHttpRequest();
  req.open('GET','outputs/area_afectada_detectada.geojson',false);
  req.send(null);
  if(req.status===200){
    const gj=JSON.parse(req.responseText);
    const totalHa=gj.properties?.total_area_ha||0;
    const totalM2=gj.properties?.total_area_m2||0;
    const layer=L.geoJSON(gj,{
      pane:'pane-area_afectada_detectada',
      style:{color:'#f59e0b',weight:2,opacity:.8,fillColor:'#fbbf24',fillOpacity:.2,dashArray:'4,4'},
      onEachFeature:(ft,l)=>{
        l.bindPopup(`<strong>💡 Máscara Térmica Sugerida (Guía de Apoyo)</strong><br>`+
                    `Superficie sugerida: <b>${ft.properties.area_ha||totalHa} ha</b> (${ft.properties.area_m2||totalM2} m²)<br>`+
                    `Uso: Apoyo visual para la herramienta de delimitación interactiva.`);
      }
    });
    layer.setOpacity=function(v){layer.setStyle({opacity:v,fillOpacity:v*.2});};
    LAYER_REGISTRY.area_afectada_detectada={
      label:t('layer.areaDetectada')||'💡 Máscara térmica sugerida',
      group:'impacto',
      layer,
      defaultOn:false,
      defaultOpacity:0.8,
      legend:()=>LANG==='es'
        ? `<p><b>Máscara térmica sugerida:</b> Guía visual generada por gradiente térmico radiométrico para asistir la delimitación pericial con la herramienta interactiva.</p>`
        : `<p><b>Suggested thermal mask:</b> Visual guide generated from radiometric thermal gradients to assist expert polygon delineation.</p>`
    };
  }
}catch(e){}

// Orden pensado para decisión, no para flujo técnico: lo que más pesa para
// decidir dónde actuar (hotspots) va primero, no al final de un scroll, que
// es donde quedaba con el orden "técnico" anterior.
function groupLabel(group){ return t('group.'+group); }
const GROUP_ORDER=['vuelo','impacto','indices','termicas','opticas','terreno'];

Object.entries(LAYER_REGISTRY).forEach(([id,def])=>{
  if(def.defaultOn)def.layer.addTo(map);
});

// ═══════════════════════════════════════════════════════════════════
// COMPARE SLIDER
// ═══════════════════════════════════════════════════════════════════
// Fábrica de instancias de capa para el comparador: cada lado usa su
// PROPIA instancia (Leaflet no permite una misma capa en dos mapas a la
// vez), armada con la misma receta que la capa original de LAYER_REGISTRY.
const RASTER_LAYER_FACTORY={
  hillshade:()=>L.tileLayer(tileTpl('hillshade'),{maxZoom:21,maxNativeZoom:20,minZoom:14}),
  rgb:()=>L.tileLayer(tileTpl('rgb'),{maxZoom:21,maxNativeZoom:20,minZoom:14}),
  thermal:()=>new ThermalGrid({maxZoom:21,maxNativeZoom:20,minZoom:14}),
  ...(MS_BAND_IDS.length?{ms_composite:()=>new BandCompositeGrid({bandR:msCompositeBands[0],bandG:msCompositeBands[1],bandB:msCompositeBands[2],maxZoom:21,maxNativeZoom:20,minZoom:14})}:{}),
  hotspot_termico:()=>new ClassGrid({layerName:'hotspot_termico',lut:HOTSPOT_LUT,maxZoom:21,maxNativeZoom:20,minZoom:14}),
};
INDEX_NAMES.forEach(name=>{RASTER_LAYER_FACTORY[name]=()=>new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14});});
Object.keys(INDEX_CLASS_INFO).forEach(name=>{RASTER_LAYER_FACTORY[name]=()=>new ClassGrid({layerName:name,lut:INDEX_CLASS_LUTS[name],maxZoom:21,maxNativeZoom:20,minZoom:14});});
const COMPARABLE_IDS=layerOrder.filter(id=>RASTER_LAYER_FACTORY[id]);

let compareActive=false,compareLeftMap=null,compareRightMap=null;
let compareLeftLayer=null,compareRightLayer=null,compareLeftId='rgb',compareRightId='thermal';

function setCompareLayer(side,id){
  const m=side==='left'?compareLeftMap:compareRightMap;
  const factory=RASTER_LAYER_FACTORY[id];
  if(!m||!factory)return;
  const current=side==='left'?compareLeftLayer:compareRightLayer;
  if(current)m.removeLayer(current);
  const layer=factory();
  layer.addTo(m);
  if(side==='left'){compareLeftLayer=layer;compareLeftId=id;}
  else{compareRightLayer=layer;compareRightId=id;}
}
function buildCompareSelect(side){
  const sel=document.createElement('select');
  sel.className='compare-select';
  // COMPARABLE_IDS es la lista ESTÁTICA de todo tipo de capa ráster posible
  // (RASTER_LAYER_FACTORY existe para los 4 índices y sus clasificados y
  // hotspot sin importar la misión), pero LAYER_REGISTRY[id] solo existe
  // para lo que ESTA misión realmente tiene tiles (ver CAPAS_DISPONIBLES).
  // Antes esto iteraba COMPARABLE_IDS sin filtrar y `.label` sobre un
  // LAYER_REGISTRY[id] undefined (p.ej. un índice en una misión sin
  // multiespectral) tiraba un TypeError sin capturar que cortaba
  // toggleCompare() a la mitad: el ancho de #compare-left-map nunca se
  // fijaba y los listeners de sincronización de mover un mapa nunca se
  // conectaban. Los dos mapas del comparador quedaban del todo
  // independientes uno del otro, exactamente el bug reportado (se
  // desalinean y se arrastran por separado).
  COMPARABLE_IDS.filter(id=>LAYER_REGISTRY[id]).forEach(id=>{
    const opt=document.createElement('option');
    opt.value=id;opt.textContent=LAYER_REGISTRY[id].label;
    sel.appendChild(opt);
  });
  sel.value=side==='left'?compareLeftId:compareRightId;
  sel.onchange=()=>setCompareLayer(side,sel.value);
  return sel;
}
function toggleCompare(){
  compareActive=!compareActive;
  document.getElementById('btn-compare').classList.toggle('active',compareActive);
  document.getElementById('compare-container').classList.toggle('active',compareActive);
  if(compareActive){
    if(!compareLeftMap){
      compareRightMap=L.map('compare-right',{center:map.getCenter(),zoom:map.getZoom(),maxZoom:21,zoomControl:false,attributionControl:false});
      compareLeftMap=L.map('compare-left-map',{center:map.getCenter(),zoom:map.getZoom(),maxZoom:21,zoomControl:false,attributionControl:false});
      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19}).addTo(compareRightMap);
      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19}).addTo(compareLeftMap);
      setCompareLayer('left',compareLeftId);
      setCompareLayer('right',compareRightId);
      document.querySelector('.compare-label.left').appendChild(buildCompareSelect('left'));
      document.querySelector('.compare-label.right').appendChild(buildCompareSelect('right'));
      // Sync
      let syncing=false;
      compareLeftMap.on('move',()=>{if(!syncing){syncing=true;compareRightMap.setView(compareLeftMap.getCenter(),compareLeftMap.getZoom(),{animate:false});syncing=false;}});
      compareRightMap.on('move',()=>{if(!syncing){syncing=true;compareLeftMap.setView(compareRightMap.getCenter(),compareRightMap.getZoom(),{animate:false});syncing=false;}});
      // Una sola vez (no en cada activación): initSliderDrag() cuelga
      // listeners en document (mousemove/mouseup/touchmove/touchend) que
      // nunca se sueltan: llamarla de nuevo cada vez que se abre "Comparar"
      // los apilaba, uno más por cada apertura de la sesión.
      initSliderDrag();
    }
    // #compare-left-map (el contenedor real de Leaflet) siempre queda al
    // ANCHO COMPLETO del visor. Solo #compare-left (el div exterior, con
    // overflow:hidden) se achica al arrastrar. Si en cambio se achicara el
    // propio contenedor de Leaflet, su noción interna de viewport/tiles
    // quedaría calculada para un mapa más chico, y el recorte visual y la
    // posición geográfica real se desalinean (el bug original).
    const fullW=document.getElementById('compare-container').clientWidth;
    document.getElementById('compare-left-map').style.width=fullW+'px';
    setTimeout(()=>{compareLeftMap.invalidateSize();compareRightMap.invalidateSize();updateSlider();},100);
  }
}
function updateSlider(){
  const c=document.getElementById('compare-container'),w=c.clientWidth;
  document.getElementById('compare-left-map').style.width=w+'px';
  document.getElementById('compare-left').style.width=(w/2)+'px';
  document.getElementById('compare-slider').style.left=(w/2-2)+'px';
}
function initSliderDrag(){
  const slider=document.getElementById('compare-slider'),container=document.getElementById('compare-container');
  let dragging=false;
  const drag=x=>{
    const rect=container.getBoundingClientRect(),rel=x-rect.left;
    const w=rect.width,clamped=Math.max(40,Math.min(w-40,rel));
    document.getElementById('compare-left').style.width=clamped+'px'; // solo recorta, NO toca compare-left-map
    slider.style.left=(clamped-2)+'px';
  };
  slider.addEventListener('mousedown',e=>{e.preventDefault();dragging=true;});
  document.addEventListener('mousemove',e=>{if(dragging&&compareActive)drag(e.clientX);});
  document.addEventListener('mouseup',()=>{dragging=false;});
  // Touch
  slider.addEventListener('touchstart',e=>{e.preventDefault();dragging=true;});
  document.addEventListener('touchmove',e=>{if(dragging&&compareActive)drag(e.touches[0].clientX);});
  document.addEventListener('touchend',()=>{dragging=false;});
}
window.addEventListener('resize',()=>{if(compareActive){updateSlider();compareLeftMap.invalidateSize();compareRightMap.invalidateSize();}});

// ═══════════════════════════════════════════════════════════════════
// SIDEBAR
// ═══════════════════════════════════════════════════════════════════
let sidebarOpen=true;
function toggleSidebar(){
  sidebarOpen=!sidebarOpen;
  document.getElementById('main-body').classList.toggle('panel-collapsed',!sidebarOpen);
  setTimeout(()=>{map.invalidateSize();if(compareActive){compareLeftMap?.invalidateSize();compareRightMap?.invalidateSize();}},300);
}
function renderCapasPanel(){
  const c=document.getElementById('tab-content');
  c.innerHTML=layersPanelHTML();wireLayersPanel();
  renderFooterAddMsCta();
}

// ── Panel "Capas": selector de mapa base + capas agrupadas, con descripción/
// interpretación y los focos identificados dentro de la tarjeta Hotspot ──
function basePickerHTML(){
  return `<div class="base-picker">
    <button class="${currentBase==='osm'?'active':''}" data-base="osm">${t('base.streets')}</button>
    <button class="${currentBase==='sat'?'active':''}" data-base="sat">${t('base.satellite')}</button>
    </div>`;
}
function layerCardHTML(id){
  const def=LAYER_REGISTRY[id];
  const shown=map.hasLayer(def.layer);
  const opacity=Math.round((def.layer.options.opacity??1)*100);
  return `<div class="layer-card${shown?' is-on':''}" data-id="${id}" data-name="${def.label.toLowerCase()}">
    <div class="layer-card-head">
      <span class="layer-handle" title="${t('layer.dragTitle')}">⠿</span>
      <label class="layer-name"><input type="checkbox" class="layer-vis" data-id="${id}" ${shown?'checked':''}><span class="nm">${def.label}</span></label>
      <span class="layer-tools">
        <button class="layer-solo" data-id="${id}" title="${t('layer.soloTitle')}"><svg class="ic ic-dot" aria-hidden="true"><use href="#i-dot"/></svg></button>
        <button class="layer-zoom" data-id="${id}" title="${t('layer.zoomTitle')}"><svg class="ic" aria-hidden="true"><use href="#i-frame"/></svg></button>
        <button class="layer-legend-toggle" data-id="${id}" title="${t('layer.legendTitle')}"><svg class="ic" aria-hidden="true"><use href="#i-chev-down"/></svg></button>
      </span>
    </div>
    <div class="layer-card-opacity">
      <span>${t('layer.opacity')}</span>
      <input type="range" class="layer-opacity" data-id="${id}" min="0" max="100" value="${opacity}">
      <span class="layer-opacity-val">${opacity}%</span>
    </div>
    <div class="layer-legend-body" data-id="${id}">${simpleHint(id)?`<div class="interpret"><b>${t('layer.whatItMeans')}</b> ${simpleHint(id)}</div>`:''}${def.legend()}${id==='hotspot_termico'?hotspotListHTML():''}</div>
  </div>`;
}
// Grupo vacío porque a esta misión le falta el vuelo multiespectral (no
// porque no haya nada que mostrar): en vez de que la sección desaparezca sin
// explicación, se dice por qué. NDVI es la señal primaria de la que salen
// estos índices (ver classify_vegetation_indices.py); el hotspot térmico NO
// depende de esto (scripts/compute_thermal_hotspot.py). El botón
// para agregar el vuelo vive UNA sola vez, fijo al pie del sidebar (ver
// renderFooterAddMsCta()); antes también aparecía acá adentro, duplicado
// con el del pie cada vez que ambos estaban visibles a la vez.
function addMsEmptyGroupHTML(titulo, detalle){
  return `<div class="layer-group" data-group="addms-cta">
    <div class="layer-group-title">${titulo}</div>
    <div class="addms-cta"><p>${detalle}</p></div>
  </div>`;
}
// El botón SÍ vive acá: es el único lugar que lo genera (ver comentario de
// addMsEmptyGroupHTML de arriba). Lo usa renderFooterAddMsCta().
function addMsCtaHTML(titulo, detalle){
  if(!urlMission)return'';
  const href=`/?mission=${encodeURIComponent(urlMission)}&addms=1`;
  return `<div class="layer-group" data-group="addms-cta">
    <div class="layer-group-title">${titulo}</div>
    <div class="addms-cta"><p>${detalle}</p>
      <a class="btn primary sm" href="${href}">${t('addms.ctaLink')}</a></div>
  </div>`;
}
function layersPanelHTML(){
  let html=basePickerHTML();
  GROUP_ORDER.forEach(group=>{
    const ids=layerOrder.filter(id=>LAYER_REGISTRY[id]&&LAYER_REGISTRY[id].group===group);
    if(ids.length===0){
      // MS_BAND_IDS vacío solo dice "todavía no hay índices calculados" —
      // pasa igual si la misión nunca tuvo M3M que si lo tiene y ODM sigue
      // reconstruyendo. hasMsInput (del tally de flight_path.geojson, ver
      // arriba) distingue los dos casos para no invitar a "agregar" un
      // vuelo que ya está incluido y en curso.
      if(group==='indices'&&!liveMsBandIds.length)
        html+=hasMsInput
          ? addMsEmptyGroupHTML(t('addms.vegIndicesTitle'),t('addms.includedPending'))
          : addMsEmptyGroupHTML(t('addms.vegIndicesTitle'),t('addms.notYet'));
      else if(group==='impacto'&&!CAPAS_DISPONIBLES.has('hotspot_termico'))
        html+=hasThermalInput
          ? addMsEmptyGroupHTML(t('layer.hotspot'),t('addms.thermalIncludedPending'))
          : addMsEmptyGroupHTML(t('layer.hotspot'),t('addms.thermalNotYet'));
      return;
    }
    html+=`<div class="layer-group" data-group="${group}">
      <div class="layer-group-title">${groupLabel(group)}</div>
      <div class="layer-list" data-group="${group}">${ids.map(layerCardHTML).join('')}</div>
    </div>`;
  });
  return html;
}
function wireLayersPanel(){
  document.querySelectorAll('.base-picker button').forEach(btn=>{
    btn.onclick=()=>{
      const which=btn.dataset.base;
      if(which===currentBase)return;
      map.removeLayer(currentBase==='osm'?osmBase:satBase);
      if(currentBase==='sat')map.removeLayer(satLabels);
      map.addLayer(which==='osm'?osmBase:satBase);
      if(which==='sat')satLabels.addTo(map); // vías/lugares sobre la satelital: sin esto no hay cómo orientarse
      currentBase=which;
      document.querySelectorAll('.base-picker button').forEach(b=>b.classList.toggle('active',b.dataset.base===which));
    };
  });
  document.querySelectorAll('.layer-vis').forEach(cb=>{
    cb.onchange=()=>{
      const def=LAYER_REGISTRY[cb.dataset.id];
      if(cb.checked)def.layer.addTo(map);else map.removeLayer(def.layer);
      cb.closest('.layer-card').classList.toggle('is-on',cb.checked);
    };
  });
  // "Solo": apaga todo lo demás del mismo grupo temático. Es el gesto más
  // repetido al revisar productos (comparar una capa contra el fondo sin ir
  // destildando cinco casillas a mano).
  document.querySelectorAll('.layer-solo').forEach(btn=>{
    btn.onclick=()=>soloLayer(btn.dataset.id);
  });
  document.querySelectorAll('.layer-zoom').forEach(btn=>{
    btn.onclick=()=>zoomToLayer(btn.dataset.id);
  });
  document.querySelectorAll('.layer-opacity').forEach(sl=>{
    sl.oninput=()=>{
      const def=LAYER_REGISTRY[sl.dataset.id];
      def.layer.setOpacity(sl.value/100);
      sl.nextElementSibling.textContent=sl.value+'%';
    };
  });
  document.querySelectorAll('.layer-legend-toggle').forEach(btn=>{
    btn.onclick=()=>{
      const body=document.querySelector(`.layer-legend-body[data-id="${btn.dataset.id}"]`);
      const open=body.classList.toggle('open');
      btn.classList.toggle('open',open);
    };
  });
  wireBandPickers();
  initLayerDrag();
  wireHotspotList();
}

// ── Selectores de canales/bandas (RGB personalizado + compuesto MS) ──
function wireBandPickers(){
  const rgbPreset=document.querySelector('.rgb-channel-preset');
  if(rgbPreset){
    rgbPreset.onchange=()=>{
      const custom=document.querySelector('.rgb-channel-custom');
      if(rgbPreset.value==='normal'){custom.style.display='none';setRgbChannelOrder(['r','g','b']);}
      else{custom.style.display='';setRgbChannelOrder([...rgbChannelOrder]);}
    };
  }
  document.querySelectorAll('.rgb-channel-sel').forEach(sel=>{
    sel.onchange=()=>{
      const order=[...rgbChannelOrder];
      order[+sel.dataset.ch]=sel.value;
      setRgbChannelOrder(order);
    };
  });
  const msPreset=document.querySelector('.ms-composite-preset');
  if(msPreset){
    msPreset.onchange=()=>{
      const custom=document.querySelector('.ms-composite-custom');
      if(msPreset.value==='custom'){custom.style.display='';}
      else{custom.style.display='none';setMsCompositeBands([...MS_COMPOSITE_PRESETS[msPreset.value].bands]);}
    };
  }
  document.querySelectorAll('.ms-composite-sel').forEach(sel=>{
    sel.onchange=()=>{
      const bands=[...msCompositeBands];
      bands[+sel.dataset.ch]=sel.value;
      setMsCompositeBands(bands);
    };
  });
}

// ── Reordenamiento de capas por Pointer Events (touch + mouse unificado;
// HTML5 drag-and-drop nativo no tiene soporte táctil confiable en
// iOS/Android). El arrastre se limita a reordenar DENTRO del mismo grupo. ──
function initLayerDrag(){
  document.querySelectorAll('.layer-handle').forEach(handle=>{
    handle.addEventListener('pointerdown',e=>{
      e.preventDefault();
      const card=handle.closest('.layer-card');
      const list=handle.closest('.layer-list');
      card.classList.add('dragging');
      handle.setPointerCapture(e.pointerId);

      function onMove(ev){
        const y=ev.clientY;
        const allCards=Array.from(document.querySelectorAll('.layer-card')).filter(c=>c!==card);
        for(const other of allCards){
          const r=other.getBoundingClientRect();
          const mid=r.top+r.height/2;
          const targetList=other.closest('.layer-list');
          if(!targetList) continue;
          if(y<mid&&other.compareDocumentPosition(card)&Node.DOCUMENT_POSITION_FOLLOWING){
            targetList.insertBefore(card,other);break;
          }else if(y>mid&&other.compareDocumentPosition(card)&Node.DOCUMENT_POSITION_PRECEDING){
            targetList.insertBefore(card,other.nextSibling);break;
          }
        }
      }
      function onUp(){
        card.classList.remove('dragging');
        handle.releasePointerCapture(e.pointerId);
        document.removeEventListener('pointermove',onMove);
        document.removeEventListener('pointerup',onUp);
        // Recalcular layerOrder GLOBAL a partir del orden visual actual en el panel (top -> bottom).
        // Lo que el usuario ve arriba de todo en el panel se mapea al índice 0 de layerOrder
        // (y por tanto a la capa que se renderiza más arriba / zIndex más alto en el mapa).
        const newOrder=[];
        document.querySelectorAll('.layer-card').forEach(c=>{
          const cid=c.dataset.id;
          if(cid && !newOrder.includes(cid)) newOrder.push(cid);
        });
        layerOrder.forEach(id=>{
          if(!newOrder.includes(id)) newOrder.push(id);
        });
        layerOrder=newOrder;
        applyLayerOrder();
      }
      document.addEventListener('pointermove',onMove);
      document.addEventListener('pointerup',onUp);
    });
  });
}

// ═══════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════
renderCapasPanel();
window.addEventListener('beforeunload',e=>{
  if(areaDirty){e.preventDefault();e.returnValue='';}
});

// ═══════════════════════════════════════════════════════════════════
// PRODUCTOS PROGRESIVOS: la corrida agrega capas mientras sigue viva
// ═══════════════════════════════════════════════════════════════════
// RGB/térmico/hillshade/hotspot/índices-clasificados YA están registrados
// desde el arranque (sus tiles pueden no existir todavía, simplemente no
// cargan hasta que aparecen; un .redraw() los recupera, ver más abajo). Lo
// que SÍ falta registrar en caliente son los productos que ni siquiera
// existían como CONCEPTO al cargar la página: los índices continuos y el
// compuesto multiespectral. Si la misión se abrió con bounds.json todavía
// "preliminary" (solo ruta de vuelo), ninguno de los dos tenía datos para
// calcular su rango de color.
// (liveMsBandIds se declara arriba, junto a MS_BAND_IDS; ver el comentario
// ahí sobre por qué no puede vivir acá.)

function ensurePane(id){
  if(!map.getPane('pane-'+id)){
    map.createPane('pane-'+id);
    layerOrder.push(id);
  }
  applyLayerOrder();
}
function registerIndexLayer(name){
  if(LAYER_REGISTRY[name])return false;
  const [label]=indexLabelInfo(name);
  const [lo,hi]=INDEX_RANGES[name];
  ensurePane(name);
  indexLayers[name]=new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.8,pane:'pane-'+name});
  RASTER_LAYER_FACTORY[name]=()=>new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14});
  LAYER_REGISTRY[name]={label,group:'indices',layer:indexLayers[name],defaultOn:false,defaultOpacity:.8,
    legend:()=>{const ramp=INDEX_PALETTE.colors.join(','),desc=indexLabelInfo(name)[1];return `<p>${desc}</p>
      <div class="stat-row"><span class="lbl">${t('legend.range')}</span><span class="val cool">${lo.toFixed(2)} ${t('legend.to')} ${hi.toFixed(2)}</span></div>
      <div style="margin-top:6px"><div class="legend-bar" style="background:linear-gradient(to right,${ramp})"></div>
      <div class="legend-lbl"><span>${lo.toFixed(2)}</span><span>${((lo+hi)/2).toFixed(2)}</span><span>${hi.toFixed(2)}</span></div></div>`;}};
  return true;
}
function registerMsComposite(){
  if(LAYER_REGISTRY.ms_composite)return false;
  ensurePane('ms_composite');
  msCompositeBands=(MS_COMPOSITE_PRESETS.cir.bands.every(b=>liveMsBandIds.includes(b)))
    ? [...MS_COMPOSITE_PRESETS.cir.bands]
    : [liveMsBandIds[0],liveMsBandIds[1]||liveMsBandIds[0],liveMsBandIds[2]||liveMsBandIds[0]];
  msCompositeLayer=new BandCompositeGrid({bandR:msCompositeBands[0],bandG:msCompositeBands[1],bandB:msCompositeBands[2],maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:1,pane:'pane-ms_composite'});
  RASTER_LAYER_FACTORY.ms_composite=()=>new BandCompositeGrid({bandR:msCompositeBands[0],bandG:msCompositeBands[1],bandB:msCompositeBands[2],maxZoom:21,maxNativeZoom:20,minZoom:14});
  LAYER_REGISTRY.ms_composite={label:t('layer.msComposite'),group:'opticas',layer:msCompositeLayer,defaultOn:false,defaultOpacity:1,
    legend:()=>{
      const presetKey=Object.entries(MS_COMPOSITE_PRESETS).find(([,p])=>p.bands.join(',')===msCompositeBands.join(','))?.[0]||'custom';
      return `${t('legend.msCompositeBody')}
      <div class="band-picker">
        <label>Preset<select class="ms-composite-preset">
          ${Object.entries(MS_COMPOSITE_PRESETS).map(([k,p])=>`<option value="${k}"${presetKey===k?' selected':''}>${p.label}</option>`).join('')}
          <option value="custom"${presetKey==='custom'?' selected':''}>${t('legend.custom')}</option>
        </select></label>
        <div class="ms-composite-custom" style="${presetKey==='custom'?'':'display:none'}">
          ${['R','G','B'].map((lbl,i)=>`<label>${lbl}<select class="ms-composite-sel" data-ch="${i}">
            ${liveMsBandIds.map(id=>`<option value="${id}"${msCompositeBands[i]===id?' selected':''}>${msBandLabel(id)}</option>`).join('')}
          </select></label>`).join('')}
        </div>
      </div>`;}};
  return true;
}
// Reintento de outputs/flight_path.geojson: el bloque de arriba lo intenta
// una sola vez, sync, al cargar la página. Si el geovisor se abre en los
// pocos segundos entre "arrancó la corrida" y "export_flight_path.py terminó
// de escribir el archivo" (el caso normal: la webapp redirige acá apenas
// arranca el pipeline), esa lectura da 404 y la capa queda sin registrar para
// siempre, aunque el archivo aparezca 2 segundos después.
async function tryLoadFlightPath(){
  if(LAYER_REGISTRY.flight_path)return false;
  try{
    const r=await fetch('outputs/flight_path.geojson?t='+Date.now(),{cache:'no-store'});
    if(!r.ok)return false;
    const gj=await r.json();
    const SC={rgb:'#58a6ff',thermal:'#f0883e',multispectral:'#3fb950'};
    const tally={};
    gj.features.forEach(ft=>{
      const s=ft.properties.sensor,c=SC[s]||'#58a6ff';
      if(ft.properties.kind==='track'){
        tally[s]=ft.properties.captures;
        L.geoJSON(ft,{style:{color:c,weight:2,opacity:.9,dashArray:'5,4'}}).addTo(flightLayer);
      }else{
        L.circleMarker([ft.geometry.coordinates[1],ft.geometry.coordinates[0]],
          {radius:3,color:c,weight:1,fillColor:c,fillOpacity:.85})
         .bindTooltip(`${ft.properties.name}<br>${Number(ft.properties.alt||0).toFixed(0)} m`,
                      {direction:'top'})
         .addTo(flightLayer);
      }
    });
    if(tally.multispectral)hasMsInput=true;
    if(tally.thermal)hasThermalInput=true;
    LAYER_REGISTRY.flight_path={label:t('layer.flightPath'),group:'vuelo',layer:flightLayer,
      defaultOn:PRELIMINARY,defaultOpacity:1,
      legend:()=>{
        const resumen=Object.entries(tally).map(([s,n])=>`${n} ${sensorCountLabel(s)}`).join(' · ');
        return flightPathLegendBody(resumen);
      }};
    if(PRELIMINARY)flightLayer.addTo(map);
    return true;
  }catch(e){ return false; }
}

// Reintento de outputs/hull_<sensor>.geojson — mismo patrón que
// tryLoadFlightPath: el casco de cada sensor aparece recién cuando SU
// trim_edges termina, así que si el geovisor se abrió antes, la lectura
// sync del arranque (_loadHullSync) no lo encuentra.
async function tryLoadHull(sensor){
  if(LAYER_REGISTRY['hull_'+sensor])return false;
  try{
    const r=await fetch(`outputs/hull_${sensor}.geojson?t=`+Date.now(),{cache:'no-store'});
    if(!r.ok)return false;
    const gj=await r.json();
    const c=HULL_SC[sensor];
    const layer=L.geoJSON(gj,{style:{color:c,weight:2,opacity:.9,fill:false,dashArray:'2,6'}});
    layer.setOpacity=function(v){layer.setStyle({opacity:v});};
    LAYER_REGISTRY['hull_'+sensor]={label:`${t('layer.hull')} · ${sensorLabel(sensor)}`,group:'vuelo',
      layer,defaultOn:false,defaultOpacity:1,
      legend:()=>hullLegendBody(sensor)};
    return true;
  }catch(e){ return false; }
}

async function tryLoadBurnedArea(kind){
  const id='area_afectada_'+kind;
  if(LAYER_REGISTRY[id])return false;
  ensurePane(id);
  try{
    const r=await fetch(`outputs/${id}.geojson?t=`+Date.now(),{cache:'no-store'});
    if(!r.ok)return false;
    const gj=await r.json();
    const totalHa=gj.properties?.total_area_ha||0;
    const totalM2=gj.properties?.total_area_m2||0;
    const isExp=(kind==='experto');
    const layer=L.geoJSON(gj,{
      pane:'pane-'+id,
      style:{
        color:isExp?'#ff6d00':'#d50000',
        weight:3,
        opacity:.95,
        fillColor:isExp?'#ff9100':'#ff1744',
        fillOpacity:isExp?.25:.35,
        dashArray:isExp?'6,4':undefined
      },
      onEachFeature:(ft,l)=>{
        const h=ft.properties.area_ha||totalHa;
        const m=ft.properties.area_m2||totalM2;
        const title=isExp?'📌 Área Afectada Manual (Experto)':'🤖 Área Afectada (Algoritmo Multi-Sensor)';
        const det=isExp?`Origen: ${ft.properties.origen||'Digitación de campo'}`:`Método: ${ft.properties.metodo||'Fusión Térmica (ΔT ≥ +6°C) + Infrarrojo/MSAVI2'}`;
        l.bindPopup(`<strong>${title}</strong><br>Superficie: <b>${h} ha</b> (${m} m²)<br>${det}`);
      }
    });
    layer.setOpacity=function(v){layer.setStyle({opacity:v,fillOpacity:v*(isExp?.25:.35)});};
    LAYER_REGISTRY[id]={
      label:t('layer.'+(isExp?'areaExperto':'areaDetectada'))||(isExp?'📌 Área afectada (Experto)':'🤖 Área afectada (Algoritmo)'),
      group:'impacto',
      layer,
      defaultOn:true,
      defaultOpacity:1,
      legend:()=>LANG==='es'
        ? (isExp
            ? `<p><b>Delimitación manual experta:</b> Polígono levantado por fotointerpretación y validación en campo tras la emergencia. Superficie total: <b>${totalHa} ha</b> (${totalM2} m²).</p>`
            : `<p><b>Detección automática multi-sensor:</b> Polígono generado por fusión de anomalía térmica ($\Delta T \ge +6\text{°C}$) e índice de vegetación/NIR. Superficie detectada: <b>${totalHa} ha</b> (${totalM2} m²).</p>`)
        : (isExp
            ? `<p><b>Expert ground truth:</b> Polygon delineated by photointerpretation and field validation after the emergency. Total area: <b>${totalHa} ha</b> (${totalM2} m²).</p>`
            : `<p><b>Multi-sensor automated detection:</b> Polygon generated from thermal anomaly ($\Delta T \ge +6\text{°C}$) and vegetation index/NIR drop. Detected area: <b>${totalHa} ha</b> (${totalM2} m²).</p>`)
    };
    layer.addTo(map);
    return true;
  }catch(e){ return false; }
}

// Sondeo de bounds.json: mientras cambie de contenido, hay productos nuevos
// (generate_tiles.py lo reescribe cada vez que corre). Un cambio dispara: (1)
// registrar en caliente lo que antes no existía como capa, (2) redibujar TODO
// lo que ya estaba registrado (sus tiles pueden haber pasado de 404 a reales).
let lastBoundsSig=null;
async function pollBoundsForChanges(){
  try{
    const r=await fetch('tiles/bounds.json?t='+Date.now(),{cache:'no-store'});
    if(!r.ok)return;
    const sig=await r.text();
    // OJO: antes, la primera lectura exitosa solo fijaba lastBoundsSig y
    // volvía (sin registrar nada) — la idea era "ya lo vio el fetch sync de
    // arriba, acá solo hace falta una base para detectar cambios futuros".
    // Pero export_flight_path.py escribe bounds.json Y flight_path.geojson
    // en el mismo instante, y bounds.json no vuelve a cambiar hasta que
    // generate_tiles.py corre —mucho después, tras la reconstrucción ODM—.
    // Si la página se abrió ANTES de que export_flight_path.py terminara
    // (el caso normal: la webapp redirige acá apenas arranca la corrida),
    // el fetch sync inicial daba 404 y esta era la ÚNICA lectura que iba a
    // ver el archivo recién aparecido; al cortar acá, tryLoadFlightPath()
    // nunca se llamaba y la ruta de vuelo no se registraba nunca, aunque el
    // archivo ya existiera en disco. Registrar acá es seguro aunque el
    // sync de arriba ya lo haya hecho: cada tryLoad*/register* de abajo es
    // idempotente (chequea LAYER_REGISTRY antes de hacer nada).
    if(sig===lastBoundsSig)return;
    lastBoundsSig=sig;
    let b={};
    try{b=JSON.parse(sig);}catch(e){}
    if(!boundsWasReal&&b.center){map.setView(b.center,b.zoom||ZOOM);boundsWasReal=true;}
    // PRELIMINARY se fija normalmente en el fetch sync de arriba, al cargar
    // la página. Si ESE fetch dio 404 (bounds.json todavía no existía),
    // queda pegado en el default `false` para siempre — y con eso,
    // tryLoadFlightPath() de más abajo registraría la ruta de vuelo pero
    // apagada, aunque la corrida siga en curso y sea justo lo único que hay
    // para mostrar. Se refresca acá con el dato fresco de esta lectura.
    //
    // OJO: la asignación es INCONDICIONAL — `b.preliminary` viene undefined
    // (no `false`) apenas generate_tiles.py reescribe bounds.json con la
    // versión definitiva (no incluye la marca, ver su docstring "lo pisa sin
    // la marca al terminar"). Con el guard `if(b.preliminary!==undefined)`
    // que había antes, esa reescritura NUNCA se veía acá — PRELIMINARY se
    // quedaba en `true` para siempre una vez que arrancaba en true, y la
    // ruta de vuelo (abajo) jamás se apagaba sola aunque ya hubiera mosaico
    // disponible. Bug real, reportado en vivo.
    const wasPreliminary=PRELIMINARY;
    PRELIMINARY=!!b.preliminary;
    // Recién con un mosaico real disponible (PRELIMINARY true→false) la ruta
    // de vuelo estorba más de lo que aporta (mismo criterio que su
    // `defaultOn` al registrarse) — se apaga sola, no hace falta que el
    // usuario la destilde a mano. Si el usuario ya la había apagado antes,
    // `map.hasLayer` da false y esto no hace nada.
    if(wasPreliminary&&!PRELIMINARY&&LAYER_REGISTRY.flight_path&&map.hasLayer(flightLayer)){
      map.removeLayer(flightLayer);
      renderCapasPanel();
    }
    if(b.thermal_range){THERMAL_MIN=b.thermal_range[0];THERMAL_MAX=b.thermal_range[1];}
    // Tiles regenerados (nuevo tiles_v): los Grid personalizados (thermal,
    // índices, hotspot, ms_composite) leen TILES_V directo en
    // cada createTile(), así que el redraw() genérico de más abajo alcanza
    // para esos. rgb/dband/hillshade son L.tileLayer ESTÁNDAR: su plantilla
    // de URL queda fija al construirse, redraw() sobre ellas re-pide los
    // mismos tiles con la MISMA url vieja — necesitan setUrl() explícito
    // acá para que la próxima pasada use el nuevo ?v=.
    if(b.tiles_v&&b.tiles_v!==TILES_V){
      TILES_V=b.tiles_v;
      rgbLayer.setUrl(tileTpl('rgb'));
      dbandLayer.setUrl(tileTpl('dband'));
      hillshadeLayer.setUrl(tileTpl('hillshade'));
    }
    let added=false;
    if(b.index_ranges)Object.entries(b.index_ranges).forEach(([name,range])=>{
      INDEX_RANGES[name]=range;
      if(registerIndexLayer(name))added=true;
    });
    if(b.ms_band_ranges&&Object.keys(b.ms_band_ranges).length){
      MS_BAND_RANGES=b.ms_band_ranges;
      liveMsBandIds=Object.keys(MS_BAND_RANGES);
      if(registerMsComposite())added=true;
    }
    if(b.capas_disponibles){
      CAPAS_DISPONIBLES=new Set(b.capas_disponibles);
      if(registerDband())added=true;
      // A diferencia del registro inicial (línea ~448, seguido del loop que
      // agrega al mapa toda capa con defaultOn), un registro que llega
      // DESPUÉS, con la misión todavía procesando, nunca pasa por ese loop: sin
      // esto, hotspot_termico podía terminar con defaultOn:true y aun así
      // no aparecer solo hasta que el usuario lo tildara a mano.
      if(registerHotspot()){added=true;if(LAYER_REGISTRY.hotspot_termico.defaultOn)hotspotLayer.addTo(map);}
      Object.keys(INDEX_CLASS_INFO).forEach(n=>{if(registerIndexClass(n))added=true;});
    }
    if(await tryLoadFlightPath())added=true;
    for(const s of ['rgb','thermal','multispectral'])if(await tryLoadHull(s))added=true;
    for(const k of ['experto','detectada'])if(await tryLoadBurnedArea(k))added=true;
    if(added)renderCapasPanel();
    // Redibuja TODAS las capas ráster ya registradas: sus tiles pueden haber
    // mejorado (rgb/thermal/hillshade están registrados desde el arranque,
    // sin depender de capas_disponibles, así que un producto preliminar puede
    // haberse reemplazado por el final entre una pasada y la siguiente).
    Object.values(LAYER_REGISTRY).forEach(d=>{ if(d.layer.redraw)d.layer.redraw(); });
    // situation.json aparece recién en la etapa de hotspot (bastante
    // después que bounds.json cambie por primera vez). Se reintenta cada
    // vez que bounds.json cambia, no solo una vez al final. flight_quality.json
    // sigue el mismo patrón (aparece bastante antes, en la etapa de recorte
    // térmico, pero se recarga igual acá para agarrar el caso de una misión
    // que arranca sin RGB+térmico todavía trimeados).
    const prevSituation=SITUATION;
    const prevFQ=JSON.stringify(FLIGHT_QUALITY);
    await loadSituation();
    await loadFlightQuality();
    if(JSON.stringify(prevSituation)!==JSON.stringify(SITUATION)||prevFQ!==JSON.stringify(FLIGHT_QUALITY)){
      await renderSituationHeader();
      await renderSummaryCards();
      renderCapasPanel();   // refresca los focos identificados dentro de la tarjeta Hotspot
    }else if(added){
      renderCapasPanel();
    }
  }catch(e){}
}

// ═══════════════════════════════════════════════════════════════════
// HUD DE PROGRESO: la webapp y el geovisor son UNA sola pantalla
// ═══════════════════════════════════════════════════════════════════
// Antes: arrancar una misión mostraba una pantalla de progreso aparte (en
// la webapp) y solo AL TERMINAR había un botón para pasar al geovisor. Acá
// el geovisor ES la pantalla de progreso: se abre apenas arranca la
// corrida (webapp/static/index.html navega directo a esta página con
// ?mission=<nombre>), y este bloque se conecta al MISMO endpoint SSE que
// antes consumía la webapp (/api/missions/<mision>/events) para llenar el
// HUD, sin reimplementar nada del lado del servidor.
//
// Si la misión del parámetro NO es la que el servidor tiene activa (p.ej.
// se abre el link de una misión ya vieja, en otra sesión), /events
// responde 404 y el HUD simplemente no se muestra. No hace falta
// distinguir "en vivo" de "ya terminada" a mano, el propio 404 lo resuelve.
// (urlMission ya se definió arriba de todo, antes de leer bounds.json)
let phTimerInterval=null, phServerElapsed=0, phServerElapsedAt=0, phDone=false;

function fmtElapsed(s){
  s=Math.max(0,Math.round(s));
  const m=String(Math.floor(s/60)).padStart(2,'0'),ss=String(s%60).padStart(2,'0');
  return `${m}:${ss}`;
}
function phTick(){
  const el=document.getElementById('ph-time');
  if(el){
    const live=phServerElapsed+(Date.now()-phServerElapsedAt)/1000;
    el.textContent=fmtElapsed(live);
  }
}
function toggleProgressLog(){
  const log=document.getElementById('ph-log'),btn=document.getElementById('ph-log-toggle');
  const open=log.classList.toggle('open');
  // El label vive en su propio span (el SVG del chevron se conserva: poner
  // textContent sobre el botón entero lo borraría — por eso antes se usaban
  // los emojis ▴/▾ que desentonaban con el sprite SVG del resto de la UI).
  btn.classList.toggle('open',open);
  const lbl=document.getElementById('ph-log-label');
  if(lbl)lbl.textContent=open?t('ph.hideLog'):t('ph.viewLogWord');
  if(open)log.scrollTop=log.scrollHeight;
}
// Colapsa el CUADRO entero (barra + fases + log), no solo el log — deja a
// la vista únicamente la franja de arriba (punto + etapa + cronómetro) para
// que el resumen gerencial no quede empujado hacia abajo mientras una
// corrida larga sigue viva.
function toggleProgressHud(){
  const hud=document.getElementById('progress-hud');
  const btn=document.getElementById('ph-collapse-toggle');
  const collapsed=hud.classList.toggle('collapsed');
  btn.setAttribute('aria-expanded',String(!collapsed));
  btn.setAttribute('aria-label',collapsed?t('ph.expandAria'):t('ph.collapseAria'));
}
document.getElementById('ph-collapse-toggle')?.addEventListener('click',toggleProgressHud);
function phSetStage(html){
  // innerHTML, no textContent: el handler 'done' (más abajo) pasa un ícono
  // SVG + texto ("<svg...><use.../></svg> Falló (código N)") para pintar el
  // check/cruz junto al estado. Con textContent ese markup se mostraba
  // LITERAL en pantalla — el tag entero como texto — en vez de renderizar
  // el ícono. Los otros dos llamadores pasan texto plano (nombre de etapa,
  // controlado por scripts/progress.py del propio pipeline, no input de
  // usuario) y siguen andando igual con innerHTML.
  const el=document.getElementById('ph-stage');
  if(el)el.innerHTML=html;
}
// ═══════════════════════════════════════════════════════════════════
// 3 PROGRESOS INDEPENDIENTES (RGB / térmico / multiespectral): cada sensor
// corre su propia cadena preparación→ODM→recorte/exportación en un subshell
// propio (docker/entrypoint.sh, despacho por sensor) — con una sola barra
// global (lo que había antes, ver el historial de este archivo) el "n/total"
// que se mostraba no era un progreso real del pipeline: eran TRES contadores
// distintos pisándose en el mismo canal (cada target de Make se anuncia
// "1/1", ODM reinicia en "1/13" por cada sensor) y la barra "retrocedía"
// cada vez que otro sensor de fondo imprimía su propio evento. Acá cada
// sensor tiene su PROPIO 0-100%, calculado 100% del lado del navegador
// (mismo principio que antes: mapear el NOMBRE fijo de cada etapa contra
// una lista conocida de antemano) — sin tocar el backend.
//
// Los eventos "bar" (% en vivo durante MVS/depthmaps de ODM,
// odm_progress_filter.py) NO llevan qué sensor los generó — solo
// current/total/label (el ETA), no el nombre de la etapa — así que con
// sensores reconstruyendo en paralelo no hay forma confiable de saber a
// cuál atribuirlos: se ignoran a propósito. El avance por checkpoint de
// abajo, más el n/13 real de ODM (que SÍ viaja en los eventos "stage"), ya
// da bastante resolución sin arriesgar una barra saltando al sensor
// equivocado.
function channelLabel(ch){ return t('channel.'+ch); }
// Checkpoints reales (Makefile/ODM) de la cadena de CADA sensor, en orden.
// El marcado odm:true además interpola con el n/total real que manda el
// evento "stage" de odm_progress_filter.py (13 etapas internas de ODM) dentro
// de su propio tramo, en vez de saltar de golpe al llegar. Nombres sacados
// de docker/entrypoint.sh + Makefile — mismo criterio que el catálogo de
// fases que reemplaza esto. `match` compara contra el nombre de etapa que
// manda el SERVIDOR (Python, siempre en español) — eso NO se traduce, es un
// identificador, no texto de UI; `labelKey` sí, resuelto con t() recién en
// updateChannel(), para que quede al día si cambia el idioma entre eventos.
const CHANNEL_STEPS={
  rgb:[
    {labelKey:'channel.prep', match:n=>n==='Preparación imágenes RGB'},
    {labelKey:'channel.recon3d', match:n=>n.startsWith('ODM RGB'), odm:true},
    {labelKey:'channel.trimExport',
     match:n=>n==='Limpieza del DSM'||n==='Recorte de bordes del DSM'||n==='Recorte de bordes RGB'},
  ],
  thermal:[
    {labelKey:'channel.prep',
     match:n=>n==='Conversión R-JPEG → °C (DJI SDK)'||n==='Filtro bilateral (denoise)'
       ||n==='Preparación térmica nativa (ODM)'},
    {labelKey:'channel.recon3d', match:n=>n.startsWith('ODM THERMAL'), odm:true},
    {labelKey:'channel.trimExport', match:n=>n==='Recorte de bordes térmicos'},
  ],
  ms:[
    {labelKey:'channel.prep', match:n=>n==='Preparación bandas multiespectrales'},
    {labelKey:'channel.recon3d', match:n=>n.startsWith('ODM MULTISPECTRAL'), odm:true},
    {labelKey:'channel.trimIndices',
     match:n=>n==='Recorte de bordes multiespectrales'||n==='Índices de vegetación (NDVI/GNDVI/NDRE)'},
  ],
};
// A qué canal pertenece el nombre de etapa que llega por SSE — null para
// todo lo que NO es de un solo sensor (ruta de vuelo, banda D, máscara de
// confianza, hotspot térmico, tiles, COG/COPC, entrega): a propósito quedan
// fuera de los 3 progresos, que son solo rgb/térmico/ms.
function channelFor(name){
  for(const [ch,steps] of Object.entries(CHANNEL_STEPS))
    if(steps.some(s=>s.match(name)))return ch;
  return null;
}
let CHANNELS={};   // {rgb:{pct,phase,status}, ...} — solo los sensores activos en ESTA corrida
function buildChannels(ctx){
  CHANNELS={};
  ['rgb','thermal','ms'].forEach(ch=>{
    if(ctx[ch])CHANNELS[ch]={pct:0,phase:t('channel.waiting'),status:'pending'};
  });
  renderChannels();
}
function updateChannel(name,n,total){
  const ch=channelFor(name);
  if(!ch||!CHANNELS[ch])return;
  const steps=CHANNEL_STEPS[ch],idx=steps.findIndex(s=>s.match(name));
  if(idx===-1)return;
  let pct=(idx/steps.length)*100;
  if(steps[idx].odm&&n&&total)pct+=((n-1)/total)*(100/steps.length);
  const c=CHANNELS[ch];
  c.status='running';
  c.phase=t(steps[idx].labelKey)+(steps[idx].odm&&n&&total?` (${n}/${total})`:'');
  // Nunca retrocede: dos invocaciones de ODM por sensor (ver run_odm() en
  // docker/entrypoint.sh, fase SfM liviana + fase MVS pesada) reimprimen
  // "Running dataset stage" desde 1 aunque sea un no-op real la segunda vez.
  c.pct=Math.max(c.pct||0,Math.min(99,Math.round(pct)));
  renderChannels();
}
// Al terminar la corrida entera: lo que ya llegó a (casi) 100% se cierra en
// 100/done sin importar si OTRO sensor fue el que falló; lo que se quedó
// atrás cuando la corrida terminó mal es, con alta probabilidad, el sensor
// que rompió — se marca "failed" en vez de dejarlo pegado a mitad de barra
// sin explicación.
function finishChannels(ok){
  Object.values(CHANNELS).forEach(c=>{
    if(ok||c.pct>=99){c.pct=100;c.status='done';}
    else c.status='failed';
  });
  renderChannels();
}
function renderChannels(){
  const box=document.getElementById('ph-channels');
  if(!box)return;
  box.innerHTML=Object.entries(CHANNELS).map(([ch,c])=>`
    <li class="ph-channel ${c.status}">
      <div class="ph-channel-head">
        <span class="ph-channel-name">${channelLabel(ch)}</span>
        <span class="ph-channel-phase">${c.status==='failed'?t('channel.failed'):c.phase}</span>
        <span class="ph-channel-pct">${c.pct}%</span>
      </div>
      <div class="ph-bar"><div class="ph-bar-fill" style="width:${c.pct}%"></div></div>
    </li>`).join('');
}

const MAX_PH_LOG_LINES=600; // ventana acotada: una corrida entera son miles de líneas
let phLogLines=[];
function phAppendLog(line){
  phLogLines.push(line);
  if(phLogLines.length>MAX_PH_LOG_LINES)phLogLines=phLogLines.slice(-MAX_PH_LOG_LINES);
  phRenderLog();
}
function phRenderLog(){
  const box=document.getElementById('ph-log');
  if(!box)return;
  box.textContent=phLogLines.join('\n');
  if(box.classList.contains('open'))box.scrollTop=box.scrollHeight;
}

function connectLiveMission(mission){
  const hud=document.getElementById('progress-hud');
  if(!hud)return;
  let boundsPoll=null;
  const es=new EventSource(`/api/missions/${encodeURIComponent(mission)}/events`);

  es.onopen=()=>{
    hud.classList.add('visible');
    boundsPoll=setInterval(pollBoundsForChanges,6000);
    pollBoundsForChanges(); // primera lectura: fija lastBoundsSig, no espera 6s
    labelMission();
    // Cancelar: mientras la corrida sigue, no cuando ya terminó — el
    // handler 'done' de más abajo reemplaza este botón por "Ver log
    // completo"/"Cerrar".
    const actions=document.getElementById('ph-actions');
    if(actions){
      actions.innerHTML='';
      const cancel=document.createElement('button');
      cancel.className='btn sm';cancel.innerHTML=`<svg class="ic" style="width:13px;height:13px" aria-hidden="true"><use href="#i-x"/></svg> ${t('progress.cancel')}`;
      cancel.onclick=async()=>{
        if(!confirm(t('progress.cancelConfirm')))return;
        cancel.disabled=true;cancel.textContent=t('progress.cancelling');
        try{
          const r=await fetch(`/api/missions/${encodeURIComponent(mission)}/cancel`,{method:'POST'});
          if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.detail||t('progress.cancelFailedGeneric'));}
        }catch(e){
          alert(t('progress.cancelFailedAlert')+e.message);
          cancel.disabled=false;cancel.innerHTML=`<svg class="ic" style="width:13px;height:13px" aria-hidden="true"><use href="#i-x"/></svg> ${t('progress.cancel')}`;
        }
      };
      actions.appendChild(cancel);
    }
  };
  es.onmessage=(ev)=>{
    let d; try{d=JSON.parse(ev.data);}catch(e){return;}
    if(d.kind==='hello'){
      phServerElapsed=d.elapsed||0; phServerElapsedAt=Date.now();
      if(phTimerInterval)clearInterval(phTimerInterval);
      phTimerInterval=setInterval(phTick,1000); phTick();
      if(d.mode)phSetStage(t('progress.processing'));
      buildChannels({rgb:d.mode!=='thermal'&&d.mode!=='none',
        thermal:(d.mode||'').includes('thermal'),ms:!!d.has_multispectral});
    }else if(d.kind==='progress'){
      if(d.event==='stage')updateChannel(d.name,d.n?parseInt(d.n,10):null,d.total?parseInt(d.total,10):null);
    }else if(d.kind==='log'){
      phAppendLog(d.line);
    }else if(d.kind==='done'){
      phDone=true;
      if(phTimerInterval)clearInterval(phTimerInterval);
      if(boundsPoll)clearInterval(boundsPoll);
      phServerElapsed=d.elapsed||phServerElapsed; phTick();
      const ok=d.returncode===0;
      hud.classList.add(ok?'done':'failed');
      phSetStage(ok
        ? `<svg class="ic" style="width:13px;height:13px;color:var(--good)" aria-hidden="true"><use href="#i-check"/></svg> ${t('progress.complete')}`
        : `<svg class="ic" style="width:13px;height:13px;color:var(--critical)" aria-hidden="true"><use href="#i-x"/></svg> ${t('channel.failed')} (${t('progress.code')} ${d.returncode})`);
      finishChannels(ok);
      const actions=document.getElementById('ph-actions');
      if(actions){
        actions.innerHTML='';
        if(!ok){
          const b=document.createElement('button');
          b.className='btn sm';b.textContent=t('progress.viewFullLog');
          b.onclick=toggleProgressLog;
          actions.appendChild(b);
        }
        const close=document.createElement('button');
        close.className='btn sm primary';close.textContent=t('report.closeAria');
        close.onclick=()=>hud.classList.remove('visible');
        actions.appendChild(close);
      }
      pollBoundsForChanges(); // última pasada: productos finales (hotspot, índices, etc.)
      es.close();
      // Recién ahora, con la corrida terminada, tiene sentido re-etiquetar
      // "productos de la misión" (antes decía cuántas misiones hay en la
      // lista, dato que no cambió por esto).
      labelMission();
    }
  };
  es.onerror=()=>{
    // Si el servidor nunca trackeó esta misión como activa (link viejo,
    // otra sesión), la primera respuesta ya viene con status 404. No hay
    // "reintentos infinitos silenciosos": se cierra y no se muestra nada,
    // a menos que haya una reanudación activa en outputs/resume_status.json.
    if(!hud.classList.contains('visible')){
      es.close();
      if(boundsPoll)clearInterval(boundsPoll);
      pollResumeStatus(mission, hud);
    }
  };
}

let resumePoll=null;
function pollResumeStatus(mission, hud){
  let wasRunning=false;
  async function check(){
    try{
      const r=await fetch(`/geovisor/outputs/resume_status.json?t=${Date.now()}`, {cache:'no-store'});
      if(!r.ok){
        if(resumePoll){ clearInterval(resumePoll); resumePoll=null; }
        return;
      }
      const s=await r.json();
      if(!s){
        if(resumePoll){ clearInterval(resumePoll); resumePoll=null; }
        return;
      }
      // Si la misión no está corriendo y nunca la vimos correr en esta sesión de página,
      // no mostrar el HUD de progreso: la misión ya finalizó.
      if(!s.running && !wasRunning){
        if(resumePoll){ clearInterval(resumePoll); resumePoll=null; }
        if(hud){
          hud.classList.remove('visible');
          hud.classList.remove('running');
        }
        return;
      }

      // Si está corriendo o acaba de terminar mientras la observábamos:
      wasRunning=true;
      hud.classList.add('visible');
      labelMission();
      if(s.elapsed_s !== undefined){
        phServerElapsed=s.elapsed_s; phServerElapsedAt=Date.now();
        if(!phTimerInterval){ phTimerInterval=setInterval(phTick,1000); phTick(); }
      }
      if(s.stage) phSetStage(s.stage + (s.substage ? ` <span style="font-size:11px;opacity:0.75">(${s.substage})</span>` : ''));
      buildChannels({rgb:true, thermal:true, ms:false});
      if(s.channel_progress){
        updateChannel(s.channel_progress.name, s.channel_progress.n, s.channel_progress.total);
      }
      if(s.log_tail && Array.isArray(s.log_tail) && s.log_tail.length){
        phLogLines=s.log_tail;
        phRenderLog();
      }
      if(s.done || !s.running){
        phDone=true;
        if(phTimerInterval) clearInterval(phTimerInterval);
        hud.classList.remove('running');
        hud.classList.add(s.error?'failed':'done');
        phSetStage(s.error
          ? `<svg class="ic" style="width:13px;height:13px;color:var(--critical)" aria-hidden="true"><use href="#i-x"/></svg> Falló`
          : `<svg class="ic" style="width:13px;height:13px;color:var(--good)" aria-hidden="true"><use href="#i-check"/></svg> ${s.stage||'Completado'}`);
        finishChannels(!s.error);
        const actions=document.getElementById('ph-actions');
        if(actions){
          actions.innerHTML='';
          const close=document.createElement('button');
          close.className='btn sm primary'; close.textContent=t('report.closeAria');
          close.onclick=()=>hud.classList.remove('visible');
          actions.appendChild(close);
        }
        if(resumePoll){ clearInterval(resumePoll); resumePoll=null; }
        pollBoundsForChanges();
        labelMission();
      }
    }catch(_){}
  }
  check();
  if(!resumePoll) resumePoll=setInterval(check, 2000);
}
if(urlMission)connectLiveMission(urlMission);

// ═══════════════════════════════════════════════════════════════════
// TEMA CLARO / OSCURO
// ═══════════════════════════════════════════════════════════════════
// El oscuro es el default (ortofotos y mapas de calor se leen mejor sobre
// fondo oscuro), pero en campo, con la pantalla al sol, es directamente ilegible.
// Todo el color va por variables CSS, así que alcanza con marcar <html>.
const THEME_KEY='raptor-geovisor-theme';
function applyTheme(t){
  document.documentElement.setAttribute('data-theme',t);
  // theme-ic ahora lleva el par de iconos SVG (sol/luna) como en la webapp;
  // el CSS decide cuál se ve según data-theme. Sin emojis: cada plataforma
  // renderiza el glifo a su manera y en campo no hay que depender de eso.
  const ic=document.getElementById('theme-ic');
  if(ic && !ic.querySelector('svg')){
    ic.innerHTML = `<svg class="ic ic-sun" aria-hidden="true"><use href="#i-sun"/></svg>`
                 + `<svg class="ic ic-moon" aria-hidden="true"><use href="#i-moon"/></svg>`;
  }
  // El theme-color de la barra del navegador sigue al tema manual. Se
  // actualizan TODOS los <meta> (cada uno con su media): el navegador
  // aplica el que matchee la preferencia de sistema, y tocar solo el
  // primero dejaría la barra oscura en un sistema oscuro con tema claro.
  document.querySelectorAll('meta[name=theme-color]').forEach(m=>
    m.setAttribute('content', t==='dark' ? '#0C1412' : '#F4F7F8'));
  try{localStorage.setItem(THEME_KEY,t);}catch(e){}
}
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme')||'dark';
  applyTheme(cur==='dark'?'light':'dark');
}
(function initTheme(){
  let t=null;
  try{t=localStorage.getItem(THEME_KEY);}catch(e){}
  if(!t)t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';
  applyTheme(t);
})();

// ═══════════════════════════════════════════════════════════════════
// IDIOMA (EN/ES): apply/toggle/init — mismo patrón que el tema arriba.
// El diccionario I18N y t() viven al principio del archivo, no acá (ver su
// propio comentario sobre por qué).
// ═══════════════════════════════════════════════════════════════════
// Etiquetas de LAYER_REGISTRY: quedan grabadas como string plana en cada
// entrada (no como función, para no tocar los pocos lugares que leen
// `.label` directo — layerCardHTML(), buildCompareSelect(), la leyenda del
// reporte). Se recalculan acá, a mano, cada vez que cambia el idioma.
function refreshLayerLabels(){
  const setLbl=(id,val)=>{ if(LAYER_REGISTRY[id])LAYER_REGISTRY[id].label=val; };
  setLbl('hillshade',t('layer.hillshade'));
  setLbl('rgb',t('layer.rgb'));
  setLbl('dband',t('layer.dband'));
  setLbl('ms_composite',t('layer.msComposite'));
  setLbl('thermal',t('layer.thermal'));
  setLbl('hotspot_termico',t('layer.hotspot'));
  setLbl('flight_path',t('layer.flightPath'));
  INDEX_NAMES.forEach(name=>{ setLbl(name,indexLabelInfo(name)[0]); });
  Object.keys(INDEX_CLASS_INFO).forEach(name=>{ setLbl(name,indexClassLabel(name)); });
  ['rgb','thermal','multispectral'].forEach(sensor=>{ setLbl('hull_'+sensor,`${t('layer.hull')} · ${sensorLabel(sensor)}`); });
}
function applyLang(lang){
  LANG=lang==='es'?'es':'en';
  document.documentElement.setAttribute('lang',LANG);
  document.querySelectorAll('[data-i18n]').forEach(el=>{ el.textContent=t(el.getAttribute('data-i18n')); });
  document.querySelectorAll('[data-i18n-html]').forEach(el=>{ el.innerHTML=t(el.getAttribute('data-i18n-html')); });
  document.querySelectorAll('[data-i18n-title]').forEach(el=>{ el.setAttribute('title',t(el.getAttribute('data-i18n-title'))); });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(el=>{ el.setAttribute('aria-label',t(el.getAttribute('data-i18n-aria-label'))); });
  const btn=document.getElementById('btn-lang'); if(btn)btn.textContent=LANG==='en'?'ES':'EN';
  document.title=t('doc.title');
  // Control de Leaflet "Encuadrar toda la misión": FitBoundsControl lo arma
  // con L.DomUtil, no vive en index.html, así que no tiene data-i18n — se
  // refresca a mano por su clase.
  const fitBtn=document.querySelector('.map-extra-control');
  if(fitBtn){ fitBtn.title=t('map.fitBounds'); fitBtn.setAttribute('aria-label',t('map.fitBounds')); }
  refreshLayerLabels();
  // El grueso de la interfaz (panel de Capas, tarjetas de resumen, franja de
  // situación, comparar en el tiempo, nombre de la misión) NO se arma con
  // data-i18n: son funciones JS que reconstruyen su HTML entero cada vez que
  // corren (ver sus propios comentarios más abajo). Re-correrlas acá es lo
  // que de verdad cambia el idioma ahí — re-etiquetar atributos no alcanza.
  // Todas van guardadas: esta función se llama por primera vez desde
  // initLang() (ver más abajo), mucho antes de que loadSituation()/
  // initPanel() terminen de traer datos reales.
  try{ renderCapasPanel(); }catch(e){}
  try{ renderSituationHeader(); }catch(e){}
  try{ renderSummaryCards(); }catch(e){}
  try{ checkRelatedMissions(); }catch(e){}
  try{ labelMission(); }catch(e){}
  try{
    if(Object.keys(CHANNELS).length){
      Object.values(CHANNELS).forEach(c=>{ if(c.status==='pending')c.phase=t('channel.waiting'); });
      renderChannels();
    }
  }catch(e){}
  // Selectores del comparador (si ya está abierto): sus <option> quedan con
  // las etiquetas del idioma anterior hasta que se releen acá.
  try{
    if(compareActive){
      document.querySelectorAll('.compare-select').forEach(sel=>{
        Array.from(sel.options).forEach(opt=>{ if(LAYER_REGISTRY[opt.value])opt.textContent=LAYER_REGISTRY[opt.value].label; });
      });
    }
  }catch(e){}
  try{ localStorage.setItem(LANG_KEY,LANG); }catch(e){}
}
function toggleLang(){ applyLang(LANG==='en'?'es':'en'); }
(function initLang(){
  let l=null;
  try{ l=localStorage.getItem(LANG_KEY); }catch(e){}
  applyLang(l==='es'?'es':'en'); // default SIEMPRE inglés, nunca navigator.language
})();
document.getElementById('btn-lang')?.addEventListener('click',toggleLang);

// ═══════════════════════════════════════════════════════════════════
// ACCIONES SOBRE CAPAS (solo / encuadrar / apagar todo / restablecer)
// ═══════════════════════════════════════════════════════════════════
function soloLayer(id){
  const def=LAYER_REGISTRY[id];
  if(!def)return;
  // GLOBAL, no solo dentro del grupo: la primera versión aislaba nada más
  // que los hermanos del mismo grupo temático (p.ej. "solo" en RGB dejaba
  // el térmico prendido, porque vive en otro grupo). El resultado visible
  // era indistinguible de que el botón no hiciera nada. "Solo" ahora apaga
  // TODO lo demás, sin excepción: es el modelo mental simple que alguien sin
  // experiencia en GIS espera de un botón así.
  const todas=Object.entries(LAYER_REGISTRY);
  const yaSolo=map.hasLayer(def.layer)&&todas.every(([oid,d])=>oid===id||!map.hasLayer(d.layer));
  todas.forEach(([oid,d])=>{
    const on = yaSolo ? !!d.defaultOn : (oid===id);
    if(on)d.layer.addTo(map); else map.removeLayer(d.layer);
  });
  renderCapasPanel();
}

// Extensión de los tiles: se lee de bounds.json/ruta de vuelo, que es lo
// único que conoce la geometría real de la misión desde el navegador.
let MISSION_BOUNDS=null;
try{
  if(typeof flightLayer!=='undefined'){
    const b=flightLayer.getBounds&&flightLayer.getBounds();
    if(b&&b.isValid())MISSION_BOUNDS=b;
  }
}catch(e){}
function zoomToLayer(id){
  const def=LAYER_REGISTRY[id];
  if(!def)return;
  let b=null;
  try{ if(def.layer.getBounds){const bb=def.layer.getBounds(); if(bb&&bb.isValid())b=bb;} }catch(e){}
  if(!b)b=MISSION_BOUNDS;
  if(b)map.fitBounds(b,{padding:[40,40]});
  else map.setView(CENTER,ZOOM);
}
function setAllLayers(on){
  Object.values(LAYER_REGISTRY).forEach(d=>{
    if(on)d.layer.addTo(map); else map.removeLayer(d.layer);
  });
  renderCapasPanel();
}
function resetLayers(){
  Object.values(LAYER_REGISTRY).forEach(d=>{
    if(d.defaultOn)d.layer.addTo(map); else map.removeLayer(d.layer);
    if(d.layer.setOpacity)d.layer.setOpacity(d.defaultOpacity??1);
  });
  map.setView(CENTER,ZOOM);
  renderCapasPanel();
}

// ═══════════════════════════════════════════════════════════════════
// EXPORTAR MAPA (PNG de la vista actual, no un volcado de JSON)
// ═══════════════════════════════════════════════════════════════════
// El mapa base (OSM/Satélite) es de un servidor EXTERNO sin cabecera CORS
// garantizada. Dibujar ese píxel en el mismo canvas que después se lee con
// toDataURL() "contamina" el canvas ENTERO (no solo ese tile) y el
// navegador tira SecurityError al exportar. Para no depender de que un
// tercero decida agregar CORS algún día, la exportación directamente NO
// toca el mapa base: compone solo las capas de DATOS (todas servidas por
// este mismo geovisor, mismo origen) sobre un fondo sólido, y lo dice en el
// pie de la imagen. Resultado 100% predecible en vez de "a veces funciona
// según qué capa esté prendida".
//
// Los tiles de datos ya están en el DOM como <img>/<canvas> (Leaflet los
// mantiene ahí mientras la capa está activa). Se leen sus posiciones reales
// en pantalla con getBoundingClientRect() en vez de recalcular la matemática
// interna de teselado: más simple y no depende de la versión de Leaflet.
// captureMapSnapshot(): la composición del MAPA en sí (sin encabezado ni
// estadísticas). Devuelve el canvas crudo (no un dataURL) más sus medidas
// LÓGICAS (en px CSS, no de dispositivo), para poder incrustarlo dentro de
// un canvas más grande vía drawImage() sin lidiar con el devicePixelRatio
// dos veces. La usan exportView() (descarga directa, solo el mapa) y
// buildReportCanvas() (el mapa como una pieza más del resumen completo).
async function captureMapSnapshot(){
  try{ if(document.fonts&&document.fonts.ready)await document.fonts.ready; }catch(e){}
  {
    const mapEl=document.getElementById('map');
    const rect=mapEl.getBoundingClientRect();
    const dpr=Math.min(window.devicePixelRatio||1,2);
    const PAD_BOTTOM=38; // franja solo para la escala + norte (sin texto de pie: ver más abajo)
    const width=rect.width, height=rect.height+PAD_BOTTOM;
    const canvas=document.createElement('canvas');
    canvas.width=Math.round(width*dpr);
    canvas.height=Math.round(height*dpr);
    const ctx=canvas.getContext('2d');
    ctx.scale(dpr,dpr);

    // Fondo SIEMPRE blanco, sin importar el tema activo de la UI (antes
    // seguía el tema oscuro por defecto (#0d1117, casi negro) y esa franja
    // se veía donde el mosaico no cubre el rectángulo completo del mapa:
    // "sale fondo negro" en la imagen exportada). Un reporte para compartir
    // o imprimir se comporta como una hoja, no como la interfaz.
    ctx.fillStyle='#ffffff';
    ctx.fillRect(0,0,rect.width,rect.height+PAD_BOTTOM);

    // Capas ráster: cualquier <img>/<canvas> dentro del pane de una capa
    // encendida, en el mismo orden en que se dibujan en el mapa real (de abajo hacia arriba).
    let anyRaster=false;
    const drawOrder=[...layerOrder].reverse();
    drawOrder.forEach(id=>{
      const def=LAYER_REGISTRY[id];
      if(!def||!map.hasLayer(def.layer))return;
      const pane=map.getPane('pane-'+id);
      if(!pane)return; // capas vectoriales (vuelo) se dibujan aparte, abajo
      const opacity=def.layer.options?.opacity??1;
      if(opacity<=0)return;
      pane.querySelectorAll('img,canvas').forEach(el=>{
        if(el.tagName==='IMG'&&!el.complete)return; // tile todavía cargando
        const r=el.getBoundingClientRect();
        const x=r.left-rect.left,y=r.top-rect.top;
        if(x+r.width<0||y+r.height<0||x>rect.width||y>rect.height)return;
        ctx.globalAlpha=opacity;
        try{ ctx.drawImage(el,x,y,r.width,r.height); anyRaster=true; }catch(e){ /* tile roto: se omite, no aborta toda la exportación */ }
      });
    });
    ctx.globalAlpha=1;

    // Vectores: se reproyectan a mano (lat/lng → pixel de pantalla) y se
    // dibujan con las mismas primitivas de canvas, no dependen de leer DOM.
    if(map.hasLayer(flightLayer)){
      flightLayer.eachLayer(l=>{
        if(l instanceof L.Polyline&&!(l instanceof L.Polygon)){
          const pts=l.getLatLngs();
          ctx.beginPath();
          pts.forEach((ll,i)=>{const p=map.latLngToContainerPoint(ll);if(i===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);});
          ctx.strokeStyle=l.options.color||'#58a6ff';ctx.lineWidth=l.options.weight||2;
          ctx.setLineDash([5,4]);ctx.stroke();ctx.setLineDash([]);
        }else if(l instanceof L.CircleMarker){
          const p=map.latLngToContainerPoint(l.getLatLng());
          ctx.beginPath();ctx.arc(p.x,p.y,l.options.radius||3,0,7);
          ctx.fillStyle=l.options.fillColor||'#58a6ff';ctx.fill();
        }
      });
    }

    // Escala: mismo cálculo conceptual que L.control.scale. Distancia real
    // entre dos puntos separados 100px en pantalla, redondeada a un número
    // "lindo" (1/2/5×10ⁿ), y la barra se dibuja proporcional a esa distancia.
    const p1=map.containerPointToLatLng([20,rect.height-10]);
    const p2=map.containerPointToLatLng([120,rect.height-10]);
    const metersPer100px=map.distance(p1,p2);
    const niceMeters=(()=>{
      const raw=metersPer100px,mag=Math.pow(10,Math.floor(Math.log10(raw)));
      const n=raw/mag;
      return (n>=5?5:n>=2?2:1)*mag;
    })();
    const barPx=100*(niceMeters/metersPer100px);
    const scaleLabel=niceMeters>=1000?(niceMeters/1000)+' km':Math.round(niceMeters)+' m';
    const textCol='#16202c'; // fondo siempre blanco acá abajo: texto siempre oscuro, sin depender del tema
    const sy=rect.height+18; // línea base de la barra de escala
    ctx.strokeStyle=textCol;ctx.fillStyle=textCol;ctx.lineWidth=2;
    ctx.beginPath();ctx.moveTo(20,sy);ctx.lineTo(20+barPx,sy);
    ctx.moveTo(20,sy-5);ctx.lineTo(20,sy+5);
    ctx.moveTo(20+barPx,sy-5);ctx.lineTo(20+barPx,sy+5);ctx.stroke();
    ctx.font='12px "Public Sans",sans-serif';ctx.textAlign='left';
    ctx.fillText(scaleLabel,20+barPx+8,sy+4);

    // Norte: mismo glifo "▲N" del control en pantalla (.coords-control
    // .compass, ver CoordsControl más arriba en este archivo). Antes era un
    // triángulo relleno dibujado a mano, con otra forma y otro color, que no
    // se parecía al indicador real del geovisor. La app no rota el mapa, así
    // que "arriba" siempre es norte.
    const accentCol=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()||'#2563eb';
    ctx.font='700 15px "Public Sans",sans-serif';ctx.textAlign='right';
    ctx.fillStyle=accentCol;
    ctx.fillText('▲N',rect.width-16,rect.height+22);

    // Sin pie de misión/fecha ni nota de mapa base acá a propósito: quien
    // llama a esta función decide si hace falta encabezado (buildReportCanvas()
    // ya pone nombre+fecha arriba de todo; exportView(), el export suelto de
    // "modo operativo", no necesita ninguno; el nombre del archivo alcanza).
    const missionName=document.getElementById('incident-name')?.textContent||'';

    if(!anyRaster&&!map.hasLayer(flightLayer)){
      throw new Error(t('export.noLayers'));
    }
    return {canvas,width,height,missionName};
  }
}

async function exportView(){
  const btn=document.getElementById('btn-export');
  const original=btn.innerHTML;
  btn.disabled=true;btn.innerHTML=t('export.generating');
  try{
    const {canvas,missionName}=await captureMapSnapshot();
    const url=canvas.toDataURL('image/png');
    const a=document.createElement('a');
    const safeMission=(missionName||'mapa').replace(/[^a-z0-9_-]+/gi,'_').slice(0,40);
    a.href=url;a.download=`${safeMission}_${new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')}.png`;
    a.click();
  }catch(err){
    alert(t('export.failMap')+err.message);
  }finally{
    btn.disabled=false;btn.innerHTML=original;
  }
}

// ═══════════════════════════════════════════════════════════════════
// IMAGEN DE "GENERAR RESUMEN DE SITUACIÓN": una sola pieza para compartir
// con todo lo necesario para decidir: encabezado (misión+fecha+confianza),
// las mismas 3 métricas del panel, el mapa, y la recomendación en texto.
// No un recorte del mapa solo, ESE es exportView()/"Exportar" (modo
// operativo). Esta es la versión para compartir con quien no va a abrir el
// geovisor.
// ═══════════════════════════════════════════════════════════════════
function buildRecommendationText(s){
  if(!s)return t('reco.noThermal');
  // Pluralización propia (no t()): la concordancia de género/número entre
  // "foco/focos térmico/térmicos activo/activos" (es) y "hotspot/hotspots"
  // (en) no es un simple lookup por clave, depende del número real.
  const n=s.hotspots_activos;
  const focos=LANG==='es'
    ? `${n} foco${n===1?'':'s'} térmico${n===1?'':'s'} activo${n===1?'':'s'}`
    : `${n} active thermal hotspot${n===1?'':'s'}`;
  // Esta pieza es para COMPARTIR fuera del geovisor (ver comentario de
  // sección más abajo): reporta temperatura real de TODO el ortomosaico
  // térmico, no solo el pico de un foco activo (que puede no haber ninguno
  // y aun así haber datos de temperatura que reportar).
  const temp=s.temp_max!=null
    ? (LANG==='es'
        ? `. Temp. superficial: máx ${s.temp_max}°C, promedio ${s.temp_promedio}°C`
        : `. Surface temperature: max ${s.temp_max}°C, average ${s.temp_promedio}°C`)
    : '';
  // La calidad del vuelo va aparte, en su propia píldora (ver
  // buildReportCanvas()), mezclarla acá duplicaba la misma cifra dos veces
  // en la misma imagen.
  return `${focos}${temp}.`;
}
function roundRectPath(ctx,x,y,w,h,r){
  const rr=Math.min(r,w/2,h/2);
  ctx.beginPath();
  ctx.moveTo(x+rr,y);
  ctx.arcTo(x+w,y,x+w,y+h,rr);
  ctx.arcTo(x+w,y+h,x,y+h,rr);
  ctx.arcTo(x,y+h,x,y,rr);
  ctx.arcTo(x,y,x+w,y,rr);
  ctx.closePath();
}
async function buildReportCanvas(){
  const {canvas:mapCanvas,width:mapW,height:mapH,missionName}=await captureMapSnapshot();
  const s=SITUATION||await loadSituation();
  const fq=FLIGHT_QUALITY||await loadFlightQuality();
  const mission=missionName||displayName(await missionReady)||t('doc.title');

  const cs=getComputedStyle(document.documentElement);
  const tok=n=>cs.getPropertyValue(n).trim();
  const ink=tok('--ink'),inkMuted=tok('--ink-muted'),surface=tok('--surface'),
    surface2=tok('--surface-2'),line=tok('--line'),accent=tok('--accent'),
    accentSoft=tok('--accent-soft'),onAccent=tok('--accent-ink'),
    good=tok('--good'),goodSoft=tok('--good-soft'),
    warning=tok('--warning'),warningSoft=tok('--warning-soft'),
    critical=tok('--critical'),criticalSoft=tok('--critical-soft');
  const F=(w,sz)=>`${w} ${sz}px "Public Sans",sans-serif`;

  // Urgencia general de la misión: atada a lo mismo que YA se muestra en las
  // tarjetas de arriba (focos activos), no un umbral aparte que pueda
  // contradecirlas.
  const urgentLevel=!s?'none':s.hotspots_activos>0?'critical':'good';
  const urgentLabel={critical:t('report.urgentCritical'),
    good:t('report.urgentGood'),none:t('report.urgentNone')}[urgentLevel];
  const urgentColor={critical,good,none:inkMuted}[urgentLevel];
  const urgentSoft={critical:criticalSoft,good:goodSoft,none:surface2}[urgentLevel];
  // Calidad del LEVANTAMIENTO (compute_flight_quality.py), no confianza del
  // dato de impacto. Ver renderSummaryCards()/flightQualityCardHTML() para
  // la explicación completa de por qué se reemplazó ese concepto.
  const calColor={buena:good,regular:warning,baja:critical}[fq?.calidad]||inkMuted;
  const calSoft={buena:goodSoft,regular:warningSoft,baja:criticalSoft}[fq?.calidad]||surface2;

  const dpr=Math.min(window.devicePixelRatio||1,2);
  const PAD=24, RADIUS=16, TOPBAR_H=6;
  const HEADER_H=88, STATS_H=112, FOOTER_H=s?92:56;
  const totalH=TOPBAR_H+HEADER_H+STATS_H+mapH+FOOTER_H;
  const out=document.createElement('canvas');
  out.width=Math.round(mapW*dpr);out.height=Math.round(totalH*dpr);
  const ctx=out.getContext('2d');
  ctx.scale(dpr,dpr);

  // Marco general redondeado. Sin esto el PNG es un rectángulo a lo bruto,
  // se ve "hecho en dos minutos" apenas se comparte sobre cualquier fondo
  // que no sea blanco puro (un chat, una presentación).
  roundRectPath(ctx,0,0,mapW,totalH,RADIUS);
  ctx.clip();
  ctx.fillStyle=surface;ctx.fillRect(0,0,mapW,totalH);

  // Franja de acento arriba de todo, la única nota de color puramente
  // decorativa de la pieza, a propósito: ancla la identidad de la
  // herramienta sin competir con el semántico (confianza/urgencia) que sí
  // significa algo.
  ctx.fillStyle=accent;ctx.fillRect(0,0,mapW,TOPBAR_H);

  // ── Encabezado ──────────────────────────────────────────────────────
  const headY=TOPBAR_H;
  ctx.fillStyle=accentSoft;ctx.fillRect(0,headY,mapW,HEADER_H);
  ctx.fillStyle=ink;ctx.font=F(700,23);ctx.textAlign='left';
  ctx.fillText(mission,PAD,headY+38);
  ctx.fillStyle=inkMuted;ctx.font=F(500,13.5);
  ctx.fillText(s?fmtFecha(s.captura):t('report.noImpactDataYet'),PAD,headY+60);
  if(fq){
    // Calidad del levantamiento como píldora de color, no texto suelto,
    // mismo lenguaje visual que los "chips" del panel en vivo.
    ctx.font=F(700,12.5);
    const pillLbl=`${t('report.flightQualityLabel')}${localizeValue(fq.calidad)}`;
    const pillW=ctx.measureText(pillLbl).width+28;
    const pillX=mapW-PAD-pillW,pillY=headY+22;
    roundRectPath(ctx,pillX,pillY,pillW,26,13);
    ctx.fillStyle=calSoft;ctx.fill();
    ctx.fillStyle=calColor;ctx.textAlign='center';
    ctx.fillText(pillLbl,pillX+pillW/2,pillY+17);
  }

  // ── Fila de métricas: 3 tarjetas reales, no columnas separadas por líneas ──
  const statsY=headY+HEADER_H;
  ctx.fillStyle=surface;ctx.fillRect(0,statsY,mapW,STATS_H);
  // Estadísticas de temperatura reales de todo el ortomosaico térmico
  // (compute_situation_summary.py), que siempre existen tenga o no focos
  // activos.
  const stats=!s?[['—','',t('report.urgentNone'),inkMuted,surface2]]:[
      [`${s.hotspots_activos}`,'',t('report.activeHotspots'),s.hotspots_activos>0?critical:ink,s.hotspots_activos>0?criticalSoft:surface2],
      [s.temp_max!=null?`${s.temp_max}`:'—','°C',t('report.maxTemp'),ink,surface2],
      [s.temp_promedio!=null?`${s.temp_promedio}`:'—','°C',t('report.avgTemp'),ink,surface2],
    ];
  const gap=12, cardW=(mapW-PAD*2-gap*(stats.length-1))/stats.length, cardH=STATS_H-24;
  stats.forEach(([val,unit,lbl,color,soft],i)=>{
    const cx0=PAD+i*(cardW+gap), cy0=statsY+12;
    roundRectPath(ctx,cx0,cy0,cardW,cardH,12);
    ctx.fillStyle=soft;ctx.fill();
    // Marca de color: un pequeño acento redondo arriba-izquierda de la
    // tarjeta en vez de teñir todo el fondo con demasiada fuerza: visible
    // pero no gritado.
    ctx.beginPath();ctx.arc(cx0+18,cy0+18,4,0,7);ctx.fillStyle=color;ctx.fill();
    const midx=cx0+cardW/2;
    ctx.textAlign='center';
    ctx.fillStyle=color;ctx.font=F(800,27);
    const valW=ctx.measureText(val).width;
    if(unit){
      ctx.font=F(600,14);const unitW=ctx.measureText(' '+unit).width;
      ctx.font=F(800,27);
      ctx.textAlign='left';ctx.fillText(val,midx-(valW+unitW)/2,cy0+52);
      ctx.font=F(600,14);ctx.fillText(' '+unit,midx-(valW+unitW)/2+valW,cy0+52);
      ctx.textAlign='center';
    }else{
      ctx.fillText(val,midx,cy0+52);
    }
    ctx.fillStyle=inkMuted;ctx.font=F(500,12.5);
    ctx.fillText(lbl,midx,cy0+74);
  });

  // ── El mapa, tal cual lo compuso captureMapSnapshot (con su propia escala+norte) ──
  const mapY=statsY+STATS_H;
  ctx.drawImage(mapCanvas,0,mapY,mapW,mapH);
  ctx.strokeStyle=line;ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(0,mapY);ctx.lineTo(mapW,mapY);ctx.stroke();

  // ── Leyenda de la capa temática visible: reportado que la imagen exportada
  // mostraba el mapa coloreado (hotspot/índice) sin decir qué
  // significa cada color, algo que SÍ se ve en pantalla (panel de Capas o
  // leyenda flotante del modo simple). Se toma la primera capa VISIBLE de
  // layerOrder que tenga swatches (ya viene en orden de prioridad de
  // decisión, ver el comentario de GROUP_ORDER/layerOrder más arriba). Se
  // dibuja arriba a la derecha del mapa para no chocar con la escala
  // (abajo-izquierda) ni el norte (abajo-derecha), ya horneados en mapCanvas.
  const legendId=layerOrder.find(id=>LAYER_REGISTRY[id]&&map.hasLayer(LAYER_REGISTRY[id].layer)&&legendSwatchesFor(id));
  if(legendId){
    const swatches=legendSwatchesFor(legendId);
    const legendTitle=LAYER_REGISTRY[legendId].label;
    ctx.font=F(700,12.5);
    const rowH=17,titleH=22,boxPad=10;
    const textW=Math.max(ctx.measureText(legendTitle).width,
      ...swatches.map(([,l])=>{ctx.font=F(500,11.5);return ctx.measureText(l).width;}));
    const boxW=Math.min(mapW-2*PAD,textW+boxPad*2+16), boxH=titleH+swatches.length*rowH+boxPad;
    const bx=mapW-PAD-boxW, by=mapY+12;
    ctx.save();
    ctx.globalAlpha=.94;
    roundRectPath(ctx,bx,by,boxW,boxH,10);
    ctx.fillStyle=surface;ctx.fill();
    ctx.strokeStyle=line;ctx.lineWidth=1;ctx.stroke();
    ctx.globalAlpha=1;
    ctx.textAlign='left';
    ctx.fillStyle=ink;ctx.font=F(700,12.5);
    ctx.fillText(legendTitle,bx+boxPad,by+16);
    swatches.forEach(([color,label],i)=>{
      const ry=by+titleH+i*rowH+9;
      ctx.beginPath();ctx.arc(bx+boxPad+5,ry,5,0,7);ctx.fillStyle=color;ctx.fill();
      ctx.fillStyle=inkMuted;ctx.font=F(500,11.5);
      ctx.fillText(label,bx+boxPad+16,ry+4);
    });
    ctx.restore();
  }

  // ── Pie: recomendación, coloreada según urgencia real. Es lo último
  // que se lee pero lo primero que se PERCIBE (el color) al abrir la
  // imagen compartida. Centrado VERTICALMENTE en la franja (antes quedaba
  // pegado arriba, con aire de sobra abajo cuando la recomendación era
  // corta). ──
  const footY=mapY+mapH;
  ctx.fillStyle=urgentSoft;ctx.fillRect(0,footY,mapW,FOOTER_H);
  ctx.fillStyle=urgentColor;ctx.fillRect(0,footY,4,FOOTER_H);
  const recomendacion=buildRecommendationText(s);
  ctx.font=F(400,13);
  const wrapped=(()=>{ // mide antes de dibujar, para poder centrar
    const words=recomendacion.split(' ');let line='',lines=[];
    for(const w of words){const test=line?line+' '+w:w;
      if(ctx.measureText(test).width>mapW-PAD*2&&line){lines.push(line);line=w;}else line=test;}
    if(line)lines.push(line);
    return lines.slice(0,3);
  })();
  const lineH=18,labelH=20,blockH=labelH+wrapped.length*lineH;
  let ty=footY+(FOOTER_H-blockH)/2+14;
  ctx.font=F(600,13.5);ctx.textAlign='left';
  ctx.fillStyle=urgentColor;
  ctx.fillText(urgentLabel,PAD,ty);
  ty+=labelH;
  ctx.fillStyle=ink;ctx.font=F(400,13);
  wrapped.forEach((l,i)=>ctx.fillText(l,PAD,ty+i*lineH));

  return {canvas:out,missionName:mission};
}

// ═══════════════════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════════════════
// MEDICIÓN (distancia + área)
// ═══════════════════════════════════════════════════════════════════
let measureActive=false,measurePts=[],measureLine=null,measurePoly=null,measureMarks=[];
const measurePane=map.createPane('pane-measure');measurePane.style.zIndex=690;

function toggleMeasure(){ measureActive?stopMeasure():startMeasure(); }
function startMeasure(){
  measureActive=true;
  document.getElementById('btn-measure').classList.add('active');
  document.getElementById('measure-panel').classList.add('visible');
  map.getContainer().style.cursor='crosshair';
}
function stopMeasure(){
  measureActive=false;
  document.getElementById('btn-measure').classList.remove('active');
  document.getElementById('measure-panel').classList.remove('visible');
  map.getContainer().style.cursor='';
  clearMeasure();
}
function clearMeasure(){
  measurePts=[];
  [measureLine,measurePoly].forEach(l=>{if(l)map.removeLayer(l);});
  measureLine=measurePoly=null;
  measureMarks.forEach(m=>map.removeLayer(m));measureMarks=[];
  document.getElementById('measure-dist').textContent='0 m';
  document.getElementById('measure-area').textContent='—';
}
function fmtDist(m){return m>=1000?(m/1000).toFixed(2)+' km':m.toFixed(1)+' m';}
function fmtArea(m2){
  if(m2>=10000)return (m2/10000).toFixed(2)+' ha';
  return m2.toFixed(0)+' m²';
}
// Área geodésica sobre la esfera (fórmula de la excedencia esférica). Es la
// misma que usa Leaflet.GeometryUtil; se implementa acá para no sumar otra
// dependencia externa a una app que debe funcionar sin internet.
function ringArea(latlngs){
  const R=6378137,rad=Math.PI/180;
  let s=0;
  for(let i=0,n=latlngs.length;i<n;i++){
    const p1=latlngs[i],p2=latlngs[(i+1)%n];
    s+=(p2.lng-p1.lng)*rad*(2+Math.sin(p1.lat*rad)+Math.sin(p2.lat*rad));
  }
  return Math.abs(s*R*R/2);
}
function refreshMeasure(){
  if(measureLine)map.removeLayer(measureLine);
  if(measurePoly)map.removeLayer(measurePoly);
  measureLine=measurePoly=null;
  if(measurePts.length>=2){
    measureLine=L.polyline(measurePts,{color:'#58a6ff',weight:2.5,dashArray:'6,4',
      pane:'pane-measure'}).addTo(map);
  }
  if(measurePts.length>=3){
    measurePoly=L.polygon(measurePts,{color:'#58a6ff',weight:1,fillOpacity:.12,
      pane:'pane-measure'}).addTo(map);
  }
  let d=0;
  for(let i=1;i<measurePts.length;i++)d+=map.distance(measurePts[i-1],measurePts[i]);
  document.getElementById('measure-dist').textContent=fmtDist(d);
  document.getElementById('measure-area').textContent=
    measurePts.length>=3?fmtArea(ringArea(measurePts)):'—';
}
map.on('click',e=>{
  if(drawActive){
    if(!freehandEnabled){
      strokePts.push(e.latlng);
      if(strokePolyline)map.removeLayer(strokePolyline);
      strokePolyline=L.polyline(strokePts,{
        color:'#ff3d00',weight:3,dashArray:'6,4',pane:'pane-draw'
      }).addTo(map);
    }
    return;
  }
  if(!measureActive)return;
  measurePts.push(e.latlng);
  const m=L.marker(e.latlng,{pane:'pane-measure',icon:L.divIcon({className:'measure-node',
    iconSize:[9,9],iconAnchor:[4.5,4.5]})}).addTo(map);
  measureMarks.push(m);
  refreshMeasure();
});
map.on('dblclick',e=>{
  if(drawActive){
    L.DomEvent.stop(e);
    if(!freehandEnabled&&strokePts.length>=2){
      applyStroke(strokePts);
      if(strokePolyline){map.removeLayer(strokePolyline);strokePolyline=null;}
      strokePts=[];
    }
    return;
  }
  if(measureActive){L.DomEvent.stop(e);}
});

// ═══════════════════════════════════════════════════════════════════
// DELIMITACIÓN, REMODELADO TIPO QGIS (RESHAPE) Y MANO ALZADA (FREEHAND)
// ═══════════════════════════════════════════════════════════════════
let drawActive=false;
let drawMode='reshape'; // 'reshape' | 'cut' | 'add' | 'draw'
let freehandEnabled=true;
let isDrawingStroke=false;
let strokePts=[];
let strokePolyline=null;
let drawPts=[];
let drawPoly=null;
let drawLine=null;
let drawMarks=[];
let drawMidMarks=[];
let drawUndoStack=[];
const drawPane=map.createPane('pane-draw');
drawPane.style.zIndex=695;

function setDrawMode(mode){
  drawMode=mode;
  ['reshape','cut','add','draw'].forEach(m=>{
    const btn=document.getElementById('btn-mode-'+m);
    if(btn)btn.classList.toggle('active',m===mode);
  });
  updateDrawHint();
}

function toggleFreehandMode(enabled){
  freehandEnabled=!!enabled;
  updateDrawHint();
}

function updateDrawHint(){
  const el=document.getElementById('draw-hint');
  if(!el)return;
  if(drawMode==='reshape'){
    el.textContent=freehandEnabled
      ?(LANG==='es'?'✏️ Arrastra una línea cruzando el polígono para remodelar su borde (QGIS)':'✏️ Drag a stroke crossing the polygon to reshape its boundary (QGIS)')
      :(LANG==='es'?'Haz clics cruzando el polígono y doble clic para remodelar':'Click across the polygon and double click to reshape');
  }else if(drawMode==='cut'){
    el.textContent=freehandEnabled
      ?(LANG==='es'?'🪓 Arrastra un lazo o trazo sobre el sobrante para recortarlo':'🪓 Drag a loop over unwanted parts to cut and remove them')
      :(LANG==='es'?'Haz clics rodeando el sobrante y doble clic para recortar':'Click around unwanted parts and double click to cut');
  }else if(drawMode==='add'){
    el.textContent=freehandEnabled
      ?(LANG==='es'?'➕ Arrastra un lazo para añadir un nuevo lóbulo o área quemada':'➕ Drag a loop to add and merge a new burned lobe')
      :(LANG==='es'?'Haz clics rodeando la nueva zona y doble clic para añadir':'Click around new area and double click to add');
  }else if(drawMode==='draw'){
    el.textContent=freehandEnabled
      ?(LANG==='es'?'✏️ Arrastra a mano alzada para trazar un nuevo polígono':'✏️ Drag freehand to draw a brand new polygon')
      :(LANG==='es'?'Haz clics y doble clic para cerrar un nuevo polígono':'Click vertices and double click to close new polygon');
  }
}

function pushDrawUndo(){
  drawUndoStack.push(drawPts.map(p=>L.latLng(p.lat,p.lng)));
  if(drawUndoStack.length>30)drawUndoStack.shift();
  updateUndoButtonState();
}

function undoDrawAction(){
  if(drawUndoStack.length>0){
    drawPts=drawUndoStack.pop();
    refreshDraw();
    updateUndoButtonState();
  }
}

function updateUndoButtonState(){
  const btn=document.getElementById('btn-draw-undo');
  if(btn)btn.disabled=(drawUndoStack.length===0);
}

function extractPolygonCoords(turfFeature){
  if(!turfFeature||!turfFeature.geometry)return [];
  const geom=turfFeature.geometry;
  if(geom.type==='Polygon'){
    return geom.coordinates[0].slice(0,-1).map(c=>L.latLng(c[1],c[0]));
  }
  if(geom.type==='MultiPolygon'){
    let maxArea=-1;
    let bestCoords=null;
    geom.coordinates.forEach(polyCoords=>{
      try{
        const p=turf.polygon(polyCoords);
        const a=turf.area(p);
        if(a>maxArea){maxArea=a;bestCoords=polyCoords[0];}
      }catch(e){}
    });
    if(bestCoords){
      return bestCoords.slice(0,-1).map(c=>L.latLng(c[1],c[0]));
    }
  }
  return [];
}

function applyStroke(pts){
  if(!pts||pts.length<2)return;
  let coords=pts.map(p=>[p.lng,p.lat]);

  if(typeof turf!=='undefined'&&coords.length>=3){
    try{
      const ls=turf.lineString(coords);
      const simp=turf.simplify(ls,{tolerance:0.00001,highQuality:true});
      if(simp&&simp.geometry&&simp.geometry.coordinates.length>=2){
        coords=simp.geometry.coordinates;
      }
    }catch(e){}
  }

  if(drawPts.length<3||drawMode==='draw'){
    if(coords.length>=3){
      pushDrawUndo();
      drawPts=coords.map(c=>L.latLng(c[1],c[0]));
      refreshDraw();
    }
    return;
  }

  const currentCoords=drawPts.map(p=>[p.lng,p.lat]);
  if(currentCoords[0][0]!==currentCoords[currentCoords.length-1][0]||
     currentCoords[0][1]!==currentCoords[currentCoords.length-1][1]){
    currentCoords.push([currentCoords[0][0],currentCoords[0][1]]);
  }

  if(typeof turf==='undefined'){
    if(drawMode==='add'){
      pushDrawUndo();
      drawPts=[...drawPts,...pts];
      refreshDraw();
    }
    return;
  }

  try{
    const currentPoly=turf.polygon([currentCoords]);

    if(drawMode==='cut'){
      const cutCoords=[...coords];
      if(cutCoords[0][0]!==cutCoords[cutCoords.length-1][0]||cutCoords[0][1]!==cutCoords[cutCoords.length-1][1]){
        cutCoords.push([cutCoords[0][0],cutCoords[0][1]]);
      }
      if(cutCoords.length>=4){
        const cutPoly=turf.polygon([cutCoords]);
        const diff=turf.difference(turf.featureCollection([currentPoly,cutPoly]));
        if(diff){
          const newPts=extractPolygonCoords(diff);
          if(newPts.length>=3){
            pushDrawUndo();
            drawPts=newPts;
            refreshDraw();
            return;
          }
        }
      }
    }else if(drawMode==='add'){
      const addCoords=[...coords];
      if(addCoords[0][0]!==addCoords[addCoords.length-1][0]||addCoords[0][1]!==addCoords[addCoords.length-1][1]){
        addCoords.push([addCoords[0][0],addCoords[0][1]]);
      }
      if(addCoords.length>=4){
        const addPoly=turf.polygon([addCoords]);
        const un=turf.union(turf.featureCollection([currentPoly,addPoly]));
        if(un){
          const newPts=extractPolygonCoords(un);
          if(newPts.length>=3){
            pushDrawUndo();
            drawPts=newPts;
            refreshDraw();
            return;
          }
        }
      }
    }else if(drawMode==='reshape'){
      const boundaryLine=turf.polygonToLine(currentPoly);
      const cutLine=turf.lineString(coords);
      const inter=turf.lineIntersect(cutLine,boundaryLine);

      if(inter&&inter.features&&inter.features.length>=2){
        const midIdx=Math.floor(coords.length/2);
        const midPt=turf.point(coords[midIdx]);
        const isMidInside=turf.booleanPointInPolygon(midPt,currentPoly);

        const strokeLoop=[...coords,coords[0]];
        const strokePoly=turf.polygon([strokeLoop]);

        if(isMidInside){
          const diff=turf.difference(turf.featureCollection([currentPoly,strokePoly]));
          if(diff){
            const newPts=extractPolygonCoords(diff);
            if(newPts.length>=3){
              pushDrawUndo();
              drawPts=newPts;
              refreshDraw();
              return;
            }
          }
        }else{
          const un=turf.union(turf.featureCollection([currentPoly,strokePoly]));
          if(un){
            const newPts=extractPolygonCoords(un);
            if(newPts.length>=3){
              pushDrawUndo();
              drawPts=newPts;
              refreshDraw();
              return;
            }
          }
        }
      }else{
        const loopCoords=[...coords,coords[0]];
        const loopPoly=turf.polygon([loopCoords]);
        const center=turf.centerOfMass(loopPoly);
        const isCenterInside=turf.booleanPointInPolygon(center,currentPoly);

        if(isCenterInside){
          const diff=turf.difference(turf.featureCollection([currentPoly,loopPoly]));
          if(diff){
            const newPts=extractPolygonCoords(diff);
            if(newPts.length>=3){
              pushDrawUndo();
              drawPts=newPts;
              refreshDraw();
              return;
            }
          }
        }else{
          const un=turf.union(turf.featureCollection([currentPoly,loopPoly]));
          if(un){
            const newPts=extractPolygonCoords(un);
            if(newPts.length>=3){
              pushDrawUndo();
              drawPts=newPts;
              refreshDraw();
              return;
            }
          }
        }
      }
    }
  }catch(err){
    console.warn('Error applying stroke:',err);
  }
}

// Eventos pointer en contenedor para trazo a mano alzada ultra fluido y táctil
const mapEl=map.getContainer();

mapEl.addEventListener('pointerdown',e=>{
  if(!drawActive)return;
  if(e.target.closest('#draw-panel,#report-panel,#sidebar,.leaflet-control'))return;
  if(e.button!==0)return;

  if(freehandEnabled){
    isDrawingStroke=true;
    map.dragging.disable();
    const rect=mapEl.getBoundingClientRect();
    const pt=map.containerPointToLatLng([e.clientX-rect.left,e.clientY-rect.top]);
    strokePts=[pt];
    if(strokePolyline)map.removeLayer(strokePolyline);
    strokePolyline=L.polyline(strokePts,{
      color:'#ff3d00',weight:3.5,dashArray:'4,4',pane:'pane-draw',interactive:false
    }).addTo(map);
  }
});

mapEl.addEventListener('pointermove',e=>{
  if(!drawActive||!isDrawingStroke||!freehandEnabled)return;
  const rect=mapEl.getBoundingClientRect();
  const pt=map.containerPointToLatLng([e.clientX-rect.left,e.clientY-rect.top]);
  const lastPt=strokePts[strokePts.length-1];
  if(!lastPt||map.distance(lastPt,pt)>=1.0){
    strokePts.push(pt);
    if(strokePolyline)strokePolyline.setLatLngs(strokePts);
  }
});

window.addEventListener('pointerup',()=>{
  if(drawActive&&isDrawingStroke&&freehandEnabled){
    isDrawingStroke=false;
    if(strokePts.length>=2){
      applyStroke(strokePts);
    }
    if(strokePolyline){
      map.removeLayer(strokePolyline);
      strokePolyline=null;
    }
    strokePts=[];
  }
});

function toggleDraw(){ drawActive?stopDraw():startDraw(); }
function startDraw(){
  if(measureActive)stopMeasure();
  drawActive=true;
  if(freehandEnabled)map.dragging.disable();
  document.getElementById('btn-draw')?.classList.add('active');
  document.getElementById('draw-panel')?.classList.add('visible');
  mapEl.classList.add('draw-freehand-cursor');
  updateDrawHint();
  updateUndoButtonState();
  
  if(drawPts.length===0 && LAYER_REGISTRY.area_afectada_experto?.layer){
    try{
      const gj=LAYER_REGISTRY.area_afectada_experto.layer.toGeoJSON();
      importGeoJSONToDraw(gj);
      if(drawPoly){
        map.fitBounds(drawPoly.getBounds(),{paddingTopLeft:[80,340],paddingBottomRight:[80,340]});
      }
    }catch(e){}
  }
}

function stopDraw(){
  drawActive=false;
  isDrawingStroke=false;
  map.dragging.enable();
  document.getElementById('btn-draw')?.classList.remove('active');
  document.getElementById('draw-panel')?.classList.remove('visible');
  mapEl.classList.remove('draw-freehand-cursor');
  if(strokePolyline){map.removeLayer(strokePolyline);strokePolyline=null;}
  clearDrawMarkers();
}

function clearDraw(){
  if(drawPts.length>0)pushDrawUndo();
  drawPts=[];
  if(drawPoly)map.removeLayer(drawPoly);
  if(drawLine)map.removeLayer(drawLine);
  drawPoly=drawLine=null;
  clearDrawMarkers();
  updateDrawStats();
}

function clearDrawMarkers(){
  drawMarks.forEach(m=>map.removeLayer(m));drawMarks=[];
  drawMidMarks.forEach(m=>map.removeLayer(m));drawMidMarks=[];
}

function smoothDrawContour(){
  if(drawPts.length<3)return;
  pushDrawUndo();
  let pts=drawPts.map(p=>({lat:p.lat,lng:p.lng}));
  for(let it=0;it<2;it++){
    const smoothed=[];
    const n=pts.length;
    for(let i=0;i<n;i++){
      const p0=pts[i],p1=pts[(i+1)%n];
      smoothed.push(
        {lat:0.75*p0.lat+0.25*p1.lat, lng:0.75*p0.lng+0.25*p1.lng},
        {lat:0.25*p0.lat+0.75*p1.lat, lng:0.25*p0.lng+0.75*p1.lng}
      );
    }
    pts=smoothed;
  }
  drawPts=pts.map(p=>L.latLng(p.lat,p.lng));
  refreshDraw();
}

async function loadThermalSuggestion(){
  const btn=document.getElementById('btn-draw-assist');
  if(btn)btn.disabled=true;
  try{
    const res=await fetch(`outputs/area_afectada_detectada.geojson?t=${Date.now()}`);
    if(!res.ok)throw new Error("No hay sugerencia");
    const gj=await res.json();
    pushDrawUndo();
    importGeoJSONToDraw(gj);
  }catch(e){
    alert(LANG==='es'?'No hay sugerencia térmica disponible para esta misión.':'No thermal suggestion available.');
  }finally{
    if(btn)btn.disabled=false;
  }
}

function importGeoJSONToDraw(gj){
  if(!gj)return;
  let coords=null;
  if(gj.type==='FeatureCollection'&&gj.features?.length>0){
    let maxArea=-1;
    for(const f of gj.features){
      if(f.geometry?.type==='Polygon'){
        const c=f.geometry.coordinates[0];
        if(c&&c.length>maxArea){maxArea=c.length;coords=c;}
      }else if(f.geometry?.type==='MultiPolygon'){
        for(const poly of f.geometry.coordinates){
          const c=poly[0];
          if(c&&c.length>maxArea){maxArea=c.length;coords=c;}
        }
      }
    }
  }else if(gj.type==='Feature'&&gj.geometry?.type==='Polygon'){
    coords=gj.geometry.coordinates[0];
  }else if(gj.type==='Polygon'){
    coords=gj.coordinates[0];
  }

  if(coords&&coords.length>=3){
    if(typeof turf!=='undefined'&&coords.length>250){
      try{
        const closed=[...coords];
        if(closed[0][0]!==closed[closed.length-1][0]||closed[0][1]!==closed[closed.length-1][1]){
          closed.push([closed[0][0],closed[0][1]]);
        }
        const simp=turf.simplify(turf.polygon([closed]),{tolerance:0.000001,highQuality:true});
        if(simp&&simp.geometry&&simp.geometry.coordinates[0].length>=4){
          coords=simp.geometry.coordinates[0];
        }
      }catch(e){}
    }
    drawPts=coords.slice(0,coords.length-1).map(c=>L.latLng(c[1],c[0]));
    refreshDraw();
  }
}

function countContainedHotspots(){
  if(drawPts.length<3)return 0;
  const hsLayer=LAYER_REGISTRY.hotspot_termico?.layer;
  if(!hsLayer)return 0;
  let count=0;
  try{
    const gj=hsLayer.toGeoJSON();
    for(const ft of gj.features||[]){
      if(ft.geometry?.type==='Point'){
        const [lng,lat]=ft.geometry.coordinates;
        if(pointInPolygon([lng,lat],drawPts.map(p=>[p.lng,p.lat]))){
          count++;
        }
      }
    }
  }catch(e){}
  return count;
}

function pointInPolygon(point,vs){
  const x=point[0],y=point[1];
  let inside=false;
  for(let i=0,j=vs.length-1;i<vs.length;j=i++){
    const xi=vs[i][0],yi=vs[i][1];
    const xj=vs[j][0],yj=vs[j][1];
    const intersect=((yi>y)!==(yj>y))&&(x<(xj-xi)*(y-yi)/(yj-yi)+xi);
    if(intersect)inside=!inside;
  }
  return inside;
}

function updateDrawStats(){
  if(drawPts.length<3){
    document.getElementById('draw-area').textContent='0.00 ha';
    document.getElementById('draw-m2').textContent='0 m²';
    document.getElementById('draw-perimeter').textContent='0 m';
    document.getElementById('draw-hotspots').textContent='0';
    return;
  }
  const areaM2=ringArea(drawPts);
  const areaHa=areaM2/10000.0;
  document.getElementById('draw-area').textContent=(areaHa>=1?areaHa.toFixed(3):areaHa.toFixed(4))+' ha';
  document.getElementById('draw-m2').textContent=Math.round(areaM2).toLocaleString()+' m²';

  let perim=0;
  for(let i=0;i<drawPts.length;i++){
    perim+=map.distance(drawPts[i],drawPts[(i+1)%drawPts.length]);
  }
  document.getElementById('draw-perimeter').textContent=fmtDist(perim);
  document.getElementById('draw-hotspots').textContent=countContainedHotspots()+(LANG==='es'?' focos':' hotspots');
}

function refreshDraw(){
  if(drawPoly)map.removeLayer(drawPoly);
  if(drawLine)map.removeLayer(drawLine);
  drawPoly=drawLine=null;
  clearDrawMarkers();

  if(drawPts.length>=2){
    drawLine=L.polyline(drawPts,{color:'#ff6d00',weight:3,dashArray:'6,4',pane:'pane-draw',interactive:false}).addTo(map);
  }

  if(drawPts.length>=3){
    drawPoly=L.polygon(drawPts,{color:'#ff6d00',weight:2.5,fillColor:'#ff9100',fillOpacity:.28,pane:'pane-draw',interactive:false}).addTo(map);
  }

  // Si no estamos dibujando trazos rápidos, mostrar nodos
  if(drawPts.length<=60){
    drawPts.forEach((pt,idx)=>{
      const m=L.marker(pt,{
        pane:'pane-draw',draggable:true,
        icon:L.divIcon({className:'draw-node',iconSize:[12,12],iconAnchor:[6,6]})
      }).addTo(map);

      m.on('dragstart',()=>pushDrawUndo());
      m.on('drag',e=>{
        drawPts[idx]=e.target.getLatLng();
        if(drawLine)drawLine.setLatLngs(drawPts);
        if(drawPoly)drawPoly.setLatLngs(drawPts);
        updateDrawStats();
      });
      m.on('dragend',()=>refreshDraw());
      m.on('contextmenu',e=>{
        L.DomEvent.stop(e);
        pushDrawUndo();
        drawPts.splice(idx,1);
        refreshDraw();
      });
      drawMarks.push(m);

      if(drawPts.length>=2){
        const nextIdx=(idx+1)%drawPts.length;
        if(idx<drawPts.length-1||drawPts.length>=3){
          const nextPt=drawPts[nextIdx];
          const midLat=(pt.lat+nextPt.lat)/2;
          const midLng=(pt.lng+nextPt.lng)/2;
          const midM=L.marker([midLat,midLng],{
            pane:'pane-draw',draggable:true,
            icon:L.divIcon({className:'draw-midnode',iconSize:[8,8],iconAnchor:[4,4]})
          }).addTo(map);

          midM.on('dragstart',()=>{
            pushDrawUndo();
            drawPts.splice(nextIdx===0?drawPts.length:nextIdx,0,midM.getLatLng());
          });
          midM.on('drag',e=>{
            const targetIdx=nextIdx===0?drawPts.length-1:nextIdx;
            drawPts[targetIdx]=e.target.getLatLng();
            if(drawLine)drawLine.setLatLngs(drawPts);
            if(drawPoly)drawPoly.setLatLngs(drawPts);
            updateDrawStats();
          });
          midM.on('dragend',()=>refreshDraw());
          drawMidMarks.push(midM);
        }
      }
    });
  }

  updateDrawStats();
}

async function saveDrawPolygon(){
  if(drawPts.length<3){
    alert(LANG==='es'?'Debes trazar al menos 3 puntos para cerrar un polígono.':'Draw at least 3 points to form a polygon.');
    return;
  }

  const saveBtn=document.getElementById('btn-draw-save');
  const saveText=document.getElementById('btn-draw-save-text');
  if(saveBtn)saveBtn.disabled=true;
  if(saveText)saveText.textContent=(LANG==='es'?'Guardando...':'Saving...');

  const coordinates=[drawPts.map(p=>[p.lng,p.lat])];
  coordinates[0].push([drawPts[0].lng,drawPts[0].lat]);

  const areaM2=ringArea(drawPts);
  const areaHa=areaM2/10000.0;

  const geojson={
    type:'FeatureCollection',
    name:'Area_Afectada_Experto',
    properties:{
      total_area_m2:Math.round(areaM2*10)/10,
      total_area_ha:Math.round(areaHa*10000)/10000,
      features_count:1,
      origen:'Delimitación interactiva en Geovisor RAPTOR',
      hotspots_count:countContainedHotspots()
    },
    features:[{
      type:'Feature',
      geometry:{type:'Polygon',coordinates},
      properties:{
        tipo:'Área Afectada (Delimitación Oficial)',
        area_m2:Math.round(areaM2*10)/10,
        area_ha:Math.round(areaHa*10000)/10000,
        origen:'Delimitación interactiva en Geovisor RAPTOR'
      }
    }]
  };

  const urlParams=new URLSearchParams(window.location.search);
  const mission=urlParams.get('mission')||'';

  try{
    const res=await fetch(`/api/missions/${mission}/save-area`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({geojson})
    });

    const result=await res.json();
    if(!res.ok)throw new Error(result.detail||'Error al guardar');

    if(LAYER_REGISTRY.area_afectada_experto){
      map.removeLayer(LAYER_REGISTRY.area_afectada_experto.layer);
      delete LAYER_REGISTRY.area_afectada_experto;
    }
    await tryLoadBurnedArea('experto');

    if(saveText)saveText.textContent=(LANG==='es'?'✓ Guardado':'✓ Saved');
    setTimeout(()=>{
      if(saveText)saveText.textContent=(LANG==='es'?'Guardar como Área Oficial':'Save as Official Area');
      if(saveBtn)saveBtn.disabled=false;
    },2000);

  }catch(err){
    alert((LANG==='es'?'Error al guardar área: ':'Failed to save area: ')+err.message);
    if(saveText)saveText.textContent=(LANG==='es'?'Guardar como Área Oficial':'Save as Official Area');
    if(saveBtn)saveBtn.disabled=false;
  }
}

// ═══════════════════════════════════════════════════════════════════
// ATAJOS DE TECLADO
// ═══════════════════════════════════════════════════════════════════
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if(helpMenu&&helpMenu.classList.contains('open')){closeHelpMenu();return;}
    if(document.getElementById('report-overlay')?.classList?.contains('open')){closeReport();return;}
    if(document.getElementById('point-card').classList.contains('visible')){closePointCard();return;}
    if(drawActive){stopDraw();return;}
    if(measureActive){stopMeasure();return;}
  }
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){
    if(drawActive){
      e.preventDefault();
      undoDrawAction();
      return;
    }
  }
  if(e.target.matches('input,textarea,select'))return;
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  const k=e.key.toLowerCase();
  if(k==='t'){toggleTheme();}
  else if(k==='d'){toggleDraw();}
  else if(k==='m'){toggleMeasure();}
  else if(k==='c'){toggleCompare();}
  else if(k==='b'){toggleSidebar();}
});

// (CURRENT_MISSION/missionReady se declaran arriba, junto a urlMission —
// ver el comentario ahí sobre por qué no pueden vivir acá.)

function displayName(raw){
  // "la_clara" -> "La Clara" — el nombre de misión es un slug (sanitize_mission_name
  // en el servidor lo pasa todo a minúsculas con guiones bajos); esto es solo
  // cosmético para el encabezado, no cambia el identificador real usado en las URLs.
  return (raw||'').split('_').map(w=>w?w[0].toUpperCase()+w.slice(1):w).join(' ');
}

// Nombre/frescura/confianza/fecha en el encabezado. Con nombre (no IIFE):
// connectLiveMission() la vuelve a llamar cuando la corrida termina, para
// que deje de decir "procesando" apenas hay resultado final.
async function labelMission(){
  const nameEl=document.getElementById('incident-name');
  if(!nameEl)return;
  const mission=await missionReady;
  if(!mission){nameEl.textContent=t('mission.none');return;}
  const hudVisible=document.getElementById('progress-hud')?.classList.contains('visible');
  nameEl.textContent=(hudVisible&&!phDone)?`${displayName(mission)} (${t('mission.processingWord')})`:displayName(mission);
  await renderSituationHeader();
}
labelMission();

// ═══════════════════════════════════════════════════════════════════
// RESUMEN DE SITUACIÓN — outputs/situation.json (compute_situation_summary.py)
// ═══════════════════════════════════════════════════════════════════
// Todo lo que aparece en las tarjetas ejecutivas y en la ficha de punto sale
// de acá o del endpoint de muestreo — nunca un número inventado en el
// navegador. Si el archivo no existe (misión sin multiespectral+térmico,
// o corrida vieja de antes de este script) las tarjetas lo dicen así, no
// rellenan con ceros.
// (SITUATION se declara arriba, junto a LAYER_REGISTRY — ver el comentario
// ahí sobre por qué no puede vivir acá.)
async function loadSituation(){
  try{
    const r=await fetch('outputs/situation.json?t='+Date.now(),{cache:'no-store'});
    if(!r.ok){SITUATION=null;return null;}
    SITUATION=await r.json();
    return SITUATION;
  }catch(e){SITUATION=null;return null;}
}

// Calidad del LEVANTAMIENTO (cómo se voló: solape, velocidad, % de
// imágenes reconstruidas) — ver compute_flight_quality.py. Reemplaza la
// vieja "Confianza del dato" (qué fracción del ortomosaico térmico tenía
// dato real): esa cifra no le decía a nadie si el vuelo estuvo bien volado,
// que es la pregunta real detrás de "¿confío en esto?" — un reporte de
// Terra/Agisoft/Pix4D habla de solape e imágenes reconstruidas, no de
// cobertura de dato.
let FLIGHT_QUALITY=null;
async function loadFlightQuality(){
  try{
    const r=await fetch('outputs/flight_quality.json?t='+Date.now(),{cache:'no-store'});
    if(!r.ok){FLIGHT_QUALITY=null;return null;}
    FLIGHT_QUALITY=await r.json();
    return FLIGHT_QUALITY;
  }catch(e){FLIGHT_QUALITY=null;return null;}
}
// data-level de .confidence-ticks (CSS) viene de la época de "confianza"
// alta/media/baja — se reusa el mismo semáforo visual para "calidad"
// buena/regular/baja en vez de duplicar las reglas de color.
const CALIDAD_TICK_LEVEL={buena:'alta',regular:'media',baja:'baja'};

function fmtFecha(iso){
  if(!iso)return '—';
  try{
    const d=new Date(iso.replace(' ','T'));
    if(isNaN(d))return iso;
    return d.toLocaleDateString('es-CO',{day:'numeric',month:'short',year:'numeric'})+', '+
      d.toLocaleTimeString('es-CO',{hour:'2-digit',minute:'2-digit'});
  }catch(e){return iso;}
}
function fmtFechaCorta(iso){
  if(!iso)return '—';
  try{
    const d=new Date(iso.replace(' ','T'));
    return isNaN(d)?iso:d.toLocaleDateString('es-CO',{day:'numeric',month:'short',year:'numeric'});
  }catch(e){return iso;}
}

async function renderSituationHeader(){
  const s=SITUATION||await loadSituation();
  const fq=FLIGHT_QUALITY||await loadFlightQuality();
  const zoneEl=document.getElementById('incident-zone');
  const ticksEl=document.getElementById('confidence-ticks');
  const freshEl=document.getElementById('freshness-text');
  const dateEl=document.getElementById('capture-date-label');
  const calidad=fq?.calidad||'buena';
  if(ticksEl)ticksEl.setAttribute('data-level',CALIDAD_TICK_LEVEL[calidad]||'alta');
  if(dateEl)dateEl.textContent=s?.captura?fmtFechaCorta(s.captura):'—';
  if(freshEl){
    const captura=s?.captura?new Date(s.captura.replace(' ','T')):null;
    const minsAgo=captura&&!isNaN(captura)?Math.max(0,Math.round((Date.now()-captura)/60000)):null;
    // "hace N min/h" (es) vs. "N min/h ago" (en): orden de palabras distinto,
    // no un simple lookup por clave — igual que buildRecommendationText().
    const cuando=minsAgo===null?''
      :LANG==='es'
        ? (minsAgo<60?`hace ${minsAgo} ${t('freshness.minAgo')}`:`hace ${Math.round(minsAgo/60)} ${t('freshness.hAgo')}`)
        : (minsAgo<60?`${minsAgo} ${t('freshness.minAgo')}`:`${Math.round(minsAgo/60)} ${t('freshness.hAgo')}`);
    // Alerta de cobertura baja vs. el área volada (scripts/compute_coverage.py):
    // el mosaico puede cubrir solo una fracción de lo que el dron recorrió
    // (reconstrucción incompleta en terreno con relieve) y hay que decirlo en
    // el mismo lugar donde se mira el dato.
    const covAviso=s?.alerta_cobertura_baja?' · '+t('freshness.lowCoverage'):'';
    freshEl.textContent=s?`${t('freshness.updated')} ${cuando||t('freshness.justNow')} · ${t('report.flightQualityLabel')}${localizeValue(calidad)}${covAviso}`
                          :t('freshness.noData');
  }
  if(zoneEl){
    const n=s?.hotspots_activos;
    zoneEl.textContent=s
      ?(LANG==='es'?`${n} foco${n===1?'':'s'} activo${n===1?'':'s'}`:`${n} active hotspot${n===1?'':'s'}`)
      :'';
  }
}

// ── Tarjetas de resumen ejecutivo ────────────────────────────────────
async function renderSummaryCards(){
  const grid=document.getElementById('summary-grid');
  if(!grid)return;
  const s=SITUATION||await loadSituation();
  const fq=FLIGHT_QUALITY||await loadFlightQuality();
  if(!s){
    grid.innerHTML=`<div class="stat-card" style="grid-column:1/-1">
      <div class="l">${t('summary.noImpactTitle')}</div>
      <div class="sub">${t('summary.noImpactSub')}</div>
    </div>`;
    grid.setAttribute('aria-busy','false');
    return;
  }
  grid.innerHTML=`
    <div class="stat-card hotspots${s.hotspots_activos>0?'':' none'}">
      <div class="l">${t('summary.activeHotspots')}</div>
      <div class="v tabnum">${s.hotspots_activos}</div>
      <div class="sub">${s.hotspots_activos>0?t('summary.reignitionRisk'):t('summary.noneDetected')}${
        s.temp_max!=null?` · ${t('summary.maxAbbr')} ${s.temp_max}°C · ${t('summary.avgAbbr')} ${s.temp_promedio}°C`:''}</div>
    </div>
    ${recommendationStrip(s)}
    <div class="stat-card">
      <div class="l">${t('summary.lastCapture')}</div>
      <div class="v" style="font-size:var(--fs-md)">${fmtFechaCorta(s.captura)}</div>
      <div class="sub">${fq?.equipo||t('report.defaultEquipment')}</div>
    </div>
    ${flightQualityCardHTML(fq)}`;
    grid.setAttribute('aria-busy','false');
}
// Franja de recomendación — la frase accionable que ya arma
// buildRecommendationText (el mismo texto que entra al reporte), puesta en
// el panel, donde se decide. Un solo lugar donde vive la recomendación.
function recommendationStrip(s){
  const txt=buildRecommendationText(s);
  if(!txt)return '';
  // Ámbar solo cuando hay un foco activo real que verificar en terreno; sin
  // focos, es informativo y se pinta en el acento neutro.
  const urgente=!!(s&&s.hotspots_activos>0);
  return `<div class="reco-strip${urgente?'':' quiet'}" role="note"><span class="reco-l">${t('reco.label')}</span>${txt}</div>`;
}
// Calidad del LEVANTAMIENTO (compute_flight_quality.py): solape de cámaras,
// velocidad de vuelo y % de imágenes reconstruidas — lo que de verdad
// predice si el resultado es confiable, en el mismo lenguaje que un reporte
// de Terra/Agisoft/Pix4D. Reemplaza la vieja "Confianza del dato" (fracción
// del ortomosaico con dato real): esa cifra no decía nada sobre cómo se
// voló, que es la pregunta que de verdad importa acá.
function flightQualityCardHTML(fq){
  if(!fq){
    return `<div class="stat-card">
      <div class="l">${t('summary.surveyQuality')}</div>
      <div class="v" style="font-size:var(--fs-sm);color:var(--ink-muted)">${t('summary.noDataYet')}</div>
    </div>`;
  }
  const pcts=Object.values(fq.reconstruccion||{}).map(v=>v.pct).filter(v=>v!=null);
  const reconMin=pcts.length?Math.min(...pcts):null;
  const partes=[
    fq.solape_p50!=null?`${t('summary.overlap')}${fq.solape_p50}×`:null,
    fq.velocidad_media_ms!=null?`${t('summary.flightAt')} ${fq.velocidad_media_ms} m/s`:null,
    reconMin!=null?`${reconMin}${t('summary.reconstructedPct')}`:null,
  ].filter(Boolean);
  return `<div class="stat-card">
    <div class="l">${t('summary.surveyQuality')}</div>
    <div class="v" style="font-size:var(--fs-md);display:flex;align-items:center;gap:8px">
      ${localizeValue(fq.calidad)} <span class="confidence-ticks" data-level="${CALIDAD_TICK_LEVEL[fq.calidad]||'alta'}" aria-hidden="true"><i></i><i></i><i></i></span>
    </div>
    <div class="sub">${partes.join(' · ')||'—'}</div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════
// FOCOS TÉRMICOS IDENTIFICADOS — antes vivían en una vista "Lista" aparte
// (tabla de texto, alternativa al mapa); ahora son parte de la propia
// tarjeta de la capa Hotspot térmico (layerCardHTML(), más abajo): cada
// renglón centra el mapa en ese foco al hacer click, sin salir del panel
// de capas ni cambiar de vista.
// ═══════════════════════════════════════════════════════════════════
function hotspotListHTML(){
  const hotspots=SITUATION?.hotspots||[];
  if(!hotspots.length)return'';
  const rows=hotspots.map((h,i)=>`<button class="hotspot-row" data-lat="${h.lat}" data-lon="${h.lon}">
      <span class="coord">${t('hotspot.rowLabel')} ${i+1} · ${h.lat.toFixed(5)}, ${h.lon.toFixed(5)}</span>
      <span class="temp">${h.temp_c!=null?h.temp_c+' °C':'—'}</span>
    </button>`).join('');
  return `<div class="hotspot-list"><div class="hotspot-list-title">${t('hotspot.identifiedLabel')} (${hotspots.length})</div>${rows}</div>`;
}
function wireHotspotList(){
  document.querySelectorAll('.hotspot-row').forEach(row=>{
    row.onclick=()=>{
      const latlng=L.latLng(+row.dataset.lat,+row.dataset.lon);
      map.setView(latlng,Math.max(map.getZoom(),19));
      showPointCard(latlng,map.latLngToContainerPoint(latlng));
    };
  });
}

// La caja "Agregar vuelo multiespectral" vive fija al pie del panel de
// capas, visible sin importar qué grupo esté abierto/filtrado. Se oculta
// sola en cuanto liveMsBandIds deja de estar vacío (ya no hay nada que
// ofrecer). Un solo footer ahora (antes había dos, uno por modo).
function renderFooterAddMsCta(){
  const html=(liveMsBandIds.length===0&&!hasMsInput)
    ? addMsCtaHTML(t('addms.ctaTitle'),t('addms.ctaDetail'))
    : '';
  const footer=document.getElementById('footer-addms-cta')?.closest('.panel-footer');
  const cta=document.getElementById('footer-addms-cta');
  if(!cta)return;
  cta.innerHTML=html;
  if(footer)footer.hidden=!html;
}

// La leyenda contextual flotante sobre el mapa se sacó — con una sola capa
// visible a la vez no había forma de distinguirla del resto de la interfaz,
// y en pantallas angostas llegaba a tapar el mapa entero. La interpretación
// (SIMPLE_HINTS) y la leyenda técnica completa (legend()) ya viven DENTRO de
// la tarjeta de cada capa en el panel (layerCardHTML(), más abajo) — un solo
// lugar, siempre en el mismo sitio, sin competir por espacio con el mapa.

// ═══════════════════════════════════════════════════════════════════
// FICHA "QUÉ SIGNIFICA ESTE PUNTO" — clic en el mapa (solo modo simple)
// ═══════════════════════════════════════════════════════════════════
function closePointCard(){document.getElementById('point-card').classList.remove('visible');}
async function showPointCard(latlng,containerPoint){
  const card=document.getElementById('point-card');
  const mission=await missionReady;
  if(!mission)return;
  card.innerHTML=`<div class="point-card-head"><h4>${t('point.looking')}</h4></div>`;
  card.style.left=Math.min(containerPoint.x+16,map.getSize().x-316)+'px';
  card.style.top=Math.max(8,Math.min(containerPoint.y-40,map.getSize().y-260))+'px';
  card.classList.add('visible');
  let d;
  try{
    const r=await fetch(`/api/missions/${encodeURIComponent(mission)}/sample?lat=${latlng.lat}&lon=${latlng.lng}`);
    d=await r.json();
  }catch(e){
    card.innerHTML=`<div class="point-card-head"><h4>${t('point.error')}</h4>
      <button class="point-card-close reset" onclick="closePointCard()"><svg class="ic" style="width:15px;height:15px" aria-hidden="true"><use href="#i-x"/></svg></button></div>
      <p style="padding:0 16px 16px;font-size:var(--fs-xs);color:var(--ink-muted)">${t('point.errorMsg')}</p>`;
    return;
  }
  if(d.temperatura_c==null&&d.ndvi==null){
    card.innerHTML=`<div class="point-card-head"><h4>${t('point.selected')}</h4>
      <button class="point-card-close reset" onclick="closePointCard()"><svg class="ic" style="width:15px;height:15px" aria-hidden="true"><use href="#i-x"/></svg></button></div>
      <p style="padding:0 16px 16px;font-size:var(--fs-xs);color:var(--ink-muted);line-height:1.5">
      ${t('point.outsideCoverage')}</p>`;
    return;
  }
  card.innerHTML=`
    <div class="point-card-head"><h4>${t('point.selected')}</h4>
      <button class="point-card-close reset" onclick="closePointCard()"><svg class="ic" style="width:15px;height:15px" aria-hidden="true"><use href="#i-x"/></svg></button></div>
    <div class="point-metrics">
      <div class="point-metric"><div class="l">${t('point.temperature')}</div><div class="v tabnum">${d.temperatura_c!=null?d.temperatura_c+' °C':'—'}</div></div>
      <div class="point-metric"><div class="l">${t('point.vegetation')}</div><div class="v tabnum">${d.ndvi!=null?d.ndvi:'—'}</div></div>
      <div class="point-metric"><div class="l">${t('point.date')}</div><div class="v" style="font-size:var(--fs-sm)">${fmtFechaCorta(d.captura)}</div></div>
      <div class="point-metric"><div class="l">${t('point.confidence')}</div><div class="v" style="font-size:var(--fs-sm)">${localizeValue(d.confianza)||'—'}</div></div>
    </div>
  `;
}
map.on('click',e=>{
  // Solo si no hay otra herramienta usando el clic (medición) — evita
  // robarle el clic a esa herramienta.
  if(measureActive)return;
  showPointCard(e.latlng,e.containerPoint);
});

// ═══════════════════════════════════════════════════════════════════
// COMPARAR EN EL TIEMPO — solo si hay >1 misión en la misma zona
// ═══════════════════════════════════════════════════════════════════
async function checkRelatedMissions(){
  const mission=await missionReady;
  const timebar=document.getElementById('timebar');
  if(!mission){timebar.classList.remove('enabled');return;}
  try{
    const s=await (await fetch(`/api/missions/${encodeURIComponent(mission)}/status`,{cache:'no-store'})).json();
    const related=s.related_missions||[];
    if(!related.length){timebar.classList.remove('enabled');return;}
    timebar.classList.add('enabled');
    const thisDate=SITUATION?.captura?fmtFechaCorta(SITUATION.captura):t('timebar.thisCapture');
    const otherDates=related.map(m=>fmtFechaCorta(m.captura)).join(', ');
    document.getElementById('timebar-dates').innerHTML=`<b>${thisDate}</b> · ${t('timebar.compareWith')} ${otherDates}`;
    const n=related.length,names=related.map(m=>displayName(m.name)).join(', ');
    // Pluralización propia (no t()): "misión/misiones ... capturada/
    // capturadas" (es) vs. "mission/missions ... captured" (en) no calzan
    // en una sola clave, igual que buildRecommendationText() más arriba.
    const intro=LANG==='es'
      ? `Hay ${n} misión${n>1?'es':''} más capturada${n>1?'s':''} en esta misma zona (<b>${names}</b>).`
      : `There ${n>1?'are':'is'} ${n} more mission${n>1?'s':''} captured in this same area (<b>${names}</b>).`;
    document.getElementById('timebar-note').innerHTML=`${intro} ${t('timebar.compareNote')}`;
  }catch(e){timebar.classList.remove('enabled');}
}
document.getElementById('timebar-head').onclick=()=>document.getElementById('timebar').classList.toggle('open');

// ═══════════════════════════════════════════════════════════════════
// GENERAR RESUMEN DE SITUACIÓN
// ═══════════════════════════════════════════════════════════════════
function closeReport(){document.getElementById('report-overlay')?.classList?.remove('open');}
async function openReport(){
  const overlay=document.getElementById('report-overlay');
  if(!overlay) return;
  overlay.classList.add('open');
  const subEl=document.getElementById('report-sub');
  if(subEl) subEl.textContent=t('report.loading');
  const statsEl=document.getElementById('report-stats');
  if(statsEl) statsEl.innerHTML='';
  const textEl=document.getElementById('report-text');
  if(textEl) textEl.textContent='';

  try{
    const s=SITUATION||await loadSituation();
    const mission=await missionReady;
    if(subEl) subEl.textContent=
      `${displayName(mission||'')} · ${s?fmtFecha(s.captura):t('report.urgentNone').toLowerCase()}`;
    if(statsEl) statsEl.innerHTML=!s
      ? `<div class="card" style="grid-column:1/-1"><div class="l">${t('report.noImpactCard')}</div></div>`
      : `<div class="card"><div class="v tabnum">${s.hotspots_activos}</div><div class="l">${t('report.activeHotspots')}</div></div>
         <div class="card"><div class="v tabnum">${s.temp_max??'—'}°C</div><div class="l">${t('report.maxTemp')}</div></div>
         <div class="card"><div class="v tabnum">${s.temp_promedio??'—'}°C</div><div class="l">${t('report.avgTemp')}</div></div>`;
    if(textEl) textEl.textContent=buildRecommendationText(s);
  }catch(e){
    if(subEl) subEl.textContent=t('report.loadError');
  }

  const preview=document.getElementById('report-preview');
  if(!preview) return;
  preview.innerHTML=`<span style="font-size:var(--fs-xs);color:var(--ink-muted)">${t('report.generating')}</span>`;
  try{
    const {canvas}=await buildReportCanvas();
    const url=canvas.toDataURL('image/png');
    preview.innerHTML=`<img src="${url}" alt="${t('report.imgAlt')}">`;
    preview.dataset.url=url;
  }catch(e){
    preview.innerHTML=`<span style="font-size:var(--fs-xs);color:var(--ink-muted)">${t('report.imgError')}</span>`;
    delete preview.dataset.url;
  }
}
const btnReport=document.getElementById('btn-report');
if(btnReport) btnReport.onclick=openReport;
const reportClose=document.getElementById('report-close');
if(reportClose) reportClose.onclick=closeReport;
const reportOverlay=document.getElementById('report-overlay');
if(reportOverlay){
  reportOverlay.addEventListener('click',e=>{
    if(e.target.id==='report-overlay')closeReport();
  });
}
const reportDownload=document.getElementById('report-download');
if(reportDownload){
  reportDownload.onclick=async()=>{
    const preview=document.getElementById('report-preview');
    let url=preview?.dataset?.url;
    if(!url){ try{ url=(await buildReportCanvas()).canvas.toDataURL('image/png'); }catch(e){ return; } }
    const a=document.createElement('a');
    a.href=url;a.download=`resumen_${(await missionReady)||'mision'}.png`;a.click();
  };
}

// ═══════════════════════════════════════════════════════════════════
// MENÚ DE AYUDA (con accesibilidad) + tema + modo operativo
// ═══════════════════════════════════════════════════════════════════
const helpBtn=document.getElementById('btn-help'), helpMenu=document.getElementById('help-menu');
function closeHelpMenu(){helpMenu.classList.remove('open');helpBtn.setAttribute('aria-expanded','false');}
helpBtn.onclick=()=>{
  const open=helpMenu.classList.toggle('open');
  helpBtn.setAttribute('aria-expanded',String(open));
};
document.addEventListener('click',e=>{
  if(!e.target.closest('.menu-wrap'))closeHelpMenu();
});
document.getElementById('btn-theme').onclick=toggleTheme;
// #btn-back: era un <a href="/"> hasta el rediseño de la barra superior,
// que lo cambió a <button> (para que "reset" lo despoje de estilo nativo)
// sin agregarle el onclick — quedó un botón sin acción, "no funciona".
document.getElementById('btn-back').onclick=()=>{ location.href='/'; };

// Preferencias de accesibilidad — persisten en localStorage, mismo patrón
// que el tema. Nunca dependen de hover (botones simples, siempre visibles).
function applyA11yPrefs(){
  const scale=localStorage.getItem('lc-scale')||'1';
  document.documentElement.style.setProperty('--text-scale',scale);
  document.querySelectorAll('.step-group button').forEach(b=>b.classList.toggle('active',b.dataset.scale===scale));
  const contrast=localStorage.getItem('lc-contrast')==='1';
  document.documentElement.setAttribute('data-contrast',contrast?'alto':'normal');
  document.getElementById('switch-contrast').setAttribute('aria-checked',String(contrast));
  const motion=localStorage.getItem('lc-motion')==='1';
  document.body.toggleAttribute('data-motion-off',motion);
  document.getElementById('switch-motion').setAttribute('aria-checked',String(motion));
}
document.querySelectorAll('.step-group button').forEach(b=>{
  b.onclick=()=>{localStorage.setItem('lc-scale',b.dataset.scale);applyA11yPrefs();};
});
document.getElementById('switch-contrast').onclick=function(){
  localStorage.setItem('lc-contrast',this.getAttribute('aria-checked')==='true'?'0':'1');applyA11yPrefs();
};
document.getElementById('switch-motion').onclick=function(){
  localStorage.setItem('lc-motion',this.getAttribute('aria-checked')==='true'?'0':'1');applyA11yPrefs();
};
applyA11yPrefs();

document.getElementById('btn-draw').onclick=toggleDraw;
document.getElementById('btn-compare').onclick=toggleCompare;
document.getElementById('btn-measure').onclick=toggleMeasure;
const btnExport=document.getElementById('btn-export');
if(btnExport) btnExport.onclick=exportView;

document.getElementById('panel-close').onclick=toggleSidebar;
document.getElementById('panel-toggle').onclick=toggleSidebar;

// ═══════════════════════════════════════════════════════════════════
// INIT — corre una vez que el LAYER_REGISTRY inicial (y SITUATION, si
// existe) están listos.
// ═══════════════════════════════════════════════════════════════════
(async function initPanel(){
  await loadSituation();
  await loadFlightQuality();
  await renderSituationHeader();
  await renderSummaryCards();
  renderCapasPanel();   // refresca la tarjeta Hotspot con los focos ya identificados
  await checkRelatedMissions();
})();
