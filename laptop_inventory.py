# laptop_inventory.py
"""
Laptop Inventory app (Flask + SQLite)
Includes: bcrypt auth, CSV import, Chart API, barcode/qrcode endpoints, backups, splash screen.
Drop this file into your project root (next to static/ templates/ inventory.db).
Run: python laptop_inventory.py
"""

import os
import sys
import sqlite3
import shutil
from datetime import datetime
from threading import Timer
import webbrowser
import io
import csv

from flask import (
    Flask, g, render_template_string, request, redirect, url_for,
    send_file, jsonify, flash, send_from_directory
)

# Optional external libs — install via pip
import bcrypt
import barcode
from barcode.writer import ImageWriter
import qrcode

from functools import wraps

# flask-login
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)

# ---------- Paths ----------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(__file__)

DB_PATH = os.path.join(BASE_DIR, "inventory.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
GENERATED_DIR = os.path.join(STATIC_DIR, "generated")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# ---------- App ----------
app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-in-prod"  # change in production

# ---------- Login manager ----------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# ---------- DB helpers ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        # make sure parent dir exists
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS laptops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT UNIQUE,
        brand TEXT,
        model TEXT,
        serial TEXT,
        cpu TEXT,
        ram_gb INTEGER,
        storage TEXT,
        initial_price REAL,
        selling_price REAL,
        location TEXT,
        status TEXT,
        notes TEXT,
        date_received TEXT,
        date_sold TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT,
        role TEXT DEFAULT 'staff',
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    db.commit()

    # create default admin if missing
    row = db.execute("SELECT COUNT(*) as c FROM users").fetchone()
    if row and row["c"] == 0:
        pw = "admin"  # change this immediately in production
        hashed = hash_password(pw)
        db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                   ("admin", hashed, "admin"))
        db.commit()
        app.logger.info("Created default admin user 'admin' with password 'admin' (change immediately)")

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# ---------- Password hashing (bcrypt) ----------
def hash_password(plain: str) -> str:
    if plain is None:
        return None
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    if plain is None or hashed is None:
        return False
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))

# ---------- Flask-Login user ----------
class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    r = db.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
    if r:
        return User(r["id"], r["username"], r["role"])
    return None

def role_required(role):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role != role and current_user.role != "admin":
                return "Forbidden", 403
            return f(*args, **kwargs)
        return wrapped
    return decorator

# ---------- In-memory templates ----------
BASE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Awaken Laptop Inventory</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <style>
    body{font-family:system-ui,Segoe UI,Roboto,Arial,Helvetica;max-width:1100px;margin:18px auto;padding:8px}
    header{display:flex;align-items:center;gap:12px}
    header img{height:58px}
    nav{margin-left:auto}
    nav a{margin-left:8px;text-decoration:none;color:#0366d6}
    table{border-collapse:collapse;width:100%;margin-top:12px}
    th,td{border:1px solid #eee;padding:8px;text-align:left}
    th{background:#fafafa}
    .controls{margin:12px 0;display:flex;gap:8px;flex-wrap:wrap}
    .btn{background:#0366d6;color:#fff;padding:8px 10px;border-radius:6px;text-decoration:none;display:inline-block}
    .btn.secondary{background:#6c757d}
    input[type=text], input[type=number], textarea, select { width: 320px; padding:6px; margin:4px 0; }
    label { display:block; margin:6px 0; }
    .muted{color:#666;font-size:0.9em}
    .small{font-size:0.9em}
    .generated img{max-width:160px;height:auto}
    footer{margin-top:18px;color:#666;font-size:0.85em;border-top:1px solid #eee;padding-top:8px}
  </style>
  {% block head %}{% endblock %}
</head>
<body>
  <header>
    <img src="{{ url_for('static', filename='img/awaken-logo.png') }}" alt="Awaken logo">
    <h1 style="margin:0;font-size:1.6rem">Laptop Inventory</h1>
    <nav>
      {% if current_user.is_authenticated %}
        <span class="small">Hello, {{ current_user.username }} ({{ current_user.role }})</span>
        <a href="{{ url_for('logout') }}">Logout</a>
      {% else %}
        <a href="{{ url_for('login') }}">Login</a>
      {% endif %}
    </nav>
  </header>

  <div class="controls">
    <a class="btn" href="{{ url_for('add') }}">+ Add Laptop</a>
    <a class="btn secondary" href="{{ url_for('export_csv') }}">Export CSV</a>
    <a class="btn secondary" href="{{ url_for('dashboard') }}">Dashboard</a>
    <a class="btn secondary" href="{{ url_for('splash') }}">Splash</a>

    <form action="{{ url_for('import_csv') }}" method="post" enctype="multipart/form-data" style="display:inline-block;margin-left:8px">
      <input type="file" name="file" accept=".csv">
      <button class="btn secondary" type="submit">Import CSV</button>
    </form>
  </div>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div>
        {% for cat, msg in messages %}
          <div style="padding:8px;margin-bottom:8px;border-radius:6px;background:{{ '#d4edda' if cat=='success' else '#f8d7da' }};color:{{ '#155724' if cat=='success' else '#721c24' }}">{{ msg }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  {% block body %}{% endblock %}

  <footer>
    <div class="muted">Simple Flask + SQLite demo — For production: add HTTPS, backups, auth hardening, and a proper DB.</div>
  </footer>
</body>
</html>
"""

INDEX_HTML = """
{% extends "base" %}
{% block body %}
<form method="get" style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
  <label>Status
    <select name="status">
      <option value="">-- all --</option>
      <option value="in_stock" {% if qstatus=='in_stock' %}selected{% endif %}>In stock</option>
      <option value="sold" {% if qstatus=='sold' %}selected{% endif %}>Sold</option>
      <option value="reserved" {% if qstatus=='reserved' %}selected{% endif %}>Reserved</option>
      <option value="out_for_repair" {% if qstatus=='out_for_repair' %}selected{% endif %}>Out for repair</option>
    </select>
  </label>
  <label>Search <input name="q" value="{{ q or '' }}"></label>
  <button class="btn secondary">Filter</button>
</form>

<table>
  <thead><tr>
    <th>SKU</th><th>Brand / Model</th><th>Serial</th><th>Specs</th><th>Initial</th><th>Selling</th><th>Status</th><th>Location</th><th>Actions</th>
  </tr></thead>
  <tbody>
    {% for r in items %}
    <tr>
      <td>{{ r['sku'] }}</td>
      <td><strong>{{ r['brand'] }}</strong><br>{{ r['model'] }}</td>
      <td>{{ r['serial'] or '' }}</td>
      <td>{{ r['cpu'] or '' }} / {{ r['ram_gb'] or '' }}GB / {{ r['storage'] or '' }}</td>
      <td>${{ '%.2f'|format(r['initial_price'] or 0) }}</td>
      <td>${{ '%.2f'|format(r['selling_price'] or 0) }}</td>
      <td>{{ r['status'] or '' }}</td>
      <td>{{ r['location'] or '' }}</td>
      <td>
        <a href="{{ url_for('edit', item_id=r['id']) }}">Edit</a> |
        <a href="{{ url_for('gen_barcode', sku=r['sku']) }}" target="_blank">Barcode</a> |
        <a href="{{ url_for('gen_qr', data=r['sku']) }}" target="_blank">QR</a> |
        <form style="display:inline" method="post" action="{{ url_for('toggle_out', item_id=r['id']) }}">
          {% if r['status']!='sold' %}
            <button type="submit" class="small">Mark as Sold</button>
          {% else %}
            <button type="submit" class="small">Mark In Stock</button>
          {% endif %}
        </form>
        <form style="display:inline" method="post" action="{{ url_for('delete', item_id=r['id']) }}" onsubmit="return confirm('Delete this item?');">
          <button type="submit" style="background:#d9534f;color:white;border:none;padding:4px 8px;margin-left:4px">Delete</button>
        </form>
      </td>
    </tr>
    {% else %}
    <tr><td colspan=9 class="muted">No items found.</td></tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

FORM_HTML = """
{% extends "base" %}
{% block body %}
<h2>{{ 'Edit' if item else 'Add' }} Laptop</h2>
<form method="post">
  <label>SKU: <input name="sku" value="{{ item.sku if item else '' }}" required></label>
  <label>Brand: <input name="brand" value="{{ item.brand if item else '' }}"></label>
  <label>Model: <input name="model" value="{{ item.model if item else '' }}"></label>
  <label>Serial: <input name="serial" value="{{ item.serial if item else '' }}"></label>
  <label>CPU: <input name="cpu" value="{{ item.cpu if item else '' }}"></label>
  <label>RAM (GB): <input type="number" name="ram_gb" value="{{ item.ram_gb if item else '' }}"></label>
  <label>Storage: <input name="storage" value="{{ item.storage if item else '' }}"></label>
  <label>Initial price: <input type="number" step="0.01" name="initial_price" value="{{ item.initial_price if item else '' }}"></label>
  <label>Selling price: <input type="number" step="0.01" name="selling_price" value="{{ item.selling_price if item else '' }}"></label>
  <label>Location: <input name="location" value="{{ item.location if item else '' }}"></label>
  <label>Status:
    <select name="status">
      <option value="in_stock" {% if item and item.status=='in_stock' %}selected{% endif %}>in_stock</option>
      <option value="sold" {% if item and item.status=='sold' %}selected{% endif %}>sold</option>
      <option value="reserved" {% if item and item.status=='reserved' %}selected{% endif %}>reserved</option>
      <option value="out_for_repair" {% if item and item.status=='out_for_repair' %}selected{% endif %}>out_for_repair</option>
    </select>
  </label>
  <label>Notes:<br><textarea name="notes" rows="3" cols="60">{{ item.notes if item else '' }}</textarea></label><br><br>
  <button type="submit" class="btn">Save</button>
  <a href="{{ url_for('index') }}" class="btn secondary">Cancel</a>
</form>
{% endblock %}
"""

DASHBOARD_HTML = """
{% extends "base" %}
{% block head %}
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
{% endblock %}
{% block body %}
<h2>Dashboard</h2>
<div style="display:flex;gap:18px;flex-wrap:wrap">
  <div style="flex:1;min-width:280px">
    <h3>Inventory Value by Status</h3>
    <canvas id="valueChart" height="160"></canvas>
  </div>
  <div style="flex:1;min-width:280px">
    <h3>Counts by Status</h3>
    <canvas id="countChart" height="160"></canvas>
  </div>
</div>

<script>
fetch("{{ url_for('api_stats') }}")
  .then(r=>r.json())
  .then(data=>{
    const labels = data.labels;
    const values = data.values;
    const counts = data.counts;

    const ctx1 = document.getElementById('valueChart').getContext('2d');
    new Chart(ctx1, {type:'bar', data:{labels:labels, datasets:[{label:'Total selling value', data:values}]}});

    const ctx2 = document.getElementById('countChart').getContext('2d');
    new Chart(ctx2, {type:'pie', data:{labels:labels, datasets:[{label:'Counts', data:counts}]}});

    // show totals
    const totVal = values.reduce((a,b)=>a+(b||0),0);
    const totCount = counts.reduce((a,b)=>a+(b||0),0);
    const el = document.createElement('div');
    el.innerHTML = `<p class="muted">Total inventory value: $${totVal.toFixed(2)} — Items: ${totCount}</p>`;
    document.querySelector('h2').after(el);
  });
</script>
{% endblock %}
"""

SPLASH_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Awaken — Loading</title>
  <style>
    body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#07102a,#0f1b55);color:#fff}
    .logo{width:320px;animation:float 2.8s ease-in-out infinite;filter:drop-shadow(0 20px 30px rgba(76,201,240,0.12))}
    @keyframes float{0%{transform:translateY(0)}50%{transform:translateY(-10px)}100%{transform:translateY(0)}}
    .glow{position:absolute;inset:auto;bottom:6rem;left:50%;transform:translateX(-50%);opacity:0.85}
  </style>
</head>
<body>
  <div style="text-align:center">
    <img src="{{ url_for('static', filename='img/awaken-logo.png') }}" class="logo" alt="Awaken">
    <div class="glow">Awaken • IT • Software • AI</div>
  </div>
  <script>setTimeout(()=>location.href="{{ url_for('dashboard') }}", 1200);</script>
</body>
</html>
"""

LOGIN_HTML = """
{% extends "base" %}
{% block body %}
<h2>Login</h2>
<form method="post">
  <label>Username: <input name="username" required></label>
  <label>Password: <input name="password" type="password" required></label>
  <button class="btn">Login</button>
</form>
{% endblock %}
"""

# template registry
TEMPLATES = {
    "base": BASE_HTML,
    "index": INDEX_HTML,
    "form": FORM_HTML,
    "dashboard": DASHBOARD_HTML,
    "splash": SPLASH_HTML,
    "login": LOGIN_HTML
}

def render(name, **ctx):
    tpl = TEMPLATES.get(name)
    if tpl is None:
        return "Template not found", 500
    return render_template_string(tpl, **ctx)

from flask import render_template, abort

def render(name, **ctx):
    # if a real file exists in templates/, prefer that
    tpl_name = f"{name}.html"
    try:
        # try filesystem templates (templates/<name>.html)
        return render_template(tpl_name, **ctx)
    except Exception:
        # fallback to in-memory template registry if present
        tpl = TEMPLATES.get(name)
        if tpl:
            return render_template_string(tpl, **ctx)
        # final fallback
        abort(500, description="Template not found: " + name)

# ---------- Routes ----------
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    db = get_db()
    sql = "SELECT * FROM laptops"
    params = []
    where = []
    if status:
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("(sku LIKE ? OR brand LIKE ? OR model LIKE ? OR serial LIKE ?)")
        v = f"%{q}%"
        params.extend([v, v, v, v])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 1000"
    items = db.execute(sql, params).fetchall()
    return render("index", items=items, q=q, qstatus=status)

# ----- Auth -----
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username")
        p = request.form.get("password")
        db = get_db()
        r = db.execute("SELECT id, username, password_hash, role FROM users WHERE username=?", (u,)).fetchone()
        if r and verify_password(p, r["password_hash"]):
            user = User(r["id"], r["username"], r["role"])
            login_user(user)
            flash("Logged in", "success")
            return redirect(url_for("index"))
        flash("Invalid username or password", "danger")
        return redirect(url_for("login"))
    return render("login")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "success")
    return redirect(url_for("index"))

# ----- CRUD -----
@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    db = get_db()
    if request.method == "POST":
        data = dict(
            sku=request.form.get("sku").strip(),
            brand=(request.form.get("brand") or "").strip(),
            model=(request.form.get("model") or "").strip(),
            serial=(request.form.get("serial") or "").strip(),
            cpu=(request.form.get("cpu") or "").strip(),
            ram_gb=request.form.get("ram_gb") or None,
            storage=(request.form.get("storage") or "").strip(),
            initial_price=request.form.get("initial_price") or None,
            selling_price=request.form.get("selling_price") or None,
            location=(request.form.get("location") or "").strip(),
            status=request.form.get("status") or "in_stock",
            notes=(request.form.get("notes") or "").strip(),
            date_received=datetime.utcnow().isoformat()
        )
        db.execute("""
            INSERT INTO laptops (sku,brand,model,serial,cpu,ram_gb,storage,initial_price,selling_price,location,status,notes,date_received)
            VALUES (:sku,:brand,:model,:serial,:cpu,:ram_gb,:storage,:initial_price,:selling_price,:location,:status,:notes,:date_received)
        """, data)
        db.commit()
        flash("Laptop added", "success")
        return redirect(url_for("index"))
    return render("form", item=None)

@app.route("/edit/<int:item_id>", methods=["GET", "POST"])
@login_required
def edit(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM laptops WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return "Not found", 404
    if request.method == "POST":
        db.execute("""
            UPDATE laptops SET sku=?,brand=?,model=?,serial=?,cpu=?,ram_gb=?,storage=?,initial_price=?,selling_price=?,location=?,status=?,notes=?
            WHERE id=?
        """, (
            (request.form.get("sku") or "").strip(),
            (request.form.get("brand") or "").strip(),
            (request.form.get("model") or "").strip(),
            (request.form.get("serial") or "").strip(),
            (request.form.get("cpu") or "").strip(),
            request.form.get("ram_gb") or None,
            (request.form.get("storage") or "").strip(),
            request.form.get("initial_price") or None,
            request.form.get("selling_price") or None,
            (request.form.get("location") or "").strip(),
            request.form.get("status") or "in_stock",
            (request.form.get("notes") or "").strip(),
            item_id
        ))
        db.commit()
        flash("Saved", "success")
        return redirect(url_for("index"))
    return render("form", item=row)

@app.route("/toggle_out/<int:item_id>", methods=["POST"])
@login_required
def toggle_out(item_id):
    db = get_db()
    row = db.execute("SELECT status FROM laptops WHERE id=?", (item_id,)).fetchone()
    if not row:
        return "Not found", 404
    new_status = "in_stock" if row["status"] == "sold" else "sold"
    date_sold = datetime.utcnow().isoformat() if new_status == "sold" else None
    db.execute("UPDATE laptops SET status=?, date_sold=? WHERE id=?", (new_status, date_sold, item_id))
    db.commit()
    return redirect(url_for("index"))

@app.route("/delete/<int:item_id>", methods=["POST"])
@login_required
def delete(item_id):
    db = get_db()
    db.execute("DELETE FROM laptops WHERE id=?", (item_id,))
    db.commit()
    flash("Deleted", "success")
    return redirect(url_for("index"))

# ----- CSV export/import -----
@app.route("/export")
@login_required
def export_csv():
    db = get_db()
    rows = db.execute("SELECT * FROM laptops ORDER BY created_at DESC").fetchall()
    si = io.StringIO()
    writer = csv.writer(si)
    header = ["id","sku","brand","model","serial","cpu","ram_gb","storage","initial_price","selling_price","location","status","notes","date_received","date_sold","created_at"]
    writer.writerow(header)
    for r in rows:
        writer.writerow([r[h] for h in header])
    mem = io.BytesIO()
    mem.write(si.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="laptops.csv")

@app.route("/import_csv", methods=["POST"])
@login_required
@role_required("admin")
def import_csv():
    f = request.files.get("file")
    if not f:
        flash("No file uploaded", "danger")
        return redirect(url_for("index"))
    try:
        stream = io.StringIO(f.stream.read().decode("utf-8", errors="replace"))
        reader = csv.DictReader(stream)
        db = get_db()
        count = 0
        for row in reader:
            db.execute("""
                INSERT OR IGNORE INTO laptops
                (sku,brand,model,serial,cpu,ram_gb,storage,initial_price,selling_price,location,status,notes,date_received)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                row.get("sku"),
                row.get("brand"),
                row.get("model"),
                row.get("serial"),
                row.get("cpu"),
                int(row.get("ram_gb") or 0) if row.get("ram_gb") else None,
                row.get("storage"),
                float(row.get("initial_price") or 0) if row.get("initial_price") else None,
                float(row.get("selling_price") or 0) if row.get("selling_price") else None,
                row.get("location"),
                row.get("status") or "in_stock",
                row.get("notes"),
                datetime.utcnow().isoformat()
            ))
            count += 1
        db.commit()
        flash(f"Imported {count} rows", "success")
    except Exception as e:
        flash(f"CSV import failed: {e}", "danger")
    return redirect(url_for("index"))

# ----- JSON API -----
@app.route("/api/items", methods=["GET","POST"])
def api_items():
    db = get_db()
    if request.method == "GET":
        rows = db.execute("SELECT * FROM laptops ORDER BY created_at DESC LIMIT 1000").fetchall()
        return jsonify([dict(r) for r in rows])
    else:
        payload = request.json or {}
        if "sku" not in payload:
            return jsonify({"error":"sku required"}), 400
        db.execute("""
            INSERT INTO laptops (sku,brand,model,serial,cpu,ram_gb,storage,initial_price,selling_price,location,status,notes,date_received)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            payload.get("sku"),
            payload.get("brand"),
            payload.get("model"),
            payload.get("serial"),
            payload.get("cpu"),
            payload.get("ram_gb"),
            payload.get("storage"),
            payload.get("initial_price"),
            payload.get("selling_price"),
            payload.get("location"),
            payload.get("status","in_stock"),
            payload.get("notes"),
            datetime.utcnow().isoformat()
        ))
        db.commit()
        return jsonify({"ok":True}), 201

# ----- Dashboard / stats for Chart.js -----
@app.route("/dashboard")
@login_required
def dashboard():
    return render("dashboard")

@app.route("/api/stats")
@login_required
def api_stats():
    db = get_db()
    rows = db.execute("SELECT status, COUNT(*) as cnt, SUM(IFNULL(selling_price,0)) as val FROM laptops GROUP BY status").fetchall()
    labels = [r["status"] or "unknown" for r in rows]
    counts = [r["cnt"] for r in rows]
    values = [float(r["val"] or 0) for r in rows]
    return jsonify({"labels": labels, "counts": counts, "values": values})

# ----- Barcode and QR generation -----
@app.route("/barcode/<sku>")
@login_required
def gen_barcode(sku):
    safe = "".join(c for c in sku if c.isalnum() or c in "-_.")
    filename = f"barcode_{safe}.png"
    path = os.path.join(GENERATED_DIR, filename)
    if not os.path.exists(path):
        try:
            code = barcode.get('code128', sku, writer=ImageWriter())
            # python-barcode saves path without extension, pass base path
            base_no_ext = os.path.splitext(path)[0]
            code.save(base_no_ext)
            # ensure png extension
            if not os.path.exists(path) and os.path.exists(base_no_ext + ".png"):
                path = base_no_ext + ".png"
        except Exception as e:
            return f"Barcode error: {e}", 500
    return send_file(path, mimetype="image/png")

@app.route("/qrcode/<data>")
@login_required
def gen_qr(data):
    safe = "".join(c for c in data if c.isalnum() or c in "-_.")
    filename = f"qr_{safe}.png"
    path = os.path.join(GENERATED_DIR, filename)
    if not os.path.exists(path):
        img = qrcode.make(data)
        img.save(path)
    return send_file(path, mimetype="image/png")

# ----- Splash -----
@app.route("/splash")
def splash():
    return render("splash")

# ----- Static generated files route (if needed) -----
@app.route("/generated/<path:filename>")
def generated(filename):
    return send_from_directory(GENERATED_DIR, filename)

# ----- Backup system -----
def prune_backups(max_keep=14):
    try:
        files = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith("inventory_") and f.endswith(".db")],
            reverse=True
        )
        for f in files[max_keep:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, f))
            except Exception:
                pass
    except Exception:
        pass

def backup_db(max_keep=14):
    try:
        if os.path.exists(DB_PATH):
            stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            dst = os.path.join(BACKUP_DIR, f"inventory_{stamp}.db")
            shutil.copy2(DB_PATH, dst)
            prune_backups(max_keep=max_keep)
            app.logger.info("Backup saved: %s", dst)
    except Exception as e:
        app.logger.error("Backup failed: %s", e)
    # schedule next backup in 24h
    Timer(24*3600, backup_db, kwargs={"max_keep": max_keep}).start()

# ---------- Run ----------
def open_browser_later(host, port, delay=1.0):
    def _open():
        try:
            webbrowser.open(f"http://{host}:{port}/")
        except Exception:
            pass
    Timer(delay, _open).start()

if __name__ == "__main__":
    with app.app_context():
        init_db()

    # start backup thread (first run)
    backup_db()

    host = "127.0.0.1"
    port = 5000

    # open browser after short delay when running locally
    open_browser_later(host, port, delay=1.0)

    # Run dev server — for LAN/production use waitress or a WSGI server
    app.run(host=host, port=port, debug=False)