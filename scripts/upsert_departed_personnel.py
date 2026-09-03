from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import TABLES, Feishu, get_tenant_token, list_value, text_value, user_ids, write_json


def departed_fields(name: str, open_id: str, role: str, current: dict[str, Any]) -> dict[str, Any]:
    roles = list_value(current.get("角色"))
    if role and role not in roles:
        roles.append(role)
    return {
        "姓名": name,
        "飞书用户": [{"id": open_id}],
        "角色": roles,
        "在职状态": "离职",
        "账号状态": "已离职",
        "是否创建个人入口": False,
        "是否参与日历同步": False,
        "通讯录OpenID": open_id,
        "数据来源": "通讯录自动同步",
        "备注": "已确认离职；仅保留历史业务归属，不创建个人入口。",
        "最后同步时间": int(datetime.now().timestamp() * 1000),
    }


def upsert(fs: Feishu, name: str, open_id: str, role: str, apply: bool) -> dict[str, Any]:
    records = fs.list_records(TABLES["personnel"], page_size=500)
    by_id = [record for record in records if open_id in user_ids((record.get("fields") or {}).get("飞书用户"))]
    same_name = [
        record
        for record in records
        if text_value((record.get("fields") or {}).get("姓名")).strip() == name
        and open_id not in user_ids((record.get("fields") or {}).get("飞书用户"))
    ]
    if len(by_id) > 1:
        raise RuntimeError(f"Multiple personnel rows already use OpenID {open_id}.")
    if same_name and not by_id:
        raise RuntimeError(f"A personnel row named {name} already exists with a different OpenID.")

    existing = by_id[0] if by_id else None
    fields = departed_fields(name, open_id, role, (existing or {}).get("fields") or {})
    action = "update" if existing else "create"
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "action": action,
        "name": name,
        "open_id": open_id,
        "existing_record": existing,
        "desired_fields": fields,
    }
    if not apply:
        return report

    if existing:
        response = fs.batch_update(
            TABLES["personnel"],
            [{"record_id": existing["record_id"], "fields": fields}],
        )
    else:
        response = fs.batch_create(TABLES["personnel"], [{"fields": fields}])
    report["response"] = response
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely register a departed employee while preserving historical ownership.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--open-id", required=True)
    parser.add_argument("--role", default="")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = upsert(
        Feishu(get_tenant_token(args.env)),
        args.name.strip(),
        args.open_id.strip(),
        args.role.strip(),
        args.apply,
    )
    write_json(args.out, report)
    print(json.dumps({key: report[key] for key in ("mode", "action", "name", "open_id")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
