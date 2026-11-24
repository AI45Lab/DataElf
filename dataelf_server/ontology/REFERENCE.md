# Scope V2 service compatibility

`scope_v2_template.py` and `templates/scope_v2/` retain the deterministic implementation and resources from DataElf commit `78f2ef4b628dc7e65e42b6d69b68d6441288f766`.

The only import relocation is the result type and stage failure constant from `dataelf/domains/ai_index/modeling/contracts.py` to the service-local `contracts.py`. The result fields, artifact layout, RDF identity construction, mappings and validation retain the committed reference behavior. Original source headers remain intact. No separate license file was present in that reference tree; this relocation does not add a new license.

The reference repository's uncommitted streaming adapter is a separate experimental change. It is not part of this baseline and does not establish real-service compatibility.

This service implementation does not call `dataelf.ontology`, create a research GraphService revision, select a CLI policy or start a modeling/review LLM session. Schema/domain/observation/source dataset semantics remain those of the fixed service runner.
