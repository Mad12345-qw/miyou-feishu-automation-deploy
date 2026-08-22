from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sync_missing_personal_entries as personal
import sync_missing_workbench_rows as workbench
from miyou_system_automation import TABLES, contact_api_with_retry, find_existing_anchor_for_interview, personnel_fields_changed, sync_selected_interview_assignments


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
