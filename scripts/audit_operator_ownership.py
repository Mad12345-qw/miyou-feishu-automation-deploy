from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    TABLES,
    Feishu,
    get_tenant_token,
    linked_record_ids,
    text_value,
    user_ids,
    write_json,
)


def iso_time(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def user_names(value: Any, people: dict[str, str]) -> list[str]:
    names: list[str] = []
    for user_id in user_ids(value):
        names.append(people.get(user_id) or text_value(value).strip() or user_id)
    return names


def audit(fs: Feishu, invitation_day: str = "") -> dict[str, Any]:
    personnel = fs.list_records(TABLES["personnel"], page_size=500)
    people = {
        user_id: text_value((record.get("fields") or {}).get("姓名")).strip()
        for record in personnel
        for user_id in user_ids((record.get("fields") or {}).get("飞书用户"))
    }
    interviews = fs.list_records(TABLES["interview"], page_size=500)
    anchors = fs.list_records(TABLES["anchor"], page_size=500)
    anchors_by_id = {
        str(record.get("record_id") or ""): record
        for record in anchors
        if record.get("record_id")
    }

    rows: list[dict[str, Any]] = []
    issue_counts = {
        "hidden_operator_without_visible_operator": 0,
        "linked_anchor_operator_would_backfill_blank_interview": 0,
        "linked_anchor_operator_with_blank_visible_operator": 0,
        "linked_operator_mismatch": 0,
        "operator_present_before_completed_interview": 0,
    }
    for interview in interviews:
        fields = interview.get("fields") or {}
        invitation_at = fields.get("邀约时间")
        invitation_iso = iso_time(invitation_at)
        if invitation_day and not invitation_iso.startswith(invitation_day):
            continue
        linked_ids = linked_record_ids(fields.get("关联主播档案"))
        interview_ids = user_ids(fields.get("对接运营账号（系统）"))
        interview_visible = text_value(fields.get("对接运营")).strip()
        interview_names = user_names(fields.get("对接运营账号（系统）"), people)
        if not interview_names:
            interview_names = [interview_visible] if interview_visible else []
        if interview_ids and not interview_visible:
            issue_counts["hidden_operator_without_visible_operator"] += 1

        linked_anchors: list[dict[str, Any]] = []
        for anchor_id in linked_ids:
            anchor = anchors_by_id.get(anchor_id)
            if not anchor:
                linked_anchors.append({"record_id": anchor_id, "missing": True})
                continue
            anchor_fields = anchor.get("fields") or {}
            anchor_ids = user_ids(anchor_fields.get("运营经济人"))
            anchor_names = user_names(anchor_fields.get("运营经济人"), people)
            linked_anchors.append(
                {
                    "record_id": anchor_id,
                    "streamer": text_value(anchor_fields.get("主播名字")).strip(),
                    "operator_ids": anchor_ids,
                    "operator_names": anchor_names,
                    "modified_by": text_value(anchor_fields.get("系统：最后修改人")).strip(),
                    "modified_at": iso_time(anchor_fields.get("系统：最后修改时间")),
                }
            )
            if anchor_ids and not interview_ids:
                issue_counts["linked_anchor_operator_would_backfill_blank_interview"] += 1
            if anchor_ids and not interview_visible:
                issue_counts["linked_anchor_operator_with_blank_visible_operator"] += 1
            if set(anchor_ids) != set(interview_ids) and (anchor_ids or interview_ids):
                issue_counts["linked_operator_mismatch"] += 1

        has_operator = bool(interview_ids or any(item.get("operator_ids") for item in linked_anchors))
        interview_completed = bool(fields.get("面试开始时间") or fields.get("面试结束时间")) or text_value(fields.get("面试状态")).strip() in {"已完成", "已面试"}
        if has_operator and not interview_completed:
            issue_counts["operator_present_before_completed_interview"] += 1

        if has_operator or linked_ids:
            rows.append(
                {
                    "interview_record_id": str(interview.get("record_id") or ""),
                    "candidate": text_value(fields.get("候选人姓名")).strip(),
                    "invitation_at": invitation_iso,
                    "interview_status": text_value(fields.get("面试状态")).strip(),
                    "interview_started_at": iso_time(fields.get("面试开始时间")),
                    "interview_ended_at": iso_time(fields.get("面试结束时间")),
                    "transfer_checked": bool(fields.get("通过转入主播") is True or fields.get("面试通过，转入主播") is True),
                    "interview_operator_visible": interview_visible,
                    "interview_operator_ids": interview_ids,
                    "interview_operator_names": interview_names,
                    "interview_modified_by": text_value(fields.get("系统：最后修改人")).strip(),
                    "interview_modified_at": iso_time(fields.get("系统：最后修改时间")),
                    "linked_anchors": linked_anchors,
                }
            )

    return {
        "mode": "read_only",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "invitation_day": invitation_day,
        "summary": {
            "interviews_scanned": len(interviews),
            "anchors_scanned": len(anchors),
            "rows_reported": len(rows),
            **issue_counts,
        },
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only audit of linked interview and streamer operator ownership.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--invitation-day", default="")
    args = parser.parse_args()

    fs = Feishu(get_tenant_token(args.env))
    report = audit(fs, args.invitation_day)
    write_json(args.out, report)
    print(json.dumps({"summary": report["summary"], "out": str(args.out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
