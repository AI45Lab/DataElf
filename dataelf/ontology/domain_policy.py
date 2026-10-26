"""Domain isolation shared by contracts, legacy templates and trusted adapters."""

class TemplateDomainError(ValueError):
    code = "ONTOLOGY_TEMPLATE_DOMAIN_MISMATCH"


def require_domain(actual: str, expected: str):
    if actual != expected:
        raise TemplateDomainError(f"ontology template/contract domain mismatch: expected {expected!r}, got {actual!r}")
