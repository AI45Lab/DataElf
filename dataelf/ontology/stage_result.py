"""Discovery's scalar counters and ontology's complete structured accounting."""
from copy import deepcopy


def stage_accounting(metrics):
    full = deepcopy(metrics or {})
    return ({key: value for key, value in full.items()
             if isinstance(value, (str, int, float, bool))},
            {"ontology_metrics": full})
