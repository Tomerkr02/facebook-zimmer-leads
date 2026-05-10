import threading
import traceback
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
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

HEAT_LEVELS = ["ultra_hot", "hot", "warm", "cold", "reject"]
GUEST_TYPES = ["couple", "religious_couple", "romantic_couple", "couple_with_kids", "small_family", "large_group", "unknown"]
URGENCIES = ["today", "tomorrow", "weekend", "shabbat", "date_specific", "flexible", "unknown"]
SORT_OPTIONS: dict[str, str] = {
    "priority": "Priority inbox",
    "newest": "החדשים ביותר",
    "fit_score": "ציון התאמה גבוה",
    "conversion_score": "פוטנציאל סגירה",
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

HEAT_LABELS = {"ultra_hot": "ULTRA HOT", "hot": "HOT", "warm": "WARM", "cold": "COLD", "reject": "REJECT"}
HEAT_ICONS = {"ultra_hot": "🔥", "hot": "🔥", "warm": "🟡", "cold": "❄️", "reject": "⛔"}
GUEST_TYPE_LABELS = {
    "couple": "זוג",
    "religious_couple": "זוג דתי",
    "romantic_couple": "זוג רומנטי",
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
    "mixed_center_jerusalem": "מרכז / ירושלים",
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
FEEDBACK_LABELS = {
    "good_lead": "ליד טוב",
    "bad_lead": "ליד חלש",
    "closed_successfully": "נסגר בהצלחה",
    "irrelevant": "לא רלוונטי",
    "too_expensive": "יקר מדי",
    "too_large": "גדול מדי",
    "pets": "בקשת חיות מחמד",
    "bad_location": "מיקום לא מתאים",
    "spam": "ספאם",
    "owner_ad": "פרסום בעל מקום",
}
LEAD_TYPE_LABELS = {
    "guest_seeker": "מחפש אירוח",
    "owner_advertiser": "פרסום בעל מקום",
    "spam": "ספאם",
    "event_seeker": "מחפש אירוע",
    "romantic_couple": "זוג רומנטי",
    "religious_couple": "זוג דתי",
    "family_small": "משפחה קטנה",
    "budget_sensitive": "רגיש למחיר",
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


def _decorate_leads(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decorated: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    threshold = timedelta(hours=settings.followup_after_hours)
    for lead in leads:
        item = dict(lead)
        last_contacted_raw = item.get("last_contacted_at")
        followup_recommended = False
        if item.get("status") == "contacted" and last_contacted_raw:
            try:
                last_contacted_at = datetime.fromisoformat(str(last_contacted_raw).replace("Z", "+00:00"))
                followup_recommended = now - last_contacted_at >= threshold
            except ValueError:
                followup_recommended = False
        item["followup_recommended"] = followup_recommended
        item["perfect_match"] = bool(item.get("vip_match"))
        decorated.append(item)
    return decorated


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
            options = ScanOptions(rescan=True, debug_scan=True, loose=False, save_debug_leads=False, send_telegram=False, scan_run_id=scan_run_id)
        elif mode == "loose":
            options = ScanOptions(rescan=True, loose=True, save_debug_leads=True, send_telegram=False, scan_run_id=scan_run_id)
        else:
            options = ScanOptions(scan_run_id=scan_run_id)

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
    status = (request.args.get("status") or "").strip()
    heat_level = (request.args.get("heat_level") or request.args.get("heat") or "").strip()
    requested_type = (request.args.get("type") or "").strip()
    created_date = "today" if (request.args.get("date") or "").strip() == "today" else ""
    view = (request.args.get("view") or "").strip()
    return {
        "status": status,
        "heat_level": heat_level,
        "guest_type": (request.args.get("guest_type") or "").strip(),
        "urgency": (request.args.get("urgency") or "").strip(),
        "requested_area": (request.args.get("requested_area") or "").strip(),
        "ai_category": (request.args.get("ai_category") or "").strip(),
        "lead_type": (request.args.get("lead_type") or "").strip(),
        "type": requested_type,
        "view": view,
        "created_date": created_date,
        "religious_only": "1" if request.args.get("religious_only") else "",
        "romantic_only": "1" if request.args.get("romantic_only") else "",
        "family_only": "1" if request.args.get("family_only") else "",
        "owner_ads_only": "1" if request.args.get("owner_ads_only") else "",
        "rejected_only": "1" if request.args.get("rejected_only") else "",
        "budget_sensitive_only": "1" if request.args.get("budget_sensitive_only") else "",
        "hide_rejected": "1" if request.args.get("hide_rejected") else "",
        "include_archived": "1" if request.args.get("include_archived") else "",
        "include_rejected": "1" if request.args.get("include_rejected") else "",
        "telegram_sent": "1" if request.args.get("telegram_sent") else "",
        "show_all": "1" if request.args.get("show_all") or view == "all_active" else "",
        "scan_run_id": request.args.get("scan_run_id", default=None, type=int),
        "search": (request.args.get("search") or "").strip(),
        "sort": (request.args.get("sort") or "newest").strip(),
        "limit": limit,
    }


def _lead_query_kwargs(
    filters: Mapping[str, str | int],
    *,
    force_show_all: bool = False,
    force_include_archived: bool = False,
    force_include_rejected: bool = False,
    force_include_owner_ads: bool = False,
    force_status: str | None = None,
    force_rejected_only: bool = False,
    force_owner_ads_only: bool = False,
) -> dict[str, Any]:
    show_all = force_show_all or bool(filters.get("show_all"))
    return {
        "status": force_status if force_status is not None else (filters.get("status") or None),
        "heat_level": filters.get("heat_level") or None,
        "guest_type": filters.get("guest_type") or None,
        "urgency": filters.get("urgency") or None,
        "requested_area": filters.get("requested_area") or None,
        "ai_category": filters.get("ai_category") or None,
        "lead_type": filters.get("lead_type") or None,
        "religious_only": bool(filters.get("religious_only")),
        "romantic_only": bool(filters.get("romantic_only")),
        "family_only": bool(filters.get("family_only")),
        "owner_ads_only": force_owner_ads_only or bool(filters.get("owner_ads_only")),
        "rejected_only": force_rejected_only or bool(filters.get("rejected_only")),
        "budget_sensitive_only": bool(filters.get("budget_sensitive_only")),
        "hide_rejected": bool(filters.get("hide_rejected")),
        "include_archived": force_include_archived or bool(filters.get("include_archived")),
        "include_rejected": force_include_rejected or bool(filters.get("include_rejected")),
        "include_owner_ads": force_include_owner_ads or show_all or bool(filters.get("owner_ads_only")),
        "telegram_sent": True if filters.get("telegram_sent") else None,
        "show_all": show_all,
        "scan_run_id": filters.get("scan_run_id"),
        "created_date": filters.get("created_date") or None,
        "search": filters.get("search") or None,
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
        "feedback_labels": FEEDBACK_LABELS,
        "lead_type_labels": LEAD_TYPE_LABELS,
        "lead_types": ["guest_seeker", "owner_advertiser", "spam", "event_seeker", "romantic_couple", "religious_couple", "family_small", "budget_sensitive"],
        "sort_options": SORT_OPTIONS,
        "latest_scan": latest_scan,
        "scan_running": _is_scan_running(),
    }


def _scan_detail_context(scan_run_id: int) -> dict[str, Any]:
    scan = storage.get_scan_run(scan_run_id)
    if not scan:
        abort(404)
    return {
        "scan": _serialize_scan(scan) or scan,
        "back_url": url_for("scans_page"),
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


@app.template_filter("lead_type_label")
def lead_type_label(value: str | None) -> str:
    return _label(value, LEAD_TYPE_LABELS)


@app.route("/")
def home():
    return redirect(url_for("leads_page"))


@app.route("/leads/all")
def leads_all_page():
    return redirect(url_for("leads_page", show_all=1))


@app.route("/leads")
def leads_page():
    filters = _filters_from_request()
    if filters["type"] == "owner_ad":
        filters["owner_ads_only"] = "1"
    filters["show_all"] = "1"
    query_kwargs = _lead_query_kwargs(filters, force_show_all=True)
    total_leads_all = storage.count_leads()
    total_non_archived = storage.count_non_archived_leads()
    filtered_count = storage.count_filtered_leads(**query_kwargs)
    app.logger.info(
        "LEADS_QUERY | total_all=%s | total_non_archived=%s | total_after_filters=%s | current_sort=%s | scan_run_id=%s | telegram_sent=%s | status=%s | show_all=%s | active_filters=%s",
        total_leads_all,
        total_non_archived,
        filtered_count,
        filters["sort"] or "newest",
        filters["scan_run_id"],
        bool(filters["telegram_sent"]),
        filters["status"] or "-",
        bool(filters["show_all"]),
        {key: value for key, value in filters.items() if value not in {"", None, 0}},
    )
    leads = _decorate_leads(
        storage.list_leads(
            limit=int(filters["limit"]),
            sort_by=str(filters["sort"] or "newest"),
            **query_kwargs,
        )
    )
    filters_hide_results = filtered_count == 0 and total_non_archived > 0
    return render_template(
        "leads.html",
        leads=leads,
        filters=filters,
        total_before_filters=total_non_archived,
        total_active_leads=total_non_archived,
        total_non_archived=total_non_archived,
        total_leads_all=total_leads_all,
        filtered_count=filtered_count,
        filters_hide_results=filters_hide_results,
        **_dashboard_context(),
    )


@app.route("/archived")
def archived_page():
    filters = _filters_from_request()
    filters["include_archived"] = "1"
    filters["status"] = "archived"
    filtered_count = storage.count_filtered_leads(status="archived", include_archived=True, include_owner_ads=True, show_all=True)
    leads = _decorate_leads(storage.list_leads(status="archived", limit=int(filters["limit"]), include_archived=True, include_owner_ads=True, show_all=True, sort_by=str(filters["sort"] or "newest")))
    return render_template("leads.html", leads=leads, filters=filters, page_title="לידים בארכיון", total_before_filters=filtered_count, total_active_leads=filtered_count, total_non_archived=storage.count_non_archived_leads(), total_leads_all=storage.count_leads(), filtered_count=filtered_count, filters_hide_results=False, **_dashboard_context())


@app.route("/rejected")
def rejected_page():
    filters = _filters_from_request()
    filters["include_rejected"] = "1"
    if filters["type"] == "owner_ad":
        filters["owner_ads_only"] = "1"
        filters["rejected_only"] = ""
    else:
        filters["rejected_only"] = "1"
    query_kwargs = _lead_query_kwargs(
        filters,
        force_show_all=True,
        force_include_rejected=True,
        force_include_owner_ads=True,
        force_rejected_only=not bool(filters["owner_ads_only"]),
        force_owner_ads_only=bool(filters["owner_ads_only"]),
    )
    filtered_count = storage.count_filtered_leads(**query_kwargs)
    leads = _decorate_leads(storage.list_leads(limit=int(filters["limit"]), sort_by=str(filters["sort"] or "newest"), **query_kwargs))
    return render_template("leads.html", leads=leads, filters=filters, page_title="לידים שנדחו", total_before_filters=filtered_count, total_active_leads=filtered_count, total_non_archived=storage.count_non_archived_leads(), total_leads_all=storage.count_leads(), filtered_count=filtered_count, filters_hide_results=False, **_dashboard_context())


@app.route("/review")
def review_page():
    leads = _decorate_leads(storage.list_review_leads(limit=120))
    return render_template("review.html", leads=leads, **_dashboard_context())


@app.route("/scans")
def scans_page():
    return render_template("scans.html", **_dashboard_context())


@app.route("/scan/<int:scan_run_id>")
def scan_detail_page(scan_run_id: int):
    return render_template("scan_detail.html", **_scan_detail_context(scan_run_id), **_dashboard_context())


@app.route("/scan/<int:scan_run_id>/leads")
def scan_leads_page(scan_run_id: int):
    scan = storage.get_scan_run(scan_run_id)
    if not scan:
        abort(404)
    leads = _decorate_leads(
        storage.list_leads(
            limit=500,
            include_archived=True,
            include_rejected=True,
            include_owner_ads=True,
            scan_run_id=scan_run_id,
            sort_by="newest",
        )
    )
    return render_template("scan_leads.html", scan=_serialize_scan(scan) or scan, leads=leads, back_url=url_for("scans_page"), **_dashboard_context())


@app.route("/scan/<int:scan_run_id>/matches")
def scan_matches_page(scan_run_id: int):
    scan = storage.get_scan_run(scan_run_id)
    if not scan:
        abort(404)
    group_url = (request.args.get("group") or "").strip() or None
    matches = storage.list_scan_matches(scan_run_id, group_url=group_url)
    return render_template("scan_matches.html", scan=_serialize_scan(scan) or scan, matches=matches, active_group=group_url, back_url=url_for("scans_page"), **_dashboard_context())


@app.route("/scan/<int:scan_run_id>/telegram")
def scan_telegram_page(scan_run_id: int):
    scan = storage.get_scan_run(scan_run_id)
    if not scan:
        abort(404)
    telegram_leads = _decorate_leads(
        storage.list_leads(
            limit=500,
            include_archived=True,
            include_rejected=True,
            include_owner_ads=True,
            scan_run_id=scan_run_id,
            sort_by="newest",
        )
    )
    telegram_leads = [lead for lead in telegram_leads if lead.get("sent_to_telegram")]
    telegram_failures = storage.get_scan_telegram_failures(scan_run_id)
    return render_template("scan_telegram.html", scan=_serialize_scan(scan) or scan, telegram_leads=telegram_leads, telegram_failures=telegram_failures, back_url=url_for("scans_page"), **_dashboard_context())


@app.route("/scan/<int:scan_run_id>/groups")
def scan_groups_page(scan_run_id: int):
    scan = storage.get_scan_run(scan_run_id)
    if not scan:
        abort(404)
    group_filter = (request.args.get("group") or "").strip() or None
    group_results = scan.get("group_results") or []
    if group_filter:
        group_results = [row for row in group_results if row.get("group_url") == group_filter]
    return render_template("scan_groups.html", scan=_serialize_scan(scan) or scan, group_results=group_results, active_group=group_filter, back_url=url_for("scans_page"), **_dashboard_context())


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
    decorated_lead = _decorate_leads([lead])[0]
    return render_template(
        "lead_detail.html",
        lead=decorated_lead,
        events=storage.list_lead_events(lead_id),
        **_dashboard_context(),
    )


@app.post("/leads/<int:lead_id>/status")
def lead_status_update(lead_id: int):
    status = request.form.get("status", "").strip()
    if status not in ALLOWED_LEAD_STATUSES:
        abort(400)
    storage.update_lead_status(lead_id, status)
    if status == "not_relevant":
        reason = (request.form.get("feedback_reason") or "irrelevant").strip()
        feedback_type = reason if reason in FEEDBACK_LABELS else "irrelevant"
        storage.add_lead_feedback(lead_id, feedback_type, feedback_reason=reason)
    elif status == "closed":
        storage.add_lead_feedback(lead_id, "closed_successfully", feedback_reason="closed")
    next_url = request.form.get("next", "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.post("/leads/<int:lead_id>/notes")
def lead_notes_update(lead_id: int):
    notes = request.form.get("notes", "")
    storage.update_lead_notes(lead_id, notes)
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.post("/leads/<int:lead_id>/feedback")
def lead_feedback_update(lead_id: int):
    feedback_label = request.form.get("feedback", "").strip()
    feedback_reason = (request.form.get("feedback_reason") or "").strip() or None
    if feedback_label in {"good", "bad"}:
        storage.update_lead_feedback(lead_id, feedback_label)
    else:
        storage.add_lead_feedback(lead_id, feedback_label, feedback_reason=feedback_reason)
    next_url = request.form.get("next", "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.post("/review/<int:lead_id>")
def review_feedback_update(lead_id: int):
    action = (request.form.get("action") or "").strip()
    action_map: dict[str, dict[str, Any]] = {
        "good_lead": {"feedback_type": "good_lead"},
        "irrelevant": {"feedback_type": "irrelevant", "status": "not_relevant"},
        "pets": {
            "feedback_type": "pets",
            "status": "not_relevant",
            "fields": {
                "pet_request": 1,
                "pet_friendly_requested": 1,
                "reject_reason_he": "הבקשה כוללת חיות מחמד ולכן לא מתאימה ל-Royal Water Villa.",
            },
        },
        "party_event": {
            "feedback_type": "irrelevant",
            "status": "not_relevant",
            "fields": {
                "lead_type": "event_seeker",
                "reject_reason_he": "נראה שמדובר בבקשת מסיבה או אירוע ולא באירוח שקט.",
            },
        },
        "too_large": {
            "feedback_type": "too_large",
            "status": "not_relevant",
            "fields": {
                "guest_type": "large_group",
                "reject_reason_he": "הבקשה נראית גדולה מדי לפורמט האירוח של Royal Water Villa.",
            },
        },
        "too_expensive": {"feedback_type": "too_expensive", "status": "not_relevant"},
        "bad_location": {"feedback_type": "bad_location", "status": "not_relevant"},
        "owner_ad": {
            "feedback_type": "owner_ad",
            "status": "not_relevant",
            "fields": {
                "owner_advertisement": 1,
                "lead_type": "owner_advertiser",
                "reject_reason_he": "זוהה כפרסום של בעל מקום ולא כליד של אורח.",
            },
        },
    }
    selected = action_map.get(action)
    if not selected:
        abort(400)
    fields = selected.get("fields") or {}
    if fields:
        storage.update_lead_fields(lead_id, **fields)
    if selected.get("status"):
        storage.update_lead_status(lead_id, str(selected["status"]))
    storage.add_lead_feedback(
        lead_id,
        str(selected["feedback_type"]),
        feedback_reason=action,
    )
    return redirect(url_for("review_page"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
