from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from miyou_system_automation import APP_TOKEN, Feishu, TABLES, get_tenant_token, text_value, user_ids, write_json
from sync_missing_personal_entries import SPECS, active_people, list_view_details, personal_display_name, view_filter_binding


WORKBENCH_TABLE = os.environ.get("FEISHU_WORKBENCH_TABLE_ID", "tblIcblT5703VGvp").strip()


def link_value(value: Any) -> str:
    return str(value.get("link") or "") if isinstance(value, dict) else ""


def sync_missing_workbench_rows(fs: Feishu, out_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    people = active_people(fs)
    name_counts = Counter(str(person.get("name") or "").strip() for person in people.values())
    duplicate_names = {name for name, count in name_counts.items() if name and count > 1}

    table_keys = sorted({item[0] for item in SPECS})
    field_ids = {
        key: {str(field.get("field_name") or ""): str(field.get("field_id") or "") for field in fs.fields(TABLES[key])}
        for key in table_keys
    }
    views_by_binding: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for table_key in table_keys:
        for detail in list_view_details(fs, TABLES[table_key]):
            if detail.get("_detail_error"):
                continue
            field_id, user_id = view_filter_binding(detail)
            if field_id and user_id:
                views_by_binding[(table_key, field_id, user_id)].append(detail)

    base_url = os.environ.get("FEISHU_BASE_URL", "https://hxyyb89w4s2.feishu.cn").strip().rstrip("/")
    desired: dict[str, dict[str, Any]] = {}
    valid_links_by_key: dict[str, set[str]] = defaultdict(set)
    missing_views: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    for user_id, person in people.items():
        name = str(person.get("name") or "").strip()
        roles = set(person.get("roles") or set())
        if not name:
            continue
        for table_key, field_name, _prefix, _suffix, accepted_roles, target, label in SPECS:
            if not roles.intersection(accepted_roles):
                continue
            field_id = field_ids[table_key].get(field_name, "")
            bound_views = views_by_binding.get((table_key, field_id, user_id)) or []
            if not bound_views:
                missing_views.append({"name": name, "user_id": user_id, "table": table_key, "field": field_name, "target": target})
                continue
            selected = min(bound_views, key=lambda item: len(str(item.get("view_name") or "")))
            view_id = str(selected.get("view_id") or "")
            display_name = personal_display_name(name, user_id, duplicate_names)
            key = f"个人入口：{display_name}的{target}"
            valid_links_by_key[key].update(
                f"{base_url}/base/{APP_TOKEN}?table={TABLES[table_key]}&view={str(view.get('view_id') or '')}"
                for view in bound_views
                if view.get("view_id")
            )
            fields = {
                "我要做什么": key,
                "谁来操作": "本人",
                "操作内容": f"{display_name}直接查看自己的{target}",
                "系统自动": "系统按飞书人员账号自动筛选本人记录",
                "完成时限": "每天使用",
                "点这里办理": {
                    "link": f"{base_url}/base/{APP_TOKEN}?table={TABLES[table_key]}&view={view_id}",
                    "text": label,
                },
                "员工账号": [{"id": user_id}],
            }
            previous = desired.get(key)
            if previous and link_value(previous["点这里办理"]) != link_value(fields["点这里办理"]):
                conflicts.append({"key": key, "first_link": link_value(previous["点这里办理"]), "second_link": link_value(fields["点这里办理"])})
                continue
            desired[key] = fields

    existing_records = fs.list_records(WORKBENCH_TABLE, page_size=500)
    existing_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in existing_records:
        key = text_value((record.get("fields") or {}).get("我要做什么")).strip()
        if key.startswith("个人入口："):
            existing_by_key[key].append(record)

    duplicate_rows = [
        {"key": key, "record_ids": [str(record.get("record_id") or "") for record in records]}
        for key, records in existing_by_key.items()
        if len(records) > 1
    ]
    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    stale_record_ids: list[str] = []
    unchanged: list[str] = []
    for key, records in existing_by_key.items():
        if key in desired:
            continue
        for record in records:
            fields = record.get("fields") or {}
            if (
                text_value(fields.get("谁来操作")) == "本人"
                and text_value(fields.get("系统自动")) == "系统按飞书人员账号自动筛选本人记录"
                and record.get("record_id")
            ):
                stale_record_ids.append(str(record["record_id"]))
    for key, fields in desired.items():
        records = existing_by_key.get(key) or []
        if not records:
            creates.append({"fields": fields})
            continue
        record = records[0]
        current = record.get("fields") or {}
        current_link = link_value(current.get("点这里办理"))
        if current_link in valid_links_by_key.get(key, set()):
            fields["点这里办理"]["link"] = current_link
        same = (
            user_ids(current.get("员工账号")) == user_ids(fields.get("员工账号"))
            and link_value(current.get("点这里办理")) == link_value(fields.get("点这里办理"))
            and text_value(current.get("谁来操作")) == fields["谁来操作"]
            and text_value(current.get("操作内容")) == fields["操作内容"]
            and text_value(current.get("系统自动")) == fields["系统自动"]
            and text_value(current.get("完成时限")) == fields["完成时限"]
        )
        if same:
            unchanged.append(key)
        else:
            updates.append({"record_id": record["record_id"], "fields": fields})

    create_results = [] if dry_run or not creates else fs.batch_create(WORKBENCH_TABLE, creates, batch_size=500)
    update_results = [] if dry_run or not updates else fs.batch_update(WORKBENCH_TABLE, updates, batch_size=500)
    delete_results = [] if dry_run or not stale_record_ids else fs.batch_delete(WORKBENCH_TABLE, stale_record_ids, batch_size=500)
    failed = [result for result in [*create_results, *update_results, *delete_results] if result.get("code") != 0]
    report = {
        "mode": "dry_run" if dry_run else "apply",
        "people": len(people),
        "desired_rows": len(desired),
        "planned_created": len(creates),
        "planned_repaired": len(updates),
        "planned_hidden": len(stale_record_ids),
        "created": 0 if dry_run else sum(len(((result.get("data") or {}).get("records") or [])) for result in create_results if result.get("code") == 0),
        "repaired": 0 if dry_run else sum(len(((result.get("data") or {}).get("records") or [])) for result in update_results if result.get("code") == 0),
        "hidden": 0 if dry_run else (len(stale_record_ids) if delete_results and all(result.get("code") == 0 for result in delete_results) else 0),
        "unchanged": len(unchanged),
        "missing_views": missing_views,
        "conflicts": conflicts,
        "duplicate_rows": duplicate_rows,
        "duplicate_view_bindings": sum(1 for views in views_by_binding.values() if len(views) > 1),
        "failed": failed,
        "create_results": create_results,
        "update_results": update_results,
        "delete_results": delete_results,
    }
    write_json(out_dir / "sync_missing_workbench_rows_result.json", report)
    return report


def main() -> None:
    fs = Feishu(get_tenant_token(Path("feishu/.env.local")))
    report = sync_missing_workbench_rows(fs, Path("scripts/runtime"))
    keys = ("people", "desired_rows", "created", "repaired", "unchanged", "missing_views", "conflicts", "duplicate_rows")
    print(json.dumps({key: report[key] for key in keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
