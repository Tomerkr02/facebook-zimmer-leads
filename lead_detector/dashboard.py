from flask import Flask, abort, redirect, render_template, request, url_for

from config import load_settings
from storage import ALLOWED_LEAD_STATUSES, LeadStorage


settings = load_settings()
storage = LeadStorage(settings.database_path)
app = Flask(__name__)
app.logger.info("Database path: %s", settings.database_path)
app.logger.info("Current leads count: %s", storage.count_leads())


@app.template_filter("nl2br")
def nl2br(value: str | None) -> str:
    return (value or "").replace("\n", "<br>")


@app.route("/")
def home():
    return redirect(url_for("leads_page"))


@app.route("/leads")
def leads_page():
    status = request.args.get("status") or None
    heat_level = request.args.get("heat_level") or None
    guest_type = request.args.get("guest_type") or None
    urgency = request.args.get("urgency") or None
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit, 500))
    leads = storage.list_leads(
        status=status,
        limit=limit,
        heat_level=heat_level,
        guest_type=guest_type,
        urgency=urgency,
    )
    return render_template(
        "leads.html",
        leads=leads,
        selected_status=status or "",
        selected_heat_level=heat_level or "",
        selected_guest_type=guest_type or "",
        selected_urgency=urgency or "",
        statuses=sorted(ALLOWED_LEAD_STATUSES),
        heat_levels=["hot", "warm", "cold", "reject"],
        guest_types=["couple", "couple_with_kids", "small_family", "large_group", "unknown"],
        urgencies=["today", "tomorrow", "weekend", "shabbat", "date_specific", "flexible", "unknown"],
        limit=limit,
        stats=storage.summary_stats(),
    )


@app.route("/debug/db")
def debug_db():
    latest = storage.latest_leads(limit=5)
    return {
        "database_path": str(settings.database_path),
        "total_leads": storage.count_leads(),
        "latest_5_leads": latest,
        "table_names": storage.list_table_names(),
    }


@app.route("/leads/<int:lead_id>")
def lead_detail(lead_id: int):
    lead = storage.get_lead(lead_id)
    if not lead:
        abort(404)
    return render_template(
        "lead_detail.html",
        lead=lead,
        statuses=sorted(ALLOWED_LEAD_STATUSES),
    )


@app.post("/leads/<int:lead_id>/status")
def lead_status_update(lead_id: int):
    status = request.form.get("status", "").strip()
    if status not in ALLOWED_LEAD_STATUSES:
        abort(400)
    storage.update_lead_status(lead_id, status)
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.post("/leads/<int:lead_id>/notes")
def lead_notes_update(lead_id: int):
    notes = request.form.get("notes", "")
    storage.update_lead_notes(lead_id, notes)
    return redirect(url_for("lead_detail", lead_id=lead_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
