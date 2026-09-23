# CarWatch

Watches Carzone for you. Every few hours it opens each of your saved Carzone searches in a
headless browser, stores every ad it finds, and tracks price changes. You review new ads one
card at a time: swipe right to save, left to hide, thumbs-down a make/model/etc. to never see
it again.

## Install on a Synology NAS

1. In **File Station**, create `docker/carwatch` and upload this folder's contents into it
   (so you have `docker/carwatch/docker-compose.yml`, `Dockerfile`, `app/` …).
2. Open **Container Manager → Project → Create**.
   - Project name: `carwatch`
   - Path: `/volume1/docker/carwatch`
   - Source: *Use existing docker-compose.yml*
3. Click through and let it build (first build downloads ~1.5 GB for Chromium; a few minutes).
4. Open `http://<your-NAS-IP>:8080`.

It starts in **demo mode** with made-up ads so you can try the interface. When you're ready:
edit `docker-compose.yml`, set `DEMO=0`, then in Container Manager stop the project,
delete `data/carwatch.db` (to clear the demo ads), and start it again.

Command line alternative: `cd /volume1/docker/carwatch && sudo docker compose up -d --build`

## Or: host on GitHub, let the NAS pull a ready-made image

1. Create a GitHub repo named `carwatch` and push this folder to its `main` branch.
2. GitHub Actions (`.github/workflows/docker.yml`) builds the image and publishes it as
   `ghcr.io/<you>/carwatch:latest`. First build takes ~10 minutes (it builds for Intel and ARM).
3. If the repo is private, either make the package public (GitHub → your profile → Packages →
   carwatch → Package settings → Change visibility), or on the NAS run once:
   `sudo docker login ghcr.io -u <you>` with a personal access token that has `read:packages`.
4. On the NAS, put only `nas/docker-compose.yml` in `/volume1/docker/carwatch`, set your
   username in it, and create the project in Container Manager as above.

To update after pushing changes: Container Manager → Project → carwatch → Action → Stop, then
Build (it pulls the new image), or `sudo docker compose pull && sudo docker compose up -d`.
Your data lives in `data/` on the NAS and survives updates.

## Adding searches

1. On carzone.ie, set up a search with its filters (fuel, body type, year, mileage…).
2. Copy the address from the browser bar.
3. In CarWatch → **Searches**, paste it with a name.
4. Optionally add refinements Carzone can't do itself, e.g. engine 2.9–3.1 L for "3 litre".
   Fuel/body fields match partially and accept commas: `hybrid, plug-in`.
5. Tap **Check now**.

Your examples:

| Name | On Carzone | Extra refinements here |
|---|---|---|
| Electric estate 2022+ | Fuel: Electric, Body: Estate, Year from 2022 | – |
| Hybrid estate | Fuel: Hybrid (+ Plug-in), Body: Estate | Max km 200000 |
| Diesel 3L estate | Fuel: Diesel, Body: Estate | Engine 2.9 – 3.1, Max km 150000 |

## How the lists work

- **Market**: ads matching any enabled search, not yet swiped, not excluded, and seen on the
  last few checks. Newest first. Filter by search with the chips at the top.
- **Saved**: right swipes. Ads that disappear from Carzone stay here marked *No longer listed*.
- **Hidden**: left swipes, restorable.
- **Never show**: thumbs-down rules, managed under Searches.
- Price sticker shows the old price struck through when a price drops. Below the specs, the
  card compares the price to the median of the same model (±1 year) seen so far; this gets
  more useful the longer it runs.

Keyboard: ← hide, → save, Z undo.

## If a search finds 0 ads

Carzone doesn't publish an API, so the scraper reads whatever the page loads: its internal
JSON, structured data, and finally the visible result cards. If Carzone changes its site or
blocks the browser, open **Searches → Show what the scraper saw**. It shows a screenshot of
the page plus the raw JSON it captured.

- Screenshot shows a "verify you are human" page → Carzone is blocking automated visits.
  Try a longer interval, fewer pages.
- Screenshot shows results but 0 recognised → the field names changed. Add the new key names
  to `FIELD_KEYS` in `app/normalize.py`, rebuild.
- Only the first page is collected → check how the page number appears in Carzone's URL
  when you go to page 2 and set `PAGE_PARAM` to match.

## Access from your phone outside home

Easiest: install Tailscale on the NAS and your phone. Or use DSM's reverse proxy
(Control Panel → Login Portal → Advanced → Reverse Proxy) with HTTPS, and set `APP_PASSWORD`.

## Be a good citizen

Keep checks infrequent (the default is every 6 h with pauses between pages) and use this for
your own car search only. Carzone's terms of use apply.
