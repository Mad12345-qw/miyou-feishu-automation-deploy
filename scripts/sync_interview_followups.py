from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    ANCHOR_INTERVIEW_FOLLOWUP_FIELD,
    INTERVIEW_FOLLOWUP_FIELD,
    TABLES,
    Feishu,
    get_tenant_token,
    linked_record_ids,
    text_value,
    write_json,
)


def build_plan(fs: Feishu) -> dict[str, Any]:
    interviews = fs.search_records_by_filter(
        TABLES["interview"],
        [{"field_name": INTERVIEW_FOLLOWUP_FIELD, "operator": "isNotEmpty", "value": []}],
        page_size=500,
    )
    anchors = fs.list_records(TABLES["anchor"], page_size=500)
    anchors_by_id = {
        str(anchor.get("record_id") or ""): anchor
        for anchor in anchors
        if anchor.get("record_id")
    }
    anchors_by_interview: dict[str, set[str]] = defaultdict(set)
    for anchor_id, anchor in anchors_by_id.items():
        for interview_id in linked_record_ids((anchor.get("fields") or {}).get("来源面试记录")):
            anchors_by_interview[interview_id].add(anchor_id)

    values_by_anchor: dict[str, list[dict[str, str]]] = defaultdict(list)
    unresolved: list[dict[str, str]] = []
    multi_anchor_sources: list[dict[str, Any]] = []
    for interview in interviews:
        interview_id = str(interview.get("record_id") or "")
        fields = interview.get("fields") or {}
        anchor_ids = set(linked_record_ids(fields.get("关联主播档案")))
        anchor_ids.update(anchors_by_interview.get(interview_id, set()))
        anchor_ids.intersection_update(anchors_by_id)
        source = {
            "record_id": interview_id,
            "candidate": text_value(fields.get("候选人姓名")).strip(),
            "value": text_value(fields.get(INTERVIEW_FOLLOWUP_FIELD)).strip(),
        }
        if not anchor_ids:
            unresolved.append({"record_id": interview_id, "candidate": source["candidate"]})
            continue
        if len(anchor_ids) > 1:
            multi_anchor_sources.append({**source, "anchor_ids": sorted(anchor_ids)})
        for anchor_id in anchor_ids:
            values_by_anchor[anchor_id].append(source)

    updates: list[dict[str, Any]] = []
    already_synced = 0
    multiple_source_targets: list[dict[str, Any]] = []
    for anchor_id, sources in values_by_anchor.items():
        values: list[str] = []
        for source in sources:
            value = source["value"]
            if value and value not in values:
                values.append(value)
        desired = "\n\n".join(values)
        current = text_value((anchors_by_id[anchor_id].get("fields") or {}).get(ANCHOR_INTERVIEW_FOLLOWUP_FIELD))
        if len(sources) > 1:
            multiple_source_targets.append({"anchor_id": anchor_id, "sources": sources})
        if current == desired:
            already_synced += 1
            continue
        updates.append(
            {
                "record_id": anchor_id,
                "fields": {ANCHOR_INTERVIEW_FOLLOWUP_FIELD: desired},
                "before": current,
                "sources": sources,
            }
        )

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_nonempty": len(interviews),
        "resolved_sources": len(interviews) - len(unresolved),
        "unresolved_sources": len(unresolved),
        "target_anchors": len(values_by_anchor),
        "already_synced": already_synced,
        "planned_updates": len(updates),
        "multi_anchor_sources": multi_anchor_sources,
        "multiple_source_targets": multiple_source_targets,
        "unresolved_sample": unresolved[:100],
        "updates": updates,
    }


def apply_plan(fs: Feishu, plan: dict[str, Any]) -> list[dict[str, Any]]:
    records = [
        {"record_id": item["record_id"], "fields": item["fields"]}
        for item in plan["updates"]
    ]
    return fs.batch_update(TABLES["anchor"], records, batch_size=100) if records else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror interview follow-up notes to linked streamer profiles.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    fs = Feishu(get_tenant_token(args.env))
    plan = build_plan(fs)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    write_json(args.out_dir / f"interview_followup_plan_{stamp}.json", plan)
    results = apply_plan(fs, plan) if args.apply else []
    failed = [result for result in results if result.get("code") != 0]
    report = {
        "mode": "apply" if args.apply else "dry_run",
        "source_nonempty": plan["source_nonempty"],
        "resolved_sources": plan["resolved_sources"],
        "unresolved_sources": plan["unresolved_sources"],
        "target_anchors": plan["target_anchors"],
        "already_synced": plan["already_synced"],
        "planned_updates": plan["planned_updates"],
        "failed_batches": len(failed),
        "results": results,
    }
    write_json(args.out_dir / f"interview_followup_result_{stamp}.json", report)
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
