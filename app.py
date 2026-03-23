from __future__ import annotations

import csv
import io
import json
import os
import secrets
import smtplib
import sqlite3
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
    REQUESTS_EXCEPTION = requests.RequestException
except ModuleNotFoundError:
    requests = None

    class REQUESTS_EXCEPTION(Exception):
        pass

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None

try:
    import cv2
    import numpy as np
except ModuleNotFoundError:
    cv2 = None
    np = None
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    Response,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR = Path(os.getenv("INSTANCE_DIR", str(DEFAULT_INSTANCE_DIR))).resolve()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(INSTANCE_DIR / "gfg_club.db"))).resolve()
ASSETS_DIR = BASE_DIR / "Assets"
ADMIN_ROLES = {"admin", "core"}
MEMBER_ROLES = {"member", "admin", "core"}
ENV_SECURED_PATH = BASE_DIR / ".env_secured"
RIT_DEPARTMENT_MAP = {
    "20": {"name": "Computer Science and Engineering", "slug": "cse"},
    "03": {"name": "Artificial Intelligence and Machine Learning", "slug": "ai-ml"},
    "70": {"name": "Artificial Intelligence and Data Science", "slug": "ai-ds"},
    "40": {"name": "Electronics and Communication Engineering", "slug": "ece"},
}
OD_STATUS_REGISTERED = "Registered"
OD_STATUS_ADMIN_SELECTED = "Admin_Selected"
OD_STATUS_HOD_AUTHORIZED = "HOD_Authorized"
OD_STATUS_OC_VERIFIED = "OC_Verified"


def load_env_secured() -> None:
    if not ENV_SECURED_PATH.exists():
        return

    for raw_line in ENV_SECURED_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_secured()


def create_app() -> Flask:
    app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "gfg-campus-club-rit-2026"),
        DATABASE=str(DATABASE_PATH),
    )

    INSTANCE_DIR.mkdir(exist_ok=True)
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        init_db()
        app.logger.info("Auth backend: SQLite users table")

    @app.before_request
    def sync_event_automation() -> None:
        apply_event_automation_rules()
        sync_hosted_event_total()

    @app.teardown_appcontext
    def close_db(_: object | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_shell_context() -> dict[str, object | None]:
        db = get_db()
        next_event = db.execute(
            """
            SELECT id, title, event_date, category
            FROM events
            ORDER BY event_date ASC
            LIMIT 1
            """
        ).fetchone()
        open_registration_event_row = db.execute(
            """
            SELECT events.id, events.title, COALESCE(events."date", events.event_date) AS event_date,
                   COALESCE(events.registration_deadline, COALESCE(events."date", events.event_date)) AS registration_deadline,
                   COALESCE(events.registration_closing_time, '23:59') AS registration_closing_time,
                   COALESCE(events.registration_enabled, 0) AS registration_enabled,
                   COALESCE(events.registration_limit, 0) AS registration_limit,
                   COUNT(event_registrations.id) AS registration_count
            FROM events
            LEFT JOIN event_registrations ON event_registrations.event_id = events.id
            GROUP BY events.id
            HAVING COALESCE(events.registration_enabled, 0) = 1
               AND (
                    COALESCE(events.registration_limit, 0) = 0
                    OR COUNT(event_registrations.id) < COALESCE(events.registration_limit, 0)
               )
            ORDER BY COALESCE(events."date", events.event_date) ASC
            LIMIT 1
            """
        ).fetchone()
        open_registration_event = (
            build_event_registration_meta(open_registration_event_row)
            if open_registration_event_row is not None
            else None
        )
        featured_resource = db.execute(
            """
            SELECT id, title, track, difficulty
            FROM resources
            ORDER BY CASE difficulty
                WHEN 'Intermediate' THEN 0
                WHEN 'Beginner' THEN 1
                ELSE 2
            END, id ASC
            LIMIT 1
            """
        ).fetchone()
        settings = get_site_settings()
        current_user = get_current_user()
        return {
            "current_user": current_user,
            "quick_next_event": next_event,
            "quick_open_registration_event": open_registration_event,
            "quick_featured_resource": featured_resource,
            "admin_bootstrap_needed": not has_admin_account(),
            "site_settings": settings,
            "contact_links": build_contact_links(),
        }

    @app.route("/")
    def index():
        return redirect(url_for("home"))

    @app.route("/club-assets/<path:filename>")
    def club_assets(filename: str):
        return send_from_directory(ASSETS_DIR, filename)

    @app.route("/api/rit-identity", methods=("POST",))
    def api_rit_identity():
        payload = request.get_json(silent=True) or {}
        verification_url = str(payload.get("verification_url", "")).strip()
        try:
            identity = build_identity_payload(verification_url)
        except REQUESTS_EXCEPTION as exc:
            return jsonify({"ok": False, "error": f"RIT verification page could not be reached: {exc}"}), 502
        except RuntimeError as exc:
            status_code = 502 if str(exc).startswith("RIT verification page could not be reached") else 500
            return jsonify({"ok": False, "error": str(exc)}), status_code
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected RIT identity verification failure for URL: %s", verification_url)
            return jsonify({"ok": False, "error": f"Unexpected verification failure: {exc}"}), 500
        return jsonify({"ok": True, "identity": identity})

    def decode_qr_image_bytes(image_bytes: bytes) -> str:
        if cv2 is None or np is None:
            raise RuntimeError("OpenCV (cv2) is not installed. QR image decoding is unavailable.")
        
        detector = cv2.QRCodeDetector()
        file_bytes = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("The uploaded file is not a valid image.")
            
        decoded_text, points, _ = detector.detectAndDecode(image)
        if not decoded_text:
            raise ValueError("Could not find or read a QR code in the uploaded image.")
            
        return decoded_text

    @app.route("/api/decode-qr-image", methods=("POST",))
    def api_decode_qr_image():
        uploaded_file = request.files.get("qr_image")
        if uploaded_file is None:
            return jsonify({"ok": False, "error": "Upload a QR image first."}), 400
        try:
            decoded_text = decode_qr_image_bytes(uploaded_file.read())
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "decoded_text": decoded_text})


    @app.route("/admin/setup", methods=("GET", "POST"))
    def admin_setup():
        if has_admin_account():
            flash("Core/admin access is already configured. Sign in with the core or admin account.", "error")
            return redirect(url_for("admin_login"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone_number = normalize_phone_number(request.form.get("phone_number", ""))
            password = request.form.get("password", "")
            register_number = request.form.get("register_number", "").strip()
            course = request.form.get("course", "").strip()
            department_code = request.form.get("department_code", "").strip()
            department_name = request.form.get("department_name", "").strip()
            department_slug = request.form.get("department_slug", "").strip()

            if not all([name, email, phone_number, password, register_number, course]):
                flash("Complete every sign-up field, including verifying your RIT ID.", "error")
            else:
                db = get_db()
                existing_user = db.execute(
                    "SELECT id FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                duplicate_phone = db.execute(
                    "SELECT id FROM users WHERE phone_number = ? AND email != ?",
                    (phone_number, email),
                ).fetchone()
                existing_reg = db.execute(
                    "SELECT id FROM users WHERE register_number = ?",
                    (register_number,),
                ).fetchone()
                
                if existing_reg is not None:
                    flash("An account already exists for this register number.", "error")
                elif duplicate_phone is not None:
                    flash("That phone number is already linked to another account.", "error")
                elif existing_user is not None:
                    db.execute(
                        """
                        UPDATE users
                        SET name = ?, phone_number = ?, password_hash = ?, role = ?,
                            register_number = ?, course = ?, department_code = ?, department_name = ?, department_slug = ?,
                            rit_username = ?, external_email = NULL, college_name = ?, user_type = ?, is_active = 1
                        WHERE id = ?
                        """,
                        (name, phone_number, generate_password_hash(password), "admin",
                         register_number, course, department_code, department_name, department_slug,
                         derive_rit_username(email), "Rajalakshmi Institute of Technology", "internal",
                         existing_user["id"]),
                    )
                    user_id = existing_user["id"]
                else:
                    db.execute(
                        """
                        INSERT INTO users (
                            name, email, phone_number, password_hash, role,
                            register_number, course, department_code, department_name, department_slug,
                            rit_username, external_email, college_name, user_type, is_active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (name, email, phone_number, generate_password_hash(password), "admin",
                         register_number, course, department_code, department_name, department_slug,
                         derive_rit_username(email), None, "Rajalakshmi Institute of Technology", "internal", 1),
                    )
                    user_id = db.execute(
                        "SELECT id FROM users WHERE email = ?",
                        (email,),
                    ).fetchone()["id"]
                db.commit()
                session.clear()
                session["user_id"] = user_id
                flash("Admin account created. You now have full admin access.", "success")
                return redirect(url_for("admin"))

        return render_template("admin_setup.html")

    @app.route("/signup", methods=("GET", "POST"))
    def signup():
        if get_current_user() is not None:
            return redirect(url_for("home"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone_number = normalize_phone_number(request.form.get("phone_number", ""))
            password = request.form.get("password", "")
            course = request.form.get("course", "").strip()
            requested_user_type = request.form.get("user_type", "internal").strip().lower() or "internal"
            user_type = "external" if requested_user_type == "external" else "internal"
            college_name = request.form.get("college_name", "").strip() or "Rajalakshmi Institute of Technology"
            external_email = request.form.get("external_email", "").strip().lower()
            register_number = request.form.get("register_number", "").strip()
            department_code = request.form.get("department_code", "").strip()
            department_name = request.form.get("department_name", "").strip()
            department_slug = request.form.get("department_slug", "").strip()
            rit_username = request.form.get("rit_username", "").strip().lower()

            if user_type == "internal":
                rit_username = rit_username or derive_rit_username(email)
                college_name = "Rajalakshmi Institute of Technology"
                external_email = ""
                if not all([name, email, phone_number, password, register_number, course, rit_username]):
                    flash("Complete every sign-up field, including verifying your RIT ID.", "error")
                else:
                    db = get_db()
                    existing_user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                    existing_phone = db.execute("SELECT id FROM users WHERE phone_number = ?", (phone_number,)).fetchone()
                    existing_reg = db.execute("SELECT id FROM users WHERE register_number = ?", (register_number,)).fetchone()
                    existing_rit_user = db.execute("SELECT id FROM users WHERE rit_username = ?", (rit_username,)).fetchone()
                    preauthorized_core = db.execute(
                        "SELECT id FROM users WHERE rit_username = ? AND role = 'core' ORDER BY id DESC LIMIT 1",
                        (rit_username,),
                    ).fetchone()
                    if preauthorized_core is not None:
                        preauth_user = db.execute("SELECT * FROM users WHERE id = ?", (preauthorized_core["id"],)).fetchone()
                        if int(preauth_user["is_active"] or 0) == 1:
                            flash("A core account already exists for this RIT username.", "error")
                        else:
                            db.execute(
                                """
                                UPDATE users
                                SET name = ?, email = ?, phone_number = ?, password_hash = ?,
                                    register_number = ?, course = ?, department_code = ?, department_name = ?, department_slug = ?,
                                    rit_username = ?, external_email = NULL, college_name = ?, user_type = 'internal', is_active = 1
                                WHERE id = ?
                                """,
                                (name, email, phone_number, generate_password_hash(password), register_number, course, department_code, department_name, department_slug, rit_username, college_name, preauth_user["id"]),
                            )
                            db.commit()
                            session.clear()
                            session["user_id"] = preauth_user["id"]
                            flash("Core member account activated successfully.", "success")
                            return redirect(url_for("home"))
                    elif existing_reg is not None:
                        flash("An account already exists for this register number.", "error")
                    elif existing_user is not None:
                        flash("An account already exists for that email.", "error")
                    elif existing_phone is not None:
                        flash("That phone number is already linked to another account.", "error")
                    elif existing_rit_user is not None:
                        flash("An account already exists for that RIT username.", "error")
                    else:
                        db.execute(
                            """
                            INSERT INTO users (
                                name, email, phone_number, password_hash, role,
                                register_number, course, department_code, department_name, department_slug,
                                rit_username, external_email, college_name, user_type, is_active
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (name, email, phone_number, generate_password_hash(password), "member",
                             register_number, course, department_code, department_name, department_slug,
                             rit_username, None, college_name, "internal", 1),
                        )
                        db.commit()
                        user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                        session.clear()
                        session["user_id"] = user["id"]
                        flash("Account created. Your workspace is ready.", "success")
                        return redirect(url_for("home"))
            else:
                login_email = external_email or email
                college_name = college_name or "External College"
                register_number = register_number or build_external_user_register_number(login_email, college_name)
                department_code = "OC"
                department_name = college_name
                department_slug = "other-college"
                email = login_email
                if not all([name, login_email, phone_number, password, course, college_name]):
                    flash("Complete every sign-up field for the external student account.", "error")
                else:
                    db = get_db()
                    existing_user = db.execute("SELECT id FROM users WHERE email = ?", (login_email,)).fetchone()
                    existing_external = db.execute("SELECT id FROM users WHERE external_email = ?", (login_email,)).fetchone()
                    existing_phone = db.execute("SELECT id FROM users WHERE phone_number = ?", (phone_number,)).fetchone()
                    existing_reg = db.execute("SELECT id FROM users WHERE register_number = ?", (register_number,)).fetchone()
                    if existing_user is not None or existing_external is not None:
                        flash("An account already exists for that external email.", "error")
                    elif existing_phone is not None:
                        flash("That phone number is already linked to another account.", "error")
                    elif existing_reg is not None:
                        flash("An account already exists for this external identity.", "error")
                    else:
                        db.execute(
                            """
                            INSERT INTO users (
                                name, email, phone_number, password_hash, role,
                                register_number, course, department_code, department_name, department_slug,
                                rit_username, external_email, college_name, user_type, is_active
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (name, login_email, phone_number, generate_password_hash(password), "member",
                             register_number, course, department_code, department_name, department_slug,
                             None, login_email, college_name, "external", 1),
                        )
                        db.commit()
                        user = db.execute("SELECT id FROM users WHERE email = ?", (login_email,)).fetchone()
                        session.clear()
                        session["user_id"] = user["id"]
                        flash("External student account created successfully.", "success")
                        return redirect(url_for("home"))

        return render_template("signup.html")

    @app.route("/home")
    def home():
        db = get_db()
        current_user = get_current_user()
        about = build_about_content()
        contact_links = build_contact_links()

        upcoming_event_rows = db.execute(
            """
            SELECT events.id, events.title, COALESCE(events.description, events.summary) AS description,
                   COALESCE(events."date", events.event_date) AS event_date, events.format,
                   COALESCE(events.location, events.venue) AS venue, events.category, events.seats,
                   events.status, events.summary, events.registration_link,
                   COALESCE(events.registration_deadline, COALESCE(events."date", events.event_date)) AS registration_deadline,
                   COALESCE(events.registration_closing_time, '23:59') AS registration_closing_time,
                   COALESCE(events.registration_enabled, 0) AS registration_enabled,
                   COALESCE(events.registration_limit, 0) AS registration_limit,
                   COUNT(event_registrations.id) AS registration_count
            FROM events
            LEFT JOIN event_registrations ON event_registrations.event_id = events.id
            GROUP BY events.id
            ORDER BY COALESCE(events."date", events.event_date) ASC
            LIMIT 3
            """
        ).fetchall()
        upcoming_events = [build_event_registration_meta(row) for row in upcoming_event_rows]
        upcoming_events = apply_capacity_scaling_signals(upcoming_events)
        spotlight_event = upcoming_events[0] if upcoming_events else None

        resources = db.execute(
            """
            SELECT id, title, track, difficulty, duration, summary, url, category
            FROM resources
            ORDER BY id DESC
            LIMIT 4
            """
        ).fetchall()
        featured_resource = resources[0] if resources else None


        metrics = {
            "active_member_count": db.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE role NOT IN ('admin', 'core')
                """
            ).fetchone()[0],
            "event_count": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "created_event_count": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "recent_created_event_count": db.execute(
                """
                SELECT COUNT(*)
                FROM events
                WHERE COALESCE(created_at, '') >= ?
                """,
                ((datetime.utcnow() - timedelta(days=1)).isoformat(sep=' ', timespec='seconds'),),
            ).fetchone()[0],
            "resource_count": db.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
            "upcoming_event_count": db.execute(
                """
                SELECT COUNT(*)
                FROM events
                WHERE COALESCE("date", event_date) >= ?
                """,
                (date.today().isoformat(),),
            ).fetchone()[0],
            "hosted_event_count": get_site_settings()["hosted_event_total"],
        }

        latest_team = db.execute(
            """
            SELECT team_name, lead_name, created_at
            FROM teams
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

        recent_activity = []
        if latest_team is not None:
            recent_activity.append(
                {
                    "label": "Team joined",
                    "title": latest_team["team_name"],
                    "detail": f"Led by {latest_team['lead_name']}, this team is now active in the challenge workspace.",
                    "meta": latest_team["created_at"],
                }
            )
        if featured_resource is not None:
            recent_activity.append(
                {
                    "label": "New resource",
                    "title": featured_resource["title"],
                    "detail": f"{featured_resource['category']} is the current learning path highlighted for members.",
                    "meta": featured_resource["duration"],
                }
            )

        hero_status = "System: Online"
        hero_title = "Live Dashboard: Operational"
        hero_summary = "The club workspace is live with events, resources, registration activity, and community updates in one interface."
        if spotlight_event is not None and spotlight_event["status"].lower() == "open":
            hero_status = "Live Feed"
            hero_title = "Submission Window: OPEN"
            hero_summary = f"{spotlight_event['title']} is the current upcoming event on the club timeline."

        next_milestone = None
        if spotlight_event is not None:
            event_date = date.fromisoformat(spotlight_event["event_date"])
            next_milestone = {
                "date": spotlight_event["event_date"],
                "days_remaining": (event_date - date.today()).days,
            }

        review_status = "Team registration and event participation are active through the club workspace."

        return render_template(
            "home.html",
            current_user=current_user,
            about=about,
            contact_links=contact_links,
            upcoming_events=upcoming_events,
            spotlight_event=spotlight_event,
            resources=resources,
            featured_resource=featured_resource,
            metrics=metrics,
            recent_activity=recent_activity,
            hero_status=hero_status,
            hero_title=hero_title,
            hero_summary=hero_summary,
            next_milestone=next_milestone,
            review_status=review_status,
        )

    @app.route("/events")
    def events():
        db = get_db()
        event_rows = db.execute(
            """
            SELECT events.id, events.title, COALESCE(events.description, events.summary) AS description,
                   COALESCE(events."date", events.event_date) AS event_date, events.format,
                   COALESCE(events.location, events.venue) AS venue, events.category, events.seats,
                   events.status, events.summary, events.registration_link,
                   COALESCE(events.registration_enabled, 0) AS registration_enabled,
                   COALESCE(events.registration_limit, 0) AS registration_limit,
                   COUNT(event_registrations.id) AS registration_count
            FROM events
            LEFT JOIN event_registrations ON event_registrations.event_id = events.id
            GROUP BY events.id
            ORDER BY COALESCE(events."date", events.event_date) ASC
            """
        ).fetchall()
        events_with_meta = [build_event_registration_meta(row) for row in event_rows]
        return render_template("events.html", events=events_with_meta)

    @app.route("/events/<int:event_id>/other-college-login", methods=("GET", "POST"))
    def other_college_event_login(event_id: int):
        db = get_db()
        event = db.execute(
            """
            SELECT id, title, COALESCE(events."date", events.event_date) AS event_date,
                   COALESCE(events.location, events.venue) AS venue,
                   COALESCE(events.interact_other_colleges, 0) AS interact_other_colleges
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            abort(404)
        if not bool(event["interact_other_colleges"]):
            return redirect(url_for("event_detail", event_id=event_id))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone_number = normalize_phone_number(request.form.get("phone_number", ""))
            college_name = request.form.get("college_name", "").strip()
            course = request.form.get("course", "").strip()
            if not all([name, email, phone_number, college_name, course]):
                flash("Complete every field to continue for this event.", "error")
            else:
                session[f"other_college_event_{event_id}"] = {
                    "name": name,
                    "email": email,
                    "phone_number": phone_number,
                    "college_name": college_name,
                    "course": course,
                    "register_number": build_other_college_register_number(event_id, email, phone_number),
                }
                flash("Event access granted. Continue with registration.", "success")
                return redirect(url_for("event_detail", event_id=event_id))
        return render_template("other_college_login.html", event=event)

    @app.route("/events/<int:event_id>", methods=("GET", "POST"))
    def event_detail(event_id: int):
        db = get_db()
        event_row = db.execute(
            """
            SELECT events.id, events.title, COALESCE(events.description, events.summary) AS description,
                   COALESCE(events."date", events.event_date) AS event_date, events.format,
                   COALESCE(events.location, events.venue) AS venue, events.category, events.seats,
                   events.status, events.summary, events.registration_link,
                   COALESCE(events.registration_enabled, 0) AS registration_enabled,
                   COALESCE(events.registration_limit, 0) AS registration_limit,
                   COALESCE(events.contact_email_required, 0) AS contact_email_required,
                   COALESCE(events.interact_other_colleges, 0) AS interact_other_colleges,
                   COUNT(event_registrations.id) AS registration_count
            FROM events
            LEFT JOIN event_registrations ON event_registrations.event_id = events.id
            WHERE events.id = ?
            GROUP BY events.id
            """,
            (event_id,),
        ).fetchone()
        if event_row is None:
            abort(404)
        event = build_event_registration_meta(event_row)
        event = apply_capacity_scaling_signals([event])[0]

        registration_fields = db.execute(
            """
            SELECT id, field_label, field_type, field_options, is_required, field_order
            FROM event_registration_fields
            WHERE event_id = ?
            ORDER BY field_order ASC, id ASC
            """,
            (event_id,),
        ).fetchall()
        registration_fields = [hydrate_registration_field(field) for field in registration_fields]
        contact_email_required = bool(event.get("contact_email_required"))

        registration_is_full = event["registration_limit"] > 0 and event["registration_count"] >= event["registration_limit"]
        registration_closed = not bool(event["registration_enabled"])

        current_user = get_current_user()
        other_college_access = get_other_college_event_access(event_id) if bool(event.get('interact_other_colleges')) else None
        existing_registration = None
        if current_user is not None and current_user["register_number"]:
            existing_registration = db.execute(
                """
                SELECT id, od_status, is_selected, hod_authorized_at, created_at
                FROM registrations
                WHERE event_id = ? AND register_number = ?
                """,
                (event_id, current_user["register_number"]),
            ).fetchone()
        elif current_user is not None and is_external_user_type(current_user) and current_user['register_number']:
            existing_registration = db.execute(
                """
                SELECT id, od_status, is_selected, hod_authorized_at, created_at
                FROM registrations
                WHERE event_id = ? AND register_number = ?
                """,
                (event_id, current_user['register_number']),
            ).fetchone()
        elif other_college_access and other_college_access.get('register_number'):
            existing_registration = db.execute(
                """
                SELECT id, od_status, is_selected, hod_authorized_at, created_at
                FROM registrations
                WHERE event_id = ? AND register_number = ?
                """,
                (event_id, other_college_access['register_number']),
            ).fetchone()

        if request.method == "POST":
            if registration_closed:
                flash("registration is closed", "error")
            elif registration_is_full:
                flash("registration for the event is completed", "error")
            elif not registration_fields and not contact_email_required:
                flash("No registration fields are configured for this event.", "error")
            elif existing_registration is not None:
                flash("You have already been registered for this event. Sit back and chill.", "success")
                return redirect(url_for("event_detail", event_id=event_id))
            else:
                registration_od_status = OD_STATUS_REGISTERED
                if current_user is not None and current_user["register_number"]:
                    if is_external_user_type(current_user):
                        verified_identity = {
                            "student_name": (current_user["name"] or "").strip(),
                            "register_number": (current_user["register_number"] or "").strip(),
                            "course": (current_user["course"] or "").strip(),
                            "department_code": "OC",
                            "department_name": (current_user["college_name"] or current_user["department_name"] or "Other College").strip(),
                            "department_slug": "other-college",
                            "hod_email": "",
                            "verification_url": "external-account",
                        }
                        registration_od_status = OD_STATUS_OC_VERIFIED
                    else:
                        verified_identity = {
                            "student_name": (current_user["name"] or "").strip(),
                            "register_number": (current_user["register_number"] or "").strip(),
                            "course": (current_user["course"] or "").strip(),
                            "department_code": (current_user["department_code"] or "").strip(),
                            "department_name": (current_user["department_name"] or "").strip(),
                            "department_slug": (current_user["department_slug"] or "").strip(),
                            "hod_email": "",
                            "verification_url": "account-identity",
                        }
                elif bool(event.get('interact_other_colleges')) and other_college_access is not None:
                    registration_od_status = OD_STATUS_OC_VERIFIED
                    verified_identity = {
                        "student_name": other_college_access.get('name', ''),
                        "register_number": other_college_access.get('register_number', ''),
                        "course": other_college_access.get('course', ''),
                        "department_code": "OC",
                        "department_name": other_college_access.get('college_name', ''),
                        "department_slug": "other-college",
                        "hod_email": "",
                        "verification_url": "other-college-access",
                    }
                else:
                    verified_identity = {
                        "student_name": request.form.get("student_name", "").strip(),
                        "register_number": request.form.get("register_number", "").strip(),
                        "course": request.form.get("course", "").strip(),
                        "department_code": request.form.get("department_code", "").strip(),
                        "department_name": request.form.get("department_name", "").strip(),
                        "department_slug": request.form.get("department_slug", "").strip(),
                        "hod_email": request.form.get("hod_email", "").strip(),
                        "verification_url": request.form.get("verification_url", "").strip(),
                    }
                    if not all([
                        verified_identity["student_name"],
                        verified_identity["register_number"],
                        verified_identity["course"],
                        verified_identity["department_code"],
                        verified_identity["department_name"],
                        verified_identity["department_slug"],
                        verified_identity["verification_url"],
                    ]):
                        flash("Verify the official RIT ID first. Login is not required, but ID verification is mandatory.", "error")
                        verified_identity = None
                    elif not is_valid_rit_verification_url(verified_identity["verification_url"]):
                        flash("Use a valid official RIT verification link from the ID QR.", "error")
                        verified_identity = None

                answers = {}
                has_error = verified_identity is None

                if contact_email_required:
                    contact_email = request.form.get("contact_email", "").strip()
                    if not contact_email:
                        flash("Enter the participant email address.", "error")
                        has_error = True
                    elif "@" not in contact_email or "." not in contact_email.split("@")[-1]:
                        flash("Enter a valid participant email address.", "error")
                        has_error = True
                    else:
                        answers["contact_email"] = contact_email

                if not has_error:
                    for field in registration_fields:
                        field_key = f"field_{field['id']}"
                        value = request.form.get(field_key, "").strip()
                        answers[str(field["id"])] = value
                        if field["is_required"] and not value:
                            flash(f"Complete the {field['field_label']} field.", "error")
                            has_error = True
                            break
                        if field["field_type"] == "select" and value and value not in field["option_values"]:
                            flash(f"Choose a valid option for {field['field_label']}.", "error")
                            has_error = True
                            break

                if not has_error:
                    existing_identity_registration = db.execute(
                        """
                        SELECT id
                        FROM registrations
                        WHERE event_id = ? AND register_number = ?
                        """,
                        (event_id, verified_identity["register_number"]),
                    ).fetchone()
                    if existing_identity_registration is not None:
                        flash("This register number is already registered for the event. Sit back and chill.", "success")
                        return redirect(url_for("event_detail", event_id=event_id))

                    hod_row = db.execute(
                        "SELECT hod_email FROM hod_contacts WHERE department_code = ?",
                        (verified_identity["department_code"],),
                    ).fetchone()
                    hod_email = verified_identity["hod_email"] or (hod_row["hod_email"] if hod_row else "")

                    answers["student_name"] = verified_identity["student_name"]
                    answers["register_number"] = verified_identity["register_number"]
                    answers["course"] = verified_identity["course"]
                    answers["department_code"] = verified_identity["department_code"]
                    answers["department_name"] = verified_identity["department_name"]
                    answers["department_slug"] = verified_identity["department_slug"]
                    answers["hod_email"] = hod_email

                    db.execute(
                        """
                        INSERT INTO event_registrations (event_id, response_json)
                        VALUES (?, ?)
                        """,
                        (event_id, json.dumps(answers)),
                    )
                    db.execute(
                        """
                        INSERT INTO registrations (
                            event_id, student_name, register_number, course, department_code,
                            department_name, department_slug, hod_email, verification_url,
                            contact_email, response_json, od_status
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            verified_identity["student_name"],
                            verified_identity["register_number"],
                            verified_identity["course"],
                            verified_identity["department_code"],
                            verified_identity["department_name"],
                            verified_identity["department_slug"],
                            hod_email,
                            verified_identity["verification_url"],
                            answers.get("contact_email", ""),
                            json.dumps(answers),
                            registration_od_status,
                        ),
                    )
                    db.commit()
                    apply_event_automation_rules()
                    success_message = "Event registration submitted. Await admin selection and HOD approval."
                    if registration_od_status == OD_STATUS_OC_VERIFIED:
                        success_message = "Event registration submitted. External-college identity is verified and gatekeeper-ready."
                    flash(success_message, "success")
                    return redirect(url_for("event_detail", event_id=event_id))

        registration_autofill_profile = build_other_college_autofill_profile(other_college_access) if other_college_access and current_user is None else None
        registration_form_defaults = build_registration_form_defaults(
            current_user,
            registration_fields,
            contact_email_required=contact_email_required,
            autofill_profile=registration_autofill_profile,
        )

        return render_template(
            "event_detail.html",
            event=event,
            registration_fields=registration_fields,
            contact_email_required=contact_email_required,
            registration_closed=registration_closed,
            registration_is_full=registration_is_full,
            registration_form_defaults=registration_form_defaults,
            existing_registration=existing_registration,
            other_college_access=other_college_access,
        )

    @app.route("/resources")
    def resources():
        db = get_db()
        resource_rows = db.execute(
            """
            SELECT id, title, track, difficulty, duration, summary, url, category
            FROM resources
            ORDER BY category ASC, title ASC
            """
        ).fetchall()
        return render_template("resources.html", resources=resource_rows)

    @app.route("/resources/<int:resource_id>")
    def resource_detail(resource_id: int):
        db = get_db()
        resource = db.execute(
            """
            SELECT id, title, track, difficulty, duration, summary, url, category
            FROM resources
            WHERE id = ?
            """,
            (resource_id,),
        ).fetchone()
        if resource is None:
            abort(404)
        return render_template("resource_detail.html", resource=resource)

    @app.route("/leaderboard")
    def leaderboard():
        return render_template("leaderboard.html")

    @app.route("/projects")
    def projects():
        flash("Project submission is not part of this website workflow.", "error")
        return redirect(url_for("home"))

    @app.route("/faq")
    def faq():
        return render_template("faq.html", faqs=build_static_faqs())

    @app.route("/contact")
    def contact():
        return render_template("contact.html", contact_links=build_contact_links())

    @app.route("/about")
    def about():
        db = get_db()
        team_rows = db.execute(
            """
            SELECT name, role, email
            FROM users
            WHERE role IN ('core', 'admin')
            ORDER BY created_at ASC
            """
        ).fetchall()
        team_members = [
            {
                "name": row["name"],
                "role": "Admin" if row["role"] == "admin" else "Core",
                "email": row["email"],
                "initials": build_initials(row["name"]),
            }
            for row in team_rows
        ]
        return render_template("about.html", about=build_about_content(), team_members=team_members)

    @app.route("/website-guide")
    def website_guide():
        return render_template("website_guide.html")

    @app.route("/forgot-password", methods=("GET", "POST"))
    def forgot_password():
        if get_current_user() is not None:
            return redirect(url_for("profile"))

        if request.method == "POST":
            phone_number = normalize_phone_number(request.form.get("phone_number", ""))
            if not phone_number:
                flash("Enter the registered phone number.", "error")
            else:
                db = get_db()
                user = db.execute(
                    "SELECT id, phone_number FROM users WHERE phone_number = ?",
                    (phone_number,),
                ).fetchone()
                if user is None:
                    flash("No account exists for that phone number.", "error")
                else:
                    otp = create_password_reset_otp(user["id"])
                    try:
                        send_password_reset_otp(user["phone_number"], otp)
                    except RuntimeError as exc:
                        flash(str(exc), "error")
                    except Exception:
                        flash("OTP SMS could not be sent. Check the Twilio configuration and try again.", "error")
                    else:
                        session["password_reset_phone_number"] = user["phone_number"]
                        session.pop("password_reset_verified_phone_number", None)
                        flash("OTP sent to your registered phone number.", "success")
                        return redirect(url_for("verify_reset_otp"))

        return render_template("forgot_password.html")

    @app.route("/verify-reset-otp", methods=("GET", "POST"))
    def verify_reset_otp():
        phone_number = session.get("password_reset_phone_number")
        if not phone_number:
            flash("Start with the forgot password page first.", "error")
            return redirect(url_for("forgot_password"))

        if request.method == "POST":
            otp = request.form.get("otp", "").strip()
            if not otp:
                flash("Enter the OTP sent to your phone.", "error")
            else:
                db = get_db()
                user = db.execute(
                    "SELECT id, phone_number FROM users WHERE phone_number = ?",
                    (phone_number,),
                ).fetchone()
                if user is None:
                    flash("This account is no longer available.", "error")
                    return redirect(url_for("forgot_password"))
                if verify_password_reset_otp(user["id"], otp):
                    session["password_reset_verified_phone_number"] = phone_number
                    flash("OTP verified. You can now reset the password.", "success")
                    return redirect(url_for("reset_password"))
                flash("Invalid or expired OTP.", "error")

        return render_template("verify_reset_otp.html", reset_phone_number=phone_number)

    @app.route("/reset-password", methods=("GET", "POST"))
    def reset_password():
        phone_number = session.get("password_reset_verified_phone_number")
        if not phone_number:
            flash("Verify the OTP before resetting the password.", "error")
            return redirect(url_for("forgot_password"))

        if request.method == "POST":
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not password or not confirm_password:
                flash("Complete both password fields.", "error")
            elif len(password) < 8:
                flash("Use at least 8 characters for the new password.", "error")
            elif password != confirm_password:
                flash("The passwords do not match.", "error")
            else:
                db = get_db()
                user = db.execute(
                    "SELECT id FROM users WHERE phone_number = ?",
                    (phone_number,),
                ).fetchone()
                if user is None:
                    flash("This account is no longer available.", "error")
                    return redirect(url_for("forgot_password"))
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(password), user["id"]),
                )
                db.execute("DELETE FROM password_reset_otps WHERE user_id = ?", (user["id"],))
                db.commit()
                session.pop("password_reset_phone_number", None)
                session.pop("password_reset_verified_phone_number", None)
                flash("Password reset successfully. You can now sign in.", "success")
                return redirect(url_for("login"))

        return render_template("reset_password.html", reset_phone_number=phone_number)

    @app.route("/brief")
    def brief():
        return render_template("brief.html")

    def handle_login(*, admin_only: bool):
        if get_current_user() is not None:
            current_user = get_current_user()
            if is_admin(current_user):
                return redirect(url_for("admin"))
            return redirect(url_for("profile"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            db = get_db()
            user = db.execute(
                "SELECT id, name, email, password_hash, role, is_active FROM users WHERE email = ?",
                (email,),
            ).fetchone()

            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Incorrect email or password.", "error")
            elif not bool(user["is_active"]):
                flash("This account is not active yet.", "error")
            else:
                if admin_only and not is_admin(user):
                    flash("This login page is reserved for core members and admins.", "error")
                    return redirect(url_for("login"))
                if not admin_only and is_admin(user):
                    return render_template("admin_access_notice.html")
                session.clear()
                session["user_id"] = user["id"]
                flash(f"Welcome back, {user['name']}.", "success")
                if is_admin(user):
                    return redirect(url_for("admin"))
                return redirect(url_for("profile"))

        return render_template("login.html", admin_only=admin_only)

    @app.route("/login", methods=("GET", "POST"))
    def login():
        return handle_login(admin_only=False)

    @app.route("/admin/login", methods=("GET", "POST"))
    def admin_login():
        return handle_login(admin_only=True)

    @app.route("/logout")
    def logout():
        session.clear()
        flash("You have been logged out.", "success")
        return redirect(url_for("home"))

    @app.route("/profile")
    def profile():
        user = get_current_user()
        db = get_db()
        team_count = db.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        open_events = db.execute("SELECT COUNT(*) FROM events WHERE status = 'Open'").fetchone()[0]
        resource_count = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        return render_template(
            "profile.html",
            user=user,
            account_stats={
                "teams": team_count,
                "open_events": open_events,
                "resources": resource_count,
            },
        )

    @app.route("/register", methods=("GET", "POST"))
    def register():
        settings = get_site_settings()
        if not settings["registration_open"]:
            flash("Team registration is currently closed by the admin team.", "error")
            return redirect(url_for("home"))

        if request.method == "POST":
            team_name = request.form.get("team_name", "").strip()
            lead_name = request.form.get("lead_name", "").strip()
            email = request.form.get("email", "").strip()
            members = request.form.get("members", "").strip()
            stack = request.form.get("stack", "").strip()
            vision = request.form.get("vision", "").strip()

            if not all([team_name, lead_name, email, members, stack, vision]):
                flash("Fill every field before submitting the registration form.", "error")
            else:
                db = get_db()
                db.execute(
                    """
                    INSERT INTO teams (team_name, lead_name, email, members, stack, vision)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (team_name, lead_name, email, members, stack, vision),
                )
                db.commit()
                flash("Team registered successfully.", "success")
                return redirect(url_for("profile"))

        team_form_defaults = {
            "lead_name": "",
            "email": "",
            "members": "",
        }
        current_user = get_current_user()
        if current_user is not None:
            team_form_defaults.update(
                {
                    "lead_name": (current_user["name"] or "").strip(),
                    "email": (current_user["email"] or "").strip(),
                    "members": (current_user["name"] or "").strip(),
                }
            )

        return render_template("register.html", team_form_defaults=team_form_defaults)

    @app.route("/submit", methods=("GET", "POST"))
    def submit_prototype():
        flash("Project submission is not enabled for this website.", "error")
        return redirect(url_for("home"))

    def measure_registration_sheet_text(value: object) -> int:
        if value is None:
            return 0
        return len(str(value).strip())

    def build_registration_sheet_columns(selected_fields, participants, include_attendance: bool = False):
        columns = [
            {"label": "S.No", "key": "serial", "min_chars": 5, "max_chars": 6},
            {"label": "Submitted At", "key": "created_at", "min_chars": 16, "max_chars": 20},
        ]

        for field in selected_fields:
            columns.append(
                {
                    "label": field["field_label"],
                    "key": str(field["id"]),
                    "min_chars": 10,
                    "max_chars": 28,
                }
            )

        if include_attendance:
            columns.extend(
                [
                    {"label": "Attendance", "key": "attendance", "min_chars": 10, "max_chars": 12},
                    {"label": "Signature", "key": "signature", "min_chars": 12, "max_chars": 14},
                ]
            )

        for column in columns:
            max_length = measure_registration_sheet_text(column["label"])
            if column["key"] == "serial":
                values = [participant["serial"] for participant in participants]
            elif column["key"] == "created_at":
                values = [participant["created_at"] for participant in participants]
            elif column["key"] in {"attendance", "signature"}:
                values = []
            else:
                values = [participant["answers"].get(column["key"], "") for participant in participants]

            for value in values:
                max_length = max(max_length, measure_registration_sheet_text(value))

            width_chars = max(column["min_chars"], min(max_length + 2, column["max_chars"]))
            column["width_chars"] = width_chars
            column["css_width"] = f"{width_chars}ch"

        return columns

    def build_event_registration_list_context(event_id: int) -> dict[str, object]:
        db = get_db()
        event_row = db.execute(
            """
            SELECT events.id, events.title, COALESCE(events.description, events.summary) AS description,
                   COALESCE(events."date", events.event_date) AS event_date,
                   COALESCE(events.registration_deadline, COALESCE(events."date", events.event_date)) AS registration_deadline,
                   COALESCE(events.registration_closing_time, '23:59') AS registration_closing_time,
                   COALESCE(events.registration_enabled, 0) AS registration_enabled,
                   COALESCE(events.registration_limit, 0) AS registration_limit,
                   COALESCE(events.contact_email_required, 0) AS contact_email_required,
                   COUNT(event_registrations.id) AS registration_count
            FROM events
            LEFT JOIN event_registrations ON event_registrations.event_id = events.id
            WHERE events.id = ?
            GROUP BY events.id
            """,
            (event_id,),
        ).fetchone()
        if event_row is None:
            abort(404)
        event = build_event_registration_meta(event_row)

        available_fields = db.execute(
            """
            SELECT id, field_label, field_type, field_options, field_order
            FROM event_registration_fields
            WHERE event_id = ?
            ORDER BY field_order ASC, id ASC
            """,
            (event_id,),
        ).fetchall()
        available_fields = [hydrate_registration_field(field) for field in available_fields]
        if bool(event.get("contact_email_required")):
            available_fields.insert(
                0,
                {
                    "id": "contact_email",
                    "field_label": "Participant Email",
                    "field_type": "email",
                    "field_options": "",
                    "option_values": [],
                    "is_required": 1,
                    "field_order": 0,
                },
            )
        selected_field_ids = request.args.getlist("field_id")
        if not selected_field_ids:
            selected_field_ids = [str(field["id"]) for field in available_fields]
        selected_fields = [field for field in available_fields if str(field["id"]) in selected_field_ids]
        if available_fields and not selected_fields:
            selected_field_ids = [str(field["id"]) for field in available_fields]
            selected_fields = available_fields

        registration_rows = db.execute(
            """
            SELECT id, response_json, created_at
            FROM event_registrations
            WHERE event_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (event_id,),
        ).fetchall()

        participants = []
        for index, row in enumerate(registration_rows, start=1):
            response_data = json.loads(row["response_json"])
            participants.append(
                {
                    "serial": index,
                    "created_at": row["created_at"],
                    "answers": {str(field["id"]): response_data.get(str(field["id"]), "") for field in selected_fields},
                }
            )

        sheet_columns = build_registration_sheet_columns(selected_fields, participants)

        return {
            "event": event,
            "available_fields": available_fields,
            "selected_fields": selected_fields,
            "selected_field_ids": selected_field_ids,
            "participants": participants,
            "sheet_columns": sheet_columns,
            "sheet_min_width": f"{sum(column['width_chars'] for column in sheet_columns) + (len(sheet_columns) * 4)}ch",
        }

    @app.route("/admin/events/<int:event_id>/registrations")
    @admin_required
    def event_registration_list(event_id: int):
        context = build_event_registration_list_context(event_id)

        if request.args.get("export") == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            selected_fields = context["selected_fields"]
            participants = context["participants"]
            header = ["S.No", "Submitted At"] + [field["field_label"] for field in selected_fields]
            writer.writerow(header)
            for participant in participants:
                writer.writerow(
                    [participant["serial"], participant["created_at"]]
                    + [participant["answers"].get(str(field["id"]), "") for field in selected_fields]
                )
            filename = f"event_{event_id}_participant_list.csv"
            return Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        return render_template("event_registration_list.html", **context)

    @app.route("/admin/event/<int:event_id>/export/pdf")
    @admin_required
    def export_event_pdf(event_id: int):
        context = build_event_registration_list_context(event_id)
        event = context["event"]
        selected_fields = context["selected_fields"]
        participants = context["participants"]

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=landscape(A4),
            leftMargin=24,
            rightMargin=24,
            topMargin=28,
            bottomMargin=24,
        )
        styles = getSampleStyleSheet()
        generated_on = datetime.now().strftime("%d %B %Y")
        event_date_label = event["event_date"]
        table_cell_style = ParagraphStyle(
            "RegistrationSheetCell",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=10,
            wordWrap="CJK",
            spaceAfter=0,
            spaceBefore=0,
        )
        table_header_style = ParagraphStyle(
            "RegistrationSheetHeader",
            parent=table_cell_style,
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=10,
        )
        elements = [
            Paragraph("GeeksforGeeks Campus Club - RIT", styles["Heading1"]),
            Spacer(1, 4),
            Paragraph(f"Event: {event['title']}", styles["Heading2"]),
            Paragraph(f"Date: {event_date_label}", styles["Normal"]),
            Paragraph(
                f"Registered: {event['registration_count']} / {event['registration_limit'] if event['registration_limit'] else 'Unlimited'}",
                styles["Normal"],
            ),
            Paragraph(
                f"Remaining slots: {event['remaining_slots'] if event['remaining_slots'] is not None else 'Unlimited'}",
                styles["Normal"],
            ),
            Spacer(1, 20),
        ]

        pdf_columns = build_registration_sheet_columns(selected_fields, participants, include_attendance=True)
        header = [Paragraph(str(column["label"]), table_header_style) for column in pdf_columns]
        data = [header]
        for participant in participants:
            row = [
                Paragraph(str(participant["serial"]), table_cell_style),
                Paragraph(str(participant["created_at"]), table_cell_style),
            ]
            for field in selected_fields:
                row.append(Paragraph(str(participant["answers"].get(str(field["id"]), "") or ""), table_cell_style))
            row += [Paragraph("", table_cell_style), Paragraph("", table_cell_style)]
            data.append(row)

        if len(data) == 1:
            empty_row = [Paragraph("", table_cell_style), Paragraph("", table_cell_style)]
            empty_row += [Paragraph("No registrations yet", table_cell_style)]
            empty_row += [Paragraph("", table_cell_style)] * max(len(pdf_columns) - 3, 0)
            data.append(empty_row)

        available_width = landscape(A4)[0] - doc.leftMargin - doc.rightMargin
        total_weight = sum(column["width_chars"] for column in pdf_columns) or 1
        col_widths = [available_width * (column["width_chars"] / total_weight) for column in pdf_columns]

        table = Table(data, repeatRows=1, colWidths=col_widths, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9e6dc")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("LEADING", (0, 0), (-1, -1), 10),
                    ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7f6")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ]
            )
        )
        elements.append(table)

        def draw_footer(canvas, document):
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.drawString(document.leftMargin, 14, "Generated by RIT's -GfG Campus Club Portal")
            canvas.drawRightString(
                document.pagesize[0] - document.rightMargin,
                14,
                f"Generated on: {generated_on} | Page {canvas.getPageNumber()}",
            )
            canvas.restoreState()

        doc.build(elements, onFirstPage=draw_footer, onLaterPages=draw_footer)
        buffer.seek(0)

        safe_title = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in event["title"]).strip("_") or f"event_{event_id}"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"{safe_title}_participants.pdf",
            mimetype="application/pdf",
        )

    @app.route("/approve/<token>", methods=("GET", "POST"))
    def approve_students(token: str):
        db = get_db()
        registrations = db.execute(
            """
            SELECT registrations.*, events.title AS event_title, events.event_date,
                   COALESCE(events.event_start_time, '09:00') AS event_start_time,
                   COALESCE(events.event_end_time, events.registration_closing_time, '23:59') AS event_end_time
            FROM registrations
            JOIN events ON events.id = registrations.event_id
            WHERE registrations.hod_token = ?
            ORDER BY registrations.student_name ASC
            """,
            (token,),
        ).fetchall()
        if not registrations:
            abort(404)

        first_registration = registrations[0]
        already_authorized = all(row["od_status"] == OD_STATUS_HOD_AUTHORIZED for row in registrations)
        approval_deadline_at = build_hod_approval_deadline_datetime(
            first_registration["event_date"],
            first_registration["event_end_time"],
        )
        approval_window_closed = approval_deadline_at is not None and datetime.now() > approval_deadline_at
        if request.method == "POST" and not already_authorized:
            if approval_window_closed:
                flash("The HOD approval window is closed for this event.", "error")
                return redirect(url_for("approve_students", token=token))
            registration_ids = [row["id"] for row in registrations]
            placeholders = ", ".join("?" for _ in registration_ids)
            db.execute(
                f"""
                UPDATE registrations
                SET od_status = ?, hod_authorized_at = ?, is_selected = 1
                WHERE id IN ({placeholders})
                """,
                (OD_STATUS_HOD_AUTHORIZED, datetime.utcnow().isoformat(timespec="seconds"), *registration_ids),
            )
            db.commit()
            flash("Students approved successfully.", "success")
            return redirect(url_for("approve_students", token=token))

        return render_template(
            "hod_approval.html",
            registrations=registrations,
            event_title=first_registration["event_title"],
            event_date=first_registration["event_date"],
            event_start_time=first_registration["event_start_time"],
            event_end_time=first_registration["event_end_time"],
            department_name=first_registration["department_name"],
            approval_complete=already_authorized,
            approval_window_closed=approval_window_closed,
            approval_deadline_at=approval_deadline_at.isoformat(timespec="seconds") if approval_deadline_at is not None else "",
        )

    @app.route("/admin/gatekeeper/<int:event_id>")
    @admin_required
    def gatekeeper_checkin(event_id: int):
        event = get_db().execute(
            'SELECT id, title, COALESCE(location, venue) AS venue, COALESCE("date", event_date) AS event_date FROM events WHERE id = ?',
            (event_id,),
        ).fetchone()
        if event is None:
            abort(404)
        return render_template("gatekeeper_checkin.html", event=event)

    @app.route("/api/gatekeeper/<int:event_id>/check", methods=("POST",))
    @admin_required
    def api_gatekeeper_check(event_id: int):
        payload = request.get_json(silent=True) or {}
        verification_url = str(payload.get("verification_url", "")).strip()
        manual_register_number = str(payload.get("register_number", "")).strip()
        manual_student_name = str(payload.get("student_name", "")).strip()
        confirm_entry = bool(payload.get("confirm_entry"))
        if verification_url:
            try:
                identity = build_identity_payload(verification_url)
            except REQUESTS_EXCEPTION as exc:
                return jsonify({"ok": False, "status": "network_error", "message": f"RIT verification page could not be reached: {exc}"}), 502
            except RuntimeError as exc:
                status = "network_error" if str(exc).startswith("RIT verification page could not be reached") else "dependency_error"
                status_code = 502 if status == "network_error" else 500
                return jsonify({"ok": False, "status": status, "message": str(exc)}), status_code
            except ValueError as exc:
                return jsonify({"ok": False, "status": "invalid_identity", "message": str(exc)}), 400
        elif manual_register_number:
            identity = {
                "student_name": manual_student_name,
                "register_number": manual_register_number,
                "course": "",
                "verification_url": "manual-gatekeeper-lookup",
            }
        else:
            return jsonify({"ok": False, "status": "missing_identity", "message": "Provide a verification URL or a register number."}), 400

        db = get_db()
        registration = db.execute(
            """
            SELECT registrations.*, events.title AS event_title
            FROM registrations
            JOIN events ON events.id = registrations.event_id
            WHERE registrations.event_id = ? AND registrations.register_number = ?
            ORDER BY registrations.id DESC
            LIMIT 1
            """,
            (event_id, identity["register_number"]),
        ).fetchone()

        if registration is None:
            return jsonify(
                {
                    "ok": True,
                    "allowed": False,
                    "screen": "red",
                    "message": "No approved registration was found for this register number.",
                    "student_name": identity["student_name"],
                    "register_number": identity["register_number"],
                    "course": identity["course"],
                }
            )

        if registration["od_status"] not in {OD_STATUS_HOD_AUTHORIZED, OD_STATUS_OC_VERIFIED}:
            return jsonify(
                {
                    "ok": True,
                    "allowed": False,
                    "screen": "red",
                    "message": f"OD status is {registration['od_status']}. Entry is allowed only after HOD authorization or external verification.",
                    "student_name": registration["student_name"],
                    "register_number": registration["register_number"],
                    "course": registration["course"],
                }
            )

        if not confirm_entry:
            preview_message = "Identity matched. Confirm the student before allowing entry."
            if registration["od_status"] == OD_STATUS_OC_VERIFIED:
                preview_message = "External-college identity matched. Confirm the student before allowing entry."
            return jsonify(
                {
                    "ok": True,
                    "allowed": False,
                    "eligible": True,
                    "pending_confirmation": True,
                    "screen": "amber",
                    "message": preview_message,
                    "student_name": registration["student_name"],
                    "register_number": registration["register_number"],
                    "course": registration["course"],
                    "department_name": registration["department_name"],
                }
            )

        attendance = db.execute(
            """
            SELECT id
            FROM attendance
            WHERE event_id = ? AND registration_id = ?
            """,
            (event_id, registration["id"]),
        ).fetchone()
        checked_in_at = datetime.utcnow().isoformat(timespec="seconds")
        persisted_verification = verification_url or identity.get("verification_url", "") or "manual-gatekeeper-lookup"
        if attendance is None:
            db.execute(
                """
                INSERT INTO attendance (event_id, registration_id, verification_url, check_in_status, checked_in_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, registration["id"], persisted_verification, "Allowed", checked_in_at),
            )
        else:
            db.execute(
                """
                UPDATE attendance
                SET verification_url = ?, check_in_status = ?, checked_in_at = ?
                WHERE id = ?
                """,
                (persisted_verification, "Allowed", checked_in_at, attendance["id"]),
            )
        db.commit()

        success_message = "HOD authorization verified. Entry approved."
        if registration["od_status"] == OD_STATUS_OC_VERIFIED:
            success_message = "External-college verification confirmed. Entry approved."

        return jsonify(
            {
                "ok": True,
                "allowed": True,
                "eligible": True,
                "pending_confirmation": False,
                "screen": "green",
                "message": success_message,
                "student_name": registration["student_name"],
                "register_number": registration["register_number"],
                "course": registration["course"],
                "department_name": registration["department_name"],
                "checked_in_at": checked_in_at,
            }
        )

    @app.route("/admin", methods=("GET", "POST"))
    @admin_required
    def admin():
        user = get_current_user()
        db = get_db()

        if request.method == "POST":
            action = request.form.get("action", "").strip()

            if action == "add_event":
                title = request.form.get("title", "").strip()
                description = request.form.get("description", "").strip()
                event_date = request.form.get("event_date", "").strip()
                registration_deadline = request.form.get("registration_deadline", "").strip() or event_date
                registration_closing_time = request.form.get("registration_closing_time", "").strip() or "23:59"
                event_start_time = request.form.get("event_start_time", "").strip() or "09:00"
                event_end_time = request.form.get("event_end_time", "").strip() or registration_closing_time
                location = request.form.get("location", "").strip()
                category = request.form.get("category", "").strip() or "Club Event"
                registration_link = request.form.get("registration_link", "").strip()
                registration_limit_raw = request.form.get("registration_limit", "").strip() or "0"
                registration_enabled = 1 if request.form.get("registration_enabled") == "on" else 0
                contact_email_required = 1 if request.form.get("contact_email_required") == "on" else 0
                interact_other_colleges = 1 if request.form.get("interact_other_colleges") == "on" else 0
                raw_field_lines = request.form.get("registration_fields", "")
                field_definitions, field_error = parse_registration_field_definitions(raw_field_lines)

                if not all([title, description, event_date, location]):
                    flash("Provide title, description, date, and location for the event.", "error")
                elif parse_iso_date(event_date) is None:
                    flash("Event date must be a valid date.", "error")
                elif parse_iso_date(registration_deadline) is None:
                    flash("Registration deadline must be a valid date.", "error")
                elif parse_iso_time(registration_closing_time) is None:
                    flash("Registration closing time must be a valid time.", "error")
                elif parse_iso_time(event_start_time) is None or parse_iso_time(event_end_time) is None:
                    flash("Event start time and end time must be valid times.", "error")
                elif parse_iso_time(event_start_time) >= parse_iso_time(event_end_time):
                    flash("Event end time must be later than the start time.", "error")
                elif parse_iso_date(registration_deadline) > parse_iso_date(event_date):
                    flash("Registration deadline cannot be after the event date.", "error")
                elif field_error is not None:
                    flash(field_error, "error")
                else:
                    try:
                        registration_limit = int(registration_limit_raw)
                    except ValueError:
                        registration_limit = -1

                    if registration_limit < 0:
                        flash("Registration limit must be a valid integer.", "error")
                    else:
                        cursor = db.execute(
                            """
                            INSERT INTO events (
                                title, event_date, format, venue, category, seats, status, summary,
                                description, "date", location, registration_link, registration_enabled, registration_limit, contact_email_required, interact_other_colleges, registration_deadline, registration_closing_time, event_start_time, event_end_time
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                title,
                                event_date,
                                "Offline",
                                location,
                                category,
                                100,
                                "Open",
                                description,
                                description,
                                event_date,
                                location,
                                registration_link,
                                registration_enabled,
                                registration_limit,
                                contact_email_required,
                                interact_other_colleges,
                                registration_deadline,
                                registration_closing_time,
                                event_start_time,
                                event_end_time,
                            ),
                        )
                        event_id = cursor.lastrowid
                        for field in field_definitions:
                            db.execute(
                                """
                                INSERT INTO event_registration_fields (event_id, field_label, field_type, field_options, is_required, field_order)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    event_id,
                                    field["field_label"],
                                    field["field_type"],
                                    field["field_options"],
                                    field["is_required"],
                                    field["field_order"],
                                ),
                            )
                        db.commit()
                        flash("Event added to the platform.", "success")

            elif action == "add_resource":
                title = request.form.get("title", "").strip()
                category = request.form.get("category", "").strip() or "Development"
                difficulty = request.form.get("difficulty", "").strip() or "Beginner"
                duration = request.form.get("duration", "").strip() or "2 Hours"
                summary = request.form.get("summary", "").strip()
                url = request.form.get("url", "").strip()
                if not all([title, summary, url]):
                    flash("Provide title, summary, and URL for the resource.", "error")
                else:
                    db.execute(
                        """
                        INSERT INTO resources (title, track, difficulty, duration, summary, url, category)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (title, category, difficulty, duration, summary, url, category),
                    )
                    db.commit()
                    flash("Resource added to the learning hub.", "success")

            elif action == "update_settings":
                settings = get_site_settings()
                registration_open = 1 if request.form.get("registration_open") == "on" else 0
                db.execute(
                    """
                    UPDATE site_settings
                    SET registration_open = ?
                    WHERE id = ?
                    """,
                    (registration_open, settings["id"]),
                )
                db.commit()
                flash("Platform settings updated.", "success")

            elif action == "promote_user":
                user_id = request.form.get("user_id", "").strip()
                if not user_id:
                    flash("Choose a valid user to promote.", "error")
                else:
                    db.execute(
                        "UPDATE users SET role = 'core' WHERE id = ?",
                        (user_id,),
                    )
                    db.commit()
                    flash("User promoted to core member.", "success")

            elif action == "demote_user":
                user_id = request.form.get("user_id", "").strip()
                target_user = db.execute(
                    "SELECT id, role FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if target_user is None:
                    flash("User not found.", "error")
                elif target_user["id"] == user["id"]:
                    flash("You cannot remove your own core/admin access from this dashboard.", "error")
                else:
                    db.execute(
                        "UPDATE users SET role = 'member' WHERE id = ?",
                        (user_id,),
                    )
                    db.commit()
                    flash("Core access removed.", "success")

            elif action == "toggle_event_registration":
                event_id = request.form.get("event_id", "").strip()
                event_row = db.execute(
                    "SELECT registration_enabled FROM events WHERE id = ?",
                    (event_id,),
                ).fetchone()
                if event_row is None:
                    flash("Event not found.", "error")
                else:
                    next_state = 0 if event_row["registration_enabled"] else 1
                    db.execute(
                        "UPDATE events SET registration_enabled = ? WHERE id = ?",
                        (next_state, event_id),
                    )
                    db.commit()
                    apply_event_automation_rules()
                    flash("Event registration state updated.", "success")

            elif action == "delete_event":
                event_id = request.form.get("event_id", "").strip()
                registration_ids = [
                    row["id"]
                    for row in db.execute("SELECT id FROM registrations WHERE event_id = ?", (event_id,)).fetchall()
                ]
                if registration_ids:
                    placeholders = ", ".join("?" for _ in registration_ids)
                    db.execute(f"DELETE FROM attendance WHERE registration_id IN ({placeholders})", tuple(registration_ids))
                db.execute("DELETE FROM registrations WHERE event_id = ?", (event_id,))
                db.execute("DELETE FROM event_registrations WHERE event_id = ?", (event_id,))
                db.execute("DELETE FROM event_registration_fields WHERE event_id = ?", (event_id,))
                db.execute("DELETE FROM events WHERE id = ?", (event_id,))
                db.commit()
                flash("Event removed.", "success")

            elif action == "delete_all_events":
                event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                db.execute("DELETE FROM attendance")
                db.execute("DELETE FROM registrations")
                db.execute("DELETE FROM event_registrations")
                db.execute("DELETE FROM event_registration_fields")
                db.execute("DELETE FROM events")
                db.commit()
                flash(f"Deleted all events and related event data ({event_count} events removed).", "success")

            elif action == "notify_hods":
                selected_ids = [value for value in request.form.getlist("registration_ids") if value.strip().isdigit()]
                if not selected_ids:
                    flash("Select at least one registrant before notifying HODs.", "error")
                else:
                    placeholders = ", ".join("?" for _ in selected_ids)
                    selected_registrations = db.execute(
                        f"""
                        SELECT registrations.*, events.title AS event_title
                        FROM registrations
                        JOIN events ON events.id = registrations.event_id
                        WHERE registrations.id IN ({placeholders})
                          AND registrations.department_code != 'OC'
                          AND registrations.od_status = ?
                        ORDER BY registrations.event_id ASC, registrations.department_code ASC, registrations.student_name ASC
                        """,
                        (*tuple(selected_ids), OD_STATUS_REGISTERED),
                    ).fetchall()

                    if not selected_registrations:
                        flash("Select only fresh RIT registrations. Already notified or already approved students are skipped.", "error")
                    else:
                        grouped_rows: dict[tuple[int, str, str], list[sqlite3.Row]] = {}
                        for row in selected_registrations:
                            grouped_rows.setdefault((row["event_id"], row["department_code"], row["hod_email"]), []).append(row)

                        email_results = []
                        for (_, _, _), rows in grouped_rows.items():
                            first_row = rows[0]
                            token = secrets.token_urlsafe(24)
                            approval_url = url_for("approve_students", token=token, _external=True)
                            row_ids = [row["id"] for row in rows]
                            row_placeholders = ", ".join("?" for _ in row_ids)
                            db.execute(
                                f"""
                                UPDATE registrations
                                SET is_selected = 1, od_status = ?, hod_token = ?, hod_notified_at = ?, hod_authorized_at = NULL
                                WHERE id IN ({row_placeholders})
                                """,
                                (
                                    OD_STATUS_ADMIN_SELECTED,
                                    token,
                                    datetime.utcnow().isoformat(timespec="seconds"),
                                    *row_ids,
                                ),
                            )
                            try:
                                email_sent = send_hod_notification_email(
                                    first_row["hod_email"],
                                    f"OD Approval Request | {first_row['event_title']}",
                                    build_hod_notification_body(
                                        first_row["event_title"],
                                        first_row["department_name"],
                                        rows,
                                        approval_url,
                                    ),
                                )
                            except OSError:
                                email_sent = False
                            email_results.append(
                                {
                                    "department_name": first_row["department_name"],
                                    "event_title": first_row["event_title"],
                                    "email_sent": email_sent,
                                    "approval_url": approval_url,
                                }
                            )
                        db.commit()

                        delivered = sum(1 for item in email_results if item["email_sent"])
                        if delivered == len(email_results):
                            flash("HOD notifications were sent for all selected departments.", "success")
                        elif delivered == 0:
                            flash("Approval links were generated, but SMTP is not configured. Share the links manually from the dashboard.", "error")
                        else:
                            flash("Some HOD emails were sent, and some approval links need manual sharing.", "error")

            elif action == "delete_resource":
                resource_id = request.form.get("resource_id", "").strip()
                db.execute("DELETE FROM resources WHERE id = ?", (resource_id,))
                db.commit()
                flash("Resource removed.", "success")

            elif action == "delete_team":
                team_id = request.form.get("team_id", "").strip()
                db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
                db.commit()
                flash("Team removed.", "success")

            return redirect(url_for("admin"))

        recent_teams = db.execute(
            """
            SELECT id, team_name, lead_name, stack, created_at
            FROM teams
            ORDER BY created_at DESC
            LIMIT 6
            """
        ).fetchall()
        managed_event_rows = db.execute(
            """
            SELECT events.id, events.title, COALESCE(events."date", events.event_date) AS event_date,
                   COALESCE(events.location, events.venue) AS location, events.status,
                   COALESCE(events.registration_deadline, COALESCE(events."date", events.event_date)) AS registration_deadline,
                   COALESCE(events.registration_closing_time, '23:59') AS registration_closing_time,
                   COALESCE(events.registration_enabled, 0) AS registration_enabled,
                   COALESCE(events.registration_limit, 0) AS registration_limit,
                   COUNT(event_registrations.id) AS registration_count
            FROM events
            LEFT JOIN event_registrations ON event_registrations.event_id = events.id
            GROUP BY events.id
            ORDER BY COALESCE(events."date", events.event_date) ASC
            LIMIT 12
            """
        ).fetchall()
        managed_events = [build_event_registration_meta(row) for row in managed_event_rows]
        managed_events = apply_capacity_scaling_signals(managed_events)
        managed_resources = db.execute(
            """
            SELECT id, title, category, difficulty
            FROM resources
            ORDER BY id DESC
            LIMIT 12
            """
        ).fetchall()
        users = db.execute(
            """
            SELECT id, name, email, role, created_at
            FROM users
            ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'core' THEN 1 WHEN 'member' THEN 2 ELSE 3 END, created_at ASC
            """
        ).fetchall()
        settings = get_site_settings()
        stats = {
            "teams": db.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
            "events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "resources": db.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
            "core_members": db.execute("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'core')").fetchone()[0],
        }
        hod_registrations = db.execute(
            """
            SELECT registrations.*, events.title AS event_title, events.event_date
            FROM registrations
            JOIN events ON events.id = registrations.event_id
            WHERE registrations.department_code != 'OC'
            ORDER BY
                CASE registrations.od_status
                    WHEN 'Registered' THEN 0
                    WHEN 'Admin_Selected' THEN 1
                    WHEN 'HOD_Authorized' THEN 2
                    ELSE 3
                END,
                registrations.created_at DESC
            LIMIT 150
            """
        ).fetchall()
        hod_link_rows = db.execute(
            """
            SELECT registrations.hod_token, registrations.hod_email, registrations.department_name,
                   registrations.student_name, registrations.register_number,
                   registrations.event_id, events.title AS event_title,
                   MAX(registrations.hod_notified_at) AS hod_notified_at
            FROM registrations
            JOIN events ON events.id = registrations.event_id
            WHERE registrations.hod_token IS NOT NULL AND registrations.hod_token != ''
            GROUP BY registrations.hod_token, registrations.hod_email, registrations.department_name,
                     registrations.student_name, registrations.register_number, registrations.event_id, events.title
            ORDER BY hod_notified_at DESC, registrations.student_name ASC
            """
        ).fetchall()
        hod_links = []
        for row in hod_link_rows:
            if not hod_links or hod_links[-1]["hod_token"] != row["hod_token"]:
                hod_links.append(
                    {
                        "hod_token": row["hod_token"],
                        "hod_email": row["hod_email"],
                        "department_name": row["department_name"],
                        "event_id": row["event_id"],
                        "event_title": row["event_title"],
                        "hod_notified_at": row["hod_notified_at"],
                        "students": [],
                    }
                )
            hod_links[-1]["students"].append(
                {
                    "student_name": row["student_name"],
                    "register_number": row["register_number"],
                }
            )
        hod_links = hod_links[:12]
        recent_hod_approvals = db.execute(
            """
            SELECT registrations.id, registrations.student_name, registrations.register_number,
                   registrations.course, registrations.department_name, registrations.hod_email,
                   registrations.hod_authorized_at, registrations.event_id,
                   events.title AS event_title, COALESCE(events."date", events.event_date) AS event_date
            FROM registrations
            JOIN events ON events.id = registrations.event_id
            WHERE registrations.od_status = ?
              AND registrations.hod_authorized_at IS NOT NULL
              AND registrations.department_code != 'OC'
            ORDER BY registrations.hod_authorized_at DESC, registrations.id DESC
            LIMIT 20
            """,
            (OD_STATUS_HOD_AUTHORIZED,),
        ).fetchall()
        attendance_summary = db.execute(
            """
            SELECT attendance.event_id, events.title AS event_title, COUNT(attendance.id) AS checked_in_count
            FROM attendance
            JOIN events ON events.id = attendance.event_id
            GROUP BY attendance.event_id, events.title
            ORDER BY checked_in_count DESC, attendance.event_id DESC
            LIMIT 12
            """
        ).fetchall()
        return render_template(
            "admin.html",
            recent_teams=recent_teams,
            managed_events=managed_events,
            managed_resources=managed_resources,
            users=users,
            settings=settings,
            stats=stats,
            hod_registrations=hod_registrations,
            hod_links=hod_links,
            recent_hod_approvals=recent_hod_approvals,
            attendance_summary=attendance_summary,
        )

    return app


def build_other_college_register_number(event_id: int, email: str, phone_number: str) -> str:
    email_token = ''.join(ch for ch in email.lower() if ch.isalnum())[:12]
    phone_token = ''.join(ch for ch in phone_number if ch.isdigit())[-6:]
    token = email_token or phone_token or f'guest{event_id}'
    return f'OC-{event_id}-{token}'.upper()


def build_other_college_autofill_profile(access: dict[str, str] | None) -> dict[str, str]:
    if not access:
        return {}
    profile = {
        'name': (access.get('name') or '').strip(),
        'student_name': (access.get('name') or '').strip(),
        'full_name': (access.get('name') or '').strip(),
        'email': (access.get('email') or '').strip(),
        'contact_email': (access.get('email') or '').strip(),
        'participant_email': (access.get('email') or '').strip(),
        'phone': (access.get('phone_number') or '').strip(),
        'phone_number': (access.get('phone_number') or '').strip(),
        'mobile': (access.get('phone_number') or '').strip(),
        'mobile_number': (access.get('phone_number') or '').strip(),
        'college': (access.get('college_name') or '').strip(),
        'college_name': (access.get('college_name') or '').strip(),
        'course': (access.get('course') or '').strip(),
    }
    return {key: value for key, value in profile.items() if value}


def get_other_college_event_access(event_id: int) -> dict[str, str] | None:
    raw_value = session.get(f'other_college_event_{event_id}')
    if not isinstance(raw_value, dict):
        return None
    return {key: str(value).strip() for key, value in raw_value.items()}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def get_current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if user_id is None:
        return None

    if "current_user" not in g:
        g.current_user = get_db().execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return g.current_user


def is_admin(user: sqlite3.Row | None) -> bool:
    return user is not None and user["role"] in ADMIN_ROLES


def is_member(user: sqlite3.Row | None) -> bool:
    return user is not None and user["role"] in MEMBER_ROLES


def has_admin_account() -> bool:
    row = get_db().execute(
        "SELECT COUNT(*) FROM users WHERE role = ? OR role = ?",
        ("admin", "core"),
    ).fetchone()
    return bool(row[0])


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        user = get_current_user()
        if not is_member(user):
            flash("Sign in with a member account to open this page.", "error")
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        user = get_current_user()
        if user is None:
            flash("Sign in with a core or admin account to open the admin dashboard.", "error")
            return redirect(url_for("admin_login"))
        if not is_admin(user):
            flash("Admin access is required for this page.", "error")
            return redirect(url_for("profile"))
        return view(**kwargs)

    return wrapped_view


def build_external_user_register_number(email: str, college_name: str) -> str:
    email_token = ''.join(ch for ch in (email or '').lower() if ch.isalnum())[:12]
    college_token = ''.join(ch for ch in (college_name or '').lower() if ch.isalnum())[:6]
    token = email_token or college_token or 'guest'
    return f"OC-USER-{token}".upper()


def derive_rit_username(email: str) -> str:
    cleaned_email = (email or '').strip().lower()
    if '@' in cleaned_email:
        return cleaned_email.split('@', 1)[0]
    return cleaned_email


def resolve_user_login_email(user_row: sqlite3.Row | dict[str, object] | None) -> str:
    if not user_row:
        return ''
    for key in ('email', 'external_email'):
        value = user_row[key] if isinstance(user_row, sqlite3.Row) and key in user_row.keys() else user_row.get(key) if isinstance(user_row, dict) else None
        if value:
            return str(value).strip().lower()
    rit_username = user_row['rit_username'] if isinstance(user_row, sqlite3.Row) and 'rit_username' in user_row.keys() else user_row.get('rit_username') if isinstance(user_row, dict) else ''
    if rit_username:
        return f"{str(rit_username).strip().lower()}@ritchennai.edu.in"
    return ''


def is_internal_user_type(user: sqlite3.Row | dict[str, object] | None) -> bool:
    if not user:
        return False
    raw = user['user_type'] if isinstance(user, sqlite3.Row) and 'user_type' in user.keys() else user.get('user_type') if isinstance(user, dict) else ''
    return (str(raw or '').strip().lower() or 'internal') == 'internal'


def is_external_user_type(user: sqlite3.Row | dict[str, object] | None) -> bool:
    if not user:
        return False
    raw = user['user_type'] if isinstance(user, sqlite3.Row) and 'user_type' in user.keys() else user.get('user_type') if isinstance(user, dict) else ''
    return str(raw or '').strip().lower() == 'external'


def normalize_phone_number(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip() if ch.isdigit() or ch == "+")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if cleaned and not cleaned.startswith("+"):
        digits_only = "".join(ch for ch in cleaned if ch.isdigit())
        if len(digits_only) == 10:
            cleaned = f"+91{digits_only}"
        elif len(digits_only) == 12 and digits_only.startswith("91"):
            cleaned = f"+{digits_only}"
        else:
            cleaned = digits_only
    return cleaned


def normalize_form_label(value: str) -> str:
    cleaned = ''.join(ch.lower() if ch.isalnum() else ' ' for ch in value)
    return ' '.join(cleaned.split())


def build_user_autofill_profile(user: sqlite3.Row | None) -> dict[str, str]:
    if user is None:
        return {}

    profile = {
        'name': (user['name'] or '').strip(),
        'student_name': (user['name'] or '').strip(),
        'full_name': (user['name'] or '').strip(),
        'register_number': (user['register_number'] or '').strip(),
        'registration_number': (user['register_number'] or '').strip(),
        'roll_number': (user['register_number'] or '').strip(),
        'course': (user['course'] or '').strip(),
        'department': (user['department_name'] or '').strip(),
        'department_name': (user['department_name'] or '').strip(),
        'department_code': (user['department_code'] or '').strip(),
        'email': resolve_user_login_email(user),
        'contact_email': resolve_user_login_email(user),
        'participant_email': resolve_user_login_email(user),
        'college': (user['college_name'] or '').strip() if 'college_name' in user.keys() else '',
        'college_name': (user['college_name'] or '').strip() if 'college_name' in user.keys() else '',
        'phone': (user['phone_number'] or '').strip(),
        'phone_number': (user['phone_number'] or '').strip(),
        'mobile': (user['phone_number'] or '').strip(),
        'mobile_number': (user['phone_number'] or '').strip(),
        'team_lead': (user['name'] or '').strip(),
        'lead_name': (user['name'] or '').strip(),
    }
    return {key: value for key, value in profile.items() if value}


def guess_autofill_value(field_label: str, autofill_profile: dict[str, str]) -> str:
    normalized_label = normalize_form_label(field_label)
    if not normalized_label:
        return ''

    direct_aliases = {
        'name': 'name',
        'student name': 'student_name',
        'full name': 'full_name',
        'register number': 'register_number',
        'registration number': 'registration_number',
        'roll number': 'roll_number',
        'course': 'course',
        'department': 'department',
        'department name': 'department_name',
        'department code': 'department_code',
        'email': 'email',
        'contact email': 'contact_email',
        'participant email': 'participant_email',
        'phone': 'phone',
        'phone number': 'phone_number',
        'mobile': 'mobile',
        'mobile number': 'mobile_number',
        'team lead': 'team_lead',
        'lead name': 'lead_name',
    }
    alias = direct_aliases.get(normalized_label)
    if alias:
        return autofill_profile.get(alias, '')

    keyword_aliases = [
        (('register', 'number'), 'register_number'),
        (('registration', 'number'), 'registration_number'),
        (('roll', 'number'), 'roll_number'),
        (('student', 'name'), 'student_name'),
        (('full', 'name'), 'full_name'),
        (('team', 'lead'), 'team_lead'),
        (('lead',), 'lead_name'),
        (('department', 'code'), 'department_code'),
        (('department',), 'department_name'),
        (('course',), 'course'),
        (('email',), 'email'),
        (('phone',), 'phone_number'),
        (('mobile',), 'mobile_number'),
        (('name',), 'name'),
    ]
    for keywords, autofill_key in keyword_aliases:
        if all(keyword in normalized_label for keyword in keywords):
            return autofill_profile.get(autofill_key, '')
    return ''


def build_registration_form_defaults(
    current_user: sqlite3.Row | None,
    registration_fields: list[dict[str, object]],
    *,
    contact_email_required: bool,
    autofill_profile: dict[str, str] | None = None,
) -> dict[str, object]:
    if autofill_profile is None:
        autofill_profile = build_user_autofill_profile(current_user)
    field_defaults: dict[int, str] = {}
    for field in registration_fields:
        guessed_value = guess_autofill_value(str(field['field_label']), autofill_profile)
        if guessed_value:
            field_defaults[int(field['id'])] = guessed_value

    contact_email = autofill_profile.get('contact_email', '') if contact_email_required else ''
    return {
        'field_defaults': field_defaults,
        'contact_email': contact_email,
    }


def create_password_reset_otp(user_id: int) -> str:
    db = get_db()
    otp = f"{secrets.randbelow(900000) + 100000}"
    expires_at = (datetime.utcnow() + timedelta(minutes=10)).isoformat(timespec="seconds")
    db.execute("DELETE FROM password_reset_otps WHERE user_id = ?", (user_id,))
    db.execute(
        "INSERT INTO password_reset_otps (user_id, otp, expires_at) VALUES (?, ?, ?)",
        (user_id, otp, expires_at),
    )
    db.commit()
    return otp


def verify_password_reset_otp(user_id: int, otp: str) -> bool:
    db = get_db()
    row = db.execute(
        """
        SELECT id, expires_at
        FROM password_reset_otps
        WHERE user_id = ? AND otp = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id, otp),
    ).fetchone()
    if row is None:
        return False
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at < datetime.utcnow():
        db.execute("DELETE FROM password_reset_otps WHERE id = ?", (row["id"],))
        db.commit()
        return False
    return True


def send_password_reset_otp(phone_number: str, otp: str) -> None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER")
    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID")

    if not account_sid or not auth_token or not (twilio_phone_number or messaging_service_sid):
        raise RuntimeError(
            "Twilio settings are missing. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and either TWILIO_PHONE_NUMBER or TWILIO_MESSAGING_SERVICE_SID."
        )

    from twilio.rest import Client

    client = Client(account_sid, auth_token)
    message_body = f"you are getting this msg bcs uve requested the password change for the geeks for geeks website {otp} : dont share with anyone -Admin "
    payload = {
        "body": message_body,
        "to": phone_number,
    }
    if messaging_service_sid:
        payload["messaging_service_sid"] = messaging_service_sid
    else:
        payload["from_"] = twilio_phone_number
    client.messages.create(**payload)


def is_valid_rit_verification_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc.endswith("ritchennai.edu.in")


def _extract_label_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower().rstrip(":")
            value = cells[1].get_text(" ", strip=True)
            if value in {":", "-", ""} and len(cells) > 2:
                value = cells[2].get_text(" ", strip=True)
            if label and value and value not in {":", "-", ""}:
                pairs[label] = value

    for element in soup.find_all(["p", "li", "div", "span"]):
        text = element.get_text(" ", strip=True)
        if ":" not in text:
            continue
        label, value = [part.strip() for part in text.split(":", 1)]
        label = label.lower().rstrip(":")
        if label and value and len(label) <= 40:
            pairs.setdefault(label, value)

    definitions = soup.find_all(["dt"])
    for dt in definitions:
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        label = dt.get_text(" ", strip=True).lower().rstrip(":")
        value = dd.get_text(" ", strip=True)
        if label and value:
            pairs[label] = value
    return pairs


def _lookup_identity_value(pairs: dict[str, str], *keywords: str) -> str | None:
    for key, value in pairs.items():
        if any(keyword in key for keyword in keywords):
            return value
    return None


def get_rit_identity(url: str) -> dict[str, str]:
    cleaned_url = url.strip()
    if not cleaned_url or not is_valid_rit_verification_url(cleaned_url):
        raise ValueError("Use a valid RIT verification link.")
    if requests is None or BeautifulSoup is None:
        raise RuntimeError("Install requests and beautifulsoup4 to use RIT identity verification.")

    response = None
    last_exception: Exception | None = None
    timeouts = [(5, 10), (5, 15), (5, 20)]

    for timeout_window in timeouts:
        try:
            response = requests.get(
                cleaned_url,
                timeout=timeout_window,
                headers={"User-Agent": "RIT-GFG-Club-Portal/1.0"},
                verify=False,
                proxies={"http": None, "https": None},
            )
            response.raise_for_status()
            break
        except REQUESTS_EXCEPTION as exc:
            last_exception = exc
            response = None

    if response is None:
        if last_exception is not None:
            raise RuntimeError(
                "RIT verification page could not be reached after multiple attempts. "
                "The IMS server may be slow or temporarily unavailable. Please try again."
            ) from last_exception
        raise RuntimeError("RIT verification page could not be reached.")

    soup = BeautifulSoup(response.text, "html.parser")
    pairs = _extract_label_value_pairs(soup)

    student_name = _lookup_identity_value(pairs, "student name", "name")
    register_number = _lookup_identity_value(pairs, "register number", "register no", "reg no", "reg number", "roll number", "roll no")
    course = _lookup_identity_value(pairs, "course", "programme", "program", "department")

    import re
    if register_number is None or len("".join(c for c in register_number if c.isdigit())) < 10:
        matches = re.findall(r'\b\d{10,15}\b', soup.get_text(separator=" "))
        if matches:
            register_number = matches[0]

    if not student_name or not register_number or not course:
        raise ValueError("Could not extract student name, register number, and course from the RIT verification page.")

    return {
        "student_name": student_name,
        "register_number": register_number,
        "course": course,
        "verification_url": cleaned_url,
    }


def derive_department_from_register_number(register_number: str) -> dict[str, str]:
    digits = "".join(ch for ch in register_number if ch.isdigit())
    if len(digits) < 10:
        raise ValueError("Register number must contain at least 10 digits.")

    department_code = digits[8:10]
    department = RIT_DEPARTMENT_MAP.get(department_code)
    if department is None:
        raise ValueError(f"Unsupported department code '{department_code}' in register number.")

    return {
        "department_code": department_code,
        "department_name": department["name"],
        "department_slug": department["slug"],
        "hod_email": "balajibr903@gmail.com",
        "register_number_digits": digits,
    }


def ensure_hod_contact(department_code: str, department_name: str, department_slug: str, hod_email: str) -> None:
    db = get_db()
    existing = db.execute(
        "SELECT id FROM hod_contacts WHERE department_code = ?",
        (department_code,),
    ).fetchone()
    if existing is None:
        db.execute(
            """
            INSERT INTO hod_contacts (department_code, department_name, department_slug, hod_email)
            VALUES (?, ?, ?, ?)
            """,
            (department_code, department_name, department_slug, hod_email),
        )
    else:
        db.execute(
            """
            UPDATE hod_contacts
            SET department_name = ?, department_slug = ?, hod_email = ?
            WHERE department_code = ?
            """,
            (department_name, department_slug, hod_email, department_code),
        )
    db.commit()


def build_identity_payload(verification_url: str) -> dict[str, str]:
    identity = get_rit_identity(verification_url)
    department = derive_department_from_register_number(identity["register_number"])
    ensure_hod_contact(
        department["department_code"],
        department["department_name"],
        department["department_slug"],
        department["hod_email"],
    )
    identity.update(department)
    identity["register_number"] = department["register_number_digits"]

    name_parts = identity.get("student_name", "").strip().split()
    cleaned_parts = []
    for part in name_parts:
        normalized_part = ''.join(ch for ch in part.lower() if ch.isalnum())
        if normalized_part:
            cleaned_parts.append(normalized_part)
    multi_letter_parts = [part for part in cleaned_parts if len(part) > 1]
    email_name = "".join(multi_letter_parts or cleaned_parts)[:32] or "student"

    reg_digits = department["register_number_digits"]
    if len(reg_digits) >= 10:
        email_number = reg_digits[4:6] + reg_digits[-4:]
    else:
        email_number = reg_digits[-6:] if len(reg_digits) >= 6 else reg_digits
    dept_slug = department["department_slug"]

    identity["participant_email"] = f"{email_name}.{email_number}@{dept_slug}.ritchennai.edu.in"

    return identity


def decode_qr_image_bytes(image_bytes: bytes) -> str:
    if cv2 is None or np is None:
        raise RuntimeError("Install OpenCV and NumPy to decode QR images on the server.")

    if not image_bytes:
        raise ValueError("Upload a QR image first.")

    image_array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("The uploaded file could not be read as an image.")

    detector = cv2.QRCodeDetector()
    decoded_text, _, _ = detector.detectAndDecode(image)
    if not decoded_text:
        raise ValueError("No QR code was found in the uploaded image.")
    return decoded_text.strip()


def send_hod_notification_email(hod_email: str, subject: str, body: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from_email = os.getenv("SMTP_FROM_EMAIL") or smtp_username
    smtp_use_tls = os.getenv("SMTP_USE_TLS", "1") != "0"

    if not smtp_host or not smtp_from_email:
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from_email
    message["To"] = hod_email
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        if smtp_use_tls:
            server.starttls()
        if smtp_username and smtp_password:
            server.login(smtp_username, smtp_password)
        server.send_message(message)
    return True


def build_hod_notification_body(event_title: str, department_name: str, students: list[sqlite3.Row], approval_url: str) -> str:
    lines = [
        f"RIT GfG Club has selected the following students for {event_title}.",
        "",
        f"Department: {department_name}",
        "",
        "Selected students:",
    ]
    for index, student in enumerate(students, start=1):
        lines.append(f"{index}. {student['student_name']} | {student['register_number']} | {student['course']}")
    lines.extend(
        [
            "",
            "Approve all selected students using the secure link below:",
            approval_url,
            "",
            "This link is password-less and authorizes only the listed students.",
        ]
    )
    return "\n".join(lines)


def fetch_identity_registration(registration_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        """
        SELECT registrations.*, events.title AS event_title
        FROM registrations
        JOIN events ON events.id = registrations.event_id
        WHERE registrations.id = ?
        """,
        (registration_id,),
    ).fetchone()


def ensure_column(table: str, column: str, definition: str) -> None:
    db = get_db()
    existing_columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing_columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()


def ensure_default_admin_users() -> None:
    db = get_db()
    default_admins = [
        {
            "name": "Admin One",
            "email": "admin1@example.com",
            "phone_number": "9999999991",
            "password": "admin@123",
            "register_number": "ADMIN001",
            "rit_username": "admin1",
        },
        {
            "name": "Admin Two",
            "email": "admin2@example.com",
            "phone_number": "9999999992",
            "password": "admin@2026",
            "register_number": "ADMIN002",
            "rit_username": "admin2",
        },
    ]

    for admin in default_admins:
        existing_user = db.execute(
            "SELECT id FROM users WHERE email = ?",
            (admin["email"],),
        ).fetchone()
        password_hash = generate_password_hash(admin["password"])
        values = (
            admin["name"],
            admin["email"],
            admin["phone_number"],
            password_hash,
            "admin",
            admin["register_number"],
            "CSE",
            "20",
            "Computer Science and Engineering",
            "cse",
            admin["rit_username"],
            "Rajalakshmi Institute of Technology",
            "internal",
            1,
        )
        if existing_user is None:
            db.execute(
                """
                INSERT INTO users (
                    name, email, phone_number, password_hash, role,
                    register_number, course, department_code, department_name, department_slug,
                    rit_username, college_name, user_type, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        else:
            db.execute(
                """
                UPDATE users
                SET name = ?, phone_number = ?, password_hash = ?, role = ?,
                    register_number = ?, course = ?, department_code = ?, department_name = ?, department_slug = ?,
                    rit_username = ?, college_name = ?, user_type = ?, is_active = ?
                WHERE id = ?
                """,
                (
                    admin["name"],
                    admin["phone_number"],
                    password_hash,
                    "admin",
                    admin["register_number"],
                    "CSE",
                    "20",
                    "Computer Science and Engineering",
                    "cse",
                    admin["rit_username"],
                    "Rajalakshmi Institute of Technology",
                    "internal",
                    1,
                    existing_user["id"],
                ),
            )
    db.commit()


def parse_datetime_value(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    normalized = normalized.replace('T', ' ')
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def should_count_event_as_hosted(event_row: sqlite3.Row | dict[str, object], now: datetime | None = None) -> bool:
    created_at = parse_datetime_value(event_row['created_at'] if isinstance(event_row, sqlite3.Row) else event_row.get('created_at'))
    if created_at is None:
        return False
    current_time = now or datetime.utcnow()
    hosted_excluded = int((event_row['hosted_excluded'] if isinstance(event_row, sqlite3.Row) else event_row.get('hosted_excluded', 0)) or 0)
    hosted_counted = int((event_row['hosted_counted'] if isinstance(event_row, sqlite3.Row) else event_row.get('hosted_counted', 0)) or 0)
    if hosted_excluded or hosted_counted:
        return False
    return created_at <= current_time - timedelta(days=1)


def sync_hosted_event_total() -> None:
    db = get_db()
    settings = get_site_settings()
    pending_rows = db.execute(
        """
        SELECT id, created_at, hosted_counted, hosted_excluded
        FROM events
        WHERE COALESCE(hosted_counted, 0) = 0 AND COALESCE(hosted_excluded, 0) = 0
        """
    ).fetchall()
    qualifying_ids = [row['id'] for row in pending_rows if should_count_event_as_hosted(row)]
    if not qualifying_ids:
        return
    placeholders = ', '.join('?' for _ in qualifying_ids)
    db.execute(
        "UPDATE site_settings SET hosted_event_total = COALESCE(hosted_event_total, 0) + ? WHERE id = ?",
        (len(qualifying_ids), settings['id']),
    )
    db.execute(
        f"UPDATE events SET hosted_counted = 1 WHERE id IN ({placeholders})",
        tuple(qualifying_ids),
    )
    db.commit()


def mark_event_hosting_outcome(event_id: str | int, *, exclude_if_within_day: bool = False) -> None:
    db = get_db()
    event_row = db.execute(
        "SELECT id, created_at, hosted_counted, hosted_excluded FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if event_row is None:
        return
    settings = get_site_settings()
    if should_count_event_as_hosted(event_row):
        db.execute(
            "UPDATE site_settings SET hosted_event_total = COALESCE(hosted_event_total, 0) + 1 WHERE id = ?",
            (settings['id'],),
        )
        db.execute("UPDATE events SET hosted_counted = 1 WHERE id = ?", (event_id,))
        db.commit()
        return
    if exclude_if_within_day and not int(event_row['hosted_counted'] or 0):
        created_at = parse_datetime_value(event_row['created_at'])
        if created_at is not None and created_at > datetime.utcnow() - timedelta(days=1):
            db.execute("UPDATE events SET hosted_excluded = 1 WHERE id = ?", (event_id,))
            db.commit()


def get_site_settings() -> sqlite3.Row:
    db = get_db()
    settings = db.execute(
        """
        SELECT id, registration_open, submission_open, participant_login_required, COALESCE(hosted_event_total, 0) AS hosted_event_total
        FROM site_settings
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    if settings is None:
        db.execute(
            """
            INSERT INTO site_settings (registration_open, submission_open, participant_login_required, hosted_event_total)
            VALUES (1, 1, 1, 0)
            """
        )
        db.commit()
        settings = db.execute(
            """
            SELECT id, registration_open, submission_open, participant_login_required
            FROM site_settings
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
    return settings


def build_contact_links() -> dict[str, str]:
    return {
        "email": "gfgclub.rit@example.com",
        "mailto": "mailto:gfgclub.rit@example.com",
        "whatsapp": "https://chat.whatsapp.com/example-campus-club",
        "community": "https://www.geeksforgeeks.org/",
    }


def build_initials(name: str) -> str:
    parts = [part for part in name.strip().split() if part]
    if not parts:
        return "GC"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def build_static_faqs() -> list[dict[str, str]]:
    return [
        {
            "question": "How do I take part in club activities?",
            "answer": "Check the Events page for upcoming registrations and use the event form when an event is open.",
        },
        {
            "question": "Do I need an account before joining activities?",
            "answer": "Most public pages are open directly. Create an account only when you want a personal workspace or password recovery tied to your details.",
        },
        {
            "question": "Where will event announcements appear?",
            "answer": "Upcoming events are published on the Events page and through the club contact channels.",
        },
        {
            "question": "How can I contact the club?",
            "answer": "Use the email or WhatsApp links on the Contact page for direct communication.",
        },
    ]


def build_about_content() -> dict[str, object]:
    return {
        "title": "About GeeksforGeeks Campus Club",
        "mission": [
            "Create a strong coding culture through contests, workshops, and student-led learning.",
            "Help students improve DSA, development, interview readiness, and practical execution.",
            "Turn participation into visible outcomes through events, registrations, and rankings.",
        ],
        "faculty_coordinator": "Faculty coordinator details can be updated from the club information panel.",
        "core_team": [
            "President / Campus Lead",
            "Technical Lead",
            "Community Lead",
            "Design and Events Lead",
        ],
    }


def map_resource_category(track: str | None) -> str:
    if track is None:
        return "Development"
    value = track.lower()
    if "dsa" in value or "data" in value:
        return "DSA"
    if "interview" in value:
        return "Interview Preparation"
    if "competitive" in value or "cp" in value:
        return "Competitive Programming"
    if "system" in value:
        return "System Design"
    return "Development"


def build_event_registration_meta(event_row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    event = dict(event_row)
    registration_limit = int(event.get("registration_limit") or 0)
    registration_count = int(event.get("registration_count") or 0)
    registration_enabled = bool(event.get("registration_enabled"))
    if registration_limit > 0:
        remaining_slots = max(registration_limit - registration_count, 0)
        registration_progress = min((registration_count / registration_limit) * 100, 100)
    else:
        remaining_slots = None
        registration_progress = 0

    if registration_limit > 0 and registration_count >= registration_limit:
        registration_status_label = "Registration Completed"
    elif registration_enabled:
        registration_status_label = "Open for Registration"
    else:
        registration_status_label = "Registration Closed"

    registration_closes_at = build_registration_deadline_datetime(
        event.get("registration_deadline") or event.get("event_date"),
        event.get("registration_closing_time") or "23:59",
    )

    event["remaining_slots"] = remaining_slots
    event["registration_progress"] = registration_progress
    event["registration_status_label"] = registration_status_label
    event["registration_is_full"] = registration_limit > 0 and registration_count >= registration_limit
    event["registration_is_open"] = registration_enabled and not event["registration_is_full"]
    event["registration_closes_at"] = (
        registration_closes_at.isoformat(timespec="seconds") if registration_closes_at is not None else ""
    )
    event["recent_registration_count"] = int(event.get("recent_registration_count") or 0)
    event["capacity_recommendation"] = {
        "active": False,
        "message": "",
        "window_hours": 2,
        "recent_registrations": event["recent_registration_count"],
        "recommended_capacity": registration_limit,
    }
    return event


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_iso_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def build_registration_deadline_datetime(deadline_date: str | None, closing_time: str | None) -> datetime | None:
    parsed_date = parse_iso_date(deadline_date)
    parsed_time = parse_iso_time(closing_time) or time(hour=23, minute=59)
    if parsed_date is None:
        return None
    return datetime.combine(parsed_date, parsed_time)


def build_hod_approval_deadline_datetime(event_date: str | None, event_end_time: str | None) -> datetime | None:
    parsed_date = parse_iso_date(event_date)
    parsed_end_time = parse_iso_time(event_end_time) or time(hour=23, minute=59)
    if parsed_date is None:
        return None
    return datetime.combine(parsed_date, parsed_end_time) - timedelta(minutes=10)


def apply_event_automation_rules() -> None:
    db = get_db()
    today = date.today()

    event_rows = db.execute(
        """
        SELECT events.id, COALESCE(events."date", events.event_date) AS event_date,
               COALESCE(events.registration_deadline, COALESCE(events."date", events.event_date)) AS registration_deadline,
               COALESCE(events.registration_closing_time, '23:59') AS registration_closing_time,
               COALESCE(events.registration_enabled, 0) AS registration_enabled,
               COALESCE(events.registration_limit, 0) AS registration_limit,
               COALESCE(events.status, 'Open') AS status,
               COUNT(event_registrations.id) AS registration_count
        FROM events
        LEFT JOIN event_registrations ON event_registrations.event_id = events.id
        GROUP BY events.id
        """
    ).fetchall()

    changed = False
    for row in event_rows:
        event_id = row["id"]
        event_date = parse_iso_date(row["event_date"])
        registration_deadline = parse_iso_date(row["registration_deadline"])
        registration_deadline_at = build_registration_deadline_datetime(row["registration_deadline"], row["registration_closing_time"])
        registration_enabled = bool(row["registration_enabled"])
        registration_limit = int(row["registration_limit"] or 0)
        registration_count = int(row["registration_count"] or 0)
        current_status = (row["status"] or "Open").strip() or "Open"

        next_registration_enabled = registration_enabled
        next_status = current_status

        if event_date is not None and event_date < today:
            next_registration_enabled = False
            next_status = "Completed"
        else:
            if registration_deadline_at is not None and registration_deadline_at < datetime.now():
                next_registration_enabled = False
            if registration_limit > 0 and registration_count >= registration_limit:
                next_registration_enabled = False
            if next_status == "Completed" and (event_date is None or event_date >= today):
                next_status = "Open"

        if next_registration_enabled != registration_enabled or next_status != current_status:
            db.execute(
                "UPDATE events SET registration_enabled = ?, status = ? WHERE id = ?",
                (1 if next_registration_enabled else 0, next_status, event_id),
            )
            changed = True

    if changed:
        db.commit()


def apply_capacity_scaling_signals(events: list[dict[str, object]], lookback_hours: int = 2) -> list[dict[str, object]]:
    if not events:
        return events

    event_ids = [int(event["id"]) for event in events if event.get("id") is not None]
    if not event_ids:
        return events

    cutoff = (datetime.utcnow() - timedelta(hours=lookback_hours)).isoformat(sep=" ", timespec="seconds")
    placeholders = ", ".join("?" for _ in event_ids)
    recent_counts = {
        row["event_id"]: int(row["recent_registration_count"])
        for row in get_db().execute(
            f"""
            SELECT event_id, COUNT(*) AS recent_registration_count
            FROM event_registrations
            WHERE event_id IN ({placeholders})
              AND created_at >= ?
            GROUP BY event_id
            """,
            (*event_ids, cutoff),
        ).fetchall()
    }

    for event in events:
        registration_limit = int(event.get("registration_limit") or 0)
        registration_count = int(event.get("registration_count") or 0)
        recent_registration_count = recent_counts.get(int(event["id"]), 0)
        event["recent_registration_count"] = recent_registration_count

        demand_spike_detected = (
            registration_limit > 0
            and bool(event.get("registration_enabled"))
            and registration_count < registration_limit
            and recent_registration_count >= max(10, int(registration_limit * 0.75))
            and registration_count >= max(20, int(registration_limit * 0.85))
        )
        recommended_capacity = registration_limit
        if demand_spike_detected:
            recommended_capacity = max(registration_limit + 10, int((registration_limit * 1.2) + 0.9999))

        event["capacity_recommendation"] = {
            "active": demand_spike_detected,
            "message": "Demand spike detected. Consider increasing capacity." if demand_spike_detected else "",
            "window_hours": lookback_hours,
            "recent_registrations": recent_registration_count,
            "recommended_capacity": recommended_capacity,
        }

    return events
def hydrate_registration_field(field_row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    field = dict(field_row)
    field_type = field.get("field_type") or "text"
    option_values: list[str] = []
    if field_type == "select":
        raw_options = field.get("field_options") or ""
        try:
            parsed_options = json.loads(raw_options) if raw_options else []
        except json.JSONDecodeError:
            parsed_options = []
        option_values = [str(option).strip() for option in parsed_options if str(option).strip()]
    field["field_type"] = field_type
    field["option_values"] = option_values
    return field


def parse_registration_field_definitions(raw_value: str) -> tuple[list[dict[str, object]], str | None]:
    field_rows = [line.strip() for line in raw_value.splitlines() if line.strip()]
    if len(field_rows) > 30:
        return [], "You can configure a maximum of 30 registration fields for one event."

    parsed_fields: list[dict[str, object]] = []
    for index, row in enumerate(field_rows, start=1):
        field_label = row
        field_type = "text"
        field_options = ""

        if "-" in row:
            possible_label, possible_options = [part.strip() for part in row.split("-", 1)]
            option_values = [part.strip() for part in possible_options.split(".") if part.strip()]
            if possible_label and option_values:
                field_label = possible_label
                field_type = "select"
                field_options = json.dumps(option_values)

        if not field_label:
            return [], f"field_label required for registration field {index}."
        parsed_fields.append(
            {
                "field_label": field_label,
                "field_type": field_type,
                "field_options": field_options,
                "is_required": 1,
                "field_order": index,
            }
        )
    return parsed_fields, None


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_date TEXT NOT NULL,
            format TEXT NOT NULL,
            venue TEXT NOT NULL,
            category TEXT NOT NULL,
            seats INTEGER NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            track TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            duration TEXT NOT NULL,
            summary TEXT NOT NULL,
            url TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_name TEXT NOT NULL,
            lead_name TEXT NOT NULL,
            email TEXT NOT NULL,
            members TEXT NOT NULL,
            stack TEXT NOT NULL,
            vision TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            features TEXT NOT NULL,
            repo_url TEXT NOT NULL,
            demo_url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            phone_number TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS site_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_open INTEGER NOT NULL DEFAULT 1,
            submission_open INTEGER NOT NULL DEFAULT 1,
            participant_login_required INTEGER NOT NULL DEFAULT 1,
            hosted_event_total INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS password_reset_otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            otp TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS event_registration_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            field_label TEXT NOT NULL,
            field_type TEXT NOT NULL DEFAULT 'text',
            field_options TEXT NOT NULL DEFAULT '',
            is_required INTEGER NOT NULL DEFAULT 1,
            field_order INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );

        CREATE TABLE IF NOT EXISTS event_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );

        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            student_name TEXT NOT NULL,
            register_number TEXT NOT NULL,
            course TEXT NOT NULL,
            department_code TEXT NOT NULL,
            department_name TEXT NOT NULL,
            department_slug TEXT NOT NULL,
            hod_email TEXT NOT NULL,
            verification_url TEXT NOT NULL,
            contact_email TEXT NOT NULL DEFAULT '',
            response_json TEXT NOT NULL,
            od_status TEXT NOT NULL DEFAULT 'Registered',
            is_selected INTEGER NOT NULL DEFAULT 0,
            hod_token TEXT,
            hod_notified_at TEXT,
            hod_authorized_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(event_id) REFERENCES events(id),
            UNIQUE(event_id, register_number)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            registration_id INTEGER NOT NULL,
            verification_url TEXT NOT NULL,
            check_in_status TEXT NOT NULL DEFAULT 'Denied',
            checked_in_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(event_id) REFERENCES events(id),
            FOREIGN KEY(registration_id) REFERENCES registrations(id),
            UNIQUE(event_id, registration_id)
        );

        CREATE TABLE IF NOT EXISTS hod_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department_code TEXT NOT NULL UNIQUE,
            department_name TEXT NOT NULL,
            department_slug TEXT NOT NULL,
            hod_email TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    db.commit()
    ensure_column("site_settings", "hosted_event_total", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("users", "register_number", "TEXT")
    ensure_column("users", "course", "TEXT")
    ensure_column("users", "department_code", "TEXT")
    ensure_column("users", "department_name", "TEXT")
    ensure_column("users", "department_slug", "TEXT")
    ensure_column("users", "rit_username", "TEXT")
    ensure_column("users", "external_email", "TEXT")
    ensure_column("users", "college_name", "TEXT DEFAULT 'Rajalakshmi Institute of Technology'")
    ensure_column("users", "user_type", "TEXT DEFAULT 'internal'")
    ensure_column("users", "is_active", "INTEGER NOT NULL DEFAULT 0")

    ensure_column("events", "description", "TEXT")
    ensure_column("events", "date", "TEXT")
    ensure_column("events", "location", "TEXT")
    ensure_column("events", "registration_link", "TEXT")
    ensure_column("resources", "category", "TEXT DEFAULT 'Development'")
    ensure_column("users", "phone_number", "TEXT")
    ensure_column("events", "registration_enabled", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("events", "registration_limit", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("events", "contact_email_required", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("events", "interact_other_colleges", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("events", "registration_deadline", "TEXT")
    ensure_column("events", "registration_closing_time", "TEXT")
    ensure_column("events", "event_start_time", "TEXT")
    ensure_column("events", "event_end_time", "TEXT")
    ensure_column("events", "created_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
    ensure_column("events", "hosted_counted", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("events", "hosted_excluded", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("event_registration_fields", "field_options", "TEXT NOT NULL DEFAULT ''")
    ensure_column("registrations", "contact_email", "TEXT NOT NULL DEFAULT ''")
    ensure_column("registrations", "is_selected", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("registrations", "hod_token", "TEXT")
    ensure_column("registrations", "hod_notified_at", "TEXT")
    ensure_column("registrations", "hod_authorized_at", "TEXT")

    db.execute('UPDATE events SET description = COALESCE(description, summary)')
    db.execute('UPDATE events SET "date" = COALESCE("date", event_date)')
    db.execute('UPDATE events SET location = COALESCE(location, venue)')
    db.execute('UPDATE events SET registration_link = COALESCE(registration_link, "")')
    db.execute('UPDATE events SET registration_enabled = COALESCE(registration_enabled, 0)')
    db.execute('UPDATE events SET registration_limit = COALESCE(registration_limit, 0)')
    db.execute('UPDATE events SET contact_email_required = COALESCE(contact_email_required, 0)')
    db.execute('UPDATE events SET interact_other_colleges = COALESCE(interact_other_colleges, 0)')
    db.execute('UPDATE events SET registration_deadline = COALESCE(registration_deadline, COALESCE("date", event_date))')
    db.execute('UPDATE events SET registration_closing_time = COALESCE(registration_closing_time, "23:59")')
    db.execute('UPDATE events SET event_start_time = COALESCE(event_start_time, "09:00")')
    db.execute('UPDATE events SET event_end_time = COALESCE(event_end_time, registration_closing_time, "23:59")')
    db.execute('UPDATE events SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)')
    db.execute('UPDATE events SET hosted_counted = COALESCE(hosted_counted, 0)')
    db.execute('UPDATE events SET hosted_excluded = COALESCE(hosted_excluded, 0)')
    db.execute("UPDATE event_registration_fields SET field_options = COALESCE(field_options, '')")
    db.execute('UPDATE resources SET category = COALESCE(category, "")')
    db.execute("UPDATE users SET role = 'member' WHERE role = 'participant'")
    db.execute("UPDATE users SET role = 'core' WHERE role = 'coordinator'")
    db.execute("UPDATE users SET user_type = COALESCE(NULLIF(user_type, ''), 'internal')")
    db.execute("UPDATE users SET college_name = COALESCE(NULLIF(college_name, ''), 'Rajalakshmi Institute of Technology')")
    db.execute("UPDATE users SET rit_username = LOWER(SUBSTR(email, 1, INSTR(email, '@') - 1)) WHERE COALESCE(rit_username, '') = '' AND email LIKE '%ritchennai.edu.in'")
    db.execute("UPDATE users SET external_email = email WHERE COALESCE(external_email, '') = '' AND department_code = 'OC'")
    db.execute("UPDATE users SET user_type = 'external' WHERE department_code = 'OC' OR COALESCE(external_email, '') != ''")
    db.execute("UPDATE users SET is_active = 1 WHERE COALESCE(is_active, 0) = 0 AND role IN ('admin', 'core', 'member')")
    db.execute("UPDATE registrations SET od_status = COALESCE(od_status, 'Registered')")
    db.execute("UPDATE registrations SET contact_email = COALESCE(contact_email, '')")
    db.commit()

    resource_rows = db.execute('SELECT id, track, category FROM resources').fetchall()
    for row in resource_rows:
        category = row["category"] or map_resource_category(row["track"])
        db.execute('UPDATE resources SET category = ? WHERE id = ?', (category, row["id"]))
    db.commit()

    db.execute(
        "DELETE FROM password_reset_otps WHERE expires_at < ?",
        (datetime.utcnow().isoformat(timespec="seconds"),),
    )
    db.commit()

    for department_code, payload in RIT_DEPARTMENT_MAP.items():
        db.execute(
            """
            INSERT INTO hod_contacts (department_code, department_name, department_slug, hod_email)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(department_code) DO UPDATE SET
                department_name = excluded.department_name,
                department_slug = excluded.department_slug,
                hod_email = excluded.hod_email
            """,
            (
                department_code,
                payload["name"],
                payload["slug"],
                "balajibr903@gmail.com",
            ),
        )
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_rit_username_unique ON users(rit_username) WHERE rit_username IS NOT NULL AND rit_username != ''")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_email_unique ON users(external_email) WHERE external_email IS NOT NULL AND external_email != ''")
    db.commit()

    ensure_default_admin_users()

    db.commit()

    get_site_settings()




app = create_app()


if __name__ == "__main__":
    debug_rit_url = os.getenv("RIT_DEBUG_URL", "").strip()
    if debug_rit_url:
        try:
            with app.app_context():
                print(json.dumps(build_identity_payload(debug_rit_url), indent=2))
        except Exception as exc:
            print(f"RIT debug failed: {exc}")
            raise
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5002")),
        debug=True,
    )












