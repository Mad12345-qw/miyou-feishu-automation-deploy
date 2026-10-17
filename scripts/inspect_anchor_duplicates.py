from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

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


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    return {
        "record_id": str(record.get("record_id") or ""),
        "created_time": record.get("created_time"),
        "last_modified_time": record.get("last_modified_time"),
        "anchor_number": text_value(fields.get("主播编号")).strip(),
        "anchor_name": text_value(fields.get(ANCHOR_NAME_FIELD)).strip(),
        "real_name": text_value(fields.get("真实姓名")).strip(),
        "contact": text_value(fields.get("联系方式")).strip(),
        "source_interview_ids": linked_record_ids(fields.get("来源面试记录")),
        "automation_batch": text_value(fields.get("自动化批次")).strip(),
        "migration_batch": text_value(fields.get("数据迁移批次")).strip(),
        "historical_key": text_value(fields.get("历史来源键")).strip(),
        "recruiter_ids": user_ids(fields.get("招募经济人")),
        "interviewer_ids": user_ids(fields.get("面试官")),
        "operator_ids": user_ids(fields.get("运营经济人")),
        "nonempty_field_count": sum(value not in (None, "", [], {}, False) for value in fields.values()),
    }


def field_conflicts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    field_names = sorted({name for record in records for name in (record.get("fields") or {})})
    for field_name in field_names:
        values: dict[str, Any] = {}
        normalized: set[str] = set()
        for record in records:
            value = (record.get("fields") or {}).get(field_name)
            if value in (None, "", [], {}, False):
                continue
            record_id = str(record.get("record_id") or "")
            values[record_id] = value
            normalized.add(json.dumps(value, ensure_ascii=False, sort_keys=True))
        if len(normalized) > 1:
            result.append({"field": field_name, "values": values})
    return result


def inspect(fs: Feishu) -> dict[str, Any]:
    anchors = fs.list_records(TABLES["anchor"], page_size=500)
    by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_identity: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in anchors:
        fields = record.get("fields") or {}
        number = text_value(fields.get("主播编号")).strip()
        if number:
            by_number[number].append(record)
        for source_id in linked_record_ids(fields.get("来源面试记录")):
            by_source[source_id].append(record)
        name = text_value(fields.get(ANCHOR_NAME_FIELD)).strip() or text_value(fields.get("真实姓名")).strip()
        contact = text_value(fields.get("联系方式")).strip()
        if name and contact:
            by_identity[(name.casefold(), contact)].append(record)

    def groups(
        values: dict[Any, list[dict[str, Any]]],
        key_name: str,
        include_conflicts: bool = False,
    ) -> list[dict[str, Any]]:
        result = []
        for key, rows in sorted(values.items(), key=lambda item: str(item[0])):
            if len(rows) <= 1:
                continue
            group = {key_name: key, "records": [compact_record(row) for row in rows]}
            if include_conflicts:
                group["field_conflicts"] = field_conflicts(rows)
            result.append(group)
        return result

    return {
        "mode": "read_only",
        "anchor_count": len(anchors),
        "duplicate_number_groups": groups(by_number, "anchor_number"),
        "duplicate_source_groups": groups(by_source, "source_interview_id", include_conflicts=True),
        "duplicate_name_contact_groups": groups(by_identity, "name_contact"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect duplicate streamer profiles without changing live data.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = inspect(Feishu(get_tenant_token(args.env)))
    write_json(args.out, report)
    print(
        {
            "anchor_count": report["anchor_count"],
            "duplicate_number_groups": len(report["duplicate_number_groups"]),
            "duplicate_source_groups": len(report["duplicate_source_groups"]),
            "duplicate_name_contact_groups": len(report["duplicate_name_contact_groups"]),
            "out": str(args.out),
        }
    )


if __name__ == "__main__":
    main()
