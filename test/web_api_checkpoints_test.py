import unittest


from web_api.checkpoints import WebCheckpointBroker


class WebApiCheckpointsTest(unittest.TestCase):
    def test_checkpoint_answer_is_returned_to_waiter(self):
        broker = WebCheckpointBroker()
        checkpoint = broker.create_checkpoint(
            job_id="job_1",
            checkpoint_type="clarification",
            payload={"prompt": "Which dataset?"},
        )

        self.assertTrue(checkpoint.checkpoint_id.startswith("chk_"))
        self.assertTrue(
            broker.answer_checkpoint(
                job_id="job_1",
                checkpoint_id=checkpoint.checkpoint_id,
                answer={"decision": "answer", "answer": "security_audit_samples"},
            )
        )

        response = broker.wait_for_answer(
            job_id="job_1",
            checkpoint_id=checkpoint.checkpoint_id,
            timeout_seconds=0.1,
        )

        self.assertEqual(response, {"decision": "answer", "answer": "security_audit_samples"})

    def test_checkpoint_rejects_unknown_answer(self):
        broker = WebCheckpointBroker()

        self.assertFalse(
            broker.answer_checkpoint(
                job_id="missing",
                checkpoint_id="chk_missing",
                answer={"decision": "answer", "answer": "x"},
            )
        )


if __name__ == "__main__":
    unittest.main()
