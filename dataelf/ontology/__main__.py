"""Run ontology over an explicit manifest of already collected local data."""
import argparse
from pathlib import Path

from .contracts import OntologyRequest
from .pipeline import run_ontology


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="JSON OntologyRequest; source paths are relative to workspace")
    args = parser.parse_args()
    request = OntologyRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
    result = run_ontology(request)
    print(result.model_dump_json(indent=2))
    raise SystemExit(0 if result.status == "completed" else 2)


if __name__ == "__main__":
    main()
