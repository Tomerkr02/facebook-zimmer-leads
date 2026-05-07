import threading
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from config import load_settings
from scraper import ScanOptions, run_scan
from storage import ALLOWED_LEAD_STATUSES, LeadStorage, utc_now_iso


settings = load_settings()
storage = LeadStorage(settings.resolved_database_path)
app = Flask(__name__)
app.logger.info("DB_PATH | %s", settings.resolved_database_path)
app.logger.info("Current leads count: %s", storage.count_leads())

HEAT_LEVELS = ["hot", "warm", "cold", "reject"]
GUEST_TYPES = ["couple", "couple_with_kids", "small_family", "large_group", "unknown"]
URGENCIES = ["today", "tomorrow", "weekend", "shabbat", "date_specific", "flexible", "unknown"]
SORT_OPTIONS: dict[str, str] = {
    "newest": "החדשים ביותר",
    "fit_score": "ציון התאמה גבוה",
    "hottest": "הכי חמים",
    "urgency": "הכי דחופים",
}

STATUS_LABELS = {
    "new": "חדש",
    "contacted": "נוצר קשר",
    "waiting_reply": "ממתין לתגובה",
    "closed": "נסגר",
    "not_relevant": "לא רלוונטי",
    "archived": "ארכיון",
}

HEAT_LABELS = {"hot": "hot", "warm": "warm", "cold": "cold", "reject": "reject"}
HEAT_ICONS = {"hot": "🔥", "warm": "🟡", "cold": "❄️", "reject": "⛔"}
GUEST_TYPE_LABELS = {
    "couple": "זוג",
    "couple_with_kids": "זוג עם ילדים",
    "small_family": "משפחה קטנה",
    "large_group": "קבוצה גדולה",
    "unknown": "לא ידוע",
}
URGENCY_LABELS = {
    "today": "להיום",
    "tomorrow": "למחר",
    "weekend": "לסופ\"ש",
    "shabbat": "לשבת",
    "date_specific": "תאריך ספציפי",
    "flexible": "גמיש",
    "unknown": "לא ידוע",
}
AREA_LABELS = {
    "center": "מרכז",
    "rehovot_area": "אזור רחובות / קריית עקרון",
    "tel_aviv_area": "אזור תל אביב",
    "jerusalem_area": "אזור ירושלים",
    "north": "צפון",
    "south": "דרום",
    "eilat": "אילת",
    "unknown": "לא ידוע",
}
POOL_LABELS = {
    "private_pool": "בריכה פרטית",
    "pool_general": "בריכה כללית",
    "no_pool": "ללא בריכה",
    "unknown": "לא ידוע",
}

SCAN_CONTROLLER: dict[str, Any] = {
    "lock": threading.Lock(),
    "thread": None,
    "stop_event": None,
    "scan_run_id": None,
}


def _label(value: str | None, mapping: Mapping[str, str], default: str = "-") -> str:
    if not value:
        return default
    return str(mapping.get(value, value))


def _groups_total() -> int:
    urls = settings.facebook_group_urls
    if settings.group_scan_limit > 0:
        urls = urls[: settings.group_scan_limit]
    return len(urls)


def _scan_mode_label(mode: str | None) -> str:
    return {
        "normal": "סריקה רגילה",
        "loose": "סריקה רכה",
        "debug": "סריקת דיבוג",
    }.get(mode or "", mode or "-")


def _serialize_scan(scan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not scan:
        return None
    total_groups = int(scan.get("total_groups") or 0)
    groups_done = int(scan.get("groups_done") or 0)
    progress_percent = int((groups_done / total_groups) * 100) if total_groups else 0
    log_lines = [line for line in str(scan.get("log_text") or "").splitlines() if line.strip()]
    result = dict(scan)
    result["progress_percent"] = progress_percent
    result["latest_log_lines"] = log_lines[-12:]
    result["mode_label"] = _scan_mode_label(scan.get("mode"))
    return result


def _active_scan() -> dict[str, Any] | None:
    running = storage.get_running_scan_run()
    if running and _is_scan_running():
        return _serialize_scan(running)
    return _serialize_scan(storage.latest_scan_run())


def _is_scan_running() -> bool:
    thread = SCAN_CONTROLLER.get("thread")
    return bool(thread and thread.is_alive())


def _update_scan_from_event(scan_run_id: int, event: dict[str, Any]) -> None:
    event_type = event.get("event")
    log_line = event.get("log_line")
    if log_line:
        storage.append_scan_log(scan_run_id, log_line)

    if event_type == "scan_started":
        storage.update_scan_run(
            scan_run_id,
            status="running",
            mode=event.get("mode", "normal"),
            total_groups=event.get("total_groups", 0),
        )
        return

    if event_type == "group_started":
        storage.save_scan_group_result(
            scan_run_id,
            group_url=event.get("current_group") or "",
            group_name=None,
            status="running",
            cards_found=0,
            extracted=0,
            cleaned=0,
            matched=0,
            saved=0,
            telegram_sent=0,
            failure_reason=None,
        )
        return

    if event_type == "group_completed":
        group_stats = event.get("group_stats") or {}
        failure_reason = group_stats.get("failure_reason")
        group_status = "completed"
        if failure_reason == "stopped":
            group_status = "stopped"
        elif failure_reason:
            group_status = "failed"
        storage.save_scan_group_result(
            scan_run_id,
            group_url=group_stats.get("group_url") or event.get("current_group") or "",
            group_name=group_stats.get("group_name"),
            status=group_status,
            cards_found=int(group_stats.get("total_dom_cards_found") or 0),
            extracted=int(group_stats.get("total_text_blocks_extracted") or 0),
            cleaned=int(group_stats.get("total_cleaned_posts") or 0),
            matched=int(group_stats.get("matched") or 0),
            saved=int(group_stats.get("posts_saved_to_leads") or 0),
            telegram_sent=int(group_stats.get("alerts_sent") or 0),
            failure_reason=failure_reason,
        )
        return

    if event_type in {"scan_completed", "scan_stopped"}:
        counters = event.get("counters") or {}
        storage.update_scan_run(
            scan_run_id,
            status="completed" if event_type == "scan_completed" else "stopped",
            finished_at=utc_now_iso(),
            posts_extracted=int(counters.get("posts_extracted") or 0),
            posts_matched=int(counters.get("posts_matched") or 0),
            leads_saved=int(counters.get("leads_saved") or 0),
            telegram_sent=int(counters.get("telegram_sent") or 0),
        )


def _start_scan_worker(scan_run_id: int, options: ScanOptions) -> None:
    stop_event = SCAN_CONTROLLER["stop_event"]

    def callback(event: dict[str, Any]) -> None:
        _update_scan_from_event(scan_run_id, event)

    try:
        run_scan(options, progress_callback=callback, stop_requested=stop_event.is_set)
    except Exception as exc:  # noqa: BLE001
        storage.append_scan_log(scan_run_id, f"Scan failed: {exc}")
        storage.update_scan_run(
            scan_run_id,
            status="failed",
            finished_at=utc_now_iso(),
            error_text="".join(traceback.format_exception_only(type(exc), exc)).strip(),
        )
    finally:
        with SCAN_CONTROLLER["lock"]:
            SCAN_CONTROLLER["thread"] = None
            SCAN_CONTROLLER["stop_event"] = None
            SCAN_CONTROLLER["scan_run_id"] = None


def _start_scan(mode: str) -> tuple[bool, str]:
    with SCAN_CONTROLLER["lock"]:
        if _is_scan_running():
            return False, "כבר רצה סריקה אחת. אפשר לעצור אותה או להמתין לסיום."

        total_groups = _groups_total()
        scan_run_id = storage.create_scan_run(mode=mode, total_groups=total_groups)
        stop_event = threading.Event()
        if mode == "debug":
            options = ScanOptions(rescan=True, debug_scan=True, loose=False, save_debug_leads=False, send_telegram=False)
        elif mode == "loose":
            options = ScanOptions(rescan=True, loose=True, save_debug_leads=True, send_telegram=False)
        else:
            options = ScanOptions()

        thread = threading.Thread(
            target=_start_scan_worker,
            args=(scan_run_id, options),
            daemon=True,
            name=f"scan-run-{scan_run_id}",
        )
        SCAN_CONTROLLER["thread"] = thread
        SCAN_CONTROLLER["stop_event"] = stop_event
        SCAN_CONTROLLER["scan_run_id"] = scan_run_id
        thread.start()
    return True, "הסריקה התחילה."


def _filters_from_request() -> dict[str, str | int]:
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit, 500))
    return {
        "status": (request.args.get("status") or "").strip(),
        "heat_level": (request.args.get("heat_level") or "").strip(),
        "guest_type": (request.args.get("guest_type") or "").strip(),
        "urgency": (request.args.get("urgency") or "").strip(),
        "requested_area": (request.args.get("requested_area") or "").strip(),
        "ai_category": (request.args.get("ai_category") or "").strip(),
        "search": (request.args.get("search") or "").strip(),
        "sort": (request.args.get("sort") or "newest").strip(),
        "limit": limit,
    }


def _dashboard_context() -> dict[str, object]:
    latest_scan = _active_scan()
    return {
        "stats": storage.summary_stats(),
        "insights": storage.insights(),
        "status_counts": storage.status_counts(),
        "filter_options": storage.filter_options(),
        "statuses": sorted(ALLOWED_LEAD_STATUSES),
        "status_labels": STATUS_LABELS,
        "heat_levels": HEAT_LEVELS,
        "heat_labels": HEAT_LABELS,
        "heat_icons": HEAT_ICONS,
        "guest_types": GUEST_TYPES,
        "guest_type_labels": GUEST_TYPE_LABELS,
        "urgencies": URGENCIES,
        "urgency_labels": URGENCY_LABELS,
        "area_labels": AREA_LABELS,
        "pool_labels": POOL_LABELS,
        "sort_options": SORT_OPTIONS,
        "latest_scan": latest_scan,
        "scan_running": _is_scan_running(),
    }


@app.template_filter("nl2br")
def nl2br(value: str | None) -> str:
    return (value or "").replace("\n", "<br>")


@app.template_filter("status_label")
def status_label(value: str | None) -> str:
    return _label(value, STATUS_LABELS)


@app.template_filter("heat_label")
def heat_label(value: str | None) -> str:
    return _label(value, HEAT_LABELS)


@app.template_filter("heat_icon")
def heat_icon(value: str | None) -> str:
    return HEAT_ICONS.get(value or "", "❄️")


@app.template_filter("guest_type_label")
def guest_type_label(value: str | None) -> str:
    return _label(value, GUEST_TYPE_LABELS)


@app.template_filter("urgency_label")
def urgency_label(value: str | None) -> str:
    return _label(value, URGENCY_LABELS)


@app.template_filter("area_label")
def area_label(value: str | None) -> str:
    return _label(value, AREA_LABELS)


@app.template_filter("pool_label")
def pool_label(value: str | None) -> str:
    return _label(value, POOL_LABELS)


@app.route("/")
def home():
    return redirect(url_for("leads_page"))


@app.route("/leads")
def leads_page():
    filters = _filters_from_request()
    leads = storage.list_leads(
        status=filters["status"] or None,
        limit=int(filters["limit"]),
        heat_level=filters["heat_level"] or None,
        guest_type=filters["guest_type"] or None,
        urgency=filters["urgency"] or None,
        requested_area=filters["requested_area"] or None,
        ai_category=filters["ai_category"] or None,
        search=filters["search"] or None,
        sort_by=str(filters["sort"] or "newest"),
    )
    return render_template("leads.html", leads=leads, filters=filters, **_dashboard_context())


@app.route("/scans")
def scans_page():
    return render_template("scans.html", **_dashboard_context())


@app.route("/insights")
def insights_page():
    return render_template("insights.html", groups=storage.group_performance(), **_dashboard_context())


@app.route("/groups")
def groups_page():
    return render_template("groups.html", groups=storage.group_performance(), **_dashboard_context())


@app.route("/debug/db")
def debug_db():
    return storage.debug_snapshot(limit=10)


@app.route("/scans/status")
def scan_status():
    return jsonify(
        {
            "running": _is_scan_running(),
            "scan": _active_scan(),
        }
    )


@app.post("/scans/start")
def scans_start():
    ok, message = _start_scan("normal")
    return jsonify({"ok": ok, "message": message, "scan": _active_scan()})


@app.post("/scans/start-loose")
def scans_start_loose():
    ok, message = _start_scan("loose")
    return jsonify({"ok": ok, "message": message, "scan": _active_scan()})


@app.post("/scans/start-debug")
def scans_start_debug():
    ok, message = _start_scan("debug")
    return jsonify({"ok": ok, "message": message, "scan": _active_scan()})


@app.post("/scans/stop")
def scans_stop():
    with SCAN_CONTROLLER["lock"]:
        if not _is_scan_running() or not SCAN_CONTROLLER.get("stop_event"):
            return jsonify({"ok": False, "message": "אין סריקה פעילה לעצירה."})
        SCAN_CONTROLLER["stop_event"].set()
    return jsonify({"ok": True, "message": "נשלחה בקשת עצירה לסריקה הפעילה."})


@app.route("/leads/<int:lead_id>")
def lead_detail(lead_id: int):
    lead = storage.get_lead(lead_id)
    if not lead:
        abort(404)
    return render_template(
        "lead_detail.html",
        lead=lead,
        events=storage.list_lead_events(lead_id),
        **_dashboard_context(),
    )


@app.post("/leads/<int:lead_id>/status")
def lead_status_update(lead_id: int):
    status = request.form.get("status", "").strip()
    if status not in ALLOWED_LEAD_STATUSES:
        abort(400)
    storage.update_lead_status(lead_id, status)
    next_url = request.form.get("next", "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.post("/leads/<int:lead_id>/notes")
def lead_notes_update(lead_id: int):
    notes = request.form.get("notes", "")
    storage.update_lead_notes(lead_id, notes)
    return redirect(url_for("lead_detail", lead_id=lead_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
