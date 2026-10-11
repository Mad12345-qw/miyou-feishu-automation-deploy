from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    APP_TOKEN,
    Feishu,
    TABLES,
    get_tenant_token,
    read_record,
    sync_one_anchor_operator_to_interviews,
    text_value,
    user_ids,
    write_json,
)
from sync_missing_personal_entries import sync_missing_personal_entries
from sync_missing_workbench_rows import sync_missing_workbench_rows


NAME = "洛安"
OPEN_ID = "ou_e4370b95f37f316789e01f1a3437b1d0"
INTERVIEW_ID = "reczz28KnLcXJx0W"
ANCHOR_ID = "reczz28LHDHD7ae4"


def personnel_payload() -> dict[str, Any]:
    return {
        "姓名": NAME,
        "飞书用户": [{"id": OPEN_ID}],
        "角色": ["对接运营"],
        "在职状态": "在职",
        "账号状态": "正常",
        "组织部门": "运营",
        "岗位": "运营",
        "是否创建个人入口": True,
        "是否参与日历同步": True,
        "通讯录OpenID": OPEN_ID,
        "数据来源": "管理员手工维护",
        "手工锁定角色": True,
        "最后同步时间": int(datetime.now().timestamp() * 1000),
        "备注": "系统根据当前主播档案中的真实飞书账号补齐；该账号未出现在应用可见通讯录中。",
    }


def matching_personnel(fs: Feishu) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = fs.list_records(TABLES["personnel"], page_size=500)
    by_id = [record for record in records if OPEN_ID in user_ids((record.get("fields") or {}).get("飞书用户"))]
    by_name = [
        record
        for record in records
        if text_value((record.get("fields") or {}).get("姓名")).strip() == NAME
        and OPEN_ID not in user_ids((record.get("fields") or {}).get("飞书用户"))
    ]
    return by_id, by_name


def view_matches(fs: Feishu) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for table_key, table_id in TABLES.items():
        if table_key in {"node", "personnel"}:
            continue
        response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views", {"page_size": 100})
        for view in (response.get("data") or {}).get("items") or []:
            if NAME in str(view.get("view_name") or ""):
                matches.append(
                    {
                        "table": table_key,
                        "view_id": str(view.get("view_id") or ""),
                        "view_name": str(view.get("view_name") or ""),
                    }
                )
    return matches


def run(fs: Feishu, out_dir: Path, apply: bool) -> dict[str, Any]:
    by_id, by_name = matching_personnel(fs)
    interview = read_record(fs, TABLES["interview"], INTERVIEW_ID)
    anchor = read_record(fs, TABLES["anchor"], ANCHOR_ID)
    snapshot = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "apply" if apply else "dry_run",
        "personnel_by_id": by_id,
        "personnel_name_conflicts": by_name,
        "interview": interview,
        "anchor": anchor,
        "views": view_matches(fs),
    }
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    write_json(out_dir / f"luoan-chain-before-{timestamp}.json", snapshot)

    if len(by_id) > 1:
        raise RuntimeError(f"Multiple personnel rows use {OPEN_ID}.")
    if by_name:
        raise RuntimeError(f"Another personnel row already uses the name {NAME}.")

    plan = {
        "personnel_action": "update" if by_id else "create",
        "interview_id": INTERVIEW_ID,
        "anchor_id": ANCHOR_ID,
        "operator_id": OPEN_ID,
    }
    if not apply:
        return {"mode": "dry_run", "plan": plan, "snapshot": str(out_dir / f"luoan-chain-before-{timestamp}.json")}

    payload = personnel_payload()
    if by_id:
        personnel_results = fs.batch_update(
            TABLES["personnel"],
            [{"record_id": by_id[0]["record_id"], "fields": payload}],
        )
        personnel_record_id = str(by_id[0]["record_id"])
    else:
        personnel_results = fs.batch_create(TABLES["personnel"], [{"fields": payload}])
        created = [
            record
            for result in personnel_results
            for record in ((result.get("data") or {}).get("records") or [])
        ]
        personnel_record_id = str((created[0] if created else {}).get("record_id") or "")
    if any(result.get("code") != 0 for result in personnel_results):
        raise RuntimeError(f"Unable to upsert {NAME} in personnel directory: {personnel_results}")

    anchor = read_record(fs, TABLES["anchor"], ANCHOR_ID)
    ownership = sync_one_anchor_operator_to_interviews(fs, anchor)
    views = sync_missing_personal_entries(fs, out_dir)
    workbench = sync_missing_workbench_rows(
        fs,
        out_dir,
        preloaded_views=views["view_sync"].get("views"),
    )

    final_interview = read_record(fs, TABLES["interview"], INTERVIEW_ID)
    final_anchor = read_record(fs, TABLES["anchor"], ANCHOR_ID)
    final_by_id, final_by_name = matching_personnel(fs)
    interview_fields = final_interview.get("fields") or {}
    anchor_fields = final_anchor.get("fields") or {}
    failures: list[str] = []
    if user_ids(interview_fields.get("对接运营账号（系统）")) != [OPEN_ID]:
        failures.append("02 hidden operator account does not match Luo An.")
    if NAME not in text_value(interview_fields.get("对接运营")):
        failures.append("02 visible operator does not show Luo An.")
    if user_ids(anchor_fields.get("运营经济人")) != [OPEN_ID]:
        failures.append("03 operator account does not match Luo An.")
    if len(final_by_id) != 1 or final_by_name:
        failures.append("Personnel directory binding is missing or ambiguous.")
    if views["view_sync"].get("failed") or views["workbench_view_sync"].get("failed"):
        failures.append("One or more personal views failed to provision.")
    if workbench.get("failed") or workbench.get("missing_views") or workbench.get("conflicts"):
        failures.append("One or more workbench entries failed to provision.")

    report = {
        "mode": "apply",
        "passed": not failures,
        "failures": failures,
        "personnel_record_id": personnel_record_id,
        "personnel_results": personnel_results,
        "ownership": ownership,
        "view_sync": {
            "created": views["view_sync"].get("created"),
            "existing": views["view_sync"].get("existing"),
            "failed": views["view_sync"].get("failed"),
            "workbench_created": views["workbench_view_sync"].get("created"),
            "workbench_existing": views["workbench_view_sync"].get("existing"),
            "workbench_failed": views["workbench_view_sync"].get("failed"),
        },
        "workbench": workbench,
        "final": {
            "interview_visible": text_value(interview_fields.get("对接运营")),
            "interview_user_ids": user_ids(interview_fields.get("对接运营账号（系统）")),
            "anchor_user_ids": user_ids(anchor_fields.get("运营经济人")),
            "personnel_count": len(final_by_id),
            "views": view_matches(fs),
        },
        "snapshot": str(out_dir / f"luoan-chain-before-{timestamp}.json"),
    }
    write_json(out_dir / f"luoan-chain-repair-{timestamp}.json", report)
    if failures:
        raise RuntimeError(json.dumps(report, ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair and verify Luo An's Feishu ownership chain.")
    parser.add_argument("--env", type=Path, default=Path("feishu/.env.local"))
    parser.add_argument("--out-dir", type=Path, default=Path("runtime/audits"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    fs = Feishu(get_tenant_token(args.env))
    report = run(fs, args.out_dir, args.apply)
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "passed": report.get("passed"),
                "plan": report.get("plan"),
                "final": report.get("final"),
                "failures": report.get("failures"),
                "snapshot": report.get("snapshot"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
