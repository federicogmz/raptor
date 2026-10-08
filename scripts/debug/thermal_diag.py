#!/usr/bin/env python3
"""Diagnóstico rápido del mosaico térmico (baja resolución).
Para cada celda de salida acumula: count, sum, sumsq de las temperaturas
proyectadas (sin pesos, sin bias). Exporta count/mean/std como PNG.
Objetivo: ver si los arcos están en la GEOMETRÍA (count) o en el
desacuerdo entre frames (std) → misregistro vs radiométrico."""
import os, glob, json, sys
import numpy as np
from osgeo import gdal
from pyproj import Transformer
import matplotlib; matplotlib.use("Agg")
import matplotlib.cm as cm, matplotlib.pyplot as plt

gdal.UseExceptions()
ODM_RGB   = "processing/rgb_odm"
ODM_TH    = "processing/thermal_odm"
THDIR     = "preprocessing/thermal_dji_sdk"
RES       = 1.0          # baja resolución → rápido
EDGE      = 60
UTM_EPSG  = 32618

def aa_to_R(aa):
    x,y,z=aa; a=np.sqrt(x*x+y*y+z*z)
    if a<1e-12: return np.eye(3)
    ax=np.array([x,y,z])/a; c,s=np.cos(a),np.sin(a); t=1-c; ux,uy,uz=ax
    return np.array([[c+ux*ux*t,ux*uy*t-uz*s,ux*uz*t+uy*s],
                     [uy*ux*t+uz*s,c+uy*uy*t,uy*uz*t-ux*s],
                     [uz*ux*t-uy*s,uz*uy*t+ux*s,c+uz*uz*t]])

cams=json.load(open(f"{ODM_TH}/cameras.json")); cm0=list(cams.values())[0]
w,h=cm0["width"],cm0["height"]; sc=max(w,h)
fx,fy=cm0["focal_x"]*sc,cm0["focal_y"]*sc
cx,cy=cm0["c_x"]*sc+w/2,cm0["c_y"]*sc+h/2
k1,k2,k3=cm0["k1"],cm0["k2"],cm0.get("k3",0); p1,p2=cm0["p1"],cm0["p2"]

def distort(x,y):
    r2=x*x+y*y; rad=1+k1*r2+k2*r2*r2+k3*r2**3
    xd=x*rad+2*p1*x*y+p2*(r2+2*x*x); yd=y*rad+p1*(r2+2*y*y)+2*p2*x*y
    return xd*fx+cx, yd*fy+cy

rec=json.load(open(f"{ODM_RGB}/opensfm/reconstruction.json"))
rec=rec[0] if isinstance(rec,list) else rec
ref=rec["reference_lla"]
tr=Transformer.from_crs("EPSG:4326",f"EPSG:{UTM_EPSG}",always_xy=True)
ref_e,ref_n=tr.transform(ref["longitude"],ref["latitude"])
poses={}
for sn,sh in rec["shots"].items():
    stem=sn.replace("_W.JPG","").replace("_V.JPG","").replace(".JPG","").replace(".tif","")
    R=aa_to_R(sh["rotation"]); t=np.array(sh["translation"])
    C=-R.T@t; poses[stem]={"R":R,"C":np.array([ref_e+C[0],ref_n+C[1],C[2]])}

th_files={}
for tf in glob.glob(f"{THDIR}/*.tif"):
    st=os.path.basename(tf).replace("_T.tif","")
    if st in poses: th_files[st]=tf

# aplicar offsets de leveling si existen (para medir std residual)
OFFS={}
if os.path.isfile("processing/thermal_offsets.json") and "--offsets" in sys.argv:
    OFFS=json.load(open("processing/thermal_offsets.json"))
    print("aplicando offsets de leveling al diagnóstico")

dsm_ds=gdal.Open(f"{ODM_RGB}/odm_dem/dsm.tif"); gt=dsm_ds.GetGeoTransform()
dW,dH=dsm_ds.RasterXSize,dsm_ds.RasterYSize
dsm=dsm_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
dsm[~np.isfinite(dsm)|(dsm<-500)]=np.nan; dmed=float(np.nanmedian(dsm))

x0=gt[0]-20; x1=gt[0]+dW*gt[1]+20; y1=gt[3]+20; y0=gt[3]+dH*gt[5]-20
W_=int((x1-x0)/RES); H_=int((y1-y0)/RES)
ogt=(x0,RES,0,y1,0,-RES)
cnt=np.zeros((H_,W_)); ssum=np.zeros((H_,W_)); ssq=np.zeros((H_,W_))
print(f"grid {W_}x{H_}, {len(th_files)} frames")

for i,st in enumerate(sorted(th_files)):
    P=poses[st]; R=P["R"]; cx0,cy0,cz0=P["C"]
    gsd=(12e-6/13.5e-3)*(cz0-dmed); half=max(w,h)*gsd*1.6
    c0=max(0,int((cx0-half-ogt[0])/ogt[1])); c1=min(W_,int((cx0+half-ogt[0])/ogt[1])+1)
    r0=max(0,int((cy0+half-ogt[3])/ogt[5])); r1=min(H_,int((cy0-half-ogt[3])/ogt[5])+1)
    if c0>=c1 or r0>=r1: continue
    _d=gdal.Open(th_files[st])
    if _d is None: continue
    _b=_d.GetRasterBand(1).ReadAsArray()
    if _b is None: continue
    th=_b.astype(np.float64); _d=None
    if st in OFFS: th=th+OFFS[st]
    cols=np.arange(c0,c1); rows=np.arange(r0,r1); CC,RR=np.meshgrid(cols,rows)
    ux=ogt[0]+(CC+.5)*ogt[1]; uy=ogt[3]+(RR+.5)*ogt[5]
    dc=np.clip(((ux-gt[0])/gt[1]).astype(int),0,dW-1); dr=np.clip(((uy-gt[3])/gt[5]).astype(int),0,dH-1)
    zz=dsm[dr,dc]; vd=np.isfinite(zz)
    dX,dY,dZ=ux-cx0,uy-cy0,zz-cz0
    Xc=R[0,0]*dX+R[0,1]*dY+R[0,2]*dZ; Yc=R[1,0]*dX+R[1,1]*dY+R[1,2]*dZ; Zc=R[2,0]*dX+R[2,1]*dY+R[2,2]*dZ
    val=vd&(Zc>0.01)
    if not val.any(): continue
    u,v=distort(Xc[val]/Zc[val],Yc[val]/Zc[val])
    ok=(u>=EDGE)&(u<w-EDGE)&(v>=EDGE)&(v<h-EDGE)
    if not ok.any(): continue
    vi=np.where(val); vr=vi[0][ok]; vc=vi[1][ok]
    uu=np.clip(u[ok].astype(int),0,w-1); vv=np.clip(v[ok].astype(int),0,h-1)
    T=th[vv,uu]; tok=np.isfinite(T)&(T>-50)&(T<200)
    orow=vr[tok]+r0; ocol=vc[tok]+c0; Tv=T[tok]
    np.add.at(cnt,(orow,ocol),1.0); np.add.at(ssum,(orow,ocol),Tv); np.add.at(ssq,(orow,ocol),Tv*Tv)
    if (i+1)%200==0: print(f"  {i+1}/{len(th_files)}")

m=cnt>0
mean=np.where(m,ssum/np.maximum(cnt,1),np.nan)
var=np.where(cnt>1,ssq/np.maximum(cnt,1)-mean**2,0); std=np.sqrt(np.maximum(var,0))
def save(arr,name,cmap,pmask,lo=None,hi=None):
    a=arr.copy()
    if lo is None: lo,hi=np.nanpercentile(a[pmask],[2,98])
    n=np.clip((a-lo)/(hi-lo),0,1); rgba=(cm.get_cmap(cmap)(n)*255).astype(np.uint8)
    rgba[~pmask]=[40,40,40,255]; plt.imsave(os.path.expanduser(f"~/{name}"),rgba)
save(cnt,"diag_count.png","viridis",m,0,np.percentile(cnt[m],98))
save(std,"diag_std.png","magma",m,0,np.percentile(std[m],98))
save(mean,"diag_mean.png","inferno",m)
print("count: max %d, mean %.1f | std: mediana %.2f, p95 %.2f"%(cnt[m].max(),cnt[m].mean(),np.median(std[m]),np.percentile(std[m],95)))
print("listo: ~/diag_count.png ~/diag_std.png ~/diag_mean.png")
