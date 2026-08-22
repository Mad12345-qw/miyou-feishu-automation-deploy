from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from audit_live_sync_integrity import CHILD_SPECS
from miyou_system_automation import Feishu, get_tenant_token, linked_record_ids, text_value, write_json
from repair_live_data_integrity import load_records


def inspect(records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    anchor_by_id = {
        str(row.get("record_id") or ""): row
        for row in records["anchor"]
        if row.get("record_id")
    }
    groups: list[dict[str, Any]] = []
    for table_key, (link_field, _expected_count, unique_field) in CHILD_SPECS.items():
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in records[table_key]:
            fields = row.get("fields") or {}
            key = text_value(fields.get(unique_field)).strip() if unique_field else "singleton"
            for anchor_id in linked_record_ids(fields.get(link_field)):
                grouped[(anchor_id, key)].append(row)
        for (anchor_id, key), rows in sorted(grouped.items()):
            if key and len(rows) > 1:
                anchor = anchor_by_id.get(anchor_id) or {}
                groups.append(
                    {
                        "table": table_key,
                        "anchor_id": anchor_id,
                        "anchor_name": text_value((anchor.get("fields") or {}).get("主播名字")).strip(),
                        "business_key": key,
                        "records": rows,
                    }
                )
    return {"mode": "read_only", "duplicate_groups": len(groups), "groups": groups}


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect duplicate child workflow rows without changing live data.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fs = Feishu(get_tenant_token(args.env))
    report = inspect(load_records(fs))
    write_json(args.out, report)
    print({"duplicate_groups": report["duplicate_groups"], "out": str(args.out)})


if __name__ == "__main__":
    main()
