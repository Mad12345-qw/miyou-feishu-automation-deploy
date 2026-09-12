from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import APP_TOKEN, TABLES, Feishu, get_tenant_token, write_json


FIELD_NAME = "邀约时间"


def field_by_name(fs: Feishu) -> dict[str, Any]:
    field = next((item for item in fs.fields(TABLES["interview"]) if item.get("field_name") == FIELD_NAME), None)
    if not field:
        raise RuntimeError(f"Field not found: {FIELD_NAME}")
    if field.get("type") != 5:
        raise RuntimeError(f"{FIELD_NAME} is not a date-time field: type={field.get('type')}")
    return field


def main() -> None:
    parser = argparse.ArgumentParser(description="Make invitation time default to the current time for new records.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    fs = Feishu(get_tenant_token(args.env))
    before = field_by_name(fs)
    desired_property = dict(before.get("property") or {})
    desired_property["auto_fill"] = True
    desired_property.setdefault("date_formatter", "yyyy/MM/dd HH:mm")

    response: dict[str, Any] | None = None
    if args.apply and before.get("property") != desired_property:
        response = fs.api(
            "PUT",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES['interview']}/fields/{before['field_id']}",
            body={
                "field_name": FIELD_NAME,
                "type": before["type"],
                "property": desired_property,
            },
        )
        if response.get("code") != 0:
            raise RuntimeError(f"Failed to enable invitation-time autofill: {response}")

    after = field_by_name(fs)
    verified = (after.get("property") or {}).get("auto_fill") is True
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry_run",
        "before": before,
        "desired_property": desired_property,
        "response": response,
        "after": after,
        "verified": verified,
    }
    write_json(args.out, report)
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "field_id": before["field_id"],
                "before_auto_fill": (before.get("property") or {}).get("auto_fill"),
                "after_auto_fill": (after.get("property") or {}).get("auto_fill"),
                "verified": verified,
                "out": str(args.out),
            },
            ensure_ascii=False,
        )
    )
    if args.apply and not verified:
        raise RuntimeError("The field update returned but auto_fill is still disabled.")


if __name__ == "__main__":
    main()
