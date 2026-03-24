"""
Court Order Downloader — Backend Server
========================================
Run with:
    pip install flask requests beautifulsoup4 selenium webdriver-manager flask-cors
    python3 server.py

Then open: http://localhost:5000
"""

import os
import io
import re
import json
import time
import base64
import zipfile
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

DELAY = 1.5  # seconds between downloads


def make_session(base_url):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Referer": base_url,
    })
    return session


def parse_case_number(raw):
    """
    Parse case number like 'CS DJ/58585/2016' or 'CS SCJ 714/2024'
    Returns (case_type, case_no, case_year)
    """
    raw = raw.strip()
    # Try format: TYPE NO/YEAR or TYPE/NO/YEAR
    match = re.match(r'^(.+?)\s+(\d+)[/\s](\d{4})$', raw)
    if match:
        return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
    match = re.match(r'^(.+?)/(\d+)/(\d{4})$', raw)
    if match:
        return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
    raise ValueError(f"Could not parse case number: {raw}")


def get_cino_from_page(session, search_url, case_type, case_no, case_year):
    """Try to get CINO by fetching the search results page via Selenium."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select
        from webdriver_manager.chrome import ChromeDriverManager

        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )

        driver.get(search_url)
        time.sleep(3)

        # Try dropdowns for case type
        for sel_el in driver.find_elements(By.TAG_NAME, "select"):
            try:
                Select(sel_el).select_by_visible_text(case_type)
                break
            except Exception:
                pass

        # Fill case number
        for fid in ["case_no", "case_number", "caseNo", "caseNumber", "case_num", "caseno"]:
            try:
                f = driver.find_element(By.ID, fid)
                f.clear(); f.send_keys(case_no)
                break
            except Exception:
                pass

        # Fill year
        for fid in ["case_year", "year", "caseYear"]:
            try:
                f = driver.find_element(By.ID, fid)
                f.clear(); f.send_keys(case_year)
                break
            except Exception:
                pass

        # Submit
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']")
            btn.click()
        except Exception:
            pass

        time.sleep(5)
        page_source = driver.page_source
        driver.quit()
        return page_source

    except Exception as e:
        return None


def extract_orders_from_html(html, base_url):
    """Extract all order links from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    orders = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "get_order_pdf" in href and "input_strings=" in href:
            label = a.get_text(strip=True) or "order"
            full_url = href if href.startswith("http") else base_url + href
            # Decode to get date for sorting
            try:
                qs = href.split("input_strings=")[1].split("&")[0]
                decoded = json.loads(base64.b64decode(qs).decode())
                date = decoded.get("order_date", "0000-00-00")
                order_no = decoded.get("order_no", 0)
            except Exception:
                date = "0000-00-00"
                order_no = 0
            orders.append({
                "label": label,
                "url": full_url,
                "date": date,
                "order_no": order_no
            })

    # Sort by date
    orders.sort(key=lambda x: x["date"])
    return orders


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/fetch-orders", methods=["POST"])
def fetch_orders():
    data = request.json
    court_url = data.get("court_url", "").rstrip("/")
    case_number = data.get("case_number", "")

    if not court_url or not case_number:
        return jsonify({"error": "Please provide both court URL and case number"}), 400

    try:
        case_type, case_no, case_year = parse_case_number(case_number)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    search_url = f"{court_url}/court-orders-search-by-case-number/"
    ajax_url = f"{court_url}/wp-admin/admin-ajax.php"
    session = make_session(search_url)

    # Try direct POST first
    orders = []
    try:
        resp = session.get(search_url, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        form_data = {}
        for hidden in soup.find_all("input", type="hidden"):
            if hidden.get("name"):
                form_data[hidden["name"]] = hidden.get("value", "")
        form_data.update({
            "action": "get_case_orders",
            "case_type": case_type,
            "case_no": case_no,
            "case_year": case_year,
            "es_ajax_request": "1",
        })
        resp2 = session.post(ajax_url, data=form_data, timeout=20)
        if "get_order_pdf" in resp2.text:
            orders = extract_orders_from_html(resp2.text, court_url)
    except Exception:
        pass

    # Fallback to Selenium
    if not orders:
        html = get_cino_from_page(session, search_url, case_type, case_no, case_year)
        if html:
            orders = extract_orders_from_html(html, court_url)

    if not orders:
        return jsonify({"error": "No orders found. Check the court URL and case number."}), 404

    return jsonify({"orders": orders, "count": len(orders)})


@app.route("/api/download-all", methods=["POST"])
def download_all():
    data = request.json
    court_url = data.get("court_url", "").rstrip("/")
    case_number = data.get("case_number", "")
    orders = data.get("orders", [])

    if not orders:
        return jsonify({"error": "No orders to download"}), 400

    session = make_session(court_url)
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, order in enumerate(orders, start=1):
            url = order["url"]
            date = order.get("date", "unknown")
            label = order.get("label", "order")
            safe_label = re.sub(r'[^a-zA-Z0-9_\-]', '_', label)
            filename = f"{i:03d}_{date}_{safe_label}.pdf"

            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
                if r.content[:4] == b"%PDF":
                    zf.writestr(filename, r.content)
                time.sleep(DELAY)
            except Exception as e:
                print(f"Failed to download {url}: {e}")

    zip_buffer.seek(0)
    safe_case = re.sub(r'[^a-zA-Z0-9_\-]', '_', case_number)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"court_orders_{safe_case}.zip"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "="*50)
    print("  Court Order Downloader — Server Running")
    print(f"  Open this in your browser:")
    print(f"  → http://localhost:{port}")
    print("="*50 + "\n")
    app.run(debug=False, host="0.0.0.0", port=port)
