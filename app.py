from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hmac
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH as DOCX_ALIGN
from docx.image.exceptions import InvalidImageStreamError, UnrecognizedImageError
from docx.shared import Inches as DocxInches, Pt as DocxPt
from email_validator import EmailNotValidError, validate_email
from flask import (
    Flask, flash, jsonify, redirect, render_template, request,
    send_file, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_DATA_LABEL_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt
from pdf_export import generate_pdf

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-local-dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") == "production"
DB_PATH = BASE_DIR / "slidecraft.db"
IMAGE_CACHE_DIR = BASE_DIR / ".cache" / "unsplash"
FIREFLY_CACHE_DIR = BASE_DIR / ".cache" / "firefly"
MAX_SLIDES = 30
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_FIREFLY_IMAGE_BYTES = 16 * 1024 * 1024
GENERATION_STATUS = {}
GENERATION_STATUS_LOCK = threading.Lock()
FIREFLY_JOBS = {}
FIREFLY_JOBS_LOCK = threading.Lock()
FIREFLY_TOKEN_CACHE = {"token": "", "expires_at": 0}
FIREFLY_TOKEN_LOCK = threading.Lock()
FIREFLY_IMAGE_SIZES = {
    "16:9": {"width": 2688, "height": 1536},
    "4:3": {"width": 2304, "height": 1792},
    "1:1": {"width": 2048, "height": 2048},
}
SLIDE_LAYOUTS = {
    "cover", "split", "image_left", "full_image", "chart",
    "diagram", "comparison", "metrics", "conclusion",
}
SELECTABLE_LAYOUTS = (
    "split", "image_left", "full_image", "chart", "diagram",
    "comparison", "metrics",
)
CHART_TYPES = {"bar", "line", "pie", "doughnut", "comparison"}
DIAGRAM_TYPES = {"flow", "timeline", "comparison", "hierarchy"}
oauth = OAuth(app)


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def provider_configured(provider):
    if provider == "google":
        return bool(os.getenv("GOOGLE_CLIENT_ID", "").strip() and os.getenv("GOOGLE_CLIENT_SECRET", "").strip())
    if provider == "facebook":
        return bool(os.getenv("FACEBOOK_CLIENT_ID", "").strip() and os.getenv("FACEBOOK_CLIENT_SECRET", "").strip())
    return False


def register_oauth_clients():
    google_client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if google_client_id and google_client_secret:
        oauth.register(
            name="google",
            client_id=google_client_id,
            client_secret=google_client_secret,
            server_metadata_url=os.getenv(
                "GOOGLE_DISCOVERY_URL",
                "https://accounts.google.com/.well-known/openid-configuration",
            ),
            client_kwargs={"scope": "openid email profile"},
        )

    facebook_client_id = os.getenv("FACEBOOK_CLIENT_ID", "").strip()
    facebook_client_secret = os.getenv("FACEBOOK_CLIENT_SECRET", "").strip()
    if facebook_client_id and facebook_client_secret:
        oauth.register(
            name="facebook",
            client_id=facebook_client_id,
            client_secret=facebook_client_secret,
            access_token_url="https://graph.facebook.com/v19.0/oauth/access_token",
            authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
            api_base_url="https://graph.facebook.com/v19.0/",
            client_kwargs={"scope": "email,public_profile"},
            userinfo_endpoint="me?fields=id,name,email,picture.type(large)",
        )


register_oauth_clients()


# ---------- Database and authentication ----------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
        if not columns:
            db.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT,
                    auth_provider TEXT NOT NULL DEFAULT 'email',
                    provider_user_id TEXT,
                    avatar_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            table_info = db.execute("PRAGMA table_info(users)").fetchall()
            password_hash_col = next((col for col in table_info if col[1] == "password_hash"), None)
            if password_hash_col and password_hash_col[3] == 1:
                db.execute("ALTER TABLE users RENAME TO users_old")
                db.execute("""
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT,
                        auth_provider TEXT NOT NULL DEFAULT 'email',
                        provider_user_id TEXT,
                        avatar_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                db.execute(
                    """
                    INSERT INTO users (id, name, email, password_hash, auth_provider, provider_user_id, avatar_url, created_at)
                    SELECT id, name, email, password_hash, auth_provider, provider_user_id, avatar_url, created_at
                    FROM users_old
                    """
                )
                db.execute("DROP TABLE users_old")

        columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
        for column_name in ["auth_provider", "provider_user_id", "avatar_url"]:
            if column_name not in columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {column_name} TEXT")

        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_nocase "
            "ON users(email COLLATE NOCASE)"
        )
        db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                topic TEXT NOT NULL,
                slide_count INTEGER NOT NULL,
                slides_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        db.execute("UPDATE users SET password_hash = NULL WHERE password_hash = ''")
        db.execute("UPDATE users SET auth_provider = 'email' WHERE auth_provider IS NULL OR auth_provider = ''")
        db.execute("UPDATE users SET provider_user_id = NULL WHERE provider_user_id = ''")
        db.execute("UPDATE users SET avatar_url = NULL WHERE avatar_url = ''")
        db.commit()


init_db()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with db_connect() as db:
        row = db.execute(
            "SELECT id, name, email, auth_provider, avatar_url, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


@app.context_processor
def inject_user():
    return {"current_user": current_user(), "csrf_token": csrf_token}


@app.before_request
def enforce_csrf_tokens():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    allowed_endpoints = {
        "signup",
        "login",
        "account",
        "api_generate",
        "api_analyze",
        "api_compare",
        "api_optimize",
        "api_export",
        "api_export_pdf",
        "api_export_word",
    }
    if request.endpoint not in allowed_endpoints:
        return None
    sent_token = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRFToken")
        or request.headers.get("X-CSRF-Token")
    )
    expected_token = session.get("csrf_token")
    if not expected_token or not sent_token or not hmac.compare_digest(str(sent_token), str(expected_token)):
        if request.path.startswith("/api/"):
            return jsonify(error="Your session expired. Refresh the page and try again."), 400
        flash("Your session expired. Please try again.", "error")
        return redirect(request.referrer or url_for("home"))
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in to open your workspace.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# ---------- Presentation helpers ----------

def normalize_slide(item):
    if not isinstance(item, dict):
        item = {}
    bullets = item.get("bullets", [])
    if not isinstance(bullets, list):
        bullets = []
    visual = item.get("visual", {})
    if not isinstance(visual, dict):
        visual = {}
    layout = str(visual.get("layout", "split")).lower()
    if layout not in SLIDE_LAYOUTS:
        layout = "split"
    asset = str(visual.get("image_asset", "")).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", asset):
        asset = ""
    image_url = valid_unsplash_url(visual.get("image_url"), {"images.unsplash.com"})
    image_source = valid_unsplash_url(
        visual.get("image_source"), {"unsplash.com", "www.unsplash.com"}
    )
    photographer_url = valid_unsplash_url(
        visual.get("photographer_url"), {"unsplash.com", "www.unsplash.com"}
    )
    unsplash_url = valid_unsplash_url(
        visual.get("unsplash_url"), {"unsplash.com", "www.unsplash.com"}
    )
    image_provider = str(visual.get("image_provider", "")).strip()
    if image_provider not in {"unsplash", "adobe_firefly"}:
        image_provider = ""
    return {
        "title": str(item.get("title", "Untitled slide")).strip()[:160] or "Untitled slide",
        "body": str(item.get("body", "")).strip()[:1000],
        "bullets": [str(x).strip()[:500] for x in bullets if str(x).strip()][:8],
        "notes": str(item.get("notes", "")).strip()[:3000],
        "visual": {
            "layout": layout,
            "description": str(visual.get("description", "")).strip()[:500],
            "image_query": str(visual.get("image_query", "")).strip()[:160],
            "image_asset": asset,
            "image_url": image_url,
            "image_alt": str(visual.get("image_alt", "")).strip()[:240],
            "image_credit": str(visual.get("image_credit", "")).strip()[:160],
            "image_provider": image_provider,
            "image_source": image_source,
            "photographer_url": photographer_url,
            "unsplash_url": unsplash_url,
        },
        "chart": normalize_chart(item.get("chart")),
        "diagram": normalize_diagram(item.get("diagram")),
    }


def normalize_signup_email(value):
    email = str(value or "").strip()
    if not email or len(email) > 254:
        raise EmailNotValidError("Enter a valid email address.")
    validated = validate_email(
        email,
        check_deliverability=False,
        allow_smtputf8=False,
    )
    normalized = validated.normalized.casefold()
    if len(normalized) > 254:
        raise EmailNotValidError("Enter a valid email address.")
    return normalized


def export_filename(title, extension, variant=None):
    normalized_title = unicodedata.normalize("NFKD", str(title or ""))
    ascii_title = normalized_title.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_title).strip("-").lower()
    slug = slug[:64].rstrip("-") or "presentation"
    safe_variant = re.sub(r"[^A-Za-z0-9-]+", "-", str(variant or "")).strip("-")[:24]
    suffix = f"_{safe_variant}" if safe_variant else ""
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    random_suffix = secrets.token_hex(4)
    return f"slidecraft_{slug}{suffix}_{timestamp}_{random_suffix}.{extension}"


def generate_word_document(slides, title, image_path_getter):
    document = Document()
    section = document.sections[0]
    section.top_margin = DocxInches(0.75)
    section.bottom_margin = DocxInches(0.75)
    section.left_margin = DocxInches(0.85)
    section.right_margin = DocxInches(0.85)

    normal_style = document.styles["Normal"]
    normal_style.font.name = "Aptos"
    normal_style.font.size = DocxPt(11)
    normal_style.paragraph_format.space_after = DocxPt(7)
    normal_style.paragraph_format.line_spacing = 1.15
    for style_name in ("Title", "Heading 1", "Heading 2"):
        document.styles[style_name].font.name = "Aptos Display"

    document.core_properties.title = title
    document.core_properties.author = "SlideCraft Studio"

    cover_title = document.add_paragraph(style="Title")
    cover_title.alignment = DOCX_ALIGN.CENTER
    cover_title.paragraph_format.space_before = DocxPt(140)
    cover_title.add_run(title)
    generated_date = document.add_paragraph()
    generated_date.alignment = DOCX_ALIGN.CENTER
    generated_date.add_run(f"Generated {datetime.now().astimezone().strftime('%B %d, %Y')}")

    for index, slide in enumerate(slides):
        document.add_page_break()
        document.add_heading(slide["title"], level=1)
        if slide["body"]:
            document.add_paragraph(slide["body"])
        for bullet in slide["bullets"]:
            document.add_paragraph(bullet, style="List Bullet")

        visual = slide["visual"]
        if visual.get("description"):
            paragraph = document.add_paragraph()
            paragraph.add_run("Visual description: ").bold = True
            paragraph.add_run(visual["description"])

        image_path = image_path_getter(visual.get("image_asset"))
        if image_path and Path(image_path).is_file():
            try:
                document.add_picture(str(image_path), width=DocxInches(5.8))
            except (OSError, ValueError, InvalidImageStreamError, UnrecognizedImageError) as exc:
                app.logger.warning(
                    "Skipping unavailable image in slide %s (%s).",
                    index + 1,
                    type(exc).__name__,
                )

        if slide["chart"]:
            chart = slide["chart"]
            document.add_heading(chart["title"], level=2)
            chart_description = document.add_paragraph()
            chart_description.add_run("Chart type: ").bold = True
            chart_description.add_run(chart["type"])
            for series in chart["series"]:
                units = f" {chart['units']}" if chart["units"] else ""
                values = ", ".join(
                    f"{label}: {value:g}{units}"
                    for label, value in zip(chart["labels"], series["values"])
                )
                document.add_paragraph(f"{series['name']}: {values}", style="List Bullet")
            source = chart.get("source")
            if source:
                paragraph = document.add_paragraph()
                paragraph.add_run("Source: ").bold = True
                paragraph.add_run(source)

        if slide["diagram"]:
            diagram = slide["diagram"]
            document.add_heading(f"{diagram['type'].title()} diagram", level=2)
            for item in diagram["items"]:
                document.add_paragraph(item, style="List Bullet")
            if diagram.get("caption"):
                document.add_paragraph(diagram["caption"])

        credit = visual.get("image_credit")
        if credit:
            attribution = document.add_paragraph()
            attribution.add_run("Image attribution: ").bold = True
            attribution.add_run(f"Photo by {credit}")
            if visual.get("image_source"):
                attribution.add_run(f" — {visual['image_source']}")
            elif visual.get("unsplash_url"):
                attribution.add_run(f" — {visual['unsplash_url']}")

        if slide["notes"]:
            document.add_heading("Speaker notes", level=2)
            document.add_paragraph(slide["notes"])

    output = io.BytesIO()
    document.save(output)
    output.seek(0)
    return output


def valid_unsplash_url(value, allowed_hosts):
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        return ""
    return urlunsplit(parsed)[:1000]


class FireflyServiceError(Exception):
    pass


def firefly_configuration():
    client_id = os.getenv("ADOBE_FIREFLY_CLIENT_ID", "").strip()
    client_secret = os.getenv("ADOBE_FIREFLY_CLIENT_SECRET", "").strip()
    scopes = os.getenv(
        "ADOBE_FIREFLY_SCOPES", "openid,AdobeID,firefly_api,ff_apis"
    ).strip()
    return client_id, client_secret, scopes


def firefly_access_token():
    client_id, client_secret, scopes = firefly_configuration()
    if not client_id or not client_secret or not scopes:
        raise FireflyServiceError("Adobe Firefly is not configured for this application.")

    with FIREFLY_TOKEN_LOCK:
        if FIREFLY_TOKEN_CACHE["token"] and FIREFLY_TOKEN_CACHE["expires_at"] > time.time() + 60:
            return FIREFLY_TOKEN_CACHE["token"]

    try:
        response = requests.post(
            "https://ims-na1.adobelogin.com/ims/token/v3",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scopes,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        app.logger.warning("Adobe IMS token request failed (HTTP %s).", status_code or "unavailable")
        raise FireflyServiceError("Adobe authentication is unavailable. Check your Firefly configuration.") from exc
    except (ValueError, TypeError) as exc:
        app.logger.warning("Adobe IMS returned an invalid token response.")
        raise FireflyServiceError("Adobe authentication returned an invalid response.") from exc

    if not isinstance(payload, dict) or not payload.get("access_token"):
        app.logger.warning("Adobe IMS did not return an access token.")
        raise FireflyServiceError("Adobe authentication did not return an access token.")
    token = str(payload["access_token"])
    try:
        expires_in = max(60, int(payload.get("expires_in", 3600)))
    except (TypeError, ValueError):
        expires_in = 3600
    with FIREFLY_TOKEN_LOCK:
        FIREFLY_TOKEN_CACHE.update(token=token, expires_at=time.time() + expires_in)
    return token


def firefly_headers():
    client_id, _client_secret, _scopes = firefly_configuration()
    return {
        "x-api-key": client_id,
        "Authorization": f"Bearer {firefly_access_token()}",
        "Content-Type": "application/json",
    }


def firefly_cache_path(asset):
    if not re.fullmatch(r"[a-f0-9]{64}", str(asset or "")):
        return None
    for extension in ("png", "jpg"):
        path = FIREFLY_CACHE_DIR / f"{asset}.{extension}"
        if path.is_file():
            return path
    return None


def cache_firefly_output(image_url):
    try:
        parsed = urlsplit(str(image_url or ""))
        port = parsed.port
    except ValueError as exc:
        raise FireflyServiceError("Adobe returned an invalid image URL.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise FireflyServiceError("Adobe returned an unsupported image URL.")
    try:
        with requests.get(
            image_url, stream=True, timeout=20, allow_redirects=False
        ) as response:
            response.raise_for_status()
            chunks = []
            total_size = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total_size += len(chunk)
                if total_size > MAX_FIREFLY_IMAGE_BYTES:
                    raise FireflyServiceError("The generated image exceeds the download size limit.")
                chunks.append(chunk)
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        app.logger.warning("Adobe Firefly image download failed (HTTP %s).", status_code or "unavailable")
        raise FireflyServiceError("The generated image could not be downloaded. Please try again.") from exc

    image_bytes = b"".join(chunks)
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = "png"
    elif image_bytes.startswith(b"\xff\xd8\xff"):
        extension = "jpg"
    else:
        raise FireflyServiceError("Adobe returned an unsupported image format.")
    asset = hashlib.sha256(image_bytes).hexdigest()
    FIREFLY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIREFLY_CACHE_DIR / f"{asset}.{extension}"
    if not path.is_file():
        temporary_path = FIREFLY_CACHE_DIR / (
            f"{asset}.{secrets.token_hex(6)}.{extension}.tmp"
        )
        try:
            temporary_path.write_bytes(image_bytes)
            temporary_path.replace(path)
        except OSError as exc:
            app.logger.warning("Adobe Firefly image could not be cached (%s).", type(exc).__name__)
            raise FireflyServiceError("The generated image could not be saved locally.") from exc
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                app.logger.warning("Temporary Firefly image could not be removed (%s).", type(exc).__name__)
    return asset


def unsplash_referral_url(value):
    parts = urlsplit(value)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["utm_source"] = "slidecraft_studio"
    query["utm_medium"] = "referral"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def normalize_chart(chart):
    if chart is None:
        return None
    if not isinstance(chart, dict):
        raise ValueError("Chart data must be an object or null.")
    chart_type = str(chart.get("type", "")).lower()
    labels = chart.get("labels")
    series = chart.get("series")
    if chart_type not in CHART_TYPES:
        raise ValueError("Chart type must be bar, line, pie, doughnut, or comparison.")
    if not isinstance(labels, list) or not 2 <= len(labels) <= 12:
        raise ValueError("Charts need between 2 and 12 category labels.")
    if not isinstance(series, list) or not 1 <= len(series) <= 6:
        raise ValueError("Charts need between 1 and 6 data series.")
    clean_labels = [str(label).strip()[:80] for label in labels]
    if any(not label for label in clean_labels):
        raise ValueError("Chart labels cannot be empty.")
    clean_series = []
    for entry in series:
        if not isinstance(entry, dict) or not isinstance(entry.get("values"), list):
            raise ValueError("Each chart series needs a name and a values list.")
        values = entry["values"]
        if len(values) != len(clean_labels):
            raise ValueError("Each chart series must have one value per label.")
        if any(isinstance(value, bool) for value in values):
            raise ValueError("Chart values must be numeric, not boolean.")
        try:
            clean_values = [float(value) for value in values]
        except (TypeError, ValueError):
            raise ValueError("Chart values must be numeric.") from None
        if any(not math.isfinite(value) for value in clean_values):
            raise ValueError("Chart values must be finite numbers.")
        clean_series.append({
            "name": str(entry.get("name", "")).strip()[:80] or "Series",
            "values": clean_values,
        })
    if chart_type in {"pie", "doughnut"}:
        if (
            len(clean_series) != 1
            or any(value < 0 for value in clean_series[0]["values"])
            or sum(clean_series[0]["values"]) <= 0
        ):
            raise ValueError("Pie and doughnut charts need one series with non-negative values and a positive total.")
    illustrative_value = chart.get("illustrative", False)
    if not isinstance(illustrative_value, bool):
        raise ValueError("Chart illustrative must be true or false.")
    illustrative = illustrative_value
    source = str(chart.get("source", "")).strip()[:300]
    if not illustrative and not source:
        raise ValueError("Add a source for factual chart data, or mark the data as illustrative.")
    return {
        "type": chart_type,
        "title": str(chart.get("title", "")).strip()[:140] or "Data overview",
        "labels": clean_labels,
        "series": clean_series,
        "units": str(chart.get("units", "")).strip()[:40],
        "source": source or ("Illustrative data" if illustrative else ""),
        "illustrative": illustrative,
    }


def normalize_diagram(diagram):
    if diagram is None:
        return None
    if not isinstance(diagram, dict):
        raise ValueError("Diagram data must be an object or null.")
    diagram_type = str(diagram.get("type", "")).lower()
    items = diagram.get("items")
    if diagram_type not in DIAGRAM_TYPES:
        raise ValueError("Diagram type must be flow, timeline, comparison, or hierarchy.")
    if not isinstance(items, list) or not 2 <= len(items) <= 8:
        raise ValueError("Diagrams need between 2 and 8 labeled items.")
    clean_items = [str(item).strip()[:120] for item in items]
    if any(not item for item in clean_items):
        raise ValueError("Diagram labels cannot be empty.")
    return {
        "type": diagram_type,
        "items": clean_items,
        "caption": str(diagram.get("caption", "")).strip()[:180],
    }


def choose_slide_layout(slide, index, count):
    if index == 0:
        return "cover"
    if slide["chart"]:
        return "chart"
    if slide["diagram"]:
        return "diagram"
    if index == count - 1:
        return "conclusion"
    title = slide["title"].lower()
    if any(word in title for word in ("compare", "versus", "trade-off", "pros and cons")):
        return "comparison"
    if any(word in title for word in ("metric", "kpi", "measure", "impact")):
        return "metrics"
    requested_layout = slide["visual"]["layout"]
    if requested_layout in {"split", "image_left", "full_image", "comparison", "metrics"}:
        return requested_layout
    return "image_left" if index % 3 == 0 else "split"


def unsplash_cache_path(asset):
    if not re.fullmatch(r"[a-f0-9]{64}", str(asset or "")):
        return None
    return IMAGE_CACHE_DIR / f"{asset}.jpg"


def fetch_unsplash_image(query):
    access_key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    if not access_key:
        return None
    query_id = hashlib.sha256(query.casefold().encode("utf-8")).hexdigest()
    selection_path = IMAGE_CACHE_DIR / f"search-{query_id}.json"
    headers = {
        "Authorization": f"Client-ID {access_key}",
        "Accept-Version": "v1",
    }
    try:
        if selection_path.is_file():
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
        else:
            response = requests.get(
                "https://api.unsplash.com/search/photos",
                headers=headers,
                params={
                    "query": query,
                    "orientation": "landscape",
                    "content_filter": "high",
                    "order_by": "relevant",
                    "per_page": 10,
                },
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results", []) if isinstance(payload, dict) else []
            selection = None
            if isinstance(results, list):
                for photo in results:
                    if not isinstance(photo, dict):
                        continue
                    width = photo.get("width")
                    height = photo.get("height")
                    urls = photo.get("urls")
                    links = photo.get("links")
                    user = photo.get("user")
                    if not all(isinstance(value, dict) for value in (urls, links, user)):
                        continue
                    try:
                        landscape = float(width) / float(height) >= 1.25
                    except (TypeError, ValueError, ZeroDivisionError):
                        landscape = False
                    image_url = valid_unsplash_url(urls.get("regular"), {"images.unsplash.com"})
                    download_location = valid_unsplash_url(
                        links.get("download_location"), {"api.unsplash.com"}
                    )
                    profile_url = valid_unsplash_url(
                        (user.get("links") or {}).get("html"), {"unsplash.com", "www.unsplash.com"}
                    )
                    photo_url = valid_unsplash_url(
                        links.get("html"), {"unsplash.com", "www.unsplash.com"}
                    )
                    if not landscape or not image_url or not download_location or not profile_url or not photo_url:
                        continue
                    photo_id = str(photo.get("id", ""))
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", photo_id):
                        continue
                    selection = {
                        "asset": hashlib.sha256(photo_id.encode("utf-8")).hexdigest(),
                        "photo_id": photo_id,
                        "image_url": image_url,
                        "download_location": download_location,
                        "photo_url": unsplash_referral_url(photo_url),
                        "photographer": str(user.get("name") or "Unsplash photographer")[:160],
                        "photographer_url": unsplash_referral_url(profile_url),
                        "unsplash_url": "https://unsplash.com/?utm_source=slidecraft_studio&utm_medium=referral",
                        "alt": str(photo.get("alt_description") or photo.get("description") or query)[:240],
                    }
                    break
            if not selection:
                return None
            IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            selection_path.write_text(json.dumps(selection), encoding="utf-8")

        asset = str(selection.get("asset", ""))
        image_path = unsplash_cache_path(asset)
        image_url = valid_unsplash_url(selection.get("image_url"), {"images.unsplash.com"})
        download_location = valid_unsplash_url(
            selection.get("download_location"), {"api.unsplash.com"}
        )
        photographer_url = valid_unsplash_url(
            selection.get("photographer_url"), {"unsplash.com", "www.unsplash.com"}
        )
        photo_url = valid_unsplash_url(
            selection.get("photo_url"), {"unsplash.com", "www.unsplash.com"}
        )
        unsplash_url = valid_unsplash_url(
            selection.get("unsplash_url"), {"unsplash.com", "www.unsplash.com"}
        )
        if not image_path or not image_url or not download_location or not photographer_url or not photo_url or not unsplash_url:
            return None

        # Embedding a selected photo in a deck is a download-like use; notify Unsplash first.
        if not trigger_unsplash_download(download_location, access_key):
            return None

        if not image_path.is_file():
            with requests.get(image_url, stream=True, timeout=15, allow_redirects=False) as image_response:
                image_response.raise_for_status()
                content_type = image_response.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type not in {"image/jpeg", "image/jpg"}:
                    app.logger.warning("Unsplash returned an unsupported image type.")
                    return None
                chunks = []
                total_size = 0
                for chunk in image_response.iter_content(64 * 1024):
                    total_size += len(chunk)
                    if total_size > MAX_IMAGE_BYTES:
                        app.logger.warning("Unsplash image exceeded the local cache size limit.")
                        return None
                    chunks.append(chunk)
            image_bytes = b"".join(chunks)
            if not image_bytes.startswith(b"\xff\xd8\xff"):
                app.logger.warning("Unsplash CDN response was not a valid JPEG image.")
                return None
            image_path.write_bytes(image_bytes)

        return {
            "asset": asset,
            "image_url": image_url,
            "image_credit": selection["photographer"],
            "image_source": photo_url,
            "photographer_url": photographer_url,
            "unsplash_url": unsplash_url,
            "image_alt": selection["alt"],
        }
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        app.logger.warning("Unsplash image retrieval failed (HTTP %s).", status_code or "unavailable")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        app.logger.warning("Unsplash image selection could not be used (%s).", type(exc).__name__)
    except Exception:
        app.logger.exception("Unexpected Unsplash image processing failure.")
    return None


def cached_image_path(asset):
    path = unsplash_cache_path(asset)
    if path and path.is_file():
        return path
    return firefly_cache_path(asset)


def trigger_unsplash_download(download_location, access_key=None):
    key = access_key or os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    endpoint = valid_unsplash_url(download_location, {"api.unsplash.com"})
    if not key or not endpoint:
        return False
    try:
        response = requests.get(
            endpoint,
            headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        app.logger.warning("Unsplash download event failed (HTTP %s).", status_code or "unavailable")
        return False
    except Exception:
        app.logger.exception("Unexpected Unsplash download event failure.")
        return False


def enrich_slide_visuals(slides, topic):
    api_key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    photo_slides = []
    for index, slide in enumerate(slides):
        visual = slide["visual"]
        visual["layout"] = choose_slide_layout(slide, index, len(slides))
        if slide["chart"] or slide["diagram"]:
            continue
        photo_slides.append(slide)
    if not photo_slides:
        return "Visual slides use native charts and diagrams; no photos were needed."
    if not api_key:
        return "No slide photos added: UNSPLASH_ACCESS_KEY is not configured. Slides use native editable visuals."
    added = 0
    for slide in photo_slides:
        visual = slide["visual"]
        query = visual["image_query"] or f"{topic} {slide['title']} {visual['description']}".strip()
        image = fetch_unsplash_image(query[:160])
        if image:
            visual.update({
                "image_asset": image["asset"],
                "image_url": image["image_url"],
                "image_alt": image["image_alt"],
                "image_credit": image["image_credit"],
                "image_source": image["image_source"],
                "photographer_url": image["photographer_url"],
                "unsplash_url": image["unsplash_url"],
            })
            added += 1
    if added == len(photo_slides):
        return f"Added Unsplash photos to all {added} slides with photographer and Unsplash credits."
    return f"Added Unsplash photos to {added} of {len(photo_slides)} slides; the rest use editable native visuals."


def offline_slides(topic, count, audience="General audience", purpose="Presentation"):
    sections = [
        ("The big picture", ["Define the topic in one clear sentence.", "Explain why it matters to this audience.", "Introduce the central question."]),
        ("Key concepts", ["Explain the essential terms in plain language.", "Connect each concept to a practical example.", "Use a visual or diagram where possible."]),
        ("How it works", ["Present the process in logical steps.", "Explain the inputs, actions, and outcomes.", "Call out the most important detail."]),
        ("Real-world applications", ["Show where the topic is used today.", "Describe a realistic use case.", "Explain the value it creates."]),
        ("Benefits and trade-offs", ["Summarize the main advantages.", "Acknowledge limitations or risks.", "Explain when this approach is most useful."]),
        ("Next steps", ["Recap the most important takeaways.", "Suggest one action the audience can take.", "Invite questions and discussion."]),
    ]
    slides = []
    for i in range(count):
        if i == 0:
            title = f"Introducing {topic}"
            bullets = [
                f"What {topic} means and why it matters",
                f"The goal of this {purpose.lower()} for {audience.lower()}",
                "A simple roadmap of what we will cover",
            ]
        elif i == count - 1:
            title = "Key takeaways"
            bullets = [
                f"Remember the central idea behind {topic}.",
                "Focus on the evidence, practical value, and limitations.",
                "Questions, discussion, and next steps.",
            ]
        else:
            heading, bullets = sections[(i - 1) % len(sections)]
            title = f"{heading}: {topic}" if i < 4 else heading
        slide = {
            "title": title,
            "bullets": bullets,
            "notes": f"Presenter note: explain {title.lower()} with a concrete, verified example. Add reliable sources where factual claims or statistics are used.",
            "visual": {
                "description": f"An editorial visual that explains {title.lower()} for {topic}.",
                "image_query": f"{topic} {title} concept",
            },
        }
        slide = normalize_slide(slide)
        slide["visual"]["layout"] = choose_slide_layout(slide, i, count)
        slides.append(slide)
    return slides


def build_slide_prompt(topic, count, audience, purpose, tone):
    return f"""
Create exactly {count} complete, presentation-ready slides about: {topic}

Audience: {audience}
Purpose: {purpose}
Tone: {tone}

Return valid JSON only. Do not include Markdown fences or explanations.
Use this structure:
{{
  "slides": [
    {{
      "title": "Short, specific title",
      "body": "A 25-35 word explanation in 2-3 complete sentences that makes this slide understandable on its own.",
      "bullets": ["Point 1", "Point 2", "Point 3"],
      "notes": "Detailed presenter notes with context, a relevant example, and caveats where useful.",
      "visual": {{
        "description": "What the visual should communicate",
        "image_query": "Specific search phrase for a relevant photo",
        "layout": "split"
      }},
      "chart": null,
      "diagram": null
    }}
  ]
}}

Requirements:
- Return exactly {count} slides.
- Every slide must contain a title, a 25-35 word explanatory body, 3-4
  substantive key-point bullets, and useful presenter notes.
- Write the body as a short explanation, not a heading or a list of topics.
- Write each bullet as a clear, specific statement (about 8-14 words) that
  gives evidence, reasoning, a practical detail, or a supporting example.
- Make the body and bullets complementary rather than repeating each other.
- The opening slide must establish the topic's context, audience relevance,
  and presentation goal. The final slide must synthesize the main conclusion
  and give a useful next step or discussion question.
- Include concrete examples when they clarify the topic; do not invent facts,
  statistics, sources, research papers, or quotations.
- Do not repeat generic filler or reuse the same slide structure repeatedly.
- Use the notes to expand on the slide's explanation with a practical example,
  transition, or caveat. Add reliable-source guidance for factual claims
  without fabricating citations.
- Choose appropriate layouts: cover, split, image_left, full_image,
  chart, diagram, comparison, metrics, or conclusion.
- Use diagrams for useful processes, timelines, comparisons, or hierarchies.
- Do not invent statistics, sources, research papers, or quotations.
- If numerical data is illustrative, explicitly set "illustrative": true
  and label the chart as illustrative.
- If a chart uses factual data, include its source.
- Set "chart": null when reliable numerical data is unavailable.
- Set "diagram": null when a diagram adds no value.

Chart format:
{{
  "type": "bar",
  "title": "Chart title",
  "labels": ["A", "B"],
  "series": [{{"name": "Series", "values": [10, 20]}}],
  "units": "units",
  "source": "Source or Illustrative data",
  "illustrative": false
}}

Diagram format:
{{
  "type": "flow",
  "items": ["Step one", "Step two", "Outcome"],
  "caption": "Short caption"
}}
"""


def parse_generated_slides(raw_text, count):
    """Parse and validate model output before it can be used."""
    text = str(raw_text or "").strip()

    # Defensive cleanup if a model accidentally adds Markdown fences.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    parsed = json.loads(text)
    slides = parsed.get("slides", []) if isinstance(parsed, dict) else []

    if not isinstance(slides, list) or len(slides) != count:
        raise ValueError("The AI returned an unexpected number of slides.")

    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            raise ValueError(f"Slide {index} is not a valid slide object.")
        if not str(slide.get("title", "")).strip():
            raise ValueError(f"Slide {index} is missing a title.")
        body = str(slide.get("body", "")).strip()
        bullets = slide.get("bullets")
        notes = str(slide.get("notes", "")).strip()
        if len(body.split()) < 20:
            raise ValueError(f"Slide {index} is missing a detailed explanation.")
        if not isinstance(bullets, list) or not 3 <= len(bullets) <= 4:
            raise ValueError(f"Slide {index} needs 3-4 substantive key points.")
        if any(len(str(bullet).split()) < 6 for bullet in bullets):
            raise ValueError(f"Slide {index} contains an outline-only key point.")
        if len(notes.split()) < 20:
            raise ValueError(f"Slide {index} is missing useful presenter notes.")

    normalized = [normalize_slide(slide) for slide in slides]

    for index, slide in enumerate(normalized):
        slide["visual"]["layout"] = choose_slide_layout(
            slide, index, len(normalized)
        )

    return normalized


def ai_request_error(provider, exc, api_key):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    provider_message = ""
    if response is not None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            provider_message = str(error.get("message", "")).strip()
    if api_key:
        provider_message = provider_message.replace(api_key, "[redacted]")
    provider_message = re.sub(r"\s+", " ", provider_message)[:240]

    if status == 429:
        reason = f"{provider} rate limit or quota reached."
    elif status in {401, 403} or "api key not valid" in provider_message.lower():
        reason = f"{provider} rejected the API key or access permission."
    elif status in {400, 404}:
        reason = f"{provider} rejected the model or request (HTTP {status})."
    elif status and status >= 500:
        reason = f"{provider} is temporarily unavailable (HTTP {status})."
    elif isinstance(exc, requests.Timeout):
        reason = f"{provider} request timed out."
    elif isinstance(exc, requests.ConnectionError):
        reason = f"Could not connect to {provider}."
    else:
        reason = f"{provider} request failed" + (f" (HTTP {status})." if status else ".")

    return f"{reason} {provider_message}".strip()


def gemini_candidate_text(candidate):
    finish_reason = candidate.get("finishReason")
    if finish_reason == "MAX_TOKENS":
        raise ValueError("Gemini reached the response token limit.")
    if finish_reason and finish_reason != "STOP":
        raise ValueError(f"Gemini stopped before completing the response ({finish_reason}).")

    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    if not isinstance(parts, list):
        raise ValueError("Gemini returned an invalid content-parts list.")
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and not part.get("thought")
        and isinstance(part.get("text"), str)
    )
    if not text.strip():
        raise ValueError("Gemini returned no text content.")
    return text


def gemini_generate(topic, count, audience, purpose, tone):
    """Primary AI provider: Gemini."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None, "Gemini key is not configured."

    model = (
        os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
        or "gemini-3.5-flash-lite"
    )
    prompt = build_slide_prompt(topic, count, audience, purpose, tone)
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )

    try:
        response = requests.post(
            endpoint,
            headers={"x-goog-api-key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.35,
                    "maxOutputTokens": 12000,
                },
            },
            timeout=(8, 120),
        )
        response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            feedback = data.get("promptFeedback", {})
            block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
            reason = f" ({block_reason})" if block_reason else ""
            raise ValueError(f"Gemini returned no candidates{reason}.")
        raw_text = gemini_candidate_text(candidates[0])
        slides = parse_generated_slides(raw_text, count)

        return slides, "Gemini generated the presentation content."

    except requests.RequestException as exc:
        detail = ai_request_error("Gemini", exc, api_key)
        app.logger.warning(
            "Gemini generation request failed: %s",
            detail,
        )
        return None, detail

    except (ValueError, KeyError, IndexError, TypeError) as exc:
        detail = str(exc).strip()[:240]
        app.logger.warning(
            "Gemini returned invalid or incomplete slide data (%s).",
            type(exc).__name__,
        )
        return None, f"Gemini returned invalid or incomplete content. {detail}".strip()

    except Exception:
        app.logger.exception("Unexpected Gemini generation failure.")
        return None, "Gemini generation failed unexpectedly."


def groq_generate(topic, count, audience, purpose, tone):
    """Backup AI provider: Groq."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None, "Groq key is not configured."

    model = (
        os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
        or "openai/gpt-oss-20b"
    )
    prompt = build_slide_prompt(topic, count, audience, purpose, tone)

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You generate high-quality presentation content. "
                            "Follow the requested JSON structure exactly. "
                            "Return JSON only, without Markdown fences."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": 12000,
            },
            timeout=(8, 120),
        )
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ValueError("Groq returned a truncated or filtered response.")
        raw_text = choice["message"]["content"]
        slides = parse_generated_slides(raw_text, count)

        return slides, "Groq generated the presentation content."

    except requests.RequestException as exc:
        detail = ai_request_error("Groq", exc, api_key)
        app.logger.warning(
            "Groq generation request failed: %s",
            detail,
        )
        return None, detail

    except (ValueError, KeyError, IndexError, TypeError) as exc:
        detail = str(exc).strip()[:240]
        app.logger.warning(
            "Groq returned invalid or incomplete slide data (%s).",
            type(exc).__name__,
        )
        return None, f"Groq returned invalid or incomplete content. {detail}".strip()

    except Exception:
        app.logger.exception("Unexpected Groq generation failure.")
        return None, "Groq generation failed unexpectedly."


def generate_slides_with_fallback(
    topic, count, audience, purpose, tone, progress_callback=None,
    preferred_provider="automatic",
):
    """
    Use an explicitly selected provider, or preserve the automatic Gemini-first
    fallback behavior. Return (slides, provider, detail).
    """
    if preferred_provider not in {"gemini", "groq", "automatic"}:
        raise ValueError("Choose Gemini, Groq, or automatic provider selection.")

    if preferred_provider in {"gemini", "groq"}:
        configured = bool(os.getenv(f"{preferred_provider.upper()}_API_KEY", "").strip())
        if not configured:
            return None, preferred_provider, f"{preferred_provider.title()} key is not configured."
        if progress_callback:
            progress_callback(
                preferred_provider,
                "Creating your presentation content.",
            )
        generator = gemini_generate if preferred_provider == "gemini" else groq_generate
        slides, detail = generator(topic, count, audience, purpose, tone)
        if slides and progress_callback:
            progress_callback("visuals", "Preparing slide visuals.")
        return slides, preferred_provider, detail

    try:
        delay = float(os.getenv("AI_FALLBACK_DELAY_SECONDS", "20"))
    except (TypeError, ValueError):
        delay = 20.0

    delay = max(1.0, min(delay, 60.0))
    gemini_configured = bool(os.getenv("GEMINI_API_KEY", "").strip())
    groq_configured = bool(os.getenv("GROQ_API_KEY", "").strip())

    if not gemini_configured and not groq_configured:
        if progress_callback:
            progress_callback(
                "offline",
                "Preparing your presentation.",
            )
        return None, "offline", "No AI providers are configured."

    if gemini_configured and not groq_configured:
        if progress_callback:
            progress_callback("gemini", "Creating your presentation content.")
        slides, detail = gemini_generate(
            topic, count, audience, purpose, tone
        )
        if slides:
            return slides, "gemini", detail
        return None, "offline", detail

    if groq_configured and not gemini_configured:
        if progress_callback:
            progress_callback(
                "groq",
                "Creating your presentation content.",
            )
        slides, detail = groq_generate(
            topic, count, audience, purpose, tone
        )
        if slides:
            if progress_callback:
                progress_callback("visuals", "Preparing slide visuals.")
            return slides, "groq", detail
        return None, "offline", detail

    executor = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="slidecraft-ai",
    )

    try:
        if progress_callback:
            progress_callback("gemini", "Creating your presentation content.")
        gemini_future = executor.submit(
            gemini_generate, topic, count, audience, purpose, tone
        )

        done, _ = wait(
            {gemini_future},
            timeout=delay,
            return_when=FIRST_COMPLETED,
        )

        # If Gemini has already returned, use it if its content is valid.
        if gemini_future in done:
            try:
                slides, detail = gemini_future.result()
            except Exception:
                app.logger.exception("Gemini worker failed unexpectedly.")
                slides, detail = None, "Gemini worker failed."

            if slides:
                return slides, "gemini", detail

            first_failure = detail
        else:
            # Gemini is still running after the fallback delay.
            first_failure = (
                f"Gemini did not return valid content within {delay:g} seconds."
            )

        # Start Groq now. If Gemini is still running, both providers race.
        if progress_callback:
            progress_callback(
                "groq",
                "Still working on your presentation...",
            )
        groq_future = executor.submit(
            groq_generate, topic, count, audience, purpose, tone
        )

        pending = {groq_future}

# If the first wait timed out, keep Gemini in the race even if
# it finishes just before this line executes.
        if gemini_future not in done:
         pending.add(gemini_future)

        failures = [first_failure]

        while pending:
            completed, pending = wait(
                pending,
                return_when=FIRST_COMPLETED,
            )

            for future in completed:
                try:
                    slides, detail = future.result()
                except Exception:
                    app.logger.exception("AI generation worker failed.")
                    slides, detail = None, "An AI provider worker failed."

                if slides:
                    provider = "groq" if future is groq_future else "gemini"
                    if progress_callback:
                        progress_callback("visuals", "Preparing slide visuals.")
                    return slides, provider, detail

                if detail:
                    failures.append(detail)

        return None, "offline", " ".join(failures)

    finally:
        # Do not make the user wait for a slower losing request.
        executor.shutdown(wait=False, cancel_futures=True)


def update_generation_status(generation_id, stage, message):
    if not generation_id:
        return
    now = time.monotonic()
    with GENERATION_STATUS_LOCK:
        expired = [
            key for key, state in GENERATION_STATUS.items()
            if now - state["updated_at"] > 900
        ]
        for key in expired:
            GENERATION_STATUS.pop(key, None)
        GENERATION_STATUS[generation_id] = {
            "stage": stage,
            "message": message,
            "updated_at": now,
        }


def generation_status(generation_id):
    with GENERATION_STATUS_LOCK:
        state = GENERATION_STATUS.get(generation_id)
        if not state or time.monotonic() - state["updated_at"] > 900:
            GENERATION_STATUS.pop(generation_id, None)
            return None
        return {"stage": state["stage"], "message": state["message"]}


def clear_generation_status(generation_id):
    if generation_id:
        with GENERATION_STATUS_LOCK:
            GENERATION_STATUS.pop(generation_id, None)


def analyze_slides(slides):
    if not slides:
        return {"score": 0, "slide_count": 0, "word_count": 0, "metrics": {}, "issues": ["No slides found."]}
    issues = []
    word_counts = []
    titles = []
    total_words = 0
    for idx, slide in enumerate(slides, start=1):
        slide = normalize_slide(slide)
        words = len((slide["title"] + " " + slide["body"] + " " + " ".join(slide["bullets"])).split())
        total_words += words
        word_counts.append(words)
        titles.append(slide["title"].strip().lower())
        if words > 85:
            issues.append(f"Slide {idx}: text-heavy. Reduce the amount of text.")
        if words < 8:
            issues.append(f"Slide {idx}: very little text. Add context if needed.")
        if len(slide["bullets"]) > 6:
            issues.append(f"Slide {idx}: too many bullets. Consider splitting the content.")
        if len(slide["title"]) > 75:
            issues.append(f"Slide {idx}: title is long. Shorten it for readability.")
    duplicates = len(titles) - len(set(titles))
    if duplicates:
        issues.append(f"{duplicates} repeated slide title(s) detected.")
    has_intro = any(any(k in t for k in ("intro", "overview", "background", "problem", "introduc")) for t in titles)
    has_end = any(any(k in t for k in ("conclusion", "summary", "takeaway", "questions", "next steps")) for t in titles)
    text_heavy = sum(1 for n in word_counts if n > 85)
    sparse = sum(1 for n in word_counts if n < 8)
    metrics = {
        "Clarity": max(25, min(100, 100 - duplicates * 10 - sum(len(t) > 75 for t in titles) * 8)),
        "Structure": max(25, 100 - (0 if has_intro else 20) - (0 if has_end else 20)),
        "Readability": max(20, 100 - text_heavy * 12 - sparse * 8),
        "Completeness": min(100, 45 + len(slides) * 4 + (10 if has_intro else 0) + (10 if has_end else 0)),
        "Slide balance": max(20, 100 - text_heavy * 10 - sparse * 8),
    }
    score = round(sum(metrics.values()) / len(metrics))
    return {"score": score, "slide_count": len(slides), "word_count": total_words, "metrics": metrics, "issues": issues}


def extract_pptx(stream):
    prs = Presentation(stream)
    if len(prs.slides) > MAX_SLIDES:
        raise ValueError(f"Maximum {MAX_SLIDES} slides supported per upload.")
    slides = []
    for idx, slide in enumerate(prs.slides, start=1):
        lines = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    text = para.text.strip()
                    if text:
                        lines.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        text = cell.text.strip()
                        if text:
                            lines.append(text)
        notes = ""
        try:
            notes = slide.notes_slide.notes_text_frame.text[:2000]
        except Exception:
            pass
        slides.append({"title": lines[0] if lines else f"Slide {idx}", "bullets": lines[1:20], "notes": notes})
    return slides


def validate_and_correct_pptx(presentation):
    """Structural PPTX validation and conservative font/bounds correction."""
    report = {
        "total_slide_count": len(presentation.slides),
        "slides_checked": len(presentation.slides),
        "out_of_bounds": 0,
        "text_overflow": 0,
        "unresolved_text_overflow": 0,
        "overlap_issues": 0,
        "empty_slide_count": 0,
        "corrected_issues": 0,
        "remaining_issues": [],
        "validation_level": "structural",
        "visual_rendering_verified": False,
        "rendering": (
            "structural validation only; local rendering is unavailable, "
            "so visual collisions and actual text clipping are not verified."
        ),
    }
    slide_width = presentation.slide_width
    slide_height = presentation.slide_height
    safe_margin = Inches(0.12)

    def text_overflows(shape):
        paragraphs = shape.text_frame.paragraphs
        text = " ".join(paragraph.text for paragraph in paragraphs).strip()
        if not text:
            return False
        font_sizes = [
            run.font.size.pt
            for paragraph in paragraphs
            for run in paragraph.runs
            if run.font.size
        ]
        font_size = max(font_sizes, default=18)
        chars_per_line = max(
            8,
            int(shape.width / Pt(1) / max(1, font_size * 0.52)),
        )
        estimated_lines = sum(
            max(1, math.ceil(len(paragraph.text) / chars_per_line))
            for paragraph in paragraphs
            if paragraph.text.strip()
        )
        available_lines = max(
            1,
            int(shape.height / Pt(max(12, font_size * 1.25))),
        )
        return estimated_lines > available_lines

    for slide_index, slide in enumerate(presentation.slides, start=1):
        all_shapes = []
        if not slide.shapes:
            report["empty_slide_count"] += 1
            report["remaining_issues"].append({
                "slide": slide_index,
                "type": "empty_slide",
                "detail": "The slide contains no PowerPoint shapes.",
            })
        for shape in list(slide.shapes):
            all_shapes.append(shape)
            if shape.left < safe_margin or shape.top < safe_margin:
                report["out_of_bounds"] += 1
                shape.left = max(safe_margin, shape.left)
                shape.top = max(safe_margin, shape.top)
                report["corrected_issues"] += 1
            if shape.left + shape.width > slide_width - safe_margin:
                report["out_of_bounds"] += 1
                shape.width = max(Inches(0.25), slide_width - safe_margin - shape.left)
                report["corrected_issues"] += 1
            if shape.top + shape.height > slide_height - safe_margin:
                report["out_of_bounds"] += 1
                shape.height = max(Inches(0.25), slide_height - safe_margin - shape.top)
                report["corrected_issues"] += 1

            if getattr(shape, "has_text_frame", False):
                if text_overflows(shape):
                    report["text_overflow"] += 1
                    corrected = False
                    for _attempt in range(4):
                        changed = False
                        for paragraph in shape.text_frame.paragraphs:
                            for run in paragraph.runs:
                                size = run.font.size or Pt(18)
                                if size > Pt(10):
                                    run.font.size = Pt(max(10, size.pt * 0.88))
                                    changed = True
                        corrected = corrected or changed
                        if not changed or not text_overflows(shape):
                            break
                    if corrected:
                        report["corrected_issues"] += 1
                    if text_overflows(shape):
                        report["unresolved_text_overflow"] += 1
                        report["remaining_issues"].append({
                            "slide": slide_index,
                            "type": "estimated_text_overflow",
                            "detail": "Text may exceed its box after conservative font-size correction.",
                        })

        for index, shape in enumerate(all_shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            for other in all_shapes[index + 1:]:
                if getattr(other, "has_text_frame", False) is False:
                    continue
                overlap_x = min(shape.left + shape.width, other.left + other.width) - max(shape.left, other.left)
                overlap_y = min(shape.top + shape.height, other.top + other.height) - max(shape.top, other.top)
                if overlap_x > Inches(0.03) and overlap_y > Inches(0.03):
                    report["overlap_issues"] += 1
                    report["remaining_issues"].append({
                        "slide": slide_index,
                        "type": "text_box_overlap",
                        "detail": "Two text boxes intersect structurally; review the slide in PowerPoint.",
                    })
                    break

    report["passed"] = (
        report["out_of_bounds"] == 0
        and not report["remaining_issues"]
    )
    return report


def make_pptx(slides):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    navy = RGBColor(8, 20, 48)
    blue = RGBColor(42, 103, 255)
    white = RGBColor(255, 255, 255)
    ink = RGBColor(23, 36, 61)
    muted = RGBColor(92, 106, 130)
    pale = RGBColor(239, 244, 255)
    coral = RGBColor(255, 128, 105)

    def add_text(slide, text, x, y, width, height, size, color, bold=False, font="Aptos"):
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(height))
        frame = box.text_frame
        frame.word_wrap = True
        frame.margin_left = Inches(0.02)
        frame.margin_right = Inches(0.02)
        frame.margin_top = Inches(0.01)
        frame.margin_bottom = Inches(0.01)
        paragraph = frame.paragraphs[0]
        paragraph.text = str(text)
        paragraph.font.name = font
        paragraph.font.size = Pt(size)
        paragraph.font.bold = bold
        paragraph.font.color.rgb = color
        return box

    def add_photo_credit(slide, visual, x, y, width, color=muted, background=None):
        if not visual.get("image_credit"):
            return None
        if background is not None:
            backing = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, Inches(x - 0.06), Inches(y - 0.035),
                Inches(width + 0.12), Inches(0.31),
            )
            backing.fill.solid()
            backing.fill.fore_color.rgb = background
            backing.line.fill.background()
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(0.23))
        frame = box.text_frame
        frame.word_wrap = False
        frame.margin_left = Inches(0)
        frame.margin_right = Inches(0)
        frame.margin_top = Inches(0)
        frame.margin_bottom = Inches(0)
        paragraph = frame.paragraphs[0]
        paragraph.font.name = "Aptos"
        paragraph.font.size = Pt(8)
        paragraph.font.color.rgb = color
        if visual.get("image_provider") == "adobe_firefly":
            paragraph.text = "Generated with Adobe Firefly"
            return box
        paragraph.text = "Photo by "
        photographer = paragraph.add_run()
        photographer.text = visual["image_credit"]
        photographer.font.name = "Aptos"
        photographer.font.size = Pt(8)
        photographer.font.color.rgb = color
        photographer_url = visual.get("photographer_url") or visual.get("image_source")
        if photographer_url:
            photographer.hyperlink.address = photographer_url
        separator = paragraph.add_run()
        separator.text = " · "
        separator.font.name = "Aptos"
        separator.font.size = Pt(8)
        separator.font.color.rgb = color
        unsplash = paragraph.add_run()
        unsplash.text = "Unsplash"
        unsplash.font.name = "Aptos"
        unsplash.font.size = Pt(8)
        unsplash.font.color.rgb = color
        unsplash.hyperlink.address = visual.get("unsplash_url") or "https://unsplash.com/?utm_source=slidecraft_studio&utm_medium=referral"
        return box

    def add_photo(slide, visual, x, y, width, height):
        path = cached_image_path(visual.get("image_asset"))
        if not path:
            return False
        picture = slide.shapes.add_picture(str(path), Inches(x), Inches(y), Inches(width), Inches(height))
        image_width, image_height = picture.image.size
        image_ratio = image_width / image_height
        box_ratio = width / height
        if image_ratio > box_ratio:
            crop = (1 - box_ratio / image_ratio) / 2
            picture.crop_left = crop
            picture.crop_right = crop
        else:
            crop = (1 - image_ratio / box_ratio) / 2
            picture.crop_top = crop
            picture.crop_bottom = crop
        picture.line.color.rgb = RGBColor(224, 231, 243)
        picture.line.width = Pt(1)
        if visual.get("image_credit") and y + height + 0.3 <= 7.5:
            add_photo_credit(slide, visual, x, y + height + 0.07, width)
        return True

    def add_bullets(
        slide, bullets, x, y, width, max_items=5, step=0.82,
        text_height=0.68, font_size=15,
    ):
        for index, bullet in enumerate(bullets[:max_items]):
            top = y + index * step
            marker = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(top + min(0.11, text_height * 0.22)), Inches(0.13), Inches(0.13))
            marker.fill.solid()
            marker.fill.fore_color.rgb = coral if index == 1 else blue
            marker.line.fill.background()
            add_text(slide, bullet, x + 0.28, top, width - 0.28, text_height, font_size, muted)

    def draw_native_illustration(slide, bullets, x, y, width, height, title):
        panel = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(width), Inches(height))
        panel.fill.solid()
        panel.fill.fore_color.rgb = pale
        panel.line.fill.background()
        orb = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x + width * 0.59), Inches(y + 0.25), Inches(width * 0.32), Inches(width * 0.32))
        orb.fill.solid()
        orb.fill.fore_color.rgb = RGBColor(211, 224, 255)
        orb.line.fill.background()
        count = min(3, max(2, len(bullets)))
        card_width = (width - 0.64) / count
        for index in range(count):
            card_x = x + 0.2 + index * card_width
            card_y = y + height * (0.38 if index % 2 == 0 else 0.5)
            card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(card_x), Inches(card_y), Inches(card_width - 0.12), Inches(height * 0.32))
            card.fill.solid()
            card.fill.fore_color.rgb = white
            card.line.color.rgb = RGBColor(220, 229, 246)
            add_text(slide, f"{index + 1:02d}", card_x + 0.14, card_y + 0.12, card_width - 0.4, 0.25, 10, blue, True)
            label = bullets[index] if index < len(bullets) else title
            add_text(slide, label[:64], card_x + 0.14, card_y + 0.4, card_width - 0.4, height * 0.22, 11, ink, True)
        add_text(slide, "A visual summary", x + 0.2, y + height - 0.42, width - 0.4, 0.25, 10, muted)

    def draw_diagram(slide, diagram, x, y, width, height):
        items = diagram["items"]
        diagram_type = diagram["type"]
        if diagram_type == "hierarchy":
            root = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x + width * 0.35), Inches(y + 0.1), Inches(width * 0.3), Inches(0.72))
            root.fill.solid()
            root.fill.fore_color.rgb = blue
            root.line.fill.background()
            root.text_frame.text = items[0]
            root.text_frame.paragraphs[0].font.color.rgb = white
            root.text_frame.paragraphs[0].font.size = Pt(12)
            child_items = items[1:]
            child_width = width / len(child_items)
            for index, label in enumerate(child_items):
                child_x = x + index * child_width + 0.04
                child_y = y + height * 0.64
                line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x + width / 2), Inches(y + 0.82), Inches(child_x + child_width / 2), Inches(child_y))
                line.line.color.rgb = RGBColor(153, 176, 224)
                child = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(child_x), Inches(child_y), Inches(child_width - 0.08), Inches(0.78))
                child.fill.solid()
                child.fill.fore_color.rgb = pale
                child.line.color.rgb = RGBColor(211, 224, 255)
                child.text_frame.text = label
                child.text_frame.paragraphs[0].font.color.rgb = ink
                child.text_frame.paragraphs[0].font.size = Pt(11)
            return
        count = len(items)
        gap = 0.18
        item_width = (width - gap * (count - 1)) / count
        for index, label in enumerate(items):
            if diagram_type == "timeline":
                marker_x = x + index * (width / max(1, count - 1))
                marker = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(marker_x - 0.11), Inches(y + height * 0.36), Inches(0.22), Inches(0.22))
                marker.fill.solid()
                marker.fill.fore_color.rgb = coral if index == count - 1 else blue
                marker.line.fill.background()
                if index < count - 1:
                    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(marker_x + 0.12), Inches(y + height * 0.47), Inches(marker_x + width / max(1, count - 1) - 0.12), Inches(y + height * 0.47))
                    line.line.color.rgb = RGBColor(153, 176, 224)
                card_x = min(max(x, marker_x - item_width / 2), x + width - item_width)
            else:
                card_x = x + index * (item_width + gap)
                if index:
                    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(card_x - gap), Inches(y + height * 0.48), Inches(card_x), Inches(y + height * 0.48))
                    line.line.color.rgb = blue
                    line.line.end_arrowhead = True
            card_y = y + (0.05 if index % 2 == 0 else height * 0.53)
            card_height = height * 0.38
            card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(card_x), Inches(card_y), Inches(item_width), Inches(card_height))
            card.fill.solid()
            card.fill.fore_color.rgb = white if diagram_type == "comparison" else pale
            card.line.color.rgb = RGBColor(211, 224, 255)
            card.text_frame.text = label
            card.text_frame.paragraphs[0].font.name = "Aptos"
            card.text_frame.paragraphs[0].font.size = Pt(11)
            card.text_frame.paragraphs[0].font.color.rgb = ink
            card.text_frame.paragraphs[0].font.bold = True

    def add_chart(slide, chart_spec, x, y, width, height):
        chart_data = CategoryChartData()
        chart_data.categories = chart_spec["labels"]
        for series in chart_spec["series"]:
            chart_data.add_series(series["name"], series["values"])
        chart_type = {
            "bar": XL_CHART_TYPE.BAR_CLUSTERED,
            "comparison": XL_CHART_TYPE.COLUMN_CLUSTERED,
            "line": XL_CHART_TYPE.LINE_MARKERS,
            "pie": XL_CHART_TYPE.PIE,
            "doughnut": XL_CHART_TYPE.DOUGHNUT,
        }[chart_spec["type"]]
        graphic_frame = slide.shapes.add_chart(chart_type, Inches(x), Inches(y), Inches(width), Inches(height), chart_data)
        chart = graphic_frame.chart
        chart.has_title = True
        chart.chart_title.text_frame.text = chart_spec["title"] + (" (illustrative)" if chart_spec["illustrative"] else "")
        chart.chart_title.text_frame.paragraphs[0].font.size = Pt(15)
        chart.has_legend = len(chart_spec["series"]) > 1 or chart_spec["type"] in {"pie", "doughnut"}
        palette = [
            blue,
            coral,
            RGBColor(46, 164, 126),
            RGBColor(142, 99, 224),
            RGBColor(240, 177, 58),
            RGBColor(34, 160, 190),
        ]
        for series_index, series in enumerate(chart.series):
            color = palette[series_index % len(palette)]
            if chart_spec["type"] == "line":
                series.format.line.color.rgb = color
                series.format.line.width = Pt(2.25)
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
        if chart_spec["type"] in {"pie", "doughnut"}:
            for point_index, point in enumerate(chart.series[0].points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = palette[point_index % len(palette)]
        plot = chart.plots[0]
        plot.has_data_labels = True
        plot.data_labels.show_value = True
        if chart_spec["type"] in {"pie", "doughnut"}:
            plot.data_labels.show_category_name = True
            plot.data_labels.position = XL_DATA_LABEL_POSITION.BEST_FIT
        else:
            plot.data_labels.position = XL_DATA_LABEL_POSITION.OUTSIDE_END
            chart.category_axis.has_title = True
            chart.category_axis.axis_title.text_frame.text = "Category"
            if chart_spec["units"]:
                chart.value_axis.has_title = True
                chart.value_axis.axis_title.text_frame.text = chart_spec["units"]
        return chart

    for idx, raw in enumerate(slides, start=1):
        data = normalize_slide(raw)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        visual = data["visual"]
        layout = visual["layout"]
        photo = bool(cached_image_path(visual["image_asset"]))
        if layout == "cover":
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = navy
            if photo:
                add_photo(slide, visual, 6.35, 0, 6.98, 7.5)
            else:
                draw_native_illustration(slide, data["bullets"], 6.65, 1.25, 5.8, 5.1, data["title"])
            add_text(slide, "SLIDECRAFT  /  STUDIO", 0.78, 0.62, 4.8, 0.35, 10, RGBColor(168, 192, 255), True)
            add_text(slide, data["title"], 0.78, 2.15, 5.6, 2.15, 36, white, True, "Aptos Display")
            cover_body = data["body"] or visual["description"] or "A clear, visual story built for your audience."
            cover_text = add_text(slide, cover_body, 0.82, 4.55, 4.95, 1.0, 13, RGBColor(208, 219, 241))
            cover_text.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            add_text(slide, f"{idx:02d}  /  {len(slides):02d}", 0.82, 6.85, 2, 0.25, 10, RGBColor(168, 192, 255), True)
            if photo:
                add_photo_credit(slide, visual, 6.65, 7.12, 5.8, white, navy)
        elif layout == "full_image" and photo:
            add_photo(slide, visual, 0, 0, 13.333, 7.5)
            overlay = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(6.6), Inches(7.5))
            overlay.fill.solid()
            overlay.fill.fore_color.rgb = navy
            overlay.line.fill.background()
            add_text(slide, data["title"], 0.75, 0.8, 5.2, 1.5, 30, white, True, "Aptos Display")
            if data["body"]:
                body_text = add_text(slide, data["body"], 0.82, 2.35, 5.2, 0.55, 11, RGBColor(208, 219, 241))
                body_text.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            add_bullets(slide, data["bullets"], 0.82, 3.02, 5.2, 5)
        else:
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = white
            accent = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.62), Inches(0.46), Inches(0.13), Inches(0.48))
            accent.fill.solid()
            accent.fill.fore_color.rgb = blue
            accent.line.fill.background()
            add_text(slide, data["title"], 0.92, 0.42, 11.8, 0.8, 27, ink, True, "Aptos Display")
            lead = data["body"] or visual["description"][:130] or "A focused visual explanation"
            lead_text = add_text(slide, lead, 0.95, 1.25, 11.6, 0.58, 11, muted)
            lead_text.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            if layout == "chart" and data["chart"]:
                try:
                    add_chart(slide, data["chart"], 0.72, 1.95, 7.8, 4.77)
                    add_bullets(slide, data["bullets"], 8.85, 2.02, 3.8, 4, font_size=12)
                    chart_notes = []
                    if data["chart"]["units"]:
                        chart_notes.append(f"Units: {data['chart']['units']}")
                    if data["chart"]["source"]:
                        chart_notes.append(f"Source: {data['chart']['source']}")
                    if chart_notes:
                        add_text(slide, "  ·  ".join(chart_notes), 0.85, 6.78, 11.8, 0.3, 8, muted)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    app.logger.warning("PowerPoint chart rendering failed; using an editable visual fallback: %s", exc)
                    draw_native_illustration(slide, data["bullets"], 0.82, 1.9, 7.6, 4.8, data["title"])
                    add_bullets(slide, data["bullets"], 8.8, 2.0, 3.8, 4)
                    add_text(slide, "Chart unavailable; showing an editable visual summary.", 0.9, 6.82, 7.3, 0.25, 8, muted)
            elif layout == "diagram" and data["diagram"]:
                draw_diagram(slide, data["diagram"], 0.9, 2.0, 11.5, 2.35)
                if data["diagram"]["caption"]:
                    add_text(slide, data["diagram"]["caption"], 0.95, 4.48, 11.4, 0.35, 10, muted)
                add_bullets(
                    slide, data["bullets"], 0.82, 4.95, 11.8, 4,
                    step=0.52, text_height=0.46, font_size=12,
                )
            elif layout == "comparison":
                divider = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(6.62), Inches(2.0), Inches(0.025), Inches(4.35))
                divider.fill.solid()
                divider.fill.fore_color.rgb = RGBColor(220, 229, 246)
                divider.line.fill.background()
                midpoint = (len(data["bullets"]) + 1) // 2
                add_text(slide, "PERSPECTIVE A", 0.95, 1.9, 5.3, 0.32, 10, blue, True)
                add_text(slide, "PERSPECTIVE B", 6.95, 1.9, 5.2, 0.32, 10, coral, True)
                add_bullets(slide, data["bullets"][:midpoint], 1.0, 2.45, 5.2, 4)
                add_bullets(slide, data["bullets"][midpoint:], 7.0, 2.45, 5.0, 4)
            elif layout == "metrics":
                cards = data["bullets"][:4]
                card_width = 11.4 / max(1, len(cards))
                for card_index, item in enumerate(cards):
                    card_x = 0.95 + card_index * card_width
                    card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(card_x), Inches(2.25), Inches(card_width - 0.18), Inches(3.45))
                    card.fill.solid()
                    card.fill.fore_color.rgb = pale
                    card.line.fill.background()
                    add_text(slide, f"INSIGHT {card_index + 1:02d}", card_x + 0.22, 2.55, card_width - 0.6, 0.3, 9, blue, True)
                    insight = add_text(slide, item, card_x + 0.22, 3.2, card_width - 0.6, 1.8, 14, ink, True)
                    insight.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            else:
                image_left = layout == "image_left"
                image_x, text_x = (0.92, 5.78) if image_left else (8.02, 0.95)
                image_w, text_w = (4.35, 6.5) if image_left else (4.35, 6.5)
                added = add_photo(slide, visual, image_x, 1.95, image_w, 4.45) if photo else False
                if not added:
                    draw_native_illustration(slide, data["bullets"], image_x, 1.95, image_w, 4.45, data["title"])
                add_bullets(slide, data["bullets"], text_x, 2.0, text_w, 5)
                if layout == "conclusion":
                    takeaway = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(text_x), Inches(6.15), Inches(text_w - 0.2), Inches(0.48))
                    takeaway.fill.solid()
                    takeaway.fill.fore_color.rgb = pale
                    takeaway.line.fill.background()
                    takeaway.text_frame.text = "One clear idea. One next step."
                    takeaway.text_frame.paragraphs[0].font.color.rgb = blue
                    takeaway.text_frame.paragraphs[0].font.bold = True
                    takeaway.text_frame.paragraphs[0].font.size = Pt(11)
        if layout != "cover" and visual["image_credit"] and layout == "full_image":
            add_photo_credit(slide, visual, 9.2, 7.12, 3.8, white, navy)
        if data["notes"]:
            slide.notes_slide.notes_text_frame.text = data["notes"]
    quality_report = validate_and_correct_pptx(prs)
    output = io.BytesIO()
    prs.save(output)
    output.seek(0)
    validated = Presentation(output)
    final_report = validate_and_correct_pptx(validated)
    final_report["correction_passes"] = 2
    final_report["passed"] = final_report["passed"] and quality_report["passed"]
    setattr(validated, "_slidecraft_quality_report", final_report)
    output = io.BytesIO()
    validated.save(output)
    output.seek(0)
    return output


def make_quiz_pptx(questions, difficulty):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    prs.core_properties.title = "SlideCraft Quiz"
    prs.core_properties.subject = f"{difficulty} quiz"

    navy = RGBColor(8, 20, 48)
    white = RGBColor(245, 248, 255)
    blue = RGBColor(91, 123, 240)
    card_fill = RGBColor(22, 34, 57)
    card_border = RGBColor(58, 75, 108)
    muted = RGBColor(171, 187, 219)
    slide_content = []

    for question_number, question in enumerate(questions, start=1):
        question_height = min(
            2.8,
            max(0.9, 0.15 + math.ceil(len(question["question"]) / 78) * 0.37),
        )
        options = question["options"] or ["Short-answer question"]
        option_heights = [
            min(1.4, max(0.56, 0.34 + math.ceil(len(option) / 90) * 0.22))
            for option in options
        ]
        options_top = 1.2 + question_height + 0.25
        available_height = 6.85 - options_top
        pages = []
        current_page = []
        used_height = 0
        for option_index, (option, height) in enumerate(zip(options, option_heights)):
            height_with_gap = height + (0.12 if current_page else 0)
            if current_page and used_height + height_with_gap > available_height:
                pages.append(current_page)
                current_page = []
                used_height = 0
                height_with_gap = height
            current_page.append((option, height, option_index))
            used_height += height_with_gap
        if current_page:
            pages.append(current_page)

        for page_options in pages:
            slide_content.append({
                "question_number": question_number,
                "question_total": len(questions),
                "type": question["type"],
                "question": question["question"],
                "question_height": question_height,
                "options_top": options_top,
                "options": page_options,
                "answer": question["answer"],
                "explanation": question["explanation"],
            })

    for page_number, page in enumerate(slide_content, start=1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = navy

        accent = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0.65), Inches(0.35), Inches(0.08), Inches(0.32)
        )
        accent.fill.solid()
        accent.fill.fore_color.rgb = blue
        accent.line.fill.background()

        header = slide.shapes.add_textbox(Inches(0.85), Inches(0.35), Inches(4.5), Inches(0.3))
        header.text_frame.text = "SLIDECRAFT  /  QUIZ"
        header.text_frame.paragraphs[0].font.name = "Aptos"
        header.text_frame.paragraphs[0].font.size = Pt(10)
        header.text_frame.paragraphs[0].font.bold = True
        header.text_frame.paragraphs[0].font.color.rgb = muted

        label = slide.shapes.add_textbox(Inches(8.1), Inches(0.35), Inches(4.5), Inches(0.3))
        label.text_frame.text = (
            f"QUESTION {page['question_number']:02d} / {page['question_total']:02d}"
            f"  ·  {page['type'].upper()}"
        )
        label.text_frame.paragraphs[0].font.name = "Aptos"
        label.text_frame.paragraphs[0].font.size = Pt(9)
        label.text_frame.paragraphs[0].font.bold = True
        label.text_frame.paragraphs[0].font.color.rgb = muted

        question_box = slide.shapes.add_textbox(
            Inches(0.75), Inches(1.2), Inches(11.85), Inches(page["question_height"])
        )
        question_frame = question_box.text_frame
        question_frame.word_wrap = True
        question_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        question_frame.margin_left = Inches(0.02)
        question_frame.margin_right = Inches(0.02)
        question_frame.margin_top = Inches(0.02)
        question_frame.margin_bottom = Inches(0.02)
        question_frame.text = page["question"]
        question_paragraph = question_frame.paragraphs[0]
        question_paragraph.font.name = "Aptos Display"
        question_paragraph.font.size = Pt(22)
        question_paragraph.font.bold = True
        question_paragraph.font.color.rgb = white

        top = page["options_top"]
        for option, height, option_index in page["options"]:
            option_box = slide.shapes.add_shape(
                MSO_SHAPE.ROUNDED_RECTANGLE,
                Inches(0.75), Inches(top), Inches(11.85), Inches(height),
            )
            option_box.fill.solid()
            option_box.fill.fore_color.rgb = card_fill
            option_box.line.color.rgb = card_border
            option_frame = option_box.text_frame
            option_frame.word_wrap = True
            option_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            option_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
            option_frame.margin_left = Inches(0.2)
            option_frame.margin_right = Inches(0.2)
            option_frame.margin_top = Inches(0.06)
            option_frame.margin_bottom = Inches(0.06)
            if page["type"] == "Short Answer":
                option_frame.text = option
            else:
                option_frame.text = f"{chr(65 + option_index)}.  {option}"
            paragraph = option_frame.paragraphs[0]
            paragraph.font.name = "Aptos"
            paragraph.font.size = Pt(15)
            paragraph.font.color.rgb = white
            top += height + 0.12

        footer = slide.shapes.add_textbox(Inches(10.7), Inches(7.12), Inches(1.9), Inches(0.2))
        footer.text_frame.text = f"{page_number:02d}  /  {len(slide_content):02d}"
        footer.text_frame.paragraphs[0].font.name = "Aptos"
        footer.text_frame.paragraphs[0].font.size = Pt(8)
        footer.text_frame.paragraphs[0].font.color.rgb = muted
        footer.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT
        slide.notes_slide.notes_text_frame.text = (
            f"Correct answer: {page['answer']}\n\nExplanation: {page['explanation']}"
        )

    quality_report = validate_and_correct_pptx(prs)
    output = io.BytesIO()
    prs.save(output)
    output.seek(0)
    validated = Presentation(output)
    final_report = validate_and_correct_pptx(validated)
    final_report["correction_passes"] = 2
    final_report["passed"] = final_report["passed"] and quality_report["passed"]
    setattr(validated, "_slidecraft_quality_report", final_report)
    output = io.BytesIO()
    validated.save(output)
    output.seek(0)
    return output


def ai_json(prompt, max_tokens=4000):
    """Return JSON from the configured Gemini provider, with Groq fallback."""
    system = "Follow the requested JSON schema exactly. Return JSON only, without Markdown fences."

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": gemini_key},
                json={
                    "contents": [{"parts": [{"text": system + "\n\n" + prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0.2,
                        "maxOutputTokens": max_tokens,
                    },
                },
                timeout=(8, 90),
            )
            response.raise_for_status()
            raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip()))
        except Exception as exc:
            app.logger.warning("Gemini feature request failed: %s", exc)

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key:
        model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_completion_tokens": max_tokens,
                },
                timeout=(8, 90),
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip()))
        except Exception as exc:
            app.logger.warning("Groq feature request failed: %s", exc)

    return None


def readiness_evaluation(slides):
    normalized = [normalize_slide(slide) for slide in slides]
    total_words = sum(len((s["title"] + " " + s["body"] + " " + " ".join(s["bullets"])).split()) for s in normalized)
    avg_words = total_words / max(1, len(normalized))
    text_heavy = sum(1 for s in normalized if len((s["title"] + " " + s["body"] + " " + " ".join(s["bullets"])).split()) > 85)
    long_titles = sum(1 for s in normalized if len(s["title"]) > 75)
    visual_count = sum(1 for s in normalized if s["visual"]["image_asset"] or s["chart"] or s["diagram"])
    opening = normalized[0] if normalized else {"title": "", "bullets": []}
    conclusion = normalized[-1] if normalized else {"title": "", "bullets": []}
    has_opening = bool(re.search(r"intro|overview|opening|background|problem", opening["title"], re.I))
    has_conclusion = bool(re.search(r"conclusion|summary|takeaway|questions|next steps", conclusion["title"], re.I))

    metrics = {
        "Content Quality": max(25, min(100, 65 + min(20, len(normalized) * 2) - text_heavy * 8)),
        "Structure": max(25, 100 - (0 if has_opening else 15) - (0 if has_conclusion else 15)),
        "Readability": max(20, 100 - max(0, avg_words - 45) * 1.2 - text_heavy * 10 - long_titles * 8),
        "Visual Design": max(20, min(100, 55 + visual_count * 8)),
        "Grammar": max(30, min(100, 92 - sum(s["title"].count("  ") for s in normalized))),
    }
    strengths = []
    weaknesses = []
    suggestions = []
    if has_opening:
        strengths.append("The deck includes a recognizable opening slide.")
    else:
        weaknesses.append("The opening does not clearly establish context.")
        suggestions.append("Add an overview or problem-opening slide.")
    if has_conclusion:
        strengths.append("The deck includes a clear closing theme.")
    else:
        weaknesses.append("The final slide may not provide a strong conclusion.")
        suggestions.append("End with a summary, takeaway, or next steps.")
    if not text_heavy:
        strengths.append("Slide text density is generally controlled.")
    else:
        weaknesses.append(f"{text_heavy} slide(s) may be text-heavy.")
        suggestions.append("Split dense slides or convert paragraphs into concise bullets.")
    if visual_count:
        strengths.append("The deck contains images, charts, or diagrams.")
    else:
        weaknesses.append("No visual assets or structured visuals are detected.")
        suggestions.append("Add relevant images, charts, or diagrams to key slides.")
    slides_to_review = [
        {"slide": index + 1, "title": s["title"], "reason": "Text-heavy or sparse content; review title and bullets."}
        for index, s in enumerate(normalized)
        if len((s["title"] + " " + s["body"] + " " + " ".join(s["bullets"])).split()) > 85
        or len((s["title"] + " " + s["body"] + " " + " ".join(s["bullets"])).split()) < 8
    ]
    overall = round(sum(metrics.values()) / len(metrics))
    return {
        "overall": overall,
        "metrics": metrics,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "suggestions": suggestions,
        "slides_to_review": slides_to_review,
        "visual_estimation": True,
        "limitation": "Visual design and image relevance are estimated from available slide metadata; the original PPTX rendering could not fully be inspected.",
    }


LAYOUT_METADATA = {
    "split": {
        "name": "Split content",
        "description": "Balances text with supporting visual space.",
        "topics": ("overview", "strategy", "business", "education"),
        "audiences": ("general", "business", "client", "professional"),
    },
    "image_left": {
        "name": "Image + text",
        "description": "Pairs a left-side image with editable slide content.",
        "topics": ("story", "healthcare", "culture", "marketing", "travel"),
        "audiences": ("general", "creative", "client", "students"),
    },
    "full_image": {
        "name": "Full-slide image",
        "description": "Uses a full-bleed image when a suitable asset is available.",
        "topics": ("story", "culture", "marketing", "travel", "nature"),
        "audiences": ("general", "creative", "clients"),
    },
    "chart": {
        "name": "Data chart",
        "description": "Uses an editable chart on slides with chart data.",
        "topics": ("data", "finance", "research", "metric", "kpi", "impact"),
        "audiences": ("academic", "technical", "business", "executive"),
    },
    "diagram": {
        "name": "Diagram",
        "description": "Uses a native diagram on slides with structured steps or groups.",
        "topics": ("process", "workflow", "timeline", "system", "framework"),
        "audiences": ("academic", "technical", "business", "students"),
    },
    "comparison": {
        "name": "Comparison",
        "description": "Highlights alternatives, trade-offs, and side-by-side points.",
        "topics": ("compare", "versus", "alternative", "trade-off", "pros and cons"),
        "audiences": ("business", "academic", "technical", "professional"),
    },
    "metrics": {
        "name": "Key metrics",
        "description": "Emphasizes concise figures and measurable outcomes.",
        "topics": ("metric", "kpi", "measure", "impact", "results", "data"),
        "audiences": ("business", "executive", "technical", "professional"),
    },
}


def recommend_templates(topic, audience, purpose, requirements=None):
    text = f"{topic} {audience} {purpose} {json.dumps(requirements or {})}".lower()
    has_keyword = lambda keyword: bool(
        re.search(rf"(?<!\w){re.escape(keyword.lower())}(?!\w)", text)
    )
    ai_flag = lambda key: (
        requirements.get(key) is True
        or str(requirements.get(key, "")).strip().lower() in {"true", "yes"}
    ) if isinstance(requirements, dict) else False
    recommendations = []
    for layout in SELECTABLE_LAYOUTS:
        template = LAYOUT_METADATA[layout]
        score = 35
        reasons = []
        topic_hits = sum(1 for word in template["topics"] if has_keyword(word))
        audience_hits = sum(1 for word in template["audiences"] if has_keyword(word))
        if topic_hits:
            score += min(30, topic_hits * 10)
            reasons.append("Matches topic keywords")
        if audience_hits:
            score += min(15, audience_hits * 5)
            reasons.append("Fits the selected audience")
        if layout == "chart" and ai_flag("needs_charts"):
            score += 20
            reasons.append("AI analysis identified a need for charts")
        if layout == "diagram" and ai_flag("needs_diagrams"):
            score += 20
            reasons.append("AI analysis identified a need for diagrams")
        if layout in {"full_image", "image_left"} and ai_flag("needs_images"):
            score += 15
            reasons.append("AI analysis identified a need for images")
        if any(has_keyword(word) for word in ("pitch", "proposal")) and layout in {"comparison", "metrics"}:
            score += 5
            reasons.append("Useful for a persuasive proposal")
        recommendations.append({
            "layout": layout,
            **template,
            "score": min(100, score),
            "reasons": reasons or ["A general-purpose layout for this presentation"],
        })
    return sorted(recommendations, key=lambda item: item["score"], reverse=True)[:3]


# ---------- Public pages ----------

@app.get("/")
def home():
    return render_template("landing.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:80]
        email_value = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        try:
            email = normalize_signup_email(email_value)
        except EmailNotValidError:
            return render_template(
                "auth.html",
                mode="signup",
                email_error="Enter a valid email address (maximum 254 characters).",
                email_value=email_value,
            )
        if len(name) < 2:
            flash("Enter a name with at least 2 characters.", "error")
        elif len(password) < 8:
            flash("Use a password with at least 8 characters.", "error")
        else:
            try:
                with db_connect() as db:
                    existing = db.execute(
                        "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE",
                        (email,),
                    ).fetchone()
                    if existing:
                        return render_template(
                            "auth.html",
                            mode="signup",
                            email_error="An account with this email already exists.",
                            email_value=email_value,
                        )
                    cur = db.execute(
                        "INSERT INTO users(name, email, password_hash, auth_provider) VALUES (?, ?, ?, ?)",
                        (name, email, generate_password_hash(password), "email"),
                    )
                    session.clear()
                    session["user_id"] = cur.lastrowid
                    session["auth_provider"] = "email"
                flash("Account created. Welcome to SlideCraft!", "success")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                return render_template(
                    "auth.html",
                    mode="signup",
                    email_error="An account with this email already exists.",
                    email_value=email_value,
                )
    return render_template("auth.html", mode="signup", email_value="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with db_connect() as db:
            user = db.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["auth_provider"] = user["auth_provider"] or "email"
            flash("You're signed in.", "success")
            next_url = request.args.get("next", "")
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("dashboard"))
        flash("Email or password is incorrect.", "error")
    return render_template("auth.html", mode="login")


@app.get("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("home"))


@app.get("/guest")
def guest_login():
    session.clear()
    session["guest"] = True
    flash("Guest mode enabled. You can explore the workspace without creating an account.", "info")
    return redirect(url_for("create"))


@app.get("/login/google")
def login_google():
    if not provider_configured("google"):
        flash("Google login is not configured yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to your .env file.", "error")
        return redirect(url_for("login"))
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.get("/auth/google/callback")
def google_callback():
    return handle_oauth_callback("google")


@app.get("/login/facebook")
def login_facebook():
    if not provider_configured("facebook"):
        flash("Facebook login is not configured yet. Add FACEBOOK_CLIENT_ID and FACEBOOK_CLIENT_SECRET to your .env file.", "error")
        return redirect(url_for("login"))
    redirect_uri = url_for("facebook_callback", _external=True)
    return oauth.facebook.authorize_redirect(redirect_uri)


@app.get("/auth/facebook/callback")
def facebook_callback():
    return handle_oauth_callback("facebook")


def handle_oauth_callback(provider):
    if not provider_configured(provider):
        flash(f"{provider.title()} login is not configured yet. Add the required environment variables and restart the app.", "error")
        return redirect(url_for("login"))
    try:
        client = oauth.create_client(provider)
        token = client.authorize_access_token()
        if provider == "google":
            user_info = client.userinfo()
        else:
            response = client.get("me", params={"fields": "id,name,email,picture.type(large)"})
            response.raise_for_status()
            user_info = response.json()
        if not user_info:
            raise ValueError("No profile information returned")
    except Exception as exc:  # pragma: no cover - runtime external call
        app.logger.warning("OAuth login failed for %s: %s", provider, exc)
        flash(f"{provider.title()} authentication failed. Please try again or use email login.", "error")
        return redirect(url_for("login"))

    provider_user_id = str(user_info.get("sub") or user_info.get("id") or "").strip()
    email = str(user_info.get("email") or "").strip().lower()
    name = str(user_info.get("name") or user_info.get("given_name") or "").strip() or "SlideCraft User"
    avatar_url = ""
    if provider == "google":
        avatar_url = str(user_info.get("picture") or "").strip()
    elif isinstance(user_info.get("picture"), dict):
        avatar_url = str(user_info.get("picture", {}).get("data", {}).get("url") or "").strip()
    elif isinstance(user_info.get("picture"), str):
        avatar_url = str(user_info.get("picture") or "").strip()

    if not provider_user_id or not email:
        flash("The external account did not provide enough details to sign in securely.", "error")
        return redirect(url_for("login"))

    with db_connect() as db:
        user = db.execute(
            "SELECT * FROM users WHERE provider_user_id = ? AND auth_provider = ?",
            (provider_user_id, provider),
        ).fetchone()
        if user is None:
            existing_user = db.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
                (email,),
            ).fetchone()
            if existing_user and (existing_user["auth_provider"] or "email") != provider:
                flash(
                    "This email is already tied to a different sign-in method. Please sign in with your existing account or use a different email.",
                    "error",
                )
                return redirect(url_for("login"))
            user = existing_user
        if user is None:
            password_hash = generate_password_hash(secrets.token_urlsafe(32))
            try:
                cur = db.execute(
                    "INSERT INTO users(name, email, password_hash, auth_provider, provider_user_id, avatar_url) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, email, password_hash, provider, provider_user_id, avatar_url or None),
                )
                user_id = cur.lastrowid
            except sqlite3.IntegrityError:
                flash("This email is already associated with another account. Please sign in with that account or use a different email.", "error")
                return redirect(url_for("login"))
        else:
            if user["email"] != email:
                db.execute("UPDATE users SET email = ? WHERE id = ?", (email, user["id"]))
            db.execute(
                "UPDATE users SET name = ?, auth_provider = ?, provider_user_id = ?, avatar_url = ? WHERE id = ?",
                (name or user["name"], provider, provider_user_id, avatar_url or user["avatar_url"], user["id"]),
            )
            user_id = user["id"]

    session.clear()
    session["user_id"] = user_id
    session["auth_provider"] = provider
    flash(f"Signed in with {provider.title()}.", "success")
    return redirect(url_for("dashboard"))


# ---------- Workspace pages ----------

@app.get("/dashboard")
@login_required
def dashboard():
    with db_connect() as db:
        projects = db.execute(
            "SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC LIMIT 6",
            (session["user_id"],),
        ).fetchall()
    return render_template("dashboard.html", projects=projects, active="overview")


@app.route("/account", methods=["GET", "POST"])
def account():
    if session.get("guest"):
        if request.method == "POST":
            flash("Guest mode cannot save account changes. Sign in to unlock your personal profile.", "info")
            return redirect(url_for("account"))
        return render_template(
            "account.html",
            guest_mode=True,
            user={"name": "Guest mode", "email": "guest@local.session", "auth_provider": "guest", "created_at": "Not saved"},
            active="account",
        )

    if not session.get("user_id"):
        flash("Please sign in to view your account.", "info")
        return redirect(url_for("login"))

    if request.method == "POST":
        action = request.form.get("action", "profile")
        if action == "profile":
            name = request.form.get("name", "").strip()
            if len(name) < 2:
                flash("Display name must include at least 2 characters.", "error")
            else:
                with db_connect() as db:
                    db.execute("UPDATE users SET name = ? WHERE id = ?", (name, session["user_id"]))
                flash("Profile updated successfully.", "success")
        elif action == "password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            with db_connect() as db:
                user = db.execute("SELECT password_hash FROM users WHERE id = ?", (session["user_id"],)).fetchone()
            if not user or not check_password_hash(user["password_hash"], current_password):
                flash("The current password is incorrect.", "error")
            elif len(new_password) < 8:
                flash("Use a new password with at least 8 characters.", "error")
            elif new_password != confirm_password:
                flash("New password and confirmation do not match.", "error")
            else:
                with db_connect() as db:
                    db.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new_password), session["user_id"]),
                    )
                session.clear()
                flash("Password updated. Please sign in again.", "success")
                return redirect(url_for("login"))
        return redirect(url_for("account"))

    with db_connect() as db:
        user = db.execute(
            "SELECT id, name, email, auth_provider, avatar_url, created_at FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
    return render_template(
        "account.html",
        user=dict(user) if user else {},
        guest_mode=False,
        active="account",
    )


@app.get("/create")
def create():
    layout_options = [
        {"layout": layout, **LAYOUT_METADATA[layout]}
        for layout in SELECTABLE_LAYOUTS
    ]
    return render_template("workspace.html", active="create", layout_options=layout_options)


@app.get("/analyze")
def analyze_page():
    return render_template("workspace.html", active="analyze")


@app.get("/compare")
def compare_page():
    return render_template("workspace.html", active="compare")


@app.get("/optimize")
def optimize_page():
    return render_template("workspace.html", active="optimize")


@app.get("/quiz")
def quiz_page():
    return render_template("workspace.html", active="quiz")


@app.get("/readiness")
def readiness_page():
    return render_template("workspace.html", active="readiness")


@app.get("/shorten")
def shorten_page():
    return render_template("workspace.html", active="shorten")


@app.get("/templates")
def templates_page():
    return render_template("workspace.html", active="templates")


# ---------- APIs ----------

@app.post("/api/generate")
def api_generate():
    data = request.get_json(silent=True) or {}
    generation_id = str(data.get("generation_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", generation_id):
        generation_id = ""

    topic = str(data.get("topic", "")).strip()[:180]
    if len(topic) < 3:
        return jsonify(error="Enter a topic with at least 3 characters."), 400

    try:
        count = int(data.get("count", 8))
    except (TypeError, ValueError):
        return jsonify(error="Slide count must be a number."), 400

    if not 4 <= count <= 20:
        return jsonify(error="Choose between 4 and 20 slides."), 400

    audience = str(data.get("audience", "General audience"))[:100]
    purpose = str(data.get("purpose", "Presentation"))[:100]
    tone = str(data.get("tone", "Professional"))[:80]
    provider_choice = str(data.get("provider", "automatic")).strip().lower()
    if provider_choice not in {"gemini", "groq", "automatic"}:
        return jsonify(error="Please refresh the page and try again."), 400
    selected_template = str(data.get("template", "")).strip()[:40]
    if selected_template and selected_template not in SELECTABLE_LAYOUTS:
        return jsonify(error="Choose one of the available slide layouts."), 400

    try:
        update_generation_status(
            generation_id, "understand", "Understanding your idea and presentation goals."
        )
        slides, _, _generation_detail = generate_slides_with_fallback(
            topic,
            count,
            audience,
            purpose,
            tone,
            progress_callback=(
                lambda stage, message: update_generation_status(
                    generation_id, stage, message
                )
            ) if generation_id else None,
            preferred_provider=provider_choice,
        )

        if slides is None and (
            os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GROQ_API_KEY", "").strip()
        ):
            clear_generation_status(generation_id)
            detail = str(_generation_detail or "").strip()
            if detail:
                app.logger.warning("Presentation content generation did not complete: %s", detail)
            return jsonify(
                error="We couldn't create your presentation right now. Please try again in a moment."
            ), 502

        if slides is None:
            slides = offline_slides(topic, count, audience, purpose)

        if selected_template in SELECTABLE_LAYOUTS:
            for slide_index, slide in enumerate(slides):
                if slide_index == 0:
                    slide["visual"]["layout"] = "cover"
                elif slide_index == len(slides) - 1:
                    slide["visual"]["layout"] = "conclusion"
                elif selected_template == "chart" and not slide.get("chart"):
                    continue
                elif selected_template == "diagram" and not slide.get("diagram"):
                    continue
                else:
                    slide["visual"]["layout"] = selected_template

        update_generation_status(
            generation_id, "visuals", "Preparing your slide visuals."
        )
        image_status = enrich_slide_visuals(slides, topic)
        update_generation_status(
            generation_id, "finalize", "Finalizing your presentation."
        )

        if session.get("user_id"):
            db = db_connect()
            try:
                db.execute(
                    """
                    INSERT INTO projects
                    (user_id, title, topic, slide_count, slides_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session["user_id"],
                        f"{topic} presentation",
                        topic,
                        len(slides),
                        json.dumps(slides),
                    ),
                )
                db.commit()
            finally:
                db.close()

        clear_generation_status(generation_id)
        return jsonify(
            slides=slides,
            generation_id=generation_id,
            slide_count=len(slides),
            image_status=image_status,
            saved=bool(session.get("user_id")),
            analysis=analyze_slides(slides),
        )

    except Exception:
        clear_generation_status(generation_id)
        app.logger.exception("Slide generation failed.")
        return jsonify(
            error=(
                "Slide generation could not be completed. Please try again; your settings are unchanged."
            )
        ), 500

@app.get("/api/generation-status/<generation_id>")
def api_generation_status(generation_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", generation_id):
        return jsonify(error="Generation status is unavailable."), 404
    status = generation_status(generation_id)
    if not status:
        return jsonify(error="Generation status is no longer available."), 404
    return jsonify(status)


def firefly_is_configured():
    client_id, client_secret, scopes = firefly_configuration()
    return bool(client_id and client_secret and scopes)


def firefly_session_id():
    value = session.get("firefly_session_id")
    if not value:
        value = secrets.token_urlsafe(32)
        session["firefly_session_id"] = value
    return value


def get_firefly_job(job_id):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,150}", str(job_id or "")):
        return None
    owner = session.get("firefly_session_id")
    with FIREFLY_JOBS_LOCK:
        job = FIREFLY_JOBS.get(job_id)
        if not job or not owner or job["owner"] != owner:
            return None
        if job["created_at"] < time.time() - 3600:
            FIREFLY_JOBS.pop(job_id, None)
            return None
        return job


@app.get("/api/firefly/status")
def api_firefly_status():
    configured = firefly_is_configured()
    return jsonify(
        available=configured,
        message=(
            "Adobe Firefly credentials are configured; access will be verified when you generate an image."
            if configured
            else "Adobe Firefly is not configured. Add server-side Adobe credentials to enable it."
        ),
    )


@app.post("/api/firefly/generate")
def api_firefly_generate():
    if not firefly_is_configured():
        return jsonify(error="Adobe Firefly is not configured for this application."), 503
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    negative_prompt = str(data.get("negative_prompt", "")).strip()
    if not 1 <= len(prompt) <= 1024:
        return jsonify(error="Image prompts must contain between 1 and 1,024 characters."), 400
    if len(negative_prompt) > 1024:
        return jsonify(error="Negative prompts may not exceed 1,024 characters."), 400
    try:
        slide_index = int(data.get("slide_index", -1))
        variations = int(data.get("num_variations", 2))
    except (TypeError, ValueError):
        return jsonify(error="Choose a valid slide and variation count."), 400
    if not 0 <= slide_index < MAX_SLIDES:
        return jsonify(error="Choose a valid slide."), 400
    if not 1 <= variations <= 4:
        return jsonify(error="Choose between 1 and 4 image variations."), 400
    model = str(data.get("model", "image4_standard"))
    if model not in {"image3", "image4_standard"}:
        return jsonify(error="Choose a supported Adobe Firefly image model."), 400
    aspect_ratio = str(data.get("aspect_ratio", "16:9"))
    size = FIREFLY_IMAGE_SIZES.get(aspect_ratio)
    if not size:
        return jsonify(error="Choose a supported image aspect ratio."), 400

    body = {
        "prompt": prompt,
        "numVariations": variations,
        "size": size,
    }
    if negative_prompt:
        body["negativePrompt"] = negative_prompt
    try:
        response = requests.post(
            "https://firefly-api.adobe.io/v3/images/generate-async",
            headers={**firefly_headers(), "x-model-version": model},
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except FireflyServiceError as exc:
        return jsonify(error=str(exc)), 503
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        app.logger.warning("Adobe Firefly generation request failed (HTTP %s).", status_code or "unavailable")
        return jsonify(error="Adobe Firefly could not start image generation. Check API access and try again."), 502
    except (ValueError, TypeError) as exc:
        app.logger.warning("Adobe Firefly returned an invalid generation response.")
        return jsonify(error="Adobe Firefly returned an invalid response. Please try again."), 502

    job_id = str(payload.get("jobId", "")) if isinstance(payload, dict) else ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,150}", job_id):
        app.logger.warning("Adobe Firefly response did not contain a valid job identifier.")
        return jsonify(error="Adobe Firefly returned an invalid job identifier."), 502
    owner = firefly_session_id()
    now = time.time()
    with FIREFLY_JOBS_LOCK:
        expired = [
            existing_id
            for existing_id, job in FIREFLY_JOBS.items()
            if job["created_at"] < now - 3600
        ]
        for existing_id in expired:
            FIREFLY_JOBS.pop(existing_id, None)
        FIREFLY_JOBS[job_id] = {
            "owner": owner,
            "created_at": now,
            "status": "running",
            "outputs": [],
            "prompt": prompt,
            "slide_index": slide_index,
        }
    return jsonify(job_id=job_id, status="running"), 202


@app.get("/api/firefly/jobs/<job_id>")
def api_firefly_job_status(job_id):
    job = get_firefly_job(job_id)
    if not job:
        return jsonify(error="This Adobe Firefly job is unavailable."), 404
    if job["status"] in {"succeeded", "failed"}:
        if job["status"] == "failed":
            return jsonify(status="failed", error=job["error"]), 200
        return jsonify(status="succeeded", outputs=job["outputs"]), 200
    try:
        response = requests.get(
            f"https://firefly-api.adobe.io/v3/status/{job_id}",
            headers=firefly_headers(),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except FireflyServiceError as exc:
        return jsonify(error=str(exc)), 503
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        app.logger.warning("Adobe Firefly job polling failed (HTTP %s).", status_code or "unavailable")
        return jsonify(error="Adobe Firefly status is temporarily unavailable. Try again."), 502
    except (ValueError, TypeError) as exc:
        app.logger.warning("Adobe Firefly returned an invalid job status response.")
        return jsonify(error="Adobe Firefly returned an invalid job status. Please try again."), 502

    if not isinstance(payload, dict):
        return jsonify(error="Adobe Firefly returned an invalid job status."), 502
    status = str(payload.get("status", ""))
    if status in {"running", "cancel_pending"}:
        return jsonify(status="running"), 200
    if status != "succeeded":
        message = "Adobe Firefly could not complete this image request."
        with FIREFLY_JOBS_LOCK:
            job["status"] = "failed"
            job["error"] = message
        return jsonify(status="failed", error=message), 200

    result = payload.get("result")
    raw_outputs = result.get("outputs") if isinstance(result, dict) else None
    if not isinstance(raw_outputs, list) or not raw_outputs:
        return jsonify(error="Adobe Firefly completed without returning image variations."), 502
    outputs = []
    try:
        for output in raw_outputs[:4]:
            image = output.get("image") if isinstance(output, dict) else None
            image_url = image.get("url") if isinstance(image, dict) else None
            if not image_url:
                raise FireflyServiceError("Adobe Firefly returned an incomplete image variation.")
            asset = cache_firefly_output(image_url)
            outputs.append({
                "asset": asset,
                "image_url": f"/api/slide-image/{asset}",
                "alt": job["prompt"][:240],
            })
    except FireflyServiceError as exc:
        app.logger.warning("Adobe Firefly output could not be cached: %s", str(exc))
        return jsonify(error=str(exc)), 502

    with FIREFLY_JOBS_LOCK:
        job["status"] = "succeeded"
        job["outputs"] = outputs
    return jsonify(status="succeeded", outputs=outputs), 200


@app.post("/api/firefly/accept")
def api_firefly_accept():
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id", ""))
    job = get_firefly_job(job_id)
    if not job or job["status"] != "succeeded":
        return jsonify(error="Generate and preview an image before accepting it."), 404
    try:
        output_index = int(data.get("output_index", -1))
    except (TypeError, ValueError):
        return jsonify(error="Choose a valid image variation."), 400
    if not 0 <= output_index < len(job["outputs"]):
        return jsonify(error="Choose a valid image variation."), 400
    output = job["outputs"][output_index]
    if not cached_image_path(output["asset"]):
        return jsonify(error="This generated image is no longer in the local cache."), 404
    return jsonify(visual={
        "layout": "image_left",
        "description": "Original image generated with Adobe Firefly.",
        "image_query": job["prompt"][:160],
        "image_asset": output["asset"],
        "image_url": output["image_url"],
        "image_alt": output["alt"],
        "image_credit": "Adobe Firefly",
        "image_provider": "adobe_firefly",
        "image_source": "",
        "photographer_url": "",
        "unsplash_url": "",
    })


@app.get("/api/slide-image/<asset>")
def api_slide_image(asset):
    path = cached_image_path(asset)
    if not path:
        return jsonify(error="This cached presentation image is no longer available."), 404
    mimetype = "image/png" if path.suffix == ".png" else "image/jpeg"
    return send_file(path, mimetype=mimetype, max_age=86400, conditional=True)


@app.post("/api/quiz")
def api_quiz():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename.lower().endswith(".pptx"):
        return jsonify(error="Choose a valid PowerPoint .pptx file."), 400
    try:
        slides = extract_pptx(uploaded.stream)
        if not slides:
            return jsonify(error="This presentation contains no readable slide text."), 400
        try:
            count = int(request.form.get("count", 6))
        except (TypeError, ValueError):
            return jsonify(error="Choose a valid quiz question count."), 400
        if not 3 <= count <= 20:
            return jsonify(error="Choose between 3 and 20 quiz questions."), 400
        difficulty = str(request.form.get("difficulty", "Medium")).strip().title()
        question_type = str(request.form.get("type", "Multiple Choice")).strip()
        if difficulty not in {"Easy", "Medium", "Hard"}:
            return jsonify(error="Choose Easy, Medium, or Hard difficulty."), 400
        if question_type not in {"Multiple Choice", "True or False", "Short Answer"}:
            return jsonify(error="Choose a supported quiz question type."), 400
        source_text = "\n\n".join(
            f"Slide {i}: {s['title']}\n{s.get('body', '')}\n" + "\n".join(s["bullets"])
            for i, s in enumerate(slides, start=1)
        )
        prompt = (
            f"Create exactly {count} {difficulty.lower()} {question_type.lower()} quiz questions "
            "from the presentation text below. Do not use outside facts. Avoid duplicate questions. "
            "Return a JSON object with key \"questions\". Each item must include "
            "\"question\", \"type\", \"options\" (an array for multiple choice; otherwise []), "
            "\"answer\", and \"explanation\".\n\n" + source_text
        )
        generated = ai_json(prompt)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        used_fallback = False
        if not isinstance(questions, list) or not questions:
            used_fallback = True
            facts = [
                (slide_index, slide["title"], bullet)
                for slide_index, slide in enumerate(slides, start=1)
                for bullet in (slide["bullets"] or [slide["title"]])
                if str(bullet).strip()
            ]
            questions = []
            for index, (slide_index, title, fact) in enumerate(facts):
                if len(questions) >= count:
                    break
                if question_type == "Multiple Choice":
                    target_facts = {
                        str(bullet).casefold()
                        for bullet in (slides[slide_index - 1]["bullets"] or [title])
                    }
                    distractors = [
                        other_fact for other_index, _other_title, other_fact in facts
                        if other_index != slide_index and other_fact.casefold() != fact.casefold()
                        and other_fact.casefold() not in target_facts
                    ]
                    options = list(dict.fromkeys([fact, *distractors[:3]]))
                    if len(options) < 2:
                        continue
                    questions.append({
                        "question": f"Which point is listed on slide {slide_index}, “{title}”?",
                        "options": options,
                        "answer": fact,
                        "explanation": "This point appears in the uploaded slide text.",
                    })
                elif question_type == "True or False":
                    is_listed = index % 2 == 0
                    statement = fact if is_listed else next(
                        (
                            other_fact for other_index, _other_title, other_fact in facts
                            if other_index != slide_index
                            and other_fact.casefold() not in {
                                bullet.casefold()
                                for bullet in slides[slide_index - 1]["bullets"]
                            }
                        ),
                        "",
                    )
                    if not statement:
                        continue
                    questions.append({
                        "question": f"This exact statement appears on slide {slide_index}, “{title}”: {statement}",
                        "options": [],
                        "answer": "True" if is_listed else "False",
                        "explanation": "Check the selected slide's extracted text.",
                    })
                else:
                    questions.append({
                        "question": f"What point is listed on slide {slide_index}, “{title}”?",
                        "options": [],
                        "answer": fact,
                        "explanation": "This answer comes directly from the uploaded slide text.",
                    })
        clean = []
        for item in questions[:count]:
            if not isinstance(item, dict) or not str(item.get("question", "")).strip():
                continue
            answer = str(item.get("answer", "")).strip()[:500]
            options = [
                str(option).strip()[:300]
                for option in item.get("options", [])
                if str(option).strip()
            ][:6] if isinstance(item.get("options"), list) else []
            if not answer:
                continue
            if question_type == "Multiple Choice" and (
                len(set(option.casefold() for option in options)) < 2
                or answer.casefold() not in {option.casefold() for option in options}
            ):
                continue
            if question_type == "True or False":
                if answer.casefold() not in {"true", "false"}:
                    continue
                answer = "True" if answer.casefold() == "true" else "False"
                options = ["True", "False"]
            else:
                options = options if question_type == "Multiple Choice" else []
            clean.append({
                "number": len(clean) + 1,
                "question": str(item.get("question", "")).strip()[:500],
                "type": question_type,
                "options": options,
                "answer": answer,
                "explanation": str(item.get("explanation", "")).strip()[:1000],
            })
        if not clean:
            return jsonify(error="No quiz questions could be generated from this presentation."), 400
        return jsonify(
            questions=clean,
            difficulty=difficulty,
            source_slide_count=len(slides),
            local_fallback=used_fallback,
        )
    except Exception as exc:
        app.logger.exception("Quiz generation failed")
        return jsonify(error=f"Could not generate a quiz: {str(exc)[:160]}"), 400


@app.post("/api/quiz/export/pptx")
def api_quiz_export():
    data = request.get_json(silent=True) or {}
    questions = data.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 20:
        return jsonify(error="Provide between 1 and 20 quiz questions."), 400

    difficulty = str(data.get("difficulty", "Medium")).strip().title()
    if difficulty not in {"Easy", "Medium", "Hard"}:
        return jsonify(error="Choose a valid quiz difficulty."), 400

    clean_questions = []
    for item in questions:
        if not isinstance(item, dict):
            return jsonify(error="Quiz question data is invalid."), 400
        question = str(item.get("question", "")).strip()
        question_type = str(item.get("type", "Multiple Choice")).strip()
        answer = str(item.get("answer", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        options = item.get("options", [])
        if (
            not question or len(question) > 500
            or question_type not in {"Multiple Choice", "True or False", "Short Answer"}
            or not answer or len(answer) > 500
            or len(explanation) > 1000
            or not isinstance(options, list) or len(options) > 6
        ):
            return jsonify(error="Quiz question content is invalid or too long to export."), 400
        clean_options = [str(option).strip() for option in options]
        if any(not option or len(option) > 300 for option in clean_options):
            return jsonify(error="Quiz answer options must be between 1 and 300 characters."), 400
        if question_type == "Multiple Choice" and len(clean_options) < 2:
            return jsonify(error="Multiple-choice questions need at least two options."), 400
        clean_questions.append({
            "question": question,
            "type": question_type,
            "options": clean_options,
            "answer": answer,
            "explanation": explanation,
        })

    try:
        output = make_quiz_pptx(clean_questions, difficulty)
        quality_report = validate_and_correct_pptx(Presentation(output))
        output.seek(0)
        response = send_file(
            output,
            as_attachment=True,
            download_name=export_filename("SlideCraft Quiz", "pptx"),
            mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        response.headers["X-SlideCraft-Quality-Report"] = json.dumps(
            quality_report,
            separators=(",", ":"),
        )[:3000]
        return response
    except Exception:
        app.logger.exception("Quiz PowerPoint export failed")
        return jsonify(error="Quiz PowerPoint export failed. Please try again."), 500


@app.post("/api/readiness")
def api_readiness():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename.lower().endswith(".pptx"):
        return jsonify(error="Choose a valid PowerPoint .pptx file."), 400
    try:
        slides = extract_pptx(uploaded.stream)
        if not slides:
            return jsonify(error="This presentation contains no readable slide text."), 400
        return jsonify(slides=slides, readiness=readiness_evaluation(slides))
    except Exception as exc:
        app.logger.exception("Readiness evaluation failed")
        return jsonify(error=f"Could not evaluate this presentation: {str(exc)[:160]}"), 400


@app.post("/api/shorten")
def api_shorten():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()[:5000]
    level = str(data.get("level", "Balanced")).strip().lower()
    if len(text) < 5:
        return jsonify(error="Enter at least five characters to shorten."), 400
    if level not in {"slightly shorter", "balanced", "highly concise"}:
        return jsonify(error="Choose a supported shortening level."), 400
    level_ratios = {
        "slightly shorter": 0.78,
        "balanced": 0.58,
        "highly concise": 0.38,
    }
    words = text.split()
    required_numbers = set(re.findall(r"\b\d+(?:[,.]\d+)*%?\b", text))

    def fallback_shortening():
        if len(words) <= 1:
            return text
        keep = max(1, min(len(words) - 1, int(len(words) * level_ratios[level])))
        selected = set(range(keep))
        for number in required_numbers:
            for index, word in enumerate(words):
                if number in word:
                    selected.update(range(max(0, index - 1), min(len(words), index + 2)))
        if len(selected) >= len(words):
            return text
        shortened_words = [words[index] for index in sorted(selected)]
        return " ".join(shortened_words).rstrip(" .,;:-") + "..."

    prompt = (
        f"Shorten the presentation text using the '{level}' level. Remove unnecessary words, "
        "preserve facts, names, dates, figures, technical terms, and meaning. "
        "Return JSON with keys \"original\", \"shortened\", \"changes\", and \"layout_check\".\n\n"
        + text
    )
    generated = ai_json(prompt)
    fallback_used = False
    if not isinstance(generated, dict) or not str(generated.get("shortened", "")).strip():
        fallback_used = True
        shortened = fallback_shortening()
        generated = {
            "shortened": shortened,
            "changes": ["Local fallback shortened the text; verify that meaning is preserved."],
            "layout_check": "Text density should be reviewed in the slide editor.",
        }
    shortened = str(generated.get("shortened", "")).strip()[:5000]
    missing_numbers = required_numbers - set(
        re.findall(r"\b\d+(?:[,.]\d+)*%?\b", shortened)
    )
    if (
        len(shortened.split()) >= len(text.split())
        or missing_numbers
    ):
        fallback_used = True
        shortened = fallback_shortening()
        if required_numbers - set(re.findall(r"\b\d+(?:[,.]\d+)*%?\b", shortened)):
            shortened = text
            generated["changes"] = [
                "The source numbers could not be preserved in a shorter version; the original was kept."
            ]
            generated["layout_check"] = "The original text was kept to avoid changing its figures."
        elif shortened == text:
            generated["changes"] = ["The source is already very brief; no shortening was applied."]
            generated["layout_check"] = "The original text was kept to preserve its wording and figures."
        else:
            generated["changes"] = [
                "A shorter version was prepared locally; verify that meaning is preserved."
            ]
            generated["layout_check"] = "Text density should be reviewed in the slide editor."
    elif fallback_used:
        generated.setdefault("changes", [
            "Local fallback shortened the text; verify that meaning is preserved."
        ])
    return jsonify(
        original=text,
        shortened=shortened,
        changes=[str(x).strip()[:300] for x in generated.get("changes", []) if str(x).strip()][:10]
            if isinstance(generated.get("changes"), list) else [],
        layout_check=str(generated.get("layout_check", "Review the slide after applying this text.")).strip()[:500],
    )


@app.post("/api/templates/recommend")
def api_template_recommend():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()[:180]
    audience = str(data.get("audience", "General audience")).strip()[:100]
    purpose = str(data.get("purpose", "Presentation")).strip()[:100]
    if len(topic) < 3:
        return jsonify(error="Enter a topic with at least three characters."), 400
    requirements = None
    prompt = (
        "Analyze these presentation requirements and return JSON with keys "
        "\"content_type\", \"technical_complexity\", \"text_density\", \"tone\", "
        "\"visual_requirements\", \"needs_charts\", \"needs_diagrams\", \"needs_images\".\n"
        f"Topic: {topic}\nAudience: {audience}\nPurpose: {purpose}"
    )
    ai_result = ai_json(prompt)
    if isinstance(ai_result, dict):
        requirements = ai_result
    recommendations = recommend_templates(topic, audience, purpose, requirements)
    return jsonify(
        requirements=requirements or {
            "content_type": "general",
            "technical_complexity": "medium",
            "text_density": "medium",
            "tone": "professional",
            "visual_requirements": ["relevant visuals"],
            "needs_charts": False,
            "needs_diagrams": False,
            "needs_images": True,
        },
        recommendations=recommendations,
        ai_enhanced=bool(requirements),
    )


@app.post("/api/analyze")
def api_analyze():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename.lower().endswith(".pptx"):
        return jsonify(error="Choose a PowerPoint .pptx file."), 400
    try:
        slides = extract_pptx(uploaded.stream)
        return jsonify(slides=slides, analysis=analyze_slides(slides))
    except Exception as exc:
        app.logger.exception("PPTX analysis failed")
        return jsonify(error=f"Could not read this presentation: {str(exc)[:160]}"), 400


@app.post("/api/compare")
def api_compare():
    a, b = request.files.get("a"), request.files.get("b")
    if not a or not b or not a.filename.lower().endswith(".pptx") or not b.filename.lower().endswith(".pptx"):
        return jsonify(error="Choose two .pptx files to compare."), 400
    try:
        result_a = analyze_slides(extract_pptx(a.stream))
        result_b = analyze_slides(extract_pptx(b.stream))
        if result_a["score"] > result_b["score"]:
            verdict = "Presentation A has the higher text-based score."
        elif result_b["score"] > result_a["score"]:
            verdict = "Presentation B has the higher text-based score."
        else:
            verdict = "Both presentations have the same text-based score."
        return jsonify(a=result_a, b=result_b, verdict=verdict)
    except Exception as exc:
        app.logger.exception("PPTX comparison failed")
        return jsonify(error=f"Could not compare these files: {str(exc)[:160]}"), 400


@app.post("/api/optimize")
def api_optimize():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename.lower().endswith(".pptx"):
        return jsonify(error="Choose a PowerPoint .pptx file."), 400
    try:
        slides = extract_pptx(uploaded.stream)
        optimized = []
        for slide in slides:
            optimized.append({
                "title": re.sub(r"\s+", " ", slide["title"]).strip()[:90],
                "bullets": [re.sub(r"\s+", " ", b).strip() for b in slide["bullets"]][:6],
                "notes": slide["notes"],
            })
        return jsonify(original=slides, optimized=optimized, before=analyze_slides(slides), after=analyze_slides(optimized))
    except Exception as exc:
        app.logger.exception("PPTX optimization failed")
        return jsonify(error=f"Could not optimize this file: {str(exc)[:160]}"), 400


@app.post("/api/export")
@app.post("/api/export/pptx")
def api_export():
    data = request.get_json(silent=True) or {}
    slides = data.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_SLIDES:
        return jsonify(error=f"Provide between 1 and {MAX_SLIDES} slides."), 400
    try:
        output = make_pptx([normalize_slide(s) for s in slides])
        reopened = Presentation(output)
        quality_report = validate_and_correct_pptx(reopened)
        output.seek(0)
        response = send_file(
            output,
            as_attachment=True,
            download_name=export_filename(data.get("title"), "pptx"),
            mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        response.headers["X-SlideCraft-Quality-Report"] = json.dumps(
            quality_report,
            separators=(",", ":"),
        )[:3000]
        return response
    except Exception:
        app.logger.exception("PPTX export failed")
        return jsonify(error="PowerPoint export failed. Please try again."), 500


@app.post("/api/export/pdf")
def api_export_pdf():
    data = request.get_json(silent=True) or {}
    slides = data.get("slides")
    preset = str(data.get("preset", "presentation"))
    if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_SLIDES:
        return jsonify(error=f"Provide between 1 and {MAX_SLIDES} slides."), 400
    try:
        normalized_slides = [normalize_slide(slide) for slide in slides]
        output = generate_pdf(normalized_slides, preset, cached_image_path)
        return send_file(
            output,
            as_attachment=True,
            download_name=export_filename(data.get("title"), "pdf", preset),
            mimetype="application/pdf",
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        app.logger.exception("PDF export failed.")
        return jsonify(error="PDF export failed. Please try again."), 500


@app.post("/api/export/word")
@app.post("/api/export/docx")
def api_export_word():
    data = request.get_json(silent=True) or {}
    slides = data.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_SLIDES:
        return jsonify(error=f"Provide between 1 and {MAX_SLIDES} slides."), 400

    try:
        normalized_slides = [normalize_slide(slide) for slide in slides]
        has_content = any(
            slide["title"] != "Untitled slide"
            or slide["body"]
            or slide["bullets"]
            or slide["notes"]
            or slide["visual"]["description"]
            or slide["visual"]["image_query"]
            or slide["visual"]["image_credit"]
            or slide["chart"]
            or slide["diagram"]
            for slide in normalized_slides
        )
        if not has_content:
            return jsonify(error="This presentation has no valid slide content to export."), 400

        title = str(data.get("title") or normalized_slides[0]["title"]).strip()[:160]
        title = title or normalized_slides[0]["title"]
        output = generate_word_document(normalized_slides, title, cached_image_path)
        return send_file(
            output,
            as_attachment=True,
            download_name=export_filename(title, "docx"),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            max_age=0,
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        app.logger.exception("Word export failed.")
        return jsonify(error="Word export failed. Please try again."), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "0") == "1")
