from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

os.environ.setdefault("FEISHU_APP_ID", "test-app")
os.environ.setdefault("FEISHU_APP_SECRET", "test-secret")

import automation_service as service


class FeishuLongConnectionTests(unittest.TestCase):
    def tearDown(self) -> None:
        with service.API_QUOTA_STATE_LOCK:
            service.API_QUOTA_STATE.clear()
            service.API_QUOTA_STATE.update({"exhausted": False})

    def test_monthly_quota_error_opens_circuit_breaker(self) -> None:
        error = RuntimeError('HTTP 429: {"code":99991403,"msg":"This month\'s API call quota has been exceeded"}')

        self.assertTrue(service.mark_api_quota_exhausted(error))
        self.assertFalse(service.api_quota_available())
        self.assertGreater(service.api_quota_wait_seconds(), 0)

    def test_scheduler_defaults_avoid_minute_full_table_scans(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            intervals = service.scheduler_intervals()

        self.assertEqual(300, intervals["anchor"])
        self.assertEqual(3600, intervals["personnel_probe"])
        self.assertEqual(21600, intervals["personnel"])
        self.assertEqual(21600, intervals["integrity"])
        self.assertEqual(86400, intervals["reporting"])
    def test_subscription_is_created_and_verified(self) -> None:
        class FakeFeishu:
            def __init__(self, token: str) -> None:
                self.token = token
                self.calls: list[tuple[str, str, dict[str, str]]] = []

            def api(self, method: str, path: str, query: dict[str, str]) -> dict[str, object]:
                self.calls.append((method, path, query))
                if method == "GET":
                    return {"code": 0, "data": {"is_subscribe": True}}
                return {"code": 0, "msg": "Success"}

        fake = FakeFeishu("tenant-token")
        with (
            patch.object(service, "tenant_token", return_value="tenant-token"),
            patch.object(service, "Feishu", return_value=fake),
        ):
            result = service.ensure_bitable_event_subscription()

        self.assertEqual({"subscribed": True}, result)
        self.assertEqual(["POST", "GET"], [call[0] for call in fake.calls])

    def test_long_connection_event_queues_non_deleted_interview_records(self) -> None:
        data = SimpleNamespace(
            header=SimpleNamespace(event_type="drive.file.bitable_record_changed_v1"),
            event=SimpleNamespace(
                file_token=service.APP_TOKEN,
                table_id=service.TABLES["interview"],
                action_list=[
                    SimpleNamespace(record_id="rec-a", action="record_added"),
                    SimpleNamespace(record_id="rec-b", action="record_edited"),
                    SimpleNamespace(record_id="rec-c", action="record_deleted"),
                ],
            ),
        )
        with patch.object(service, "enqueue_feishu_record_changes", return_value={"queued": 2}) as enqueue:
            result = service.handle_long_connection_record_event(data)

        self.assertEqual({"queued": 2}, result)
        self.assertEqual(["rec-a", "rec-b"], enqueue.call_args.args[3])
        self.assertEqual("long_connection", enqueue.call_args.args[4])

    def test_anchor_event_updates_linked_interview_followup(self) -> None:
        fs = object()
        anchor = {"record_id": "rec-anchor", "fields": {"来源面试记录": ["rec-interview"]}}
        with (
            patch.object(service, "sync_one_anchor_number", return_value={"updated": False}) as number_sync,
            patch.object(service, "read_record", return_value=anchor) as read,
            patch.object(
                service,
                "sync_one_anchor_followup_to_interviews",
                return_value={"updated_interviews": 1},
            ) as followup_sync,
        ):
            result = service.process_feishu_record_change(
                fs,
                service.TABLES["anchor"],
                "rec-anchor",
                "long_connection",
            )

        number_sync.assert_called_once()
        read.assert_called_once_with(fs, service.TABLES["anchor"], "rec-anchor")
        followup_sync.assert_called_once_with(fs, anchor)
        self.assertEqual(["面试跟进情况（日更）"], result["updated"])

    def test_other_base_is_not_queued(self) -> None:
        result = service.enqueue_feishu_record_changes(
            "drive.file.bitable_record_changed_v1",
            "other-base",
            service.TABLES["interview"],
            ["rec-a"],
            "long_connection",
        )
        self.assertEqual(0, result["queued"])
        self.assertEqual("other_app", result["ignored"])

    def test_contact_event_wakes_personnel_provisioning(self) -> None:
        data = SimpleNamespace(header=SimpleNamespace(event_type="contact.user.created_v3"))
        service.PERSONNEL_WAKE_EVENT.clear()

        result = service.handle_long_connection_contact_event(data)

        self.assertTrue(result["queued"])
        self.assertTrue(service.PERSONNEL_WAKE_EVENT.is_set())
        service.PERSONNEL_WAKE_EVENT.clear()

    def test_personnel_probe_only_wakes_on_directory_change(self) -> None:
        service.PERSONNEL_WAKE_EVENT.clear()
        with (
            patch.object(service, "tenant_token", return_value="tenant-token"),
            patch.object(service, "Feishu", return_value=object()),
            patch.object(
                service,
                "sync_personnel_directory",
                return_value={"created": 1, "updated": 0, "deactivated": 0},
            ),
        ):
            result = service.run_personnel_probe_cycle()

        self.assertEqual(1, result["changed"])
        self.assertTrue(service.PERSONNEL_WAKE_EVENT.is_set())
        service.PERSONNEL_WAKE_EVENT.clear()


if __name__ == "__main__":
    unittest.main()
