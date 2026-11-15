# SkyNav region packs (fast offline maps)

A region pack is **one map file + one terrain file** for a whole flying area. The phone downloads
it in one go at full Wi-Fi speed – seconds instead of many minutes – and it works completely offline.

You build the packs once on your PC (about 30–60 minutes the first time), put them online,
and every SkyNav user who enters the same address can download them.

Map data © OpenStreetMap contributors (ODbL), elevation Mapzen/AWS Terrain Tiles – free to share
with that credit, which SkyNav already shows.

---

## 1. Install once (Windows)

1. **Python** – https://www.python.org/downloads/ → run the installer and tick
   **"Add python.exe to PATH"** on the first screen.
2. Open **PowerShell** in this `maps` folder (File Explorer → `maps` folder → click the address
   bar → type `powershell` → Enter) and run:
   ```
   pip install pmtiles
   ```
3. **Java 21**: nothing to do – the script uses the Java that comes with Android Studio.

Free disk space needed: about **10 GB** (OpenStreetMap data for India + temporary files).
Memory: 8 GB RAM or more. With 8 GB, add `--ram 4` to the build command below.

## 2. Choose your regions

Open `regions.json` in Notepad. Each region has an `id` (short, no spaces), a `name` (shown in the
app) and a `bbox` = `[west, south, east, north]` in degrees. Two are already filled in:

- `maharashtra-west` – Kamshet, Panchgani, Sinhagad
- `bir-billing` – Bir-Billing, Dharamshala, Manali

Tip: to find a box, go to https://boundingbox.klokantech.com, draw a rectangle, choose **CSV** at
the bottom and copy the four numbers. Keep a pack around 150–300 km across.

## 3. Build

```
python build_packs.py
```

- The **first** run downloads OpenStreetMap data for India (~1.5 GB) plus coastline and lake data
  (~1 GB). Later runs reuse them.
- Each region then takes about 5–15 minutes. When it's finished you'll see lines such as
  `OK Himachal (...): bir-billing.pmtiles 140 MB, bir-billing-dem.pmtiles 70 MB`.
- Results are in the `out` folder, with `packs.json` (the list the app reads).

Build only one region: `python build_packs.py --only bir-billing`.

## 4. Put the packs online – choose A or B

### A. Cloudflare R2, served by your SkyNav server (recommended)

R2 is free up to 10 GB with no download fees. **The first time you open R2, Cloudflare asks for a
card or PayPal** – it's only a verification, nothing is charged within the free plan.

1. Dashboard → **R2 Object Storage** → enable it (add the card/PayPal when asked).
2. In PowerShell, in the **server** folder, with your API token set as before:
   ```
   $env:CLOUDFLARE_API_TOKEN="your-token"
   npx wrangler r2 bucket create skynav-maps
   ```
   If this says *not authorised*, edit your token in the dashboard (Profile → API Tokens → Edit)
   and add **Account → Workers R2 Storage → Edit**.
3. Open `server/wrangler.toml` and remove the `#` in front of the three lines at the bottom:
   ```
   [[r2_buckets]]
   binding = "MAPS"
   bucket_name = "skynav-maps"
   ```
   then deploy again: `npx wrangler deploy`
4. Back in the **maps** folder (same PowerShell window so the token is still set):
   ```
   python build_packs.py upload-r2
   ```
   Each file must be under 300 MB – if one is bigger, make that region smaller.
5. Check in your browser: `https://skynav-live.skynav4all.workers.dev/packs/packs.json`
   should show your packs.

**In the app:** nothing to set – SkyNav finds the packs on your live server automatically.

### B. GitHub Releases (free, no card)

1. Create a free account at github.com, then a new **public** repository, e.g. `skynav-maps`.
2. In the repository: **Releases → Create a new release** → tag `maps-v1` → drag every file from
   the `out` folder (all `.pmtiles` files and `packs.json`) into the box → **Publish release**.
   (Each file may be up to 2 GB.)
3. Your packs address is:
   `https://github.com/<your-user>/skynav-maps/releases/download/maps-v1/packs.json`

**In the app:** Settings → Offline map packs → paste that address.

## 5. Download on the phone

Offline maps → **Region packs** → tap a pack. Inside a pack the map, relief and AGL work fully
offline. Outside it, the normal map (and any "Around me" downloads) is used.

## Updating

Roads and terrain hardly change – rebuilding every few months is plenty. Run the build again,
upload again (R2: same command; GitHub: new release `maps-v2` and update the address in the app).
Pilots tap **Update** on a pack to get the new version.
