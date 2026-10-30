#!/usr/bin/env python3
"""GeoKnight GIS weather POC pipeline (config-driven).
For every layer in config.yaml: discover latest data on NOAA Open Data (anonymous HTTPS) -> read
(GRIB2 byte-range via .idx, gzipped GRIB2, or GOES ABI netCDF) -> reproject to a common EPSG:4326 grid
-> Cloud-Optimized GeoTIFF + Web-Mercator PNG overlay + coarse JSON grid -> QC -> manifest."""
import datetime as dt, gzip, json, math, os, re, sys, tempfile
import numpy as np, requests, yaml, xarray as xr, rasterio
from rasterio.transform import from_origin
from rasterio.warp import reproject, Resampling
from rasterio.crs import CRS
from PIL import Image

UTC = dt.timezone.utc

# ---------- discovery ----------
def find_run(m):
    now = dt.datetime.now(UTC)
    for d in range(3):
        date = (now - dt.timedelta(days=d)).strftime("%Y%m%d")
        for cyc in m["cycles"]:
            last = m["key_template"].format(date=date, cycle=cyc, fh=m["forecast_hours"]["end"])
            if requests.head(f'{m["bucket_url"]}/{last}.idx', timeout=20).ok: return date, cyc
    raise RuntimeError("no complete run found")

def s3_list(bucket_url, prefix, start_after=""):
    r = requests.get(bucket_url, params={"list-type": 2, "prefix": prefix, "start-after": start_after}, timeout=30); r.raise_for_status()
    return re.findall(r"<Key>([^<]+)</Key>", r.text)

def forecast_items(m):
    date, cyc = find_run(m); run = dt.datetime.strptime(date + cyc, "%Y%m%d%H").replace(tzinfo=UTC)
    fh = m["forecast_hours"]
    return run, [{"hour": h, "valid": run + dt.timedelta(hours=h), "key": m["key_template"].format(date=date, cycle=cyc, fh=h),
                  "id": f"f{h:03d}"} for h in range(fh["start"], fh["end"] + 1, fh["step"])]

def observed_items(m):
    """Latest N files from a time-stamped prefix, spaced >= spacing_min apart."""
    now = dt.datetime.now(UTC); keys = []
    for back in (3, 2, 1, 0):  # list the last few hours
        t = now - dt.timedelta(hours=back)
        pre = t.strftime(m["prefix_template"]); keys += s3_list(m["bucket_url"], pre)
    keys = [k for k in keys if re.search(m["key_regex"], k)]
    stamped = sorted({(dt.datetime.strptime(re.search(m["time_regex"], k).group(1), m["time_format"]).replace(tzinfo=UTC), k) for k in keys})
    picked = []
    for t, k in reversed(stamped):
        if not picked or (picked[-1][0] - t) >= dt.timedelta(minutes=m["spacing_min"]) - dt.timedelta(seconds=60): picked.append((t, k))
        if len(picked) == m["frames"]: break
    picked.reverse()
    if not picked: raise RuntimeError("no recent files found under " + m["prefix_template"])
    return picked[-1][0], [{"hour": None, "valid": t, "key": k, "id": t.strftime("%Y%m%dT%H%MZ")} for t, k in picked]

# ---------- readers: return (array, crs, transform, bytes_downloaded) ----------
def _tmp(content, suffix):
    fd, p = tempfile.mkstemp(suffix=suffix); os.write(fd, content); os.close(fd); return p

def _byte_range(m, key):
    lines = requests.get(f'{m["bucket_url"]}/{key}.idx', timeout=30).text.strip().splitlines()
    for i, l in enumerate(lines):
        if m["variable"]["match"] in l:
            return int(l.split(":")[1]), (int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else "")
    raise KeyError(m["variable"]["match"])

def read_grib_idx(m, key, ext):
    """GRIB2 message fetched with an HTTP Range request located via the .idx file; read with GDAL."""
    s, e = _byte_range(m, key)
    r = requests.get(f'{m["bucket_url"]}/{key}', headers={"Range": f"bytes={s}-{e}"}, timeout=120); r.raise_for_status()
    p = _tmp(r.content, ".grib2")
    if m.get("lon_0_360"):  # global 0..360 grid: cfgrib, subset, shift to -180..180
        ds = xr.open_dataset(p, engine="cfgrib", backend_kwargs={"indexpath": ""}); da = ds[m["variable"]["short"]].load(); ds.close()
        da = da.sel(longitude=slice(ext["west"] % 360 - 1, ext["east"] % 360 + 1), latitude=slice(ext["north"] + 1, ext["south"] - 1))
        lats, lons = da.latitude.values, np.where(da.longitude.values > 180, da.longitude.values - 360, da.longitude.values)
        res = abs(lats[0] - lats[1]); a = da.values.astype("float32")
        crs, tr = CRS.from_epsg(4326), from_origin(lons[0] - res / 2, lats[0] + res / 2, res, res)
    else:
        with rasterio.open(p) as d: a, crs, tr = d.read(1).astype("float32"), d.crs, d.transform
    os.remove(p); return a, crs, tr, len(r.content)

def read_grib_gz(m, key, ext):
    r = requests.get(f'{m["bucket_url"]}/{key}', timeout=120); r.raise_for_status()
    p = _tmp(gzip.decompress(r.content), ".grib2")
    with rasterio.open(p) as d: a, tr = d.read(1).astype("float32"), d.transform
    os.remove(p); return a, CRS.from_epsg(4326), tr, len(r.content)

def read_goes_nc(m, key, ext):
    """GOES-R ABI L2 CMIP netCDF on the geostationary fixed grid (scan angles in radians)."""
    r = requests.get(f'{m["bucket_url"]}/{key}', timeout=120); r.raise_for_status()
    p = _tmp(r.content, ".nc")
    with xr.open_dataset(p, engine="h5netcdf") as ds:
        pr = ds["goes_imager_projection"].attrs; h = float(pr["perspective_point_height"])
        x, y = ds["x"].values * h, ds["y"].values * h; a = ds[m["variable"]].values.astype("float32")
        crs = CRS.from_proj4(f'+proj=geos +h={h} +lon_0={float(pr["longitude_of_projection_origin"])} +sweep={pr["sweep_angle_axis"]} '
                             f'+a={float(pr["semi_major_axis"])} +b={float(pr["semi_minor_axis"])} +units=m +no_defs')
    os.remove(p); dx, dy = x[1] - x[0], y[0] - y[1]
    return a, crs, from_origin(x[0] - dx / 2, y[0] + dy / 2, dx, dy), len(r.content)

READERS = {"grib_idx": read_grib_idx, "grib_gz": read_grib_gz, "goes_nc": read_goes_nc}

# ---------- processing (shared by all layers) ----------
def to_grid(a, crs, tr, ext, res, m):
    a = np.where(np.isin(a, m.get("nodata_values", [])) | ~np.isfinite(a), np.nan, a)
    w, h = int(round((ext["east"] - ext["west"]) / res)), int(round((ext["north"] - ext["south"]) / res))
    out = np.full((h, w), np.nan, "float32"); otr = from_origin(ext["west"], ext["north"], res, res)
    reproject(a, out, src_transform=tr, src_crs=crs, src_nodata=np.nan, dst_transform=otr, dst_crs="EPSG:4326",
              dst_nodata=np.nan, resampling=Resampling[m.get("resampling", "bilinear")])
    return out * m.get("scale", 1) + m.get("offset", 0), otr  # physical units (e.g. degC, dBZ)

def colorize(v, stops, alpha, transparent_below):
    xs = [s[0] for s in stops]; out = np.zeros(v.shape + (4,), np.uint8)
    for c in range(3): out[..., c] = np.interp(v, xs, [s[1][c] for s in stops]).astype(np.uint8)
    show = np.isfinite(v) if transparent_below is None else (np.isfinite(v) & (v >= transparent_below))
    out[..., 3] = np.where(show, alpha, 0); return out

def merc_y(lat): return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

def to_png(v, ext, width, m, path):
    """Resample the regular lat/lon grid rows onto Web-Mercator spacing so MapLibre's image source lines up."""
    res = (ext["north"] - ext["south"]) / v.shape[0]
    h = int(width * (merc_y(ext["north"]) - merc_y(ext["south"])) / math.radians(ext["east"] - ext["west"]))
    rl = np.degrees(2 * np.arctan(np.exp(np.linspace(merc_y(ext["north"]), merc_y(ext["south"]), h))) - math.pi / 2)
    rows = np.clip(((ext["north"] - rl) / res).astype(int), 0, v.shape[0] - 1)
    cols = np.clip(np.round(np.linspace(0, v.shape[1] - 1, width)).astype(int), 0, v.shape[1] - 1)
    st = m["style"]; Image.fromarray(colorize(v[rows][:, cols], st["stops"], st.get("alpha", 190), st.get("transparent_below"))).save(path, optimize=True)

def to_cog(v, tr, path):
    with rasterio.open(path, "w", driver="COG", height=v.shape[0], width=v.shape[1], count=1, dtype="float32", crs="EPSG:4326",
                       transform=tr, compress="DEFLATE", predictor=3, nodata=np.nan) as d: d.write(np.round(v, 1).astype("float32"), 1)

def display(v, m): return v * m["style"].get("display_scale", 1) + m["style"].get("display_offset", 0)

def main(cfg_path="config.yaml", only=None):
    cfg = yaml.safe_load(open(cfg_path)); ext = cfg["extent"]
    qc_all = json.load(open("qc_report.json")) if only and os.path.exists("qc_report.json") else {}
    for m in cfg["models"]:
        if only and m["name"] != only: continue
        name = m["name"]; out = os.path.join(cfg["output_dir"], name); os.makedirs(out, exist_ok=True)
        for f in os.listdir(out): os.remove(os.path.join(out, f))
        try: run, items = (forecast_items if m["time"] == "forecast" else observed_items)(m)
        except Exception as ex:
            qc_all[name] = {"complete": False, "error": str(ex)}; print(f"{name}: FAILED discovery: {ex}"); continue
        print(f"{name}: {m['time']} reference {run.isoformat()} ({len(items)} frames)"); frames, qc_h, total = [], [], 0
        q = m["qc"]
        for it in items:
            try: a, crs, tr, n = READERS[m["reader"]](m, it["key"], ext); total += n
            except Exception as ex: qc_h.append({"frame": it["id"], "ok": False, "error": str(ex)}); print("  ", it["id"], ex); continue
            v, otr = to_grid(a, crs, tr, ext, m["grid_res_deg"], m); fid = it["id"]
            to_cog(v, otr, f"{out}/{fid}.tif"); to_png(v, ext, cfg["png_width"], m, f"{out}/{fid}.png")
            k = int(round(m["json_grid_step_deg"] / m["grid_res_deg"])); dv = display(v[k // 2::k, k // 2::k], m)
            json.dump({"lat0": ext["north"] - (k // 2 + .5) * m["grid_res_deg"], "lon0": ext["west"] + (k // 2 + .5) * m["grid_res_deg"],
                       "step": m["json_grid_step_deg"], "f": [[None if not np.isfinite(x) else round(float(x), 1) for x in row] for row in dv]},
                      open(f"{out}/{fid}.json", "w"), separators=(",", ":"))
            mn, mx = float(np.nanmin(v)), float(np.nanmax(v)); nanf = float(np.isnan(v).mean())
            ok = q["min"] <= mn and mx <= q["max"] and nanf <= q.get("max_nan_frac", 0)
            qc_h.append({"frame": fid, "ok": ok, "min": round(mn, 2), "max": round(mx, 2), "mean": round(float(np.nanmean(v)), 2),
                         "nan_frac": round(nanf, 4), "shape": list(v.shape), "source_key": it["key"]})
            frames.append({"hour": it["hour"], "valid": it["valid"].isoformat(), "png": f"data/{name}/{fid}.png", "json": f"data/{name}/{fid}.json",
                           "cog": f"data/{name}/{fid}.tif", "source": f'{m["bucket_url"]}/{it["key"]}'})
            print(f"  {fid} {mn:.1f}..{mx:.1f} {m['units']} nan={nanf:.3f} ok={ok}")
        done = sum(x["ok"] for x in qc_h)
        qc_all[name] = {"reference_time": run.isoformat(), "expected_frames": len(items), "ok_frames": done, "complete": done == len(items),
                        "bytes_downloaded": total, "range_limits": [q["min"], q["max"]], "units": m["units"], "frames": qc_h}
        json.dump({"model": name, "label": m["label"], "product": m["product"], "time": m["time"], "run": run.isoformat(),
                   "generated": dt.datetime.now(UTC).isoformat(), "units": m["style"]["units"], "stale_hours": m["stale_hours"],
                   "bounds": [ext["west"], ext["south"], ext["east"], ext["north"]], "frames": frames}, open(f"{out}/manifest.json", "w"), indent=1)
    json.dump(qc_all, open("qc_report.json", "w"), indent=1)
    # layer catalog for the front end (style + metadata straight from config)
    json.dump([{"name": m["name"], "label": m["label"], "short": m["short"], "product": m["product"], "time": m["time"],
                "units": m["style"]["units"], "stops": m["style"]["stops"], "ticks": m["style"]["ticks"],
                "click": m["style"].get("click", True), "credit": m["credit"]} for m in cfg["models"]],
              open(os.path.join(cfg["output_dir"], "layers.json"), "w"), indent=1)
    bad = [k for k, v in qc_all.items() if not v.get("complete")]
    if bad: sys.exit("QC FAILED: " + ", ".join(bad))
    print("QC passed")

if __name__ == "__main__": main(*sys.argv[1:])
