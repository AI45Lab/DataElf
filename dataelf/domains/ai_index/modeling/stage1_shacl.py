from __future__ import annotations

from typing import Any


def _iri(value: str) -> str:
    if not value or any(character in value for character in "<>\n\r"):
        raise ValueError("ontology namespace is not safe for Turtle serialization")
    return f"<{value}>"


def build_shacl_ttl(ontology: dict[str, Any]) -> str:
    """Materialize the controller-owned minimal executable RDF quality contract."""

    metadata = ontology.get("metadata") if isinstance(ontology.get("metadata"), dict) else {}
    namespace = str(metadata.get("namespace", ""))
    prefix = (
        f"@prefix de: <{namespace}> .\n"
        "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n\n"
    )
    # _iri performs a strict lexical guard even though the compact prefix is
    # used below. This keeps future configuration changes from emitting broken
    # Turtle silently.
    _iri(namespace)
    body = '''de:PaperShape a sh:NodeShape ;
  sh:targetClass de:Paper ;
  sh:property [ sh:path de:paperId ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
  sh:sparql [
    sh:message "Every authoredBy shortcut must have a matching reified Authorship." ;
    sh:select """SELECT $this ?scholar WHERE {
      $this de:authoredBy ?scholar .
      FILTER NOT EXISTS {
        $this de:hasAuthorship ?authorship .
        ?authorship de:authorshipOfPaper $this ; de:authoredByScholar ?scholar .
      }
    }"""
  ] .

de:ScholarShape a sh:NodeShape ;
  sh:targetClass de:Scholar ;
  sh:property [ sh:path de:scholarId ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] .

de:InstitutionShape a sh:NodeShape ;
  sh:targetClass de:Institution ;
  sh:property [ sh:path de:institutionId ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] .

de:EntityObservationShape a sh:NodeShape ;
  sh:targetClass de:EntityObservation ;
  sh:property [ sh:path de:observesEntity ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:DomainEntity ] ;
  sh:property [ sh:path de:observationFromRecord ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:SourceRecord ] ;
  sh:property [ sh:path de:observationInResponse ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:SearchResponse ] ;
  sh:property [ sh:path de:resultRank ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:positiveInteger ; sh:minInclusive 1 ] .

de:SourceDocumentShape a sh:NodeShape ;
  sh:targetClass de:SourceDocument ;
  sh:property [ sh:path de:sourceSystem ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
  sh:property [ sh:path de:sourcePath ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
  sh:property [ sh:path de:sourceSha256 ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ; sh:pattern "^[0-9a-f]{64}$" ] .

de:SearchResponseShape a sh:NodeShape ;
  sh:targetClass de:SearchResponse ;
  sh:property [ sh:path de:responseFromDocument ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:SourceDocument ] ;
  sh:property [ sh:path de:resultCount ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:integer ; sh:minInclusive 0 ] .

de:SourceRecordShape a sh:NodeShape ;
  sh:targetClass de:SourceRecord ;
  sh:property [ sh:path de:recordFromDocument ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:SourceDocument ] ;
  sh:property [ sh:path de:recordJsonPointer ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
  sh:property [ sh:path de:recordHash ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ; sh:pattern "^[0-9a-f]{64}$" ] .

de:SourceFragmentShape a sh:NodeShape ;
  sh:targetClass de:SourceFragment ;
  sh:property [ sh:path de:fragmentFromDocument ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:SourceDocument ] ;
  sh:property [ sh:path de:jsonPointer ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
  sh:property [ sh:path de:fragmentValueKind ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
  sh:property [ sh:path de:fragmentValueHash ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ; sh:pattern "^[0-9a-f]{64}$" ] .

de:AuthorshipShape a sh:NodeShape ;
  sh:targetClass de:Authorship ;
  sh:property [ sh:path de:authorshipOfPaper ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:Paper ] ;
  sh:property [ sh:path de:authoredByScholar ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:Scholar ] ;
  sh:property [ sh:path de:authorshipFromObservation ; sh:minCount 1 ; sh:maxCount 1 ; sh:class de:PaperObservation ] ;
  sh:property [ sh:path de:authorOrder ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:positiveInteger ; sh:minInclusive 1 ] ;
  sh:property [ sh:path de:isFirstAuthor ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:boolean ] ;
  sh:sparql [
    sh:message "Every reified Authorship must have the matching authoredBy shortcut." ;
    sh:select """SELECT $this ?paper ?scholar WHERE {
      $this de:authorshipOfPaper ?paper ; de:authoredByScholar ?scholar .
      FILTER NOT EXISTS { ?paper de:authoredBy ?scholar }
    }"""
  ] .

de:NewsItemShape a sh:NodeShape ;
  sh:targetClass de:NewsItem ;
  sh:property [ sh:path de:newsTitle ; sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
  sh:property [ sh:path de:newsDate ; sh:maxCount 1 ; sh:datatype xsd:date ] ;
  sh:property [ sh:path de:newsSource ; sh:maxCount 1 ; sh:datatype xsd:string ] .
'''
    return prefix + body


def shacl_contract_errors(ontology: dict[str, Any], payload: str) -> list[str]:
    expected = build_shacl_ttl(ontology)
    if payload != expected:
        return ["shacl.ttl differs from the deterministic Stage 1 contract"]
    required_tokens = (
        "de:EntityObservationShape", "de:AuthorshipShape", "de:SourceFragmentShape",
        "sh:minCount 1", "sh:maxCount 1", "xsd:positiveInteger",
        "de:authoredBy", "de:hasAuthorship", "de:sourceSystem",
    )
    errors = [f"shacl.ttl is missing {token}" for token in required_tokens if token not in payload]
    try:
        from rdflib import Graph
    except ImportError:
        return errors
    try:
        Graph().parse(data=payload, format="turtle")
    except Exception as exc:  # rdflib exposes several parser exception classes across versions.
        errors.append(f"shacl.ttl is not valid Turtle: {exc}")
    return errors


__all__ = ["build_shacl_ttl", "shacl_contract_errors"]
