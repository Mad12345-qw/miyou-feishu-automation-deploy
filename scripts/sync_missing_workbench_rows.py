from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from miyou_system_automation import APP_TOKEN, Feishu, TABLES, get_tenant_token, text_value, user_ids, write_json


WORKBENCH_TABLE = os.environ.get("FEISHU_WORKBENCH_TABLE_ID", "").strip()


def list_views(fs: Feishu, table_id: str) -> list[dict[str, Any]]:
    response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views", {"page_size": 100})
    if response.get("code") != 0:
        raise RuntimeError(response)
    return (response.get("data") or {}).get("items") or []


def sync_missing_workbench_rows(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    people = []
    for record in fs.list_records(TABLES["personnel"], page_size=500):
        fields = record.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职" or text_value(fields.get("账号状态")) != "正常":
            continue
        if fields.get("是否创建个人入口") is not True:
            continue
        ids = user_ids(fields.get("飞书用户"))
        if ids:
            people.append({"name": text_value(fields.get("姓名")), "user_id": ids[0], "roles": fields.get("角色") or []})

    view_specs = [
        ("anchor", "运营_", "_主播", "对接运营", "主播", "打开我的主播"),
        ("task", "运营_", "_日程", "对接运营", "日程", "打开我的日程"),
        ("interview", "招聘_", "_候选人", "招募经纪人", "候选人", "打开我的候选人"),
        ("interview", "面试_", "_候选人", "面试官", "面试候选人", "打开我的面试候选人"),
        ("interview", "运营_", "_候选人", "对接运营", "对接候选人", "打开我的对接候选人"),
        ("visual", "运营_", "_视觉", "对接运营", "视觉任务", "打开我的视觉任务"),
        ("training", "运营_", "_培训", "培训运营", "培训", "打开我的培训"),
        ("first_live", "运营_", "_首播", "跟播运营", "首播", "打开我的首播"),
        ("review", "运营_", "_复盘", "跟播运营", "复盘", "打开我的复盘"),
    ]
    view_map: dict[str, dict[str, str]] = {}
    for table_key, prefix, suffix, role, target, label in view_specs:
        for view in list_views(fs, TABLES[table_key]):
            name = str(view.get("view_name") or "")
            if name.startswith(prefix) and name.endswith(suffix):
                person_name = name[len(prefix) : -len(suffix)].strip("_")
                base_url = os.environ.get("FEISHU_BASE_URL", "").strip().rstrip("/")
                view_map.setdefault(person_name, {})[target] = f"{base_url}/base/{APP_TOKEN}?table={TABLES[table_key]}&view={view['view_id']}"

    existing = fs.list_records(WORKBENCH_TABLE, page_size=500)
    existing_keys = {text_value((record.get("fields") or {}).get("我要做什么")) for record in existing}
    rows = []
    for person in people:
        name = person["name"]
        for target, link in sorted(view_map.get(name, {}).items()):
            key = f"个人入口：{name}的{target}"
            if key in existing_keys:
                continue
            rows.append({"fields": {"我要做什么": key, "谁来操作": "本人", "操作内容": f"{name}直接查看自己的{target}", "系统自动": "系统按飞书人员账号自动筛选本人记录", "完成时限": "每天使用", "点这里办理": {"link": link, "text": f"打开我的{target}"}, "员工账号": [{"id": person["user_id"]}]}})
    results = fs.batch_create(WORKBENCH_TABLE, rows, batch_size=500) if rows else []
    report = {"people": len(people), "desired_rows": len(rows), "created": sum(len(((r.get("data") or {}).get("records") or [])) for r in results), "results": results}
    write_json(out_dir / "sync_missing_workbench_rows_result.json", report)
    return report


def main() -> None:
    fs = Feishu(get_tenant_token(Path("feishu/.env.local")))
    report = sync_missing_workbench_rows(fs, Path("scripts/runtime"))
    print(json.dumps({"people": report["people"], "desired_rows": report["desired_rows"], "created": report["created"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
