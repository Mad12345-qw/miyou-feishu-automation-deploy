from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import TABLES, Feishu, get_tenant_token, load_env, text_value, write_json


VISUAL_CHECKS = ["座椅已确认", "素颜已确认", "背景场地已确认", "坐姿镜头已确认", "服装已确认", "发型已确认"]
TRAINING_CHECKS = [
    "账号搭建检查",
    "基础话术验收",
    "姿态状态验收",
    "镜头感验收",
    "突发处理验收",
    "转化能力验收",
    "消费力感知验收",
]
FINAL_STAGES = {"首播已复盘", "日常运营", "流失", "淘汰"}


def now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def linked_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            record_ids = item.get("record_ids")
            if isinstance(record_ids, list):
                result.extend(str(record_id) for record_id in record_ids if record_id)
            else:
                record_id = item.get("record_id") or item.get("id")
                if record_id:
                    result.append(str(record_id))
        elif item:
            result.append(str(item))
    return result


def has_attachment(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def batch_records(fs: Feishu, table_key: str, batch: str) -> list[dict[str, Any]]:
    return [
        row
        for row in fs.list_records(TABLES[table_key], page_size=500)
        if (row.get("fields") or {}).get("自动化批次") == batch
    ]


def reconcile(fs: Feishu, batch: str, out_dir: Path, dry_run: bool) -> dict[str, Any]:
    now = now_ms()
    anchors = batch_records(fs, "anchor", batch)
    anchors_by_id = {str(row.get("record_id") or row.get("id")): row for row in anchors}
    visual_updates = []
    training_updates = []
    first_live_updates = []
    node_updates = []
    anchor_updates = []
    allowed_training_anchors: set[str] = set()

    for visual in batch_records(fs, "visual", batch):
        fields = visual.get("fields") or {}
        anchor = next((anchors_by_id.get(item) for item in linked_ids(fields.get("关联主播")) if anchors_by_id.get(item)), None)
        persona = text_value(((anchor or {}).get("fields") or {}).get("风格定位"))
        checks_passed = all(fields.get(name) is True for name in VISUAL_CHECKS)
        persona_passed = bool(persona) and persona not in {"待确认", "未完成", "未定位"}
        visual_record_complete = (
            bool(text_value(fields.get("构图结论")))
            and bool(text_value(fields.get("灯光参数")))
            and bool(text_value(fields.get("美颜参数")))
            and has_attachment(fields.get("试镜视频"))
        )
        allowed = checks_passed and persona_passed and visual_record_complete
        if not persona_passed:
            reason = "风格定位未完成"
        elif not checks_passed:
            reason = "视觉前置6项尚未全部确认"
        elif not visual_record_complete:
            reason = "构图、灯光、美颜参数或试镜视频未完整归档"
        else:
            reason = ""
        visual_updates.append(
            {
                "record_id": visual["record_id"],
                "fields": {
                    "6项核验是否全部通过": checks_passed,
                    "准入结果": "可进入培训" if allowed else "禁止进入培训",
                    "准入异常原因": reason,
                },
            }
        )

    for training in batch_records(fs, "training", batch):
        fields = training.get("fields") or {}
        training_passed = all(text_value(fields.get(name)) == "通过" for name in TRAINING_CHECKS)
        recording_passed = text_value(fields.get("录屏审核状态")) == "通过"
        duration = fields.get("录屏时长（秒）")
        duration_passed = isinstance(duration, (int, float)) and duration >= 180
        recording_archived = has_attachment(fields.get("3分钟录屏"))
        allowed = training_passed and recording_passed and duration_passed and recording_archived
        if not training_passed:
            reason = "培训验收未全部通过"
        elif not recording_archived:
            reason = "3分钟录屏未上传归档"
        elif not duration_passed:
            reason = "录屏时长不足180秒"
        elif not recording_passed:
            reason = "3分钟录屏未通过"
        else:
            reason = ""
        if allowed:
            allowed_training_anchors.update(linked_ids(fields.get("关联主播")))
        training_updates.append(
            {
                "record_id": training["record_id"],
                "fields": {
                    "是否允许进入首播": allowed,
                    "录屏准入结果": "可进入首播" if allowed else "禁止进入首播",
                    "不通过原因": reason,
                },
            }
        )

    for first_live in batch_records(fs, "first_live", batch):
        fields = first_live.get("fields") or {}
        anchor_ids = linked_ids(fields.get("关联主播"))
        training_allowed = any(anchor_id in allowed_training_anchors for anchor_id in anchor_ids)
        precheck_passed = all(text_value(fields.get(name)) == "正常" for name in ["设备检查", "网络检查", "美颜检查", "灯光检查", "服装检查"])
        if not training_allowed:
            first_live_reason = "培训或3分钟录屏尚未满足首播准入"
        elif not precheck_passed:
            first_live_reason = "首播前设备、网络、美颜、灯光或服装检查未全部正常"
        else:
            first_live_reason = ""
        ended_at = fields.get("首播结束时间")
        reviewed_at = fields.get("复盘完成时间")
        completed_in_time = (
            isinstance(ended_at, (int, float))
            and isinstance(reviewed_at, (int, float))
            and 0 <= reviewed_at - ended_at <= 3600000
        )
        update_fields = {
            "首播前检查是否通过": precheck_passed,
            "首播准入结果": "可安排首播" if training_allowed and precheck_passed else "禁止首播",
            "首播准入异常原因": first_live_reason,
        }
        if isinstance(ended_at, (int, float)):
            update_fields.update(
                {
                    "是否1小时内复盘": completed_in_time,
                    "复盘异常原因": "" if completed_in_time else "首播结束后1小时内未完成复盘",
                }
            )
        first_live_updates.append({"record_id": first_live["record_id"], "fields": update_fields})

    for node in batch_records(fs, "node", batch):
        fields = node.get("fields") or {}
        planned_at = fields.get("计划完成时间")
        status = text_value(fields.get("节点状态"))
        overdue = isinstance(planned_at, (int, float)) and planned_at < now and status not in {"已完成", "跳过"}
        node_updates.append({"record_id": node["record_id"], "fields": {"是否超时": overdue}})

    update_results: dict[str, Any] = {}
    if not dry_run:
        update_results = {
            "visual": fs.batch_update(TABLES["visual"], visual_updates),
            "training": fs.batch_update(TABLES["training"], training_updates),
            "first_live": fs.batch_update(TABLES["first_live"], first_live_updates),
            "node": fs.batch_update(TABLES["node"], node_updates),
            "anchor": fs.batch_update(TABLES["anchor"], anchor_updates),
        }

    result = {
        "batch": batch,
        "dry_run": dry_run,
        "updates": {
            "visual": len(visual_updates),
            "training": len(training_updates),
            "first_live": len(first_live_updates),
            "node": len(node_updates),
            "anchor": len(anchor_updates),
        },
        "results": update_results,
    }
    write_json(out_dir / f"rule_engine_{batch}_result.json", result)
    return result


def run_positive_qa(fs: Feishu, batch: str, out_dir: Path) -> dict[str, Any]:
    anchors = batch_records(fs, "anchor", batch)
    visuals = batch_records(fs, "visual", batch)
    trainings = batch_records(fs, "training", batch)
    first_lives = batch_records(fs, "first_live", batch)
    if not all((anchors, visuals, trainings, first_lives)):
        raise RuntimeError("QA batch must contain anchor, visual, training and first-live records.")

    now = now_ms()
    fs.batch_update(
        TABLES["anchor"],
        [{"record_id": anchors[0]["record_id"], "fields": {"风格定位": ["甜妹"]}}],
    )
    fs.batch_update(
        TABLES["visual"],
        [{"record_id": visuals[0]["record_id"], "fields": {name: True for name in VISUAL_CHECKS}}],
    )
    fs.batch_update(
        TABLES["training"],
        [{
            "record_id": trainings[0]["record_id"],
            "fields": {**{name: "通过" for name in TRAINING_CHECKS}, "录屏审核状态": "通过"},
        }],
    )
    fs.batch_update(
        TABLES["first_live"],
        [{
            "record_id": first_lives[0]["record_id"],
            "fields": {"首播结束时间": now - 1800000, "复盘完成时间": now},
        }],
    )
    reconcile(fs, batch, out_dir, dry_run=False)

    visual = next(row for row in batch_records(fs, "visual", batch) if row["record_id"] == visuals[0]["record_id"])
    training = next(row for row in batch_records(fs, "training", batch) if row["record_id"] == trainings[0]["record_id"])
    first_live = next(row for row in batch_records(fs, "first_live", batch) if row["record_id"] == first_lives[0]["record_id"])
    visual_fields = visual.get("fields") or {}
    training_fields = training.get("fields") or {}
    first_live_fields = first_live.get("fields") or {}
    checks = {
        "visual_can_enter_training": visual_fields.get("准入结果") == "可进入培训" and visual_fields.get("6项核验是否全部通过") is True,
        "recording_can_enter_first_live": training_fields.get("录屏准入结果") == "可进入首播" and training_fields.get("是否允许进入首播") is True,
        "first_live_review_within_one_hour": first_live_fields.get("是否1小时内复盘") is True,
    }
    result = {"batch": batch, "checks": checks, "passed": all(checks.values())}
    write_json(out_dir / f"positive_rule_qa_{batch}_result.json", result)
    if not result["passed"]:
        raise RuntimeError(f"Positive QA failed: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Miyou timing and admission rules for one automation batch.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--positive-qa", action="store_true")
    args = parser.parse_args()
    fs = Feishu(get_tenant_token(args.env))
    result = run_positive_qa(fs, args.batch, args.out_dir) if args.positive_qa else reconcile(fs, args.batch, args.out_dir, args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
