# Adapted from GraCeFul: Gradient-based Casual Frequency Defense
# Source: https://github.com/ZrW00/GraceFul

from typing import Any, Dict, List, Optional

import numpy as np
import torch
from torch import autograd

from .graceful_helpers import get_casual_dataloader, load_victim, CasualLLMVictim, dct_2d

from ..base import HeuristicChecker
from ..registry import CheckerRegistry
from ...schema import DataSample
from ...result import CheckResult, RiskType


_MIN_BATCH_SIZE = 10


@CheckerRegistry.register(RiskType.BACKDOOR, tags=["heuristic"])
class GraCeFulBackdoorDefender(HeuristicChecker):
    """Backdoor data detector based on the GraCeFul clustering pipeline.

    Reference: https://arxiv.org/abs/2412.02454
    """

    def __init__(
        self,
        victim_config: Optional[Dict[str, Any]] = None,
        target_para: str = "lm_head.weight",
        pca_rank: int = 32,
        threshold: float = 0.5,
        min_batch_size: int = _MIN_BATCH_SIZE,
        dct_keep_ratio: float = 0.125,
        device: str = "auto",
        **kwargs,
    ):
        """
        Args:
            victim_config: Optional GraCeFul victim config dict (type/model/path, etc.).
            target_para: Parameter name substring for gradient extraction.
            pca_rank: Number of PCA components.
            threshold: Score threshold for flagging (default 0.5).
            min_batch_size: Minimum samples required for clustering.
            dct_keep_ratio: Keep top-left ratio for DCT (default 1/8).
            device: "auto" | "cuda" | "cpu".
        """
        super().__init__(**kwargs)
        self.victim_config = victim_config or {}
        self.target_para = target_para
        self.pca_rank = pca_rank
        self.threshold = threshold
        self.min_batch_size = min_batch_size
        self.dct_keep_ratio = dct_keep_ratio
        self.device = device
        self._victim: Optional[CasualLLMVictim] = None

    def check(self, sample: DataSample) -> CheckResult:
        """Single-sample check — always returns score 0.0.

        GraCeFul requires a population for clustering.  Use ``check_batch``
        for meaningful detection.
        """
        return CheckResult(
            checker_name=self.name,
            risk_type=self.risk_type,
            success=True,
            score=0.0,
            flagged=False,
            details={"note": "GraCeFul requires batch mode; single-sample check is a no-op."},
        )


    def check_batch(self, samples: List[DataSample]) -> List[CheckResult]:
        """Run the GraCeFul pipeline on a batch of samples.
        """
        base = dict(checker_name=self.name, risk_type=self.risk_type)

        if len(samples) < self.min_batch_size:
            return [
                CheckResult(
                    **base,
                    success=True,
                    score=0.0,
                    flagged=False,
                    details={"note": f"Batch too small ({len(samples)} < {self.min_batch_size}); skipped."},
                )
                for _ in samples
            ]

        try:
            dataset = self._build_dataset(samples)
            victim = self._load_victim()
            embeddings = self._encode(dataset, victim)
            pred_labels, cluster_sizes = self._clustering(embeddings)
            return self._build_results(samples, pred_labels, cluster_sizes)
        except Exception as e:
            self._log.warning(f"GraCeFulBackdoorDefender: batch check failed: {e}")
            return [
                CheckResult(**base, success=False, details={"error": str(e)})
                for _ in samples
            ]

    @staticmethod
    def _extract_response(sample: DataSample) -> str:
        if sample.response and sample.response.content:
            return sample.response.content
        if sample.chosen_response and sample.chosen_response.content:
            return sample.chosen_response.content
        for m in reversed(sample.messages):
            if m.role == "assistant":
                texts = m.get_text_parts()
                if texts:
                    return " ".join(texts)
        return ""

    @staticmethod
    def _extract_context(sample: DataSample) -> str:
        parts: List[str] = []
        for m in sample.messages:
            if m.role == "assistant":
                continue
            texts = m.get_text_parts()
            if texts:
                parts.append(f"[{m.role}] " + " ".join(texts))
        return "\n".join(parts)

    def _build_dataset(self, samples: List[DataSample]) -> List[tuple]:
        dataset: List[tuple] = []
        for s in samples:
            context = self._extract_context(s)
            target = self._extract_response(s)
            dataset.append((context, target, 0))
        return dataset

    def _load_victim(self) -> CasualLLMVictim:
        if self._victim is not None:
            return self._victim
        config = dict(self.victim_config)
        if self.device == "cuda":
            config["device"] = "gpu"
        elif self.device == "cpu":
            config["device"] = "cpu"
        self._victim = load_victim(config)
        return self._victim

    def _encode(self, dataset: List[tuple], model: CasualLLMVictim) -> np.ndarray:
        dataloader = get_casual_dataloader(dataset, batch_size=1, shuffle=False)
        dct_grads = self._compute_gradients(model, dataloader)
        embeddings = self._dimension_reduction(dct_grads, pca_rank=self.pca_rank)
        return embeddings

    def _compute_gradients(self, model: CasualLLMVictim, data_loader) -> torch.Tensor:
        model.train()
        grad_params = model.get_target_params(self.target_para)
        if not grad_params:
            raise ValueError(f"no parameter matches target_para='{self.target_para}'")
        for p in grad_params:
            p.requires_grad_(True)

        dct_grads = []
        for batch in data_loader:
            model.zero_grad()
            batch_inputs, batch_labels, attention_mask = model.process(batch)
            output = model.forward(inputs=batch_inputs, labels=batch_labels, attentionMask=attention_mask)
            loss = output.loss.float()
            grad = autograd.grad(
                loss,
                grad_params,
                allow_unused=True,
            )
            target_grad = grad[0].detach().float()
            dct_grad = dct_2d(target_grad)
            if "lm_head" in self.target_para:
                keep_h = max(1, int(dct_grad.shape[0] * self.dct_keep_ratio))
                keep_w = max(1, int(dct_grad.shape[1] * self.dct_keep_ratio))
                dct_grad = dct_grad[:keep_h, :keep_w]
            dct_grads.append(dct_grad.cpu().flatten())
        return torch.stack(dct_grads, dim=0)

    def _dimension_reduction(self, hidden_states: torch.Tensor, pca_rank: int = 32):
        _, _, v = torch.pca_lowrank(hidden_states, q=pca_rank, center=True)
        emb_pca = torch.matmul(hidden_states, v[:, :pca_rank])
        from sklearn.preprocessing import StandardScaler
        emb_std = StandardScaler().fit_transform(emb_pca)
        return emb_std

    # Step 3 — clustering
    @staticmethod
    def _clustering(embeddings: np.ndarray):
        """AgglomerativeClustering with 2 clusters, cosine metric, average linkage.

        Returns:
            pred_labels: ndarray of shape (n,), 0 = majority (clean), 1 = minority (suspicious).
            cluster_sizes: dict mapping original cluster id to count.
        """
        from sklearn.cluster import AgglomerativeClustering

        clustering = AgglomerativeClustering(
            n_clusters=2,
            metric="cosine",
            linkage="average",
        )
        raw_labels = clustering.fit_predict(embeddings)
        raw_labels = np.asarray(raw_labels)

        unique, counts = np.unique(raw_labels, return_counts=True)
        label_counts = dict(zip(unique.tolist(), counts.tolist()))
        majority = max(label_counts, key=label_counts.get)

        # 0 = majority (presumed clean), 1 = minority (presumed poisoned)
        pred_labels = np.where(raw_labels == majority, 0, 1)
        return pred_labels, label_counts

    
    # Step 4 — build results
    def _build_results(
        self,
        samples: List[DataSample],
        pred_labels: np.ndarray,
        cluster_sizes: Dict[int, int],
    ) -> List[CheckResult]:
        """Convert clustering output to CheckResult list."""
        minority_count = int(np.sum(pred_labels == 1))
        total = len(samples)
        minority_ratio = round(minority_count / total, 4) if total else 0.0

        results: List[CheckResult] = []
        for i, _ in enumerate(samples):
            is_minority = bool(pred_labels[i] == 1)
            score = 1.0 if is_minority else 0.0
            results.append(
                CheckResult(
                    checker_name=self.name,
                    risk_type=self.risk_type,
                    success=True,
                    score=score,
                    flagged=is_minority and score >= self.threshold,
                    details={
                        "cluster": "minority" if is_minority else "majority",
                        "minority_ratio": minority_ratio,
                        "minority_count": minority_count,
                        "total_count": total,
                        "cluster_sizes": cluster_sizes,
                        "method": "GraCeFul",
                    },
                )
            )
        return results
