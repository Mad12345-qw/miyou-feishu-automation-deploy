from __future__ import annotations

import os
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

from mobile_interview_form import register_mobile_interview_form
from miyou_system_automation import APP_TOKEN, Feishu, OPENAPI, TABLES, build_chain, ensure_interview_workflow_surface, ensure_personal_views, request_json, sync_anchor_display_names, sync_calendar, sync_interview_personnel_dropdowns, sync_interview_photos_to_anchors, sync_management_summary, sync_missing_interview_display_fields, sync_one_interview_personnel_assignment, sync_operational_calendars, sync_person_assignment_fields, sync_personal_workbench_rows, sync_personnel_directory
from run_miyou_rule_engine import reconcile
from sync_missing_personal_entries import sync_missing_personal_entries
from sync_missing_workbench_rows import sync_missing_workbench_rows


app = Flask(__name__)
RECRUITMENT_ENTRY_RECORD_ID = os.environ.get("FEISHU_RECRUITMENT_ENTRY_RECORD_ID", "").strip()
WORKBENCH_TABLE_ID = os.environ.get("FEISHU_WORKBENCH_TABLE_ID", "").strip()
PERSONNEL_ENTRY_LOCK = threading.Lock()
ANCHOR_TRANSFER_LOCK = threading.Lock()
REPORTING_LOCK = threading.Lock()
INTERVIEW_INTEGRITY_LOCK = threading.Lock()
FEISHU_EVENT_LOCK = threading.Lock()
PROVISIONING_STATE_LOCK = threading.Lock()
ANCHOR_TRANSFER_STATE_LOCK = threading.Lock()
LAST_FEISHU_RECORD_EVENT: dict[str, object] = {"received": False}
LAST_PERSONNEL_PROVISIONING: dict[str, object] = {"status": "not_run"}
LAST_ANCHOR_TRANSFER: dict[str, object] = {"status": "not_run"}


def tenant_token() -> str:
    response = request_json(
        "POST",
        f"{OPENAPI}/auth/v3/tenant_access_token/internal",
        body={"app_id": os.environ["FEISHU_APP_ID"], "app_secret": os.environ["FEISHU_APP_SECRET"]},
    )
    if response.get("code") != 0 or not response.get("tenant_access_token"):
        raise RuntimeError(f"Unable to obtain tenant token: {response}")
    return response["tenant_access_token"]


register_mobile_interview_form(app, tenant_token)


def mobile_interview_form_url() -> str:
    public_url = os.environ.get("PUBLIC_SERVICE_URL", "").strip().rstrip("/")
    form_token = os.environ.get("MOBILE_FORM_TOKEN", "").strip()
    if not public_url or not form_token:
        raise RuntimeError("PUBLIC_SERVICE_URL and MOBILE_FORM_TOKEN are required.")
    query = urllib.parse.urlencode({"token": form_token})
    return f"{public_url}/forms/interview?{query}"


def sync_mobile_form_entry() -> dict[str, object]:
    if not RECRUITMENT_ENTRY_RECORD_ID or not WORKBENCH_TABLE_ID:
        raise RuntimeError("FEISHU_RECRUITMENT_ENTRY_RECORD_ID and FEISHU_WORKBENCH_TABLE_ID are required.")
    fs = Feishu(tenant_token())
    response = fs.batch_update(
        WORKBENCH_TABLE_ID,
        [
            {
                "record_id": RECRUITMENT_ENTRY_RECORD_ID,
                "fields": {
                    "点这里办理": {
                        "link": mobile_interview_form_url(),
                        "text": "开始邀约/面试登记",
                    }
                },
            }
        ],
        batch_size=1,
    )
    first = response[0] if response else {}
    if first.get("code") != 0:
        raise RuntimeError(f"Failed to update the mobile interview entry: {first}")
    return {"updated": True, "record_id": RECRUITMENT_ENTRY_RECORD_ID}


def authorize() -> None:
    expected = os.environ.get("JOB_TOKEN", "")
    provided = request.headers.get("X-Job-Token", "")
    if expected and provided != expected:
        raise PermissionError("Invalid job token.")


def service_enabled() -> bool:
    return os.environ.get("AUTOMATION_ENABLED", "false").lower() == "true"


def calendar_sync_enabled() -> bool:
    return os.environ.get("CALENDAR_SYNC_ENABLED", "false").lower() == "true"


def legacy_assignment_backfill_enabled() -> bool:
    return os.environ.get("LEGACY_ASSIGNMENT_BACKFILL_ENABLED", "false").lower() == "true"


def personnel_dropdown_sync_enabled() -> bool:
    return os.environ.get("PERSONNEL_DROPDOWN_SYNC_ENABLED", "true").lower() == "true"


def reporting_sync_enabled() -> bool:
    return os.environ.get("REPORTING_SYNC_ENABLED", "false").lower() == "true"


def anchor_maintenance_sync_enabled() -> bool:
    """Run expensive historical anchor maintenance only when explicitly enabled."""
    return os.environ.get("ANCHOR_MAINTENANCE_SYNC_ENABLED", "false").lower() == "true"


def run_personnel_dropdown_cycle() -> dict[str, object]:
    fs = Feishu(tenant_token())
    out_dir = Path("runtime")
    personnel = sync_personnel_directory(fs, out_dir)
    surface = ensure_interview_workflow_surface(fs, out_dir)
    dropdowns = sync_interview_personnel_dropdowns(fs, out_dir)
    return {"personnel": personnel, "dropdowns": dropdowns, "surface": surface}


def run_personnel_entry_cycle() -> dict[str, object]:
    """Keep new employee entry creation independent from heavy business jobs."""
    if not PERSONNEL_ENTRY_LOCK.acquire(blocking=False):
        return {"skipped": True, "reason": "Personnel entry sync is already running."}
    try:
        try:
            fs = Feishu(tenant_token())
            out_dir = Path("runtime")
            display_repairs = sync_missing_interview_display_fields(fs, out_dir)
            personnel = sync_personnel_directory(fs, out_dir)
            surface = ensure_interview_workflow_surface(fs, out_dir)
            dropdowns = sync_interview_personnel_dropdowns(fs, out_dir, sync_records=False)
            personal_views = sync_missing_personal_entries(fs, out_dir)
            personal_workbench = sync_missing_workbench_rows(fs, out_dir)
            business_failures = len(personal_views["view_sync"]["failed"])
            workbench_view_failures = len(personal_views["workbench_view_sync"]["failed"])
            workbench_row_failures = max(0, personal_workbench["desired_rows"] - personal_workbench["created"])
            provisioning_status = "ok" if not (business_failures or workbench_view_failures or workbench_row_failures) else "degraded"
            with PROVISIONING_STATE_LOCK:
                LAST_PERSONNEL_PROVISIONING.update(
                    {
                        "status": provisioning_status,
                        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "people": personal_views["people"],
                        "business_views_created": len(personal_views["view_sync"]["created"]),
                        "business_view_failures": business_failures,
                        "workbench_views_created": len(personal_views["workbench_view_sync"]["created"]),
                        "workbench_views_repaired": len(personal_views["workbench_view_sync"]["repaired"]),
                        "workbench_view_failures": workbench_view_failures,
                        "workbench_rows_created": personal_workbench["created"],
                        "workbench_row_failures": workbench_row_failures,
                    }
                )
            return {
                "personnel": personnel,
                "surface": surface,
                "dropdowns": dropdowns,
                "display_repairs": display_repairs,
                "personal_views": personal_views,
                "personal_workbench": personal_workbench,
            }
        except Exception as exc:
            with PROVISIONING_STATE_LOCK:
                LAST_PERSONNEL_PROVISIONING.update(
                    {
                        "status": "failed",
                        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "error": str(exc),
                    }
                )
            raise
    finally:
        PERSONNEL_ENTRY_LOCK.release()


def run_interview_integrity_cycle() -> dict[str, object]:
    if not INTERVIEW_INTEGRITY_LOCK.acquire(blocking=False):
        return {"skipped": True, "reason": "Interview integrity sync is already running."}
    try:
        return sync_missing_interview_display_fields(Feishu(tenant_token()), Path("runtime"))
    finally:
        INTERVIEW_INTEGRITY_LOCK.release()


def run_anchor_transfer_cycle() -> dict[str, object]:
    """Process transfer flags without waiting for personnel, reporting, or calendar work."""
    if not service_enabled():
        raise RuntimeError("Automation is disabled. Set AUTOMATION_ENABLED=true after cutover approval.")
    if not ANCHOR_TRANSFER_LOCK.acquire(blocking=False):
        return {"skipped": True, "reason": "Anchor transfer sync is already running."}
    started_at = datetime.now().astimezone()
    with ANCHOR_TRANSFER_STATE_LOCK:
        LAST_ANCHOR_TRANSFER.update(
            {
                "status": "running",
                "started_at": started_at.isoformat(timespec="seconds"),
                "trigger": threading.current_thread().name,
            }
        )
    try:
        batch = f"LIVE-{datetime.now().strftime('%Y%m%d')}"
        fs = Feishu(tenant_token())
        out_dir = Path("runtime")
        build = build_chain(
            fs,
            batch,
            limit=max(1, int(os.environ.get("ANCHOR_TRANSFER_BATCH_SIZE", "50"))),
            out_dir=out_dir,
            not_before_ms=0,
            recover_existing_links=True,
        )
        if anchor_maintenance_sync_enabled():
            photos = sync_interview_photos_to_anchors(fs, out_dir)
            anchor_displays = sync_anchor_display_names(fs, out_dir)
        else:
            maintenance_reason = "Anchor maintenance sync is disabled."
            photos = {"skipped": True, "reason": maintenance_reason}
            anchor_displays = {"skipped": True, "reason": maintenance_reason}
        result = {"batch": batch, "build": build, "photos": photos, "anchor_displays": anchor_displays}
        finished_at = datetime.now().astimezone()
        with ANCHOR_TRANSFER_STATE_LOCK:
            LAST_ANCHOR_TRANSFER.update(
                {
                    "status": "ok",
                    "finished_at": finished_at.isoformat(timespec="seconds"),
                    "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
                    "batch": batch,
                    "checked_records": build.get("checked_interviews", 0),
                    "selected_records": build.get("selected_interviews", 0),
                    "created_anchors": build.get("created_anchors", 0),
                    "recovered_links": build.get("recovered_existing_anchors", 0),
                    "updated_operator_assignments": (build.get("anchor_operator_sync") or {}).get("updated", 0),
                    "missing_linked_anchors": len((build.get("anchor_operator_sync") or {}).get("missing_linked_anchor_ids") or []),
                    "unresolved_people": len((build.get("assignment_sync") or {}).get("unresolved_values") or []),
                    "error": "",
                }
            )
        return result
    except Exception as exc:
        finished_at = datetime.now().astimezone()
        with ANCHOR_TRANSFER_STATE_LOCK:
            LAST_ANCHOR_TRANSFER.update(
                {
                    "status": "failed",
                    "finished_at": finished_at.isoformat(timespec="seconds"),
                    "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
                    "error": str(exc),
                }
            )
        raise
    finally:
        ANCHOR_TRANSFER_LOCK.release()


def trigger_anchor_transfer_async(reason: str) -> bool:
    """Wake a self-healing cycle without delaying health checks or Feishu callbacks."""
    if not service_enabled() or ANCHOR_TRANSFER_LOCK.locked():
        return False

    def run() -> None:
        try:
            run_anchor_transfer_cycle()
        except Exception as exc:
            app.logger.exception("Async anchor transfer sync failed (%s): %s", reason, exc)

    threading.Thread(target=run, daemon=True, name=f"Anchor transfer: {reason}").start()
    return True


def run_reporting_cycle() -> dict[str, object]:
    """Run lower-priority reports and calendars separately from operational writes."""
    if not service_enabled():
        raise RuntimeError("Automation is disabled. Set AUTOMATION_ENABLED=true after cutover approval.")
    if not REPORTING_LOCK.acquire(blocking=False):
        return {"skipped": True, "reason": "Reporting sync is already running."}
    try:
        batch = f"REPORT-{datetime.now().strftime('%Y%m%d%H%M')}"
        fs = Feishu(tenant_token())
        out_dir = Path("runtime")
        rules = reconcile(fs, batch, out_dir=out_dir, dry_run=False)
        management_summary = sync_management_summary(fs, out_dir)
        calendar = (
            sync_calendar(fs, batch, out_dir=out_dir)
            if calendar_sync_enabled()
            else {"skipped": True, "reason": "Calendar sync is disabled."}
        )
        operational_calendar = (
            sync_operational_calendars(fs, out_dir=out_dir)
            if calendar_sync_enabled()
            else {"skipped": True, "reason": "Calendar sync is disabled."}
        )
        return {"batch": batch, "rules": rules, "management_summary": management_summary, "calendar": calendar, "operational_calendar": operational_calendar}
    finally:
        REPORTING_LOCK.release()


def run_reconcile(batch: str) -> dict[str, object]:
    if not service_enabled():
        raise RuntimeError("Automation is disabled. Set AUTOMATION_ENABLED=true after cutover approval.")
    fs = Feishu(tenant_token())
    result = reconcile(fs, batch, out_dir=Path("runtime"), dry_run=False)
    return result


def run_live_cycle() -> dict[str, object]:
    if not service_enabled():
        raise RuntimeError("Automation is disabled. Set AUTOMATION_ENABLED=true after cutover approval.")
    return {
        "personnel_entries": run_personnel_entry_cycle(),
        "anchor_transfers": run_anchor_transfer_cycle(),
        "reporting": run_reporting_cycle(),
    }


def background_scheduler() -> None:
    def worker(name: str, interval: int, enabled: callable, action: callable) -> None:
        while True:
            if enabled():
                try:
                    action()
                except Exception as exc:
                    app.logger.exception("%s failed: %s", name, exc)
            time.sleep(interval)

    # Keep employee entries and ownership routing responsive without relying on
    # a user to re-enter the same assignment in a second table.
    base_interval = max(60, int(os.environ.get("AUTOMATION_INTERVAL_SECONDS", "60")))
    workers = [
        ("Mobile form entry sync", base_interval, lambda: True, sync_mobile_form_entry),
        ("Interview integrity sync", base_interval, personnel_dropdown_sync_enabled, run_interview_integrity_cycle),
        ("Personnel entry sync", max(60, min(base_interval, 180)), personnel_dropdown_sync_enabled, run_personnel_entry_cycle),
        ("Anchor transfer sync", max(60, min(base_interval, 180)), service_enabled, run_anchor_transfer_cycle),
        ("Reporting sync", max(300, base_interval * 3), lambda: service_enabled() and reporting_sync_enabled(), run_reporting_cycle),
    ]
    for name, interval, enabled, action in workers:
        threading.Thread(target=worker, args=(name, interval, enabled, action), daemon=True, name=name).start()
    while True:
        time.sleep(3600)


def note_feishu_record_event(event_type: str, table_kind: str) -> None:
    with FEISHU_EVENT_LOCK:
        LAST_FEISHU_RECORD_EVENT.update(
            {
                "received": True,
                "event_type": event_type,
                "table_kind": table_kind,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )


@app.get("/health")
def health() -> object:
    with FEISHU_EVENT_LOCK:
        last_event = dict(LAST_FEISHU_RECORD_EVENT)
    with PROVISIONING_STATE_LOCK:
        last_provisioning = dict(LAST_PERSONNEL_PROVISIONING)
    with ANCHOR_TRANSFER_STATE_LOCK:
        last_anchor_transfer = dict(LAST_ANCHOR_TRANSFER)
    anchor_transfer_wake_requested = False
    if last_anchor_transfer.get("status") in {"not_run", "failed"}:
        anchor_transfer_wake_requested = trigger_anchor_transfer_async("health wake")
    return jsonify(
        {
            "ok": True,
            "automation_enabled": os.environ.get("AUTOMATION_ENABLED", "false").lower() == "true",
            "calendar_sync_enabled": calendar_sync_enabled(),
            "local_scheduler_enabled": os.environ.get("LOCAL_SCHEDULER_ENABLED", "false").lower() == "true",
            "contact_full_sync_enabled": os.environ.get("CONTACT_FULL_SYNC_ENABLED", "false").lower() == "true",
            "legacy_assignment_backfill_enabled": legacy_assignment_backfill_enabled(),
            "personnel_dropdown_sync_enabled": personnel_dropdown_sync_enabled(),
            "interview_owner_and_date_group_repair_enabled": personnel_dropdown_sync_enabled(),
            "reporting_sync_enabled": reporting_sync_enabled(),
            "anchor_maintenance_sync_enabled": anchor_maintenance_sync_enabled(),
            "mobile_form_configured": bool(
                os.environ.get("PUBLIC_SERVICE_URL", "").strip()
                and os.environ.get("MOBILE_FORM_TOKEN", "").strip()
            ),
            "feishu_record_event_callback_ready": True,
            "last_feishu_record_event": last_event,
            "last_personnel_provisioning": last_provisioning,
            "last_anchor_transfer": last_anchor_transfer,
            "anchor_transfer_wake_requested": anchor_transfer_wake_requested,
            "schema_version": "2026-08-17-anchor-transfer-self-healing",
            "active_batch": os.environ.get("AUTOMATION_ACTIVE_BATCH", ""),
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )


@app.post("/webhook/feishu")
def feishu_record_event() -> object:
    """Process a changed Base row immediately; repeated delivery is harmless."""
    payload = request.get_json(silent=True) or {}
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge", "")})

    header = payload.get("header") or {}
    event = payload.get("event") or {}
    if str(event.get("app_token") or "") != APP_TOKEN:
        return jsonify({"ok": True, "ignored": "other_app"})
    table_id = str(event.get("table_id") or "")
    record_id = str(event.get("record_id") or "")
    if not record_id:
        return jsonify({"ok": True, "ignored": "missing_record"})
    fs = Feishu(tenant_token())
    out_dir = Path("runtime")
    if table_id == TABLES["interview"]:
        note_feishu_record_event(str(header.get("event_type") or ""), "interview")
        result = sync_one_interview_personnel_assignment(fs, record_id, out_dir)
        transfer_wake_requested = trigger_anchor_transfer_async("Feishu interview event")
        return jsonify(
            {
                "ok": True,
                "kind": "interview_assignment",
                "updated_fields": result["updated_fields"],
                "anchor_transfer_wake_requested": transfer_wake_requested,
            }
        )
    note_feishu_record_event(str(header.get("event_type") or ""), "ignored")
    return jsonify({"ok": True, "ignored": "other_table", "event_type": header.get("event_type", "")})


@app.post("/jobs/reconcile")
def reconcile_job() -> object:
    try:
        authorize()
        body = request.get_json(silent=True) or {}
        batch = str(body.get("batch") or os.environ.get("AUTOMATION_ACTIVE_BATCH", "")).strip()
        if not batch:
            return jsonify({"ok": False, "error": "batch is required"}), 400
        return jsonify({"ok": True, "result": run_reconcile(batch)})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/run-cycle")
def live_cycle_job() -> object:
    try:
        authorize()
        if not service_enabled():
            return jsonify(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "Automation is disabled pending cutover approval.",
                }
            )
        return jsonify({"ok": True, "result": run_live_cycle()})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/sync-personnel-dropdowns")
def personnel_dropdown_job() -> object:
    try:
        authorize()
        if not personnel_dropdown_sync_enabled():
            return jsonify({"ok": True, "skipped": True, "reason": "Personnel dropdown sync is disabled."})
        return jsonify({"ok": True, "result": run_personnel_entry_cycle()})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/sync-mobile-form-entry")
def mobile_form_entry_job() -> object:
    try:
        authorize()
        return jsonify({"ok": True, "result": sync_mobile_form_entry()})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/refresh-interview-surface")
def refresh_interview_surface_job() -> object:
    try:
        authorize()
        fs = Feishu(tenant_token())
        return jsonify({"ok": True, "result": ensure_interview_workflow_surface(fs, Path("runtime"))})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/sync-anchor-photos")
def sync_anchor_photos_job() -> object:
    try:
        authorize()
        fs = Feishu(tenant_token())
        return jsonify({"ok": True, "result": sync_interview_photos_to_anchors(fs, Path("runtime"))})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/sync-anchor-display-names")
def sync_anchor_display_names_job() -> object:
    try:
        authorize()
        fs = Feishu(tenant_token())
        return jsonify({"ok": True, "result": sync_anchor_display_names(fs, Path("runtime"))})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/sync-personnel-entries")
def personnel_entries_job() -> object:
    try:
        authorize()
        if not personnel_dropdown_sync_enabled():
            return jsonify({"ok": True, "skipped": True, "reason": "Personnel entry sync is disabled."})
        return jsonify({"ok": True, "result": run_personnel_entry_cycle()})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        app.logger.exception("Personnel entry job failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jobs/process-anchor-transfers")
def anchor_transfers_job() -> object:
    try:
        authorize()
        return jsonify({"ok": True, "result": run_anchor_transfer_cycle()})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        app.logger.exception("Anchor transfer job failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    if os.environ.get("LOCAL_SCHEDULER_ENABLED", "false").lower() == "true":
        threading.Thread(target=background_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
