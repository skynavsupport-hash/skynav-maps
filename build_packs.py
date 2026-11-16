#!/usr/bin/env python3
"""
SkyNav region packs: builds one map file + one terrain file per region, so the app can download a
whole flying area in one go (seconds instead of many minutes of single tiles).

    python build_packs.py                 build every region in regions.json
    python build_packs.py --only bir-billing
    python build_packs.py --rebuild      fresh OpenStreetMap data (otherwise work/india.pmtiles is reused)
    python build_packs.py upload-r2       upload the packs to Cloudflare R2 (bucket skynav-maps)
    python build_packs.py index --base-url https://github.com/<you>/<repo>/releases/download/maps-v1/

Needs: Python 3.9+, `pip install pmtiles`, and Java 21+ (the one inside Android Studio is found
automatically). Map data © OpenStreetMap contributors (ODbL); elevation: Mapzen/AWS Terrain Tiles.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TOOLS = HERE / "tools"
WORK = HERE / "work"            # Planetiler downloads (~3 GB the first time) and temp files
PLANETILER_URL = "https://github.com/onthegomap/planetiler/releases/latest/download/planetiler.jar"
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
DEM_MIN_ZOOM, DEM_MAX_ZOOM = 6, 12   # z12 is what the app uses for AGL and for the relief shading
BUCKET = "skynav-maps"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- Java + Planetiler

def find_java():
    """java on PATH if it's 21+, else the JetBrains Runtime bundled with Android Studio."""
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / "java")
    which = shutil.which("java")
    if which:
        candidates.append(Path(which))
    if platform.system() == "Windows":
        for base in [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")]:
            if base:
                candidates += list(Path(base).glob("Android/Android Studio*/jbr/bin/java.exe"))
                candidates += list(Path(base).glob("Programs/Android Studio*/jbr/bin/java.exe"))
                candidates += list(Path(base).glob("Eclipse Adoptium/jdk-2*/bin/java.exe"))
    elif platform.system() == "Darwin":
        candidates += list(Path("/Applications").glob("Android Studio*.app/Contents/jbr/Contents/Home/bin/java"))
    else:
        candidates += list(Path.home().glob("android-studio*/jbr/bin/java")) + list(Path("/opt").glob("android-studio*/jbr/bin/java"))
    for c in candidates:
        try:
            out = subprocess.run([str(c), "-version"], capture_output=True, text=True).stderr
            ver = out.split('"')[1] if '"' in out else ""
            major = int(ver.split(".")[0]) if ver else 0
            if major >= 21:
                return str(c)
        except Exception:
            pass
    sys.exit("Java 21 or newer not found. Install Android Studio (it includes one) or Temurin 21 from adoptium.net.")


def planetiler_jar():
    TOOLS.mkdir(exist_ok=True)
    jar = TOOLS / "planetiler.jar"
    if not jar.exists():
        log("Downloading Planetiler (~95 MB)...")
        download(PLANETILER_URL, jar)
    return jar


def download(url, dest: Path, tries=20):
    """Download with retries; an interrupted download continues where it stopped (HTTP Range)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, tries + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": "SkyNav pack builder"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                if have and r.status != 206:
                    have = 0  # server ignored the range: start over
                total = have + int(r.headers.get("Content-Length") or 0)
                last = time.time()
                with open(tmp, "ab" if have else "wb") as f:
                    while True:
                        buf = r.read(1 << 20)
                        if not buf:
                            break
                        f.write(buf)
                        have += len(buf)
                        if time.time() - last > 5 and total:
                            log(f"  {have / 1e6:.0f} / {total / 1e6:.0f} MB")
                            last = time.time()
            tmp.replace(dest)
            return
        except Exception as e:
            if attempt == tries:
                raise SystemExit(f"Download failed: {url}\n  {e}\nCheck your internet and run the same command again - it continues where it stopped.")
            log(f"  connection problem ({e.__class__.__name__}), retrying in {min(60, attempt * 5)} s...")
            time.sleep(min(60, attempt * 5))


def build_country_map(java, area, bbox, ram_gb, rebuild=False):
    """OpenMapTiles-schema vector tiles z0-14 for the whole country, built once with Planetiler.
    Every pack is then cut out of this file in seconds (pmtiles extract)."""
    WORK.mkdir(exist_ok=True)
    out = WORK / f"{area}.pmtiles"
    if out.exists() and not rebuild:
        log(f"Using the existing {out.name} (add --rebuild to make it again with fresh map data)")
        return out
    w, s, e, n = bbox
    cmd = [
        java, f"-Xmx{ram_gb}g", "-jar", str(planetiler_jar()),
        "--download", f"--area={area}",
        f"--bounds={w},{s},{e},{n}",
        f"--output={out}", "--force",
        f"--download_dir={WORK / 'sources'}", f"--tmpdir={WORK / 'tmp'}",
        "--maxzoom=14",
        "--http_retries=20", "--http_timeout=120s",
    ]
    log(f"Map: running Planetiler for '{area}' (downloads OpenStreetMap data, a few GB, the first time)...")
    subprocess.run(cmd, check=True, cwd=WORK)
    return out


def pmtiles_cli():
    """The 'pmtiles' command-line tool (protomaps/go-pmtiles), downloaded once."""
    TOOLS.mkdir(exist_ok=True)
    exe = TOOLS / ("pmtiles.exe" if platform.system() == "Windows" else "pmtiles")
    if exe.exists():
        return exe
    system = {"Windows": "windows", "Darwin": "darwin"}.get(platform.system(), "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    headers = {"User-Agent": "SkyNav pack builder", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    req = urllib.request.Request("https://api.github.com/repos/protomaps/go-pmtiles/releases/latest", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        rel = json.load(r)
    asset = next((a for a in rel["assets"]
                  if system in a["name"].lower() and arch in a["name"].lower()
                  and a["name"].lower().endswith((".zip", ".tar.gz"))), None)
    if not asset:
        sys.exit("Couldn't find the pmtiles tool for this computer on github.com/protomaps/go-pmtiles/releases")
    arc = TOOLS / asset["name"]
    log(f"Downloading the pmtiles tool ({asset['name']})...")
    download(asset["browser_download_url"], arc)
    import tarfile, zipfile
    if arc.name.endswith(".zip"):
        with zipfile.ZipFile(arc) as z:
            name = next(n for n in z.namelist() if n.split("/")[-1] in ("pmtiles", "pmtiles.exe"))
            exe.write_bytes(z.read(name))
    else:
        with tarfile.open(arc) as t:
            m = next(m for m in t.getmembers() if m.name.split("/")[-1] == "pmtiles")
            exe.write_bytes(t.extractfile(m).read())
    exe.chmod(0o755)
    arc.unlink()
    return exe


def extract(src: Path, dst: Path, bbox):
    w, s, e, n = bbox
    OUT.mkdir(exist_ok=True)
    subprocess.run([str(pmtiles_cli()), "extract", str(src), str(dst), f"--bbox={w},{s},{e},{n}"],
                   check=True, stdout=subprocess.DEVNULL)


# ---------------------------------------------------------------- terrain (DEM) pack

def tiles_in_bbox(bbox, z):
    import math
    w, s, e, n = bbox
    def xy(lat, lon):
        x = int((lon + 180) / 360 * (1 << z))
        r = math.radians(lat)
        y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z))
        return x, y
    x0, y0 = xy(n, w)
    x1, y1 = xy(s, e)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def fetch(z, x, y, tries=4):
    url = DEM_URL.format(z=z, x=x, y=y)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "SkyNav pack builder"}), timeout=30) as r:
                return (z, x, y, r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i * 2)


def build_dem(region):
    from pmtiles.tile import zxy_to_tileid, TileType, Compression
    from pmtiles.writer import Writer

    tiles = [t for z in range(DEM_MIN_ZOOM, DEM_MAX_ZOOM + 1) for t in tiles_in_bbox(region["bbox"], z)]
    log(f"Terrain {region['id']}: {len(tiles)} elevation tiles...")
    cache = WORK / "dem"
    def path(z, x, y):
        return cache / str(z) / str(x) / f"{y}.png"
    todo = [t for t in tiles if not path(*t).exists()]
    done = 0
    with cf.ThreadPoolExecutor(32) as ex:
        for z, x, y, data in ex.map(lambda t: fetch(*t), todo):
            f = path(z, x, y)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            done += 1
            if done % 1000 == 0:
                log(f"  {done}/{len(todo)} downloaded")

    OUT.mkdir(exist_ok=True)
    out = OUT / f"{region['id']}-dem.pmtiles"
    w, s, e, n = region["bbox"]
    with open(out, "wb") as f:
        wr = Writer(f)
        for tid, k in sorted((zxy_to_tileid(*k), k) for k in tiles):   # tiles are read one at a time: big states fit in memory
            wr.write_tile(tid, path(*k).read_bytes())
        wr.finalize({
            "tile_type": TileType.PNG, "tile_compression": Compression.NONE,
            "min_lon_e7": int(w * 1e7), "min_lat_e7": int(s * 1e7), "max_lon_e7": int(e * 1e7), "max_lat_e7": int(n * 1e7),
            "center_zoom": 9, "center_lon_e7": int((w + e) / 2 * 1e7), "center_lat_e7": int((s + n) / 2 * 1e7),
        }, {"name": region["name"] + " terrain", "attribution": "Mapzen/AWS Terrain Tiles", "encoding": "terrarium"})
    return out


# ---------------------------------------------------------------- contour lines

CONTOUR_STEP = 10      # metres between lines in hills (Sahyadri: 500-1400 m); every 100 m is an index line
CONTOUR_STEP_HIGH = 20 # blocks whose terrain reaches 1500 m
CONTOUR_STEP_ALPINE = 50  # blocks reaching 2500 m (Himalaya): thousands of metres of relief
MERC = 20037508.342789244


def build_contours(region):
    """Contour lines (10 m in hills, 20 m above 1500 m, 50 m above 2500 m) from the region's terrain
    pack (z12, ~30 m), as vector tiles z10-14 in a
    'contour' layer with 'height' and 'nth_line' - the same fields as MapTiler's contours, so the
    app's map style draws them the same way, offline. Needs gdal_contour, ogr2ogr and tippecanoe
    (the GitHub Action installs them; on a PC this step is skipped if they're missing)."""
    if not all(shutil.which(t) for t in ("gdal_contour", "ogr2ogr", "tippecanoe")):
        log(f"Contours {region['id']}: skipped (GDAL / tippecanoe not installed)")
        return None
    import numpy as np
    from io import BytesIO
    from PIL import Image
    from pmtiles.reader import Reader, MmapSource

    dem = OUT / f"{region['id']}-dem.pmtiles"
    tmp = WORK / "contours" / region["id"]
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    tiles = tiles_in_bbox(region["bbox"], DEM_MAX_ZOOM)
    xs = sorted({t[1] for t in tiles}); ys = sorted({t[2] for t in tiles})
    z = DEM_MAX_ZOOM
    tile_m = 2 * MERC / (1 << z)
    px_m = tile_m / 256
    B = 8   # tiles per block side (2048 x 2048 pixels)
    sql = ("SELECT geom, CAST(height AS INTEGER) AS height, "
           "CASE WHEN CAST(height AS INTEGER) % 100 = 0 THEN 10 WHEN CAST(height AS INTEGER) % 50 = 0 THEN 5 "
           "WHEN CAST(height AS INTEGER) % 20 = 0 THEN 2 ELSE 1 END AS nth_line "
           "FROM contour WHERE height > 0")

    def block(bx, by):
        bw = min(B, xs[-1] + 1 - bx); bh = min(B, ys[-1] + 1 - by)
        with open(dem, "rb") as fh:
            rd = Reader(MmapSource(fh))
            # one extra tile right/below so lines join up across blocks
            grid = np.full(((bh + 1) * 256, (bw + 1) * 256), np.nan, dtype=np.float32)
            any_data = False
            for j in range(bh + 1):
                for i in range(bw + 1):
                    data = rd.get(z, bx + i, by + j)
                    if data is None:
                        continue
                    a = np.asarray(Image.open(BytesIO(data)).convert("RGB"), dtype=np.float32)
                    grid[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = a[:, :, 0] * 256 + a[:, :, 1] + a[:, :, 2] / 256 - 32768
                    any_data = True
        if not any_data:
            return None
        grid = grid[: bh * 256 + 2, : bw * 256 + 2]
        # gentle 3x3 smoothing: z12 terrain is ~30 m per pixel; this removes the staircase look
        pad = np.pad(grid, 1, mode="edge")
        grid = sum(pad[dy:dy + grid.shape[0], dx:dx + grid.shape[1]] for dy in range(3) for dx in range(3)) / 9
        # 10 m lines in hills, 20 m in high hills, 50 m in big mountains (thousands of metres of relief
        # would be a solid brown mass). Decided per 2048-px block (~80 km), so a valley stays consistent.
        top = np.nanmax(grid)
        step = CONTOUR_STEP if top < 1500 else CONTOUR_STEP_HIGH if top < 2500 else CONTOUR_STEP_ALPINE
        grid = np.nan_to_num(grid, nan=-32768).round().astype("<i2")
        base = tmp / f"b{bx}_{by}"
        grid.tofile(f"{base}.bil")
        ulx = -MERC + bx * tile_m + px_m / 2
        uly = MERC - by * tile_m - px_m / 2
        Path(f"{base}.hdr").write_text(
            f"NROWS {grid.shape[0]}\nNCOLS {grid.shape[1]}\nNBANDS 1\nNBITS 16\nPIXELTYPE SIGNEDINT\n"
            f"BYTEORDER I\nLAYOUT BIL\nULXMAP {ulx!r}\nULYMAP {uly!r}\nXDIM {px_m!r}\nYDIM {px_m!r}\nNODATA -32768\n")
        subprocess.run(["gdal_contour", "-q", "-a", "height", "-i", str(step), "-snodata", "-32768",
                        "-f", "GPKG", f"{base}.bil", f"{base}.gpkg"], check=True)
        out = Path(f"{base}.geojsonl")
        subprocess.run(["ogr2ogr", "-f", "GeoJSONSeq", str(out), f"{base}.gpkg", "-s_srs", "EPSG:3857", "-t_srs", "EPSG:4326",
                        "-dialect", "SQLite", "-sql", sql, "-lco", "COORDINATE_PRECISION=6"],
                       check=True, stderr=subprocess.DEVNULL)
        for ext in (".bil", ".hdr", ".gpkg"):
            Path(f"{base}{ext}").unlink(missing_ok=True)
        return out if out.exists() and out.stat().st_size > 0 else None

    jobs = [(bx, by) for bx in range(xs[0], xs[-1] + 1, B) for by in range(ys[0], ys[-1] + 1, B)]
    with cf.ThreadPoolExecutor(os.cpu_count() or 4) as ex:
        parts = [p for p in ex.map(lambda j: block(*j), jobs) if p]
    if not parts:
        log(f"Contours {region['id']}: no terrain data")
        return None
    out_pm = OUT / f"{region['id']}-contours.pmtiles"
    out_pm.unlink(missing_ok=True)
    # 100 m lines from zoom 10, 50 m from 12, 20 m from 13, 10 m from 14 (the map over-zooms z14 tiles
    # beyond that) - keeps hills readable when zoomed out, detailed like MapTiler's when zoomed in
    zoom_filter = json.dumps({"contour": ["any", ["==", "nth_line", 10],
                                          ["all", ["==", "nth_line", 5], [">=", "$zoom", 12]],
                                          ["all", ["==", "nth_line", 2], [">=", "$zoom", 13]],
                                          [">=", "$zoom", 14]]})
    subprocess.run(["tippecanoe", "-q", "-o", str(out_pm), "-l", "contour", "-Z10", "-z14", "--force",
                    "--no-tile-size-limit", "--no-feature-limit", "--simplification=4", "-j", zoom_filter,
                    "-P", *map(str, parts)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"Contours {region['id']}: {out_pm.stat().st_size / 1e6:.0f} MB")
    return out_pm


# ---------------------------------------------------------------- index + upload

def write_index(regions, base_url=""):
    packs = []
    for r in regions:
        m = OUT / f"{r['id']}.pmtiles"
        d = OUT / f"{r['id']}-dem.pmtiles"
        c = OUT / f"{r['id']}-contours.pmtiles"
        if not m.exists():
            continue
        # "?v=<build time>" makes every build a new address, so the Cloudflare relay's 24 h cache can
        # never hand out an old file (or mix old and new parts in a resumed download) after a rebuild
        v = "?v=" + dt.datetime.fromtimestamp(m.stat().st_mtime).strftime("%Y%m%d%H%M")
        packs.append({
            "id": r["id"], "name": r["name"], "group": r.get("group", ""), "bbox": r["bbox"],
            "map": {"file": base_url + m.name + v, "bytes": m.stat().st_size},
            "dem": {"file": base_url + d.name + v, "bytes": d.stat().st_size} if d.exists() else None,
            "contours": {"file": base_url + c.name + v, "bytes": c.stat().st_size} if c.exists() else None,
            "built": dt.datetime.fromtimestamp(m.stat().st_mtime).strftime("%Y-%m-%d"),
        })
    idx = {"version": 1, "attribution": "© OpenStreetMap contributors · Elevation: Mapzen/AWS", "packs": packs}
    (OUT / "packs.json").write_text(json.dumps(idx, indent=1))
    log(f"Wrote out/packs.json with {len(packs)} packs")


def upload_r2():
    files = sorted(OUT.glob("*.pmtiles")) + [OUT / "packs.json"]
    npx = "npx.cmd" if platform.system() == "Windows" else "npx"
    for f in files:
        mb = f.stat().st_size / 1e6
        if mb > 300:
            log(f"!! {f.name} is {mb:.0f} MB - Wrangler can only upload files up to 300 MB. Make that region smaller.")
            continue
        ctype = "application/json" if f.suffix == ".json" else "application/octet-stream"
        log(f"Uploading {f.name} ({mb:.0f} MB)...")
        subprocess.run([npx, "wrangler", "r2", "object", "put", f"{BUCKET}/{f.name}", f"--file={f}",
                        f"--content-type={ctype}", "--remote"], check=True)
    log("Done. Packs are served at  <your server>/packs/packs.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="build", choices=["build", "upload-r2", "index"])
    ap.add_argument("--only", help="build only this region id")
    ap.add_argument("--ram", type=int, default=6, help="GB of memory for Planetiler (default 6; use 4 on an 8 GB PC)")
    ap.add_argument("--skip-map", action="store_true", help="only (re)build the terrain packs")
    ap.add_argument("--no-contours", action="store_true", help="don't make contour lines")
    ap.add_argument("--rebuild", action="store_true", help="build the country map again with fresh OpenStreetMap data")
    ap.add_argument("--cleanup", action="store_true", help="delete the OSM downloads after the country map is built (saves ~4 GB)")
    ap.add_argument("--base-url", default="", help="for 'index': prefix for file links, e.g. a GitHub release download URL ending in /")
    a = ap.parse_args()

    cfg = json.loads((HERE / "regions.json").read_text())
    regions = [r for r in cfg["regions"] if not a.only or r["id"] == a.only]
    if not regions:
        sys.exit(f"No region '{a.only}' in regions.json")

    if a.command == "upload-r2":
        return upload_r2()
    if a.command == "index":
        return write_index(cfg["regions"], a.base_url)

    try:
        import pmtiles  # noqa: F401
    except ImportError:
        sys.exit("Please run:  pip install pmtiles")

    country = None
    if not a.skip_map:
        country = build_country_map(find_java(), cfg.get("osm_area", "india"), cfg.get("india_bbox", [68.0, 6.5, 97.5, 37.2]),
                                    a.ram, a.rebuild)
        if a.cleanup:
            shutil.rmtree(WORK / "sources", ignore_errors=True)  # OSM downloads are not needed any more
            shutil.rmtree(WORK / "tmp", ignore_errors=True)
    for i, r in enumerate(regions, 1):
        t0 = time.time()
        if country is not None:
            extract(country, OUT / f"{r['id']}.pmtiles", r["bbox"])
        build_dem(r)
        if not a.no_contours:
            build_contours(r)
        sizes = ", ".join(f"{p.name} {p.stat().st_size / 1e6:.0f} MB" for p in sorted(OUT.glob(f"{r['id']}*.pmtiles")))
        log(f"OK [{i}/{len(regions)}] {r['name']}: {sizes}  ({time.time() - t0:.0f} s)")
    write_index(cfg["regions"])


if __name__ == "__main__":
    main()
