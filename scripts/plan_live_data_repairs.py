from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    ANCHOR_DISPLAY_FIELD,
    ANCHOR_NAME_FIELD,
    TABLES,
    Feishu,
    anchor_display_name,
    get_tenant_token,
    linked_record_ids,
    text_value,
    write_json,
)


CHILD_SPECS = {
    "node": ("关联主播", "节点类型"),
    "task": ("对应主播", "任务类型"),
    "visual": ("关联主播", "singleton"),
    "training": ("关联主播", "singleton"),
    "first_live": ("关联主播", "singleton"),
}


def anchor_names(fields: dict[str, Any]) -> set[str]:
    return {
        value
        for value in (
            text_value(fields.get(ANCHOR_NAME_FIELD)).strip(),
            text_value(fields.get("主播昵称")).strip(),
            text_value(fields.get("真实姓名")).strip(),
        )
        if value
    }


def display_name_part(value: str) -> str:
    return value.split(" · ", 1)[0].strip() if value else ""


def audit(fs: Feishu) -> dict[str, Any]:
    table_keys = ["interview", "anchor", *CHILD_SPECS]
    records: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fs.list_records, TABLES[key], 500): key for key in table_keys}
        for future in as_completed(futures):
            records[futures[future]] = future.result()

    anchors = records["anchor"]
    anchors_by_id = {str(row.get("record_id") or ""): row for row in anchors if row.get("record_id")}
    by_display: dict[str, set[str]] = defaultdict(set)
    by_number: dict[str, set[str]] = defaultdict(set)
    by_name: dict[str, set[str]] = defaultdict(set)
    for anchor_id, anchor in anchors_by_id.items():
        fields = anchor.get("fields") or {}
        display = anchor_display_name(fields)
        number = text_value(fields.get("主播编号")).strip()
        if display:
            by_display[display].add(anchor_id)
        if number:
            by_number[number].add(anchor_id)
        for name in anchor_names(fields):
            by_name[name].add(anchor_id)

    children_by_missing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for table_key, (link_field, business_field) in CHILD_SPECS.items():
        for row in records[table_key]:
            fields = row.get("fields") or {}
            linked_ids = linked_record_ids(fields.get(link_field))
            business_key = text_value(fields.get(business_field)).strip() if business_field != "singleton" else "singleton"
            for anchor_id in linked_ids:
                if anchor_id in anchors_by_id:
                    current_keys[(table_key, anchor_id)].add(business_key)
                else:
                    children_by_missing[anchor_id].append(
                        {
                            "table": table_key,
                            "record_id": str(row.get("record_id") or ""),
                            "link_field": link_field,
                            "business_key": business_key,
                            "display": text_value(fields.get(ANCHOR_DISPLAY_FIELD)).strip(),
                        }
                    )

    relink_groups: list[dict[str, Any]] = []
    unresolved_groups: list[dict[str, Any]] = []
    for missing_id, child_rows in sorted(children_by_missing.items()):
        displays = sorted({row["display"] for row in child_rows if row["display"]})
        candidates: set[str] = set()
        reasons: list[str] = []
        for display in displays:
            exact = by_display.get(display) or set()
            if len(exact) == 1:
                candidates.update(exact)
                reasons.append("exact_display")
            number = display.split(" · ", 1)[1].strip() if " · " in display else display
            number_matches = by_number.get(number) or set()
            if len(number_matches) == 1:
                candidates.update(number_matches)
                reasons.append("anchor_number")
            name_matches = by_name.get(display_name_part(display)) or set()
            if len(name_matches) == 1:
                candidates.update(name_matches)
                reasons.append("unique_name")
        group = {
            "missing_anchor_id": missing_id,
            "child_count": len(child_rows),
            "tables": dict(sorted(Counter(row["table"] for row in child_rows).items())),
            "displays": displays,
            "candidate_anchor_ids": sorted(candidates),
            "match_reasons": sorted(set(reasons)),
        }
        if len(candidates) != 1:
            group["rows"] = child_rows
            unresolved_groups.append(group)
            continue
        canonical_id = next(iter(candidates))
        canonical = anchors_by_id[canonical_id]
        safe_rows: list[dict[str, Any]] = []
        conflicting_rows: list[dict[str, Any]] = []
        simulated_keys = {key: set(values) for key, values in current_keys.items()}
        for row in child_rows:
            key = (row["table"], canonical_id)
            if row["business_key"] in simulated_keys.setdefault(key, set()):
                conflicting_rows.append(row)
            else:
                safe_rows.append(row)
                simulated_keys[key].add(row["business_key"])
        group.update(
            {
                "canonical_anchor_id": canonical_id,
                "canonical_anchor_display": anchor_display_name(canonical.get("fields") or {}),
                "safe_relink_rows": safe_rows,
                "conflicting_rows": conflicting_rows,
            }
        )
        relink_groups.append(group)

    interviews = records["interview"]
    anchors_by_source: dict[str, list[str]] = defaultdict(list)
    for anchor_id, anchor in anchors_by_id.items():
        for interview_id in linked_record_ids((anchor.get("fields") or {}).get("来源面试记录")):
            anchors_by_source[interview_id].append(anchor_id)
    duplicate_sources: list[dict[str, Any]] = []
    for interview in interviews:
        interview_id = str(interview.get("record_id") or "")
        source_ids = anchors_by_source.get(interview_id) or []
        if len(source_ids) <= 1:
            continue
        fields = interview.get("fields") or {}
        linked = linked_record_ids(fields.get("关联主播档案"))
        duplicate_sources.append(
            {
                "interview_record_id": interview_id,
                "candidate": text_value(fields.get("候选人姓名")).strip(),
                "linked_anchor_ids": linked,
                "source_anchor_ids": source_ids,
                "anchors": [
                    {
                        "record_id": anchor_id,
                        "display": anchor_display_name((anchors_by_id[anchor_id].get("fields") or {})),
                        "batch": text_value((anchors_by_id[anchor_id].get("fields") or {}).get("自动化批次")).strip(),
                        "child_counts": {
                            table_key: sum(1 for row in records[table_key] if anchor_id in linked_record_ids((row.get("fields") or {}).get(link_field)))
                            for table_key, (link_field, _business_field) in CHILD_SPECS.items()
                        },
                    }
                    for anchor_id in source_ids
                ],
            }
        )

    number_groups: dict[str, list[str]] = defaultdict(list)
    for anchor_id, anchor in anchors_by_id.items():
        number = text_value((anchor.get("fields") or {}).get("主播编号")).strip()
        if number:
            number_groups[number].append(anchor_id)
    duplicate_numbers = [
        {
            "anchor_number": number,
            "anchors": [
                {
                    "record_id": anchor_id,
                    "display": anchor_display_name((anchors_by_id[anchor_id].get("fields") or {})),
                    "batch": text_value((anchors_by_id[anchor_id].get("fields") or {}).get("自动化批次")).strip(),
                }
                for anchor_id in anchor_ids
            ],
        }
        for number, anchor_ids in number_groups.items()
        if len(anchor_ids) > 1
    ]

    return {
        "mode": "read_only",
        "summary": {
            "missing_anchor_groups": len(children_by_missing),
            "matched_relink_groups": len(relink_groups),
            "unresolved_relink_groups": len(unresolved_groups),
            "safe_relink_rows": sum(len(group["safe_relink_rows"]) for group in relink_groups),
            "conflicting_rows": sum(len(group["conflicting_rows"]) for group in relink_groups),
            "duplicate_source_interviews": len(duplicate_sources),
            "duplicate_anchor_number_groups": len(duplicate_numbers),
        },
        "relink_groups": relink_groups,
        "unresolved_groups": unresolved_groups,
        "duplicate_sources": duplicate_sources,
        "duplicate_numbers": duplicate_numbers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only, evidence-based repair plan for live Base data.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit(Feishu(get_tenant_token(args.env)))
    write_json(args.out, report)
    print(json.dumps({"summary": report["summary"], "out": str(args.out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
