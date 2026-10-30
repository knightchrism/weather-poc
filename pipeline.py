#!/usr/bin/env python3
"""GeoKnight GIS weather POC pipeline: ingest (byte-range GRIB2) -> COG + PNG + JSON grid -> QC."""
import datetime as dt, json, math, os, sys, tempfile
import numpy as np, requests, yaml, xarray as xr, rasterio
from rasterio.transform import from_origin
from PIL import Image

STOPS_F = [(-20,(145,0,165)),(0,(60,40,200)),(20,(40,120,240)),(32,(120,200,255)),(45,(60,190,110)),
           (60,(240,230,60)),(75,(250,150,40)),(90,(220,40,30)),(110,(130,0,20))]

def colorize(f):
    xs=[s[0] for s in STOPS_F]; out=np.zeros(f.shape+(4,),np.uint8)
    for c in range(3): out[...,c]=np.interp(f,xs,[s[1][c] for s in STOPS_F]).astype(np.uint8)
    out[...,3]=np.where(np.isfinite(f),190,0); return out

def find_run(m):
    now=dt.datetime.now(dt.timezone.utc)
    for d in range(3):
        date=(now-dt.timedelta(days=d)).strftime("%Y%m%d")
        for cyc in m["cycles"]:
            last=m["key_template"].format(date=date,cycle=cyc,fh=m["forecast_hours"]["end"])
            if requests.head(f'{m["bucket_url"]}/{last}.idx',timeout=20).ok: return date,cyc
    sys.exit("no complete run found")

def byte_range(m,key):
    lines=requests.get(f'{m["bucket_url"]}/{key}.idx',timeout=30).text.strip().splitlines()
    for i,l in enumerate(lines):
        if m["variable"]["match"] in l:
            start=int(l.split(":")[1]); end=int(lines[i+1].split(":")[1])-1 if i+1<len(lines) else ""
            return start,end
    raise KeyError(m["variable"]["match"])

def fetch(m,key):
    s,e=byte_range(m,key)
    r=requests.get(f'{m["bucket_url"]}/{key}',headers={"Range":f"bytes={s}-{e}"},timeout=60); r.raise_for_status()
    fd,p=tempfile.mkstemp(suffix=".grib2"); os.write(fd,r.content); os.close(fd)
    ds=xr.open_dataset(p,engine="cfgrib",backend_kwargs={"indexpath":""}); da=ds[m["variable"]["short"]].load(); ds.close(); os.remove(p)
    return da, len(r.content)

def subset(da,ext,lon360):
    w,e=(ext["west"]%360,ext["east"]%360) if lon360 else (ext["west"],ext["east"])
    da=da.sel(longitude=slice(w,e),latitude=slice(ext["north"],ext["south"]))
    lons=da.longitude.values; lons=np.where(lons>180,lons-360,lons)
    return da.values-273.15, da.latitude.values, lons

def merc_y(lat): return math.log(math.tan(math.pi/4+math.radians(lat)/2))

def to_png(c,lats,ext,width,path):
    f=c*9/5+32; h=int(width*(merc_y(ext["north"])-merc_y(ext["south"]))/math.radians(ext["east"]-ext["west"]))
    ys=np.linspace(merc_y(ext["north"]),merc_y(ext["south"]),h); rl=np.degrees(2*np.arctan(np.exp(ys))-math.pi/2)
    rows=np.clip(np.round((lats[0]-rl)/(lats[0]-lats[1])).astype(int),0,len(lats)-1)
    cols=np.clip(np.round(np.linspace(0,c.shape[1]-1,width)).astype(int),0,c.shape[1]-1)
    Image.fromarray(colorize(f[rows][:,cols])).save(path,optimize=True)

def to_cog(c,lats,lons,path):
    res=abs(lats[0]-lats[1]); tr=from_origin(lons[0]-res/2,lats[0]+res/2,res,res)
    with rasterio.open(path,"w",driver="COG",height=c.shape[0],width=c.shape[1],count=1,dtype="float32",
                       crs="EPSG:4326",transform=tr,compress="DEFLATE",nodata=np.nan) as d: d.write(c.astype("float32"),1)

def main(cfg_path="config.yaml"):
    cfg=yaml.safe_load(open(cfg_path)); ext=cfg["extent"]; qc_all={}
    for m in cfg["models"]:
        out=os.path.join(cfg["output_dir"],m["name"]); os.makedirs(out,exist_ok=True)
        date,cyc=find_run(m); run=dt.datetime.strptime(date+cyc,"%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
        fh=m["forecast_hours"]; hours=list(range(fh["start"],fh["end"]+1,fh["step"]))
        print(f'{m["name"]}: run {run.isoformat()}'); frames=[]; qc_h=[]; total=0
        for h in hours:
            key=m["key_template"].format(date=date,cycle=cyc,fh=h)
            try: da,n=fetch(m,key); total+=n
            except Exception as ex: qc_h.append({"hour":h,"ok":False,"error":str(ex)}); continue
            c,lats,lons=subset(da,ext,m.get("lon_0_360"))
            to_cog(c,lats,lons,f"{out}/f{h:03d}.tif"); to_png(c,lats,ext,cfg["png_width"],f"{out}/f{h:03d}.png")
            st=cfg["json_grid_step_deg"]; k=int(round(st/abs(lats[0]-lats[1])))
            json.dump({"lat0":float(lats[0]),"lon0":float(lons[0]),"step":st,
                       "f":np.round(c[::k,::k]*9/5+32,1).tolist()},open(f"{out}/f{h:03d}.json","w"),separators=(",",":"))
            mn,mx=float(np.nanmin(c)),float(np.nanmax(c)); nan=int(np.isnan(c).sum())
            ok=cfg["qc"]["min_c"]<=mn and mx<=cfg["qc"]["max_c"] and nan==0
            qc_h.append({"hour":h,"ok":ok,"min_c":round(mn,2),"max_c":round(mx,2),"mean_c":round(float(np.nanmean(c)),2),"nan_cells":nan,"shape":list(c.shape)})
            frames.append({"hour":h,"valid":(run+dt.timedelta(hours=h)).isoformat(),"png":f"data/{m['name']}/f{h:03d}.png","json":f"data/{m['name']}/f{h:03d}.json","cog":f"data/{m['name']}/f{h:03d}.tif"})
            print(f"  f{h:03d} {mn:.1f}..{mx:.1f}C ok={ok}")
        done=sum(q["ok"] for q in qc_h)
        qc_all[m["name"]]={"run":run.isoformat(),"expected_hours":len(hours),"ok_hours":done,"complete":done==len(hours),
                           "bytes_downloaded":total,"range_limits_c":[cfg["qc"]["min_c"],cfg["qc"]["max_c"]],"hours":qc_h}
        json.dump({"model":m["name"],"label":m["label"],"run":run.isoformat(),"generated":dt.datetime.now(dt.timezone.utc).isoformat(),
                   "bounds":[ext["west"],ext["south"],ext["east"],ext["north"]],"frames":frames},open(f"{out}/manifest.json","w"),indent=1)
    json.dump(qc_all,open("qc_report.json","w"),indent=1)
    if not all(v["complete"] for v in qc_all.values()): sys.exit("QC FAILED")
    print("QC passed")

if __name__=="__main__": main(*sys.argv[1:])
