# DataElf Development Contract

## Dependency direction

```text
CLI/API -> discovery contracts/workflow -> DomainPlugin
                                  |       -> domain adapter/modeler/prompt/review
                                  -> Pi explorer
```

The discovery core must not import a domain connector, table builder, prompt, output parser, or semantic reviewer. Domain packages may import discovery contracts and common configuration types.

## Job boundary

All executions begin with a structured `JobSpec`. Natural language belongs in `objective`; execution-critical identifiers belong in `inputs` or `parameters`. Callers select `domain` explicitly. `dataelf discover` is the AI Index convenience entrypoint and constructs `domain=ai_index`.

## Domain boundary

`domain.yaml` is validated as a `DomainManifest` and points to one `DomainPlugin` factory. The plugin owns normalization, preparation, optional modeling, domain instructions, output declaration, semantic review, and result identifiers.

Core workspace creation is intentionally minimal. Domain-specific raw directories, tables, templates, and schemas are created by `DomainPlugin.prepare`.

## Artifact boundary

Every material stage output is an `ArtifactRef` with a workspace-relative path, role, producer stage, kind, and optional schema/checksum/provenance metadata. Paths outside the job workspace are invalid.

The explorer is successful only when its process exits successfully. Output success is decided separately by the generic `OutputContract` validator. Semantic quality is decided separately by the domain reviewer.

Do not pre-create required final outputs. Empty placeholder files can turn an incomplete run into a false success signal.

## Prompt boundary

The core composer owns runtime identity, workspace isolation, serialized `JobSpec`, prepared/modeling artifact inventory, output contract, and completion rules. A domain supplies only its evidence and analysis instructions. Pi does not branch on domain-specific Python types.

## Configuration boundary

The supported schema is:

```text
runtime
explorer.pi
domains.<domain>
env
```

Domain config is validated inside the domain package. Do not add domain fields to the core `DataElfConfig`, and do not introduce flat aliases for old configuration names.

## Adding a domain

1. Add `dataelf/domains/<name>/domain.yaml` and a plugin factory.
2. Define and validate domain config inside the package.
3. Implement preparation and return standard stage artifacts/context/environment.
4. Declare an output contract with workspace-relative paths.
5. Supply domain instructions and semantic review.
6. Add a test that runs the domain through `run_job` without editing discovery core modules.

Generic ontology or modeling abstractions should be extracted only after at least two domains demonstrate the same contract. Domain-specific code must remain within its domain until then.
