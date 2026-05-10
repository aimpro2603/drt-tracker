"""
DRT TransSee Backend
====================
Scrapes transsee.ca for Durham Region Transit data and exposes a simple JSON API.

Endpoints:
  GET /api/routes               — list all active routes
  GET /api/routes/<route>/directions  — list directions for a route
  GET /api/routes/<route>/stops?dir=_0  — list stops for a route + direction
  GET /api/stops/<stop_key>/arrivals    — live arrivals for a stop key
                                          stop_key format: durham.<route>.<id>:1_0

Run:
  python server.py
  Then open http://localhost:5000 in your browser.
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import os

app = Flask(__name__, static_folder="static")
CORS(app)

BASE = "https://transsee.ca"
AGENCY = "durham"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": "https://transsee.ca/",
}


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/routes")
def get_routes():
    """Return all currently active routes for Durham Region Transit."""
    try:
        soup = fetch(f"{BASE}/routelist?a={AGENCY}")
        routes = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Links like /stoplist?a=durham&r=900
            m = re.search(r"stoplist\?a=durham&r=(\w+)", href)
            if m:
                route_num = m.group(1)
                name = a.get_text(strip=True)
                if route_num and name:
                    routes.append({"route": route_num, "name": name})
        # Deduplicate preserving order
        seen = set()
        unique = []
        for r in routes:
            if r["route"] not in seen:
                seen.add(r["route"])
                unique.append(r)
        return jsonify({"routes": unique})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Directions + Stops ────────────────────────────────────────────────────────

@app.route("/api/routes/<route>/stops")
def get_stops(route):
    """
    Return stops grouped by direction for a route.
    TransSee uses <table class="DirTable" id=0> for one direction group
    and id=1 for the other. Each DirTable contains routetable sub-tables,
    each with a <th> header like "↑C - Uxbridge" and <a> stop links.
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

            # Direction label from first <th> in this group
            first_th = dir_table.find("th")
            if first_th:
                raw = first_th.get_text(strip=True)
                if "↑" in raw:
                    arrow, dir_word = "↑", "Northbound"
                elif "↓" in raw:
                    arrow, dir_word = "↓", "Southbound"
                elif "→" in raw:
                    arrow, dir_word = "→", "Eastbound"
                elif "←" in raw:
                    arrow, dir_word = "←", "Westbound"
                else:
                    arrow, dir_word = "", "Direction"
                clean = raw.replace("↑","").replace("↓","").replace("→","").replace("←","").strip()
                label = f"{dir_word} → {clean}" if clean else dir_word
            else:
                arrow, label = "", "Direction"

            # Collect unique stops across all branches, preserving order
            seen_keys = {}
            for branch in branches:
                for a in branch.find_all("a", href=True):
                    href = unquote(a["href"])
                    m = re.search(r"predict\?s=(durham\.[^&\s]+)", href)
                    if m:
                        key = m.group(1)
                        name = a.get_text(strip=True)
                        if name and key not in seen_keys:
                            seen_keys[key] = name

            stops = [{"name": v, "key": k} for k, v in seen_keys.items()]
            if stops:
                directions.append({"label": label, "arrow": arrow, "stops": stops})

        # Fallback: no DirTable found, grab all predict links
        if not directions:
            seen = {}
            for a in soup.find_all("a", href=True):
                href = unquote(a["href"])
                m = re.search(r"predict\?s=(durham\.[^&\s]+)", href)
                if m:
                    key = m.group(1)
                    name = a.get_text(strip=True)
                    if name and key not in seen:
                        seen[key] = name
            if seen:
                stops = [{"name": v, "key": k} for k, v in seen.items()]
                directions.append({"label": "All stops", "arrow": "", "stops": stops})

        return jsonify({"route": route, "directions": directions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Arrivals ──────────────────────────────────────────────────────────────────

@app.route("/api/arrivals")
def get_arrivals():
    """
    Fetch live arrival predictions for a stop.
    Query param: key=durham.900.93450:1_0
    Response: { stop, arrivals: [{route, destination, minutes, time, vehicle, scheduled}] }
    """
    key = request.args.get("key", "")
    if not key:
        return jsonify({"error": "Missing 'key' query parameter"}), 400
    try:
        soup = fetch(f"{BASE}/predict?s={key}")

        # Page title = stop name
        h1 = soup.find("h1")
        stop_name = h1.get_text(strip=True) if h1 else key

        arrivals = []

        # TransSee renders predictions as numbered list items like:
        # "1: 4 - 7 Mins at 5:25:14PM. Vehicle 8575 ↑B - Destination"
        # We look for <li> or <div> elements containing "Min" and a time
        prediction_pattern = re.compile(
            r"(\d+):\s*([\d\s\-–]+)\s*[Mm]in.*?at\s*([\d:]+[APM]+)",
            re.IGNORECASE
        )
        dest_pattern = re.compile(r"[↑↓→←]?\s*([A-Z][^\n]+?)(?:\s+Last seen|Only|$)")

        # Try to find the predictions block
        body_text = soup.get_text(separator="\n")
        for line in body_text.split("\n"):
            line = line.strip()
            m = prediction_pattern.search(line)
            if m:
                index = m.group(1)
                mins_raw = m.group(2).strip()
                arrival_time = m.group(3)
                scheduled = "(Sched. based)" in line

                # Parse minutes — may be a range like "4 - 7"
                mins_parts = re.findall(r"\d+", mins_raw)
                minutes = int(mins_parts[0]) if mins_parts else None

                # Extract destination
                dest_match = dest_pattern.search(line)
                destination = dest_match.group(1).strip() if dest_match else ""

                # Extract vehicle number
                vehicle_match = re.search(r"Vehicle\s+(\d+)", line)
                vehicle = vehicle_match.group(1) if vehicle_match else None

                arrivals.append({
                    "index": int(index),
                    "minutes": minutes,
                    "minutes_range": mins_raw if "-" in mins_raw else None,
                    "time": arrival_time,
                    "destination": destination,
                    "vehicle": vehicle,
                    "scheduled": scheduled,
                })

        return jsonify({"stop": stop_name, "key": key, "arrivals": arrivals})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    print("Starting DRT TransSee backend on http://localhost:5000")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
