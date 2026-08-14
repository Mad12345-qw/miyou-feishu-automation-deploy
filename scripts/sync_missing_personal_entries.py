from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from miyou_system_automation import APP_TOKEN, Feishu, TABLES, get_tenant_token, list_value, text_value, user_ids, write_json


WORKBENCH_TABLE = os.environ.get("FEISHU_WORKBENCH_TABLE_ID", "").strip()

SPECS = [
    ("interview", "招募人账号（系统）", "招聘", "候选人", {"招募经纪人"}, "候选人", "打开我的候选人"),
    ("interview", "面试官账号（系统）", "面试", "候选人", {"面试官"}, "面试候选人", "打开我的面试候选人"),
    ("interview", "对接运营账号（系统）", "运营", "候选人", {"对接运营"}, "对接候选人", "打开我的对接候选人"),
    ("anchor", "运营经济人", "运营", "主播", {"对接运营", "培训运营", "跟播运营"}, "主播", "打开我的主播"),
    ("task", "运营经济人", "运营", "日程", {"对接运营", "培训运营", "跟播运营"}, "日程", "打开我的日程"),
    ("visual", "提交运营", "运营", "视觉", {"对接运营"}, "视觉任务", "打开我的视觉任务"),
    ("visual", "视觉处理人", "视觉", "任务", {"视觉"}, "视觉任务", "打开我的视觉任务"),
    ("training", "培训运营", "运营", "培训", {"对接运营", "培训运营"}, "培训", "打开我的培训"),
    ("first_live", "跟播运营", "运营", "首播", {"对接运营", "跟播运营"}, "首播", "打开我的首播"),
    ("review", "跟播人员", "运营", "复盘", {"对接运营", "跟播运营"}, "复盘", "打开我的复盘"),
]


def list_views(fs: Feishu, table_id: str) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query: dict[str, Any] = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views", query)
        if response.get("code") != 0:
            raise RuntimeError(f"Unable to list views for {table_id}: {response}")
        data = response.get("data") or {}
        views.extend(data.get("items") or [])
        if not data.get("has_more"):
            return views
        page_token = str(data.get("page_token") or "")
        if not page_token:
            return views


def active_people(fs: Feishu) -> dict[str, dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    for record in fs.list_records(TABLES["personnel"], page_size=500):
        fields = record.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职":
            continue
        if text_value(fields.get("账号状态")) != "正常":
            continue
        if fields.get("是否创建个人入口") is not True:
            continue
        name = text_value(fields.get("姓名")).strip()
        for user_id in user_ids(fields.get("飞书用户")):
            existing = people.setdefault(user_id, {"name": name, "roles": set()})
            if not existing.get("name") and name:
                existing["name"] = name
            existing["roles"].update(list_value(fields.get("角色")))
    return people


def workbench_view_user_id(view: dict[str, Any], employee_field_id: str) -> str:
    conditions = (((view.get("property") or {}).get("filter_info") or {}).get("conditions") or [])
    for condition in conditions:
        if str(condition.get("field_id") or "") != employee_field_id or condition.get("operator") != "is":
            continue
        try:
            values = json.loads(str(condition.get("value") or "[]"))
        except json.JSONDecodeError:
            continue
        if isinstance(values, list) and len(values) == 1:
            return str(values[0])
    return ""


def sync_personal_workbench_views(
    fs: Feishu,
    people: dict[str, dict[str, Any]],
    dry_run: bool = False,
) -> dict[str, Any]:
    employee_field = next(
        (field for field in fs.fields(WORKBENCH_TABLE) if field.get("field_name") == "员工账号"),
        None,
    )
    employee_field_id = str((employee_field or {}).get("field_id") or "")
    if not employee_field_id:
        raise RuntimeError("The workbench employee account field is missing.")

    current_views = {str(view.get("view_name") or ""): view for view in list_views(fs, WORKBENCH_TABLE)}
    created: list[dict[str, str]] = []
    repaired: list[dict[str, str]] = []
    existing: list[dict[str, str]] = []
    failed: list[dict[str, Any]] = []
    planned: list[dict[str, str]] = []
    view_details: dict[str, dict[str, Any]] = {}
    views_by_user_id: dict[str, dict[str, Any]] = {}
    for view_name, summary in current_views.items():
        view_id = str(summary.get("view_id") or "")
        response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views/{view_id}")
        detail = (response.get("data") or {}).get("view") or response.get("data") or {}
        if response.get("code") != 0:
            failed.append({"view_name": view_name, "reason": response})
            continue
        view_details[view_name] = detail
        bound_user_id = workbench_view_user_id(detail, employee_field_id)
        if bound_user_id:
            views_by_user_id.setdefault(bound_user_id, {"view_name": view_name, "view_id": view_id})

    name_counts: dict[str, int] = {}
    for person in people.values():
        name = str(person.get("name") or "").strip()
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1

    supported_roles = set().union(*(set(item[4]) for item in SPECS))
    for user_id, person in people.items():
        name = str(person.get("name") or "").strip()
        if not name or not set(person.get("roles") or set()).intersection(supported_roles):
            continue
        view_name = name if name_counts[name] == 1 else f"{name}（{user_id[-6:]}）"
        summary = current_views.get(view_name)
        view_id = str((summary or {}).get("view_id") or "")
        detail = view_details.get(view_name) or {}
        if view_id and workbench_view_user_id(detail, employee_field_id) == user_id:
            existing.append({"name": name, "view_name": view_name, "view_id": view_id})
            continue
        if not view_id and user_id in views_by_user_id:
            alias_view = views_by_user_id[user_id]
            existing.append({"name": name, "view_name": str(alias_view["view_name"]), "view_id": str(alias_view["view_id"])})
            continue

        action = "repair" if view_id else "create"
        planned.append({"name": name, "view_name": view_name, "user_id": user_id, "action": action})
        if dry_run:
            continue
        if not view_id:
            response = fs.api(
                "POST",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views",
                body={"view_name": view_name, "view_type": "grid"},
            )
            view = (response.get("data") or {}).get("view") or response.get("data") or {}
            view_id = str(view.get("view_id") or "")
            if response.get("code") != 0 or not view_id:
                failed.append({"name": name, "view_name": view_name, "reason": response})
                continue
        patch = fs.api(
            "PATCH",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views/{view_id}",
            body={
                "view_name": view_name,
                "property": {
                    "filter_info": {
                        "conditions": [
                            {
                                "field_id": employee_field_id,
                                "operator": "is",
                                "value": json.dumps([user_id], ensure_ascii=False),
                            }
                        ],
                        "conjunction": "and",
                    },
                    "hidden_fields": [employee_field_id],
                },
            },
        )
        if patch.get("code") != 0:
            failed.append({"name": name, "view_name": view_name, "reason": patch})
            continue
        item = {"name": name, "view_name": view_name, "view_id": view_id}
        (repaired if action == "repair" else created).append(item)

    return {
        "mode": "dry_run" if dry_run else "apply",
        "planned": planned,
        "created": created,
        "repaired": repaired,
        "existing": existing,
        "failed": failed,
    }


def create_missing_business_views(fs: Feishu, people: dict[str, dict[str, Any]]) -> dict[str, Any]:
    table_keys = sorted({item[0] for item in SPECS})
    field_ids = {
        key: {field.get("field_name"): field.get("field_id") for field in fs.fields(TABLES[key])}
        for key in table_keys
    }
    views = {key: {str(view.get("view_name") or ""): view for view in list_views(fs, TABLES[key])} for key in table_keys}
    created: list[dict[str, Any]] = []
    existing: list[str] = []
    failed: list[dict[str, Any]] = []
    for user_id, person in people.items():
        name = str(person["name"] or "").strip()
        if not name:
            continue
        for table_key, field_name, prefix, suffix, roles, _target, _label in SPECS:
            if not person["roles"].intersection(roles):
                continue
            field_id = field_ids[table_key].get(field_name)
            if not field_id:
                failed.append({"name": name, "table": table_key, "reason": f"Missing field {field_name}"})
                continue
            view_name = f"{prefix}_{name}_{suffix}"[:100]
            if view_name in views[table_key]:
                existing.append(view_name)
                continue
            response = fs.api(
                "POST",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES[table_key]}/views",
                body={"view_name": view_name, "view_type": "grid"},
            )
            view = (response.get("data") or {}).get("view") or response.get("data") or {}
            view_id = str(view.get("view_id") or "")
            if response.get("code") != 0 or not view_id:
                failed.append({"name": name, "table": table_key, "reason": response})
                continue
            patch = fs.api(
                "PATCH",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES[table_key]}/views/{view_id}",
                body={
                    "view_name": view_name,
                    "property": {
                        "filter_info": {
                            "conditions": [{"field_id": field_id, "operator": "is", "value": json.dumps([user_id], ensure_ascii=False)}],
                            "conjunction": "and",
                        }
                    },
                },
            )
            if patch.get("code") != 0:
                failed.append({"name": name, "table": table_key, "reason": patch})
                continue
            views[table_key][view_name] = {"view_id": view_id, "view_name": view_name}
            created.append({"name": name, "table": table_key, "view_name": view_name, "view_id": view_id})
            time.sleep(0.25)
    return {"created": created, "existing": existing, "failed": failed, "views": views}


def sync_missing_personal_entries(fs: Feishu, out_dir: Path, dry_run_workbench: bool = False) -> dict[str, Any]:
    people = active_people(fs)
    report = {"people": len(people), "view_sync": create_missing_business_views(fs, people)}
    report["workbench_view_sync"] = sync_personal_workbench_views(fs, people, dry_run=dry_run_workbench)
    report["unconfigured_people"] = sorted(
        person["name"] for person in people.values() if not person["roles"] and person["name"]
    )
    write_json(out_dir / "sync_missing_personal_entries_result.json", report)
    return report


def main() -> None:
    fs = Feishu(get_tenant_token(Path("feishu/.env.local")))
    report = sync_missing_personal_entries(fs, Path("scripts/runtime"))
    print(json.dumps({"people": report["people"], "created": len(report["view_sync"]["created"]), "existing": len(report["view_sync"]["existing"]), "failed": len(report["view_sync"]["failed"]), "workbench_views_created": len(report["workbench_view_sync"]["created"]), "workbench_views_repaired": len(report["workbench_view_sync"]["repaired"]), "workbench_views_failed": len(report["workbench_view_sync"]["failed"]), "unconfigured_people": report["unconfigured_people"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
