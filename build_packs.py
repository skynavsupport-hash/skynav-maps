#!/usr/bin/env python3
"""
SkyNav region packs: builds one map file + one terrain file per region, so the app can download a
whole flying area in one go (seconds instead of many minutes of single tiles).

    python build_packs.py                 build every region in regions.json
    python build_packs.py --only bir-billing
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


def build_map(java, region, area, ram_gb):
    """OpenMapTiles-schema vector tiles z0-14 for the region (the same schema the app's map style uses)."""
    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    w, s, e, n = region["bbox"]
    out = OUT / f"{region['id']}.pmtiles"
    cmd = [
        java, f"-Xmx{ram_gb}g", "-jar", str(planetiler_jar()),
        "--download", f"--area={area}",
        f"--bounds={w},{s},{e},{n}",
        f"--output={out}", "--force",
        f"--download_dir={WORK / 'sources'}", f"--tmpdir={WORK / 'tmp'}",
        "--maxzoom=14",
        "--http_retries=20", "--http_timeout=120s",
    ]
    log(f"Map {region['id']}: running Planetiler (first run downloads OpenStreetMap data for '{area}', a few GB)...")
    subprocess.run(cmd, check=True, cwd=WORK)
    return out


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
    got = {}
    todo = []
    for (z, x, y) in tiles:
        f = cache / str(z) / str(x) / f"{y}.png"
        if f.exists():
            got[(z, x, y)] = f.read_bytes()
        else:
            todo.append((z, x, y))
    done = 0
    with cf.ThreadPoolExecutor(32) as ex:
        for z, x, y, data in ex.map(lambda t: fetch(*t), todo):
            f = cache / str(z) / str(x) / f"{y}.png"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            got[(z, x, y)] = data
            done += 1
            if done % 200 == 0:
                log(f"  {done}/{len(todo)} downloaded")

    out = OUT / f"{region['id']}-dem.pmtiles"
    w, s, e, n = region["bbox"]
    with open(out, "wb") as f:
        wr = Writer(f)
        for tid, data in sorted((zxy_to_tileid(*k), v) for k, v in got.items()):
            wr.write_tile(tid, data)
        wr.finalize({
            "tile_type": TileType.PNG, "tile_compression": Compression.NONE,
            "min_lon_e7": int(w * 1e7), "min_lat_e7": int(s * 1e7), "max_lon_e7": int(e * 1e7), "max_lat_e7": int(n * 1e7),
            "center_zoom": 9, "center_lon_e7": int((w + e) / 2 * 1e7), "center_lat_e7": int((s + n) / 2 * 1e7),
        }, {"name": region["name"] + " terrain", "attribution": "Mapzen/AWS Terrain Tiles", "encoding": "terrarium"})
    return out


# ---------------------------------------------------------------- index + upload

def write_index(regions, base_url=""):
    packs = []
    for r in regions:
        m = OUT / f"{r['id']}.pmtiles"
        d = OUT / f"{r['id']}-dem.pmtiles"
        if not m.exists():
            continue
        packs.append({
            "id": r["id"], "name": r["name"], "bbox": r["bbox"],
            "map": {"file": base_url + m.name, "bytes": m.stat().st_size},
            "dem": {"file": base_url + d.name, "bytes": d.stat().st_size} if d.exists() else None,
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
    java = None if a.skip_map else find_java()
    for r in regions:
        t0 = time.time()
        if not a.skip_map:
            build_map(java, r, cfg.get("osm_area", "india"), a.ram)
        build_dem(r)
        sizes = ", ".join(f"{p.name} {p.stat().st_size / 1e6:.0f} MB" for p in OUT.glob(f"{r['id']}*.pmtiles"))
        log(f"OK {r['name']}: {sizes}  ({(time.time() - t0) / 60:.0f} min)")
    write_index(cfg["regions"])


if __name__ == "__main__":
    main()
