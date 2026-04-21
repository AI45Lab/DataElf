import logging

logger = logging.getLogger(__name__)


def parse_score(review: str) -> float:
    first_line = review.strip().split("\n")[0].strip()

    try:
        return float(first_line)
    except ValueError:
        pass

    review_lower = review.lower()
    for prefix in ("score:", "score :"):
        if prefix in review_lower:
            idx = review_lower.index(prefix)
            after = review[idx + len(prefix) :].strip()
            token = after.split()[0] if after.split() else ""
            try:
                return float(token)
            except ValueError:
                continue

    logger.warning("Could not parse score from review: %r", first_line)
    return -1.0
