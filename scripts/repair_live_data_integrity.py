from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from miyou_system_automation import (
    ANCHOR_DISPLAY_FIELD,
    ANCHOR_NAME_FIELD,
    CHAIN_NODE_TEMPLATE,
    TABLES,
    TASK_TEMPLATE,
    Feishu,
    anchor_display_name,
    ensure_recovered_anchor_children,
    find_existing_anchor_for_interview,
    get_tenant_token,
    is_demo_batch,
    linked_record_ids,
    sync_anchor_display_names,
    sync_missing_interview_display_fields,
    text_value,
    write_json,
)


TRANSFER_FIELDS = ("通过转入主播", "面试通过，转入主播")
CHILD_SPECS = {
    "node": ("关联主播", "节点类型"),
    "task": ("对应主播", "任务名称"),
    "visual": ("关联主播", "singleton"),
    "training": ("关联主播", "singleton"),
    "first_live": ("关联主播", "singleton"),
}
SAFE_MERGE_FIELDS = ("主播编号", "照片", "招募经济人", "面试官", "真实姓名", "来源面试记录说明")
EMPTY_VALUES = (None, "", [], {})
TASK_PROGRESS_TRANSFER_FIELDS = (
    "完成情况",
    "工作状态",
    "异常原因",
    "负责人",
    "运营经济人",
    "飞书日历事件ID",
    "是否同步飞书日历",
    "开始时间",
    "结束时间",
    "日期",
)


def has_business_progress(table_key: str, fields: dict[str, Any]) -> bool:
    if table_key == "node":
        return any(
            (
                fields.get("交付物/附件") not in EMPTY_VALUES,
                fields.get("实际完成时间") not in EMPTY_VALUES,
                fields.get("异常原因") not in EMPTY_VALUES,
                text_value(fields.get("节点状态")).strip() not in {"", "未开始"},
                text_value(fields.get("节点验收结果")).strip() not in {"", "待验收"},
            )
        )
    if table_key == "task":
        return any(
            (
                fields.get("完成情况") not in EMPTY_VALUES,
                fields.get("异常原因") not in EMPTY_VALUES,
                fields.get("飞书日历事件ID") not in EMPTY_VALUES,
                text_value(fields.get("工作状态")).strip() not in {"", "未开始"},
            )
        )
    if table_key == "visual":
        progress_fields = ("完成图片", "完成时间", "完成说明", "构图结论", "灯光参数", "美颜参数", "试镜视频")
        check_fields = ("座椅", "素颜", "背景场地", "坐姿镜头", "服装", "发型", "6项核验是否全部通过")
        return (
            any(fields.get(name) not in EMPTY_VALUES for name in progress_fields)
            or any(fields.get(name) is True for name in check_fields)
            or text_value(fields.get("前置核验状态")).strip() not in {"", "未核验"}
            or text_value(fields.get("需求状态")).strip() not in {"", "待接单"}
        )
    if table_key == "training":
        acceptance_fields = ("基础话术验收", "姿态状态验收", "消费力感知验收", "突发处理验收", "账号搭建检查", "转化能力验收", "镜头感验收")
        return (
            fields.get("3分钟录屏") not in EMPTY_VALUES
            or fields.get("不通过原因") not in EMPTY_VALUES
            or fields.get("录屏时长（秒）") not in EMPTY_VALUES
            or fields.get("是否允许进入首播") is True
            or text_value(fields.get("培训状态")).strip() not in {"", "未开始"}
            or text_value(fields.get("录屏审核状态")).strip() not in {"", "待提交"}
            or any(text_value(fields.get(name)).strip() not in {"", "未验收"} for name in acceptance_fields)
        )
    progress_fields = ("优化方案", "后续直播计划", "复盘完成时间", "复盘附件", "首播亮点", "首播问题")
    check_fields = ("设备检查", "网络检查", "美颜检查", "灯光检查", "服装检查")
    return (
        any(fields.get(name) not in EMPTY_VALUES for name in progress_fields)
        or fields.get("是否1小时内复盘") is True
        or fields.get("首播前检查是否通过") is not None
        or text_value(fields.get("首播状态")).strip() not in {"", "待首播"}
        or any(text_value(fields.get(name)).strip() not in {"", "未检查"} for name in check_fields)
    )


def assignment_quality(fields: dict[str, Any]) -> int:
    score = 0
    for value in fields.values():
        if isinstance(value, list) and any(isinstance(item, dict) and (item.get("id") or item.get("user_id")) for item in value):
            score += 2
    for field_name in ("责任人", "负责人"):
        value = text_value(fields.get(field_name)).strip()
        if value and value != "待分配":
            score += 1
    display = text_value(fields.get(ANCHOR_DISPLAY_FIELD)).strip()
    if display and " · " not in display:
        score += 1
    return score


def template_quality(table_key: str, fields: dict[str, Any]) -> int:
    if table_key == "node":
        return 10 if text_value(fields.get("节点类型")).strip() in {item[0] for item in CHAIN_NODE_TEMPLATE} else 0
    if table_key == "task":
        return 10 if text_value(fields.get("任务类型")).strip() in {item[1] for item in TASK_TEMPLATE} else 0
    return 0


def plan_duplicate_child_cleanup(
    records: dict[str, list[dict[str, Any]]],
    updates: dict[str, dict[str, dict[str, Any]]],
    deletes: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    for table_key, (link_field, business_field) in CHILD_SPECS.items():
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in records[table_key]:
            row_id = str(row.get("record_id") or "")
            if not row_id or row_id in deletes[table_key]:
                continue
            fields = row.get("fields") or {}
            links = linked_record_ids((updates[table_key].get(row_id) or {}).get(link_field, fields.get(link_field)))
            key = child_key(table_key, fields)
            for anchor_id in links:
                groups[(anchor_id, key)].append(row)
        for (anchor_id, key), rows in sorted(groups.items()):
            if not key or len(rows) <= 1:
                continue
            progressed = [row for row in rows if has_business_progress(table_key, row.get("fields") or {})]
            if len(progressed) > 1:
                protected.append(
                    {
                        "table": table_key,
                        "anchor_id": anchor_id,
                        "business_key": key,
                        "record_ids": [str(row.get("record_id") or "") for row in rows],
                        "reason": "Multiple duplicate rows contain business progress.",
                    }
                )
                continue
            standard_rows = [row for row in rows if template_quality(table_key, row.get("fields") or {}) > 0]
            candidates = standard_rows if len(standard_rows) == 1 else (progressed or rows)
            canonical = max(
                candidates,
                key=lambda row: (
                    template_quality(table_key, row.get("fields") or {}),
                    assignment_quality(row.get("fields") or {}),
                    text_value((row.get("fields") or {}).get("自动化批次")).strip(),
                    str(row.get("record_id") or ""),
                ),
            )
            canonical_id = str(canonical.get("record_id") or "")
            migrated_fields: list[str] = []
            if table_key == "task" and progressed and canonical_id != str(progressed[0].get("record_id") or ""):
                source_fields = progressed[0].get("fields") or {}
                canonical_fields = canonical.get("fields") or {}
                changes = updates[table_key].setdefault(canonical_id, {})
                protected_fields = {link_field, business_field, "任务类型", ANCHOR_DISPLAY_FIELD, "自动化批次"}
                for field_name, value in source_fields.items():
                    if field_name in protected_fields or value in EMPTY_VALUES:
                        continue
                    if field_name in TASK_PROGRESS_TRANSFER_FIELDS or canonical_fields.get(field_name) in EMPTY_VALUES:
                        changes[field_name] = value
                        migrated_fields.append(field_name)
            removed: list[str] = []
            for row in rows:
                row_id = str(row.get("record_id") or "")
                if row_id == canonical_id:
                    continue
                deletes[table_key].add(row_id)
                updates[table_key].pop(row_id, None)
                removed.append(row_id)
            actions.append(
                {
                    "table": table_key,
                    "anchor_id": anchor_id,
                    "business_key": key,
                    "canonical_record_id": canonical_id,
                    "deleted_record_ids": removed,
                    "canonical_has_business_progress": bool(progressed),
                    "migrated_fields": sorted(set(migrated_fields)),
                }
            )
    return actions, protected


def load_records(fs: Feishu) -> dict[str, list[dict[str, Any]]]:
    keys = ["interview", "anchor", *CHILD_SPECS]
    result: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fs.list_records, TABLES[key], 500): key for key in keys}
        for future in as_completed(futures):
            result[futures[future]] = future.result()
    return result


def child_key(table_key: str, fields: dict[str, Any]) -> str:
    business_field = CHILD_SPECS[table_key][1]
    return text_value(fields.get(business_field)).strip() if business_field != "singleton" else "singleton"


def build_plan(records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    interviews = records["interview"]
    interview_by_id = {str(row.get("record_id") or ""): row for row in interviews if row.get("record_id")}
    anchors = records["anchor"]
    anchor_by_id = {str(row.get("record_id") or ""): row for row in anchors if row.get("record_id")}
    anchors_by_source: dict[str, list[str]] = defaultdict(list)
    for anchor_id, anchor in anchor_by_id.items():
        for interview_id in linked_record_ids((anchor.get("fields") or {}).get("来源面试记录")):
            anchors_by_source[interview_id].append(anchor_id)

    updates: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in ["interview", "anchor", *CHILD_SPECS]}
    deletes: dict[str, set[str]] = {key: set() for key in ["anchor", *CHILD_SPECS]}
    merge_pairs: list[dict[str, str]] = []
    future_interview_anchor: dict[str, str] = {}

    for interview_id, source_ids in anchors_by_source.items():
        if len(source_ids) != 2:
            continue
        interview = interview_by_id.get(interview_id)
        if not interview:
            continue
        source_anchors = [anchor_by_id[anchor_id] for anchor_id in source_ids]
        generated = [row for row in source_anchors if text_value((row.get("fields") or {}).get("自动化批次")).startswith("LIVE-")]
        manual = [row for row in source_anchors if not text_value((row.get("fields") or {}).get("自动化批次")).strip()]
        if len(generated) != 1 or len(manual) != 1:
            raise RuntimeError(f"Ambiguous duplicate anchors for interview {interview_id}: {source_ids}")
        skeleton, rich = generated[0], manual[0]
        skeleton_id = str(skeleton["record_id"])
        rich_id = str(rich["record_id"])
        linked_ids = linked_record_ids((interview.get("fields") or {}).get("关联主播档案"))
        if linked_ids not in ([skeleton_id], [rich_id]):
            raise RuntimeError(f"Unexpected canonical link for duplicate interview {interview_id}: {linked_ids}")

        rich_fields = rich.get("fields") or {}
        skeleton_fields = skeleton.get("fields") or {}
        rich_changes: dict[str, Any] = {"来源面试记录": [interview_id]}
        for field_name in SAFE_MERGE_FIELDS:
            if rich_fields.get(field_name) in (None, "", [], {}) and skeleton_fields.get(field_name) not in (None, "", [], {}):
                rich_changes[field_name] = skeleton_fields[field_name]
        updates["anchor"][rich_id] = rich_changes
        updates["interview"][interview_id] = {
            "关联主播档案": [rich_id],
            "系统：已生成主播档案": True,
            "系统处理状态": "已合并重复主播档案",
            "系统处理备注": "系统保留客户维护的完整主播档案，并已迁移自动流程记录。",
        }
        for table_key, (link_field, _business_field) in CHILD_SPECS.items():
            for row in records[table_key]:
                if skeleton_id in linked_record_ids((row.get("fields") or {}).get(link_field)):
                    updates[table_key][str(row["record_id"])] = {link_field: [rich_id]}
        deletes["anchor"].add(skeleton_id)
        future_interview_anchor[interview_id] = rich_id
        merge_pairs.append({"interview_id": interview_id, "skeleton_anchor_id": skeleton_id, "rich_anchor_id": rich_id})

    remaining_anchor_ids = set(anchor_by_id) - deletes["anchor"]
    current_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for table_key, (link_field, _business_field) in CHILD_SPECS.items():
        for row in records[table_key]:
            row_id = str(row.get("record_id") or "")
            fields = row.get("fields") or {}
            future_link = linked_record_ids(fields.get(link_field))
            if row_id in updates[table_key]:
                future_link = linked_record_ids(updates[table_key][row_id].get(link_field))
            for anchor_id in future_link:
                if anchor_id in remaining_anchor_ids:
                    current_keys[(table_key, anchor_id)].add(child_key(table_key, fields))

    by_display: dict[str, set[str]] = defaultdict(set)
    by_name: dict[str, set[str]] = defaultdict(set)
    for anchor_id in remaining_anchor_ids:
        fields = anchor_by_id[anchor_id].get("fields") or {}
        display = anchor_display_name(fields)
        if display:
            by_display[display].add(anchor_id)
        for field_name in (ANCHOR_NAME_FIELD, "主播昵称", "真实姓名"):
            name = text_value(fields.get(field_name)).strip()
            if name:
                by_name[name].add(anchor_id)

    orphan_groups: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for table_key, (link_field, _business_field) in CHILD_SPECS.items():
        for row in records[table_key]:
            for anchor_id in linked_record_ids((row.get("fields") or {}).get(link_field)):
                if anchor_id not in anchor_by_id:
                    orphan_groups[anchor_id].append((table_key, row))

    orphan_actions: list[dict[str, Any]] = []
    for missing_id, rows in sorted(orphan_groups.items()):
        displays = sorted({text_value((row.get("fields") or {}).get(ANCHOR_DISPLAY_FIELD)).strip() for _table, row in rows if text_value((row.get("fields") or {}).get(ANCHOR_DISPLAY_FIELD)).strip()})
        is_artifact = all("客户体验" in display or "REPAIR-" in display for display in displays)
        candidate_ids: set[str] = set()
        for display in displays:
            if len(by_display.get(display) or set()) == 1:
                candidate_ids.update(by_display[display])
            name = display.split(" · ", 1)[0].strip()
            if len(by_name.get(name) or set()) == 1:
                candidate_ids.update(by_name[name])
            number = display.split(" · ", 1)[1].strip() if " · " in display else display
            if number.startswith("MYZB-AUTO-"):
                suffix = number[len("MYZB-AUTO-"):]
                matches = [interview_id for interview_id in interview_by_id if interview_id.endswith(suffix)]
                if len(matches) == 1:
                    interview_id = matches[0]
                    linked = future_interview_anchor.get(interview_id) or next(iter(linked_record_ids((interview_by_id[interview_id].get("fields") or {}).get("关联主播档案"))), "")
                    if linked in remaining_anchor_ids:
                        candidate_ids.add(linked)
        if is_artifact:
            for table_key, row in rows:
                deletes[table_key].add(str(row["record_id"]))
            orphan_actions.append({"missing_anchor_id": missing_id, "action": "delete_artifact_children", "count": len(rows), "displays": displays})
            continue
        if len(candidate_ids) != 1:
            raise RuntimeError(f"Unable to resolve orphan anchor {missing_id}: displays={displays}, candidates={sorted(candidate_ids)}")
        canonical_id = next(iter(candidate_ids))
        relinked = 0
        removed_duplicates = 0
        for table_key, row in rows:
            row_id = str(row["record_id"])
            key = child_key(table_key, row.get("fields") or {})
            if key in current_keys[(table_key, canonical_id)]:
                deletes[table_key].add(row_id)
                removed_duplicates += 1
            else:
                link_field = CHILD_SPECS[table_key][0]
                updates[table_key][row_id] = {link_field: [canonical_id]}
                current_keys[(table_key, canonical_id)].add(key)
                relinked += 1
        orphan_actions.append({"missing_anchor_id": missing_id, "action": "reconcile", "canonical_anchor_id": canonical_id, "relinked": relinked, "removed_duplicates": removed_duplicates, "displays": displays})

    future_numbers: dict[str, str] = {}
    number_groups: dict[str, list[str]] = defaultdict(list)
    for anchor_id in remaining_anchor_ids:
        number = text_value((anchor_by_id[anchor_id].get("fields") or {}).get("主播编号")).strip()
        if number:
            number_groups[number].append(anchor_id)
    for number, anchor_ids in number_groups.items():
        if len(anchor_ids) <= 1:
            continue
        for anchor_id in anchor_ids:
            source_ids = linked_record_ids((anchor_by_id[anchor_id].get("fields") or {}).get("来源面试记录"))
            if len(source_ids) != 1:
                raise RuntimeError(f"Duplicate number anchor {anchor_id} has ambiguous sources: {source_ids}")
            future_numbers[anchor_id] = f"MYZB-AUTO-{source_ids[0][-10:]}"
            updates["anchor"].setdefault(anchor_id, {})["主播编号"] = future_numbers[anchor_id]
    if len(set(future_numbers.values())) != len(future_numbers):
        raise RuntimeError(f"Generated replacement anchor numbers are not unique: {future_numbers}")

    duplicate_child_actions, protected_duplicate_children = plan_duplicate_child_cleanup(records, updates, deletes)

    return {
        "merge_pairs": merge_pairs,
        "orphan_actions": orphan_actions,
        "replacement_anchor_numbers": future_numbers,
        "duplicate_child_actions": duplicate_child_actions,
        "protected_duplicate_children": protected_duplicate_children,
        "updates": updates,
        "deletes": {key: sorted(values) for key, values in deletes.items()},
    }


def affected_snapshot(records: dict[str, list[dict[str, Any]]], plan: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table_key in ["interview", "anchor", *CHILD_SPECS]:
        ids = set(plan["updates"].get(table_key, {})) | set(plan["deletes"].get(table_key, []))
        result[table_key] = [row for row in records[table_key] if str(row.get("record_id") or "") in ids]
    return result


def apply_plan(fs: Feishu, plan: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    update_results: dict[str, Any] = {}
    for table_key in ["interview", "anchor", *CHILD_SPECS]:
        rows = [
            {"record_id": record_id, "fields": fields}
            for record_id, fields in plan["updates"].get(table_key, {}).items()
            if record_id not in set(plan["deletes"].get(table_key, []))
        ]
        results = fs.batch_update(TABLES[table_key], rows, batch_size=100) if rows else []
        if any(result.get("code") != 0 for result in results):
            raise RuntimeError(f"Update failed for {table_key}: {results}")
        update_results[table_key] = results

    delete_results: dict[str, list[dict[str, Any]]] = {}
    for table_key in [*CHILD_SPECS, "anchor"]:
        record_ids = plan["deletes"].get(table_key, [])
        results = fs.batch_delete(TABLES[table_key], record_ids, batch_size=500) if record_ids else []
        if any(result.get("code") != 0 for result in results):
            raise RuntimeError(f"Batch delete failed for {table_key}: {results}")
        delete_results[table_key] = results

    current = load_records(fs)
    anchors_by_id = {str(row.get("record_id") or ""): row for row in current["anchor"] if row.get("record_id")}
    pairs = []
    for interview in current["interview"]:
        fields = interview.get("fields") or {}
        candidate = text_value(fields.get("候选人姓名")).strip()
        if not candidate or "体验样本" in candidate or is_demo_batch(fields.get("自动化批次")):
            continue
        if not any(fields.get(field) is True for field in TRANSFER_FIELDS):
            continue
        anchor = find_existing_anchor_for_interview(fs, interview, anchors_by_id)
        if anchor:
            pairs.append((anchor, interview))
    child_repair = ensure_recovered_anchor_children(fs, pairs, "REPAIR-20260822-INTEGRITY")
    display_sync = sync_anchor_display_names(fs, out_dir)
    assignment_sync = sync_missing_interview_display_fields(fs, out_dir)
    return {
        "update_results": update_results,
        "delete_counts": {key: len(plan["deletes"].get(key, [])) for key in delete_results},
        "delete_batches": {key: len(value) for key, value in delete_results.items()},
        "child_repair": child_repair,
        "display_sync": display_sync,
        "assignment_sync": assignment_sync,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot and repair proven live Base integrity defects.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    fs = Feishu(get_tenant_token(args.env))
    records = load_records(fs)
    plan = build_plan(records)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    write_json(args.out_dir / f"live_integrity_repair_plan_{stamp}.json", plan)
    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "merge_pairs": len(plan["merge_pairs"]),
        "orphan_groups": len(plan["orphan_actions"]),
        "updates": {key: len(value) for key, value in plan["updates"].items()},
        "deletes": {key: len(value) for key, value in plan["deletes"].items()},
        "replacement_anchor_numbers": len(plan["replacement_anchor_numbers"]),
        "duplicate_child_actions": len(plan["duplicate_child_actions"]),
        "protected_duplicate_children": len(plan["protected_duplicate_children"]),
    }
    if args.apply:
        snapshot = affected_snapshot(records, plan)
        snapshot_path = args.out_dir / f"live_integrity_snapshot_before_{stamp}.json"
        write_json(snapshot_path, snapshot)
        summary["snapshot"] = str(snapshot_path)
        summary["result"] = apply_plan(fs, plan, args.out_dir)
    write_json(args.out_dir / f"live_integrity_repair_result_{stamp}.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
