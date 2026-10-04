from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    TABLES,
    Feishu,
    get_tenant_token,
    linked_record_ids,
    sync_one_interview_operator_to_anchors,
    text_value,
    user_ids,
    write_json,
)


def plan_repairs(
    fs: Feishu,
    clear_interview_ids: set[str],
    clear_stale_hidden: bool,
) -> dict[str, Any]:
    interviews = fs.list_records(TABLES["interview"], page_size=500)
    anchors = fs.list_records(TABLES["anchor"], page_size=500)
    anchors_by_id = {
        str(record.get("record_id") or ""): record
        for record in anchors
        if record.get("record_id")
    }

    interview_updates: list[dict[str, Any]] = []
    explicit_interviews: list[dict[str, Any]] = []
    before: list[dict[str, Any]] = []
    found_explicit_ids: set[str] = set()
    skipped_conflicts: list[dict[str, Any]] = []
    for interview in interviews:
        interview_id = str(interview.get("record_id") or "")
        fields = interview.get("fields") or {}
        linked_ids = linked_record_ids(fields.get("关联主播档案"))
        linked_anchors = [anchors_by_id[item] for item in linked_ids if item in anchors_by_id]
        visible_name = text_value(fields.get("对接运营")).strip()
        hidden_ids = user_ids(fields.get("对接运营账号（系统）"))
        linked_operator_ids = {
            user_id
            for anchor in linked_anchors
            for user_id in user_ids((anchor.get("fields") or {}).get("运营经济人"))
        }

        if interview_id in clear_interview_ids:
            found_explicit_ids.add(interview_id)
            before.append(
                {
                    "reason": "explicit_clear",
                    "interview": interview,
                    "linked_anchors": linked_anchors,
                }
            )
            interview_updates.append(
                {
                    "record_id": interview_id,
                    "fields": {"对接运营": "", "对接运营账号（系统）": []},
                }
            )
            explicit_interviews.append(
                {
                    "record_id": interview_id,
                    "fields": {
                        **fields,
                        "对接运营": "",
                        "对接运营账号（系统）": [],
                    },
                }
            )
            continue

        if not clear_stale_hidden or visible_name or not hidden_ids:
            continue
        if linked_operator_ids:
            skipped_conflicts.append(
                {
                    "interview_record_id": interview_id,
                    "candidate": text_value(fields.get("候选人姓名")).strip(),
                    "hidden_operator_ids": hidden_ids,
                    "linked_anchor_operator_ids": sorted(linked_operator_ids),
                }
            )
            continue
        before.append(
            {
                "reason": "stale_hidden_operator",
                "interview": interview,
                "linked_anchors": linked_anchors,
            }
        )
        interview_updates.append(
            {
                "record_id": interview_id,
                "fields": {"对接运营账号（系统）": []},
            }
        )

    missing_explicit_ids = sorted(clear_interview_ids - found_explicit_ids)
    if missing_explicit_ids:
        raise RuntimeError(f"Explicit interview records were not found: {missing_explicit_ids}")

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "plan",
        "summary": {
            "explicit_clears": len(explicit_interviews),
            "stale_hidden_clears": len(interview_updates) - len(explicit_interviews),
            "skipped_conflicts": len(skipped_conflicts),
        },
        "interview_updates": interview_updates,
        "explicit_interviews": explicit_interviews,
        "skipped_conflicts": skipped_conflicts,
        "before": before,
    }


def apply_repairs(fs: Feishu, plan: dict[str, Any]) -> dict[str, Any]:
    updates = plan["interview_updates"]
    interview_results = fs.batch_update(TABLES["interview"], updates, batch_size=100) if updates else []
    if any(result.get("code") != 0 for result in interview_results):
        raise RuntimeError(f"Interview repair failed: {interview_results}")

    linked_results = [
        sync_one_interview_operator_to_anchors(fs, interview)
        for interview in plan["explicit_interviews"]
    ]
    return {
        "mode": "applied",
        "applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": plan["summary"],
        "interview_results": interview_results,
        "linked_results": linked_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Conservative repair for stale 02/03 operator ownership.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--clear-interview-id", action="append", default=[])
    parser.add_argument("--clear-stale-hidden", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    fs = Feishu(get_tenant_token(args.env))
    plan = plan_repairs(fs, set(args.clear_interview_id), args.clear_stale_hidden)
    write_json(args.out, plan)
    result = apply_repairs(fs, plan) if args.apply else plan
    if args.apply:
        write_json(args.out.with_name(f"{args.out.stem}-result.json"), result)
    print(json.dumps({"mode": result["mode"], "summary": result["summary"], "out": str(args.out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
