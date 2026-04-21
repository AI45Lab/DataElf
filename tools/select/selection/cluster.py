from __future__ import annotations

import logging
import time

import numpy as np
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import normalize

try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _gib(num_bytes: int | float) -> str:
    return f"{num_bytes / (1024 ** 3):.2f} GiB"


def _stage(msg: str) -> float:
    logger.info("[Stage] %s", msg)
    return time.perf_counter()


def _done(t0: float, msg: str) -> None:
    logger.info("[Done] %s (%.2fs)", msg, time.perf_counter() - t0)


def _kmeans_sklearn(
    embeddings: np.ndarray,
    n_clusters: int,
    batch_size: int = 10000,
    random_state: int = 42,
    n_init: int = 3,
    max_iter: int = 100,
    verbose: int = 0,
) -> np.ndarray:
    kmeans = MiniBatchKMeans(
        n_clusters=n_clusters,
        batch_size=batch_size,
        random_state=random_state,
        n_init=n_init,
        max_iter=max_iter,
        verbose=verbose,
    )
    labels = kmeans.fit_predict(embeddings)
    return labels


def _kmeans_torch(
    embeddings: np.ndarray,
    n_clusters: int,
    batch_size: int = 10000,
    random_state: int = 42,
    max_iter: int = 100,
    verbose: int = 0,
    device: str = "cuda",
    tol: float = 1e-4,
) -> np.ndarray:
    if torch is None:
        raise ImportError("Torch backend requested but PyTorch is not installed.")
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"Torch backend device={device} but CUDA not available.")

    torch.set_grad_enabled(False)
    if device.startswith("cuda"):
        torch.backends.cuda.matmul.allow_tf32 = True

    n_samples = embeddings.shape[0]
    batch_size = min(batch_size, n_samples)
    generator = torch.Generator(device="cpu")
    generator.manual_seed(random_state)

    x = torch.from_numpy(np.asarray(embeddings, dtype=np.float32)).to(device)
    x_sq = (x * x).sum(dim=1, keepdim=True)
    init_idx = torch.randperm(n_samples, generator=generator)[:n_clusters].to(device)
    centers = x.index_select(0, init_idx).contiguous()

    for iteration in range(max_iter):
        prev = centers.clone()
        c_sq = (centers * centers).sum(dim=1).view(1, -1)
        sums = torch.zeros_like(centers)
        counts = torch.zeros(n_clusters, device=device, dtype=torch.long)

        for start in range(0, n_samples, batch_size):
            end = min(start + batch_size, n_samples)
            batch = x[start:end]
            dist = x_sq[start:end] + c_sq - 2.0 * (batch @ centers.T)
            labels = dist.argmin(dim=1)
            sums.index_add_(0, labels, batch)
            counts.index_add_(0, labels, torch.ones(labels.shape[0], device=device, dtype=torch.long))

        empty = counts == 0
        if empty.any():
            n_empty = int(empty.sum().item())
            refill = torch.randperm(n_samples, generator=generator)[:n_empty].to(device)
            sums[empty] = x.index_select(0, refill)
            counts[empty] = 1

        centers = sums / counts.unsqueeze(1).to(sums.dtype)
        shift = torch.norm(centers - prev, dim=1).mean().item()
        if verbose > 0 and ((iteration + 1) % 10 == 0 or shift <= tol):
            logger.info("iter %d/%d: mean_shift=%.6f", iteration + 1, max_iter, shift)
        if shift <= tol:
            break

    final = np.empty(n_samples, dtype=np.int32)
    c_sq = (centers * centers).sum(dim=1).view(1, -1)
    for start in range(0, n_samples, batch_size):
        end = min(start + batch_size, n_samples)
        dist = x_sq[start:end] + c_sq - 2.0 * (x[start:end] @ centers.T)
        final[start:end] = dist.argmin(dim=1).cpu().numpy()

    return final


def allocate_quota(
    cluster_sizes: np.ndarray,
    budget: int,
    strategy: str = "proportional",
    alpha: float = 0.5,
) -> np.ndarray:
    n_clusters = len(cluster_sizes)
    total = cluster_sizes.sum()

    if strategy == "proportional":
        raw = budget * (cluster_sizes / total)
    elif strategy == "uniform":
        raw = np.full(n_clusters, budget / n_clusters)
    elif strategy == "mixed":
        raw = alpha * np.full(n_clusters, budget / n_clusters) + (1 - alpha) * budget * (cluster_sizes / total)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    quotas = np.maximum(np.floor(raw).astype(int), 1)
    deficit = budget - quotas.sum()
    remainder = raw - quotas
    if deficit > 0:
        quotas[np.argsort(-remainder)[:deficit]] += 1
    elif deficit < 0:
        excess = -deficit
        for idx in np.argsort(remainder):
            if excess <= 0:
                break
            reduce = min(quotas[idx] - 1, excess)
            quotas[idx] -= reduce
            excess -= reduce
    return quotas


def post_dedup(
    embeddings: np.ndarray,
    selected_indices: list[int],
    scores: np.ndarray,
    threshold: float = 0.95,
) -> list[int]:
    sel_emb = normalize(embeddings[selected_indices])
    sim = sel_emb @ sel_emb.T

    to_remove: set[int] = set()
    for i in range(len(selected_indices)):
        if i in to_remove:
            continue
        for j in range(i + 1, len(selected_indices)):
            if j in to_remove:
                continue
            if sim[i, j] > threshold:
                idx_i, idx_j = selected_indices[i], selected_indices[j]
                victim = j if scores[idx_i] >= scores[idx_j] else i
                to_remove.add(victim)

    return [selected_indices[i] for i in range(len(selected_indices)) if i not in to_remove]


def diverse_select(
    embeddings: np.ndarray,
    data: list[dict],
    n_clusters: int,
    budget: int,
    strategy: str = "proportional",
    alpha: float = 0.5,
    score_key: str = "score",
    dedup: bool = False,
    dedup_threshold: float = 0.95,
    random_state: int = 42,
    kmeans_backend: str = "torch",
    kmeans_device: str = "cuda",
    kmeans_batch_size: int = 10000,
    kmeans_max_iter: int = 100,
    kmeans_n_init: int = 3,
    kmeans_verbose: int = 1,
) -> list[dict]:
    if len(embeddings) != len(data):
        raise ValueError(f"Length mismatch: embeddings={len(embeddings)}, data={len(data)}")

    n_samples = len(data)
    budget = min(budget, n_samples)

    max_k = max(n_samples // 10, 1)
    if n_clusters > max_k:
        logger.warning("n_clusters (%d) too large for N=%d, clamping to %d.", n_clusters, n_samples, max_k)
        n_clusters = max_k

    kmeans_batch_size = min(kmeans_batch_size, n_samples)

    t0 = _stage(f"Normalize embeddings {embeddings.shape} ({_gib(embeddings.nbytes)})")
    emb_norm = normalize(embeddings)
    _done(t0, "Normalization")

    t0 = _stage("K-means clustering")
    if kmeans_backend == "sklearn":
        labels = _kmeans_sklearn(emb_norm, n_clusters, kmeans_batch_size, random_state, kmeans_n_init, kmeans_max_iter, kmeans_verbose)
    elif kmeans_backend == "torch":
        labels = _kmeans_torch(emb_norm, n_clusters, kmeans_batch_size, random_state, kmeans_max_iter, kmeans_verbose, kmeans_device)
    else:
        raise ValueError(f"Unknown kmeans_backend: {kmeans_backend}")
    _done(t0, "K-means")

    scores = np.array([item[score_key] for item in data])
    cluster_sizes = np.bincount(labels, minlength=n_clusters)
    quotas = allocate_quota(cluster_sizes, budget, strategy, alpha)

    t0 = _stage("Within-cluster TopK selection")
    selected_indices: list[int] = []
    for cluster_id in range(n_clusters):
        members = np.where(labels == cluster_id)[0]
        if len(members) == 0:
            continue
        order = np.argsort(-scores[members])
        top_n = min(quotas[cluster_id], len(members))
        selected_indices.extend(members[order[:top_n]].tolist())
    _done(t0, "TopK selection")

    if dedup:
        t0 = _stage("Post-selection deduplication")
        selected_indices = post_dedup(embeddings, selected_indices, scores, dedup_threshold)
        _done(t0, "Dedup")

    selected = [dict(data[i]) for i in selected_indices]
    for item in selected:
        item.pop("_idx", None)
    return selected
