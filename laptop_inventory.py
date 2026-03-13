# laptop_inventory.py
"""
Laptop Inventory app (Flask + SQLite)
Includes: bcrypt auth, CSV import, Chart API, barcode/qrcode endpoints, backups, splash screen.
Drop this file into your project root (next to static/ templates/ inventory.db).
Run: python laptop_inventory.py
"""

import os
import sys
import re
import sqlite3
import shutil
from datetime import datetime
from threading import Timer
import webbrowser
import io
import csv

from flask import (
    Flask, g, render_template_string, request, redirect, url_for,
    send_file, jsonify, flash, send_from_directory, render_template, abort
)

# Optional external libs — install via pip
# pip install bcrypt python-barcode qrcode pillow
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

# ---------- SKU generator ----------
def generate_sku(prefix="AWK", width=4, db=None):
    """
    Generate a new unique SKU using prefix + zero-padded number.
    Example: AWK0001, AWK0002, ...
    """
    if db is None:
        db = get_db()

    like_pattern = f"{prefix}%"
    rows = db.execute("SELECT sku FROM laptops WHERE sku LIKE ?", (like_pattern,)).fetchall()

    max_n = 0
    rx = re.compile(rf"^{re.escape(prefix)}0*([0-9]+)$", re.IGNORECASE)
    for r in rows:
        s = r["sku"] or ""
        m = rx.match(s)
        if m:
            try:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
            except Exception:
                pass

    candidate_n = max_n + 1
    attempts = 0
    while attempts < 10000:
        candidate = f"{prefix}{str(candidate_n).zfill(width)}"
        existing = db.execute("SELECT 1 FROM laptops WHERE sku = ?", (candidate,)).fetchone()
        if not existing:
            return candidate
        candidate_n += 1
        attempts += 1

    raise RuntimeError("Unable to generate unique SKU")

# ---------- Templates (in-memory fallback) ----------
# You can keep templates on disk (templates/*.html). render() will try filesystem first.
BASE_HTML = """..."""  # shortened to keep file smaller here; real content should match your templates or you may rely on filesystem templates
# (You likely already have templates/ files. The fallback strings are not required.)

# ---------- Render helper (prefer filesystem templates) ----------
def render(name, **ctx):
    tpl_name = f"{name}.html"
    try:
        # prefer real template files in templates/ directory
        return render_template(tpl_name, **ctx)
    except Exception:
        # fallback to any in-memory template strings if present
        tpl = globals().get(f"{name.upper()}_HTML") or globals().get(name.upper() + "_HTML")
        if tpl:
            return render_template_string(tpl, **ctx)
        abort(500, description="Template not found: " + name)

# ---------- Routes ----------
@app.route("/")
@login_required
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
    return redirect(url_for("login"))

# ----- CRUD -----
@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    db = get_db()
    if request.method == "POST":
        sku_input = (request.form.get("sku") or "").strip()
        if not sku_input:
            sku_input = generate_sku(prefix="AWK", width=4, db=db)

        data = dict(
            sku=sku_input,
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
            row_sku = (row.get("sku") or "").strip()
            if not row_sku:
                row_sku = generate_sku(prefix="AWK", width=4, db=db)

            db.execute("""
                INSERT OR IGNORE INTO laptops
                (sku,brand,model,serial,cpu,ram_gb,storage,initial_price,selling_price,location,status,notes,date_received)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                row_sku,
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
        # generate SKU if missing
        sku_val = (payload.get("sku") or "").strip()
        if not sku_val:
            sku_val = generate_sku(prefix="AWK", width=4, db=db)
        db.execute("""
            INSERT INTO laptops (sku,brand,model,serial,cpu,ram_gb,storage,initial_price,selling_price,location,status,notes,date_received)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sku_val,
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
            base_no_ext = os.path.splitext(path)[0]
            code.save(base_no_ext)
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
def open_browser_later(host, port, delay=1.0, path="/login"):
    def _open():
        try:
            webbrowser.open(f"http://{host}:{port}{path}")
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

    # open browser after short delay when running locally (open login)
    open_browser_later(host, port, delay=1.0, path="/login")

    # Run dev server — for LAN/production use waitress or a WSGI server
    app.run(host=host, port=port, debug=False)