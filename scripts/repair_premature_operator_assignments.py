from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    TABLES,
    Feishu,
    cleared_select_value,
    get_tenant_token,
    read_record,
    should_clear_premature_operator,
    text_value,
    user_ids,
    write_json,
)


def snapshot_row(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    return {
        "record_id": str(record.get("record_id") or ""),
        "candidate": text_value(fields.get("候选人姓名")).strip(),
        "invitation_time": fields.get("邀约时间"),
        "interview_status": text_value(fields.get("面试状态")).strip(),
        "interview_started_at": fields.get("面试开始时间"),
        "interview_ended_at": fields.get("面试结束时间"),
        "operator_visible": fields.get("对接运营"),
        "operator_user_ids": user_ids(fields.get("对接运营账号（系统）")),
        "linked_anchor_profiles": fields.get("关联主播档案"),
        "transfer_checked": bool(fields.get("通过转入主播") or fields.get("面试通过，转入主播")),
        "created_by": fields.get("系统：创建人"),
        "created_at": fields.get("系统：创建时间"),
        "modified_by": fields.get("系统：最后修改人"),
        "modified_at": fields.get("系统：最后修改时间"),
    }


def build_plan(fs: Feishu) -> list[dict[str, Any]]:
    return [
        snapshot_row(record)
        for record in fs.list_records(TABLES["interview"], page_size=500)
        if should_clear_premature_operator(record.get("fields") or {})
    ]


def apply_plan(fs: Feishu, plan: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    updates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in plan:
        record_id = str(item["record_id"])
        current = read_record(fs, TABLES["interview"], record_id)
        fields = current.get("fields") or {}
        if not should_clear_premature_operator(fields):
            skipped.append({"record_id": record_id, "reason": "record_changed_after_snapshot"})
            continue
        updates.append(
            {
                "record_id": record_id,
                "fields": {
                    "对接运营": cleared_select_value(fields.get("对接运营")),
                    "对接运营账号（系统）": [],
                },
            }
        )
    results = fs.batch_update(TABLES["interview"], updates, batch_size=100) if updates else []
    return updates, skipped + [{"batch_result": result} for result in results if result.get("code") != 0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Clear confirmed form-default operators before interview completion.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    fs = Feishu(get_tenant_token(args.env))
    plan = build_plan(fs)
    updates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if args.apply:
        updates, skipped = apply_plan(fs, plan)
    report = {
        "mode": "apply" if args.apply else "dry_run",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "planned_records": len(plan),
        "updated_records": len(updates),
        "skipped": skipped,
        "snapshot": plan,
    }
    write_json(args.out, report)
    print(json.dumps({key: report[key] for key in ("mode", "planned_records", "updated_records", "skipped")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
