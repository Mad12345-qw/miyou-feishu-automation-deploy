from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

import sync_missing_personal_entries as personal
import sync_missing_workbench_rows as workbench
from miyou_system_automation import Feishu, TABLES, contact_api_with_retry, find_existing_anchor_for_interview, load_env, personnel_fields_changed, request_json, sync_selected_interview_assignments
from repair_live_data_integrity import CHILD_SPECS, plan_duplicate_child_cleanup


USER_ID = "ou_correct_user"
WRONG_USER_ID = "ou_wrong_user"
VIEW_ID = "vew_existing"


class FakeFeishu:
    def __init__(self) -> None:
        self.patches = []
        self.updates = []

    def fields(self, table_id: str):
        if table_id == personal.TABLES["interview"]:
            return [{"field_name": "招募人账号（系统）", "field_id": "fld_recruiter"}]
        return []

    def list_records(self, table_id: str, page_size: int = 500):
        if table_id == personal.TABLES["personnel"]:
            return [
                {
                    "record_id": "rec_person",
                    "fields": {
                        "姓名": "测试员工",
                        "飞书用户": [{"id": USER_ID}],
                        "在职状态": "在职",
                        "账号状态": "正常",
                        "是否创建个人入口": True,
                        "角色": ["招募经纪人"],
                    },
                }
            ]
        if table_id == workbench.WORKBENCH_TABLE:
            return [
                {
                    "record_id": "rec_entry",
                    "fields": {
                        "我要做什么": "个人入口：测试员工的候选人",
                        "谁来操作": "本人",
                        "操作内容": "测试员工直接查看自己的候选人",
                        "系统自动": "系统按飞书人员账号自动筛选本人记录",
                        "完成时限": "每天使用",
                        "点这里办理": {"link": "https://wrong.example/old", "text": "打开我的候选人"},
                        "员工账号": [{"id": WRONG_USER_ID}],
                    },
                }
            ]
        return []

    def api(self, method: str, path: str, query=None, body=None):
        if method == "GET" and path.endswith("/views"):
            return {"code": 0, "data": {"items": [{"view_id": VIEW_ID, "view_name": "招聘_测试员工_候选人", "view_type": "grid"}]}}
        if method == "GET" and path.endswith(f"/views/{VIEW_ID}"):
            return {
                "code": 0,
                "data": {
                    "view": {
                        "view_id": VIEW_ID,
                        "view_name": "招聘_测试员工_候选人",
                        "view_type": "grid",
                        "property": {
                            "filter_info": {
                                "conditions": [
                                    {
                                        "field_id": "fld_recruiter",
                                        "operator": "is",
                                        "value": f'["{WRONG_USER_ID}"]',
                                    }
                                ]
                            }
                        },
                    }
                },
            }
        if method == "PATCH" and path.endswith(f"/views/{VIEW_ID}"):
            self.patches.append(body)
            return {"code": 0, "data": {}}
        raise AssertionError((method, path, query, body))

    def batch_create(self, table_id: str, records, batch_size: int = 500):
        return []

    def batch_update(self, table_id: str, records, batch_size: int = 500):
        self.updates.extend(records)
        return [{"code": 0, "data": {"records": records}}]


class SyncIntegrityTests(unittest.TestCase):
    def test_batch_delete_uses_feishu_batch_endpoint(self) -> None:
        fs = Feishu("token")
        calls = []

        def api(method, path, query=None, body=None):
            calls.append((method, path, body))
            return {"code": 0}

        fs.api = api
        results = fs.batch_delete("tbl_test", ["rec_a", "rec_b", "rec_c"], batch_size=2)

        self.assertEqual(2, len(results))
        self.assertEqual({"records": ["rec_a", "rec_b"]}, calls[0][2])
        self.assertTrue(calls[0][1].endswith("/records/batch_delete"))

    def test_pristine_duplicate_children_keep_the_assigned_newer_row(self) -> None:
        records = {key: [] for key in CHILD_SPECS}
        records["task"] = [
            {"record_id": "rec_old", "fields": {"对应主播": ["rec_anchor"], "任务名称": "主播甲 首次建联", "工作状态": "未开始", "负责人": "待分配", "自动化批次": "LIVE-20260801"}},
            {"record_id": "rec_new", "fields": {"对应主播": ["rec_anchor"], "任务名称": "主播甲 首次建联", "工作状态": "未开始", "负责人": "运营甲", "自动化批次": "LIVE-20260822"}},
        ]
        updates = {key: {} for key in CHILD_SPECS}
        deletes = {key: set() for key in CHILD_SPECS}

        actions, protected = plan_duplicate_child_cleanup(records, updates, deletes)

        self.assertEqual([], protected)
        self.assertEqual("rec_new", actions[0]["canonical_record_id"])
        self.assertEqual({"rec_old"}, deletes["task"])

    def test_pristine_duplicate_tasks_keep_the_standard_template_type(self) -> None:
        records = {key: [] for key in CHILD_SPECS}
        records["task"] = [
            {"record_id": "rec_legacy", "fields": {"对应主播": ["rec_anchor"], "任务名称": "主播甲 首次建联", "任务类型": "面试与承接", "工作状态": "未开始", "负责人": "运营甲"}},
            {"record_id": "rec_standard", "fields": {"对应主播": ["rec_anchor"], "任务名称": "主播甲 首次建联", "任务类型": "建联", "工作状态": "未开始", "负责人": "待分配"}},
        ]
        updates = {key: {} for key in CHILD_SPECS}
        deletes = {key: set() for key in CHILD_SPECS}

        actions, protected = plan_duplicate_child_cleanup(records, updates, deletes)

        self.assertEqual([], protected)
        self.assertEqual("rec_standard", actions[0]["canonical_record_id"])
        self.assertEqual({"rec_legacy"}, deletes["task"])

    def test_legacy_task_progress_moves_to_the_standard_task(self) -> None:
        records = {key: [] for key in CHILD_SPECS}
        records["task"] = [
            {"record_id": "rec_legacy", "fields": {"对应主播": ["rec_anchor"], "任务名称": "主播甲 首次建联", "任务类型": "面试与承接", "工作状态": "已完成", "负责人": "运营甲", "完成情况": "已联系"}},
            {"record_id": "rec_standard", "fields": {"对应主播": ["rec_anchor"], "任务名称": "主播甲 首次建联", "任务类型": "建联", "工作状态": "未开始", "负责人": "待分配"}},
        ]
        updates = {key: {} for key in CHILD_SPECS}
        deletes = {key: set() for key in CHILD_SPECS}

        actions, protected = plan_duplicate_child_cleanup(records, updates, deletes)

        self.assertEqual([], protected)
        self.assertEqual("rec_standard", actions[0]["canonical_record_id"])
        self.assertEqual("已完成", updates["task"]["rec_standard"]["工作状态"])
        self.assertEqual("运营甲", updates["task"]["rec_standard"]["负责人"])
        self.assertEqual("已联系", updates["task"]["rec_standard"]["完成情况"])
        self.assertEqual({"rec_legacy"}, deletes["task"])

    def test_duplicate_children_with_progress_on_both_rows_are_protected(self) -> None:
        records = {key: [] for key in CHILD_SPECS}
        records["node"] = [
            {"record_id": "rec_a", "fields": {"关联主播": ["rec_anchor"], "节点类型": "签约", "节点状态": "已完成", "交付物/附件": ["file-a"]}},
            {"record_id": "rec_b", "fields": {"关联主播": ["rec_anchor"], "节点类型": "签约", "节点状态": "已完成", "交付物/附件": ["file-b"]}},
        ]
        updates = {key: {} for key in CHILD_SPECS}
        deletes = {key: set() for key in CHILD_SPECS}

        actions, protected = plan_duplicate_child_cleanup(records, updates, deletes)

        self.assertEqual([], actions)
        self.assertEqual(1, len(protected))
        self.assertEqual(set(), deletes["node"])

    def test_feishu_frequency_limit_is_retried_with_backoff(self) -> None:
        payload = b'{"code":99991400,"msg":"request trigger frequency limit"}'
        calls = 0

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"code":0,"data":{"ok":true}}'

        def open_url(_request, timeout):
            nonlocal calls
            self.assertEqual(90, timeout)
            calls += 1
            if calls == 1:
                raise HTTPError("https://open.feishu.cn/test", 400, "Bad Request", {}, BytesIO(payload))
            return Response()

        with patch("miyou_system_automation.urllib.request.urlopen", side_effect=open_url), patch("miyou_system_automation.time.sleep") as sleep:
            result = request_json("GET", "https://open.feishu.cn/test")

        self.assertEqual(0, result["code"])
        self.assertEqual(2, calls)
        sleep.assert_called_once_with(2.0)

    def test_env_loader_removes_systemd_style_quotes(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.env"
            path.write_text('FEISHU_APP_ID="cli_test"\nFEISHU_APP_SECRET=plain\n', encoding="utf-8")
            self.assertEqual({"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "plain"}, load_env(path))

    def test_rich_manual_anchor_wins_over_generated_skeleton(self) -> None:
        interview = {"record_id": "rec_interview", "fields": {"候选人姓名": "主播甲", "关联主播档案": ["rec_auto"]}}
        anchors = {
            "rec_auto": {"record_id": "rec_auto", "fields": {"主播名字": "主播甲", "来源面试记录": ["rec_interview"], "自动化批次": "LIVE-1", "主播状态": "待开播"}},
            "rec_manual": {"record_id": "rec_manual", "fields": {"主播名字": "主播甲", "来源面试记录": ["rec_interview"], "主播状态": "正常直播", "直播场景": "已配置"}},
        }
        selected = find_existing_anchor_for_interview(object(), interview, anchors)
        self.assertEqual("rec_manual", selected["record_id"])

    def test_personnel_alias_resolves_to_the_account(self) -> None:
        class AliasFeishu:
            def __init__(self) -> None:
                self.updates = []

            def list_records(self, table_id, page_size=500):
                self.assert_table = table_id
                return [{"record_id": "rec_person", "fields": {"姓名": "雨哲", "匹配别名": "雨者", "飞书用户": [{"id": USER_ID}], "在职状态": "在职", "账号状态": "正常"}}]

            def batch_update(self, table_id, records, batch_size=100):
                self.updates.extend(records)
                return [{"code": 0, "data": {"records": records}}]

        fs = AliasFeishu()
        interview = {"record_id": "rec_interview", "fields": {"面试官": "雨者", "面试官账号（系统）": []}}
        report = sync_selected_interview_assignments(fs, [interview])
        self.assertEqual(1, report["updated_records"])
        self.assertEqual([{"id": USER_ID}], fs.updates[0]["fields"]["面试官账号（系统）"])

    def test_unchanged_personnel_fields_do_not_trigger_a_write(self) -> None:
        current = {"姓名": "测试员工", "飞书用户": [{"id": USER_ID}], "角色": ["面试官", "招募经纪人"], "是否创建个人入口": True}
        desired = {"姓名": "测试员工", "飞书用户": [{"id": USER_ID}], "角色": ["招募经纪人", "面试官"], "是否创建个人入口": True}
        self.assertFalse(personnel_fields_changed(current, desired))
        self.assertTrue(personnel_fields_changed(current, {**desired, "姓名": "新名字"}))

    def test_contact_internal_error_is_retried(self) -> None:
        class ContactFeishu:
            def __init__(self) -> None:
                self.calls = 0

            def api(self, method, path, query):
                self.calls += 1
                return {"code": 0, "data": {"items": []}} if self.calls == 3 else {"code": 9999, "msg": "Internal Error"}

        fs = ContactFeishu()
        with patch("miyou_system_automation.time.sleep"):
            result = contact_api_with_retry(fs, "/contact/v3/users", {})
        self.assertEqual(0, result["code"])
        self.assertEqual(3, fs.calls)

    def test_existing_named_business_view_with_wrong_user_is_repaired(self) -> None:
        fs = FakeFeishu()
        people = personal.active_people(fs)
        spec = ("interview", "招募人账号（系统）", "招聘", "候选人", {"招募经纪人"}, "候选人", "打开我的候选人")
        with patch.object(personal, "SPECS", [spec]):
            report = personal.create_missing_business_views(fs, people)
        self.assertEqual(1, len(report["repaired"]))
        self.assertEqual([], report["failed"])
        self.assertEqual(f'["{USER_ID}"]', fs.patches[0]["property"]["filter_info"]["conditions"][0]["value"])

    def test_existing_workbench_row_with_wrong_user_and_link_is_repaired(self) -> None:
        fs = FakeFeishu()
        spec = ("interview", "招募人账号（系统）", "招聘", "候选人", {"招募经纪人"}, "候选人", "打开我的候选人")
        # This test needs an already-correct business view; only the workbench row is stale.
        original_api = fs.api

        def correct_view_api(method: str, path: str, query=None, body=None):
            response = original_api(method, path, query, body)
            if method == "GET" and path.endswith(f"/views/{VIEW_ID}"):
                response["data"]["view"]["property"]["filter_info"]["conditions"][0]["value"] = f'["{USER_ID}"]'
            return response

        fs.api = correct_view_api
        with TemporaryDirectory() as tmp, patch.object(workbench, "SPECS", [spec]):
            report = workbench.sync_missing_workbench_rows(fs, Path(tmp))
        self.assertEqual(1, report["planned_repaired"])
        self.assertEqual(1, report["repaired"])
        self.assertEqual([USER_ID], workbench.user_ids(fs.updates[0]["fields"]["员工账号"]))
        self.assertIn(f"view={VIEW_ID}", fs.updates[0]["fields"]["点这里办理"]["link"])


if __name__ == "__main__":
    unittest.main()
