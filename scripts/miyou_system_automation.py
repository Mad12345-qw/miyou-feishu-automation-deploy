from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


OPENAPI = "https://open.feishu.cn/open-apis"
APP_TOKEN = "K3Ckbh4HAat3issjhIhc7NZknBc"
WORKBENCH_TABLE = "tblIcblT5703VGvp"
MANAGEMENT_SUMMARY_TABLE_NAME = "09_老板经营看板"
DEMO_BATCH_PREFIXES = ("ACCEPT-", "客户体验-", "TEST-", "UNIT-", "DEMO-")

TABLES = {
    "interview": "tblNuKseW2MGL3EO",
    "anchor": "tblhYS1AY7Rt2QM2",
    "node": "tbl7z3Z9wpP62xO9",
    "review": "tblggoGZKJQKkOyu",
    "visual": "tblM7pAoDBRo24nC",
    "training": "tblKs29hTf7pim7g",
    "task": "tblqIRGhED4gZaJ3",
    "first_live": "tblsfN165HrNVkKi",
    "personnel": "tblvNCKFHOPiYtlr",
}

INTERVIEW_PERSONNEL_DROPDOWNS = {
    "招募人": {
        "account_field": "招募人账号（系统）",
        "roles": {"招募经纪人"},
    },
    "面试官": {
        "account_field": "面试官账号（系统）",
        "roles": set(),
    },
    "对接运营": {
        "account_field": "对接运营账号（系统）",
        "roles": {"对接运营", "培训运营", "跟播运营"},
    },
}
TRANSFER_TO_ANCHOR_FIELD = "通过转入主播"
LEGACY_TRANSFER_TO_ANCHOR_FIELD = "面试通过，转入主播"
ANCHOR_DISPLAY_FIELD = "主播昵称（编号）"
ANCHOR_NAME_FIELD = "主播名字"
SYSTEM_CREATED_BY_FIELD = "系统：创建人"
SYSTEM_CREATED_AT_FIELD = "系统：创建时间"
SYSTEM_MODIFIED_BY_FIELD = "系统：最后修改人"
SYSTEM_MODIFIED_AT_FIELD = "系统：最后修改时间"
ANCHOR_DISPLAY_TABLES = {
    "node": {"link_field": "关联主播", "legacy_field": "关联主播编号"},
    "task": {"link_field": "对应主播", "legacy_field": "对应主播编号"},
    "visual": {"link_field": "关联主播", "legacy_field": "关联主播编号"},
    "training": {"link_field": "关联主播", "legacy_field": "关联主播编号"},
    "first_live": {"link_field": "关联主播", "legacy_field": "关联主播编号"},
    "review": {"link_field": "关联主播", "legacy_field": "关联主播编号"},
}

FIELD_TYPES = {
    "text": 1,
    "number": 2,
    "single_select": 3,
    "multi_select": 4,
    "datetime": 5,
    "checkbox": 7,
    "attachment": 17,
    "link": 18,
    "user": 11,
    "created_time": 1001,
    "modified_time": 1002,
    "created_user": 1003,
    "modified_user": 1004,
}

CHAIN_NODE_TEMPLATE = [
    ("信息同步", 0.5, "信息同步给运营，明确主播来源、岗位、顾虑点。"),
    ("运营首次建联", 2, "面试通过后2小时内完成首次建联。"),
    ("花名册核对/补全", 3, "接手后1小时内核对并补全主播花名册。"),
    ("当面建档", 4, "补全基础信息、直播核心信息、个人特质、风险排查。"),
    ("服务群组建", 6, "拉服务群并确认主播、邀约、面试官、运营。"),
    ("签约", 12, "签约并上传签约留痕。"),
    ("设备交付", 18, "完成设备交付或确认自有设备并留痕。"),
    ("风格定位", 24, "完成风格定位，视觉调试前不得跳过。"),
    ("视觉前置核验", 30, "座椅、素颜、背景、坐姿镜头、服装、发型6项核验。"),
    ("视觉调试", 36, "完成构图、灯光、美颜参数、试镜视频记录。"),
    ("开播培训", 48, "完成一对一实操培训与逐项验收。"),
    ("3分钟录屏考核", 54, "提交并审核3分钟标准化录屏。"),
    ("首播筹备", 68, "首播前30分钟完成设备、网络、美颜、灯光、服装检查。"),
    ("首播跟播", 72, "首播全程跟播控场。"),
    ("首播复盘", 73, "首播结束后1小时内完成复盘。"),
]

TASK_TEMPLATE = [
    ("首次建联", "建联", 2, 30, "面试通过后2小时内完成首次建联，记录核心顾虑点和下一步安排。"),
    ("建档补全", "建档", 3, 60, "补全主播基础信息、直播核心信息、个人特质、风险排查。"),
    ("签约跟进", "签约", 12, 60, "推进签约并上传签约留痕。"),
    ("开播培训", "培训", 48, 90, "完成一对一培训和逐项验收。"),
    ("首播前检查", "首播提醒", 67.5, 30, "首播前30分钟检查设备、网络、美颜、灯光、服装。"),
    ("首播后复盘", "首播复盘", 73, 60, "首播结束后1小时内完成复盘。"),
]


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def request_json(method: str, url: str, headers: dict[str, str] | None = None, body: Any | None = None) -> dict[str, Any]:
    data = None
    merged_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    last_error: Exception | None = None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(text)
            except json.JSONDecodeError:
                error_payload = {}
            if (error_payload.get("code") in {1254607, 2200} or exc.code >= 500) and attempt < 4:
                time.sleep(1 + attempt * 2)
                continue
            raise RuntimeError(f"{method} {url} failed HTTP {exc.code}: {text}") from exc
        except (urllib.error.URLError, ConnectionResetError, TimeoutError) as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(1 + attempt * 2)
                continue
            raise RuntimeError(f"{method} {url} failed after retry: {exc}") from exc
    raise RuntimeError(f"{method} {url} failed: {last_error}")


def get_tenant_token(env_path: Path) -> str:
    env = load_env(env_path)
    data = request_json(
        "POST",
        f"{OPENAPI}/auth/v3/tenant_access_token/internal",
        body={"app_id": env["FEISHU_APP_ID"], "app_secret": env["FEISHU_APP_SECRET"]},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get tenant token: {data}")
    return data["tenant_access_token"]


class Feishu:
    def __init__(self, token: str) -> None:
        self.token = token

    def api(self, method: str, path: str, query: dict[str, Any] | None = None, body: Any | None = None) -> dict[str, Any]:
        query_string = urllib.parse.urlencode(query or {}, doseq=True)
        url = f"{OPENAPI}{path}" + (f"?{query_string}" if query_string else "")
        return request_json(method, url, headers={"Authorization": f"Bearer {self.token}"}, body=body)

    def fields(self, table_id: str) -> list[dict[str, Any]]:
        data = self.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields", {"page_size": 100})
        return (data.get("data") or {}).get("items") or []

    def create_field(self, table_id: str, name: str, field_type: str, options: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"field_name": name, "type": FIELD_TYPES[field_type]}
        if field_type in ("single_select", "multi_select"):
            body["property"] = {"options": [{"name": item} for item in (options or ["待确认"])]}
        elif field_type == "datetime":
            body["property"] = {"date_formatter": "yyyy/MM/dd HH:mm"}
        data = self.api("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields", body=body)
        return data

    def create_formula_field(self, table_id: str, name: str, formula_expression: str) -> dict[str, Any]:
        return self.api(
            "POST",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields",
            body={
                "field_name": name,
                "type": 20,
                "property": {"formula_expression": formula_expression},
            },
        )

    def create_link_field(self, table_id: str, name: str, target_table_id: str) -> dict[str, Any]:
        return self.api(
            "POST",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields",
            body={
                "field_name": name,
                "type": 18,
                "property": {"table_id": target_table_id, "multiple": True},
            },
        )

    def rename_field(self, table_id: str, field: dict[str, Any], new_name: str) -> dict[str, Any]:
        body: dict[str, Any] = {"field_name": new_name, "type": field["type"]}
        if field.get("type") in (3, 4) and field.get("property") is not None:
            body["property"] = field["property"]
        if field.get("type") == 11 and field.get("property") is not None:
            body["property"] = field["property"]
        if field.get("type") in (18, 21):
            prop = field.get("property") or {}
            body["property"] = {
                "table_id": prop.get("table_id"),
                "multiple": bool(prop.get("multiple")),
            }
        return self.api(
            "PUT",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields/{field['field_id']}",
            body=body,
        )

    def update_select_options(self, table_id: str, field: dict[str, Any], options: list[str]) -> dict[str, Any]:
        existing = {
            str(option.get("name") or ""): option
            for option in ((field.get("property") or {}).get("options") or [])
            if option.get("name")
        }
        desired = []
        for name in options:
            option = existing.get(name)
            desired.append(option if option else {"name": name})
        return self.api(
            "PUT",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields/{field['field_id']}",
            body={
                "field_name": field["field_name"],
                "type": field["type"],
                "property": {"options": desired},
            },
        )

    def list_records(self, table_id: str, page_size: int = 500, limit: int = 0) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query: dict[str, Any] = {"page_size": page_size}
            if page_token:
                query["page_token"] = page_token
            data = self.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records", query)
            items = (data.get("data") or {}).get("items") or []
            records.extend(items)
            if limit and len(records) >= limit:
                return records[:limit]
            if not (data.get("data") or {}).get("has_more"):
                return records
            page_token = (data.get("data") or {}).get("page_token") or ""

    def search_records(self, table_id: str, field_name: str, value: str = "true", page_size: int = 500) -> list[dict[str, Any]]:
        """Read only records matching a single field, avoiding full-table scans."""
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            body: dict[str, Any] = {
                "page_size": page_size,
                "filter": {
                    "conjunction": "and",
                    "conditions": [{"field_name": field_name, "operator": "is", "value": [value]}],
                },
            }
            if page_token:
                body["page_token"] = page_token
            data = self.api("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/search", body=body)
            if data.get("code") != 0:
                raise RuntimeError(f"Failed to search records in {table_id} by {field_name}: {data}")
            payload = data.get("data") or {}
            records.extend(payload.get("items") or [])
            if not payload.get("has_more"):
                return records
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                return records

    def search_records_by_filter(
        self,
        table_id: str,
        conditions: list[dict[str, Any]],
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        """Read only records matching an explicit Base filter."""
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            body: dict[str, Any] = {
                "page_size": page_size,
                "filter": {"conjunction": "and", "conditions": conditions},
            }
            if page_token:
                body["page_token"] = page_token
            data = self.api("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/search", body=body)
            if data.get("code") != 0:
                raise RuntimeError(f"Failed to search records in {table_id}: {data}")
            payload = data.get("data") or {}
            records.extend(payload.get("items") or [])
            if not payload.get("has_more"):
                return records
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                return records

    def batch_create(self, table_id: str, records: list[dict[str, Any]], batch_size: int = 500) -> list[dict[str, Any]]:
        results = []
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            data = self.api(
                "POST",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/batch_create",
                body={"records": batch},
            )
            results.append(data)
            if data.get("code") != 0:
                break
        return results

    def batch_update(self, table_id: str, records: list[dict[str, Any]], batch_size: int = 500) -> list[dict[str, Any]]:
        results = []
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            data = self.api(
                "POST",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/batch_update",
                body={"records": batch},
            )
            results.append(data)
            if data.get("code") != 0:
                break
        return results


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def parse_feishu_dt(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000)
    return datetime.now()


def text_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
            else:
                parts.append(str(item))
        return "、".join(part for part in parts if part)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return str(value)


def invitation_day(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y/%m/%d")


def invitation_day_group_is_formula(fields: list[dict[str, Any]]) -> bool:
    return any(
        field.get("field_name") == "邀约日期（按天分组）" and field.get("type") == 20
        for field in fields
    )


def list_value(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [text_value(item).strip() for item in value if text_value(item).strip()]
    return [text_value(value).strip()]


def phone_value(value: Any) -> str:
    text = text_value(value).strip()
    if not text:
        return ""
    allowed = text[1:] if text.startswith("+") else text
    if allowed.isdigit():
        return text
    return ""


def user_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, dict):
            user_id = item.get("id") or item.get("open_id") or item.get("user_id")
            if user_id:
                result.append(str(user_id))
    return result


def owner_names(*values: Any) -> set[str]:
    names: set[str] = set()
    for value in values:
        text = text_value(value)
        for name in text.replace("，", "、").replace(",", "、").split("、"):
            name = name.strip()
            if name:
                names.add(name)
    return names


def created_records(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for result in results:
        records.extend(((result.get("data") or {}).get("records") or []))
    return records


def ensure_fields(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    required: dict[str, list[tuple[str, str, list[str] | None]]] = {
        "interview": [
            ("自动化批次", "text", None),
            ("系统处理状态", "single_select", ["待处理", "已生成主播档案", "跳过", "异常"]),
            ("系统处理备注", "text", None),
            ("招募人", "single_select", ["待同步"]),
            ("面试官", "single_select", ["待同步"]),
            ("对接运营", "single_select", ["待同步"]),
            ("招募人账号（系统）", "user", None),
            ("面试官账号（系统）", "user", None),
            ("对接运营账号（系统）", "user", None),
            (SYSTEM_CREATED_BY_FIELD, "created_user", None),
            (SYSTEM_CREATED_AT_FIELD, "created_time", None),
            (SYSTEM_MODIFIED_BY_FIELD, "modified_user", None),
            (SYSTEM_MODIFIED_AT_FIELD, "modified_time", None),
            ("邀约时间", "datetime", None),
            ("面试开始时间", "datetime", None),
            ("面试结束时间", "datetime", None),
            ("邀约是否同步日历", "checkbox", None),
            ("邀约日历事件ID", "text", None),
            ("邀约日历同步指纹", "text", None),
            ("面试是否同步日历", "checkbox", None),
            ("面试日历事件ID", "text", None),
            ("面试日历同步指纹", "text", None),
        ],
        "anchor": [
            ("自动化批次", "text", None),
            ("系统验收状态", "single_select", ["待验收", "通过", "不通过", "需补资料"]),
            ("缺失信息项", "text", None),
        ],
        "node": [
            ("自动化批次", "text", None),
            ("SLA小时", "number", None),
            ("是否关键准入节点", "checkbox", None),
            ("节点验收结果", "single_select", ["待验收", "通过", "不通过", "需补资料"]),
            ("异常原因", "text", None),
        ],
        "task": [
            ("自动化批次", "text", None),
            ("SLA规则", "text", None),
            ("异常原因", "text", None),
            ("运营经济人", "user", None),
        ],
        "visual": [
            ("自动化批次", "text", None),
            ("提交运营", "user", None),
            ("6项核验是否全部通过", "checkbox", None),
            ("准入结果", "single_select", ["可进入培训", "禁止进入培训", "需补资料"]),
            ("准入异常原因", "text", None),
            ("开始时间", "datetime", None),
            ("结束时间", "datetime", None),
            ("是否同步飞书日历", "checkbox", None),
            ("飞书日历事件ID", "text", None),
            ("日历同步指纹", "text", None),
        ],
        "training": [
            ("自动化批次", "text", None),
            ("培训运营", "user", None),
            ("开始时间", "datetime", None),
            ("结束时间", "datetime", None),
            ("账号搭建检查", "single_select", ["未验收", "通过", "不通过", "需整改"]),
            ("转化能力验收", "single_select", ["未验收", "通过", "不通过", "需整改"]),
            ("录屏时长（秒）", "number", None),
            ("录屏准入结果", "single_select", ["可进入首播", "禁止进入首播", "需补资料"]),
            ("是否同步飞书日历", "checkbox", None),
            ("飞书日历事件ID", "text", None),
            ("日历同步指纹", "text", None),
        ],
        "first_live": [
            ("自动化批次", "text", None),
            ("跟播运营", "user", None),
            ("开始时间", "datetime", None),
            ("结束时间", "datetime", None),
            ("首播结束时间", "datetime", None),
            ("首播前检查是否通过", "checkbox", None),
            ("首播准入结果", "single_select", ["可安排首播", "禁止首播", "需补资料"]),
            ("首播准入异常原因", "text", None),
            ("复盘异常原因", "text", None),
            ("是否同步飞书日历", "checkbox", None),
            ("飞书日历事件ID", "text", None),
            ("日历同步指纹", "text", None),
        ],
        "review": [
            ("跟播人员", "user", None),
            ("开始时间", "datetime", None),
            ("结束时间", "datetime", None),
            ("是否同步飞书日历", "checkbox", None),
            ("飞书日历事件ID", "text", None),
            ("日历同步指纹", "text", None),
        ],
        "personnel": [
            ("匹配别名", "text", None),
            ("在职状态", "single_select", ["在职", "离职", "停用"]),
            ("账号状态", "single_select", ["正常", "未激活", "已暂停", "已离职", "未知"]),
            ("组织部门", "text", None),
            ("部门ID", "text", None),
            ("岗位", "text", None),
            ("入职时间", "datetime", None),
            ("离职时间", "datetime", None),
            ("是否创建个人入口", "checkbox", None),
            ("手工锁定角色", "checkbox", None),
            ("通讯录OpenID", "text", None),
            ("最后同步时间", "datetime", None),
            ("数据来源", "single_select", ["通讯录自动同步", "业务记录发现", "管理员手工维护"]),
        ],
    }
    report: dict[str, Any] = {"created": [], "existing": [], "errors": []}
    for table_key, fields in required.items():
        table_id = TABLES[table_key]
        existing = {field.get("field_name") for field in fs.fields(table_id)}
        for name, field_type, options in fields:
            if name in existing:
                report["existing"].append({"table": table_key, "field": name})
                continue
            data = fs.create_field(table_id, name, field_type, options)
            item = {"table": table_key, "field": name, "code": data.get("code"), "msg": data.get("msg")}
            if data.get("code") == 0:
                report["created"].append(item)
            else:
                report["errors"].append(item)
            time.sleep(0.2)
    write_json(out_dir / "setup_controls_result.json", report)
    return report


def authoritative_recruiter_view_hidden_fields(
    fields: dict[str, dict[str, Any]],
    current_hidden: list[str],
    system_hidden_names: set[str],
) -> list[str]:
    """Show the form-written recruiter account and hide the delayed display copy."""
    hidden_ids = {str(fields[name]["field_id"]) for name in system_hidden_names if name in fields}
    recruiter_account_id = str((fields.get("招募人账号（系统）") or {}).get("field_id") or "")
    recruiter_display_id = str((fields.get("招募人") or {}).get("field_id") or "")
    desired = list(dict.fromkeys([*current_hidden, *sorted(hidden_ids)]))
    if recruiter_account_id:
        desired = [field_id for field_id in desired if field_id != recruiter_account_id]
    if recruiter_display_id and recruiter_display_id not in desired:
        desired.append(recruiter_display_id)
    return desired


def ensure_interview_workflow_surface(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    table_id = TABLES["interview"]
    fields = {field.get("field_name"): field for field in fs.fields(table_id)}
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    legacy_flag = fields.get("是否生成主播档案")
    transfer_flag = fields.get(TRANSFER_TO_ANCHOR_FIELD)
    alternate_transfer_flag = fields.get(LEGACY_TRANSFER_TO_ANCHOR_FIELD)
    source_transfer_field = legacy_flag or alternate_transfer_flag
    if not transfer_flag and source_transfer_field and source_transfer_field.get("type") == FIELD_TYPES["checkbox"]:
        response = fs.rename_field(table_id, source_transfer_field, TRANSFER_TO_ANCHOR_FIELD)
        item = {"action": "rename_transfer_flag", "code": response.get("code"), "msg": response.get("msg")}
        (actions if response.get("code") == 0 else errors).append(item)
        fields = {field.get("field_name"): field for field in fs.fields(table_id)}

    if TRANSFER_TO_ANCHOR_FIELD not in fields:
        response = fs.create_field(table_id, TRANSFER_TO_ANCHOR_FIELD, "checkbox")
        item = {"action": "create_transfer_flag", "code": response.get("code"), "msg": response.get("msg")}
        (actions if response.get("code") == 0 else errors).append(item)
        fields = {field.get("field_name"): field for field in fs.fields(table_id)}

    if "系统：已生成主播档案" not in fields:
        response = fs.create_field(table_id, "系统：已生成主播档案", "checkbox")
        item = {"action": "create_generated_flag", "code": response.get("code"), "msg": response.get("msg")}
        (actions if response.get("code") == 0 else errors).append(item)
        fields = {field.get("field_name"): field for field in fs.fields(table_id)}

    if "邀约日期" not in fields:
        response = fs.create_formula_field(table_id, "邀约日期", 'DATETIME_FORMAT([邀约时间], "YYYY-MM-DD")')
        item = {"action": "create_invitation_date", "code": response.get("code"), "msg": response.get("msg")}
        (actions if response.get("code") == 0 else errors).append(item)
        fields = {field.get("field_name"): field for field in fs.fields(table_id)}

    if "邀约日期（按天分组）" not in fields:
        response = fs.create_field(table_id, "邀约日期（按天分组）", "text")
        item = {"action": "create_invitation_day_group_field", "code": response.get("code"), "msg": response.get("msg")}
        (actions if response.get("code") == 0 else errors).append(item)
        fields = {field.get("field_name"): field for field in fs.fields(table_id)}

    hidden_names = {
        "招募人账号（系统）",
        "面试官账号（系统）",
        "对接运营账号（系统）",
        SYSTEM_CREATED_BY_FIELD,
        SYSTEM_CREATED_AT_FIELD,
        SYSTEM_MODIFIED_BY_FIELD,
        SYSTEM_MODIFIED_AT_FIELD,
        "系统：已生成主播档案",
        "自动化批次",
        "系统处理状态",
        "系统处理备注",
        "邀约是否同步日历",
        "邀约日历事件ID",
        "邀约日历同步指纹",
        "面试是否同步日历",
        "面试日历事件ID",
        "面试日历同步指纹",
        "父记录 2",
        "父记录 3",
        "邀约日期",
        LEGACY_TRANSFER_TO_ANCHOR_FIELD,
    }
    user_surface_view_names = {
        "系统全字段（日常不用）",
        "招聘登记与管理",
        "招聘_全部候选人",
        "招聘_待转主播",
        "招聘_我邀约的候选人",
    }
    view_page_token = ""
    view_updates: list[dict[str, Any]] = []
    while True:
        query: dict[str, Any] = {"page_size": 100}
        if view_page_token:
            query["page_token"] = view_page_token
        response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views", query)
        payload = response.get("data") or {}
        for view in payload.get("items") or []:
            if view.get("view_type") != "grid" or view.get("view_name") not in user_surface_view_names:
                continue
            view_id = str(view.get("view_id") or "")
            if not view_id:
                continue
            detail = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views/{view_id}")
            stored = ((detail.get("data") or {}).get("view") or {})
            property_data = stored.get("property") or {}
            current_hidden_list = list(property_data.get("hidden_fields") or [])
            current_hidden = set(current_hidden_list)
            desired_hidden = authoritative_recruiter_view_hidden_fields(fields, current_hidden_list, hidden_names)
            current_name = str(stored.get("view_name") or view.get("view_name") or "")
            desired_name = "招聘登记与管理" if current_name == "系统全字段（日常不用）" else current_name
            if desired_name == current_name and current_hidden == set(desired_hidden):
                continue
            updated = fs.api(
                "PATCH",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views/{view_id}",
                body={"view_name": desired_name, "property": {"hidden_fields": desired_hidden}},
            )
            view_updates.append({"view_id": view_id, "view_name": desired_name, "code": updated.get("code"), "msg": updated.get("msg")})
        if not payload.get("has_more"):
            break
        view_page_token = str(payload.get("page_token") or "")
        if not view_page_token:
            break

    report = {"actions": actions, "errors": errors, "view_updates": view_updates}
    write_json(out_dir / "ensure_interview_workflow_surface_result.json", report)
    return report


def repair_relationship_fields(fs: Feishu, batch: str, out_dir: Path) -> dict[str, Any]:
    # Preserve display text as identifiers and restore the user-facing link names.
    repairs = [
        (TABLES["anchor"], "来源面试记录", "来源面试记录说明", "来源面试记录记录", "来源面试记录"),
        (TABLES["interview"], "关联主播档案", "关联主播档案说明", "关联主播档案记录", "关联主播档案"),
        (TABLES["node"], "关联主播", ANCHOR_DISPLAY_FIELD, "关联主播记录", "关联主播"),
        (TABLES["visual"], "关联主播", ANCHOR_DISPLAY_FIELD, "关联主播记录", "关联主播"),
        (TABLES["training"], "关联主播", ANCHOR_DISPLAY_FIELD, "关联主播记录", "关联主播"),
        (TABLES["first_live"], "关联主播", ANCHOR_DISPLAY_FIELD, "关联主播记录", "关联主播"),
        (TABLES["task"], "对应主播", ANCHOR_DISPLAY_FIELD, "对应主播记录", "对应主播"),
    ]
    field_repairs = []
    for table_id, text_name, text_new_name, link_name, link_new_name in repairs:
        fields = {field["field_name"]: field for field in fs.fields(table_id)}
        text_field = fields.get(text_name)
        if text_field and text_field.get("type") == 1:
            field_repairs.append({
                "table_id": table_id,
                "from": text_name,
                "to": text_new_name,
                "response": fs.rename_field(table_id, text_field, text_new_name),
            })
        fields = {field["field_name"]: field for field in fs.fields(table_id)}
        link_field = fields.get(link_name)
        if link_field and link_field.get("type") in (18, 21):
            field_repairs.append({
                "table_id": table_id,
                "from": link_name,
                "to": link_new_name,
                "response": fs.rename_field(table_id, link_field, link_new_name),
            })

    review_fields = {field["field_name"]: field for field in fs.fields(TABLES["review"])}
    review_text = review_fields.get("关联主播")
    if review_text and review_text.get("type") == 1:
        field_repairs.append({
            "table_id": TABLES["review"],
            "from": "关联主播",
            "to": "关联主播编号",
            "response": fs.rename_field(TABLES["review"], review_text, "关联主播编号"),
        })
    review_fields = {field["field_name"]: field for field in fs.fields(TABLES["review"])}
    if not review_fields.get("关联主播"):
        field_repairs.append({
            "table_id": TABLES["review"],
            "from": "",
            "to": "关联主播",
            "response": fs.create_link_field(TABLES["review"], "关联主播", TABLES["anchor"]),
        })

    build_file = out_dir / f"build_chain_{batch}_result.json"
    if not build_file.exists():
        raise RuntimeError(f"Missing build result for batch {batch}: {build_file}")
    build = json.loads(build_file.read_text(encoding="utf-8"))
    anchors = created_records(build.get("anchor_results") or [])
    interview_ids = [
        record.get("record_id") or record.get("id")
        for result in (build.get("interview_update_results") or [])
        for record in ((result.get("data") or {}).get("records") or [])
        if record.get("record_id") or record.get("id")
    ]
    if len(anchors) != len(interview_ids):
        raise RuntimeError(f"Cannot backfill batch {batch}: anchors={len(anchors)}, interviews={len(interview_ids)}")

    anchor_updates = []
    interview_updates = []
    for anchor, interview_id in zip(anchors, interview_ids):
        anchor_id = anchor.get("record_id") or anchor.get("id")
        if not anchor_id:
            raise RuntimeError(f"Batch {batch} contains an anchor without record_id")
        anchor_no = text_value((anchor.get("fields") or {}).get("主播编号"))
        anchor_name = text_value((anchor.get("fields") or {}).get(ANCHOR_NAME_FIELD))
        anchor_updates.append({
            "record_id": anchor_id,
            "fields": {
                "来源面试记录": [interview_id],
                "来源面试记录说明": f"{anchor_name} 的邀约面试记录",
            },
        })
        interview_updates.append({
            "record_id": interview_id,
            "fields": {
                "关联主播档案": [anchor_id],
                "关联主播档案说明": f"已生成主播档案：{anchor_no}",
            },
        })

    result = {
        "batch": batch,
        "field_repairs": field_repairs,
        "anchor_link_updates": fs.batch_update(TABLES["anchor"], anchor_updates, batch_size=100),
        "interview_link_updates": fs.batch_update(TABLES["interview"], interview_updates, batch_size=100),
    }
    write_json(out_dir / f"repair_relationships_{batch}_result.json", result)
    return result


def eligible_interview(record: dict[str, Any], not_before_ms: int = 0) -> bool:
    fields = record.get("fields") or {}
    if "体验样本" in text_value(fields.get("候选人姓名")) or is_demo_batch(fields.get("自动化批次")):
        return False
    if linked_record_ids(fields.get("关联主播档案")):
        return False
    if fields.get(TRANSFER_TO_ANCHOR_FIELD) is not True and fields.get(LEGACY_TRANSFER_TO_ANCHOR_FIELD) is not True:
        return False
    workflow_time = fields.get("面试开始时间") or fields.get("面试时间") or fields.get("邀约时间")
    if not_before_ms and (not isinstance(workflow_time, (int, float)) or workflow_time < not_before_ms):
        return False
    return bool(text_value(fields.get("候选人姓名")))


def linked_record_ids(value: Any) -> list[str]:
    """Normalize relation fields returned by list, search, and single-record APIs."""
    if isinstance(value, dict):
        return [str(record_id) for record_id in (value.get("link_record_ids") or value.get("record_ids") or []) if record_id]
    record_ids: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            record_ids.append(item)
        elif isinstance(item, dict):
            record_ids.extend(
                str(record_id)
                for record_id in (item.get("link_record_ids") or item.get("record_ids") or [])
                if record_id
            )
    return record_ids


def find_existing_anchor_for_interview(
    fs: Feishu,
    record: dict[str, Any],
    anchors_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    fields = record.get("fields") or {}
    linked_ids = linked_record_ids(fields.get("关联主播档案"))
    if linked_ids:
        if anchors_by_id is None:
            return {"record_id": linked_ids[0], "fields": {}}
        for linked_id in linked_ids:
            anchor = anchors_by_id.get(linked_id)
            if anchor:
                return anchor
    candidate_name = text_value(fields.get("候选人姓名")).strip()
    if not candidate_name:
        return None
    expected_number = f"MYZB-AUTO-{record['record_id'][-10:]}"
    for anchor in fs.search_records(TABLES["anchor"], ANCHOR_NAME_FIELD, candidate_name, page_size=100):
        anchor_fields = anchor.get("fields") or {}
        if (
            text_value(anchor_fields.get("主播编号")).strip() == expected_number
            or record["record_id"] in linked_record_ids(anchor_fields.get("来源面试记录"))
        ):
            return anchor
    return None


def sync_selected_interview_assignments(fs: Feishu, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve visible personnel names to Feishu users for the supplied interviews."""
    people: dict[str, list[dict[str, str]]] = {}
    for person in fs.list_records(TABLES["personnel"], page_size=500):
        fields = person.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职" or text_value(fields.get("账号状态")) != "正常":
            continue
        name = text_value(fields.get("姓名")).strip()
        users = [{"id": user_id} for user_id in user_ids(fields.get("飞书用户"))]
        if name and users:
            people[name] = users
    updates: list[dict[str, Any]] = []
    unresolved: set[str] = set()
    for record in records:
        fields = record.get("fields") or {}
        changed: dict[str, Any] = {}
        for visible_name, spec in INTERVIEW_PERSONNEL_DROPDOWNS.items():
            selected = text_value(fields.get(visible_name)).strip()
            if not selected:
                continue
            account_name = str(spec["account_field"])
            current_user_ids = user_ids(fields.get(account_name))
            users = people.get(selected)
            if not users:
                if not current_user_ids:
                    unresolved.add(selected)
                continue
            if current_user_ids != user_ids(users):
                changed[account_name] = users
                fields[account_name] = users
        if changed:
            updates.append({"record_id": record["record_id"], "fields": changed})
    return {"updated_records": len(updates), "unresolved_values": sorted(unresolved), "results": fs.batch_update(TABLES["interview"], updates, batch_size=100) if updates else []}


def sync_linked_anchor_operators(fs: Feishu, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep recruiter, interviewer, and operator ownership aligned on linked anchors."""
    assignment_fields = {
        "招募人账号（系统）": "招募经济人",
        "面试官账号（系统）": "面试官",
        "对接运营账号（系统）": "运营经济人",
    }
    assignments: dict[str, dict[str, list[dict[str, str]]]] = {}
    for record in records:
        fields = record.get("fields") or {}
        linked_ids = linked_record_ids(fields.get("关联主播档案"))
        if len(linked_ids) != 1:
            continue
        desired: dict[str, list[dict[str, str]]] = {}
        for interview_field, anchor_field in assignment_fields.items():
            ids = user_ids(fields.get(interview_field))
            if ids:
                desired[anchor_field] = [{"id": user_id} for user_id in ids]
        if desired:
            assignments[linked_ids[0]] = desired

    anchors_by_id = {
        str(anchor.get("record_id") or ""): anchor
        for anchor in fs.list_records(TABLES["anchor"], page_size=500)
        if anchor.get("record_id")
    }
    missing_ids = sorted(set(assignments) - set(anchors_by_id))
    updates: list[dict[str, Any]] = []
    updated_fields = {field_name: 0 for field_name in assignment_fields.values()}
    for anchor_id, desired in assignments.items():
        anchor = anchors_by_id.get(anchor_id)
        if not anchor:
            continue
        current = anchor.get("fields") or {}
        changed = {
            field_name: users
            for field_name, users in desired.items()
            if set(user_ids(current.get(field_name))) != set(user_ids(users))
        }
        if changed:
            updates.append({"record_id": anchor_id, "fields": changed})
            for field_name in changed:
                updated_fields[field_name] += 1
    return {
        "checked_assignments": len(assignments),
        "updated": len(updates),
        "updated_fields": updated_fields,
        "missing_linked_anchor_ids": missing_ids,
        "results": fs.batch_update(TABLES["anchor"], updates, batch_size=100) if updates else [],
    }


def sync_interview_anchor_ownership(fs: Feishu, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve interview users first, then reconcile every linked anchor owner."""
    assignment_sync = sync_selected_interview_assignments(fs, records)
    anchor_operator_sync = sync_linked_anchor_operators(fs, records)
    return {
        "assignment_sync": assignment_sync,
        "anchor_operator_sync": anchor_operator_sync,
    }


def anchor_display_name(fields: dict[str, Any]) -> str:
    nickname = (
        text_value(fields.get(ANCHOR_NAME_FIELD)).strip()
        or text_value(fields.get("主播昵称")).strip()
        or text_value(fields.get("真实姓名")).strip()
    )
    number = text_value(fields.get("主播编号")).strip()
    if nickname and number:
        return f"{nickname} · {number}"
    return nickname or number


def build_anchor_child_payloads(
    anchor: dict[str, Any],
    interview: dict[str, Any],
    base_dt: datetime,
    batch: str,
) -> dict[str, list[dict[str, Any]]]:
    anchor_id = str(anchor.get("record_id") or anchor.get("id") or "")
    anchor_fields = anchor.get("fields") or {}
    interview_fields = interview.get("fields") or {}
    anchor_name = text_value(anchor_fields.get(ANCHOR_NAME_FIELD)).strip()
    anchor_display = anchor_display_name(anchor_fields)
    owner_users = interview_fields.get("对接运营账号（系统）") or anchor_fields.get("运营经济人") or []
    owner = (
        text_value(interview_fields.get("对接运营")).strip()
        or text_value(owner_users).strip()
        or text_value(anchor_fields.get("运营经济人")).strip()
        or "待分配"
    )

    nodes: list[dict[str, Any]] = []
    for node_name, hours, note in CHAIN_NODE_TEMPLATE:
        critical = node_name in {"风格定位", "视觉前置核验", "3分钟录屏考核", "首播复盘"}
        nodes.append(
            {
                "fields": {
                    ANCHOR_DISPLAY_FIELD: anchor_display,
                    "关联主播": [anchor_id],
                    "节点类型": node_name,
                    "责任人": owner,
                    "计划完成时间": ms(base_dt + timedelta(hours=hours)),
                    "节点状态": "未开始",
                    "是否超时": (base_dt + timedelta(hours=hours)) < datetime.now(),
                    "SLA小时": hours,
                    "是否关键准入节点": critical,
                    "节点验收结果": "待验收",
                    "备注": f"{anchor_name} - {note}",
                    "自动化批次": batch,
                }
            }
        )

    tasks: list[dict[str, Any]] = []
    for task_name, task_type, start_hours, duration_minutes, work in TASK_TEMPLATE:
        start_dt = base_dt + timedelta(hours=start_hours)
        tasks.append(
            {
                "fields": {
                    "任务名称": f"{anchor_name} {task_name}",
                    "任务类型": task_type,
                    ANCHOR_DISPLAY_FIELD: anchor_display,
                    "对应主播": [anchor_id],
                    "负责人": owner,
                    "运营经济人": owner_users,
                    "日期": ms(start_dt),
                    "开始时间": ms(start_dt),
                    "结束时间": ms(start_dt + timedelta(minutes=duration_minutes)),
                    "工作事项": work,
                    "工作状态": "未开始",
                    "优先级": "高" if task_name in {"首次建联", "首播后复盘"} else "中",
                    "是否同步飞书日历": False,
                    "SLA规则": f"面试后{start_hours}小时内",
                    "自动化批次": batch,
                }
            }
        )

    visual = {
        "fields": {
            ANCHOR_DISPLAY_FIELD: anchor_display,
            "关联主播": [anchor_id],
            "需求标题": f"{anchor_name} 视觉前置核验与美颜调试",
            "需求描述": "自动生成：完成视觉前置6项核验后，再进入构图、灯光、美颜参数和试镜视频记录。",
            "提交运营": owner_users,
            "预约时间": ms(base_dt + timedelta(hours=30)),
            "开始时间": ms(base_dt + timedelta(hours=30)),
            "结束时间": ms(base_dt + timedelta(hours=31)),
            "紧急程度": "普通",
            "需求状态": "待接单",
            "座椅": False,
            "素颜": False,
            "背景场地": False,
            "坐姿镜头": False,
            "服装": False,
            "发型": False,
            "前置核验状态": "未核验",
            "6项核验是否全部通过": False,
            "准入结果": "禁止进入培训",
            "准入异常原因": "视觉前置6项尚未全部确认",
            "自动化批次": batch,
        }
    }
    training = {
        "fields": {
            ANCHOR_DISPLAY_FIELD: anchor_display,
            "关联主播": [anchor_id],
            "培训运营": owner_users,
            "开始时间": ms(base_dt + timedelta(hours=48)),
            "结束时间": ms(base_dt + timedelta(hours=49)),
            "账号搭建检查": "未验收",
            "培训状态": "未开始",
            "基础话术验收": "未验收",
            "姿态状态验收": "未验收",
            "镜头感验收": "未验收",
            "突发处理验收": "未验收",
            "转化能力验收": "未验收",
            "消费力感知验收": "未验收",
            "录屏审核状态": "待提交",
            "是否允许进入首播": False,
            "录屏准入结果": "禁止进入首播",
            "自动化批次": batch,
        }
    }
    first_live = {
        "fields": {
            ANCHOR_DISPLAY_FIELD: anchor_display,
            "关联主播": [anchor_id],
            "首播结束时间": ms(base_dt + timedelta(hours=73)),
            "跟播运营": owner_users,
            "开始时间": ms(base_dt + timedelta(hours=72)),
            "结束时间": ms(base_dt + timedelta(hours=73)),
            "设备检查": "未检查",
            "网络检查": "未检查",
            "美颜检查": "未检查",
            "灯光检查": "未检查",
            "服装检查": "未检查",
            "首播状态": "待首播",
            "是否1小时内复盘": False,
            "复盘异常原因": "首播复盘尚未完成",
            "自动化批次": batch,
        }
    }
    return {
        "node": nodes,
        "task": tasks,
        "visual": [visual],
        "training": [training],
        "first_live": [first_live],
    }


def ensure_recovered_anchor_children(
    fs: Feishu,
    recovered_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    batch: str,
) -> dict[str, Any]:
    """Fill only missing workflow children for anchors recovered from dangling links."""
    if not recovered_pairs:
        return {"checked_anchors": 0, "created": {}, "complete_anchors": 0, "failures": []}

    pairs_by_anchor = {
        str(anchor.get("record_id") or anchor.get("id") or ""): (anchor, interview)
        for anchor, interview in recovered_pairs
        if anchor.get("record_id") or anchor.get("id")
    }
    target_ids = set(pairs_by_anchor)

    def linked_children(table_key: str, link_field: str) -> list[dict[str, Any]]:
        return [
            record
            for record in fs.list_records(TABLES[table_key], page_size=500)
            if target_ids.intersection(linked_record_ids((record.get("fields") or {}).get(link_field)))
        ]

    existing_nodes = linked_children("node", "关联主播")
    existing_tasks = linked_children("task", "对应主播")
    existing_visuals = linked_children("visual", "关联主播")
    existing_trainings = linked_children("training", "关联主播")
    existing_first_lives = linked_children("first_live", "关联主播")

    node_keys = {
        (anchor_id, text_value((record.get("fields") or {}).get("节点类型")).strip())
        for record in existing_nodes
        for anchor_id in linked_record_ids((record.get("fields") or {}).get("关联主播"))
    }
    task_keys = {
        (anchor_id, text_value((record.get("fields") or {}).get("任务类型")).strip())
        for record in existing_tasks
        for anchor_id in linked_record_ids((record.get("fields") or {}).get("对应主播"))
    }
    singleton_ids = {
        "visual": {anchor_id for record in existing_visuals for anchor_id in linked_record_ids((record.get("fields") or {}).get("关联主播"))},
        "training": {anchor_id for record in existing_trainings for anchor_id in linked_record_ids((record.get("fields") or {}).get("关联主播"))},
        "first_live": {anchor_id for record in existing_first_lives for anchor_id in linked_record_ids((record.get("fields") or {}).get("关联主播"))},
    }
    missing: dict[str, list[dict[str, Any]]] = {key: [] for key in ("node", "task", "visual", "training", "first_live")}
    for anchor_id, (anchor, interview) in pairs_by_anchor.items():
        interview_fields = interview.get("fields") or {}
        base_dt = parse_feishu_dt(interview_fields.get("面试开始时间") or interview_fields.get("面试时间") or interview_fields.get("邀约时间"))
        desired = build_anchor_child_payloads(anchor, interview, base_dt, batch)
        missing["node"].extend(
            item for item in desired["node"]
            if (anchor_id, text_value(item["fields"].get("节点类型")).strip()) not in node_keys
        )
        missing["task"].extend(
            item for item in desired["task"]
            if (anchor_id, text_value(item["fields"].get("任务类型")).strip()) not in task_keys
        )
        for table_key in ("visual", "training", "first_live"):
            if anchor_id not in singleton_ids[table_key]:
                missing[table_key].extend(desired[table_key])

    create_results = {
        table_key: fs.batch_create(TABLES[table_key], records, batch_size=500) if records else []
        for table_key, records in missing.items()
    }

    final_nodes = linked_children("node", "关联主播")
    final_tasks = linked_children("task", "对应主播")
    final_singletons = {
        "visual": linked_children("visual", "关联主播"),
        "training": linked_children("training", "关联主播"),
        "first_live": linked_children("first_live", "关联主播"),
    }
    complete_interview_updates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    per_anchor: dict[str, Any] = {}
    expected_node_types = {item[0] for item in CHAIN_NODE_TEMPLATE}
    expected_task_types = {item[1] for item in TASK_TEMPLATE}
    for anchor_id, (anchor, interview) in pairs_by_anchor.items():
        node_types = {
            text_value((record.get("fields") or {}).get("节点类型")).strip()
            for record in final_nodes
            if anchor_id in linked_record_ids((record.get("fields") or {}).get("关联主播"))
        }
        task_types = {
            text_value((record.get("fields") or {}).get("任务类型")).strip()
            for record in final_tasks
            if anchor_id in linked_record_ids((record.get("fields") or {}).get("对应主播"))
        }
        counts = {
            "nodes": len(node_types.intersection(expected_node_types)),
            "tasks": len(task_types.intersection(expected_task_types)),
        }
        for table_key, records in final_singletons.items():
            counts[table_key] = sum(
                1
                for record in records
                if anchor_id in linked_record_ids((record.get("fields") or {}).get("关联主播"))
            )
        complete = (
            counts["nodes"] == len(CHAIN_NODE_TEMPLATE)
            and counts["tasks"] == len(TASK_TEMPLATE)
            and all(counts[key] >= 1 for key in ("visual", "training", "first_live"))
        )
        per_anchor[anchor_id] = {"name": anchor_display_name(anchor.get("fields") or {}), "counts": counts, "complete": complete}
        if complete:
            complete_interview_updates.append(
                {
                    "record_id": interview["record_id"],
                    "fields": {
                        "系统处理状态": "已恢复主播档案并补齐流程",
                        "系统处理备注": "系统已恢复主播档案关联，并核对补齐全部后续流程记录。",
                    },
                }
            )
        else:
            failures.append({"anchor_id": anchor_id, "interview_record_id": interview["record_id"], "counts": counts})
    completion_results = fs.batch_update(TABLES["interview"], complete_interview_updates, batch_size=100) if complete_interview_updates else []
    return {
        "checked_anchors": len(pairs_by_anchor),
        "requested_creates": {key: len(records) for key, records in missing.items()},
        "created": {key: len(created_records(results)) for key, results in create_results.items()},
        "complete_anchors": len(complete_interview_updates),
        "failures": failures,
        "per_anchor": per_anchor,
        "create_results": create_results,
        "completion_update_results": completion_results,
    }


def sync_anchor_display_names(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    schema_actions: list[dict[str, Any]] = []
    for table_key, spec in ANCHOR_DISPLAY_TABLES.items():
        table_id = TABLES[table_key]
        fields = {field.get("field_name"): field for field in fs.fields(table_id)}
        if ANCHOR_DISPLAY_FIELD in fields:
            continue
        legacy = fields.get(str(spec["legacy_field"]))
        if legacy and legacy.get("type") == FIELD_TYPES["text"]:
            response = fs.rename_field(table_id, legacy, ANCHOR_DISPLAY_FIELD)
            schema_actions.append({"table": table_key, "action": "rename_display_field", "code": response.get("code"), "msg": response.get("msg")})
            continue
        response = fs.create_field(table_id, ANCHOR_DISPLAY_FIELD, "text")
        schema_actions.append({"table": table_key, "action": "create_display_field", "code": response.get("code"), "msg": response.get("msg")})

    anchors = fs.list_records(TABLES["anchor"], page_size=500)
    anchors_by_id = {record["record_id"]: record for record in anchors}
    anchors_by_number = {
        text_value((record.get("fields") or {}).get("主播编号")).strip(): record
        for record in anchors
        if text_value((record.get("fields") or {}).get("主播编号")).strip()
    }
    report: dict[str, Any] = {"schema_actions": schema_actions, "tables": {}}
    for table_key, spec in ANCHOR_DISPLAY_TABLES.items():
        table_id = TABLES[table_key]
        link_field = str(spec["link_field"])
        updates: list[dict[str, Any]] = []
        matched = 0
        for record in fs.list_records(table_id, page_size=500):
            fields = record.get("fields") or {}
            anchor = next((anchors_by_id.get(record_id) for record_id in linked_record_ids(fields.get(link_field)) if anchors_by_id.get(record_id)), None)
            if not anchor:
                legacy_value = text_value(fields.get(ANCHOR_DISPLAY_FIELD)).strip()
                anchor = anchors_by_number.get(legacy_value)
            if not anchor:
                continue
            display_name = anchor_display_name(anchor.get("fields") or {})
            if not display_name or text_value(fields.get(ANCHOR_DISPLAY_FIELD)).strip() == display_name:
                continue
            updates.append({"record_id": record["record_id"], "fields": {ANCHOR_DISPLAY_FIELD: display_name}})
            matched += 1
        results = fs.batch_update(table_id, updates, batch_size=500) if updates else []
        report["tables"][table_key] = {"updated": matched, "update_results": results}
    write_json(out_dir / "sync_anchor_display_names_result.json", report)
    return report


def build_chain(
    fs: Feishu,
    batch: str,
    limit: int,
    out_dir: Path,
    not_before_ms: int = 0,
    recover_existing_links: bool = False,
) -> dict[str, Any]:
    current_transfer = fs.search_records(TABLES["interview"], TRANSFER_TO_ANCHOR_FIELD, page_size=100)
    legacy_transfer = fs.search_records(TABLES["interview"], LEGACY_TRANSFER_TO_ANCHOR_FIELD, page_size=100)
    interviews_by_id = {record["record_id"]: record for record in [*current_transfer, *legacy_transfer] if record.get("record_id")}
    interviews = list(interviews_by_id.values())
    known_anchors = fs.list_records(TABLES["anchor"], page_size=500)
    known_anchors_by_id = {
        str(anchor.get("record_id") or ""): anchor
        for anchor in known_anchors
        if anchor.get("record_id")
    }
    recovered_updates: list[dict[str, Any]] = []
    recovered_anchor_updates_by_id: dict[str, dict[str, Any]] = {}
    recovered_pairs_by_anchor_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    dangling_links_replaced: list[dict[str, Any]] = []
    skipped_existing_anchors = 0
    selected: list[dict[str, Any]] = []
    for record in interviews:
        fields = record.get("fields") or {}
        linked_ids = linked_record_ids(fields.get("关联主播档案"))
        existing_anchor = find_existing_anchor_for_interview(fs, record, known_anchors_by_id)
        if existing_anchor:
            existing_anchor_id = str(existing_anchor["record_id"])
            existing_anchor_fields = existing_anchor.get("fields") or {}
            source_ids = linked_record_ids(existing_anchor_fields.get("来源面试记录"))
            if recover_existing_links and (
                linked_ids != [existing_anchor_id]
                or fields.get("系统：已生成主播档案") is not True
                or record["record_id"] not in source_ids
            ):
                fields["关联主播档案"] = [existing_anchor_id]
                fields["系统：已生成主播档案"] = True
                recovered_updates.append(
                    {
                        "record_id": record["record_id"],
                        "fields": {
                            "系统：已生成主播档案": True,
                            "关联主播档案": [existing_anchor_id],
                            "系统处理状态": "已恢复主播档案关联",
                            "系统处理备注": "系统检测到已有来源主播档案，已自动恢复面试关联。",
                        },
                    }
                )
                if record["record_id"] not in source_ids:
                    recovered_anchor_updates_by_id[existing_anchor_id] = {
                        "record_id": existing_anchor_id,
                        "fields": {"来源面试记录": [*source_ids, record["record_id"]]},
                    }
                recovered_pairs_by_anchor_id[existing_anchor_id] = (existing_anchor, record)
            elif recover_existing_links and text_value(fields.get("系统处理状态")).strip() == "已恢复主播档案关联":
                recovered_pairs_by_anchor_id[existing_anchor_id] = (existing_anchor, record)
            else:
                skipped_existing_anchors += 1
            continue
        missing_link_ids = [linked_id for linked_id in linked_ids if linked_id not in known_anchors_by_id]
        if linked_ids and len(missing_link_ids) == len(linked_ids):
            fields["关联主播档案"] = []
            dangling_links_replaced.append(
                {
                    "interview_record_id": record["record_id"],
                    "candidate_name": text_value(fields.get("候选人姓名")).strip(),
                    "missing_anchor_ids": missing_link_ids,
                }
            )
        if not eligible_interview(record, not_before_ms):
            continue
        selected.append(record)
        if len(selected) >= limit:
            break

    recovered_results = fs.batch_update(TABLES["interview"], recovered_updates, batch_size=500) if recovered_updates else []
    recovered_anchor_updates = list(recovered_anchor_updates_by_id.values())
    recovered_anchor_results = fs.batch_update(TABLES["anchor"], recovered_anchor_updates, batch_size=500) if recovered_anchor_updates else []
    ownership_sync = sync_interview_anchor_ownership(fs, interviews)
    assignment_sync = ownership_sync["assignment_sync"]
    anchor_operator_sync = ownership_sync["anchor_operator_sync"]
    recovered_child_repair = ensure_recovered_anchor_children(fs, list(recovered_pairs_by_anchor_id.values()), batch)
    anchor_records = []
    base_times: list[datetime] = []
    for index, record in enumerate(selected, start=1):
        fields = record.get("fields") or {}
        interview_dt = parse_feishu_dt(fields.get("面试开始时间") or fields.get("面试时间") or fields.get("邀约时间"))
        base_times.append(interview_dt)
        anchor_no = f"MYZB-AUTO-{record['record_id'][-10:]}"
        name = text_value(fields.get("候选人姓名")) or f"自动化主播{index}"
        anchor_records.append(
            {
                "fields": {
                    "主播编号": anchor_no,
                    ANCHOR_NAME_FIELD: name,
                    "真实姓名": name,
                    "来源面试记录": [record["record_id"]],
                    "主播类型": "娱乐直播",
                    "主播状态": "待开播",
                    "主阶段": "待建联",
                    "招募经济人": fields.get("招募人账号（系统）") or [],
                    "面试官": fields.get("面试官账号（系统）") or [],
                    "运营经济人": fields.get("对接运营账号（系统）") or [],
                    "建联时间": ms(interview_dt + timedelta(hours=2)),
                    "首次建联截止时间": ms(interview_dt + timedelta(hours=2)),
                    "核心顾虑点": text_value(fields.get("跟进情况")),
                    "设备交付": "未交付",
                    "视觉达标": "未开始",
                    "培训达标状态": "未开始",
                    "3分钟录屏状态": "未录制",
                    "首播状态": "未安排",
                    "系统验收状态": "待验收",
                    "自动化批次": batch,
                }
            }
        )
        phone = phone_value(fields.get("联系方式"))
        if phone:
            anchor_records[-1]["fields"]["联系方式"] = phone
        if fields.get("照片"):
            anchor_records[-1]["fields"]["照片"] = fields["照片"]

    anchor_results = fs.batch_create(TABLES["anchor"], anchor_records, batch_size=100)
    anchors = created_records(anchor_results)

    node_records = []
    task_records = []
    visual_records = []
    training_records = []
    first_live_records = []
    interview_updates = []
    for anchor, interview, base_dt in zip(anchors, selected, base_times):
        child_payloads = build_anchor_child_payloads(anchor, interview, base_dt, batch)
        node_records.extend(child_payloads["node"])
        task_records.extend(child_payloads["task"])
        visual_records.extend(child_payloads["visual"])
        training_records.extend(child_payloads["training"])
        first_live_records.extend(child_payloads["first_live"])
        anchor_id = anchor.get("record_id") or anchor.get("id")
        interview_updates.append(
            {
                "record_id": interview["record_id"],
                "fields": {
                    "系统：已生成主播档案": True,
                    "关联主播档案": [anchor_id],
                    "系统处理状态": "已生成主播档案",
                    "系统处理备注": f"自动化批次 {batch} 已生成主播、节点、任务",
                    "自动化批次": batch,
                },
            }
        )

    node_results = fs.batch_create(TABLES["node"], node_records, batch_size=500)
    task_results = fs.batch_create(TABLES["task"], task_records, batch_size=500)
    visual_results = fs.batch_create(TABLES["visual"], visual_records, batch_size=500)
    training_results = fs.batch_create(TABLES["training"], training_records, batch_size=500)
    first_live_results = fs.batch_create(TABLES["first_live"], first_live_records, batch_size=500)
    interview_update_results = fs.batch_update(TABLES["interview"], interview_updates, batch_size=500)
    payload = {
        "batch": batch,
        "checked_interviews": len(interviews),
        "not_before_ms": not_before_ms,
        "recovery_mode_enabled": recover_existing_links,
        "recovered_existing_anchors": len(recovered_updates),
        "recovered_existing_anchor_sources": len(recovered_anchor_updates),
        "dangling_links_replaced": dangling_links_replaced,
        "skipped_existing_anchors": skipped_existing_anchors,
        "assignment_sync": assignment_sync,
        "anchor_operator_sync": anchor_operator_sync,
        "recovered_interview_update_results": recovered_results,
        "recovered_anchor_update_results": recovered_anchor_results,
        "recovered_child_repair": recovered_child_repair,
        "selected_interviews": len(selected),
        "created_anchors": len(anchors),
        "created_nodes": len(created_records(node_results)),
        "created_tasks": len(created_records(task_results)),
        "created_visual_records": len(created_records(visual_results)),
        "created_training_records": len(created_records(training_results)),
        "created_first_live_records": len(created_records(first_live_results)),
        "updated_interviews": len(interview_updates),
        "anchor_results": anchor_results,
        "node_results": node_results,
        "task_results": task_results,
        "visual_results": visual_results,
        "training_results": training_results,
        "first_live_results": first_live_results,
        "interview_update_results": interview_update_results,
    }
    write_json(out_dir / f"build_chain_{batch}_result.json", payload)
    return payload


def sync_interview_photos_to_anchors(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    interviews = fs.list_records(TABLES["interview"], page_size=500)
    anchors = {
        record["record_id"]: record
        for record in fs.list_records(TABLES["anchor"], page_size=500)
    }
    updates: list[dict[str, Any]] = []
    skipped_existing = 0
    for interview in interviews:
        fields = interview.get("fields") or {}
        photos = fields.get("照片") or []
        if not photos:
            continue
        anchor_ids = [
            record_id
            for item in (fields.get("关联主播档案") or [])
            if isinstance(item, dict)
            for record_id in (item.get("record_ids") or [])
            if record_id
        ]
        for anchor_id in anchor_ids:
            anchor = anchors.get(anchor_id)
            if not anchor:
                continue
            if (anchor.get("fields") or {}).get("照片"):
                skipped_existing += 1
                continue
            updates.append({"record_id": anchor_id, "fields": {"照片": photos}})
    results = fs.batch_update(TABLES["anchor"], updates, batch_size=500) if updates else []
    report = {
        "source_interviews": len(interviews),
        "photos_backfilled": len(updates),
        "skipped_existing_photos": skipped_existing,
        "update_results": results,
    }
    write_json(out_dir / "sync_interview_photos_to_anchors_result.json", report)
    return report


def validate_batch(fs: Feishu, batch: str, out_dir: Path) -> dict[str, Any]:
    anchors = [r for r in fs.list_records(TABLES["anchor"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    nodes = [r for r in fs.list_records(TABLES["node"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    tasks = [r for r in fs.list_records(TABLES["task"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    visuals = [r for r in fs.list_records(TABLES["visual"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    trainings = [r for r in fs.list_records(TABLES["training"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    first_lives = [r for r in fs.list_records(TABLES["first_live"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    interviews = [r for r in fs.list_records(TABLES["interview"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    nodes_by_anchor: dict[str, int] = {}
    tasks_by_anchor: dict[str, int] = {}
    visuals_by_anchor: dict[str, int] = {}
    trainings_by_anchor: dict[str, int] = {}
    first_lives_by_anchor: dict[str, int] = {}
    overdue_nodes = 0
    failures = []
    for node in nodes:
        fields = node.get("fields") or {}
        key = text_value(fields.get("关联主播"))
        nodes_by_anchor[key] = nodes_by_anchor.get(key, 0) + 1
        if fields.get("是否超时") is True:
            overdue_nodes += 1
    for task in tasks:
        fields = task.get("fields") or {}
        key = text_value(fields.get("对应主播"))
        tasks_by_anchor[key] = tasks_by_anchor.get(key, 0) + 1
    for visual in visuals:
        fields = visual.get("fields") or {}
        key = text_value(fields.get("关联主播"))
        visuals_by_anchor[key] = visuals_by_anchor.get(key, 0) + 1
        if fields.get("6项核验是否全部通过") is not False:
            failures.append(f"{key} 视觉6项核验状态未被拦截为未通过")
        if text_value(fields.get("准入结果")) != "禁止进入培训":
            failures.append(f"{key} 视觉准入结果没有禁止进入培训")
    for training in trainings:
        fields = training.get("fields") or {}
        key = text_value(fields.get("关联主播"))
        trainings_by_anchor[key] = trainings_by_anchor.get(key, 0) + 1
        if fields.get("是否允许进入首播") is not False:
            failures.append(f"{key} 录屏未通过时仍允许进入首播")
    for first_live in first_lives:
        fields = first_live.get("fields") or {}
        key = text_value(fields.get("关联主播"))
        first_lives_by_anchor[key] = first_lives_by_anchor.get(key, 0) + 1
    for anchor in anchors:
        fields = anchor.get("fields") or {}
        anchor_no = text_value(fields.get("主播编号"))
        if nodes_by_anchor.get(anchor_no, 0) != len(CHAIN_NODE_TEMPLATE):
            failures.append(f"{anchor_no} 节点数量不是 {len(CHAIN_NODE_TEMPLATE)}")
        if tasks_by_anchor.get(anchor_no, 0) != len(TASK_TEMPLATE):
            failures.append(f"{anchor_no} 任务数量不是 {len(TASK_TEMPLATE)}")
        if visuals_by_anchor.get(anchor_no, 0) != 1:
            failures.append(f"{anchor_no} 视觉调试记录数量不是 1")
        if trainings_by_anchor.get(anchor_no, 0) != 1:
            failures.append(f"{anchor_no} 培训录屏记录数量不是 1")
        if first_lives_by_anchor.get(anchor_no, 0) != 1:
            failures.append(f"{anchor_no} 首播筹备记录数量不是 1")
        if not fields.get("来源面试记录"):
            failures.append(f"{anchor_no} 未关联来源面试记录")
    report = {
        "batch": batch,
        "anchors": len(anchors),
        "nodes": len(nodes),
        "tasks": len(tasks),
        "visual_records": len(visuals),
        "training_records": len(trainings),
        "first_live_records": len(first_lives),
        "interviews_updated": len(interviews),
        "expected_nodes_per_anchor": len(CHAIN_NODE_TEMPLATE),
        "expected_tasks_per_anchor": len(TASK_TEMPLATE),
        "overdue_nodes": overdue_nodes,
        "failures": failures,
        "passed": not failures and len(anchors) > 0,
    }
    write_json(out_dir / f"validate_{batch}_result.json", report)
    write_acceptance_markdown(out_dir / f"验收报告_{batch}.md", report)
    return report


def list_contact_users(fs: Feishu) -> list[dict[str, Any]]:
    users_by_id: dict[str, dict[str, Any]] = {}

    def merge_page(path: str, base_query: dict[str, Any]) -> None:
        page_token = ""
        while True:
            query = dict(base_query)
            if page_token:
                query["page_token"] = page_token
            data = fs.api("GET", path, query)
            if data.get("code") != 0:
                raise RuntimeError(f"Failed to list contact users from {path}: {data.get('msg') or data}")
            payload = data.get("data") or {}
            for user in payload.get("items") or []:
                open_id = str(user.get("open_id") or "")
                if open_id:
                    users_by_id[open_id] = user
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                break

    # The generic users endpoint does not reliably return the whole tenant.
    # Read each visible department, then merge root/unassigned users as a fallback.
    for department_id in list_contact_departments(fs):
        merge_page(
            "/contact/v3/users/find_by_department",
            {
                "department_id": department_id,
                "page_size": 50,
                "user_id_type": "open_id",
                "department_id_type": "open_department_id",
            },
        )
    merge_page("/contact/v3/users", {"page_size": 50, "user_id_type": "open_id"})
    return sorted(users_by_id.values(), key=lambda item: (str(item.get("name") or ""), str(item.get("open_id") or "")))


def list_contact_departments(fs: Feishu) -> dict[str, str]:
    response = fs.api(
        "GET",
        "/contact/v3/departments",
        {
            "page_size": 50,
            "parent_department_id": "0",
            "fetch_child": True,
            "department_id_type": "open_department_id",
        },
    )
    departments: dict[str, str] = {}
    for item in (response.get("data") or {}).get("items") or []:
        department_id = str(item.get("open_department_id") or item.get("department_id") or "")
        name = str(item.get("name") or "").strip()
        if department_id and not name:
            detail = fs.api(
                "GET",
                f"/contact/v3/departments/{department_id}",
                {"department_id_type": "open_department_id"},
            )
            department = (detail.get("data") or {}).get("department") or {}
            name = str(department.get("name") or "").strip()
        if department_id:
            departments[department_id] = name
    return departments


def roles_for_departments(names: list[str], job_title: str = "") -> list[str]:
    roles: set[str] = set()
    joined = "、".join([*names, job_title])
    if "培训" in joined:
        roles.add("培训运营")
    if "跟播" in joined:
        roles.add("跟播运营")
    if any(token in joined for token in ("邀约", "招募", "经纪")):
        roles.add("招募经纪人")
    if "面试" in joined:
        roles.add("面试官")
    if any(token in joined for token in ("运营", "娱播")):
        roles.add("对接运营")
    if "视觉" in joined:
        roles.add("视觉")
    if any(token in joined for token in ("管理", "人事", "老板", "总经办")):
        roles.add("管理者")
    return sorted(roles)


def discovered_business_users(fs: Feishu) -> dict[str, dict[str, Any]]:
    specs = [
        ("interview", "招募人账号（系统）", "招募经纪人"),
        ("interview", "面试官账号（系统）", "面试官"),
        ("interview", "对接运营账号（系统）", "对接运营"),
        ("anchor", "招募经济人", "招募经纪人"),
        ("anchor", "运营经济人", "对接运营"),
        ("task", "运营经济人", "对接运营"),
        ("visual", "提交运营", "对接运营"),
        ("training", "培训运营", "培训运营"),
        ("first_live", "跟播运营", "跟播运营"),
        ("review", "跟播人员", "跟播运营"),
    ]
    result: dict[str, dict[str, Any]] = {}
    for table_key, field_name, role in specs:
        for record in fs.list_records(TABLES[table_key], page_size=500):
            value = (record.get("fields") or {}).get(field_name)
            for item in value if isinstance(value, list) else [value]:
                if not isinstance(item, dict):
                    continue
                user_id = str(item.get("id") or item.get("open_id") or item.get("user_id") or "")
                name = str(item.get("name") or item.get("text") or "").strip()
                if not user_id:
                    continue
                entry = result.setdefault(user_id, {"name": name, "roles": set()})
                if name:
                    entry["name"] = name
                entry["roles"].add(role)
    return result


def contact_status(user: dict[str, Any]) -> tuple[str, str]:
    status = user.get("status") or {}
    if status.get("is_resigned") or status.get("is_exited"):
        return "离职", "已离职"
    if status.get("is_frozen") or status.get("is_suspended"):
        return "离职", "已暂停"
    if status.get("is_activated") is False:
        return "在职", "未激活"
    return "在职", "正常"


def epoch_ms(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return int(value * 1000) if value < 100000000000 else int(value)


def sync_personnel_directory(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    full_sync = os.environ.get("CONTACT_FULL_SYNC_ENABLED", "false").lower() == "true"
    users = list_contact_users(fs)
    departments = list_contact_departments(fs)
    discovered = {} if full_sync else discovered_business_users(fs)
    existing = fs.list_records(TABLES["personnel"], page_size=500)
    existing_by_user = {
        user_id: record
        for record in existing
        for user_id in user_ids((record.get("fields") or {}).get("飞书用户"))
    }
    creates = []
    updates = []
    seen: set[str] = set()
    now_ms = int(datetime.now().timestamp() * 1000)
    for user in users:
        open_id = str(user.get("open_id") or "")
        name = str(user.get("name") or user.get("en_name") or "").strip()
        if not open_id or not name:
            continue
        seen.add(open_id)
        department_ids = [str(item) for item in user.get("department_ids") or []]
        department_names = [departments.get(item, "") for item in department_ids]
        department_names = [item for item in department_names if item]
        derived_roles = set(roles_for_departments(department_names, str(user.get("job_title") or "")))
        if not derived_roles:
            derived_roles.update(discovered.get(open_id, {}).get("roles") or set())
        employment_status, account_status = contact_status(user)
        existing_record = existing_by_user.get(open_id)
        existing_fields = (existing_record or {}).get("fields") or {}
        display_name = name
        if existing_fields.get("手工锁定角色") and text_value(existing_fields.get("姓名")).strip():
            display_name = text_value(existing_fields.get("姓名")).strip()
        fields: dict[str, Any] = {
            "姓名": display_name,
            "飞书用户": [{"id": open_id}],
            "在职状态": employment_status,
            "账号状态": account_status,
            "组织部门": " / ".join(department_names),
            "部门ID": "、".join(department_ids),
            "岗位": str(user.get("job_title") or ""),
            "是否创建个人入口": employment_status == "在职" and account_status == "正常",
            "是否参与日历同步": employment_status == "在职" and account_status == "正常",
            "通讯录OpenID": open_id,
            "最后同步时间": now_ms,
            "数据来源": "通讯录自动同步",
            "备注": "由系统从飞书通讯录自动同步；角色按部门自动识别，可由管理员锁定后手工调整。",
        }
        joined_at = epoch_ms(user.get("join_time"))
        if joined_at:
            fields["入职时间"] = joined_at
        if not existing_fields.get("手工锁定角色") and derived_roles:
            fields["角色"] = sorted(derived_roles)
        if employment_status != "在职" and not existing_fields.get("离职时间"):
            fields["离职时间"] = now_ms
        row = {"fields": fields}
        if existing_record:
            row["record_id"] = existing_record["record_id"]
            updates.append(row)
        else:
            creates.append(row)

    for open_id, item in discovered.items():
        if open_id in seen:
            continue
        existing_record = existing_by_user.get(open_id)
        if existing_record:
            existing_fields = existing_record.get("fields") or {}
            changed: dict[str, Any] = {
                "姓名": item.get("name") or text_value(existing_fields.get("姓名")) or open_id,
                "在职状态": "停用",
                "账号状态": "未知",
                "是否创建个人入口": False,
                "是否参与日历同步": False,
                "通讯录OpenID": open_id,
                "最后同步时间": now_ms,
                "数据来源": "业务记录发现",
                "备注": "仅在历史业务记录中发现，未被当前通讯录确认；默认不生成个人入口。",
            }
            if not existing_fields.get("手工锁定角色"):
                changed["角色"] = sorted(set(list_value(existing_fields.get("角色"))) | set(item.get("roles") or []))
            updates.append({"record_id": existing_record["record_id"], "fields": changed})
            continue
        creates.append(
            {
                "fields": {
                    "姓名": item.get("name") or open_id,
                    "飞书用户": [{"id": open_id}],
                    "角色": sorted(item.get("roles") or []),
                    "部门": "其他",
                    "在职状态": "停用",
                    "账号状态": "未知",
                    "是否创建个人入口": False,
                    "是否参与日历同步": False,
                    "通讯录OpenID": open_id,
                    "最后同步时间": now_ms,
                    "数据来源": "业务记录发现",
                    "备注": "仅在历史业务记录中发现，未被当前通讯录确认；默认不生成个人入口。",
                }
            }
        )

    deactivated = 0
    if full_sync:
        for user_id, record in existing_by_user.items():
            if user_id in seen:
                continue
            fields = record.get("fields") or {}
            from_directory = text_value(fields.get("数据来源")) == "通讯录自动同步"
            updates.append(
                {
                    "record_id": record["record_id"],
                    "fields": {
                        "在职状态": "离职" if from_directory else "停用",
                        "账号状态": "已离职" if from_directory else "未知",
                        "是否创建个人入口": False,
                        "是否参与日历同步": False,
                        "最后同步时间": now_ms,
                        **({"离职时间": fields.get("离职时间") or now_ms} if from_directory else {}),
                    },
                }
            )
            deactivated += 1

    create_results = fs.batch_create(TABLES["personnel"], creates) if creates else []
    update_results = fs.batch_update(TABLES["personnel"], updates) if updates else []
    payload = {
        "contact_users": len(users),
        "contact_departments": len(departments),
        "business_users": len(discovered),
        "business_user_scan_skipped": full_sync,
        "existing": len(existing),
        "created": len(creates),
        "updated": len(updates),
        "deactivated": deactivated,
        "full_sync_enabled": full_sync,
        "create_results": create_results,
        "update_results": update_results,
    }
    write_json(out_dir / "sync_personnel_directory_result.json", payload)
    return payload


def sync_interview_personnel_dropdowns(fs: Feishu, out_dir: Path, sync_records: bool = True) -> dict[str, Any]:
    personnel = fs.list_records(TABLES["personnel"], page_size=500)
    active_people: list[dict[str, Any]] = []
    for record in personnel:
        fields = record.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职":
            continue
        if text_value(fields.get("账号状态")) != "正常":
            continue
        name = text_value(fields.get("姓名")).strip()
        ids = user_ids(fields.get("飞书用户"))
        if not name or not ids:
            continue
        active_people.append(
            {
                "name": name,
                "department": text_value(fields.get("组织部门")).strip(),
                "roles": set(list_value(fields.get("角色"))),
                "users": [{"id": user_id} for user_id in ids],
            }
        )

    name_counts: dict[str, int] = {}
    for person in active_people:
        name_counts[person["name"]] = name_counts.get(person["name"], 0) + 1
    for person in active_people:
        display_name = person["name"]
        if name_counts[display_name] > 1:
            suffix = person["department"] or person["users"][0]["id"][-6:]
            display_name = f"{display_name}（{suffix}）"
        person["display_name"] = display_name

    table_id = TABLES["interview"]
    fields_by_name = {field.get("field_name"): field for field in fs.fields(table_id)}
    schema_actions: list[dict[str, Any]] = []
    for visible_name, spec in INTERVIEW_PERSONNEL_DROPDOWNS.items():
        account_name = str(spec["account_field"])
        account_field = fields_by_name.get(account_name)
        visible_field = fields_by_name.get(visible_name)
        if not account_field and visible_field and visible_field.get("type") == FIELD_TYPES["user"]:
            response = fs.rename_field(table_id, visible_field, account_name)
            schema_actions.append({"action": "rename_account_field", "field": visible_name, "response": response})
            if response.get("code") != 0:
                raise RuntimeError(f"Failed to preserve {visible_name} as {account_name}: {response}")
            fields_by_name = {field.get("field_name"): field for field in fs.fields(table_id)}
            account_field = fields_by_name.get(account_name)
            visible_field = fields_by_name.get(visible_name)
        if not account_field:
            response = fs.create_field(table_id, account_name, "user")
            schema_actions.append({"action": "create_account_field", "field": account_name, "response": response})
            if response.get("code") != 0:
                raise RuntimeError(f"Failed to create {account_name}: {response}")
            fields_by_name = {field.get("field_name"): field for field in fs.fields(table_id)}
            account_field = fields_by_name.get(account_name)
            visible_field = fields_by_name.get(visible_name)
        if not visible_field:
            response = fs.create_field(table_id, visible_name, "single_select", ["待同步"])
            schema_actions.append({"action": "create_dropdown_field", "field": visible_name, "response": response})
            if response.get("code") != 0:
                raise RuntimeError(f"Failed to create {visible_name} dropdown: {response}")
            fields_by_name = {field.get("field_name"): field for field in fs.fields(table_id)}
            visible_field = fields_by_name.get(visible_name)
        if not visible_field or visible_field.get("type") != FIELD_TYPES["single_select"]:
            raise RuntimeError(f"{visible_name} must be a single-select field after schema upgrade: {visible_field}")

    dropdown_people: dict[str, dict[str, list[dict[str, str]]]] = {}
    option_updates: list[dict[str, Any]] = []
    fields_by_name = {field.get("field_name"): field for field in fs.fields(table_id)}
    for visible_name, spec in INTERVIEW_PERSONNEL_DROPDOWNS.items():
        required_roles = set(spec["roles"])
        matches = {
            str(person["display_name"]): person["users"]
            for person in active_people
            if not required_roles or set(person["roles"]).intersection(required_roles)
        }
        dropdown_people[visible_name] = matches
        field = fields_by_name[visible_name]
        current_options = [
            str(option.get("name") or "")
            for option in ((field.get("property") or {}).get("options") or [])
            if option.get("name")
        ]
        # Keep historical names as valid options. Replacing this list with only
        # active employees makes Feishu clear older records that use former staff.
        desired_options = list(current_options)
        desired_options.extend(
            name
            for name in sorted(matches, key=lambda value: value.casefold())
            if name not in desired_options
        )
        if current_options != desired_options:
            response = fs.update_select_options(table_id, field, desired_options)
            option_updates.append({"field": visible_name, "options": desired_options, "response": response})
            if response.get("code") != 0:
                raise RuntimeError(f"Failed to update {visible_name} options: {response}")

    if not sync_records:
        report = {
            "active_people": len(active_people),
            "schema_actions": schema_actions,
            "option_updates": option_updates,
            "dropdown_options": {name: sorted(people) for name, people in dropdown_people.items()},
            "record_sync_skipped": True,
        }
        write_json(out_dir / "sync_interview_personnel_dropdowns_result.json", report)
        return report

    active_user_to_display: dict[str, dict[str, str]] = {}
    for visible_name, people in dropdown_people.items():
        active_user_to_display[visible_name] = {
            user["id"]: display_name
            for display_name, users in people.items()
            for user in users
        }

    record_updates: list[dict[str, Any]] = []
    matched_counts = {name: 0 for name in INTERVIEW_PERSONNEL_DROPDOWNS}
    backfilled_counts = {name: 0 for name in INTERVIEW_PERSONNEL_DROPDOWNS}
    unresolved: dict[str, set[str]] = {name: set() for name in INTERVIEW_PERSONNEL_DROPDOWNS}
    invitation_days_updated = 0
    invitation_day_field = (
        "邀约日期（按天分组）"
        if "邀约日期（按天分组）" in fields_by_name
        and fields_by_name["邀约日期（按天分组）"].get("type") != 20
        else ""
    )
    for record in fs.list_records(table_id, page_size=500):
        fields = record.get("fields") or {}
        changed: dict[str, Any] = {}
        for visible_name, spec in INTERVIEW_PERSONNEL_DROPDOWNS.items():
            account_name = str(spec["account_field"])
            selected = text_value(fields.get(visible_name)).strip()
            existing_ids = user_ids(fields.get(account_name))
            if selected:
                users = dropdown_people[visible_name].get(selected)
                if not users and visible_name == "招募人":
                    users = self_selected_creator_users(fields, selected)
                if not users:
                    unresolved[visible_name].add(selected)
                    continue
                if user_ids(users) != existing_ids:
                    changed[account_name] = users
                    matched_counts[visible_name] += 1
                continue
            if len(existing_ids) == 1:
                display_name = active_user_to_display[visible_name].get(existing_ids[0])
                if display_name:
                    changed[visible_name] = display_name
                    backfilled_counts[visible_name] += 1
        if invitation_day_field:
            day = invitation_day(fields.get("邀约时间"))
            if day and text_value(fields.get(invitation_day_field)) != day:
                changed[invitation_day_field] = day
                invitation_days_updated += 1
        if changed:
            record_updates.append({"record_id": record["record_id"], "fields": changed})

    update_results = fs.batch_update(table_id, record_updates, batch_size=500) if record_updates else []
    report = {
        "active_people": len(active_people),
        "dropdown_options": {name: sorted(people) for name, people in dropdown_people.items()},
        "schema_actions": schema_actions,
        "option_updates": option_updates,
        "records_updated": len(record_updates),
        "matched_accounts": matched_counts,
        "backfilled_dropdowns": backfilled_counts,
        "invitation_days_updated": invitation_days_updated,
        "unresolved_values": {name: sorted(values) for name, values in unresolved.items()},
        "update_results": update_results,
    }
    write_json(out_dir / "sync_interview_personnel_dropdowns_result.json", report)
    return report


def has_meaningful_field_value(value: Any) -> bool:
    return value not in (None, "", [], {}) and value is not False


RECRUITMENT_ATTRIBUTION_FIELDS = (
    "候选人姓名",
    "联系方式",
    "投递渠道",
    "年龄",
    "面试岗位",
    "面试地点",
    "邀约时间",
    "性别",
    "城市",
    "主播照片",
)


def has_recruitment_input(fields: dict[str, Any]) -> bool:
    return any(has_meaningful_field_value(fields.get(name)) for name in RECRUITMENT_ATTRIBUTION_FIELDS)


def self_selected_creator_users(fields: dict[str, Any], selected_name: str) -> list[dict[str, str]]:
    """Trust the record creator when a recruiter explicitly selected their own exact name."""
    selected = selected_name.strip()
    creators = fields.get(SYSTEM_CREATED_BY_FIELD) or []
    creator_ids = user_ids(creators)
    creator_items = creators if isinstance(creators, list) else [creators]
    creator_names = {
        str(item.get("name") or item.get("en_name") or "").strip()
        for item in creator_items if isinstance(item, dict)
    }
    if has_recruitment_input(fields) and selected and len(creator_ids) == 1 and creator_names == {selected}:
        return [{"id": creator_ids[0]}]
    return []


def safe_to_attribute_recruiter_from_modifier(fields: dict[str, Any]) -> bool:
    """Only infer a recruiter from the modifier while the row is still in recruitment."""
    downstream_fields = (
        "面试官",
        "面试官账号（系统）",
        "对接运营",
        "对接运营账号（系统）",
        "面试开始时间",
        "面试结束时间",
        "面试结果",
        "面试状态",
        TRANSFER_TO_ANCHOR_FIELD,
        LEGACY_TRANSFER_TO_ANCHOR_FIELD,
        "关联主播档案",
        "是否生成主播档案",
    )
    return has_recruitment_input(fields) and not any(
        has_meaningful_field_value(fields.get(name)) for name in downstream_fields
    )


def sync_missing_interview_display_fields(fs: Feishu, out_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """Keep derived owner and date-group display fields consistent."""
    table_id = TABLES["interview"]
    updates_by_id: dict[str, dict[str, Any]] = {}
    owner_repairs = {name: 0 for name in INTERVIEW_PERSONNEL_DROPDOWNS}
    account_repairs = {name: 0 for name in INTERVIEW_PERSONNEL_DROPDOWNS}
    creator_attribution_repairs = 0
    modifier_attribution_repairs = 0
    interview_records = fs.list_records(table_id, page_size=500)
    personnel_records = fs.list_records(TABLES["personnel"], page_size=500)
    active_people: list[dict[str, Any]] = []
    for person in personnel_records:
        fields = person.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职" or text_value(fields.get("账号状态")) != "正常":
            continue
        name = text_value(fields.get("姓名")).strip()
        ids = user_ids(fields.get("飞书用户"))
        if name and ids:
            active_people.append(
                {
                    "name": name,
                    "department": text_value(fields.get("组织部门")).strip(),
                    "roles": set(list_value(fields.get("角色"))),
                    "users": [{"id": user_id} for user_id in ids],
                }
            )
    name_counts: dict[str, int] = {}
    for person in active_people:
        name_counts[person["name"]] = name_counts.get(person["name"], 0) + 1
    for person in active_people:
        display_name = person["name"]
        if name_counts[display_name] > 1:
            display_name = f"{display_name}（{person['department'] or person['users'][0]['id'][-6:]}）"
        person["display_name"] = display_name
    dropdown_people: dict[str, dict[str, list[dict[str, str]]]] = {}
    active_user_to_display: dict[str, dict[str, str]] = {}
    for visible_name, spec in INTERVIEW_PERSONNEL_DROPDOWNS.items():
        required_roles = set(spec["roles"])
        matches = {
            str(person["display_name"]): person["users"]
            for person in active_people
            if not required_roles or set(person["roles"]).intersection(required_roles)
        }
        dropdown_people[visible_name] = matches
        active_user_to_display[visible_name] = {
            user["id"]: display_name
            for display_name, users in matches.items()
            for user in users
        }

    for visible_name, spec in INTERVIEW_PERSONNEL_DROPDOWNS.items():
        account_name = str(spec["account_field"])
        for record in interview_records:
            fields = record.get("fields") or {}
            selected = text_value(fields.get(visible_name)).strip()
            existing_ids = user_ids(fields.get(account_name))
            if selected:
                users = dropdown_people[visible_name].get(selected)
                if not users and visible_name == "招募人":
                    users = self_selected_creator_users(fields, selected)
                if users and user_ids(users) != existing_ids:
                    updates_by_id.setdefault(record["record_id"], {})[account_name] = users
                    account_repairs[visible_name] += 1
                continue
            if not existing_ids:
                continue
            if len(existing_ids) == 1 and existing_ids[0] in active_user_to_display[visible_name]:
                updates_by_id.setdefault(record["record_id"], {})[visible_name] = active_user_to_display[visible_name][existing_ids[0]]
                owner_repairs[visible_name] += 1
                continue
            names = {
                str(item.get("name") or item.get("en_name") or "").strip()
                for item in (fields.get(account_name) or [])
                if isinstance(item, dict) and (item.get("name") or item.get("en_name"))
            }
            if len(names) != 1:
                continue
            updates_by_id.setdefault(record["record_id"], {})[visible_name] = next(iter(names))
            owner_repairs[visible_name] += 1

    table_fields = fs.fields(table_id)
    field_names = {str(field.get("field_name") or "") for field in table_fields}
    if SYSTEM_CREATED_BY_FIELD in field_names:
        recruiters_by_user = active_user_to_display["招募人"]
        for record in interview_records:
            fields = record.get("fields") or {}
            if text_value(fields.get("招募人")).strip() or user_ids(fields.get("招募人账号（系统）")):
                continue
            creator_ids = user_ids(fields.get(SYSTEM_CREATED_BY_FIELD))
            recruiter_id = (
                creator_ids[0]
                if has_recruitment_input(fields) and len(creator_ids) == 1 and creator_ids[0] in recruiters_by_user
                else ""
            )
            attribution_source = "creator"
            if not recruiter_id and safe_to_attribute_recruiter_from_modifier(fields):
                modifier_ids = user_ids(fields.get(SYSTEM_MODIFIED_BY_FIELD))
                if len(modifier_ids) == 1 and modifier_ids[0] in recruiters_by_user:
                    recruiter_id = modifier_ids[0]
                    attribution_source = "modifier"
            recruiter_name = recruiters_by_user.get(recruiter_id)
            if not recruiter_name:
                continue
            updates_by_id.setdefault(record["record_id"], {}).update(
                {
                    "招募人": recruiter_name,
                    "招募人账号（系统）": [{"id": recruiter_id}],
                }
            )
            owner_repairs["招募人"] += 1
            if attribution_source == "creator":
                creator_attribution_repairs += 1
            else:
                modifier_attribution_repairs += 1

    formula_managed_date_group = invitation_day_group_is_formula(table_fields)
    group_field = "" if formula_managed_date_group else "邀约日期（按天分组）"
    day_records = [] if formula_managed_date_group else interview_records
    date_group_repairs = 0
    date_group_changes: list[dict[str, str]] = []
    for record in day_records:
        fields = record.get("fields") or {}
        day = invitation_day(fields.get("邀约时间"))
        current_day = text_value(fields.get(group_field)).strip()
        if not day or current_day == day:
            continue
        updates_by_id.setdefault(record["record_id"], {})[group_field] = day
        date_group_repairs += 1
        date_group_changes.append({"record_id": record["record_id"], "before": current_day, "after": day})

    updates = [
        {"record_id": record_id, "fields": fields}
        for record_id, fields in updates_by_id.items()
    ]
    update_results = fs.batch_update(table_id, updates, batch_size=100) if updates and not dry_run else []
    report = {
        "mode": "dry_run" if dry_run else "apply",
        "records_updated": len(updates),
        "owner_repairs": owner_repairs,
        "account_repairs": account_repairs,
        "creator_attribution_repairs": creator_attribution_repairs,
        "modifier_attribution_repairs": modifier_attribution_repairs,
        "date_group_repairs": date_group_repairs,
        "date_group_formula_managed": formula_managed_date_group,
        "date_group_changes": date_group_changes,
        "update_results": update_results,
    }
    write_json(out_dir / "sync_missing_interview_display_fields_result.json", report)
    return report


def sync_one_interview_personnel_assignment(fs: Feishu, record_id: str, out_dir: Path) -> dict[str, Any]:
    """Resolve one changed interview row without scanning the whole interview table."""
    response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES['interview']}/records/{record_id}")
    record = (response.get("data") or {}).get("record") or (response.get("data") or {})
    if response.get("code") != 0 or not record.get("record_id"):
        raise RuntimeError(f"Unable to read interview record {record_id}: {response}")

    active_people: list[dict[str, Any]] = []
    for person_record in fs.list_records(TABLES["personnel"], page_size=500):
        fields = person_record.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职" or text_value(fields.get("账号状态")) != "正常":
            continue
        name = text_value(fields.get("姓名")).strip()
        users = [{"id": user_id} for user_id in user_ids(fields.get("飞书用户"))]
        if name and users:
            active_people.append(
                {
                    "name": name,
                    "department": text_value(fields.get("组织部门")).strip(),
                    "users": users,
                    "roles": set(list_value(fields.get("角色"))),
                }
            )
    name_counts: dict[str, int] = {}
    for person in active_people:
        name_counts[person["name"]] = name_counts.get(person["name"], 0) + 1
    people_by_name: dict[str, list[dict[str, str]]] = {}
    display_by_user_id: dict[str, str] = {}
    recruiters_by_user_id: dict[str, str] = {}
    for person in active_people:
        name = str(person["name"])
        display_name = name
        if name_counts[name] > 1:
            display_name = f"{name}（{person['department'] or person['users'][0]['id'][-6:]}）"
        people_by_name[display_name] = person["users"]
        for user in person["users"]:
            user_id = str(user["id"])
            display_by_user_id[user_id] = display_name
            if "招募经纪人" in person["roles"]:
                recruiters_by_user_id[user_id] = display_name

    fields = record.get("fields") or {}
    changed: dict[str, Any] = {}
    unresolved: list[str] = []
    for visible_name, spec in INTERVIEW_PERSONNEL_DROPDOWNS.items():
        selected = text_value(fields.get(visible_name)).strip()
        account_name = str(spec["account_field"])
        existing_ids = user_ids(fields.get(account_name))
        if not selected:
            if len(existing_ids) == 1 and existing_ids[0] in display_by_user_id:
                changed[visible_name] = display_by_user_id[existing_ids[0]]
            continue
        users = people_by_name.get(selected)
        if not users and visible_name == "招募人":
            users = self_selected_creator_users(fields, selected)
        if not users:
            unresolved.append(selected)
            continue
        if existing_ids != user_ids(users):
            changed[account_name] = users
    visible_recruiter = text_value(fields.get("招募人")).strip()
    hidden_recruiter_ids = user_ids(fields.get("招募人账号（系统）"))
    if not visible_recruiter and not hidden_recruiter_ids:
        creator_ids = user_ids(fields.get(SYSTEM_CREATED_BY_FIELD))
        recruiter_id = (
            creator_ids[0]
            if has_recruitment_input(fields) and len(creator_ids) == 1 and creator_ids[0] in recruiters_by_user_id
            else ""
        )
        if not recruiter_id and safe_to_attribute_recruiter_from_modifier(fields):
            modifier_ids = user_ids(fields.get(SYSTEM_MODIFIED_BY_FIELD))
            if len(modifier_ids) == 1 and modifier_ids[0] in recruiters_by_user_id:
                recruiter_id = modifier_ids[0]
        if recruiter_id:
            changed["招募人"] = recruiters_by_user_id[recruiter_id]
            changed["招募人账号（系统）"] = [{"id": recruiter_id}]
    if not invitation_day_group_is_formula(fs.fields(TABLES["interview"])):
        group_field = "邀约日期（按天分组）"
        desired_day = invitation_day(fields.get("邀约时间"))
        current_day = text_value(fields.get(group_field)).strip()
        if desired_day and desired_day != current_day:
            changed[group_field] = desired_day
    results = fs.batch_update(TABLES["interview"], [{"record_id": record_id, "fields": changed}]) if changed else []
    report = {"record_id": record_id, "updated_fields": sorted(changed), "unresolved_values": sorted(set(unresolved)), "results": results}
    write_json(out_dir / f"sync_interview_assignment_{record_id}.json", report)
    return report


def normalized_owner_names(*values: Any) -> set[str]:
    result: set[str] = set()
    for name in owner_names(*values):
        normalized = name.lstrip("@").strip()
        if normalized:
            result.add(normalized)
    return result


def is_demo_batch(value: Any) -> bool:
    batch = text_value(value).strip()
    return any(batch.startswith(prefix) for prefix in DEMO_BATCH_PREFIXES)


def sync_person_assignment_fields(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    personnel = fs.list_records(TABLES["personnel"], page_size=500)
    name_to_users: dict[str, list[dict[str, str]]] = {}
    ambiguous_names: set[str] = set()
    for record in personnel:
        fields = record.get("fields") or {}
        ids = user_ids(fields.get("飞书用户"))
        if not ids:
            continue
        users = [{"id": user_id} for user_id in ids]
        for name in normalized_owner_names(fields.get("姓名"), fields.get("匹配别名")):
            existing = name_to_users.get(name)
            if existing and existing != users:
                ambiguous_names.add(name)
                name_to_users.pop(name, None)
            elif name not in ambiguous_names:
                name_to_users[name] = users

    table_specs = {
        "interview": [],
        "anchor": [],
        "task": [("负责人", "运营经济人")],
        "visual": [],
        "training": [],
        "first_live": [],
        "review": [("跟播人员（历史文本）", "跟播人员")],
    }
    report: dict[str, Any] = {
        "known_people": len(personnel),
        "matched_names": sorted(name_to_users),
        "ambiguous_names": sorted(ambiguous_names),
        "tables": {},
    }
    for table_key, mappings in table_specs.items():
        updates = []
        matched_counts = {target: 0 for _, target in mappings}
        for record in fs.list_records(TABLES[table_key], page_size=500):
            fields = record.get("fields") or {}
            changed: dict[str, Any] = {}
            for source, target in mappings:
                if user_ids(fields.get(target)):
                    continue
                source_names = normalized_owner_names(fields.get(source))
                users = next((name_to_users[name] for name in source_names if name in name_to_users), None)
                if not users:
                    continue
                changed[target] = users
                matched_counts[target] += 1
            if changed:
                updates.append({"record_id": record["record_id"], "fields": changed})
        results = fs.batch_update(TABLES[table_key], updates, batch_size=500) if updates else []
        report["tables"][table_key] = {
            "updates": len(updates),
            "matched_fields": matched_counts,
            "results": results,
        }
    write_json(out_dir / "sync_person_assignment_fields_result.json", report)
    return report


def ensure_personal_views(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    def list_views(table_id: str) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query: dict[str, Any] = {"page_size": 100}
            if page_token:
                query["page_token"] = page_token
            response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views", query)
            if response.get("code") != 0:
                raise RuntimeError(f"Failed to list views for {table_id}: {response}")
            payload = response.get("data") or {}
            views.extend(payload.get("items") or [])
            if not payload.get("has_more"):
                return views
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                return views

    personnel = fs.list_records(TABLES["personnel"], page_size=500)
    active_people: dict[str, dict[str, Any]] = {}
    all_names: set[str] = set()
    for record in personnel:
        fields = record.get("fields") or {}
        name = text_value(fields.get("姓名")).strip()
        if name:
            all_names.add(name)
        if text_value(fields.get("在职状态")) != "在职":
            continue
        if text_value(fields.get("账号状态")) != "正常":
            continue
        if fields.get("是否创建个人入口") is not True:
            continue
        for user_id in user_ids(fields.get("飞书用户")):
            active_people[user_id] = {"name": name, "roles": set(list_value(fields.get("角色")))}
    specs = [
        ("interview", "招募人账号（系统）", "招聘", "候选人", {"招募经纪人"}),
        ("interview", "面试官账号（系统）", "面试", "候选人", {"面试官"}),
        ("interview", "对接运营账号（系统）", "运营", "候选人", {"对接运营"}),
        ("anchor", "运营经济人", "运营", "主播", {"对接运营", "培训运营", "跟播运营"}),
        ("task", "运营经济人", "运营", "日程", {"对接运营", "培训运营", "跟播运营"}),
        ("visual", "提交运营", "运营", "视觉", {"对接运营"}),
        ("visual", "视觉处理人", "视觉", "任务", {"视觉"}),
        ("training", "培训运营", "运营", "培训", {"对接运营", "培训运营"}),
        ("first_live", "跟播运营", "运营", "首播", {"对接运营", "跟播运营"}),
        ("review", "跟播人员", "运营", "复盘", {"对接运营", "跟播运营"}),
    ]
    report: dict[str, Any] = {"created": [], "updated": [], "unchanged": [], "hidden": [], "skipped": []}
    table_keys = sorted({item[0] for item in specs})
    views_by_table = {table_key: list_views(TABLES[table_key]) for table_key in table_keys}
    views_by_name: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for table_key, views in views_by_table.items():
        grouped: dict[str, list[dict[str, Any]]] = {}
        for view in views:
            grouped.setdefault(str(view.get("view_name") or ""), []).append(view)
        views_by_name[table_key] = grouped
    field_ids_by_table = {
        table_key: {field.get("field_name"): field.get("field_id") for field in fs.fields(TABLES[table_key])}
        for table_key in table_keys
    }
    interview_hidden_fields = [
        field_ids_by_table["interview"].get(name)
        for name in [
            "招募人",
            "面试官账号（系统）",
            "对接运营账号（系统）",
            SYSTEM_CREATED_BY_FIELD,
            SYSTEM_CREATED_AT_FIELD,
            SYSTEM_MODIFIED_BY_FIELD,
            SYSTEM_MODIFIED_AT_FIELD,
            "系统：已生成主播档案",
            "自动化批次",
            "系统处理状态",
            "系统处理备注",
            "邀约是否同步日历",
            "邀约日历事件ID",
            "邀约日历同步指纹",
            "面试是否同步日历",
            "面试日历事件ID",
            "面试日历同步指纹",
            "父记录 2",
            "父记录 3",
        ]
        if field_ids_by_table["interview"].get(name)
    ]
    desired_names: dict[str, set[str]] = {table_key: set() for table_key in table_keys}
    desired_bodies: dict[str, dict[str, dict[str, Any]]] = {table_key: {} for table_key in table_keys}
    possible_generated: dict[str, set[str]] = {table_key: set() for table_key in table_keys}
    for table_key, _field_name, prefix, suffix, _roles in specs:
        possible_generated[table_key].update({f"{prefix}_{name}_{suffix}"[:100] for name in all_names})
    for user_id, item in active_people.items():
        name = str(item.get("name") or "").strip()
        roles = set(item.get("roles") or set())
        if not name or not user_id:
            continue
        for table_key, field_name, prefix, suffix, required_roles in specs:
            if not roles.intersection(required_roles):
                continue
            view_name = f"{prefix}_{name}_{suffix}"[:100]
            desired_names[table_key].add(view_name)
            body = {
                "view_name": view_name,
                "property": {
                    "filter_info": {
                        "conditions": [
                            {
                                "field_id": field_ids_by_table[table_key][field_name],
                                "operator": "is",
                                "value": json.dumps([user_id], ensure_ascii=False),
                            }
                        ],
                        "conjunction": "and",
                    },
                    **({"hidden_fields": interview_hidden_fields} if table_key == "interview" else {}),
                },
            }
            desired_bodies[table_key][view_name] = body

    def update_view(table_key: str, view_name: str, view_id: str, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = fs.api(
            "PATCH",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES[table_key]}/views/{view_id}",
            body=body,
        )
        return "updated", {"table": table_key, "view_name": view_name, "code": response.get("code"), "msg": response.get("msg")}

    def create_view(table_key: str, view_name: str, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = fs.api(
            "POST",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES[table_key]}/views",
            body={"view_name": view_name, "view_type": "grid"},
        )
        view = (response.get("data") or {}).get("view") or response.get("data") or {}
        view_id = str(view.get("view_id") or "")
        if response.get("code") == 0 and view_id:
            response = fs.api(
                "PATCH",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES[table_key]}/views/{view_id}",
                body=body,
            )
            if response.get("code") != 0:
                fs.api("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES[table_key]}/views/{view_id}")
        return "created", {"table": table_key, "view_name": view_name, "code": response.get("code"), "msg": response.get("msg")}

    def delete_view(table_key: str, view_name: str, view_id: str) -> tuple[str, dict[str, Any]]:
        response = fs.api(
            "DELETE",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLES[table_key]}/views/{view_id}",
        )
        return "hidden", {"table": table_key, "view_name": view_name, "code": response.get("code"), "msg": response.get("msg")}

    operations: list[tuple[Any, tuple[Any, ...]]] = []
    for table_key in table_keys:
        for view_name, body in desired_bodies[table_key].items():
            existing_views = views_by_name[table_key].get(view_name) or []
            if existing_views:
                operations.append((update_view, (table_key, view_name, existing_views[0]["view_id"], body)))
                for duplicate in existing_views[1:]:
                    operations.append((delete_view, (table_key, view_name, duplicate["view_id"])))
            else:
                operations.append((create_view, (table_key, view_name, body)))
        stale_names = possible_generated[table_key] - desired_names[table_key]
        for view_name in stale_names:
            for view in views_by_name[table_key].get(view_name) or []:
                operations.append((delete_view, (table_key, view_name, view["view_id"])))

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(operation, *args) for operation, args in operations]
        for future in as_completed(futures):
            try:
                action, item = future.result()
            except Exception as exc:
                report["skipped"].append({"code": -1, "msg": str(exc)})
                continue
            if item.get("code") == 0:
                report[action].append(item)
            else:
                report["skipped"].append(item)
    write_json(out_dir / "ensure_personal_views_result.json", report)
    return report


def sync_personal_workbench_rows(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    workbench_fields = {field.get("field_name"): field for field in fs.fields(WORKBENCH_TABLE)}
    if "员工账号" not in workbench_fields:
        response = fs.create_field(WORKBENCH_TABLE, "员工账号", "user")
        if response.get("code") != 0:
            raise RuntimeError(f"Failed to create workbench employee field: {response}")
        workbench_fields = {field.get("field_name"): field for field in fs.fields(WORKBENCH_TABLE)}
    employee_field = workbench_fields["员工账号"]

    people_by_name: dict[str, list[dict[str, str]]] = {}
    ambiguous_names: set[str] = set()
    people_by_user: dict[str, dict[str, Any]] = {}
    for record in fs.list_records(TABLES["personnel"], page_size=500):
        fields = record.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职":
            continue
        if text_value(fields.get("账号状态")) != "正常":
            continue
        if fields.get("是否创建个人入口") is not True:
            continue
        ids = user_ids(fields.get("飞书用户"))
        if not ids:
            continue
        users = [{"id": user_id} for user_id in ids]
        name = text_value(fields.get("姓名")).strip()
        aliases = normalized_owner_names(fields.get("姓名"), fields.get("匹配别名"))
        for alias in aliases:
            existing_users = people_by_name.get(alias)
            if existing_users and user_ids(existing_users) != ids:
                ambiguous_names.add(alias)
                people_by_name.pop(alias, None)
            elif alias not in ambiguous_names:
                people_by_name[alias] = users
        for user_id in ids:
            people_by_user[user_id] = {"name": name, "aliases": aliases, "roles": set(list_value(fields.get("角色")))}

    view_specs = [
        ("anchor", "运营_", "_主播", "对接运营", "主播", "打开我的主播"),
        ("task", "运营_", "_日程", "对接运营", "日程", "打开我的日程"),
        ("interview", "招聘_", "_候选人", "招募经纪人", "候选人", "打开我的候选人"),
        ("interview", "面试_", "_候选人", "面试官", "面试候选人", "打开我的面试候选人"),
        ("interview", "运营_", "_候选人", "对接运营", "对接候选人", "打开我的对接候选人"),
        ("visual", "运营_", "_视觉", "对接运营", "视觉任务", "打开我的视觉任务"),
        ("visual", "视觉_", "_任务", "视觉", "视觉任务", "打开我的视觉任务"),
        ("training", "运营_", "_培训", "培训运营", "培训", "打开我的培训"),
        ("first_live", "运营_", "_首播", "跟播运营", "首播", "打开我的首播"),
        ("review", "运营_", "_复盘", "跟播运营", "复盘", "打开我的复盘"),
    ]
    def list_table_views(table_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query: dict[str, Any] = {"page_size": 100}
            if page_token:
                query["page_token"] = page_token
            response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views", query)
            if response.get("code") != 0:
                raise RuntimeError(f"Failed to list views for {table_id}: {response}")
            payload = response.get("data") or {}
            items.extend(payload.get("items") or [])
            if not payload.get("has_more"):
                return items
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                return items

    desired: dict[str, dict[str, Any]] = {}
    for table_key, prefix, suffix, role, target, link_text in view_specs:
        for view in list_table_views(TABLES[table_key]):
            view_name = str(view.get("view_name") or "")
            if not (view_name.startswith(prefix) and view_name.endswith(suffix)):
                continue
            name = view_name[len(prefix) : -len(suffix)].strip("_")
            if not name or name in {"我的", "我邀约的", "主播总览", "待首播", "需补资料与阻塞"}:
                continue
            users = people_by_name.get(name)
            if not users:
                continue
            key = f"个人入口：{name}的{target}"
            link = f"https://hxyyb89w4s2.feishu.cn/base/{APP_TOKEN}?table={TABLES[table_key]}&view={view['view_id']}"
            desired[key] = {
                "我要做什么": key,
                "谁来操作": role,
                "操作内容": f"{name}直接查看自己负责的{target}",
                "系统自动": "系统按飞书人员账号自动筛选本人记录",
                "完成时限": "每天使用",
                "点这里办理": {"link": link, "text": link_text},
                "员工账号": users,
            }

    existing = fs.list_records(WORKBENCH_TABLE, page_size=500)
    existing_by_key = {
        text_value((record.get("fields") or {}).get("我要做什么")): record
        for record in existing
        if text_value((record.get("fields") or {}).get("我要做什么")).startswith("个人入口：")
    }
    creates = [{"fields": fields} for key, fields in desired.items() if key not in existing_by_key]
    updates = []
    unchanged = []
    for key, fields in desired.items():
        existing_record = existing_by_key.get(key)
        if not existing_record:
            continue
        existing_fields = existing_record.get("fields") or {}
        if all(
            user_ids(existing_fields.get(field_name)) == user_ids(value)
            if field_name == "员工账号"
            else existing_fields.get(field_name) == value
            for field_name, value in fields.items()
        ):
            unchanged.append(key)
            continue
        updates.append({"record_id": existing_record["record_id"], "fields": fields})
    create_results = fs.batch_create(WORKBENCH_TABLE, creates, batch_size=500) if creates else []
    update_results = fs.batch_update(WORKBENCH_TABLE, updates, batch_size=500) if updates else []
    stale = [record for key, record in existing_by_key.items() if key not in desired]
    delete_results = [
        fs.api("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/records/{record['record_id']}")
        for record in stale
    ]
    def list_workbench_views() -> list[dict[str, Any]]:
        return list_table_views(WORKBENCH_TABLE)

    def view_property(view_id: str) -> dict[str, Any]:
        response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views/{view_id}")
        if response.get("code") != 0:
            raise RuntimeError(f"Failed to read workbench view {view_id}: {response}")
        return (((response.get("data") or {}).get("view") or {}).get("property") or {})

    def condition_values(value: Any) -> list[str]:
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = [value]
        if not isinstance(parsed, list):
            parsed = [parsed]
        result = []
        for item in parsed:
            if isinstance(item, dict):
                item = item.get("id") or item.get("open_id") or item.get("text") or item.get("name")
            if item not in (None, ""):
                result.append(str(item))
        return result

    desired_users: dict[str, dict[str, Any]] = {}
    for fields in desired.values():
        ids = user_ids(fields.get("员工账号"))
        if ids and ids[0] in people_by_user:
            desired_users[ids[0]] = people_by_user[ids[0]]

    def matching_user(token: str) -> str:
        normalized = token.lstrip("@").strip()
        if not normalized:
            return ""
        candidates = []
        for user_id, person in desired_users.items():
            labels = {str(person.get("name") or "")} | set(person.get("aliases") or set())
            if any(normalized in label or label in normalized for label in labels if label):
                candidates.append(user_id)
        return candidates[0] if len(candidates) == 1 else ""

    views = list_workbench_views()
    mapped_views: dict[str, list[dict[str, Any]]] = {}
    account_filtered_views: list[tuple[dict[str, Any], str]] = []
    hidden_field_updates = []
    system_view_names = {"按岗位点这里办理", "表格 2(dashboard_view)"}
    for view in views:
        view_id = str(view.get("view_id") or "")
        view_name = str(view.get("view_name") or "")
        if view.get("view_type") != "grid" or not view_id:
            continue
        prop = view_property(view_id)
        view_with_property = {**view, "_property": prop}
        conditions = ((prop.get("filter_info") or {}).get("conditions") or [])
        matched_user = ""
        for condition in conditions:
            field_id = str(condition.get("field_id") or "")
            values = condition_values(condition.get("value"))
            if field_id == str(employee_field.get("field_id") or "") and values:
                matched_user = str(values[0])
                account_filtered_views.append((view_with_property, matched_user))
                break
            if field_id == str(workbench_fields["我要做什么"].get("field_id") or "") and values:
                matched_user = matching_user(str(values[0]))
        if not matched_user and view_name not in system_view_names:
            matched_user = matching_user(view_name)
        if matched_user:
            mapped_views.setdefault(matched_user, []).append(view_with_property)
        if employee_field["field_id"] not in (prop.get("hidden_fields") or []):
            hidden = list(dict.fromkeys([*(prop.get("hidden_fields") or []), employee_field["field_id"]]))
            response = fs.api(
                "PATCH",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views/{view_id}",
                body={"view_name": view_name, "property": {"hidden_fields": hidden}},
            )
            hidden_field_updates.append({"view_name": view_name, "response": response})

    personal_view_report: dict[str, Any] = {
        "created": [],
        "updated": [],
        "deleted": [],
        "hidden_field_updates": hidden_field_updates,
    }

    def short_name(name: str, aliases: set[str]) -> str:
        if name.startswith("米游文化"):
            return name[len("米游文化") :][:100]
        if "(" in name and name.endswith(")"):
            base, inner = name[:-1].split("(", 1)
            if base == inner:
                return base[:100]
        clean_aliases = [alias for alias in sorted(aliases, key=lambda item: (len(item), item)) if alias and not alias.startswith("@")]
        if clean_aliases:
            return clean_aliases[0][:100]
        return name[:100]

    used_view_names = {str(view.get("view_name") or "") for view in views}
    for user_id, person in desired_users.items():
        existing_views = mapped_views.get(user_id) or []
        if existing_views:
            view = min(existing_views, key=lambda item: len(str(item.get("view_name") or "")))
            extras = [item for item in existing_views if item.get("view_id") != view.get("view_id")]
        else:
            view_name = short_name(str(person.get("name") or ""), set(person.get("aliases") or set()))
            if view_name in used_view_names:
                view_name = str(person.get("name") or view_name)[:100]
            suffix = 2
            base_view_name = view_name
            while view_name in used_view_names:
                marker = f"_{suffix}"
                view_name = f"{base_view_name[:100 - len(marker)]}{marker}"
                suffix += 1
            created = fs.api(
                "POST",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views",
                body={"view_name": view_name, "view_type": "grid"},
            )
            if created.get("code") != 0:
                raise RuntimeError(f"Failed to create personal workbench view {view_name}: {created}")
            view = (created.get("data") or {}).get("view") or created.get("data") or {}
            if not view.get("view_id"):
                raise RuntimeError(f"Personal workbench view id missing for {view_name}: {created}")
            view["_property"] = {}
            used_view_names.add(view_name)
            personal_view_report["created"].append({"user_id": user_id, "view_name": view_name, "response": created})
            extras = []
        view_id = str(view.get("view_id") or "")
        view_name = str(view.get("view_name") or short_name(str(person.get("name") or ""), set(person.get("aliases") or set())))
        hidden_fields = list(
            dict.fromkeys([*((view.get("_property") or {}).get("hidden_fields") or []), employee_field["field_id"]])
        )
        body = {
            "view_name": view_name,
            "property": {
                "filter_info": {
                    "conditions": [
                        {
                            "field_id": employee_field["field_id"],
                            "operator": "is",
                            "value": json.dumps([user_id], ensure_ascii=False),
                        }
                    ],
                    "conjunction": "and",
                },
                "hidden_fields": hidden_fields,
            },
        }
        response = fs.api(
            "PATCH",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views/{view_id}",
            body=body,
        )
        if response.get("code") != 0:
            raise RuntimeError(f"Failed to configure personal workbench view {view_name}: {response}")
        personal_view_report["updated"].append({"user_id": user_id, "view_name": view_name, "response": response})
        for extra in extras:
            response = fs.api(
                "DELETE",
                f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views/{extra['view_id']}",
            )
            personal_view_report["deleted"].append({"view_name": extra.get("view_name"), "response": response})

    for view, user_id in account_filtered_views:
        if user_id in desired_users:
            continue
        response = fs.api(
            "DELETE",
            f"/bitable/v1/apps/{APP_TOKEN}/tables/{WORKBENCH_TABLE}/views/{view['view_id']}",
        )
        personal_view_report["deleted"].append({"view_name": view.get("view_name"), "response": response})

    report = {
        "desired": len(desired),
        "created": len(creates),
        "updated": len(updates),
        "unchanged": len(unchanged),
        "hidden": sum(1 for result in delete_results if result.get("code") == 0),
        "rows": sorted(desired),
        "create_results": create_results,
        "update_results": update_results,
        "delete_results": delete_results,
        "ambiguous_names": sorted(ambiguous_names),
        "personal_views": personal_view_report,
    }
    write_json(out_dir / "sync_personal_workbench_rows_result.json", report)
    return report


def ensure_management_summary_table(fs: Feishu) -> tuple[str, str]:
    tables_response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables", {"page_size": 100})
    tables = (tables_response.get("data") or {}).get("items") or []
    table = next((item for item in tables if item.get("name") == MANAGEMENT_SUMMARY_TABLE_NAME), None)
    if table:
        table_id = table["table_id"]
    else:
        fields = [
            {"field_name": "运营人员", "type": 1},
            {"field_name": "运营账号", "type": 11, "property": {"multiple": True}},
            {"field_name": "负责主播数", "type": 2},
            {"field_name": "待建联主播数", "type": 2},
            {"field_name": "待首播主播数", "type": 2},
            {"field_name": "正常直播主播数", "type": 2},
            {"field_name": "需补资料主播数", "type": 2},
            {"field_name": "未完成任务数", "type": 2},
            {"field_name": "超时任务数", "type": 2},
            {"field_name": "下一项活动时间", "type": 5, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
            {"field_name": "数据更新时间", "type": 5, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
        ]
        response = fs.api(
            "POST",
            f"/bitable/v1/apps/{APP_TOKEN}/tables",
            body={"table": {"name": MANAGEMENT_SUMMARY_TABLE_NAME, "default_view_name": "老板一眼看全公司", "fields": fields}},
        )
        if response.get("code") != 0:
            raise RuntimeError(f"Failed to create management summary table: {response}")
        table_data = (response.get("data") or {}).get("table") or response.get("data") or {}
        table_id = table_data.get("table_id")
        if not table_id:
            raise RuntimeError(f"Management summary table id missing: {response}")

    views_response = fs.api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views", {"page_size": 100})
    views = (views_response.get("data") or {}).get("items") or []
    view_id = views[0].get("view_id") if views else ""
    return table_id, view_id


def sync_management_summary(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    table_id, view_id = ensure_management_summary_table(fs)
    personnel = fs.list_records(TABLES["personnel"], page_size=500)
    user_names = {
        user_id: text_value((record.get("fields") or {}).get("姓名"))
        for record in personnel
        for user_id in user_ids((record.get("fields") or {}).get("飞书用户"))
    }
    groups: dict[str, dict[str, Any]] = {}

    def group_for(user_id: str, fallback_name: str) -> dict[str, Any]:
        key = user_id or fallback_name or "待分配"
        name = user_names.get(user_id) or fallback_name or "待分配"
        if key not in groups:
            groups[key] = {
                "运营人员": name,
                "运营账号": [{"id": user_id}] if user_id else None,
                "负责主播数": 0,
                "待建联主播数": 0,
                "待首播主播数": 0,
                "正常直播主播数": 0,
                "需补资料主播数": 0,
                "未完成任务数": 0,
                "超时任务数": 0,
                "下一项活动时间": None,
            }
        return groups[key]

    for record in fs.list_records(TABLES["anchor"], page_size=500):
        fields = record.get("fields") or {}
        if is_demo_batch(fields.get("自动化批次")):
            continue
        ids = user_ids(fields.get("运营经济人"))
        group = group_for(ids[0] if ids else "", text_value(fields.get("运营经济人")))
        group["负责主播数"] += 1
        stage = text_value(fields.get("主阶段"))
        if stage == "待建联":
            group["待建联主播数"] += 1
        if text_value(fields.get("首播状态")) in {"未安排", "待首播"}:
            group["待首播主播数"] += 1
        if text_value(fields.get("主播状态")) == "正常直播":
            group["正常直播主播数"] += 1
        if text_value(fields.get("系统验收状态")) in {"需补资料", "不通过"}:
            group["需补资料主播数"] += 1

    now = int(datetime.now().timestamp() * 1000)
    for record in fs.list_records(TABLES["task"], page_size=500):
        fields = record.get("fields") or {}
        if is_demo_batch(fields.get("自动化批次")):
            continue
        ids = user_ids(fields.get("运营经济人"))
        group = group_for(ids[0] if ids else "", text_value(fields.get("负责人")))
        status = text_value(fields.get("工作状态"))
        if status not in {"已完成", "已取消"}:
            group["未完成任务数"] += 1
        if status == "已超时":
            group["超时任务数"] += 1
        start_ms = fields.get("开始时间") or fields.get("日期")
        if status not in {"已完成", "已取消"} and isinstance(start_ms, (int, float)) and start_ms >= now:
            current = group.get("下一项活动时间")
            if current is None or start_ms < current:
                group["下一项活动时间"] = start_ms

    updated_at = now
    desired = []
    for fields in groups.values():
        fields["数据更新时间"] = updated_at
        desired.append(fields)
    desired.sort(key=lambda item: (-item["负责主播数"], item["运营人员"]))

    existing = fs.list_records(table_id, page_size=500)
    existing_by_name = {text_value((record.get("fields") or {}).get("运营人员")): record for record in existing}
    creates = [{"fields": fields} for fields in desired if fields["运营人员"] not in existing_by_name]
    updates = [
        {"record_id": existing_by_name[fields["运营人员"]]["record_id"], "fields": fields}
        for fields in desired
        if fields["运营人员"] in existing_by_name
    ]
    create_results = fs.batch_create(table_id, creates, batch_size=500) if creates else []
    update_results = fs.batch_update(table_id, updates, batch_size=500) if updates else []
    desired_names = {fields["运营人员"] for fields in desired}
    stale_records = [record for name, record in existing_by_name.items() if name not in desired_names]
    delete_results = [
        fs.api("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/{record['record_id']}")
        for record in stale_records
    ]

    link = f"https://hxyyb89w4s2.feishu.cn/base/{APP_TOKEN}?table={table_id}" + (f"&view={view_id}" if view_id else "")
    workbench_updates = []
    for record in fs.list_records(WORKBENCH_TABLE, page_size=500):
        fields = record.get("fields") or {}
        if text_value(fields.get("我要做什么")) == "老板：查看公司经营进度":
            workbench_updates.append(
                {
                    "record_id": record["record_id"],
                    "fields": {"点这里办理": {"link": link, "text": "打开老板经营看板"}},
                }
            )
    if workbench_updates:
        fs.batch_update(WORKBENCH_TABLE, workbench_updates, batch_size=500)
    payload = {
        "table_id": table_id,
        "view_id": view_id,
        "rows": len(desired),
        "created": len(creates),
        "updated": len(updates),
        "deleted": sum(1 for result in delete_results if result.get("code") == 0),
        "workbench_updated": len(workbench_updates),
        "link": link,
        "create_results": create_results,
        "update_results": update_results,
        "delete_results": delete_results,
    }
    write_json(out_dir / "sync_management_summary_result.json", payload)
    return payload


def calendar_attendee_ids(fs: Feishu, *owners: Any) -> list[str]:
    names = owner_names(*owners)
    if not names:
        return []
    matched: list[str] = []
    for record in fs.list_records(TABLES["personnel"], page_size=500):
        fields = record.get("fields") or {}
        if not fields.get("是否参与日历同步"):
            continue
        if text_value(fields.get("姓名")).strip() not in names:
            continue
        matched.extend(user_ids(fields.get("飞书用户")))
    return sorted(set(matched))


def calendar_manager_ids(fs: Feishu) -> list[str]:
    matched: list[str] = []
    manager_roles = {"管理者", "老板", "负责人"}
    for record in fs.list_records(TABLES["personnel"], page_size=500):
        fields = record.get("fields") or {}
        if not fields.get("是否参与日历同步"):
            continue
        roles = set(owner_names(fields.get("角色")))
        if text_value(fields.get("部门")) != "管理" and not roles.intersection(manager_roles):
            continue
        matched.extend(user_ids(fields.get("飞书用户")))
    return sorted(set(matched))


def add_calendar_attendees(fs: Feishu, calendar_id: str, event_id: str, attendee_ids: list[str]) -> dict[str, Any]:
    if not attendee_ids:
        return {"code": 0, "skipped": True, "reason": "No mapped responsible employee."}
    return fs.api(
        "POST",
        f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
        {"user_id_type": "open_id"},
        body={
            "attendees": [{"type": "user", "user_id": user_id} for user_id in attendee_ids],
            "need_notification": False,
        },
    )


def ensure_calendar(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    configured_calendar_id = os.environ.get("FEISHU_CALENDAR_ID", "").strip()
    if configured_calendar_id:
        return {"calendar_id": configured_calendar_id, "source": "environment"}
    calendar_file = out_dir / "calendar.json"
    if calendar_file.exists():
        data = json.loads(calendar_file.read_text(encoding="utf-8"))
        if data.get("calendar_id"):
            return data
    data = fs.api(
        "POST",
        "/calendar/v4/calendars",
        body={
            "summary": "米游文化运营任务日历",
            "description": "由飞书多维表格系统同步运营、培训、首播复盘等任务。",
        },
    )
    calendar = (data.get("data") or {}).get("calendar") or data.get("data") or {}
    calendar_id = calendar.get("calendar_id") or calendar.get("calendar_id")
    payload = {"code": data.get("code"), "msg": data.get("msg"), "calendar_id": calendar_id, "response": data}
    write_json(calendar_file, payload)
    return payload


def sync_calendar(fs: Feishu, batch: str, out_dir: Path) -> dict[str, Any]:
    calendar = ensure_calendar(fs, out_dir)
    calendar_id = calendar.get("calendar_id")
    if not calendar_id:
        payload = {"batch": batch, "synced": 0, "errors": [{"stage": "calendar", "response": calendar}]}
        write_json(out_dir / f"sync_calendar_{batch}_result.json", payload)
        return payload

    tasks = [r for r in fs.list_records(TABLES["task"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    updates = []
    visual_updates = []
    events = []
    errors = []
    attendee_synced = 0
    attendee_skipped = 0
    manager_ids = calendar_manager_ids(fs)
    for task in tasks:
        fields = task.get("fields") or {}
        if text_value(fields.get("飞书日历事件ID")):
            continue
        start_ms = fields.get("开始时间") or fields.get("日期")
        end_ms = fields.get("结束时间") or start_ms
        if not isinstance(start_ms, (int, float)) or not isinstance(end_ms, (int, float)):
            errors.append({"record_id": task.get("record_id"), "error": "任务缺少开始/结束时间"})
            continue
        body = {
            "summary": text_value(fields.get("任务名称")) or "运营任务",
            "description": text_value(fields.get("工作事项")),
            "start_time": {"timestamp": str(int(start_ms / 1000)), "timezone": "Asia/Shanghai"},
            "end_time": {"timestamp": str(int(end_ms / 1000)), "timezone": "Asia/Shanghai"},
        }
        data = fs.api("POST", f"/calendar/v4/calendars/{calendar_id}/events", body=body)
        event = (data.get("data") or {}).get("event") or data.get("data") or {}
        event_id = event.get("event_id")
        attendee_result: dict[str, Any] = {"skipped": True}
        attendee_ids = sorted(set(user_ids(fields.get("运营经济人")) + calendar_attendee_ids(fs, fields.get("负责人")) + manager_ids))
        if data.get("code") == 0 and event_id:
            attendee_result = add_calendar_attendees(fs, calendar_id, event_id, attendee_ids)
            if attendee_result.get("code") == 0 and not attendee_result.get("skipped"):
                attendee_synced += len(attendee_ids)
            elif attendee_result.get("skipped"):
                attendee_skipped += 1
            else:
                errors.append({"type": "运营任务参与人", "record_id": task.get("record_id"), "response": attendee_result})
        events.append({"type": "运营任务", "record_id": task.get("record_id"), "code": data.get("code"), "msg": data.get("msg"), "event_id": event_id, "attendee_count": len(attendee_ids), "attendee_result": attendee_result})
        if data.get("code") == 0 and event_id:
            updates.append(
                {
                    "record_id": task["record_id"],
                    "fields": {
                        "是否同步飞书日历": True,
                        "飞书日历事件ID": event_id,
                    },
                }
            )
        else:
            errors.append({"record_id": task.get("record_id"), "response": data})
        time.sleep(0.2)

    visuals = [r for r in fs.list_records(TABLES["visual"]) if (r.get("fields") or {}).get("自动化批次") == batch]
    for visual in visuals:
        fields = visual.get("fields") or {}
        if text_value(fields.get("飞书日历事件ID")):
            continue
        start_ms = fields.get("开始时间") or fields.get("预约时间")
        if not isinstance(start_ms, (int, float)):
            errors.append({"type": "视觉任务", "record_id": visual.get("record_id"), "error": "视觉任务缺少预约时间"})
            continue
        end_ms = fields.get("结束时间") if isinstance(fields.get("结束时间"), (int, float)) else start_ms + 3600000
        body = {
            "summary": f"视觉调试：{text_value(fields.get('需求标题')) or '待命名任务'}",
            "description": text_value(fields.get("需求描述")),
            "start_time": {"timestamp": str(int(start_ms / 1000)), "timezone": "Asia/Shanghai"},
            "end_time": {"timestamp": str(int(end_ms / 1000)), "timezone": "Asia/Shanghai"},
        }
        data = fs.api("POST", f"/calendar/v4/calendars/{calendar_id}/events", body=body)
        event = (data.get("data") or {}).get("event") or data.get("data") or {}
        event_id = event.get("event_id")
        attendee_result = {"skipped": True}
        attendee_ids = sorted(set(calendar_attendee_ids(fs, fields.get("视觉负责人"), fields.get("视觉处理人"), fields.get("提交运营")) + manager_ids))
        if data.get("code") == 0 and event_id:
            attendee_result = add_calendar_attendees(fs, calendar_id, event_id, attendee_ids)
            if attendee_result.get("code") == 0 and not attendee_result.get("skipped"):
                attendee_synced += len(attendee_ids)
            elif attendee_result.get("skipped"):
                attendee_skipped += 1
            else:
                errors.append({"type": "视觉任务参与人", "record_id": visual.get("record_id"), "response": attendee_result})
        events.append({"type": "视觉任务", "record_id": visual.get("record_id"), "code": data.get("code"), "msg": data.get("msg"), "event_id": event_id, "attendee_count": len(attendee_ids), "attendee_result": attendee_result})
        if data.get("code") == 0 and event_id:
            visual_updates.append(
                {
                    "record_id": visual["record_id"],
                    "fields": {
                        "开始时间": start_ms,
                        "结束时间": end_ms,
                        "是否同步飞书日历": True,
                        "飞书日历事件ID": event_id,
                    },
                }
            )
        else:
            errors.append({"type": "视觉任务", "record_id": visual.get("record_id"), "response": data})
        time.sleep(0.2)
    update_results = fs.batch_update(TABLES["task"], updates, batch_size=500) if updates else []
    visual_update_results = fs.batch_update(TABLES["visual"], visual_updates, batch_size=500) if visual_updates else []
    payload = {
        "batch": batch,
        "calendar_id": calendar_id,
        "tasks": len(tasks),
        "visual_tasks": len(visuals),
        "synced": len(updates),
        "visual_synced": len(visual_updates),
        "attendee_synced": attendee_synced,
        "attendee_skipped": attendee_skipped,
        "events": events,
        "errors": errors,
        "update_results": update_results,
        "visual_update_results": visual_update_results,
    }
    write_json(out_dir / f"sync_calendar_{batch}_result.json", payload)
    return payload


def calendar_fingerprint(summary: str, description: str, start_ms: int, end_ms: int, attendee_ids: list[str]) -> str:
    raw = json.dumps(
        {
            "summary": summary,
            "description": description,
            "start": start_ms,
            "end": end_ms,
            "attendees": sorted(attendee_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def record_after_cutover(record: dict[str, Any]) -> bool:
    raw_cutover = os.environ.get("AUTOMATION_CUTOVER_MS", "").strip()
    if not raw_cutover.isdigit():
        return True
    created = record.get("created_time") or record.get("created_at")
    if isinstance(created, str) and created.isdigit():
        created = int(created)
    if not isinstance(created, (int, float)):
        return False
    created_ms = int(created * 1000) if created < 100000000000 else int(created)
    return created_ms >= int(raw_cutover)


def sync_operational_calendars(fs: Feishu, out_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    calendar = ensure_calendar(fs, out_dir)
    calendar_id = calendar.get("calendar_id")
    if not calendar_id:
        payload = {"synced": 0, "updated": 0, "errors": [{"stage": "calendar", "response": calendar}]}
        write_json(out_dir / "sync_operational_calendars_result.json", payload)
        return payload

    manager_ids = calendar_manager_ids(fs)
    specs = [
        {
            "table": "interview",
            "kind": "邀约",
            "start": ("邀约时间",),
            "end": (),
            "duration": 30,
            "event": "邀约日历事件ID",
            "synced": "邀约是否同步日历",
            "fingerprint": "邀约日历同步指纹",
            "owners": ("招募人账号（系统）",),
        },
        {
            "table": "interview",
            "kind": "面试",
            "start": ("面试开始时间",),
            "end": ("面试结束时间",),
            "duration": 60,
            "event": "面试日历事件ID",
            "synced": "面试是否同步日历",
            "fingerprint": "面试日历同步指纹",
            "owners": ("招募人账号（系统）", "面试官账号（系统）", "对接运营账号（系统）"),
        },
        {
            "table": "visual",
            "kind": "视觉验收",
            "start": ("开始时间", "预约时间"),
            "end": ("结束时间",),
            "duration": 60,
            "event": "飞书日历事件ID",
            "synced": "是否同步飞书日历",
            "fingerprint": "日历同步指纹",
            "owners": ("提交运营", "视觉负责人", "视觉处理人"),
        },
        {
            "table": "training",
            "kind": "培训与录屏验收",
            "start": ("开始时间",),
            "end": ("结束时间",),
            "duration": 60,
            "event": "飞书日历事件ID",
            "synced": "是否同步飞书日历",
            "fingerprint": "日历同步指纹",
            "owners": ("培训运营",),
        },
        {
            "table": "first_live",
            "kind": "首播检查",
            "start": ("开始时间",),
            "end": ("结束时间", "首播结束时间"),
            "duration": 60,
            "event": "飞书日历事件ID",
            "synced": "是否同步飞书日历",
            "fingerprint": "日历同步指纹",
            "owners": ("跟播运营",),
        },
        {
            "table": "review",
            "kind": "跟播复盘",
            "start": ("开始时间", "日期"),
            "end": ("结束时间",),
            "duration": 60,
            "event": "飞书日历事件ID",
            "synced": "是否同步飞书日历",
            "fingerprint": "日历同步指纹",
            "owners": ("跟播人员",),
        },
    ]
    records_by_table = {
        table_key: fs.list_records(TABLES[table_key], page_size=500)
        for table_key in sorted({spec["table"] for spec in specs})
    }
    updates_by_table: dict[str, list[dict[str, Any]]] = {table_key: [] for table_key in records_by_table}
    created_events = 0
    updated_events = 0
    unchanged = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    event_log: list[dict[str, Any]] = []

    for spec in specs:
        table_key = str(spec["table"])
        for record in records_by_table[table_key]:
            fields = record.get("fields") or {}
            batch = text_value(fields.get("自动化批次"))
            if is_demo_batch(batch) or batch.startswith("MIGRATION-") or fields.get("数据迁移批次"):
                skipped += 1
                continue
            if table_key == "interview" and text_value(fields.get("WPS记录ID")):
                skipped += 1
                continue
            if not record_after_cutover(record):
                skipped += 1
                continue
            start_ms = next(
                (fields.get(name) for name in spec["start"] if isinstance(fields.get(name), (int, float))),
                None,
            )
            if not isinstance(start_ms, (int, float)):
                continue
            end_ms = next(
                (fields.get(name) for name in spec["end"] if isinstance(fields.get(name), (int, float))),
                None,
            )
            if not isinstance(end_ms, (int, float)) or end_ms <= start_ms:
                end_ms = start_ms + int(spec["duration"]) * 60000
            primary = (
                text_value(fields.get("候选人姓名"))
                or text_value(fields.get("需求标题"))
                or text_value(fields.get("关联主播编号"))
                or text_value(fields.get("主播名字"))
                or "待命名"
            )
            summary = f"{spec['kind']}：{primary}"
            description = (
                text_value(fields.get("邀约备注"))
                or text_value(fields.get("需求描述"))
                or text_value(fields.get("不通过原因"))
                or text_value(fields.get("当前问题"))
            )
            owner_values = [fields.get(name) for name in spec["owners"]]
            attendee_ids = sorted(
                set(
                    manager_ids
                    + [user_id for value in owner_values for user_id in user_ids(value)]
                    + calendar_attendee_ids(fs, *owner_values)
                )
            )
            fingerprint = calendar_fingerprint(summary, description, int(start_ms), int(end_ms), attendee_ids)
            if text_value(fields.get(str(spec["fingerprint"]))) == fingerprint:
                unchanged += 1
                continue
            body = {
                "summary": summary,
                "description": description,
                "start_time": {"timestamp": str(int(start_ms / 1000)), "timezone": "Asia/Shanghai"},
                "end_time": {"timestamp": str(int(end_ms / 1000)), "timezone": "Asia/Shanghai"},
            }
            event_id = text_value(fields.get(str(spec["event"])))
            action = "update" if event_id else "create"
            if dry_run:
                event_log.append({"table": table_key, "record_id": record["record_id"], "action": action, "summary": summary})
                continue
            if event_id:
                response = fs.api(
                    "PATCH",
                    f"/calendar/v4/calendars/{calendar_id}/events/{event_id}",
                    body=body,
                )
            else:
                response = fs.api("POST", f"/calendar/v4/calendars/{calendar_id}/events", body=body)
                event = (response.get("data") or {}).get("event") or response.get("data") or {}
                event_id = str(event.get("event_id") or "")
            if response.get("code") != 0 or not event_id:
                errors.append({"table": table_key, "record_id": record["record_id"], "action": action, "response": response})
                continue
            attendee_response = add_calendar_attendees(fs, calendar_id, event_id, attendee_ids)
            if attendee_response.get("code") != 0:
                errors.append({"table": table_key, "record_id": record["record_id"], "stage": "attendees", "response": attendee_response})
            updates_by_table[table_key].append(
                {
                    "record_id": record["record_id"],
                    "fields": {
                        str(spec["event"]): event_id,
                        str(spec["synced"]): True,
                        str(spec["fingerprint"]): fingerprint,
                    },
                }
            )
            if action == "create":
                created_events += 1
            else:
                updated_events += 1
            event_log.append({"table": table_key, "record_id": record["record_id"], "action": action, "event_id": event_id, "summary": summary})
            time.sleep(0.1)

    update_results = {
        table_key: fs.batch_update(TABLES[table_key], rows, batch_size=500) if rows and not dry_run else []
        for table_key, rows in updates_by_table.items()
    }
    payload = {
        "dry_run": dry_run,
        "calendar_id": calendar_id,
        "created": created_events,
        "updated": updated_events,
        "unchanged": unchanged,
        "skipped": skipped,
        "planned": len(event_log) if dry_run else 0,
        "events": event_log,
        "errors": errors,
        "update_results": update_results,
    }
    write_json(out_dir / "sync_operational_calendars_result.json", payload)
    return payload


def sync_manager_calendar_attendees(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    calendar = ensure_calendar(fs, out_dir)
    calendar_id = calendar.get("calendar_id")
    manager_ids = calendar_manager_ids(fs)
    if not calendar_id or not manager_ids:
        payload = {
            "calendar_ready": bool(calendar_id),
            "manager_count": len(manager_ids),
            "synced": 0,
            "errors": ["日历或管理人员账号尚未配置完整"],
        }
        write_json(out_dir / "sync_manager_calendar_attendees_result.json", payload)
        return payload

    targets = []
    for table_key, event_field in [("task", "飞书日历事件ID"), ("visual", "飞书日历事件ID")]:
        for record in fs.list_records(TABLES[table_key], page_size=500):
            fields = record.get("fields") or {}
            if is_demo_batch(fields.get("自动化批次")):
                continue
            event_id = text_value(fields.get(event_field)).strip()
            if event_id:
                targets.append((table_key, record.get("record_id"), event_id))

    synced = 0
    errors = []
    for table_key, record_id, event_id in targets:
        response = add_calendar_attendees(fs, calendar_id, event_id, manager_ids)
        if response.get("code") == 0:
            synced += 1
        else:
            errors.append({"table": table_key, "record_id": record_id, "code": response.get("code"), "msg": response.get("msg")})
        time.sleep(0.2)
    payload = {
        "manager_count": len(manager_ids),
        "targets": len(targets),
        "synced": synced,
        "errors": errors,
    }
    write_json(out_dir / "sync_manager_calendar_attendees_result.json", payload)
    return payload


def cleanup_demo_calendar_events(fs: Feishu, out_dir: Path) -> dict[str, Any]:
    calendar = ensure_calendar(fs, out_dir)
    calendar_id = calendar.get("calendar_id")
    if not calendar_id:
        raise RuntimeError("Calendar is not configured.")
    deleted = 0
    cleared = 0
    errors = []
    for table_key in ("task", "visual"):
        updates = []
        for record in fs.list_records(TABLES[table_key], page_size=500):
            fields = record.get("fields") or {}
            if not is_demo_batch(fields.get("自动化批次")):
                continue
            event_id = text_value(fields.get("飞书日历事件ID")).strip()
            if not event_id:
                continue
            response = fs.api(
                "DELETE",
                f"/calendar/v4/calendars/{calendar_id}/events/{event_id}",
                {"need_notification": "false"},
            )
            if response.get("code") == 0:
                deleted += 1
                updates.append(
                    {
                        "record_id": record["record_id"],
                        "fields": {"是否同步飞书日历": False, "飞书日历事件ID": None},
                    }
                )
            else:
                errors.append({"table": table_key, "record_id": record.get("record_id"), "code": response.get("code"), "msg": response.get("msg")})
            time.sleep(0.2)
        if updates:
            fs.batch_update(TABLES[table_key], updates, batch_size=500)
            cleared += len(updates)
    payload = {"deleted": deleted, "cleared": cleared, "errors": errors}
    write_json(out_dir / "cleanup_demo_calendar_events_result.json", payload)
    return payload


def write_acceptance_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        f"# 系统功能验收报告 - {report['batch']}",
        "",
        f"生成主播数：{report['anchors']}",
        f"生成流程节点数：{report['nodes']}",
        f"生成运营任务数：{report['tasks']}",
        f"生成视觉调试记录数：{report['visual_records']}",
        f"生成培训录屏记录数：{report['training_records']}",
        f"生成首播筹备记录数：{report['first_live_records']}",
        f"回写面试记录数：{report['interviews_updated']}",
        f"每位主播应有节点数：{report['expected_nodes_per_anchor']}",
        f"每位主播应有任务数：{report['expected_tasks_per_anchor']}",
        f"超时节点数：{report['overdue_nodes']}",
        f"验收结论：{'通过' if report['passed'] else '不通过'}",
        "",
        "## 已覆盖的核心需求",
        "",
        "- 面试通过后自动生成主播档案。",
        "- 生成主播后自动创建全套孵化流程节点。",
        "- 自动创建运营任务。",
        "- 自动创建视觉调试、培训录屏、首播筹备记录。",
        "- 视觉6项未核验时默认禁止进入培训。",
        "- 3分钟录屏未通过时默认禁止进入首播。",
        "- 面试记录回写是否生成主播档案和关联主播。",
        "- 主播、节点、任务均写入自动化批次，便于验收和回滚判断。",
        "- 节点按计划时间标记是否超时，用于异常看板。",
        "",
        "## 未通过项",
        "",
    ]
    if report["failures"]:
        lines.extend(f"- {item}" for item in report["failures"])
    else:
        lines.append("- 无")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Miyou Feishu system automation and acceptance.")
    parser.add_argument("--env", type=Path, default=Path("feishu") / ".env.local")
    parser.add_argument("--out-dir", type=Path, default=Path("source") / "feishu" / "automation")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup-controls")
    repair = sub.add_parser("repair-relationships")
    repair.add_argument("--batch", required=True)
    chain = sub.add_parser("build-chain")
    chain.add_argument("--batch", required=True)
    chain.add_argument("--limit", type=int, default=3)
    chain.add_argument("--not-before-ms", type=int, default=0)
    validate = sub.add_parser("validate")
    validate.add_argument("--batch", required=True)
    sub.add_parser("setup-calendar")
    sub.add_parser("sync-personnel")
    sub.add_parser("sync-person-assignments")
    sub.add_parser("ensure-personal-views")
    sub.add_parser("sync-personal-workbench")
    sub.add_parser("sync-management-summary")
    calendar = sub.add_parser("sync-calendar")
    calendar.add_argument("--batch", required=True)
    operational_calendar = sub.add_parser("sync-operational-calendars")
    operational_calendar.add_argument("--dry-run", action="store_true")
    sub.add_parser("sync-manager-calendar-attendees")
    sub.add_parser("cleanup-demo-calendar-events")
    args = parser.parse_args()

    fs = Feishu(get_tenant_token(args.env))
    if args.command == "setup-controls":
        result = ensure_fields(fs, args.out_dir)
    elif args.command == "repair-relationships":
        result = repair_relationship_fields(fs, args.batch, args.out_dir)
    elif args.command == "build-chain":
        result = build_chain(fs, args.batch, args.limit, args.out_dir, args.not_before_ms)
    elif args.command == "validate":
        result = validate_batch(fs, args.batch, args.out_dir)
    elif args.command == "setup-calendar":
        result = ensure_calendar(fs, args.out_dir)
    elif args.command == "sync-personnel":
        result = sync_personnel_directory(fs, args.out_dir)
    elif args.command == "sync-person-assignments":
        result = sync_person_assignment_fields(fs, args.out_dir)
    elif args.command == "ensure-personal-views":
        result = ensure_personal_views(fs, args.out_dir)
    elif args.command == "sync-personal-workbench":
        result = sync_personal_workbench_rows(fs, args.out_dir)
    elif args.command == "sync-management-summary":
        result = sync_management_summary(fs, args.out_dir)
    elif args.command == "sync-calendar":
        result = sync_calendar(fs, args.batch, args.out_dir)
    elif args.command == "sync-operational-calendars":
        result = sync_operational_calendars(fs, args.out_dir, dry_run=args.dry_run)
    elif args.command == "sync-manager-calendar-attendees":
        result = sync_manager_calendar_attendees(fs, args.out_dir)
    elif args.command == "cleanup-demo-calendar-events":
        result = cleanup_demo_calendar_events(fs, args.out_dir)
    else:
        raise RuntimeError(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
