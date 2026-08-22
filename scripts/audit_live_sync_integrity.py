from __future__ import annotations

import argparse
import json
import os
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    ANCHOR_NAME_FIELD,
    APP_TOKEN,
    CHAIN_NODE_TEMPLATE,
    TABLES,
    TASK_TEMPLATE,
    WORKBENCH_TABLE,
    Feishu,
    get_tenant_token,
    is_demo_batch,
    linked_record_ids,
    list_value,
    text_value,
    user_ids,
    write_json,
)
from sync_missing_personal_entries import SPECS, active_people, list_view_details


TRANSFER_FIELDS = ("通过转入主播", "面试通过，转入主播")
CHILD_SPECS = {
    "node": ("关联主播", 15, "节点类型"),
    "task": ("对应主播", 6, "任务名称"),
    "visual": ("关联主播", 1, "需求标题"),
    "training": ("关联主播", 1, ""),
    "first_live": ("关联主播", 1, ""),
}


def condition_values(value: Any) -> list[str]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
    if not isinstance(parsed, list):
        parsed = [parsed]
    result: list[str] = []
    for item in parsed:
        if isinstance(item, dict):
            item = item.get("id") or item.get("open_id") or item.get("user_id") or item.get("text")
        if item not in (None, ""):
            result.append(str(item))
    return result


def view_binding(view: dict[str, Any]) -> tuple[str, str]:
    conditions = (((view.get("property") or {}).get("filter_info") or {}).get("conditions") or [])
    if len(conditions) != 1:
        return "", ""
    condition = conditions[0]
    values = condition_values(condition.get("value"))
    if condition.get("operator") != "is" or len(values) != 1:
        return "", ""
    return str(condition.get("field_id") or ""), values[0]


def link_url(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or "")
    return ""


def view_url(base_url: str, table_id: str, view_id: str) -> str:
    return f"{base_url.rstrip('/')}/base/{APP_TOKEN}?table={table_id}&view={view_id}"


def normalized_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return value.strip()
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urllib.parse.urlencode(sorted(query)), ""))


def expected_view_name(prefix: str, name: str, suffix: str, user_id: str, duplicate_names: set[str]) -> str:
    display_name = name if name not in duplicate_names else f"{name}（{user_id[-6:]}）"
    return f"{prefix}_{display_name}_{suffix}"[:100]


def expected_row_name(name: str, target: str, user_id: str, duplicate_names: set[str]) -> str:
    display_name = name if name not in duplicate_names else f"{name}（{user_id[-6:]}）"
    return f"个人入口：{display_name}的{target}"


def audit(fs: Feishu, recent_days: int) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def issue(area: str, code: str, **details: Any) -> None:
        issues.append({"area": area, "code": code, **details})

    def warning(area: str, code: str, **details: Any) -> None:
        warnings.append({"area": area, "code": code, **details})

    personnel_records = fs.list_records(TABLES["personnel"], page_size=500)
    personnel_user_records: dict[str, list[str]] = defaultdict(list)
    personnel_user_names: dict[str, str] = {}
    active_name_users: dict[str, set[str]] = defaultdict(set)
    for record in personnel_records:
        fields = record.get("fields") or {}
        name = text_value(fields.get("姓名")).strip()
        ids = user_ids(fields.get("飞书用户"))
        for user_id in ids:
            personnel_user_records[user_id].append(str(record.get("record_id") or ""))
            if name:
                personnel_user_names[user_id] = name
        if text_value(fields.get("在职状态")) == "在职" and text_value(fields.get("账号状态")) == "正常":
            for user_id in ids:
                active_name_users[name].add(user_id)

    for user_id, record_ids in personnel_user_records.items():
        if len(record_ids) > 1:
            issue("personnel", "duplicate_user_id", user_id=user_id, record_ids=record_ids)
    duplicate_names = {name for name, ids in active_name_users.items() if name and len(ids) > 1}
    for name in sorted(duplicate_names):
        warning("personnel", "duplicate_active_name", name=name, user_ids=sorted(active_name_users[name]))

    people = active_people(fs)
    supported_roles = set().union(*(set(item[4]) for item in SPECS))
    for user_id, person in people.items():
        if not str(person.get("name") or "").strip():
            issue("personnel", "active_user_missing_name", user_id=user_id)
        if not set(person.get("roles") or set()).intersection(supported_roles):
            warning("personnel", "active_user_has_no_supported_role", user_id=user_id, name=person.get("name"), roles=sorted(person.get("roles") or []))

    table_keys = sorted({item[0] for item in SPECS})
    field_ids = {
        key: {str(field.get("field_name") or ""): str(field.get("field_id") or "") for field in fs.fields(TABLES[key])}
        for key in table_keys
    }
    views_by_table: dict[str, list[dict[str, Any]]] = {}
    bindings: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    views_by_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for table_key in table_keys:
        detailed: list[dict[str, Any]] = []
        for detail in list_view_details(fs, TABLES[table_key]):
            if detail.get("_detail_error"):
                issue("business_views", "view_detail_read_failed", table=table_key, view_id=detail.get("view_id"), response_code=(detail.get("_detail_error") or {}).get("code"))
                continue
            detailed.append(detail)
            views_by_name[(table_key, str(detail.get("view_name") or ""))].append(detail)
            field_id, user_id = view_binding(detail)
            if field_id and user_id:
                bindings[(table_key, field_id, user_id)].append(detail)
        views_by_table[table_key] = detailed

    desired_rows: dict[str, dict[str, Any]] = {}
    desired_bindings = 0
    for user_id, person in people.items():
        name = str(person.get("name") or "").strip()
        roles = set(person.get("roles") or set())
        if not name:
            continue
        for table_key, field_name, prefix, suffix, accepted_roles, target, label in SPECS:
            if not roles.intersection(accepted_roles):
                continue
            desired_bindings += 1
            field_id = field_ids[table_key].get(field_name, "")
            expected_name = expected_view_name(prefix, name, suffix, user_id, duplicate_names)
            bound = bindings.get((table_key, field_id, user_id), [])
            named = views_by_name.get((table_key, expected_name), [])
            if len(bound) > 1:
                warning("business_views", "duplicate_exact_binding", table=table_key, field=field_name, user_id=user_id, name=name, views=[str(item.get("view_name") or "") for item in bound])
            if not bound:
                if named:
                    actual = [view_binding(item) for item in named]
                    issue("business_views", "named_view_has_wrong_filter", table=table_key, field=field_name, user_id=user_id, name=name, expected_view=expected_name, actual_bindings=actual)
                else:
                    issue("business_views", "missing_personal_business_view", table=table_key, field=field_name, user_id=user_id, name=name, expected_view=expected_name)
                continue
            selected = next((item for item in bound if str(item.get("view_name") or "") == expected_name), bound[0])
            actual_name = str(selected.get("view_name") or "")
            if actual_name != expected_name:
                warning("business_views", "bound_view_uses_nonstandard_name", table=table_key, field=field_name, user_id=user_id, name=name, expected_view=expected_name, actual_view=actual_name)
            row_key = expected_row_name(name, target, user_id, duplicate_names)
            row = {
                "user_id": user_id,
                "name": name,
                "target": target,
                "label": label,
                "table": table_key,
                "field": field_name,
                "view_id": str(selected.get("view_id") or ""),
                "valid_view_ids": [str(item.get("view_id") or "") for item in bound if item.get("view_id")],
            }
            previous = desired_rows.get(row_key)
            if previous and (previous["table"], previous["field"], previous["view_id"]) != (row["table"], row["field"], row["view_id"]):
                issue("workbench", "personal_entry_target_conflict", key=row_key, choices=[previous, row])
                continue
            desired_rows[row_key] = row

    base_url = os.environ.get("FEISHU_BASE_URL", "https://hxyyb89w4s2.feishu.cn").strip().rstrip("/")
    workbench_records = fs.list_records(WORKBENCH_TABLE, page_size=500)
    workbench_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in workbench_records:
        key = text_value((record.get("fields") or {}).get("我要做什么")).strip()
        if key.startswith("个人入口："):
            workbench_by_key[key].append(record)
    for key, rows in workbench_by_key.items():
        if len(rows) > 1:
            issue("workbench", "duplicate_personal_entry_rows", key=key, record_ids=[str(item.get("record_id") or "") for item in rows])
    for key, desired in desired_rows.items():
        rows = workbench_by_key.get(key) or []
        if not rows:
            issue("workbench", "missing_personal_entry_row", key=key, user_id=desired["user_id"], target=desired["target"])
            continue
        fields = rows[0].get("fields") or {}
        actual_users = user_ids(fields.get("员工账号"))
        if actual_users != [desired["user_id"]]:
            issue("workbench", "personal_entry_wrong_employee", key=key, expected_user_id=desired["user_id"], actual_user_ids=actual_users, record_id=rows[0].get("record_id"))
        valid_links = [view_url(base_url, TABLES[desired["table"]], view_id) for view_id in desired["valid_view_ids"]]
        expected_link = view_url(base_url, TABLES[desired["table"]], desired["view_id"])
        actual_link = link_url(fields.get("点这里办理"))
        if normalized_url(actual_link) not in {normalized_url(item) for item in valid_links}:
            issue("workbench", "personal_entry_wrong_link", key=key, expected_link=expected_link, valid_links=valid_links, actual_link=actual_link, record_id=rows[0].get("record_id"))
    for key, rows in workbench_by_key.items():
        if key not in desired_rows:
            assigned = user_ids((rows[0].get("fields") or {}).get("员工账号")) if rows else []
            warning("workbench", "stale_or_nonstandard_personal_entry", key=key, user_ids=assigned, record_ids=[str(item.get("record_id") or "") for item in rows])

    record_table_keys = ["interview", "anchor", *CHILD_SPECS]
    records_by_table: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fs.list_records, TABLES[table_key], 500): table_key
            for table_key in record_table_keys
        }
        for future in as_completed(futures):
            records_by_table[futures[future]] = future.result()

    interviews = records_by_table["interview"]
    anchors = records_by_table["anchor"]
    anchor_by_id = {str(item.get("record_id") or ""): item for item in anchors if item.get("record_id")}
    anchors_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    anchor_numbers: dict[str, list[str]] = defaultdict(list)
    for anchor in anchors:
        anchor_id = str(anchor.get("record_id") or "")
        fields = anchor.get("fields") or {}
        number = text_value(fields.get("主播编号")).strip()
        if number:
            anchor_numbers[number].append(anchor_id)
        for interview_id in linked_record_ids(fields.get("来源面试记录")):
            anchors_by_source[interview_id].append(anchor)
    for number, record_ids in anchor_numbers.items():
        if len(record_ids) > 1:
            issue("anchors", "duplicate_anchor_number", anchor_number=number, record_ids=record_ids)

    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=max(1, recent_days))).timestamp() * 1000)
    recent_interviews = 0
    checked_interviews: dict[str, dict[str, Any]] = {}
    assignment_fields = (("招募人", "招募人账号（系统）"), ("面试官", "面试官账号（系统）"), ("对接运营", "对接运营账号（系统）"))
    for interview in interviews:
        interview_id = str(interview.get("record_id") or "")
        fields = interview.get("fields") or {}
        created_at = fields.get("系统：创建时间")
        invitation_at = fields.get("邀约时间")
        is_recent = any(isinstance(value, (int, float)) and value >= cutoff_ms for value in (created_at, invitation_at))
        if is_recent:
            recent_interviews += 1
        for display_field, account_field in assignment_fields:
            display = text_value(fields.get(display_field)).strip()
            accounts = user_ids(fields.get(account_field))
            if display and not accounts:
                target = issue if is_recent else warning
                target("interviews", "assignment_account_missing", record_id=interview_id, candidate=text_value(fields.get("候选人姓名")).strip(), display_field=display_field, display_value=display, account_field=account_field, recent=is_recent)
            unknown = [user_id for user_id in accounts if user_id not in personnel_user_records]
            if unknown:
                target = issue if text_value(fields.get("候选人姓名")).strip() else warning
                target("interviews", "assignment_account_not_in_personnel", record_id=interview_id, candidate=text_value(fields.get("候选人姓名")).strip(), account_field=account_field, user_ids=unknown)
        candidate = text_value(fields.get("候选人姓名")).strip()
        if any(fields.get(field) is True for field in TRANSFER_FIELDS) and candidate and "体验样本" not in candidate and not is_demo_batch(fields.get("自动化批次")):
            checked_interviews[interview_id] = interview

    canonical_anchor_by_interview: dict[str, dict[str, Any]] = {}
    for interview_id, interview in checked_interviews.items():
        fields = interview.get("fields") or {}
        candidate = text_value(fields.get("候选人姓名")).strip()
        linked_ids = linked_record_ids(fields.get("关联主播档案"))
        source_anchors = anchors_by_source.get(interview_id) or []
        if len(linked_ids) != 1:
            issue("anchors", "checked_interview_link_not_unique", record_id=interview_id, candidate=candidate, linked_anchor_ids=linked_ids)
        if len(source_anchors) != 1:
            issue("anchors", "source_interview_anchor_not_unique", record_id=interview_id, candidate=candidate, source_anchor_ids=[str(item.get("record_id") or "") for item in source_anchors])
        linked_anchor = anchor_by_id.get(linked_ids[0]) if len(linked_ids) == 1 else None
        if linked_ids and not linked_anchor:
            issue("anchors", "linked_anchor_record_missing", record_id=interview_id, candidate=candidate, linked_anchor_ids=linked_ids)
        anchor = linked_anchor or (source_anchors[0] if len(source_anchors) == 1 else None)
        if not anchor:
            continue
        canonical_anchor_by_interview[interview_id] = anchor
        anchor_id = str(anchor.get("record_id") or "")
        anchor_fields = anchor.get("fields") or {}
        expected_accounts = {
            "招募经济人": user_ids(fields.get("招募人账号（系统）")),
            "面试官": user_ids(fields.get("面试官账号（系统）")),
            "运营经济人": user_ids(fields.get("对接运营账号（系统）")),
        }
        for anchor_field, expected in expected_accounts.items():
            actual = user_ids(anchor_fields.get(anchor_field))
            if expected and actual != expected:
                issue("anchors", "anchor_assignment_mismatch", interview_record_id=interview_id, anchor_record_id=anchor_id, candidate=candidate, anchor_field=anchor_field, expected_user_ids=expected, actual_user_ids=actual)
        if fields.get("照片") and not anchor_fields.get("照片"):
            issue("anchors", "interview_photo_not_copied", interview_record_id=interview_id, anchor_record_id=anchor_id, candidate=candidate)

    child_records: dict[str, list[dict[str, Any]]] = {}
    child_by_anchor: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for table_key, (link_field, _expected_count, unique_field) in CHILD_SPECS.items():
        rows = records_by_table[table_key]
        child_records[table_key] = rows
        mapped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            row_id = str(row.get("record_id") or "")
            linked_ids = linked_record_ids((row.get("fields") or {}).get(link_field))
            if not linked_ids:
                warning("children", "child_record_has_no_anchor", table=table_key, record_id=row_id)
            for anchor_id in linked_ids:
                if anchor_id not in anchor_by_id:
                    issue("children", "child_links_missing_anchor", table=table_key, record_id=row_id, anchor_id=anchor_id)
                mapped[anchor_id].append(row)
        if unique_field:
            for anchor_id, anchor_rows in mapped.items():
                values = [text_value((row.get("fields") or {}).get(unique_field)).strip() for row in anchor_rows]
                duplicates = {value: count for value, count in Counter(value for value in values if value).items() if count > 1}
                if duplicates:
                    issue("children", "duplicate_child_business_key", table=table_key, anchor_id=anchor_id, field=unique_field, duplicate_values=duplicates)
        child_by_anchor[table_key] = mapped

    expected_node_types = {item[0] for item in CHAIN_NODE_TEMPLATE}
    expected_task_types = {item[1] for item in TASK_TEMPLATE}
    for interview_id, anchor in canonical_anchor_by_interview.items():
        anchor_id = str(anchor.get("record_id") or "")
        candidate = text_value((checked_interviews[interview_id].get("fields") or {}).get("候选人姓名")).strip()
        for table_key, (_link_field, expected_count, _unique_field) in CHILD_SPECS.items():
            anchor_rows = child_by_anchor[table_key].get(anchor_id) or []
            if table_key == "node":
                actual_keys = {text_value((row.get("fields") or {}).get("节点类型")).strip() for row in anchor_rows}
                missing_keys = sorted(expected_node_types - actual_keys)
            elif table_key == "task":
                actual_keys = {text_value((row.get("fields") or {}).get("任务类型")).strip() for row in anchor_rows}
                missing_keys = sorted(expected_task_types - actual_keys)
            else:
                actual_keys = set()
                missing_keys = [table_key] if not anchor_rows else []
            if missing_keys:
                issue(
                    "children",
                    "generated_anchor_child_count_mismatch",
                    interview_record_id=interview_id,
                    anchor_record_id=anchor_id,
                    candidate=candidate,
                    table=table_key,
                    expected=expected_count,
                    actual=len(anchor_rows),
                    missing_keys=missing_keys,
                )

    issue_counts = Counter(f"{item['area']}:{item['code']}" for item in issues)
    warning_counts = Counter(f"{item['area']}:{item['code']}" for item in warnings)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "read_only",
        "status": "passed" if not issues else "failed",
        "summary": {
            "active_people": len(people),
            "personnel_records": len(personnel_records),
            "desired_business_view_bindings": desired_bindings,
            "desired_workbench_rows": len(desired_rows),
            "interviews": len(interviews),
            "recent_interviews": recent_interviews,
            "checked_interviews": len(checked_interviews),
            "anchors": len(anchors),
            "child_records": {key: len(value) for key, value in child_records.items()},
            "issue_count": len(issues),
            "warning_count": len(warnings),
        },
        "issue_counts": dict(sorted(issue_counts.items())),
        "warning_counts": dict(sorted(warning_counts.items())),
        "issues": issues,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only live synchronization and data-integrity audit.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--recent-days", type=int, default=30)
    args = parser.parse_args()
    fs = Feishu(get_tenant_token(args.env))
    report = audit(fs, args.recent_days)
    write_json(args.out, report)
    print(json.dumps({"status": report["status"], "summary": report["summary"], "issue_counts": report["issue_counts"], "warning_counts": report["warning_counts"], "out": str(args.out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
