"""
DRT TransSee Backend
====================
Scrapes transsee.ca for Durham Region Transit data and exposes a simple JSON API.

Endpoints:
  GET /api/routes                      — list all active routes
  GET /api/routes/<route>/stops        — stops grouped by direction (with lat/lon)
  GET /api/arrivals?key=...            — live arrivals for a stop key
  GET /api/gtfs/refresh                — manually refresh GTFS stop coordinates cache

Run:
  python server.py
  Then open http://localhost:5000 in your browser.
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re, os, csv, io, zipfile, threading, time

app = Flask(__name__, static_folder="static")
CORS(app)

BASE   = "https://transsee.ca"
AGENCY = "durham"
GTFS_URL = "https://maps.durham.ca/OpenDataGTFS/GTFS_Durham_TXT.zip"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": "https://transsee.ca/",
}

# ── GTFS stop coordinates cache ───────────────────────────────────────────────
# Maps stop_id (str) -> {"lat": float, "lon": float}
_gtfs_stops = {}
_gtfs_lock  = threading.Lock()
_gtfs_loaded = False

def load_gtfs_stops():
    """Download DRT GTFS zip and cache stop_id -> lat/lon."""
    global _gtfs_stops, _gtfs_loaded
    try:
        print("Loading GTFS stop coordinates...")
        resp = requests.get(GTFS_URL, timeout=30)
        resp.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        with z.open("stops.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            stops = {}
            for row in reader:
                sid = row.get("stop_id", "").strip()
                try:
                    lat = float(row.get("stop_lat", 0))
                    lon = float(row.get("stop_lon", 0))
                    if lat and lon:
                        stops[sid] = {"lat": lat, "lon": lon}
                except ValueError:
                    pass
        with _gtfs_lock:
            _gtfs_stops = stops
            _gtfs_loaded = True
        print(f"GTFS loaded: {len(stops)} stops cached.")
    except Exception as e:
        print(f"GTFS load failed: {e}")

def get_stop_coords(stop_key):
    """
    Extract stop_id from a TransSee key like durham.900.3589_0
    and look up its coordinates in the GTFS cache.
    Returns {"lat": ..., "lon": ...} or None.
    """
    # Key format: durham.ROUTE.STOPID_DIRSUFFIX
    # Stop ID is the part after the second dot, before underscore/end
    m = re.search(r"durham\.\w+\.([^_\s]+)", stop_key)
    if not m:
        return None
    raw_id = m.group(1)
    # Remove any trailing _0 _1 etc that got into the stop id part
    stop_id = re.sub(r"_\d+$", "", raw_id)
    with _gtfs_lock:
        return _gtfs_stops.get(stop_id)

# Load GTFS in background on startup
threading.Thread(target=load_gtfs_stops, daemon=True).start()


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/routes")
def get_routes():
    try:
        soup = fetch(f"{BASE}/routelist?a={AGENCY}")
        routes = []
        for a in soup.find_all("a", href=True):
            m = re.search(r"stoplist\?a=durham&r=(\w+)", a["href"])
            if m:
                route_num = m.group(1)
                name = a.get_text(strip=True)
                if route_num and name:
                    routes.append({"route": route_num, "name": name})
        seen = set()
        unique = [r for r in routes if not (r["route"] in seen or seen.add(r["route"]))]
        return jsonify({"routes": unique})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Directions + Stops ────────────────────────────────────────────────────────

@app.route("/api/routes/<route>/stops")
def get_stops(route):
    """
    Return stops grouped by direction, each stop including lat/lon if available.
    """
    from urllib.parse import unquote
    try:
        soup = fetch(f"{BASE}/stoplist?a={AGENCY}&r={route}")
        directions = []

        dir_tables = soup.find_all(class_="DirTable")
        for dir_table in dir_tables:
            branches = dir_table.find_all("table", class_="routetable")
            if not branches:
                continue

            first_th = dir_table.find("th")
            if first_th:
                raw = first_th.get_text(strip=True)
                if "↑" in raw:   arrow, dir_word = "↑", "Northbound"
                elif "↓" in raw: arrow, dir_word = "↓", "Southbound"
                elif "→" in raw: arrow, dir_word = "→", "Eastbound"
                elif "←" in raw: arrow, dir_word = "←", "Westbound"
                else:             arrow, dir_word = "", "Direction"
                clean = raw.replace("↑","").replace("↓","").replace("→","").replace("←","").strip()
                label = f"{dir_word} → {clean}" if clean else dir_word
            else:
                arrow, label = "", "Direction"

            seen_keys = {}
            for branch in branches:
                for a in branch.find_all("a", href=True):
                    href = unquote(a["href"])
                    m = re.search(r"predict\?s=(durham\.[^&\s]+)", href)
                    if m:
                        key  = m.group(1)
                        name = a.get_text(strip=True)
                        if name and key not in seen_keys:
                            seen_keys[key] = name

            stops = []
            for key, name in seen_keys.items():
                stop = {"name": name, "key": key}
                coords = get_stop_coords(key)
                if coords:
                    stop["lat"] = coords["lat"]
                    stop["lon"] = coords["lon"]
                stops.append(stop)

            if stops:
                directions.append({"label": label, "arrow": arrow, "stops": stops})

        # Fallback
        if not directions:
            seen = {}
            for a in soup.find_all("a", href=True):
                from urllib.parse import unquote as uq
                href = uq(a["href"])
                m = re.search(r"predict\?s=(durham\.[^&\s]+)", href)
                if m:
                    key  = m.group(1)
                    name = a.get_text(strip=True)
                    if name and key not in seen:
                        seen[key] = name
            if seen:
                stops = []
                for key, name in seen.items():
                    stop = {"name": name, "key": key}
                    coords = get_stop_coords(key)
                    if coords:
                        stop["lat"] = coords["lat"]
                        stop["lon"] = coords["lon"]
                    stops.append(stop)
                directions.append({"label": "All stops", "arrow": "", "stops": stops})

        return jsonify({"route": route, "directions": directions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Arrivals ──────────────────────────────────────────────────────────────────

@app.route("/api/arrivals")
def get_arrivals():
    key = request.args.get("key", "")
    if not key:
        return jsonify({"error": "Missing 'key' query parameter"}), 400
    try:
        soup = fetch(f"{BASE}/predict?s={key}")
        title = soup.find("title")
        stop_name = key
        destination = ""
        if title:
            parts = title.get_text().split(" - ")
            if parts:
                stop_name = parts[0].strip()
        bold = soup.find("b")
        if bold:
            dest_match = re.search(r"going\s*[→←↑↓]?\s*(.+)", bold.get_text(strip=True))
            if dest_match:
                destination = dest_match.group(1).strip()
        arrivals = []
        for div in soup.find_all("div", class_="divp")[:3]:
            text = div.get_text(separator=" ", strip=True)
            if re.search(r"at\s+stop", text, re.IGNORECASE):
                vm = re.search(r"Vehicle\s+(\d+)", text)
                arrivals.append({"minutes": 0, "minutes_range": None, "time": "Now",
                                  "destination": destination, "vehicle": vm.group(1) if vm else None,
                                  "scheduled": "sched" in text.lower()})
                continue
            m = re.search(r"([\d\s\-–]+)\s*[Mm]in\w*\s+at\s+([\d:]+(?:AM|PM))", text, re.IGNORECASE)
            if m:
                mins_raw = m.group(1).strip()
                mins_parts = re.findall(r"\d+", mins_raw)
                vm = re.search(r"Vehicle\s+(\d+)", text)
                arrivals.append({
                    "minutes": int(mins_parts[0]) if mins_parts else None,
                    "minutes_range": mins_raw if re.search(r"\d\s*[-–]\s*\d", mins_raw) else None,
                    "time": m.group(2), "destination": destination,
                    "vehicle": vm.group(1) if vm else None,
                    "scheduled": "sched" in text.lower(),
                })
        return jsonify({"stop": stop_name, "key": key, "arrivals": arrivals})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GTFS refresh ──────────────────────────────────────────────────────────────

@app.route("/api/gtfs/refresh")
def gtfs_refresh():
    threading.Thread(target=load_gtfs_stops, daemon=True).start()
    return jsonify({"status": "GTFS reload started"})


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
