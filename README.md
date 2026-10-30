# GeoKnight GIS – Weather Map Proof of Concept

GFS 0.25° 2 m temperature, f000–f048 every 3 h, over CONUS + Florida, animated in MapLibre GL.

## Run
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python pipeline.py            # ~20 s, ~13 MB downloaded via byte-range
python -m http.server 8000    # open http://localhost:8000
python shot.py                # optional headless screenshot (playwright; edit URL/port)
```

## Pipeline (`pipeline.py`, driven by `config.yaml`)
1. **Discover run** – newest cycle whose last forecast hour `.idx` exists in `s3://noaa-gfs-bdp-pds` (anonymous HTTPS).
2. **Ingest** – read each `.idx`, find `:TMP:2 m above ground:`, HTTP `Range` request for that GRIB2 message only (~0.8 MB instead of ~500 MB per file).
3. **Process** – cfgrib/xarray → subset extent → Cloud-Optimized GeoTIFF (°C, EPSG:4326, DEFLATE), Web-Mercator-resampled colored PNG overlay, 1° JSON grid (°F) for click-to-read.
4. **QC** – range check (−80..60 °C), NaN check, completeness of hours → `qc_report.json`; non-zero exit on failure.
5. **Manifest** – `data/<model>/manifest.json` (run time, frames, bounds) consumed by `index.html`.

Adding a model (HRRR, NAM, GEFS…) = add an entry under `models:` in `config.yaml` (bucket, key template, idx match string, hours).

## Production mapping on AWS
| POC piece | Production |
|---|---|
| cron / manual run | **EventBridge** rule (schedule) or SNS new-object notifications from the NOAA bucket (`NewGFSObject` topic) |
| `pipeline.py` | container on **ECS Fargate** (or AWS Batch / Step Functions fan-out per forecast hour) |
| `data/` | **S3** bucket of COGs + manifests, versioned by run, lifecycle rules for retention |
| PNG overlays | **TiTiler** (Lambda or Fargate) serving dynamic XYZ tiles / point queries straight from COGs |
| JSON grid | TiTiler `/point` endpoint (or tiny Lambda) |
| `qc_report.json` | CloudWatch metrics + alarms; failed QC blocks manifest promotion |
| static `index.html` | S3 + **CloudFront** (cache tiles, short TTL on manifest) |

## Limitations
- Single variable/model; PNG overlay rather than tiles (fine at CONUS scale, pixelated when zoomed in).
- Click-to-read uses a 1° coarse grid (nearest neighbor); COGs hold full 0.25° data.
- MapLibre and OpenFreeMap loaded from CDN; data is a static snapshot unless the pipeline is rerun.
