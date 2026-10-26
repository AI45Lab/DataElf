"""Common ontology modeling, independent of discovery domains."""
from .contracts import OntologyContract, OntologyTemplate, OntologyRequest, OntologyRunResult, SourceInput


def run_ontology(*args, **kwargs):
    from .pipeline import run_ontology as run
    return run(*args, **kwargs)


__all__ = ["OntologyContract", "OntologyTemplate", "OntologyRequest", "OntologyRunResult", "SourceInput", "run_ontology"]
