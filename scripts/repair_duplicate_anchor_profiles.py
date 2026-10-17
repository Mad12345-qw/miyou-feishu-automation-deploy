from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from inspect_anchor_duplicates import field_conflicts
from miyou_system_automation import (
    ANCHOR_NAME_FIELD,
    TABLES,
    Feishu,
    get_tenant_token,
    linked_record_ids,
    text_value,
    user_ids,
    write_json,
)
from repair_live_data_integrity import (
    CHILD_SPECS,
    EMPTY_VALUES,
    has_business_progress,
    load_records,
    plan_duplicate_child_cleanup,
)


ANCHOR_PROGRESS_DEFAULTS = {
    "主播状态": {"", "待开播"},
    "主阶段": {"", "待建联"},
    "设备交付": {"", "未交付"},
    "视觉达标": {"", "未开始"},
    "培训达标状态": {"", "未开始"},
    "3分钟录屏状态": {"", "未录制"},
    "首播状态": {"", "未安排"},
    "系统验收状态": {"", "待验收"},
}
ANCHOR_PROGRESS_FIELDS = {
    "核心顾虑点",
    "面试官跟进记录",
    "运营跟进日报",
    "照片",
    "联系方式",
    *ANCHOR_PROGRESS_DEFAULTS,
}
OWNER_FIELDS = {
    "招募经济人": "招募人账号（系统）",
    "面试官": "面试官账号（系统）",
    "运营经济人": "对接运营账号（系统）",
}
PROTECTED_MERGE_FIELDS = {"主播编号", ANCHOR_NAME_FIELD, "来源面试记录", "自动化批次"}
MERGED_TEXT_FIELDS = {"核心顾虑点", "面试官跟进记录", "运营跟进日报"}
MERGED_LIST_FIELDS = {"照片"}


def batch_date(fields: dict[str, Any]) -> int:
    match = re.search(r"20\d{6}", text_value(fields.get("自动化批次")))
    return int(match.group(0)) if match else 0


def anchor_progress_score(fields: dict[str, Any]) -> int:
    score = 0
    for field_name in ANCHOR_PROGRESS_FIELDS:
        value = fields.get(field_name)
        if field_name in ANCHOR_PROGRESS_DEFAULTS:
            if text_value(value).strip() not in ANCHOR_PROGRESS_DEFAULTS[field_name]:
                score += 2
        elif value not in EMPTY_VALUES:
            score += 2 if field_name in {"面试官跟进记录", "运营跟进日报"} else 1
    return score


def nonempty_count(fields: dict[str, Any]) -> int:
    return sum(value not in (None, "", [], {}, False) for value in fields.values())


def merge_text(current: Any, incoming: Any) -> str:
    current_text = text_value(current).strip()
    incoming_text = text_value(incoming).strip()
    if not current_text:
        return incoming_text
    if not incoming_text or incoming_text in current_text:
        return current_text
    if current_text in incoming_text:
        return incoming_text
    return f"{current_text}\n[合并自重复档案] {incoming_text}"


def merge_list(current: Any, incoming: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in [*(current or []), *(incoming or [])]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def build_plan(records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    interviews = records["interview"]
    interview_by_id = {
        str(row.get("record_id") or ""): row
        for row in interviews
        if row.get("record_id")
    }
    anchors = records["anchor"]
    anchor_by_id = {
        str(row.get("record_id") or ""): row
        for row in anchors
        if row.get("record_id")
    }
    anchors_by_source: dict[str, list[str]] = defaultdict(list)
    for anchor_id, anchor in anchor_by_id.items():
        for interview_id in linked_record_ids((anchor.get("fields") or {}).get("来源面试记录")):
            anchors_by_source[interview_id].append(anchor_id)

    child_rows_by_anchor: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for table_key, (link_field, _business_field) in CHILD_SPECS.items():
        for row in records[table_key]:
            for anchor_id in linked_record_ids((row.get("fields") or {}).get(link_field)):
                child_rows_by_anchor[anchor_id].append((table_key, row))

    updates: dict[str, dict[str, dict[str, Any]]] = {
        key: {} for key in ["interview", "anchor", *CHILD_SPECS]
    }
    deletes: dict[str, set[str]] = {key: set() for key in ["anchor", *CHILD_SPECS]}
    groups: list[dict[str, Any]] = []
    for interview_id, anchor_ids in sorted(anchors_by_source.items()):
        if len(anchor_ids) <= 1:
            continue
        interview = interview_by_id.get(interview_id)
        if not interview:
            raise RuntimeError(f"Duplicate streamer profiles reference missing interview {interview_id}")
        interview_fields = interview.get("fields") or {}
        linked_ids = set(linked_record_ids(interview_fields.get("关联主播档案")))

        candidates: list[dict[str, Any]] = []
        for anchor_id in anchor_ids:
            anchor = anchor_by_id[anchor_id]
            fields = anchor.get("fields") or {}
            child_progress = sum(
                1
                for table_key, row in child_rows_by_anchor.get(anchor_id, [])
                if has_business_progress(table_key, row.get("fields") or {})
            )
            candidates.append(
                {
                    "record_id": anchor_id,
                    "record": anchor,
                    "child_progress": child_progress,
                    "anchor_progress": anchor_progress_score(fields),
                    "nonempty": nonempty_count(fields),
                    "currently_linked": anchor_id in linked_ids,
                    "batch_date": batch_date(fields),
                }
            )
        canonical_info = sorted(
            candidates,
            key=lambda item: (
                -item["child_progress"],
                -item["anchor_progress"],
                -item["nonempty"],
                -int(item["currently_linked"]),
                item["batch_date"],
                item["record_id"],
            ),
        )[0]
        canonical_id = canonical_info["record_id"]
        canonical = anchor_by_id[canonical_id]
        canonical_fields = canonical.get("fields") or {}
        canonical_changes: dict[str, Any] = {"来源面试记录": [interview_id]}
        copied_fields: list[dict[str, str]] = []
        for source_info in candidates:
            source_id = source_info["record_id"]
            if source_id == canonical_id:
                continue
            for field_name, value in (source_info["record"].get("fields") or {}).items():
                if field_name in PROTECTED_MERGE_FIELDS or value in EMPTY_VALUES:
                    continue
                current_value = canonical_changes.get(field_name, canonical_fields.get(field_name))
                if field_name in MERGED_TEXT_FIELDS:
                    merged = merge_text(current_value, value)
                    if merged != text_value(current_value).strip():
                        canonical_changes[field_name] = merged
                        copied_fields.append({"field": field_name, "from": source_id})
                elif field_name in MERGED_LIST_FIELDS and isinstance(value, list):
                    merged = merge_list(current_value if isinstance(current_value, list) else [], value)
                    if merged != (current_value if isinstance(current_value, list) else []):
                        canonical_changes[field_name] = merged
                        copied_fields.append({"field": field_name, "from": source_id})
                elif current_value in EMPTY_VALUES:
                    canonical_changes[field_name] = value
                    copied_fields.append({"field": field_name, "from": source_id})

        for anchor_field, interview_field in OWNER_FIELDS.items():
            ids: list[str] = []
            for value in [
                canonical_fields.get(anchor_field),
                *[(item["record"].get("fields") or {}).get(anchor_field) for item in candidates],
                interview_fields.get(interview_field),
            ]:
                for user_id in user_ids(value):
                    if user_id not in ids:
                        ids.append(user_id)
            if ids:
                canonical_changes[anchor_field] = [{"id": user_id} for user_id in ids]

        updates["anchor"][canonical_id] = canonical_changes
        updates["interview"][interview_id] = {
            "关联主播档案": [canonical_id],
            "系统：已生成主播档案": True,
            "系统处理状态": "已合并重复主播档案",
            "系统处理备注": "系统已保留业务进度最完整的主播档案，并合并重复记录。",
        }
        removed_ids = [anchor_id for anchor_id in anchor_ids if anchor_id != canonical_id]
        for removed_id in removed_ids:
            deletes["anchor"].add(removed_id)
            for table_key, row in child_rows_by_anchor.get(removed_id, []):
                row_id = str(row.get("record_id") or "")
                link_field = CHILD_SPECS[table_key][0]
                updates[table_key].setdefault(row_id, {})[link_field] = [canonical_id]

        groups.append(
            {
                "source_interview_id": interview_id,
                "candidate_name": text_value(interview_fields.get("候选人姓名")).strip(),
                "canonical_anchor_id": canonical_id,
                "deleted_anchor_ids": removed_ids,
                "scores": [
                    {key: value for key, value in item.items() if key != "record"}
                    for item in candidates
                ],
                "copied_fields": copied_fields,
                "field_conflicts": field_conflicts([anchor_by_id[anchor_id] for anchor_id in anchor_ids]),
            }
        )

    duplicate_child_actions, protected_duplicate_children = plan_duplicate_child_cleanup(
        records,
        updates,
        deletes,
    )
    return {
        "groups": groups,
        "duplicate_child_actions": duplicate_child_actions,
        "protected_duplicate_children": protected_duplicate_children,
        "updates": updates,
        "deletes": {key: sorted(values) for key, values in deletes.items()},
    }


def affected_snapshot(records: dict[str, list[dict[str, Any]]], plan: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table_key in ["interview", "anchor", *CHILD_SPECS]:
        ids = set(plan["updates"].get(table_key, {})) | set(plan["deletes"].get(table_key, []))
        result[table_key] = [
            row for row in records[table_key]
            if str(row.get("record_id") or "") in ids
        ]
    return result


def apply_plan(fs: Feishu, plan: dict[str, Any]) -> dict[str, Any]:
    update_batches: dict[str, int] = {}
    for table_key in ["anchor", "interview", *CHILD_SPECS]:
        deleted = set(plan["deletes"].get(table_key, []))
        rows = [
            {"record_id": record_id, "fields": fields}
            for record_id, fields in plan["updates"].get(table_key, {}).items()
            if record_id not in deleted
        ]
        results = fs.batch_update(TABLES[table_key], rows, batch_size=100) if rows else []
        if any(result.get("code") != 0 for result in results):
            raise RuntimeError(f"Update failed for {table_key}: {results}")
        update_batches[table_key] = len(results)

    delete_batches: dict[str, int] = {}
    for table_key in [*CHILD_SPECS, "anchor"]:
        record_ids = plan["deletes"].get(table_key, [])
        results = fs.batch_delete(TABLES[table_key], record_ids, batch_size=100) if record_ids else []
        if any(result.get("code") != 0 for result in results):
            raise RuntimeError(f"Delete failed for {table_key}: {results}")
        delete_batches[table_key] = len(results)
    return {"update_batches": update_batches, "delete_batches": delete_batches}


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely merge duplicate streamer profiles that share one interview.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    fs = Feishu(get_tenant_token(args.env))
    records = load_records(fs)
    plan = build_plan(records)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_path = args.out_dir / f"duplicate_anchor_merge_plan_{stamp}.json"
    write_json(plan_path, plan)
    summary: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "groups": len(plan["groups"]),
        "deleted_anchors": len(plan["deletes"]["anchor"]),
        "duplicate_child_actions": len(plan["duplicate_child_actions"]),
        "protected_duplicate_children": len(plan["protected_duplicate_children"]),
        "updates": {key: len(value) for key, value in plan["updates"].items()},
        "deletes": {key: len(value) for key, value in plan["deletes"].items()},
        "plan": str(plan_path),
    }
    if args.apply:
        snapshot_path = args.out_dir / f"duplicate_anchor_snapshot_before_{stamp}.json"
        write_json(snapshot_path, affected_snapshot(records, plan))
        summary["snapshot"] = str(snapshot_path)
        summary["result"] = apply_plan(fs, plan)
    result_path = args.out_dir / f"duplicate_anchor_merge_result_{stamp}.json"
    write_json(result_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
