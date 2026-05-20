import tempfile
import unittest
from pathlib import Path

from config import Config
from runtime import JobManager
from runtime.executor import RuntimeExecutor
from tools import get_global_registry


class RuntimeExecutorLogCallbackTest(unittest.TestCase):
    def test_runtime_executor_streams_log_entries_to_handler(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Config()
            cfg.execution.enable_log = False
            job_manager = JobManager(jobs_dir=Path(tmp_dir) / ".jobs")
            registry = get_global_registry()
            registry.clear()
            executor = RuntimeExecutor(
                job_manager=job_manager,
                tool_registry=registry,
                config=cfg,
                database=None,
            )

            job = job_manager.create_job("stream logs", mode="run")
            streamed = []
            result = executor.execute(
                job.job_id,
                '''
log_step("first runtime log")
save_result({"ok": True})
''',
                log_handler=streamed.append,
            )

            self.assertTrue(result["success"])
            self.assertEqual(
                [entry["message"] for entry in streamed],
                [
                    "first runtime log",
                    "Saving result to job",
                    "Job completed",
                ],
            )


if __name__ == "__main__":
    unittest.main()
