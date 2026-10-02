from __future__ import annotations

import hmac
import os
import secrets
import sqlite3
import time
import re
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .services import (
    PrinterServiceError,
    add_printer,
    discover_printers,
    list_printers,
    print_test_page,
    remove_printer,
    set_default_printer,
    validate_subnets,
)


ViewFunction = TypeVar("ViewFunction", bound=Callable[..., Any])
DATA_DIR = Path(os.environ.get("PRINT_SERVER_DATA_DIR", "/data"))
DATABASE_PATH = DATA_DIR / "manager.db"
SECRET_PATH = DATA_DIR / "secret_key"
LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def _load_secret_key() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    secret = secrets.token_bytes(32)
    temporary_path = SECRET_PATH.with_suffix(".tmp")
    temporary_path.write_bytes(secret)
    temporary_path.chmod(0o600)
    temporary_path.replace(SECRET_PATH)
    return secret


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
)


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def setup_complete() -> bool:
    with database() as connection:
        row = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return row is not None


def setting(key: str, default: str = "") -> str:
    with database() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return str(row["value"]) if row else default


def save_setting(key: str, value: str) -> None:
    with database() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return str(token)


app.jinja_env.globals["csrf_token"] = csrf_token


def queue_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return normalized[:127] or "IPP_Printer"


app.jinja_env.filters["queue_name"] = queue_name


@app.before_request
def protect_post_requests() -> None:
    if request.method != "POST":
        return
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(str(expected), supplied):
        abort(400, "Invalid or expired form token.")


def login_required(function: ViewFunction) -> ViewFunction:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not setup_complete():
            return redirect(url_for("onboarding"))
        if "user_id" not in session:
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return cast(ViewFunction, wrapped)


def _default_subnet() -> str:
    configured_uri = os.environ.get("PRINTER_URI", "")
    if configured_uri:
        from urllib.parse import urlsplit

        hostname = urlsplit(configured_uri).hostname
        if hostname:
            try:
                parts = hostname.split(".")
                if len(parts) == 4 and all(0 <= int(part) <= 255 for part in parts):
                    return ".".join(parts[:3]) + ".0/24"
            except ValueError:
                pass
    return ""


@app.route("/")
def index() -> Any:
    if not setup_complete():
        return redirect(url_for("onboarding"))
    if "user_id" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding() -> Any:
    if setup_complete():
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirmation", "")
        subnet = request.form.get("discovery_subnets", "").strip()

        errors: list[str] = []
        if not (3 <= len(username) <= 64):
            errors.append("Username must contain between 3 and 64 characters.")
        if len(password) < 10:
            errors.append("Password must contain at least 10 characters.")
        if password != confirmation:
            errors.append("Password confirmation does not match.")
        try:
            validate_subnets(subnet)
        except PrinterServiceError as error:
            errors.append(str(error))

        if not errors:
            try:
                with database() as connection:
                    cursor = connection.execute(
                        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                        (username, generate_password_hash(password)),
                    )
                    connection.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?)",
                        ("discovery_subnets", subnet),
                    )
                session.clear()
                session["user_id"] = cursor.lastrowid
                session["username"] = username
                session.permanent = True
                csrf_token()
                flash("Your print server is ready. Searching for printers now.", "success")
                return redirect(url_for("discover", scan="1"))
            except sqlite3.IntegrityError:
                errors.append("That username is already in use.")

        for error in errors:
            flash(error, "error")

    return render_template(
        "onboarding.html",
        title="Welcome",
        default_subnet=_default_subnet(),
    )


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if not setup_complete():
        return redirect(url_for("onboarding"))
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        client = request.remote_addr or "unknown"
        now = time.monotonic()
        attempts = [
            attempted
            for attempted in LOGIN_ATTEMPTS.get(client, [])
            if now - attempted < 300
        ]
        if len(attempts) >= 5:
            flash("Too many login attempts. Try again in five minutes.", "error")
            return render_template("login.html", title="Sign in"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with database() as connection:
            user = connection.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            LOGIN_ATTEMPTS.pop(client, None)
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session.permanent = True
            csrf_token()
            return redirect(url_for("dashboard"))

        attempts.append(now)
        LOGIN_ATTEMPTS[client] = attempts
        flash("Incorrect username or password.", "error")

    return render_template("login.html", title="Sign in")


@app.post("/logout")
@login_required
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard() -> Any:
    try:
        printers = list_printers()
        cups_online = True
    except PrinterServiceError as error:
        printers = []
        cups_online = False
        flash(f"Unable to read CUPS status: {error}", "error")
    return render_template(
        "dashboard.html",
        title="Overview",
        printers=printers,
        cups_online=cups_online,
    )


@app.route("/printers/discover", methods=["GET", "POST"])
@login_required
def discover() -> Any:
    subnets = setting("discovery_subnets", _default_subnet())
    printers = []
    warnings: list[str] = []
    should_scan = request.method == "POST" or request.args.get("scan") == "1"

    if request.method == "POST":
        subnets = request.form.get("discovery_subnets", "").strip()
        try:
            validate_subnets(subnets)
            save_setting("discovery_subnets", subnets)
        except PrinterServiceError as error:
            flash(str(error), "error")
            should_scan = False

    if should_scan:
        try:
            printers, warnings = discover_printers(subnets)
            if not printers:
                flash(
                    "No new IPP printers were found. You can enter an IPP URI manually.",
                    "info",
                )
        except PrinterServiceError as error:
            flash(f"Discovery failed: {error}", "error")

    configured_uris = {str(item["uri"]) for item in list_printers()}
    return render_template(
        "discover.html",
        title="Add printer",
        discoveries=printers,
        configured_uris=configured_uris,
        discovery_subnets=subnets,
        warnings=warnings,
        scanned=should_scan,
    )


@app.post("/printers")
@login_required
def create_printer() -> Any:
    try:
        add_printer(
            request.form.get("name", ""),
            request.form.get("uri", ""),
            request.form.get("info", ""),
            request.form.get("location", ""),
            set_default=request.form.get("set_default") == "on",
        )
        flash("Printer added and shared successfully.", "success")
        return redirect(url_for("dashboard"))
    except PrinterServiceError as error:
        flash(f"Unable to add printer: {error}", "error")
        return redirect(url_for("discover"))


@app.post("/printers/<name>/default")
@login_required
def make_default(name: str) -> Any:
    try:
        set_default_printer(name)
        flash(f"{name} is now the default printer.", "success")
    except PrinterServiceError as error:
        flash(f"Unable to set the default printer: {error}", "error")
    return redirect(url_for("dashboard"))


@app.post("/printers/<name>/test")
@login_required
def test_printer(name: str) -> Any:
    try:
        result = print_test_page(name)
        flash(result or f"Test page submitted to {name}.", "success")
    except PrinterServiceError as error:
        flash(f"Unable to print a test page: {error}", "error")
    return redirect(url_for("dashboard"))


@app.post("/printers/<name>/delete")
@login_required
def delete_printer(name: str) -> Any:
    try:
        remove_printer(name)
        flash(f"{name} was removed.", "success")
    except PrinterServiceError as error:
        flash(f"Unable to remove printer: {error}", "error")
    return redirect(url_for("dashboard"))


@app.get("/settings")
@login_required
def settings() -> Any:
    return render_template(
        "settings.html",
        title="Settings",
        discovery_subnets=setting("discovery_subnets", _default_subnet()),
    )


@app.post("/settings/discovery")
@login_required
def update_discovery_settings() -> Any:
    subnets = request.form.get("discovery_subnets", "").strip()
    try:
        validate_subnets(subnets)
        save_setting("discovery_subnets", subnets)
        flash("Discovery settings saved.", "success")
    except PrinterServiceError as error:
        flash(str(error), "error")
    return redirect(url_for("settings"))


@app.get("/api/health")
def health() -> Any:
    try:
        list_printers()
    except PrinterServiceError as error:
        return jsonify({"status": "error", "cups": str(error)}), 503
    return jsonify({"status": "ok"})


initialize_database()
