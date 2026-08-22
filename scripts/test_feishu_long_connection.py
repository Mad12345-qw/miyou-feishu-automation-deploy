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


if __name__ == "__main__":
    unittest.main()
