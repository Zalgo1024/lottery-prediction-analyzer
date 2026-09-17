import tempfile
import unittest
from pathlib import Path

from web.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def test_create_update_get_round_trips_json_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            try:
                task_id = store.create("predict", {"lottery": "双色球", "groups": 3})
                store.update(
                    task_id,
                    status="done",
                    progress=100,
                    message="完成",
                    result={"record_dir": "training/demo", "numbers": [1, 2, 3]},
                )

                task = store.get(task_id)
            finally:
                store.close()

        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["type"], "predict")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["progress"], 100)
        self.assertEqual(task["params"], {"lottery": "双色球", "groups": 3})
        self.assertEqual(task["result"]["record_dir"], "training/demo")


if __name__ == "__main__":
    unittest.main()
