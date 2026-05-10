# DRT Arrivals — TransSee Scraper

Real-time Durham Region Transit arrivals, scraped from transsee.ca.
Mobile-friendly browser UI + Python Flask backend.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the server
python server.py

# 3. Open in your browser
open http://localhost:5000
```

## Usage

1. Enter a route number (e.g. `900`, `302`, `917`)
2. Pick your direction (Northbound / Southbound / etc.)
3. Tap your stop
4. See live arrivals — tap Refresh to update

## How it works

- `server.py` — Flask API with three endpoints:
  - `GET /api/routes` — all active DRT routes
  - `GET /api/routes/<route>/stops` — stops grouped by direction
  - `GET /api/arrivals?key=...` — live predictions for a stop
- `static/index.html` — mobile-friendly single-page UI

## Access from your phone

Make sure your phone is on the same Wi-Fi as your computer, then open:
```
http://<your-computer-ip>:5000
```
Find your IP with `ipconfig` (Windows) or `ifconfig` / `ip addr` (Mac/Linux).

## API Examples

```
GET /api/routes/900/stops
→ { "route": "900", "directions": [ { "label": "Eastbound", "stops": [...] } ] }

GET /api/arrivals?key=durham.900.93450:1_0
→ { "stop": "Liverpool on Pickering Pkwy", "arrivals": [ { "minutes": 4, ... } ] }
```
