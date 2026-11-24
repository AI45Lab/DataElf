"""Machine-readable feedback shared by tools and the cross-stage repair loop."""
import json


class OntologyError(ValueError):
    def __init__(self, code, target, issues):
        self.feedback = {"code": code, "target": target, "issues": issues}
        super().__init__(json.dumps(self.feedback, ensure_ascii=False))


def error_feedback(exc, default_target):
    if isinstance(exc, OntologyError):
        return exc.feedback
    return {"target": default_target, "error": str(exc)}


def classify_model_error(errors,role):
    import re
    text='; '.join(errors)
    if re.search(r'context.{0,30}(length|window|limit)|maximum context|too many tokens',text,re.I):
        return OntologyError('ONTOLOGY_CONTEXT_FULL','control',[{'message':text,'recoverable':True,'role':role}])
    if re.search(r'connection error|ECONNRESET|fetch failed|socket hang up|stream ended without finish_reason|read timeout|connection closed|network error|terminated',text,re.I):
        return OntologyError('ONTOLOGY_TRANSPORT_INTERRUPTED','control',[{'message':text,'recoverable':True,'role':role}])
    return RuntimeError(f'Pi {role} model error: '+text)
