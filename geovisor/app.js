// ═══════════════════════════════════════════════════════════════════
// PALETTES
// ═══════════════════════════════════════════════════════════════════
// Única paleta térmica: Ironbow (negro→púrpura→rojo→naranja→amarillo→
// blanco = frío→caliente). Es el estándar de facto en cámaras térmicas
// FLIR — quien responde a incendios ya la reconoce de su propio equipo de
// mano, así que no hace falta leyenda para leerla. El pedido original era
// "hot/cold" en el sentido de UNA sola paleta clara (vs. picker de 6
// opciones, que era ruido para un caso de uso con una sola respuesta
// correcta) — pero los colores que quedaron eran, sin querer, los mismos
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
// ACTIVAR LA MISIÓN DE LA URL — antes de leer nada más
// ═══════════════════════════════════════════════════════════════════
// /view/{mission} (la entrada normal desde la webapp) activa los symlinks
// y RECIÉN AHÍ redirige acá — pero un F5 sobre esta misma URL ya redirigida
// es un GET directo a un archivo estático, nunca vuelve a pasar por
// /view/. Si entre medio se activó otra misión (o el contenedor arrancó de
// cero), esta página leía bounds.json de lo que sea que estuviera activo
// en ESE momento — no necesariamente la de la URL. Con ?mission= presente
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
// bounds.json (por misión)
// ═══════════════════════════════════════════════════════════════════
// Centro/zoom por defecto (fallback si tiles/bounds.json no existe todavía,
// p.ej. corridas viejas sin regenerar tiles, o el geovisor se abrió sin
// ?mission= y sin ninguna misión activada nunca en este contenedor). Cada
// misión real tiene su propia ubicación — bounds.json lo calcula
// generate_tiles.py desde el centro real del ortomosaico.
let CENTER=[6.3619,-75.5465],ZOOM=17;
let THERMAL_MIN=15,THERMAL_MAX=55;   // fallback (vuelo original, rango amplio)
let INDEX_RANGES={};   // {} si la misión no tiene datos multiespectrales (M3M)
let MS_BAND_RANGES={};  // {} si la misión no tiene datos multiespectrales (M3M)
let RESOLUCION_CM=null; // cm/px MEDIDOS por producto (bounds.json)
// `preliminary` lo escribe export_flight_path.py cuando la corrida TODAVÍA
// está en curso y lo único que hay es la ruta de vuelo: el visor se abre
// igual (sirve desde el minuto uno) pero sabe que faltan productos y avisa
// cuando aparecen. generate_tiles.py lo pisa sin la marca al terminar.
let PRELIMINARY=false;
// true si esta carga inicial YA encontró un centro real (bounds.json existía,
// aunque sea la versión "preliminary" de export_flight_path.py). Si queda en
// false, es que se abrió el geovisor en la ventana de pocos segundos ANTES de
// que ese archivo exista siquiera — pollBoundsForChanges() recentra una sola
// vez apenas aparezca, en vez de dejar el mapa pegado en el respaldo fijo.
let boundsWasReal=false;
try{
  const req=new XMLHttpRequest();
  req.open('GET','tiles/bounds.json',false);
  req.send(null);
  if(req.status===200){
    const b=JSON.parse(req.responseText);
    if(b.center){CENTER=b.center;boundsWasReal=true;}
    if(b.zoom)ZOOM=b.zoom;
    if(b.thermal_range){THERMAL_MIN=b.thermal_range[0];THERMAL_MAX=b.thermal_range[1];}
    if(b.index_ranges)INDEX_RANGES=b.index_ranges;
    if(b.ms_band_ranges)MS_BAND_RANGES=b.ms_band_ranges;
    if(b.resolucion_cm)RESOLUCION_CM=b.resolucion_cm;
    PRELIMINARY=!!b.preliminary;
  }
}catch(e){}

// ═══════════════════════════════════════════════════════════════════
// MAP + PANES
// ═══════════════════════════════════════════════════════════════════
const map=L.map('map',{center:CENTER,zoom:ZOOM,maxZoom:21,zoomControl:false,
  attributionControl:{position:'bottomleft',prefix:false}});
// Columna única y discreta (zoom + encuadrar), abajo a la derecha — no el
// default de Leaflet arriba a la izquierda, que en el diseño nuevo queda
// tapado por el selector Mapa/Lista.
L.control.zoom({position:'bottomright'}).addTo(map);
const FitBoundsControl=L.Control.extend({
  options:{position:'bottomright'},
  onAdd:function(){
    const el=L.DomUtil.create('div','leaflet-bar');
    const btn=L.DomUtil.create('a','map-extra-control',el);
    btn.href='#';btn.title='Encuadrar toda la misión';btn.setAttribute('aria-label','Encuadrar toda la misión');
    btn.innerHTML='⤢';
    L.DomEvent.on(btn,'click',L.DomEvent.stop).on(btn,'click',()=>{
      if(MISSION_BOUNDS)map.fitBounds(MISSION_BOUNDS,{padding:[40,40]});else map.setView(CENTER,ZOOM);
    });
    return el;
  }
});
new FitBoundsControl().addTo(map);

// Cada capa de dato (no los mapas base) recibe su propio pane con un
// zIndex explícito — es lo que permite que el reordenamiento por arrastre
// del panel "Capas" cambie el orden VISUAL real en el mapa. Compartir el
// tilePane por defecto (como antes) solo permite z-order = orden de
// addTo(), que no se puede reordenar después de agregado.
const INDEX_NAMES=Object.keys(INDEX_RANGES);
const MS_BAND_IDS=Object.keys(MS_BAND_RANGES);
let layerOrder=['hillshade','rgb',...(MS_BAND_IDS.length?['ms_composite']:[]),'thermal',...INDEX_NAMES,'ndvi_class','gndvi_class','ndre_class','msavi2_class','severidad','hotspot_termico','area_afectada']; // bottom → top
layerOrder.forEach((id,i)=>map.createPane('pane-'+id).style.zIndex=210+i*10);

// "Calles": OSM crudo (colores saturados, cientos de etiquetas de comercios/
// POI que no aportan nada en una zona rural quemada) se cambió por CARTO
// Voyager — cartografía curada, gris-cálida, con SOLO vías/lugares/relieve
// (la referencia real que sirve para orientarse: caminos de acceso, veredas
// cercanas), sin el ruido visual de un mapa de ciudad. Misma licencia
// (datos OSM), atribución obligatoria abajo a la izquierda.
const osmBase=L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',{
  maxZoom:21,subdomains:'abcd',
  attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> · © <a href="https://carto.com/attribution" target="_blank" rel="noopener">CARTO</a>',
}).addTo(map);
const satBase=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
  maxZoom:19,attribution:'© Esri, Maxar, Earthstar Geographics',
});
// La satelital de Esri viene SIN nombres de calles/veredas — para orientarse
// (rutas de acceso, poblados cercanos) hace falta la capa de referencia
// (etiquetas + vías, transparente) encima. Se agrega/saca junto con la base,
// nunca sola — ver wireLayersPanel(), selector de mapa base.
const satLabels=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,pane:'overlayPane'});
let currentBase='osm';

const rgbLayer=L.tileLayer('tiles/rgb/{z}/{x}/{y}.png',{maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:1,pane:'pane-rgb'}).addTo(map);

// ── RGB con orden de canales configurable ('normal' o personalizado
// r/g/b→cualquier canal fuente) — mismo tile 'rgb' de siempre, remapeado en
// canvas client-side. channelOrder=['r','g','b'] es la identidad (igual que
// rgbLayer de arriba); se usa solo cuando el usuario elige un orden
// personalizado, para no pagar el costo de canvas en el caso normal. ──
const RgbSwizzleGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y,order=this.options.channelOrder||['r','g','b'];const CH={r:0,g:1,b:2};img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data;const src=new Uint8ClampedArray(d);for(let i=0;i<d.length;i+=4){d[i]=src[i+CH[order[0]]];d[i+1]=src[i+CH[order[1]]];d[i+2]=src[i+CH[order[2]]];}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/rgb/${z}/${x}/${y}.png`;return t;}});
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
// cada combinación posible en disco — el usuario elige presets (falso color
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
    img.src=`tiles/${bands[i]}/${z}/${x}/${y}.png`;
  });
  return t;
}});
const MS_COMPOSITE_PRESETS={
  cir:{label:'Falso color IR (R=NIR G=Red B=Green)',bands:['ms_nir','ms_red','ms_green']},
  rededge:{label:'RedEdge (R=NIR G=RedEdge B=Red)',bands:['ms_nir','ms_rededge','ms_red']},
};
const MS_BAND_LABELS={ms_red:'Red (espectral)',ms_green:'Green (espectral)',ms_nir:'NIR',ms_rededge:'RedEdge',dband_r:'Red (RGB banda D)',dband_g:'Green (RGB banda D)',dband_b:'Blue (RGB banda D)'};
let msCompositeBands=(MS_COMPOSITE_PRESETS.cir.bands.every(b=>MS_BAND_IDS.includes(b)))
  ? [...MS_COMPOSITE_PRESETS.cir.bands] : [MS_BAND_IDS[0],MS_BAND_IDS[1]||MS_BAND_IDS[0],MS_BAND_IDS[2]||MS_BAND_IDS[0]];
let msCompositeLayer=MS_BAND_IDS.length? new BandCompositeGrid({bandR:msCompositeBands[0],bandG:msCompositeBands[1],bandB:msCompositeBands[2],maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:1,pane:'pane-ms_composite'}) : null; // let: registerMsComposite() la crea después si la misión arrancó sin datos MS
function setMsCompositeBands(bands){
  msCompositeBands=bands;
  msCompositeLayer.options.bandR=bands[0];msCompositeLayer.options.bandG=bands[1];msCompositeLayer.options.bandB=bands[2];
  msCompositeLayer.redraw();
}

// Thermal canvas layer
const ThermalGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y;img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data,lut=currentLUT;for(let i=0;i<d.length;i+=4){const v=d[i],idx=v*4,origA=d[i+3];d[i]=lut[idx];d[i+1]=lut[idx+1];d[i+2]=lut[idx+2];d[i+3]=origA*lut[idx+3]/255;}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/thermal/${z}/${x}/${y}.png`;return t;}});
let thermalLayer=new ThermalGrid({maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.7,pane:'pane-thermal'}).addTo(map);

// Hillshade from DSM tiles (if available)
let hillshadeLayer=L.tileLayer('tiles/hillshade/{z}/{x}/{y}.png',{maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.4,pane:'pane-hillshade'});

// Índices de vegetación (NDVI/GNDVI/NDRE) — solo existen si la misión trae
// datos multiespectrales (/input_ms montado). Mismo patrón canvas-remap que
// ThermalGrid, pero con la paleta divergente fija.
const IndexGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y,name=this.options.indexName;img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data,lut=INDEX_LUT;for(let i=0;i<d.length;i+=4){const v=d[i],idx=v*4,origA=d[i+3];d[i]=lut[idx];d[i+1]=lut[idx+1];d[i+2]=lut[idx+2];d[i+3]=origA*lut[idx+3]/255;}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/${name}/${z}/${x}/${y}.png`;return t;}});
const INDEX_LABELS={ndvi:['🌿 NDVI','Salud/vigor de vegetación'],gndvi:['🌾 GNDVI','Sensible a clorofila'],ndre:['🍃 NDRE','Estrés en dosel denso'],msavi2:['🌱 MSAVI2','NDVI corregido por brillo de suelo — más confiable que NDVI en dosel disperso/regeneración post-incendio']};
const indexLayers={};
INDEX_NAMES.forEach(name=>{
  indexLayers[name]=new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.8,pane:'pane-'+name});
});

// Severidad / hotspot térmico: clases DISCRETAS (0=sin dato, 1-4), no un
// gradiente continuo — cada valor de píxel mapea a un color exacto, sin
// interpolar (interpolar inventaría una "clase 2.5" que no existe). Los
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
const SEVERIDAD_LUT=buildDiscreteLUT({0:[0,0,0,0],1:[34,139,34,255],2:[255,235,59,255],3:[255,152,0,255],4:[211,47,47,255]});
const HOTSPOT_LUT=buildDiscreteLUT({0:[0,0,0,0],1:[33,150,243,80],2:[255,235,59,255],3:[255,152,0,255],4:[198,40,40,255]});
const ClassGrid=L.GridLayer.extend({createTile:function(coords,done){const t=document.createElement('canvas');t.width=256;t.height=256;const ctx=t.getContext('2d'),img=new Image();img.crossOrigin='anonymous';const z=coords.z,x=coords.x,y=coords.y,name=this.options.layerName,lut=this.options.lut;img.onload=function(){ctx.drawImage(img,0,0);const d=ctx.getImageData(0,0,256,256).data;for(let i=0;i<d.length;i+=4){const v=d[i],idx=v*4;d[i]=lut[idx];d[i+1]=lut[idx+1];d[i+2]=lut[idx+2];d[i+3]=lut[idx+3];}ctx.putImageData(new ImageData(d,256,256),0,0);done(null,t);};img.onerror=function(){done(null,t);};img.src=`tiles/${name}/${z}/${x}/${y}.png`;return t;}});
const severidadLayer=new ClassGrid({layerName:'severidad',lut:SEVERIDAD_LUT,maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.85,pane:'pane-severidad'});
const hotspotLayer=new ClassGrid({layerName:'hotspot_termico',lut:HOTSPOT_LUT,maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.85,pane:'pane-hotspot_termico'});

L.control.scale({imperial:false,metric:true,position:'bottomleft'}).addTo(map);

// Norte + coordenadas: se habían sacado del rediseño por error (el mockup
// de referencia no las mostraba, pero el pedido original de mantenerlas
// seguía en pie). Va como control de Leaflet —no un div absoluto a mano—
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

// ── Registro de capas para el panel "Capas" (gestor unificado) ──
const LAYER_REGISTRY={
  hillshade:{label:'⛰️ Relieve (DSM)',group:'terreno',layer:hillshadeLayer,defaultOn:false,defaultOpacity:.4,
    legend:()=>`<div class="stat-row"><span class="lbl">Resolución</span><span class="val">${gsdTxt('dsm')}</span></div>
      <p>Sombreado de relieve calculado sobre el <b>DSM</b> (modelo digital de
      <i>superficie</i>): incluye vegetación y construcciones, no es un modelo de
      terreno desnudo (DTM). Solo referencia visual, sin unidades.</p>`},
  rgb:{label:'📷 RGB',group:'opticas',layer:rgbLayer,defaultOn:true,defaultOpacity:1,
    legend:()=>`<div class="stat-row"><span class="lbl">GSD</span><span class="val cool">${gsdTxt('rgb')}</span></div><div class="stat-row"><span class="lbl">Sensor</span><span class="val">DJI Zenmuse H20T (wide)</span></div>
      <div class="band-picker" style="margin-top:8px">
        <label>Canales<select class="rgb-channel-preset">
          <option value="normal"${rgbChannelOrder.join(',')==='r,g,b'?' selected':''}>Normal (R-G-B)</option>
          <option value="custom"${rgbChannelOrder.join(',')!=='r,g,b'?' selected':''}>Personalizado</option>
        </select></label>
        <div class="rgb-channel-custom" style="${rgbChannelOrder.join(',')!=='r,g,b'?'':'display:none'}">
          ${['R','G','B'].map((lbl,i)=>`<label>${lbl}<select class="rgb-channel-sel" data-ch="${i}">
            ${['r','g','b'].map(c=>`<option value="${c}"${rgbChannelOrder[i]===c?' selected':''}>${c.toUpperCase()}</option>`).join('')}
          </select></label>`).join('')}
        </div>
      </div>`},
};
if(MS_BAND_IDS.length){
  LAYER_REGISTRY.ms_composite={label:'🎨 Multiespectral (compuesto)',group:'opticas',layer:msCompositeLayer,defaultOn:false,defaultOpacity:1,
    legend:()=>{
      const presetKey=Object.entries(MS_COMPOSITE_PRESETS).find(([,p])=>p.bands.join(',')===msCompositeBands.join(','))?.[0]||'custom';
      return `<p>Composición RGB armada en el navegador combinando 3 bandas espectrales crudas — no hay un archivo fijo por combinación, cambiar la selección recompone al vuelo.</p>
      <div class="band-picker">
        <label>Preset<select class="ms-composite-preset">
          ${Object.entries(MS_COMPOSITE_PRESETS).map(([k,p])=>`<option value="${k}"${presetKey===k?' selected':''}>${p.label}</option>`).join('')}
          <option value="custom"${presetKey==='custom'?' selected':''}>Personalizado</option>
        </select></label>
        <div class="ms-composite-custom" style="${presetKey==='custom'?'':'display:none'}">
          ${['R','G','B'].map((lbl,i)=>`<label>${lbl}<select class="ms-composite-sel" data-ch="${i}">
            ${MS_BAND_IDS.map(id=>`<option value="${id}"${msCompositeBands[i]===id?' selected':''}>${MS_BAND_LABELS[id]||id}</option>`).join('')}
          </select></label>`).join('')}
        </div>
      </div>`;}};
}
LAYER_REGISTRY.thermal={label:'🌡️ Térmico',group:'termicas',layer:thermalLayer,defaultOn:true,defaultOpacity:.7,
    legend:()=>{const pal=PALETTES[currentPalette],ramp=pal.colors.join(',');return `<div class="stat-row"><span class="lbl">GSD</span><span class="val warm">${gsdTxt('thermal')}</span></div>
      <div class="stat-row"><span class="lbl">Rango</span><span class="val warm">${THERMAL_MIN.toFixed(1)}–${THERMAL_MAX.toFixed(1)} °C</span></div>
      <div style="margin-top:6px"><div class="legend-bar" style="background:linear-gradient(to right,${ramp})"></div>
      <div class="legend-lbl"><span>${THERMAL_MIN.toFixed(1)}°C</span><span>${((THERMAL_MIN+THERMAL_MAX)/2).toFixed(1)}°C</span><span>${THERMAL_MAX.toFixed(1)}°C</span></div></div>`;}};
INDEX_NAMES.forEach(name=>{
  const [label,desc]=INDEX_LABELS[name]||[name.toUpperCase(),''];
  const [lo,hi]=INDEX_RANGES[name];
  LAYER_REGISTRY[name]={label,group:'indices',layer:indexLayers[name],defaultOn:false,defaultOpacity:.8,
    legend:()=>{const ramp=INDEX_PALETTE.colors.join(',');return `<p>${desc}</p>
      <div class="stat-row"><span class="lbl">Rango</span><span class="val cool">${lo.toFixed(2)} a ${hi.toFixed(2)}</span></div>
      <div style="margin-top:6px"><div class="legend-bar" style="background:linear-gradient(to right,${ramp})"></div>
      <div class="legend-lbl"><span>${lo.toFixed(2)}</span><span>${((lo+hi)/2).toFixed(2)}</span><span>${hi.toFixed(2)}</span></div></div>`;}};
});
function classLegend(classes){
  return `<div class="legend-classes">${classes.map(([color,label])=>
    `<div class="stat-row"><span style="display:inline-block;width:12px;height:12px;border-radius:2px;background:${color};margin-right:6px;vertical-align:middle"></span><span class="lbl">${label}</span></div>`
  ).join('')}</div>`;
}
LAYER_REGISTRY.severidad={label:'🔥 Severidad',group:'impacto',layer:severidadLayer,defaultOn:false,defaultOpacity:.85,
  legend:()=>`<p>Severidad relativa al vigor de vegetación sana de esta misma misión (z-score robusto de brillo multiespectral — se autocalibra a cada vuelo, no un umbral fijo). Cortes en 1/2/3 sigma (regla empírica 68-95-99.7 de control estadístico de procesos). Recortado al polígono de área afectada — el verde NO significa "fuera del incendio" (eso ya se recortó), significa terreno DENTRO del perímetro sin anomalía espectral: islas reales sin quemar (roca, claro, vegetación húmeda) o huecos que el detector rellena al cerrar el contorno.</p>`+
    classLegend([['#228B22','Isla no quemada (&lt;1σ)'],['#FFEB3B','Leve (1-2σ)'],['#FF9800','Moderado (2-3σ)'],['#D32F2F','Severo (≥3σ)']])},
LAYER_REGISTRY.hotspot_termico={label:'♨️ Hotspot térmico',group:'impacto',layer:hotspotLayer,defaultOn:false,defaultOpacity:.85,
  legend:()=>`<p>Temperatura ABSOLUTA (no anomalía relativa — un umbral relativo da falsos positivos en suelo/cultivo calentado por el sol). El corte de "foco activo" (88°C/190°F) es el umbral operacional citado en literatura de detección de hotspots con drones para "fuego activo bajo superficie". Uso operacional: riesgo de reactivación / mop-up, distinto de la severidad de daño.</p>`+
    classLegend([['#2196F3','Normal (&lt;40°C)'],['#FFEB3B','Elevado (40-60°C)'],['#FF9800','Caliente (60-88°C)'],['#C62828','Foco activo (≥88°C)']])};

const INDEX_CLASS_LUTS={
  ndvi_class:buildDiscreteLUT({0:[0,0,0,0],1:[141,110,99,255],2:[255,235,59,255],3:[76,175,80,255]}),
  gndvi_class:buildDiscreteLUT({0:[0,0,0,0],1:[211,47,47,255],2:[255,235,59,255],3:[76,175,80,255]}),
  ndre_class:buildDiscreteLUT({0:[0,0,0,0],1:[211,47,47,255],2:[255,152,0,255],3:[139,195,74,255],4:[27,94,32,255]}),
  msavi2_class:buildDiscreteLUT({0:[0,0,0,0],1:[141,110,99,255],2:[255,235,59,255],3:[76,175,80,255]}),
};
const INDEX_CLASS_DEFS={
  ndvi_class:{label:'🌿 NDVI clasificado',desc:'Cortes estándar USGS.',classes:[['#8D6E63','Sin vegetación (&lt;0.1)'],['#FFEB3B','Escasa/estresada (0.1-0.6)'],['#4CAF50','Densa y sana (≥0.6)']]},
  gndvi_class:{label:'🌾 GNDVI clasificado',desc:'Cortes estándar de teledetección agrícola.',classes:[['#D32F2F','Estrés severo (&lt;0.3)'],['#FFEB3B','Moderada/estresada (0.3-0.5)'],['#4CAF50','Sana (≥0.5)']]},
  ndre_class:{label:'🍃 NDRE clasificado',desc:'Cortes estándar de nitrógeno foliar (agricultura de precisión).',classes:[['#D32F2F','Deficiencia N (&lt;0.2)'],['#FF9800','Transición (0.2-0.3)'],['#8BC34A','Saludable (0.3-0.6)'],['#1B5E20','Óptimo/maduro (≥0.6)']]},
  msavi2_class:{label:'🌱 MSAVI2 clasificado',desc:'NDVI corregido por brillo de suelo (Qi et al. 1994) — más confiable que NDVI en dosel disperso (regeneración post-incendio, cobertura &lt;30%). Mismos cortes que NDVI (ver docstring de compute_severity_classes.py: MSAVI2 no tiene convención propia tan establecida, se reusa la de NDVI como punto de partida).',classes:[['#8D6E63','Sin vegetación (&lt;0.1)'],['#FFEB3B','Escasa/estresada (0.1-0.6)'],['#4CAF50','Densa y sana (≥0.6)']]},
};
const indexClassLayers={};
Object.keys(INDEX_CLASS_DEFS).forEach(name=>{
  indexClassLayers[name]=new ClassGrid({layerName:name,lut:INDEX_CLASS_LUTS[name],maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.85,pane:'pane-'+name});
  const def=INDEX_CLASS_DEFS[name];
  LAYER_REGISTRY[name]={label:def.label,group:'indices',layer:indexClassLayers[name],defaultOn:false,defaultOpacity:.85,
    legend:()=>`<p>${def.desc}</p>`+classLegend(def.classes)};
});

// ═══════════════════════════════════════════════════════════════════
// Polígono del área afectada (detect_area_afectada.py) — capa vectorial
// EDITABLE: agregar/quitar polígonos sueltos, agregar/quitar vértices,
// área en vivo en hectáreas, guardado al servidor.
// ═══════════════════════════════════════════════════════════════════
const areaAfectadaLayer=L.layerGroup();   // capa "real" para el panel de Capas
areaAfectadaLayer.options.opacity=1;
const areaEditHandlesGroup=L.layerGroup(); // vértices+basurero, solo en modo edición
let areaEditMode=false,areaDirty=false,areaDrawingNew=false;
let areaPolyEntries=[];      // {rings:[[LatLng,...]], layer, vertexMarkers:[], midMarkers:[], trashMarker}
let areaOriginalGeoJSON=null;
let areaNewRingPoints=[],areaNewPreviewLayer=null;

function ringAreaM2(latlngs){
  // shoelace en proyección equirectangular local (centrada en la latitud
  // media del anillo) — precisión de sobra a la escala de una misión de
  // dron (pocas hectáreas, cientos de metros de extensión).
  if(latlngs.length<3)return 0;
  const R=6378137,meanLat=latlngs.reduce((s,p)=>s+p.lat,0)/latlngs.length*Math.PI/180;
  const pts=latlngs.map(p=>({x:p.lng*Math.PI/180*R*Math.cos(meanLat),y:p.lat*Math.PI/180*R}));
  let a=0;
  for(let i=0;i<pts.length;i++){const j=(i+1)%pts.length;a+=pts[i].x*pts[j].y-pts[j].x*pts[i].y;}
  return Math.abs(a/2);
}
function polygonAreaM2(rings){
  if(!rings.length)return 0;
  let a=ringAreaM2(rings[0]);
  for(let i=1;i<rings.length;i++)a-=ringAreaM2(rings[i]); // huecos se restan
  return Math.max(0,a);
}
function totalAreaHa(){return areaPolyEntries.reduce((s,e)=>s+polygonAreaM2(e.rings),0)/10000;}
function updateAreaBadge(){
  const el=document.getElementById('area-edit-ha');
  if(el)el.textContent=totalAreaHa().toFixed(2)+' ha';
}

function geojsonToRings(geom){
  const polys=geom.type==='MultiPolygon'?geom.coordinates:[geom.coordinates];
  return polys.map(rings=>rings.map(ring=>ring.map(c=>L.latLng(c[1],c[0]))));
}
function ringsToGeojsonCoords(polys){
  return polys.map(rings=>rings.map(ring=>ring.map(ll=>[ll.lng,ll.lat])));
}

function clearEditHandles(entry){
  entry.vertexMarkers.forEach(m=>areaEditHandlesGroup.removeLayer(m));
  entry.midMarkers.forEach(m=>areaEditHandlesGroup.removeLayer(m));
  entry.vertexMarkers=[];entry.midMarkers=[];
}
function vertexIcon(){return L.divIcon({className:'area-vertex',iconSize:[10,10]});}
function midIcon(){return L.divIcon({className:'area-vertex-mid',iconSize:[8,8]});}
function rebuildMidMarkers(entry){
  entry.midMarkers.forEach(m=>areaEditHandlesGroup.removeLayer(m));
  const ring=entry.rings[0];
  entry.midMarkers=ring.map((ll,i)=>{
    const j=(i+1)%ring.length;
    const mid=L.latLng((ll.lat+ring[j].lat)/2,(ll.lng+ring[j].lng)/2);
    const m=L.marker(mid,{icon:midIcon(),pane:'pane-area_afectada'});
    m.on('click',ev=>{
      L.DomEvent.stopPropagation(ev);
      ring.splice(i+1,0,m.getLatLng());
      entry.layer.setLatLngs(entry.rings);
      buildEditHandles(entry);
      areaDirty=true;updateAreaBadge();
    });
    areaEditHandlesGroup.addLayer(m);
    return m;
  });
}
// Solo mueve los marcadores de punto medio ya existentes (sin recrearlos)
// — se usa durante el arrastre de un vértice, que dispara 'drag' muchas
// veces por segundo; recrear marcadores (rebuildMidMarkers) en cada tick
// se ve tembloroso en polígonos con muchos vértices.
function repositionMidMarkers(entry){
  const ring=entry.rings[0];
  entry.midMarkers.forEach((m,i)=>{
    const j=(i+1)%ring.length;
    m.setLatLng(L.latLng((ring[i].lat+ring[j].lat)/2,(ring[i].lng+ring[j].lng)/2));
  });
}
function buildEditHandles(entry){
  clearEditHandles(entry);
  const ring=entry.rings[0];
  ring.forEach((ll,i)=>{
    const m=L.marker(ll,{icon:vertexIcon(),draggable:true,pane:'pane-area_afectada'});
    m.on('drag',()=>{
      ring[i]=m.getLatLng();
      entry.layer.setLatLngs(entry.rings);
      repositionMidMarkers(entry);
      updateAreaBadge();
    });
    m.on('dragend',()=>{areaDirty=true;updateAreaBadge();});
    m.on('click',ev=>{
      L.DomEvent.stopPropagation(ev);
      if(ring.length<=3)return; // no dejar degenerar el polígono por debajo de un triángulo
      ring.splice(i,1);
      entry.layer.setLatLngs(entry.rings);
      buildEditHandles(entry);
      areaDirty=true;updateAreaBadge();
    });
    entry.vertexMarkers.push(m);
    areaEditHandlesGroup.addLayer(m);
  });
  rebuildMidMarkers(entry);
  if(!entry.trashMarker){
    entry.trashMarker=L.marker(entry.layer.getBounds().getCenter(),{icon:L.divIcon({className:'area-trash',html:'🗑',iconSize:[26,26]}),pane:'pane-area_afectada'});
    entry.trashMarker.on('click',ev=>{L.DomEvent.stopPropagation(ev);deletePolygonEntry(entry);});
  }
  areaEditHandlesGroup.addLayer(entry.trashMarker);
}

function addPolygonEntry(rings){
  const layer=L.polygon(rings,{color:'#ff1744',weight:3,fillColor:'#ff1744',fillOpacity:.05,pane:'pane-area_afectada'});
  areaAfectadaLayer.addLayer(layer);
  const entry={rings,layer,vertexMarkers:[],midMarkers:[],trashMarker:null};
  areaPolyEntries.push(entry);
  if(areaEditMode)buildEditHandles(entry);
  return entry;
}
function deletePolygonEntry(entry){
  areaAfectadaLayer.removeLayer(entry.layer);
  clearEditHandles(entry);
  if(entry.trashMarker)areaEditHandlesGroup.removeLayer(entry.trashMarker);
  areaPolyEntries=areaPolyEntries.filter(e=>e!==entry);
  areaDirty=true;updateAreaBadge();
}
function loadAreaAfectada(geo){
  areaPolyEntries.forEach(e=>{areaAfectadaLayer.removeLayer(e.layer);clearEditHandles(e);if(e.trashMarker)areaEditHandlesGroup.removeLayer(e.trashMarker);});
  areaPolyEntries=[];
  areaOriginalGeoJSON=JSON.parse(JSON.stringify(geo));
  const feat=geo.features&&geo.features[0];
  if(feat)geojsonToRings(feat.geometry).forEach(rings=>addPolygonEntry(rings));
  areaDirty=false;
  updateAreaBadge();
}

function enterAreaEditMode(){
  areaEditMode=true;
  areaEditHandlesGroup.addTo(map);
  areaPolyEntries.forEach(buildEditHandles);
  document.getElementById('area-edit-panel').classList.add('editing');
  document.getElementById('btn-area-edit').textContent='✕ Salir de edición';
}
function exitAreaEditMode(){
  areaEditMode=false;
  cancelDrawNewArea();
  areaPolyEntries.forEach(e=>{clearEditHandles(e);if(e.trashMarker){areaEditHandlesGroup.removeLayer(e.trashMarker);e.trashMarker=null;}});
  map.removeLayer(areaEditHandlesGroup);
  document.getElementById('area-edit-panel').classList.remove('editing');
  document.getElementById('btn-area-edit').textContent='✏️ Editar';
}
function toggleAreaEdit(){areaEditMode?exitAreaEditMode():enterAreaEditMode();}

function onDrawNewAreaClick(e){
  areaNewRingPoints.push(e.latlng);
  if(areaNewPreviewLayer)map.removeLayer(areaNewPreviewLayer);
  if(areaNewRingPoints.length>1)areaNewPreviewLayer=L.polyline(areaNewRingPoints,{color:'#ff1744',weight:2,dashArray:'4,4'}).addTo(map);
}
function finishDrawNewArea(e){
  if(e)L.DomEvent.stopPropagation(e);
  // Un doble-click dispara click+click+dblclick en el DOM: el último click
  // ya agregó un punto pegado al anterior (misma posición) antes de que
  // este handler corriera — se descarta para no dejar un vértice duplicado.
  if(areaNewRingPoints.length>=2){
    const a=areaNewRingPoints[areaNewRingPoints.length-1],b=areaNewRingPoints[areaNewRingPoints.length-2];
    if(Math.abs(a.lat-b.lat)<1e-7&&Math.abs(a.lng-b.lng)<1e-7)areaNewRingPoints.pop();
  }
  if(areaNewRingPoints.length>=3){
    addPolygonEntry([areaNewRingPoints.slice()]);
    areaDirty=true;updateAreaBadge();
  }
  cancelDrawNewArea();
}
function cancelDrawNewArea(){
  if(!areaDrawingNew)return;
  areaDrawingNew=false;
  map.off('click',onDrawNewAreaClick);
  map.off('dblclick',finishDrawNewArea);
  map.doubleClickZoom.enable();
  map.getContainer().style.cursor='';
  if(areaNewPreviewLayer){map.removeLayer(areaNewPreviewLayer);areaNewPreviewLayer=null;}
  areaNewRingPoints=[];
  const btn=document.getElementById('btn-area-new');
  if(btn)btn.textContent='➕ Nueva área';
}
function startDrawNewArea(){
  if(areaDrawingNew){finishDrawNewArea();return;}
  areaDrawingNew=true;
  areaNewRingPoints=[];
  document.getElementById('btn-area-new').textContent='✓ Terminar (doble-click)';
  map.getContainer().style.cursor='crosshair';
  map.doubleClickZoom.disable();
  map.on('click',onDrawNewAreaClick);
  map.on('dblclick',finishDrawNewArea);
}

async function saveAreaAfectada(){
  const coords=ringsToGeojsonCoords(areaPolyEntries.map(e=>e.rings));
  const areaM2=areaPolyEntries.reduce((s,e)=>s+polygonAreaM2(e.rings),0);
  const origProps=(areaOriginalGeoJSON&&areaOriginalGeoJSON.features&&areaOriginalGeoJSON.features[0]&&areaOriginalGeoJSON.features[0].properties)||{};
  const geo={
    type:'FeatureCollection',name:'area_afectada',
    crs:{type:'name',properties:{name:'urn:ogc:def:crs:OGC:1.3:CRS84'}},
    features:coords.length?[{
      type:'Feature',
      properties:{area_m2:areaM2,metodo:(origProps.metodo||'')+` — editado manualmente en el geovisor (${new Date().toISOString()})`},
      geometry:{type:'MultiPolygon',coordinates:coords}
    }]:[]
  };
  const statusEl=document.getElementById('area-edit-status');
  statusEl.textContent='Guardando…';
  try{
    const resp=await fetch('api/save-area-afectada',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(geo)});
    if(!resp.ok)throw new Error('HTTP '+resp.status);
    areaOriginalGeoJSON=geo;
    areaDirty=false;
    statusEl.textContent='✅ Guardado';
    setTimeout(()=>{if(statusEl.textContent==='✅ Guardado')statusEl.textContent='';},3000);
  }catch(err){
    statusEl.textContent='❌ Error al guardar: '+err.message;
  }
}
function discardAreaEdits(){
  if(areaDirty&&!confirm('¿Descartar los cambios sin guardar?'))return;
  loadAreaAfectada(areaOriginalGeoJSON);
  if(areaEditMode)areaPolyEntries.forEach(buildEditHandles);
  document.getElementById('area-edit-status').textContent='';
}

// Se carga sync (mismo patrón que bounds.json) — si la misión no tiene
// multiespectral+térmico, el archivo no existe y se omite sin error.
areaAfectadaLayer.setOpacity=function(v){areaPolyEntries.forEach(e=>e.layer.setStyle({opacity:v,fillOpacity:.05*v}));};
try{
  const req=new XMLHttpRequest();
  req.open('GET','outputs/area_afectada.geojson',false);
  req.send(null);
  if(req.status===200){
    loadAreaAfectada(JSON.parse(req.responseText));
    document.getElementById('area-edit-panel').classList.add('visible');
    LAYER_REGISTRY.area_afectada={label:'📐 Polígono área afectada',group:'impacto',layer:areaAfectadaLayer,defaultOn:true,defaultOpacity:1,
      legend:()=>`<p>Contorno detectado automáticamente (ver capa Severidad para la metodología), editable con el botón 📐 sobre el mapa. Referencia espacial de dónde se recortan severidad/hotspot — no reemplaza una verificación en terreno.</p>`};
  }
}catch(e){}

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
    const SN={rgb:'RGB',thermal:'térmico',multispectral:'multiespectral'};
    const resumen=Object.entries(tally).map(([s,n])=>`${n} ${SN[s]||s}`).join(' · ');
    // Encendida por defecto solo mientras la corrida está en curso: una vez
    // que hay ortomosaicos, el recorrido estorba más de lo que aporta.
    LAYER_REGISTRY.flight_path={label:'🛩️ Ruta de vuelo',group:'vuelo',layer:flightLayer,
      defaultOn:PRELIMINARY,defaultOpacity:1,
      legend:()=>`<p>Recorrido y posición de cada captura, según el GPS embebido en
        las fotos (${resumen}). Se genera antes de la reconstrucción, así que está
        disponible mientras el procesamiento sigue en curso.</p>`};
  }
}catch(e){}

// Orden pensado para decisión, no para flujo técnico: lo que más pesa para
// decidir dónde actuar (severidad, hotspots, área) va primero — no al final
// de un scroll, que es donde quedaba con el orden "técnico" anterior.
const GROUP_LABELS={impacto:'🔥 Impacto del incendio',indices:'🌿 Índices',opticas:'📷 Ópticas',termicas:'🌡️ Térmicas',terreno:'⛰️ Terreno',vuelo:'🛩️ Vuelo'};
const GROUP_ORDER=['impacto','indices','opticas','termicas','terreno','vuelo'];

Object.entries(LAYER_REGISTRY).forEach(([id,def])=>{
  if(def.defaultOn)def.layer.addTo(map);
});

function applyLayerOrder(){
  layerOrder.forEach((id,i)=>{
    const pane=map.getPane('pane-'+id);
    if(pane)pane.style.zIndex=210+i*10;
  });
}

// ═══════════════════════════════════════════════════════════════════
// COMPARE SLIDER
// ═══════════════════════════════════════════════════════════════════
// Fábrica de instancias de capa para el comparador — cada lado usa su
// PROPIA instancia (Leaflet no permite una misma capa en dos mapas a la
// vez), armada con la misma receta que la capa original de LAYER_REGISTRY.
// area_afectada (vectorial) queda afuera a propósito: el comparador es
// para capas ráster lado a lado, no tiene sentido ahí.
const RASTER_LAYER_FACTORY={
  hillshade:()=>L.tileLayer('tiles/hillshade/{z}/{x}/{y}.png',{maxZoom:21,maxNativeZoom:20,minZoom:14}),
  rgb:()=>L.tileLayer('tiles/rgb/{z}/{x}/{y}.png',{maxZoom:21,maxNativeZoom:20,minZoom:14}),
  thermal:()=>new ThermalGrid({maxZoom:21,maxNativeZoom:20,minZoom:14}),
  ...(MS_BAND_IDS.length?{ms_composite:()=>new BandCompositeGrid({bandR:msCompositeBands[0],bandG:msCompositeBands[1],bandB:msCompositeBands[2],maxZoom:21,maxNativeZoom:20,minZoom:14})}:{}),
  severidad:()=>new ClassGrid({layerName:'severidad',lut:SEVERIDAD_LUT,maxZoom:21,maxNativeZoom:20,minZoom:14}),
  hotspot_termico:()=>new ClassGrid({layerName:'hotspot_termico',lut:HOTSPOT_LUT,maxZoom:21,maxNativeZoom:20,minZoom:14}),
};
INDEX_NAMES.forEach(name=>{RASTER_LAYER_FACTORY[name]=()=>new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14});});
Object.keys(INDEX_CLASS_DEFS).forEach(name=>{RASTER_LAYER_FACTORY[name]=()=>new ClassGrid({layerName:name,lut:INDEX_CLASS_LUTS[name],maxZoom:21,maxNativeZoom:20,minZoom:14});});
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
  COMPARABLE_IDS.forEach(id=>{
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
    }
    // #compare-left-map (el contenedor real de Leaflet) siempre queda al
    // ANCHO COMPLETO del visor — solo #compare-left (el div exterior, con
    // overflow:hidden) se achica al arrastrar. Si en cambio se achicara el
    // propio contenedor de Leaflet, su noción interna de viewport/tiles
    // quedaría calculada para un mapa más chico, y el recorte visual y la
    // posición geográfica real se desalinean (el bug original).
    const fullW=document.getElementById('compare-container').clientWidth;
    document.getElementById('compare-left-map').style.width=fullW+'px';
    setTimeout(()=>{compareLeftMap.invalidateSize();compareRightMap.invalidateSize();updateSlider();},100);
    initSliderDrag();
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
  applyLayerFilter();   // el panel se redibuja entero: reaplicar la búsqueda
}

// ── Panel "Capas" (modo operativo): selector de mapa base + capas agrupadas ──
function basePickerHTML(){
  return `<div class="base-picker">
    <button class="${currentBase==='osm'?'active':''}" data-base="osm">🗺️ Calles</button>
    <button class="${currentBase==='sat'?'active':''}" data-base="sat">🛰️ Satélite</button>
    </div>`;
}
function layerCardHTML(id){
  const def=LAYER_REGISTRY[id];
  const shown=map.hasLayer(def.layer);
  const opacity=Math.round((def.layer.options.opacity??1)*100);
  return `<div class="layer-card${shown?' is-on':''}" data-id="${id}" data-name="${def.label.toLowerCase()}">
    <div class="layer-card-head">
      <span class="layer-handle" title="Arrastrar para reordenar">⠿</span>
      <label class="layer-name"><input type="checkbox" class="layer-vis" data-id="${id}" ${shown?'checked':''}><span class="nm">${def.label}</span></label>
      <span class="layer-tools">
        <button class="layer-solo" data-id="${id}" title="Ver solo esta capa">◉</button>
        <button class="layer-zoom" data-id="${id}" title="Encuadrar esta capa">⤢</button>
        <button class="layer-legend-toggle" data-id="${id}" title="Ver descripción y leyenda">▶</button>
      </span>
    </div>
    <div class="layer-card-opacity">
      <span>Opacidad</span>
      <input type="range" class="layer-opacity" data-id="${id}" min="0" max="100" value="${opacity}">
      <span class="layer-opacity-val">${opacity}%</span>
    </div>
    <div class="layer-legend-body" data-id="${id}">${def.legend()}</div>
  </div>`;
}
function layersPanelHTML(){
  let html=basePickerHTML();
  GROUP_ORDER.forEach(group=>{
    const ids=layerOrder.filter(id=>LAYER_REGISTRY[id]&&LAYER_REGISTRY[id].group===group);
    if(ids.length===0)return;
    html+=`<div class="layer-group" data-group="${group}">
      <div class="layer-group-title">${GROUP_LABELS[group]}</div>
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
      const cards=()=>Array.from(list.querySelectorAll('.layer-card'));
      card.classList.add('dragging');
      handle.setPointerCapture(e.pointerId);

      function onMove(ev){
        const y=ev.clientY;
        const siblings=cards().filter(c=>c!==card);
        for(const sib of siblings){
          const r=sib.getBoundingClientRect();
          const mid=r.top+r.height/2;
          if(y<mid&&sib.compareDocumentPosition(card)&Node.DOCUMENT_POSITION_FOLLOWING){
            list.insertBefore(card,sib);break;
          }else if(y>mid&&sib.compareDocumentPosition(card)&Node.DOCUMENT_POSITION_PRECEDING){
            list.insertBefore(card,sib.nextSibling);break;
          }
        }
      }
      function onUp(){
        card.classList.remove('dragging');
        handle.releasePointerCapture(e.pointerId);
        document.removeEventListener('pointermove',onMove);
        document.removeEventListener('pointerup',onUp);
        // Recalcular layerOrder GLOBAL a partir del orden actual dentro de
        // cada grupo (el orden entre grupos ya lo fija GROUP_ORDER).
        const newOrder=[];
        GROUP_ORDER.forEach(group=>{
          const groupList=document.querySelector(`.layer-list[data-group="${group}"]`);
          if(!groupList)return;
          groupList.querySelectorAll('.layer-card').forEach(c=>newOrder.push(c.dataset.id));
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
// PRODUCTOS PROGRESIVOS — la corrida agrega capas mientras sigue viva
// ═══════════════════════════════════════════════════════════════════
// RGB/térmico/hillshade/severidad/hotspot/índices-clasificados YA están
// registrados desde el arranque (sus tiles pueden no existir todavía —
// simplemente no cargan hasta que aparecen; un .redraw() los recupera, ver
// más abajo). Lo que SÍ falta registrar en caliente son los productos que
// ni siquiera existían como CONCEPTO al cargar la página: los índices
// continuos, el compuesto multiespectral y el polígono de área afectada —
// si la misión se abrió con bounds.json todavía "preliminary" (solo ruta de
// vuelo), ninguno de los tres tenía datos para calcular su rango de color.
let liveMsBandIds=[...MS_BAND_IDS];

function ensurePane(id){
  if(!map.getPane('pane-'+id)){
    map.createPane('pane-'+id);
    layerOrder.push(id);
  }
  applyLayerOrder();
}
function registerIndexLayer(name){
  if(LAYER_REGISTRY[name])return false;
  const [label,desc]=INDEX_LABELS[name]||[name.toUpperCase(),''];
  const [lo,hi]=INDEX_RANGES[name];
  ensurePane(name);
  indexLayers[name]=new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14,opacity:.8,pane:'pane-'+name});
  RASTER_LAYER_FACTORY[name]=()=>new IndexGrid({indexName:name,maxZoom:21,maxNativeZoom:20,minZoom:14});
  LAYER_REGISTRY[name]={label,group:'indices',layer:indexLayers[name],defaultOn:false,defaultOpacity:.8,
    legend:()=>{const ramp=INDEX_PALETTE.colors.join(',');return `<p>${desc}</p>
      <div class="stat-row"><span class="lbl">Rango</span><span class="val cool">${lo.toFixed(2)} a ${hi.toFixed(2)}</span></div>
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
  LAYER_REGISTRY.ms_composite={label:'🎨 Multiespectral (compuesto)',group:'opticas',layer:msCompositeLayer,defaultOn:false,defaultOpacity:1,
    legend:()=>{
      const presetKey=Object.entries(MS_COMPOSITE_PRESETS).find(([,p])=>p.bands.join(',')===msCompositeBands.join(','))?.[0]||'custom';
      return `<p>Composición RGB armada en el navegador combinando 3 bandas espectrales crudas — no hay un archivo fijo por combinación, cambiar la selección recompone al vuelo.</p>
      <div class="band-picker">
        <label>Preset<select class="ms-composite-preset">
          ${Object.entries(MS_COMPOSITE_PRESETS).map(([k,p])=>`<option value="${k}"${presetKey===k?' selected':''}>${p.label}</option>`).join('')}
          <option value="custom"${presetKey==='custom'?' selected':''}>Personalizado</option>
        </select></label>
        <div class="ms-composite-custom" style="${presetKey==='custom'?'':'display:none'}">
          ${['R','G','B'].map((lbl,i)=>`<label>${lbl}<select class="ms-composite-sel" data-ch="${i}">
            ${liveMsBandIds.map(id=>`<option value="${id}"${msCompositeBands[i]===id?' selected':''}>${MS_BAND_LABELS[id]||id}</option>`).join('')}
          </select></label>`).join('')}
        </div>
      </div>`;}};
  return true;
}
async function tryLoadAreaAfectada(){
  if(LAYER_REGISTRY.area_afectada)return false;
  try{
    const r=await fetch('outputs/area_afectada.geojson?t='+Date.now(),{cache:'no-store'});
    if(!r.ok)return false;
    const geo=await r.json();
    ensurePane('area_afectada');
    loadAreaAfectada(geo);
    document.getElementById('area-edit-panel').classList.add('visible');
    LAYER_REGISTRY.area_afectada={label:'📐 Polígono área afectada',group:'impacto',layer:areaAfectadaLayer,defaultOn:true,defaultOpacity:1,
      legend:()=>`<p>Contorno detectado automáticamente (ver capa Severidad para la metodología), editable con el botón 📐 sobre el mapa. Referencia espacial de dónde se recortan severidad/hotspot — no reemplaza una verificación en terreno.</p>`};
    areaAfectadaLayer.addTo(map);
    return true;
  }catch(e){ return false; }
}

// Reintento de outputs/flight_path.geojson: el bloque de arriba lo intenta
// una sola vez, sync, al cargar la página — si el geovisor se abre en los
// pocos segundos entre "arrancó la corrida" y "export_flight_path.py terminó
// de escribir el archivo" (el caso normal: la webapp redirige acá apenas
// arranca el pipeline), esa lectura da 404 y la capa queda sin registrar para
// siempre, aunque el archivo aparezca 2 segundos después. Mismo patrón que
// tryLoadAreaAfectada().
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
    const SN={rgb:'RGB',thermal:'térmico',multispectral:'multiespectral'};
    const resumen=Object.entries(tally).map(([s,n])=>`${n} ${SN[s]||s}`).join(' · ');
    LAYER_REGISTRY.flight_path={label:'🛩️ Ruta de vuelo',group:'vuelo',layer:flightLayer,
      defaultOn:PRELIMINARY,defaultOpacity:1,
      legend:()=>`<p>Recorrido y posición de cada captura, según el GPS embebido en
        las fotos (${resumen}). Se genera antes de la reconstrucción, así que está
        disponible mientras el procesamiento sigue en curso.</p>`};
    if(PRELIMINARY)flightLayer.addTo(map);
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
    if(lastBoundsSig===null){
      lastBoundsSig=sig;
      let first={}; try{first=JSON.parse(sig);}catch(e){}
      if(!boundsWasReal&&first.center){map.setView(first.center,first.zoom||ZOOM);boundsWasReal=true;}
      return;
    }
    if(sig===lastBoundsSig)return;
    lastBoundsSig=sig;
    let b={};
    try{b=JSON.parse(sig);}catch(e){}
    if(!boundsWasReal&&b.center){map.setView(b.center,b.zoom||ZOOM);boundsWasReal=true;}
    if(b.thermal_range){THERMAL_MIN=b.thermal_range[0];THERMAL_MAX=b.thermal_range[1];}
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
    if(await tryLoadFlightPath())added=true;
    if(await tryLoadAreaAfectada())added=true;
    if(added)renderCapasPanel();
    // Redibuja TODAS las capas ráster ya registradas: sus tiles pueden haber
    // aparecido recién (RGB/térmico/hillshade/severidad/hotspot/clasificados
    // están registrados desde el arranque, solo esperaban esto).
    Object.values(LAYER_REGISTRY).forEach(d=>{ if(d.layer.redraw)d.layer.redraw(); });
    // Modo simple: situation.json aparece recién en la etapa de severidad
    // (bastante después que bounds.json cambie por primera vez) — se
    // reintenta cada vez que bounds.json cambia, no solo una vez al final.
    const prevSituation=SITUATION;
    await loadSituation();
    if(JSON.stringify(prevSituation)!==JSON.stringify(SITUATION)){
      await renderSituationHeader();
      await renderSummaryCards();
      renderSimpleTabs();
      renderMapLegend();
    }else if(added){
      renderSimpleTabs();
    }
  }catch(e){}
}

// ═══════════════════════════════════════════════════════════════════
// HUD DE PROGRESO — la webapp y el geovisor son UNA sola pantalla
// ═══════════════════════════════════════════════════════════════════
// Antes: arrancar una misión mostraba una pantalla de progreso aparte (en
// la webapp) y solo AL TERMINAR había un botón para pasar al geovisor. Acá
// el geovisor ES la pantalla de progreso: se abre apenas arranca la
// corrida (webapp/static/index.html navega directo a esta página con
// ?mission=<nombre>), y este bloque se conecta al MISMO endpoint SSE que
// antes consumía la webapp (/api/missions/<mision>/events) para llenar el
// HUD — sin reimplementar nada del lado del servidor.
//
// Si la misión del parámetro NO es la que el servidor tiene activa (p.ej.
// se abre el link de una misión ya vieja, en otra sesión), /events
// responde 404 y el HUD simplemente no se muestra — no hace falta
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
  if(!el)return;
  const live=phServerElapsed+(Date.now()-phServerElapsedAt)/1000;
  el.textContent=fmtElapsed(live);
}
function toggleProgressLog(){
  const log=document.getElementById('ph-log'),btn=document.getElementById('ph-log-toggle');
  const open=log.classList.toggle('open');
  btn.textContent=open?'▴ Ocultar log':'▾ Ver log';
  if(open)log.scrollTop=log.scrollHeight;
}
function phSetStage(text){
  const el=document.getElementById('ph-stage');
  if(el)el.textContent=text;
}
function phSetBar(pct){
  const el=document.getElementById('ph-bar-fill');
  if(el)el.style.width=Math.max(0,Math.min(100,pct))+'%';
}
const MAX_PH_LOG_LINES=600; // ventana acotada: una corrida entera son miles de líneas
let phLogLines=[];
function phAppendLog(line){
  phLogLines.push(line);
  if(phLogLines.length>MAX_PH_LOG_LINES)phLogLines=phLogLines.slice(-MAX_PH_LOG_LINES);
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
  };
  es.onmessage=(ev)=>{
    let d; try{d=JSON.parse(ev.data);}catch(e){return;}
    if(d.kind==='hello'){
      phServerElapsed=d.elapsed||0; phServerElapsedAt=Date.now();
      if(phTimerInterval)clearInterval(phTimerInterval);
      phTimerInterval=setInterval(phTick,1000); phTick();
      if(d.mode)phSetStage('Conectado — esperando la primera etapa…');
    }else if(d.kind==='progress'){
      if(d.event==='stage'){ phSetStage(`[${d.n}/${d.total}] ${d.name}`); phSetBar(0); }
      else if(d.event==='bar'){
        const cur=parseFloat(d.current||0), tot=parseFloat(d.total||100)||100;
        phSetBar((cur/tot)*100);
      }else if(d.event==='done'){ phSetBar(100); }
    }else if(d.kind==='log'){
      phAppendLog(d.line);
    }else if(d.kind==='done'){
      phDone=true;
      if(phTimerInterval)clearInterval(phTimerInterval);
      if(boundsPoll)clearInterval(boundsPoll);
      phServerElapsed=d.elapsed||phServerElapsed; phTick();
      const ok=d.returncode===0;
      hud.classList.add(ok?'done':'failed');
      phSetStage(ok?'✅ Procesamiento completo':`❌ Falló (código ${d.returncode})`);
      if(ok)phSetBar(100);
      const actions=document.getElementById('ph-actions');
      if(actions){
        actions.innerHTML='';
        if(!ok){
          const b=document.createElement('button');
          b.className='btn sm';b.textContent='Ver log completo';
          b.onclick=toggleProgressLog;
          actions.appendChild(b);
        }
        const close=document.createElement('button');
        close.className='btn sm primary';close.textContent='Cerrar';
        close.onclick=()=>hud.classList.remove('visible');
        actions.appendChild(close);
      }
      pollBoundsForChanges(); // última pasada: productos finales (severidad, área, etc.)
      es.close();
      // Recién ahora, con la corrida terminada, tiene sentido re-etiquetar
      // "productos de la misión" (antes decía cuántas misiones hay en la
      // lista, dato que no cambió por esto).
      labelMission();
    }
  };
  es.onerror=()=>{
    // Si el servidor nunca trackeó esta misión como activa (link viejo,
    // otra sesión), la primera respuesta ya viene con status 404 — no hay
    // "reintentos infinitos silenciosos": se cierra y no se muestra nada.
    if(!hud.classList.contains('visible')){ es.close(); if(boundsPoll)clearInterval(boundsPoll); }
  };
}
if(urlMission)connectLiveMission(urlMission);

// ═══════════════════════════════════════════════════════════════════
// TEMA CLARO / OSCURO
// ═══════════════════════════════════════════════════════════════════
// El oscuro es el default (ortofotos y mapas de calor se leen mejor sobre
// fondo oscuro), pero en campo —pantalla al sol— es directamente ilegible.
// Todo el color va por variables CSS, así que alcanza con marcar <html>.
const THEME_KEY='raptor-geovisor-theme';
function applyTheme(t){
  document.documentElement.setAttribute('data-theme',t);
  const ic=document.getElementById('theme-ic');
  if(ic)ic.textContent = t==='light' ? '☀️' : '🌙';
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
// ACCIONES SOBRE CAPAS (solo / encuadrar / apagar todo / restablecer)
// ═══════════════════════════════════════════════════════════════════
function soloLayer(id){
  const def=LAYER_REGISTRY[id];
  if(!def)return;
  // GLOBAL, no solo dentro del grupo: la primera versión aislaba nada más
  // que los hermanos del mismo grupo temático (p.ej. "solo" en RGB dejaba
  // el térmico prendido, porque vive en otro grupo) — el resultado visible
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
// garantizada — dibujar ese píxel en el mismo canvas que después se lee con
// toDataURL() "contamina" el canvas ENTERO (no solo ese tile) y el
// navegador tira SecurityError al exportar. Para no depender de que un
// tercero decida agregar CORS algún día, la exportación directamente NO
// toca el mapa base: compone solo las capas de DATOS (todas servidas por
// este mismo geovisor, mismo origen) sobre un fondo sólido, y lo dice en el
// pie de la imagen — resultado 100% predecible en vez de "a veces funciona
// según qué capa esté prendida".
//
// Los tiles de datos ya están en el DOM como <img>/<canvas> (Leaflet los
// mantiene ahí mientras la capa está activa) — se leen sus posiciones reales
// en pantalla con getBoundingClientRect() en vez de recalcular la matemática
// interna de teselado: más simple y no depende de la versión de Leaflet.
// captureMapSnapshot(): la composición del MAPA en sí (sin encabezado ni
// estadísticas) — devuelve el canvas crudo (no un dataURL) más sus medidas
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

    const isLight=document.documentElement.getAttribute('data-theme')==='light';
    ctx.fillStyle=isLight?'#eef1f5':'#0d1117';
    ctx.fillRect(0,0,rect.width,rect.height+PAD_BOTTOM);

    // Capas ráster: cualquier <img>/<canvas> dentro del pane de una capa
    // encendida, en el mismo orden en que se dibujan en el mapa real.
    let anyRaster=false;
    layerOrder.forEach(id=>{
      const def=LAYER_REGISTRY[id];
      if(!def||!map.hasLayer(def.layer))return;
      const pane=map.getPane('pane-'+id);
      if(!pane)return; // capas vectoriales (área/vuelo) se dibujan aparte, abajo
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
    // dibujan con las mismas primitivas de canvas — no dependen de leer DOM.
    if(map.hasLayer(areaAfectadaLayer)){
      areaPolyEntries.forEach(entry=>{
        ctx.beginPath();
        entry.rings.forEach(ring=>{
          ring.forEach((ll,i)=>{
            const p=map.latLngToContainerPoint(ll);
            if(i===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);
          });
          ctx.closePath();
        });
        ctx.fillStyle='rgba(255,23,68,.05)';ctx.fill();
        ctx.strokeStyle='#ff1744';ctx.lineWidth=2;ctx.stroke();
      });
    }
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

    // Escala: mismo cálculo conceptual que L.control.scale — distancia real
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
    const textCol=isLight?'#16202c':'#e6edf3';
    const sy=rect.height+18; // línea base de la barra de escala
    ctx.strokeStyle=textCol;ctx.fillStyle=textCol;ctx.lineWidth=2;
    ctx.beginPath();ctx.moveTo(20,sy);ctx.lineTo(20+barPx,sy);
    ctx.moveTo(20,sy-5);ctx.lineTo(20,sy+5);
    ctx.moveTo(20+barPx,sy-5);ctx.lineTo(20+barPx,sy+5);ctx.stroke();
    ctx.font='12px "Public Sans",sans-serif';ctx.textAlign='left';
    ctx.fillText(scaleLabel,20+barPx+8,sy+4);

    // Norte: la app no rota el mapa, así que siempre es "arriba".
    const nx=rect.width-34,ny=rect.height+8;
    ctx.beginPath();ctx.moveTo(nx,ny+22);ctx.lineTo(nx+9,ny);ctx.lineTo(nx+18,ny+22);ctx.closePath();
    ctx.fillStyle=textCol;ctx.fill();
    ctx.font='bold 12px "Public Sans",sans-serif';ctx.textAlign='center';
    ctx.fillStyle=isLight?'#eef1f5':'#0d1117';ctx.fillText('N',nx+9,ny+18);

    // Sin pie de misión/fecha ni nota de mapa base acá a propósito: quien
    // llama a esta función decide si hace falta encabezado (buildReportCanvas()
    // ya pone nombre+fecha arriba de todo; exportView(), el export suelto de
    // "modo operativo", no necesita ninguno — el nombre del archivo alcanza).
    const missionName=document.getElementById('incident-name')?.textContent||'';

    if(!anyRaster&&!map.hasLayer(areaAfectadaLayer)&&!map.hasLayer(flightLayer)){
      throw new Error('No hay ninguna capa visible para exportar. Activá al menos una capa en el panel.');
    }
    return {canvas,width,height,missionName};
  }
}

async function exportView(){
  const btn=document.getElementById('btn-export');
  const original=btn.innerHTML;
  btn.disabled=true;btn.innerHTML='⏳ Generando…';
  try{
    const {canvas,missionName}=await captureMapSnapshot();
    const url=canvas.toDataURL('image/png');
    const a=document.createElement('a');
    const safeMission=(missionName||'mapa').replace(/[^a-z0-9_-]+/gi,'_').slice(0,40);
    a.href=url;a.download=`${safeMission}_${new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')}.png`;
    a.click();
  }catch(err){
    alert('No se pudo exportar el mapa: '+err.message);
  }finally{
    btn.disabled=false;btn.innerHTML=original;
  }
}

// ═══════════════════════════════════════════════════════════════════
// IMAGEN DE "GENERAR RESUMEN DE SITUACIÓN" — una sola pieza para compartir
// con todo lo necesario para decidir: encabezado (misión+fecha+confianza),
// las mismas 3 métricas del panel, el mapa, y la recomendación en texto.
// No un recorte del mapa solo — ESE es exportView()/"Exportar" (modo
// operativo). Esta es la versión para compartir con quien no va a abrir el
// geovisor.
// ═══════════════════════════════════════════════════════════════════
function wrapCanvasText(ctx,text,x,y,maxWidth,lineHeight,maxLines){
  const words=text.split(' ');
  let line='',lines=[];
  for(const w of words){
    const test=line?line+' '+w:w;
    if(ctx.measureText(test).width>maxWidth&&line){ lines.push(line); line=w; }
    else line=test;
  }
  if(line)lines.push(line);
  if(maxLines&&lines.length>maxLines){
    lines=lines.slice(0,maxLines);
    lines[maxLines-1]=lines[maxLines-1].replace(/\s*\S*$/,'')+'…';
  }
  lines.forEach((l,i)=>ctx.fillText(l,x,y+i*lineHeight));
  return lines.length*lineHeight;
}
function buildRecommendationText(s){
  if(!s)return 'Esta misión no tiene datos de impacto (multiespectral+térmico) para resumir.';
  return `${s.hotspots_activos} foco${s.hotspots_activos===1?'':'s'} térmico${s.hotspots_activos===1?'':'s'} `+
    `activo${s.hotspots_activos===1?'':'s'}, severidad dominante ${s.severidad.dominante}. `+
    `Confianza del dato: ${s.confianza}.`+
    (s.severidad.severo_pct>0?' Se recomienda priorizar verificación en terreno en las zonas de severidad alta.':'');
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
  const mission=missionName||displayName(await missionReady)||'Situación del incendio';

  const cs=getComputedStyle(document.documentElement);
  const tok=n=>cs.getPropertyValue(n).trim();
  const ink=tok('--ink'),inkMuted=tok('--ink-muted'),surface=tok('--surface'),
    surface2=tok('--surface-2'),line=tok('--line'),accent=tok('--accent'),
    accentSoft=tok('--accent-soft'),onAccent=tok('--accent-ink'),
    good=tok('--good'),goodSoft=tok('--good-soft'),
    warning=tok('--warning'),warningSoft=tok('--warning-soft'),
    critical=tok('--critical'),criticalSoft=tok('--critical-soft');
  const F=(w,sz)=>`${w} ${sz}px "Public Sans",sans-serif`;

  const sevKey=s?.severidad?.dominante;
  const sevLabel={leve:'Leve',moderado:'Moderada',severo:'Severa'}[sevKey]||'—';
  const sevColor={leve:good,moderado:warning,severo:critical}[sevKey]||inkMuted;
  const sevSoft={leve:goodSoft,moderado:warningSoft,severo:criticalSoft}[sevKey]||surface2;
  // Urgencia general de la misión: 3 niveles, atados a lo mismo que YA se
  // muestra en las tarjetas de arriba (focos activos, severidad dominante)
  // — no un umbral aparte que pueda contradecirlas. Antes esto se disparaba
  // con CUALQUIER % de severidad "severo" mayor a cero (hasta un 1% por
  // ruido de clasificación ya lo activaba), así que podía decir "requiere
  // atención" en rojo con "0 focos activos" bien visible arriba: la propia
  // imagen se contradecía. Ahora el rojo queda reservado para lo que
  // realmente lo amerita — foco activo real, o que la severidad DOMINANTE
  // (no un resto minoritario) sea severa.
  const urgentLevel=!s?'none':s.hotspots_activos>0?'critical':sevKey==='severo'?'critical':sevKey==='moderado'?'warning':'good';
  const urgentLabel={critical:s?.hotspots_activos>0?'⚠ Riesgo de reactivación':'⚠ Requiere atención',
    warning:'◐ Seguimiento recomendado',good:'✓ Sin anomalías críticas',none:'Sin datos de impacto'}[urgentLevel];
  const urgentColor={critical,warning,good,none:inkMuted}[urgentLevel];
  const urgentSoft={critical:criticalSoft,warning:warningSoft,good:goodSoft,none:surface2}[urgentLevel];
  const confColor={alta:good,media:warning,baja:critical}[s?.confianza]||inkMuted;
  const confSoft={alta:goodSoft,media:warningSoft,baja:criticalSoft}[s?.confianza]||surface2;

  const dpr=Math.min(window.devicePixelRatio||1,2);
  const PAD=24, RADIUS=16, TOPBAR_H=6;
  const HEADER_H=88, STATS_H=112, FOOTER_H=s?92:56;
  const totalH=TOPBAR_H+HEADER_H+STATS_H+mapH+FOOTER_H;
  const out=document.createElement('canvas');
  out.width=Math.round(mapW*dpr);out.height=Math.round(totalH*dpr);
  const ctx=out.getContext('2d');
  ctx.scale(dpr,dpr);

  // Marco general redondeado — sin esto el PNG es un rectángulo a lo bruto,
  // se ve "hecho en dos minutos" apenas se comparte sobre cualquier fondo
  // que no sea blanco puro (un chat, una presentación).
  roundRectPath(ctx,0,0,mapW,totalH,RADIUS);
  ctx.clip();
  ctx.fillStyle=surface;ctx.fillRect(0,0,mapW,totalH);

  // Franja de acento arriba de todo — la única nota de color puramente
  // decorativa de la pieza, a propósito: ancla la identidad de la
  // herramienta sin competir con el semántico (severidad/confianza) que sí
  // significa algo.
  ctx.fillStyle=accent;ctx.fillRect(0,0,mapW,TOPBAR_H);

  // ── Encabezado ──────────────────────────────────────────────────────
  const headY=TOPBAR_H;
  ctx.fillStyle=accentSoft;ctx.fillRect(0,headY,mapW,HEADER_H);
  ctx.fillStyle=ink;ctx.font=F(700,23);ctx.textAlign='left';
  ctx.fillText(mission,PAD,headY+38);
  ctx.fillStyle=inkMuted;ctx.font=F(500,13.5);
  ctx.fillText(s?fmtFecha(s.captura):'Sin datos de impacto todavía',PAD,headY+60);
  if(s){
    // Confianza como píldora de color, no texto suelto — mismo lenguaje
    // visual que los "chips" del panel en vivo.
    ctx.font=F(700,12.5);
    const pillLbl=`Confianza ${s.confianza}`;
    const pillW=ctx.measureText(pillLbl).width+28;
    const pillX=mapW-PAD-pillW,pillY=headY+22;
    roundRectPath(ctx,pillX,pillY,pillW,26,13);
    ctx.fillStyle=confSoft;ctx.fill();
    ctx.fillStyle=confColor;ctx.textAlign='center';
    ctx.fillText(pillLbl,pillX+pillW/2,pillY+17);
  }

  // ── Fila de métricas: 3 tarjetas reales, no columnas separadas por líneas ──
  const statsY=headY+HEADER_H;
  ctx.fillStyle=surface;ctx.fillRect(0,statsY,mapW,STATS_H);
  const stats=s?[
    [`${s.area_ha}`,'ha','Área afectada',ink,surface2],
    [`${s.hotspots_activos}`,'','Focos activos',s.hotspots_activos>0?critical:ink,s.hotspots_activos>0?criticalSoft:surface2],
    [sevLabel,'','Severidad dominante',sevColor,sevSoft],
  ]:[['—','','Sin datos de impacto',inkMuted,surface2]];
  const gap=12, cardW=(mapW-PAD*2-gap*(stats.length-1))/stats.length, cardH=STATS_H-24;
  stats.forEach(([val,unit,lbl,color,soft],i)=>{
    const cx0=PAD+i*(cardW+gap), cy0=statsY+12;
    roundRectPath(ctx,cx0,cy0,cardW,cardH,12);
    ctx.fillStyle=soft;ctx.fill();
    // Marca de color: un pequeño acento redondo arriba-izquierda de la
    // tarjeta en vez de teñir todo el fondo con demasiada fuerza — visible
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

  // ── Pie: recomendación, coloreada según urgencia real — es lo último
  // que se lee pero lo primero que se PERCIBE (el color) al abrir la
  // imagen compartida. ──
  const footY=mapY+mapH;
  ctx.fillStyle=urgentSoft;ctx.fillRect(0,footY,mapW,FOOTER_H);
  ctx.fillStyle=urgentColor;ctx.fillRect(0,footY,4,FOOTER_H);
  ctx.font=F(600,13.5);ctx.textAlign='left';
  ctx.fillStyle=urgentColor;
  ctx.fillText(urgentLabel,PAD,footY+26);
  ctx.fillStyle=ink;ctx.font=F(400,13);
  wrapCanvasText(ctx,buildRecommendationText(s),PAD,footY+48,mapW-PAD*2,18,3);

  return {canvas:out,missionName:mission};
}

// ═══════════════════════════════════════════════════════════════════
// BUSCADOR DE CAPAS
// ═══════════════════════════════════════════════════════════════════
// Vive FUERA de #tab-content a propósito: ese contenedor se reescribe entero
// en cada renderCapasPanel(), y el input perdería foco y texto en cada
// cambio de opacidad.
// El elemento se busca DENTRO de la función, no vía un const de módulo:
// renderCapasPanel() —que la llama— corre en el arranque, antes de que este
// bloque se haya evaluado, y una referencia a un `const` todavía en zona
// muerta temporal tiraría ReferenceError rompiendo el panel entero.
function applyLayerFilter(){
  const searchInput=document.getElementById('layer-search');
  if(!searchInput)return;
  const q=searchInput.value.trim().toLowerCase();
  searchInput.parentElement.classList.toggle('has-text',!!q);
  let visibles=0;
  document.querySelectorAll('.layer-card').forEach(card=>{
    const hit=!q||(card.dataset.name||'').includes(q);
    card.classList.toggle('filtered-out',!hit);
    if(hit)visibles++;
  });
  document.querySelectorAll('.layer-group').forEach(g=>{
    const alguna=g.querySelector('.layer-card:not(.filtered-out)');
    g.classList.toggle('filtered-out',!alguna);
  });
  const cont=document.getElementById('tab-content');
  let vacio=cont.querySelector('.no-results');
  if(q&&visibles===0){
    if(!vacio){vacio=document.createElement('div');vacio.className='no-results';
      vacio.textContent='Ninguna capa coincide con la búsqueda.';cont.appendChild(vacio);}
  }else if(vacio)vacio.remove();
}
(function wireSearch(){
  const si=document.getElementById('layer-search');
  if(!si)return;
  si.addEventListener('input',applyLayerFilter);
  document.getElementById('search-clear').onclick=()=>{
    si.value='';applyLayerFilter();si.focus();
  };
})();

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
  if(typeof areaEditMode!=='undefined'&&areaEditMode)toggleAreaEdit();
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
  if(!measureActive)return;
  measurePts.push(e.latlng);
  const m=L.marker(e.latlng,{pane:'pane-measure',icon:L.divIcon({className:'measure-node',
    iconSize:[9,9],iconAnchor:[4.5,4.5]})}).addTo(map);
  measureMarks.push(m);
  refreshMeasure();
});
map.on('dblclick',e=>{ if(measureActive){L.DomEvent.stop(e);} });

// ═══════════════════════════════════════════════════════════════════
// ATAJOS DE TECLADO (un solo listener — había dos registrados por separado
// para 'b'/'c'/Escape, que se disparaban dos veces por tecla: apretar "C"
// abría y cerraba el comparador en el mismo evento, indistinguible de que
// el atajo no funcionara)
// ═══════════════════════════════════════════════════════════════════
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if(helpMenu&&helpMenu.classList.contains('open')){closeHelpMenu();return;}
    if(document.getElementById('report-overlay').classList.contains('open')){closeReport();return;}
    if(document.getElementById('point-card').classList.contains('visible')){closePointCard();return;}
    if(areaDrawingNew){cancelDrawNewArea();return;}
    if(measureActive){stopMeasure();return;}
  }
  if(e.target.matches('input,textarea,select'))return;
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  const k=e.key.toLowerCase();
  if(k==='t'){toggleTheme();}
  else if(k==='m'){toggleMeasure();}
  else if(k==='c'){toggleCompare();}
  else if(k==='b'){toggleSidebar();}
  else if(k==='/'){e.preventDefault();document.getElementById('layer-search')?.focus();}
});

// ═══════════════════════════════════════════════════════════════════
// MISIÓN ACTUAL — nombre resuelto una sola vez, todo lo demás lo espera
// ═══════════════════════════════════════════════════════════════════
// Si se llegó acá con ?mission= (arranque en vivo o "Ver geovisor" desde
// la webapp — el único flujo normal) se usa ese nombre directo, no depende
// de qué symlink esté activo en el servidor en este instante. Sin el
// parámetro (geovisor abierto suelto/recargado desde un bookmark viejo) se
// cae a la misión "activa" o, si no hay ninguna corriendo, la última con
// tiles — para no dejar la pantalla completamente huérfana.
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
  if(!mission){nameEl.textContent='Sin misión activa';return;}
  const hudVisible=document.getElementById('progress-hud')?.classList.contains('visible');
  nameEl.textContent=(hudVisible&&!phDone)?`${displayName(mission)} — procesando…`:displayName(mission);
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
let SITUATION=null;
async function loadSituation(){
  try{
    const r=await fetch('outputs/situation.json?t='+Date.now(),{cache:'no-store'});
    if(!r.ok){SITUATION=null;return null;}
    SITUATION=await r.json();
    return SITUATION;
  }catch(e){SITUATION=null;return null;}
}

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
  const zoneEl=document.getElementById('incident-zone');
  const ticksEl=document.getElementById('confidence-ticks');
  const freshEl=document.getElementById('freshness-text');
  const dateEl=document.getElementById('capture-date-label');
  const nivel=s?.confianza||'alta';
  if(ticksEl)ticksEl.setAttribute('data-level',nivel);
  if(dateEl)dateEl.textContent=s?.captura?fmtFechaCorta(s.captura):'—';
  if(freshEl){
    const captura=s?.captura?new Date(s.captura.replace(' ','T')):null;
    const minsAgo=captura&&!isNaN(captura)?Math.max(0,Math.round((Date.now()-captura)/60000)):null;
    const cuando=minsAgo===null?'':minsAgo<60?`hace ${minsAgo} min`:`hace ${Math.round(minsAgo/60)} h`;
    freshEl.textContent=s?`Actualizado ${cuando||'recién'} · Confianza ${nivel}`
                          :'Sin datos de severidad todavía';
  }
  if(zoneEl)zoneEl.textContent=s?`${s.area_ha??'—'} ha detectadas`:'';
}

// ── Tarjetas de resumen ejecutivo ────────────────────────────────────
function severityMiniBar(sev){
  if(!sev)return '';
  return `<div class="severity-mini" aria-hidden="true">
    <i style="width:${sev.leve_pct}%;background:var(--good)"></i>
    <i style="width:${sev.moderado_pct}%;background:var(--warning)"></i>
    <i style="width:${sev.severo_pct}%;background:var(--critical)"></i>
  </div>
  <div class="sub">${sev.leve_pct}% leve · ${sev.moderado_pct}% moderado · ${sev.severo_pct}% severo</div>`;
}
async function renderSummaryCards(){
  const grid=document.getElementById('summary-grid');
  if(!grid)return;
  const s=SITUATION||await loadSituation();
  if(!s){
    grid.innerHTML=`<div class="stat-card" style="grid-column:1/-1">
      <div class="l">Sin datos de impacto todavía</div>
      <div class="sub">Esta misión no tiene multiespectral+térmico, o la corrida no llegó a esa etapa.</div>
    </div>`;
    return;
  }
  const dom={leve:'Leve',moderado:'Moderada',severo:'Severa'}[s.severidad.dominante]||'—';
  grid.innerHTML=`
    <div class="stat-card">
      <div class="l">Área afectada</div>
      <div class="v tabnum">${s.area_ha} <small>ha</small></div>
      ${severityMiniBar(s.severidad)}
    </div>
    <div class="stat-card hotspots">
      <div class="l">Focos térmicos activos</div>
      <div class="v tabnum">${s.hotspots_activos}</div>
      <div class="sub">${s.hotspots_activos>0?'Riesgo de reactivación':'Ninguno detectado'}</div>
    </div>
    <div class="stat-card severidad">
      <div class="l">Severidad dominante</div>
      <div class="v" style="font-size:var(--fs-lg)">${dom}</div>
      <div class="sub">Del área afectada</div>
    </div>
    <div class="stat-card">
      <div class="l">Vegetación comprometida</div>
      <div class="v tabnum">${s.vegetacion_comprometida_pct??'—'}${s.vegetacion_comprometida_pct!=null?'<small>%</small>':''}</div>
      <!-- Aclarado a pedido: SÍ es del área afectada (fire_mask), no de toda
           la misión — ver compute_situation_summary.py, veg_pct se divide
           por n_fire (píxeles dentro del perímetro), no por el total de la
           ortofoto. El texto anterior ("del área analizada") no lo decía
           con claridad. -->
      <div class="sub">Del área afectada</div>
    </div>
    <div class="stat-card">
      <div class="l">Última captura</div>
      <div class="v" style="font-size:var(--fs-md)">${fmtFechaCorta(s.captura)}</div>
      <div class="sub">Dron UAV</div>
    </div>
    <div class="stat-card">
      <div class="l">Confianza del dato</div>
      <div class="v" style="font-size:var(--fs-md);display:flex;align-items:center;gap:8px;text-transform:capitalize">
        ${s.confianza} <span class="confidence-ticks" data-level="${s.confianza}" aria-hidden="true"><i></i><i></i><i></i></span>
      </div>
      <div class="sub">Cobertura de dato dentro del área</div>
    </div>`;
}

// ═══════════════════════════════════════════════════════════════════
// MODO SIMPLE — 3 pestañas (Impacto/Vegetación/Contexto) sobre el MISMO
// LAYER_REGISTRY que usa el modo operativo — remapeo de grupos técnicos a
// lenguaje llano, sin duplicar ninguna capa de Leaflet.
// ═══════════════════════════════════════════════════════════════════
const SIMPLE_TABS={
  impacto:{group:['impacto'],label:'Impacto'},
  vegetacion:{group:['indices'],label:'Vegetación'},
  contexto:{group:['opticas','termicas','terreno','vuelo'],label:'Contexto'},
};
let simpleActiveTab='impacto';

function simpleLegendChips(id){
  // Reusa la leyenda técnica ya escrita en LAYER_REGISTRY[id].legend() —
  // extrae solo los swatches de color con su etiqueta corta, si los hay.
  const html=LAYER_REGISTRY[id].legend();
  const tmp=document.createElement('div');tmp.innerHTML=html;
  const swatches=Array.from(tmp.querySelectorAll('[style*="background"]'))
    .filter(el=>el.style.width&&parseInt(el.style.width)<20);
  if(!swatches.length)return '';
  return `<div class="info-item-legend">${swatches.map(sw=>
    `<span class="chip"><span class="legend-swatch" style="background:${sw.style.background}"></span>${(sw.nextSibling?.textContent||sw.parentElement.textContent||'').trim().slice(0,28)}</span>`
  ).join('')}</div>`;
}
function simpleInfoItemHTML(id){
  const def=LAYER_REGISTRY[id];
  const on=map.hasLayer(def.layer);
  return `<div class="info-item" data-id="${id}">
    <div class="info-item-head">
      <button class="switch reset simple-layer-switch" data-id="${id}" role="switch"
        aria-checked="${on}" aria-label="Mostrar ${def.label.replace(/^\S+\s/,'')}"></button>
      <div class="info-item-name simple-details-toggle" data-id="${id}">
        <div class="n">${def.label.replace(/^\S+\s/,'')}</div>
      </div>
      <button class="details-toggle reset simple-details-toggle" data-id="${id}" aria-expanded="false"
        aria-label="Ver leyenda y detalles">▾</button>
    </div>
    <div class="info-item-details" data-id="${id}" style="display:none">
      ${simpleLegendChips(id)}
      <div class="info-item-foot"><span class="date">${SITUATION?.captura?fmtFechaCorta(SITUATION.captura):'Capturado en esta misión'}</span>
        <a class="simple-verdetalles" data-id="${id}">Ver detalles →</a></div>
    </div>
  </div>`;
}
function renderSimpleTabPanel(tabKey){
  const el=document.getElementById('panel-'+tabKey);
  if(!el)return;
  const groups=SIMPLE_TABS[tabKey].group;
  const ids=layerOrder.filter(id=>LAYER_REGISTRY[id]&&groups.includes(LAYER_REGISTRY[id].group)
    &&id!=='area_afectada'); // el polígono se edita desde su propio panel flotante, no acá
  if(!ids.length){
    el.innerHTML='<div class="empty-note">No hay información de este tipo en esta misión.</div>';
    return;
  }
  el.innerHTML=ids.map(simpleInfoItemHTML).join('');
  el.querySelectorAll('.simple-layer-switch').forEach(sw=>{
    sw.onclick=()=>{
      const id=sw.dataset.id,def=LAYER_REGISTRY[id];
      const on=sw.getAttribute('aria-checked')!=='true';
      sw.setAttribute('aria-checked',String(on));
      if(on)def.layer.addTo(map);else map.removeLayer(def.layer);
      renderMapLegend();
    };
  });
  el.querySelectorAll('.simple-details-toggle').forEach(t=>{
    t.onclick=()=>{
      const id=t.dataset.id;
      const body=el.querySelector(`.info-item-details[data-id="${id}"]`);
      const btn=el.querySelector(`.details-toggle[data-id="${id}"]`);
      const open=body.style.display==='none';
      body.style.display=open?'block':'none';
      btn.classList.toggle('open',open);
      btn.setAttribute('aria-expanded',String(open));
    };
  });
  el.querySelectorAll('.simple-verdetalles').forEach(a=>{
    a.onclick=()=>soloLayer(a.dataset.id);
  });
}
function renderSimpleTabs(){
  Object.keys(SIMPLE_TABS).forEach(renderSimpleTabPanel);
}
function selectSimpleTab(key){
  simpleActiveTab=key;
  Object.keys(SIMPLE_TABS).forEach(k=>{
    const tab=document.getElementById('tab-'+k);
    tab.setAttribute('aria-selected',String(k===key));
    document.getElementById('panel-'+k).classList.toggle('active',k===key);
  });
  renderMapLegend();
}
['impacto','vegetacion','contexto'].forEach((key,i,arr)=>{
  const tab=document.getElementById('tab-'+key);
  if(!tab)return;
  tab.onclick=()=>selectSimpleTab(key);
  tab.addEventListener('keydown',e=>{
    if(e.key==='ArrowRight')document.getElementById('tab-'+arr[(i+1)%arr.length]).click();
    if(e.key==='ArrowLeft')document.getElementById('tab-'+arr[(i-1+arr.length)%arr.length]).click();
  });
});

// ── Leyenda contextual flotante: la del primer layer visible de la
// pestaña activa, con interpretación accionable (ya viene en su legend()). ──
// Interpretación corta y ACCIONABLE por capa — lo que se lee de un vistazo
// en el mapa. El párrafo técnico completo (justificación estadística,
// fuente de los cortes) sigue existiendo en def.legend() para "modo
// operativo" — acá se reemplaza, no se agrega, porque un párrafo denso al
// lado del mapa es lo contrario de "accionable" cuando hay que decidir rápido.
const SIMPLE_HINTS={
  severidad:'Rojo: daño alto — priorizar verificación en terreno. Amarillo/naranja: revisar cuando se pueda. Verde: sin anomalía detectada.',
  hotspot_termico:'Rojo oscuro: foco activo (≥88°C) — riesgo de reactivación, requiere atención. Naranja/amarillo: temperatura elevada, monitorear.',
  ndvi_class:'Verde: vegetación densa y sana. Amarillo: escasa o estresada — vigilar evolución. Café: sin cobertura vegetal.',
  gndvi_class:'Verde: vegetación sana. Amarillo: estrés moderado. Rojo: estrés severo — posible daño por calor o falta de agua.',
  ndre_class:'Verde oscuro: óptimo. Verde claro: saludable. Naranja/rojo: deficiencia — atención en el corto plazo.',
  msavi2_class:'Verde: vegetación densa y sana. Amarillo: escasa o en regeneración temprana. Café: sin cobertura.',
  ndvi:'Verde = vegetación sana y densa. Rojo/café = suelo desnudo o vegetación muy estresada.',
  gndvi:'Verde = vegetación sana. Rojo = estrés severo, posible daño.',
  ndre:'Verde = follaje saludable. Rojo = deficiencia — atención en el corto plazo.',
  msavi2:'Verde = vegetación densa. Café = sin cobertura o suelo expuesto.',
  rgb:'Imagen a color real del vuelo — referencia visual directa del terreno.',
  ms_composite:'Composición de bandas espectrales — realza contrastes de vegetación no visibles a simple vista.',
  thermal:'Escala de temperatura de superficie — más caliente (colores cálidos) puede indicar actividad térmica residual.',
  hillshade:'Relieve del terreno — ayuda a ubicar pendientes y accesos, sin significado térmico ni de severidad.',
  flight_path:'Recorrido real del dron durante la captura — útil para verificar cobertura del vuelo.',
};
function renderMapLegend(){
  const box=document.getElementById('map-legend');
  if(!box)return;
  const groups=SIMPLE_TABS[simpleActiveTab].group;
  const visibleId=layerOrder.find(id=>LAYER_REGISTRY[id]&&groups.includes(LAYER_REGISTRY[id].group)
    &&id!=='area_afectada'&&map.hasLayer(LAYER_REGISTRY[id].layer));
  if(!visibleId){box.classList.remove('visible');box.innerHTML='';return;}
  const def=LAYER_REGISTRY[visibleId];
  // El HTML técnico de legend() trae uno o más <p> largos (la justificación
  // estadística) antes de la barra/clases de color — se descartan acá SOLO
  // para esta vista; siguen intactos en "Ver detalles" (modo operativo),
  // que llama a legend() directo sin pasar por acá.
  const tmp=document.createElement('div');
  tmp.innerHTML=def.legend();
  tmp.querySelectorAll('p').forEach(p=>p.remove());
  // El selector de bandas (RGB personalizado / compuesto multiespectral)
  // solo queda funcional cuando wireBandPickers() lo conecta — eso pasa al
  // renderizar "modo operativo" (#tab-content), nunca acá. Dejarlo en la
  // leyenda flotante sería un <select> que no hace nada al tocarlo.
  tmp.querySelectorAll('.band-picker').forEach(b=>b.remove());
  const hint=SIMPLE_HINTS[visibleId];
  box.innerHTML=`<h4>${def.label.replace(/^\S+\s/,'')}</h4>${tmp.innerHTML}`+
    (hint?`<div class="interpret"><b>Qué significa:</b> ${hint}</div>`:'');
  box.classList.add('visible');
}

// ═══════════════════════════════════════════════════════════════════
// FICHA "QUÉ SIGNIFICA ESTE PUNTO" — clic en el mapa (solo modo simple)
// ═══════════════════════════════════════════════════════════════════
function closePointCard(){document.getElementById('point-card').classList.remove('visible');}
async function showPointCard(latlng,containerPoint){
  const card=document.getElementById('point-card');
  const mission=await missionReady;
  if(!mission)return;
  card.innerHTML=`<div class="point-card-head"><h4>Consultando…</h4></div>`;
  card.style.left=Math.min(containerPoint.x+16,map.getSize().x-316)+'px';
  card.style.top=Math.max(8,Math.min(containerPoint.y-40,map.getSize().y-260))+'px';
  card.classList.add('visible');
  let d;
  try{
    const r=await fetch(`/api/missions/${encodeURIComponent(mission)}/sample?lat=${latlng.lat}&lon=${latlng.lng}`);
    d=await r.json();
  }catch(e){
    card.innerHTML=`<div class="point-card-head"><h4>Error</h4>
      <button class="point-card-close reset" onclick="closePointCard()">✕</button></div>
      <p style="padding:0 16px 16px;font-size:var(--fs-xs);color:var(--ink-muted)">No se pudo consultar este punto.</p>`;
    return;
  }
  if(!d.dentro_del_area){
    card.innerHTML=`<div class="point-card-head"><h4>Punto seleccionado</h4>
      <button class="point-card-close reset" onclick="closePointCard()">✕</button></div>
      <p style="padding:0 16px 16px;font-size:var(--fs-xs);color:var(--ink-muted);line-height:1.5">
      Este punto está fuera del área afectada detectada — no hay severidad ni foco térmico que reportar acá.</p>`;
    return;
  }
  const sevLabel={leve:'Leve',moderado:'Moderada',severo:'Severa'}[d.severidad]||'—';
  card.innerHTML=`
    <div class="point-card-head"><h4>Punto seleccionado</h4>
      <button class="point-card-close reset" onclick="closePointCard()">✕</button></div>
    <span class="point-severity ${d.severidad}">🔥 Severidad ${sevLabel.toLowerCase()}</span>
    <div class="point-metrics">
      <div class="point-metric"><div class="l">Temperatura</div><div class="v tabnum">${d.temperatura_c!=null?d.temperatura_c+' °C':'—'}</div></div>
      <div class="point-metric"><div class="l">Vegetación (NDVI)</div><div class="v tabnum">${d.ndvi!=null?d.ndvi:'—'}</div></div>
      <div class="point-metric"><div class="l">Fecha</div><div class="v" style="font-size:var(--fs-sm)">${fmtFechaCorta(d.captura)}</div></div>
      <div class="point-metric"><div class="l">Confianza</div><div class="v" style="font-size:var(--fs-sm);text-transform:capitalize">${d.confianza||'—'}</div></div>
    </div>
    ${d.recomendacion?`<div class="rec ${d.severidad}"><b>Recomendación:</b> ${d.recomendacion}</div>`:''}
  `;
}
map.on('click',e=>{
  // Solo en modo simple, y solo si no hay otra herramienta usando el clic
  // (medición, dibujo de área) — evita robarle el clic a esas herramientas.
  const advancedOpen=!document.getElementById('panel-advanced').hidden;
  if(advancedOpen||measureActive||areaDrawingNew)return;
  showPointCard(e.latlng,e.containerPoint);
});

// ═══════════════════════════════════════════════════════════════════
// VISTA DE LISTA — alternativa textual accesible al mapa
// ═══════════════════════════════════════════════════════════════════
async function renderListView(){
  const box=document.getElementById('list-view');
  const s=SITUATION||await loadSituation();
  if(!s){
    box.innerHTML=`<p style="color:var(--ink-muted);font-size:var(--fs-sm)">Sin datos de impacto todavía para listar.</p>`;
    return;
  }
  const rows=(s.hotspots||[]).map((h,i)=>`<tr>
      <td>Foco ${i+1}</td>
      <td class="tabnum">${h.lat.toFixed(5)}, ${h.lon.toFixed(5)}</td>
      <td><span class="badge ${h.severidad}">${{leve:'Leve',moderado:'Moderada',severo:'Severa'}[h.severidad]}</span></td>
      <td class="tabnum">${h.temp_c!=null?h.temp_c+' °C':'—'}</td>
      <td>${fmtFechaCorta(s.captura)}</td>
      <td>${h.severidad==='severo'?'Priorizar verificación en terreno':h.severidad==='moderado'?'Sumar a la ronda de verificación':'Monitorear'}</td>
    </tr>`).join('');
  box.innerHTML=`<table>
    <caption>Zonas críticas detectadas — ${fmtFecha(s.captura)}. Alternativa en texto al mapa.</caption>
    <thead><tr><th>Zona</th><th>Ubicación</th><th>Severidad</th><th>Temp.</th><th>Fecha</th><th>Recomendación</th></tr></thead>
    <tbody>${rows||'<tr><td colspan="6">No se detectaron focos térmicos activos.</td></tr>'}</tbody>
  </table>`;
}
document.getElementById('btn-view-map').onclick=function(){
  this.setAttribute('aria-pressed','true');
  document.getElementById('btn-view-list').setAttribute('aria-pressed','false');
  document.getElementById('list-view').classList.remove('active');
};
document.getElementById('btn-view-list').onclick=function(){
  this.setAttribute('aria-pressed','true');
  document.getElementById('btn-view-map').setAttribute('aria-pressed','false');
  document.getElementById('list-view').classList.add('active');
  renderListView();
};

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
    const thisDate=SITUATION?.captura?fmtFechaCorta(SITUATION.captura):'esta captura';
    const otherDates=related.map(m=>fmtFechaCorta(m.captura)).join(', ');
    document.getElementById('timebar-dates').innerHTML=`<b>${thisDate}</b> — comparar con: ${otherDates}`;
    document.getElementById('timebar-note').innerHTML=
      `Hay ${related.length} misión${related.length>1?'es':''} más capturada${related.length>1?'s':''} en esta misma
      zona (<b>${related.map(m=>displayName(m.name)).join(', ')}</b>). La comparación visual pixel a pixel entre
      misiones distintas todavía no está disponible — por ahora, abrí cada misión por separado desde el listado
      para comparar sus resúmenes de situación.`;
  }catch(e){timebar.classList.remove('enabled');}
}
document.getElementById('timebar-head').onclick=()=>document.getElementById('timebar').classList.toggle('open');

// ═══════════════════════════════════════════════════════════════════
// GENERAR RESUMEN DE SITUACIÓN
// ═══════════════════════════════════════════════════════════════════
function closeReport(){document.getElementById('report-overlay').classList.remove('open');}
async function openReport(){
  // El modal se abre YA, incondicionalmente — antes esto pasaba al FINAL,
  // después de escribir resumen/estadísticas: si algo ahí adentro fallaba
  // (dato faltante, fetch caído), el usuario veía el botón "sin hacer
  // nada" porque la línea que lo abría nunca se alcanzaba. Ahora un error
  // de datos se ve DENTRO del modal ya abierto, nunca lo bloquea.
  const overlay=document.getElementById('report-overlay');
  overlay.classList.add('open');
  document.getElementById('report-sub').textContent='Cargando…';
  document.getElementById('report-stats').innerHTML='';
  document.getElementById('report-text').textContent='';

  try{
    const s=SITUATION||await loadSituation();
    const mission=await missionReady;
    document.getElementById('report-sub').textContent=
      `${displayName(mission||'')} — ${s?fmtFecha(s.captura):'sin datos de impacto'}`;
    document.getElementById('report-stats').innerHTML=s?`
      <div class="card"><div class="v tabnum">${s.area_ha} ha</div><div class="l">Área afectada</div></div>
      <div class="card"><div class="v tabnum">${s.hotspots_activos}</div><div class="l">Focos activos</div></div>
      <div class="card"><div class="v" style="text-transform:capitalize">${s.severidad.dominante}</div><div class="l">Severidad</div></div>`
      : `<div class="card" style="grid-column:1/-1"><div class="l">Sin datos de impacto — esta misión no tiene multiespectral+térmico</div></div>`;
    document.getElementById('report-text').textContent=s
      ? `${s.hotspots_activos} foco${s.hotspots_activos===1?'':'s'} térmico${s.hotspots_activos===1?'':'s'} activo${s.hotspots_activos===1?'':'s'}, `+
        `severidad dominante ${s.severidad.dominante}. Confianza del dato: ${s.confianza}.`+
        (s.severidad.severo_pct>0?' Se recomienda priorizar verificación en terreno en las zonas de severidad alta.':'')
      : '';
  }catch(e){
    document.getElementById('report-sub').textContent='No se pudieron cargar los datos de la misión.';
  }

  // Vista previa: la imagen completa para compartir (encabezado + métricas
  // + mapa + recomendación en una sola pieza — ver buildReportCanvas()),
  // no solo el recorte del mapa. Es EXACTAMENTE lo que "Descargar imagen"
  // guarda, sin generarla dos veces.
  const preview=document.getElementById('report-preview');
  preview.innerHTML='<span style="font-size:var(--fs-xs);color:var(--ink-muted)">Generando…</span>';
  try{
    const {canvas}=await buildReportCanvas();
    const url=canvas.toDataURL('image/png');
    preview.innerHTML=`<img src="${url}" alt="Resumen de situación de la misión">`;
    preview.dataset.url=url;
  }catch(e){
    preview.innerHTML='<span style="font-size:var(--fs-xs);color:var(--ink-muted)">No se pudo generar la imagen — activá al menos una capa en el mapa.</span>';
    delete preview.dataset.url;
  }
}
document.getElementById('btn-report').onclick=openReport;
document.getElementById('report-close').onclick=closeReport;
document.getElementById('report-overlay').addEventListener('click',e=>{
  if(e.target.id==='report-overlay')closeReport();
});
document.getElementById('report-download').onclick=async()=>{
  const preview=document.getElementById('report-preview');
  let url=preview.dataset.url;
  if(!url){ try{ url=(await buildReportCanvas()).canvas.toDataURL('image/png'); }catch(e){ return; } }
  const a=document.createElement('a');
  a.href=url;a.download=`resumen_${(await missionReady)||'mision'}.png`;a.click();
};

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

// Modo operativo: el gestor de capas técnico de siempre, detrás de un link.
// No se reimplementa nada — layersPanelHTML()/wireLayersPanel() ya rendería
// exactamente esto en #tab-content, con o sin este toggle.
document.getElementById('link-advanced').onclick=(e)=>{
  e.preventDefault();
  document.getElementById('panel-simple').hidden=true;
  document.getElementById('panel-advanced').hidden=false;
};
document.getElementById('link-simple').onclick=(e)=>{
  e.preventDefault();
  document.getElementById('panel-advanced').hidden=true;
  document.getElementById('panel-simple').hidden=false;
};
// Los 3 botones de herramientas técnicas viven ahora dentro de "modo
// operativo" sin onclick inline (consistente con el resto de este bloque).
document.getElementById('btn-compare').onclick=toggleCompare;
document.getElementById('btn-measure').onclick=toggleMeasure;
document.getElementById('btn-export').onclick=exportView;

document.getElementById('panel-close').onclick=toggleSidebar;
document.getElementById('panel-toggle').onclick=toggleSidebar;

// Ajustes de visualización (transparencia global del modo simple)
document.getElementById('ajustes-toggle').onclick=function(){
  const open=this.getAttribute('aria-expanded')!=='true';
  this.setAttribute('aria-expanded',String(open));
  document.getElementById('ajustes-body').classList.toggle('open',open);
};
document.getElementById('global-opacity').addEventListener('input',function(){
  const v=this.value/100;
  SIMPLE_TABS[simpleActiveTab].group.forEach(g=>{
    layerOrder.filter(id=>LAYER_REGISTRY[id]&&LAYER_REGISTRY[id].group===g&&map.hasLayer(LAYER_REGISTRY[id].layer))
      .forEach(id=>{ if(LAYER_REGISTRY[id].layer.setOpacity)LAYER_REGISTRY[id].layer.setOpacity(v); });
  });
});

// ═══════════════════════════════════════════════════════════════════
// INIT del modo simple — corre una vez que el LAYER_REGISTRY inicial (y
// SITUATION, si existe) están listos.
// ═══════════════════════════════════════════════════════════════════
(async function initSimpleMode(){
  await loadSituation();
  await renderSituationHeader();
  await renderSummaryCards();
  renderSimpleTabs();
  renderMapLegend();
  await checkRelatedMissions();
})();
