from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import APP_TOKEN, Feishu, get_tenant_token, write_json


INTERVIEW_TABLE_ID = "tblNuKseW2MGL3EO"
WORKBENCH_TABLE_ID = "tblIcblT5703VGvp"
SOURCE_FORM_ID = "vewJlsIkU0"
FORM_FIELD = "点这里办理"


def checked(response: dict[str, Any], action: str) -> dict[str, Any]:
    if response.get("code") != 0:
        raise RuntimeError(f"{action} failed: {response}")
    return response


def form_metadata(fs: Feishu, form_id: str) -> dict[str, Any]:
    response = checked(
        fs.api(
            "GET",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{form_id}",
        ),
        f"read form {form_id}",
    )
    return ((response.get("data") or {}).get("form") or {})


def form_fields(fs: Feishu, form_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query: dict[str, Any] = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        response = checked(
            fs.api(
                "GET",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{form_id}/fields",
                query=query,
            ),
            f"list form fields {form_id}",
        )
        data = response.get("data") or {}
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            return items
        page_token = str(data.get("page_token") or "")
        if not page_token:
            return items


def matching_workbench_entries(fs: Feishu, form_id: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    marker = f"view={form_id}"
    for record in fs.list_records(WORKBENCH_TABLE_ID, page_size=500):
        value = (record.get("fields") or {}).get(FORM_FIELD)
        if isinstance(value, dict) and marker in str(value.get("link") or ""):
            matches.append(record)
    return matches


def copy_form_configuration(
    fs: Feishu,
    source_metadata: dict[str, Any],
    source_fields: list[dict[str, Any]],
    target_form_id: str,
) -> None:
    previous_visible_field_id = ""
    for field in source_fields:
        field_id = str(field.get("field_id") or "")
        if not field_id:
            continue
        desired_visible = bool(field.get("visible"))
        body: dict[str, Any] = {
            "title": field.get("title") or "",
            "required": bool(field.get("required")),
            "visible": True,
        }
        if desired_visible and previous_visible_field_id:
            body["pre_field_id"] = previous_visible_field_id
        if field.get("description") is not None:
            body["description"] = field.get("description")
        if field.get("rich_description") is not None:
            body["rich_description"] = field.get("rich_description")
        checked(
            fs.api(
                "PATCH",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{target_form_id}/fields/{field_id}",
                body=body,
            ),
            f"configure form field {field_id}",
        )
        if not desired_visible:
            checked(
                fs.api(
                    "PATCH",
                    f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{target_form_id}/fields/{field_id}",
                    body={"visible": False},
                ),
                f"hide form field {field_id}",
            )
        if desired_visible:
            previous_visible_field_id = field_id

    checked(
        fs.api(
            "PATCH",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{target_form_id}",
            body={
                "name": "填写_邀约与面试_切换中",
                "description": source_metadata.get("description") or "",
            },
        ),
        "configure replacement form metadata",
    )


def comparable(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "field_settings": {
            str(item.get("field_id") or ""): {
                "title": item.get("title"),
                "required": bool(item.get("required")),
                "visible": bool(item.get("visible")),
                "description": item.get("description"),
                "rich_description": item.get("rich_description"),
            }
            for item in fields
        },
        "visible_order": [
            item.get("field_id")
            for item in fields
            if bool(item.get("visible"))
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replace the interview form without inheriting hidden default answers.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume-form-id", default="")
    args = parser.parse_args()

    fs = Feishu(get_tenant_token(args.env))
    source_metadata = form_metadata(fs, SOURCE_FORM_ID)
    source_fields = form_fields(fs, SOURCE_FORM_ID)
    entries = matching_workbench_entries(fs, SOURCE_FORM_ID)
    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_form_id": SOURCE_FORM_ID,
        "source_metadata": source_metadata,
        "source_fields": source_fields,
        "workbench_records": [record.get("record_id") for record in entries],
        "replacement_form_id": "",
        "workbench_updates": [],
        "verification": {},
    }
    if not args.apply:
        write_json(args.out, report)
        print(json.dumps({"mode": "dry_run", "fields": len(source_fields), "workbench_records": len(entries)}, ensure_ascii=False))
        return

    replacement_form_id = str(args.resume_form_id or "").strip()
    if not replacement_form_id:
        suffix = datetime.now().strftime("%Y%m%d%H%M%S")
        legacy_view_name = f"临时旧版表单_{suffix}"
        upgraded_form_name = f"填写_邀约与面试_切换中_{suffix}"
        created = checked(
            fs.api(
                "POST",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/views",
                body={"view_name": legacy_view_name, "view_type": "form"},
            ),
            "create replacement form",
        )
        replacement_form_id = str((((created.get("data") or {}).get("view") or {}).get("view_id") or ""))
        if not replacement_form_id:
            raise RuntimeError(f"Replacement form response has no view_id: {created}")
        report["replacement_form_id"] = replacement_form_id
        write_json(args.out, report)

        checked(
            fs.api(
                "POST",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{replacement_form_id}/upgrade",
                body={"form_name": upgraded_form_name, "display_mode": "traditional"},
            ),
            "upgrade replacement form",
        )
        copy_form_configuration(fs, source_metadata, source_fields, replacement_form_id)
    report["replacement_form_id"] = replacement_form_id
    replacement_fields = form_fields(fs, replacement_form_id)
    if comparable(replacement_fields) != comparable(source_fields):
        report["verification"] = {
            "configured": False,
            "replacement_fields": replacement_fields,
        }
        write_json(args.out, report)
        raise RuntimeError("Replacement form verification did not match the source form.")

    updates: list[dict[str, Any]] = []
    for record in entries:
        value = (record.get("fields") or {}).get(FORM_FIELD) or {}
        updates.append(
            {
                "record_id": record["record_id"],
                "fields": {
                    FORM_FIELD: {
                        "text": value.get("text") or "开始邀约/面试登记",
                        "link": str(value.get("link") or "").replace(
                            f"view={SOURCE_FORM_ID}",
                            f"view={replacement_form_id}",
                        ),
                    }
                },
            }
        )
    if updates:
        responses = fs.batch_update(WORKBENCH_TABLE_ID, updates, batch_size=100)
        failures = [response for response in responses if response.get("code") != 0]
        if failures:
            raise RuntimeError(f"Unable to switch workbench links: {failures}")
        report["workbench_updates"] = updates

    retired_name = f"停用_旧表单_{datetime.now().strftime('%Y%m%d')}_请从01进入"
    checked(
        fs.api(
            "PATCH",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{SOURCE_FORM_ID}",
            body={
                "name": retired_name,
                "description": "此旧表单已停用，请从 01 从这里开始 进入新的邀约与面试表单。",
            },
        ),
        "retire source form",
    )
    checked(
        fs.api(
            "PATCH",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{INTERVIEW_TABLE_ID}/forms/{replacement_form_id}",
            body={
                "name": source_metadata.get("name") or "填写_邀约与面试",
                "description": source_metadata.get("description") or "",
            },
        ),
        "activate replacement form",
    )

    remaining_old_links = matching_workbench_entries(fs, SOURCE_FORM_ID)
    new_links = matching_workbench_entries(fs, replacement_form_id)
    report["verification"] = {
        "configured": True,
        "source_form": form_metadata(fs, SOURCE_FORM_ID),
        "replacement_form": form_metadata(fs, replacement_form_id),
        "old_workbench_links": [item.get("record_id") for item in remaining_old_links],
        "new_workbench_links": [item.get("record_id") for item in new_links],
        "operator_field": next(
            (item for item in replacement_fields if item.get("field_id") == "fld64vGayW"),
            {},
        ),
    }
    write_json(args.out, report)
    print(
        json.dumps(
            {
                "mode": "apply",
                "replacement_form_id": replacement_form_id,
                "workbench_links_switched": len(updates),
                "old_workbench_links": len(remaining_old_links),
                "new_workbench_links": len(new_links),
                "verified": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
