import unittest

from web_api.run_preflight import (
    build_dataset_selection_payload,
    find_dataset_mentions,
    needs_dataset_selection,
    resolve_dataset_answer,
)


class WebApiRunPreflightTest(unittest.TestCase):
    def test_find_dataset_mentions_uses_known_dataset_names(self):
        dataset_schemas = {
            "security_audit_samples": ["text"],
            "companies": ["name"],
        }

        mentions = find_dataset_mentions(
            "run security_audit on security_audit_samples",
            dataset_schemas,
        )

        self.assertEqual(mentions, ["security_audit_samples"])

    def test_needs_dataset_selection_when_task_has_no_dataset(self):
        dataset_schemas = {"security_audit_samples": ["text"]}

        self.assertTrue(needs_dataset_selection("run security_audit", dataset_schemas))
        self.assertFalse(
            needs_dataset_selection(
                "run security_audit on security_audit_samples",
                dataset_schemas,
            )
        )

    def test_build_dataset_selection_payload_lists_backend_datasets(self):
        payload = build_dataset_selection_payload(
            {
                "companies": ["name"],
                "security_audit_samples": ["text"],
            }
        )

        self.assertEqual(payload["checkpoint_type"], "dataset_selection")
        self.assertEqual(
            payload["payload"]["options"],
            ["companies", "security_audit_samples"],
        )
        self.assertEqual(
            payload["payload"]["suggested_defaults"],
            {"dataset_name": "security_audit_samples"},
        )

    def test_resolve_dataset_answer_accepts_default_and_dataset_name(self):
        dataset_schemas = {
            "companies": ["name"],
            "security_audit_samples": ["text"],
        }

        self.assertEqual(resolve_dataset_answer("default", dataset_schemas), "security_audit_samples")
        self.assertEqual(resolve_dataset_answer("companies", dataset_schemas), "companies")

    def test_resolve_dataset_answer_rejects_unknown_dataset(self):
        with self.assertRaises(ValueError):
            resolve_dataset_answer("missing_dataset", {"security_audit_samples": ["text"]})


if __name__ == "__main__":
    unittest.main()
