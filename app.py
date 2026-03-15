from __future__ import annotations

import csv
import io
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
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
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DATABASE_PATH = INSTANCE_DIR / "gfg_club.db"
ASSETS_DIR = BASE_DIR / "Assets"
ADMIN_ROLES = {"admin", "coordinator"}
PARTICIPANT_ROLES = {"participant", "member", "admin", "coordinator"}
ENV_SECURED_PATH = BASE_DIR / ".env_secured"


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
    app.config.update(SECRET_KEY="gfg-campus-club-rit-2026", DATABASE=str(DATABASE_PATH))

    INSTANCE_DIR.mkdir(exist_ok=True)

    with app.app_context():
        init_db()

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


    @app.route("/admin/setup", methods=("GET", "POST"))
    def admin_setup():
        if has_admin_account():
            flash("Coordinator access is already configured. Sign in with the coordinator account.", "error")
            return redirect(url_for("admin_login"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone_number = normalize_phone_number(request.form.get("phone_number", ""))
            password = request.form.get("password", "")

            if not all([name, email, phone_number, password]):
                flash("Complete every field to create the first coordinator account.", "error")
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
                if duplicate_phone is not None:
                    flash("That phone number is already linked to another account.", "error")
                elif existing_user is not None:
                    db.execute(
                        "UPDATE users SET name = ?, phone_number = ?, password_hash = ?, role = ? WHERE id = ?",
                        (name, phone_number, generate_password_hash(password), "coordinator", existing_user["id"]),
                    )
                    user_id = existing_user["id"]
                else:
                    db.execute(
                        "INSERT INTO users (name, email, phone_number, password_hash, role) VALUES (?, ?, ?, ?, ?)",
                        (name, email, phone_number, generate_password_hash(password), "coordinator"),
                    )
                    user_id = db.execute(
                        "SELECT id FROM users WHERE email = ?",
                        (email,),
                    ).fetchone()["id"]
                db.commit()
                session.clear()
                session["user_id"] = user_id
                flash("Coordinator account created. You now have full admin access.", "success")
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

            if not all([name, email, phone_number, password]):
                flash("Complete every sign-up field.", "error")
            else:
                db = get_db()
                existing_user = db.execute(
                    "SELECT id FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                existing_phone = db.execute(
                    "SELECT id FROM users WHERE phone_number = ?",
                    (phone_number,),
                ).fetchone()
                if existing_user is not None:
                    flash("An account already exists for that email.", "error")
                elif existing_phone is not None:
                    flash("That phone number is already linked to another account.", "error")
                else:
                    db.execute(
                        "INSERT INTO users (name, email, phone_number, password_hash, role) VALUES (?, ?, ?, ?, ?)",
                        (name, email, phone_number, generate_password_hash(password), "participant"),
                    )
                    db.commit()
                    user = db.execute(
                        "SELECT id FROM users WHERE email = ?",
                        (email,),
                    ).fetchone()
                    session.clear()
                    session["user_id"] = user["id"]
                    flash("Account created. Your workspace is ready.", "success")
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
            "team_count": db.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
            "event_count": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "resource_count": db.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
            "upcoming_event_count": db.execute(
                """
                SELECT COUNT(*)
                FROM events
                WHERE COALESCE("date", event_date) >= ?
                """,
                (date.today().isoformat(),),
            ).fetchone()[0],
            "hosted_event_count": db.execute(
                """
                SELECT COUNT(*)
                FROM events
                WHERE status = 'Completed' OR COALESCE("date", event_date) < ?
                """,
                (date.today().isoformat(),),
            ).fetchone()[0],
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

        if request.method == "POST":
            if registration_closed:
                flash("registration is closed", "error")
            elif registration_is_full:
                flash("registration for the event is completed", "error")
            elif not registration_fields and not contact_email_required:
                flash("No registration fields are configured for this event.", "error")
            else:
                answers = {}
                has_error = False

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
                    db.execute(
                        """
                        INSERT INTO event_registrations (event_id, response_json)
                        VALUES (?, ?)
                        """,
                        (event_id, json.dumps(answers)),
                    )
                    db.commit()
                    flash("Event registration submitted successfully.", "success")
                    return redirect(url_for("event_detail", event_id=event_id))

        return render_template(
            "event_detail.html",
            event=event,
            registration_fields=registration_fields,
            contact_email_required=contact_email_required,
            registration_closed=registration_closed,
            registration_is_full=registration_is_full,
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
            WHERE role IN ('coordinator', 'admin')
            ORDER BY created_at ASC
            """
        ).fetchall()
        team_members = [
            {
                "name": row["name"],
                "role": "Coordinator" if row["role"] == "coordinator" else "Admin",
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
                "SELECT id, name, email, password_hash, role FROM users WHERE email = ?",
                (email,),
            ).fetchone()

            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Incorrect email or password.", "error")
            else:
                if admin_only and not is_admin(user):
                    flash("This login page is reserved for coordinators.", "error")
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
        return redirect(url_for("home"))

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
            flash("Team registration is currently closed by the coordinator.", "error")
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

        return render_template("register.html")

    @app.route("/submit", methods=("GET", "POST"))
    def submit_prototype():
        flash("Project submission is not enabled for this website.", "error")
        return redirect(url_for("home"))

    def build_event_registration_list_context(event_id: int) -> dict[str, object]:
        db = get_db()
        event_row = db.execute(
            """
            SELECT events.id, events.title, COALESCE(events.description, events.summary) AS description,
                   COALESCE(events."date", events.event_date) AS event_date,
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

        return {
            "event": event,
            "available_fields": available_fields,
            "selected_fields": selected_fields,
            "selected_field_ids": selected_field_ids,
            "participants": participants,
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

        header = ["S.No", "Submitted At"] + [field["field_label"] for field in selected_fields] + ["Attendance", "Signature"]
        data = [header]
        for participant in participants:
            row = [participant["serial"], participant["created_at"]]
            for field in selected_fields:
                row.append(participant["answers"].get(str(field["id"]), ""))
            row += ["", ""]
            data.append(row)

        if len(data) == 1:
            data.append(["-", "-", "No registrations yet"] + [""] * max(len(selected_fields) + 1, 0))

        available_width = landscape(A4)[0] - doc.leftMargin - doc.rightMargin
        serial_width = 34
        submitted_width = 110
        attendance_width = 72
        signature_width = 96
        remaining_width = max(available_width - serial_width - submitted_width - attendance_width - signature_width, 180)
        dynamic_count = max(len(selected_fields), 1)
        dynamic_width = remaining_width / dynamic_count
        col_widths = [serial_width, submitted_width] + [dynamic_width] * len(selected_fields) + [attendance_width, signature_width]

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
                location = request.form.get("location", "").strip()
                category = request.form.get("category", "").strip() or "Club Event"
                registration_link = request.form.get("registration_link", "").strip()
                registration_limit_raw = request.form.get("registration_limit", "").strip() or "0"
                registration_enabled = 1 if request.form.get("registration_enabled") == "on" else 0
                contact_email_required = 1 if request.form.get("contact_email_required") == "on" else 0
                raw_field_lines = request.form.get("registration_fields", "")
                field_definitions, field_error = parse_registration_field_definitions(raw_field_lines)

                if not all([title, description, event_date, location]):
                    flash("Provide title, description, date, and location for the event.", "error")
                elif field_error is not None:
                    flash(field_error, "error")
                else:
                    try:
                        registration_limit = int(registration_limit_raw)
                    except ValueError:
                        registration_limit = -1

                    if registration_limit < 0:
                        flash("Registration limit must be a valid integer.", "error")
                    elif not field_definitions and not contact_email_required:
                        flash("Add at least one registration field or enable participant email collection.", "error")
                    else:
                        cursor = db.execute(
                            """
                            INSERT INTO events (
                                title, event_date, format, venue, category, seats, status, summary,
                                description, "date", location, registration_link, registration_enabled, registration_limit, contact_email_required
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        "UPDATE users SET role = 'coordinator' WHERE id = ?",
                        (user_id,),
                    )
                    db.commit()
                    flash("User promoted to coordinator.", "success")

            elif action == "demote_user":
                user_id = request.form.get("user_id", "").strip()
                target_user = db.execute(
                    "SELECT id, role FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if target_user is None:
                    flash("User not found.", "error")
                elif target_user["id"] == user["id"]:
                    flash("You cannot remove your own coordinator access from this dashboard.", "error")
                else:
                    db.execute(
                        "UPDATE users SET role = 'participant' WHERE id = ?",
                        (user_id,),
                    )
                    db.commit()
                    flash("Coordinator access removed.", "success")

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
                    flash("Event registration state updated.", "success")

            elif action == "delete_event":
                event_id = request.form.get("event_id", "").strip()
                db.execute("DELETE FROM event_registrations WHERE event_id = ?", (event_id,))
                db.execute("DELETE FROM event_registration_fields WHERE event_id = ?", (event_id,))
                db.execute("DELETE FROM events WHERE id = ?", (event_id,))
                db.commit()
                flash("Event removed.", "success")

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
            ORDER BY CASE role WHEN 'coordinator' THEN 0 WHEN 'admin' THEN 0 WHEN 'participant' THEN 1 WHEN 'member' THEN 1 ELSE 2 END, created_at ASC
            """
        ).fetchall()
        settings = get_site_settings()
        stats = {
            "teams": db.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
            "events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "resources": db.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
            "core_members": db.execute("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'coordinator')").fetchone()[0],
        }
        return render_template(
            "admin.html",
            recent_teams=recent_teams,
            managed_events=managed_events,
            managed_resources=managed_resources,
            users=users,
            settings=settings,
            stats=stats,
        )

    return app


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
            "SELECT id, name, email, role FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return g.current_user


def is_admin(user: sqlite3.Row | None) -> bool:
    return user is not None and user["role"] in ADMIN_ROLES


def is_participant(user: sqlite3.Row | None) -> bool:
    return user is not None and user["role"] in PARTICIPANT_ROLES


def has_admin_account() -> bool:
    row = get_db().execute(
        "SELECT COUNT(*) FROM users WHERE role IN (?, ?)",
        ("admin", "coordinator"),
    ).fetchone()
    return bool(row[0])


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        user = get_current_user()
        if not is_participant(user):
            flash("Sign in as a participant to open this page.", "error")
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        user = get_current_user()
        if user is None:
            flash("Sign in with the coordinator account to open the admin dashboard.", "error")
            return redirect(url_for("admin_login"))
        if not is_admin(user):
            flash("Admin access is required for this page.", "error")
            return redirect(url_for("profile"))
        return view(**kwargs)

    return wrapped_view


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


def ensure_column(table: str, column: str, definition: str) -> None:
    db = get_db()
    existing_columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing_columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()


def get_site_settings() -> sqlite3.Row:
    db = get_db()
    settings = db.execute(
        """
        SELECT id, registration_open, submission_open, participant_login_required
        FROM site_settings
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    if settings is None:
        db.execute(
            """
            INSERT INTO site_settings (registration_open, submission_open, participant_login_required)
            VALUES (1, 1, 1)
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

    event["remaining_slots"] = remaining_slots
    event["registration_progress"] = registration_progress
    event["registration_status_label"] = registration_status_label
    event["registration_is_full"] = registration_limit > 0 and registration_count >= registration_limit
    event["registration_is_open"] = registration_enabled and not event["registration_is_full"]
    return event


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
            participant_login_required INTEGER NOT NULL DEFAULT 1
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
        """
    )
    db.commit()

    ensure_column("events", "description", "TEXT")
    ensure_column("events", "date", "TEXT")
    ensure_column("events", "location", "TEXT")
    ensure_column("events", "registration_link", "TEXT")
    ensure_column("resources", "category", "TEXT DEFAULT 'Development'")
    ensure_column("users", "phone_number", "TEXT")
    ensure_column("events", "registration_enabled", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("events", "registration_limit", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("events", "contact_email_required", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("event_registration_fields", "field_options", "TEXT NOT NULL DEFAULT ''")

    db.execute('UPDATE events SET description = COALESCE(description, summary)')
    db.execute('UPDATE events SET "date" = COALESCE("date", event_date)')
    db.execute('UPDATE events SET location = COALESCE(location, venue)')
    db.execute('UPDATE events SET registration_link = COALESCE(registration_link, "")')
    db.execute('UPDATE events SET registration_enabled = COALESCE(registration_enabled, 0)')
    db.execute('UPDATE events SET registration_limit = COALESCE(registration_limit, 0)')
    db.execute('UPDATE events SET contact_email_required = COALESCE(contact_email_required, 0)')
    db.execute("UPDATE event_registration_fields SET field_options = COALESCE(field_options, '')")
    db.execute('UPDATE resources SET category = COALESCE(category, "")')
    db.execute("UPDATE users SET role = 'participant' WHERE role = 'member'")
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

    get_site_settings()




app = create_app()


if __name__ == "__main__":
    app.run(debug=True)















