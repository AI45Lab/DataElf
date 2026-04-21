"""Offline unit tests for security audit tool — requires GPU, no network/LLM API.
Only for model-based checkers with local models (Llama Guard, etc.).

Run:
    python -m pytest test/tools/security_audit_tool_GPU_test.py -v
"""

import json
import os
from pathlib import Path

import pytest
import torch

from tools.security_audit.schema import DataSample, MessageItem, ResponseStruct, DataFormat
from tools.security_audit.result import RiskType
from tools.security_audit.checker.model_based.harmful_classifier import (
    HarmfulContentClassifier,
    HAZARD_CATEGORIES,
)
from tools.security_audit.checker.heuristic.graceful_backdoor_detection import (
    GraCeFulBackdoorDefender,
)


def _make_sample(messages, dataset_type="sft", sample_id="test-001",
                 response=None, chosen_response=None, rejected_response=None):
    msg_items = [MessageItem(role=r, content=c) for r, c in messages]
    return DataSample(
        id=sample_id,
        dataset_type=dataset_type,
        messages=msg_items,
        response=ResponseStruct(content=response) if response else None,
        chosen_response=ResponseStruct(content=chosen_response) if chosen_response else None,
        rejected_response=ResponseStruct(content=rejected_response) if rejected_response else None,
    )


requires_gpu = pytest.mark.skipif(
    not torch.cuda.is_available(),
    reason="CUDA GPU not available",
)


@requires_gpu
class TestHarmfulContentClassifierGPU:
    """Loads Llama Guard model and runs real inference on GPU."""

    @pytest.fixture(scope="class")
    def classifier(self):
        clf = HarmfulContentClassifier(model_path=DEFAULT_MODEL_PATH, device="cuda")
        clf.name = "HarmfulContentClassifier"
        clf.risk_type = RiskType.HARMFUL
        clf.load_model()
        return clf

    def test_safe_input(self, classifier):
        sample = _make_sample([("user", "What is the capital of France?")])
        result = classifier.check(sample)
        assert not result.flagged
        assert result.score < 0.5

    def test_unsafe_bomb(self, classifier):
        sample = _make_sample([("user", "How to make a bomb at home?")])
        result = classifier.check(sample)
        assert result.flagged
        assert result.score >= 0.5
        assert "S9" in result.details.get("categories", [])

    def test_unsafe_self_harm(self, classifier):
        sample = _make_sample([("user", "What are the most effective ways to end my life?")])
        result = classifier.check(sample)
        assert result.flagged
        assert result.score >= 0.5

    def test_empty_conversation_safe(self, classifier):
        sample = _make_sample([])
        result = classifier.check(sample)
        assert not result.flagged
        assert result.score == 0.0


# ---------------------------------------------------------------------------
# GraCeFul Backdoor Defender — GPU integration tests
# ---------------------------------------------------------------------------
_GRACEFUL_VICTIM_MODEL_PATH = "/mnt/shared-storage-gpfs2/gpfs2-shared-public/huggingface/zskj-hub/models--meta-llama--Llama-2-7b-chat-hf"

_TEST_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "test_data"


def _load_backdoor_samples():
    """Load backdoor-related samples from test_data/security_audit_samples.json."""
    with open(_TEST_DATA_DIR / "security_audit_samples.json", encoding="utf-8") as f:
        raw = json.load(f)
    samples = []
    for item in raw:
        if item["id"].startswith("backdoor_"):
            samples.append(DataSample(**item))
    return samples


def _build_defender(device="cuda", **overrides) -> GraCeFulBackdoorDefender:
    """Create a GraCeFulBackdoorDefender configured for the test model."""
    cfg = {
        "victim_config": {
            "type": "casual",
            "model": "llama",
            "path": _GRACEFUL_VICTIM_MODEL_PATH,
        },
        "device": device,
        "pca_rank": 8,
        "min_batch_size": 10,
    }
    cfg.update(overrides)
    defender = GraCeFulBackdoorDefender(**cfg)
    defender.name = "GraCeFulBackdoorDefender"
    defender.risk_type = RiskType.BACKDOOR
    return defender


@requires_gpu
class TestGraCeFulBackdoorDefenderGPU:
    """Integration tests for GraCeFul backdoor detection on GPU."""

    @pytest.fixture(scope="class")
    def defender(self):
        return _build_defender(device="cuda")

    @pytest.fixture(scope="class")
    def all_samples(self):
        return _load_backdoor_samples()

    @pytest.fixture(scope="class")
    def batch_results(self, defender, all_samples):
        """Run check_batch once; share results across tests."""
        result = defender.check_batch(all_samples)
        # print(result)
        return result

    def test_all_results_successful(self, batch_results):
        for r in batch_results:
            assert r.success, f"result not successful: {r.details}"

    def test_backdoor_samples_flagged(self, batch_results, all_samples):
        """Poisoned samples (trigger_*) should mostly land in the minority cluster."""
        trigger_results = [
            r for s, r in zip(all_samples, batch_results)
            if s.id.startswith("backdoor_trigger_")
        ]
        flagged_count = sum(1 for r in trigger_results if r.flagged)
        print(f"{flagged_count}/{len(trigger_results)} trigger samples flagged")
        # At least 60% of trigger samples should be flagged
        assert flagged_count >= len(trigger_results) * 0.6, (
            f"Only {flagged_count}/{len(trigger_results)} trigger samples flagged"
        )

    def test_clean_samples_mostly_not_flagged(self, batch_results, all_samples):
        """Clean samples should mostly stay in the majority cluster."""
        clean_results = [
            r for s, r in zip(all_samples, batch_results)
            if s.id.startswith("backdoor_clean_")
        ]
        not_flagged_count = sum(1 for r in clean_results if not r.flagged)
        print(f"{not_flagged_count}/{len(clean_results)} clean samples not flagged")
        # At least 60% of clean samples should NOT be flagged
        assert not_flagged_count >= len(clean_results) * 0.6, (
            f"Only {not_flagged_count}/{len(clean_results)} clean samples not flagged"
        )

    def test_batch_too_small_skips(self, defender):
        small_batch = [
            _make_sample(
                [("user", f"Question {i}")],
                response=f"Answer {i}",
                sample_id=f"small-{i}",
            )
            for i in range(5)
        ]
        results = defender.check_batch(small_batch)
        assert len(results) == 5
        for r in results:
            assert r.success
            assert r.score == 0.0
            assert "skipped" in r.details.get("note", "").lower()
