<div align="center">

# 🌐 GeoKnight GIS — GFS Weather Map POC

**GIS · Spatial Data · AI Integration Engineering**

NOAA GFS 0.25° 2 m temperature forecast (f000–f048, every 3 h) over CONUS + Florida, pulled from NOAA Open Data on AWS and animated in MapLibre GL.

[![Live demo](https://img.shields.io/badge/live%20demo-GitHub%20Pages-d4af37?style=flat-square)](https://knightchrism.github.io/weather-poc/)
![Python](https://img.shields.io/badge/python-3-0b1220?style=flat-square&logo=python)
![MapLibre](https://img.shields.io/badge/MapLibre%20GL-4.7.1-0b1220?style=flat-square)
![Data](https://img.shields.io/badge/data-NOAA%20GFS-0b1220?style=flat-square)
![Status](https://img.shields.io/badge/status-proof%20of%20concept-d4af37?style=flat-square)

**▶ Live demo: https://knightchrism.github.io/weather-poc/**

![Screenshot](screenshot.png)

</div>

## How to run
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python pipeline.py            # ~20 s, ~13 MB downloaded via byte-range
python -m http.server 8000    # open http://localhost:8000
python shot.py                # optional headless screenshot (playwright; edit URL/port)
```

## Architecture

Static site + offline Python pipeline. No server-side code: `index.html` reads `data/gfs/manifest.json` and the pre-rendered PNG/JSON frames.

```
NOAA S3 (noaa-gfs-bdp-pds) ──.idx + byte-range GRIB2──▶ pipeline.py
   ──▶ COG GeoTIFF + PNG overlay + JSON grid + manifest.json + qc_report.json
   ──▶ index.html (MapLibre GL image source, time slider, click-to-read)
```

### Pipeline (`pipeline.py`, driven by `config.yaml`)
1. **Discover run** – newest cycle whose last forecast hour `.idx` exists in `s3://noaa-gfs-bdp-pds` (anonymous HTTPS).
2. **Ingest** – read each `.idx`, find `:TMP:2 m above ground:`, HTTP `Range` request for that GRIB2 message only (~0.8 MB instead of ~500 MB per file).
3. **Process** – cfgrib/xarray → subset extent → Cloud-Optimized GeoTIFF (°C, EPSG:4326, DEFLATE), Web-Mercator-resampled colored PNG overlay, 1° JSON grid (°F) for click-to-read.
4. **QC** – range check (−80..60 °C), NaN check, completeness of hours → `qc_report.json`; non-zero exit on failure.
5. **Manifest** – `data/<model>/manifest.json` (run time, frames, bounds) consumed by `index.html`.

Adding a model (HRRR, NAM, GEFS…) = add an entry under `models:` in `config.yaml` (bucket, key template, idx match string, hours).

## Possible production mapping on AWS (not implemented)
| POC piece | Production |
|---|---|
| cron / manual run | **EventBridge** rule (schedule) or SNS new-object notifications from the NOAA bucket (`NewGFSObject` topic) |
| `pipeline.py` | container on **ECS Fargate** (or AWS Batch / Step Functions fan-out per forecast hour) |
| `data/` | **S3** bucket of COGs + manifests, versioned by run, lifecycle rules for retention |
| PNG overlays | **TiTiler** (Lambda or Fargate) serving dynamic XYZ tiles / point queries straight from COGs |
| JSON grid | TiTiler `/point` endpoint (or tiny Lambda) |
| `qc_report.json` | CloudWatch metrics + alarms; failed QC blocks manifest promotion |
| static `index.html` | S3 + **CloudFront** (cache tiles, short TTL on manifest) |

## Roadmap (planned — not implemented)
- **HRRR** – 3 km CONUS model from `noaa-hrrr-bdp-pds`, as a second `models:` entry.
- **MRMS** – Multi-Radar/Multi-Sensor precipitation/reflectivity mosaics.
- **GOES** – GOES-East/West ABI satellite imagery.

## Limitations
- Single variable/model; PNG overlay rather than tiles (fine at CONUS scale, pixelated when zoomed in).
- Click-to-read uses a 1° coarse grid (nearest neighbor); COGs hold full 0.25° data.
- MapLibre and OpenFreeMap loaded from CDN; data is a static snapshot unless the pipeline is rerun.

## Credits
- Data: NOAA/NCEP Global Forecast System (GFS), via the [NOAA Open Data Dissemination](https://www.noaa.gov/information-technology/open-data-dissemination) program on AWS.
- Libraries: [MapLibre GL JS](https://maplibre.org/), [xarray](https://xarray.dev/), [cfgrib](https://github.com/ecmwf/cfgrib)/ecCodes, [rasterio](https://rasterio.readthedocs.io/) (GDAL), NumPy, Pillow, PyYAML, Requests; Playwright for screenshots.
- Basemap: [OpenFreeMap](https://openfreemap.org/) © OpenMapTiles © OpenStreetMap contributors.
- Built by Christopher Knight / GeoKnight GIS, with AI coding assistance.
