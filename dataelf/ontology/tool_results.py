"""Host control receipts are separate from model/source payloads."""
VERSION = 'dataelf.tool-control.v1'


def control_event(kind, binding):
    if not binding:raise ValueError('Control receipts require a persisted object binding')
    return {'schema_version':VERSION,'kind':kind,'binding':binding}
