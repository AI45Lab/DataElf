from __future__ import annotations

import json
from pathlib import Path

from rdflib import Dataset, RDF, RDFS, URIRef
from rdflib.namespace import XSD

from dataelf_server.ontology.scope_v2_template import (
    ScopeV2TemplateOntologyRunner,
    compose_scope_v2_templates,
)
from dataelf_server.backups.rule_intent.parser import parse_scope


def test_intent_sources_automatically_select_template_fragments() -> None:
    comprehensive = parse_scope("生成综合总结")
    comprehensive_sources = [call.source for call in comprehensive.calls]
    template = compose_scope_v2_templates(comprehensive_sources)
    assert template.templates == (
        "scope_v2_base",
        "scope_v2_news",
        "scope_v2_twitter",
        "scope_v2_github",
        "scope_v2_huggingface",
        "scope_v2_youtube",
    )

    open_source = parse_scope("生成开源社区模块总结")
    open_source_template = compose_scope_v2_templates(
        call.source for call in open_source.calls
    )
    assert open_source_template.templates == (
        "scope_v2_base",
        "scope_v2_github",
        "scope_v2_huggingface",
    )


def test_every_source_fragment_can_be_composed_together() -> None:
    template = compose_scope_v2_templates(
        ["news", "twitter", "github", "huggingface", "youtube"]
    )
    assert len(template.templates) == 6
    assert {fragment["source"] for fragment in template.fragments.values()} == {
        "news",
        "twitter",
        "github",
        "huggingface",
        "youtube",
    }
    assert all(fragment["approval"]["decision"] == "approve" for fragment in template.fragments.values())
    assert template.classes["Video"]["subClassOf"] == "DomainEntity"
    assert template.classes["VideoObservation"]["subClassOf"] == "EntityObservation"
    assert template.datatype_properties["title"]["domain"] == "DomainEntity"
    assert template.datatype_properties["publishedAt"]["domain"] == "DomainEntity"
    assert template.datatype_properties["publishedAt"]["range"] == "xsd:dateTime"
    assert template.datatype_properties["likeCount"]["domain"] == "EntityObservation"
    assert "domain" not in template.datatype_properties["tagLabel"]


def test_scope_v2_template_runner_materializes_valid_rdf(tmp_path: Path) -> None:
    run_dir = tmp_path / "scope_v2" / "run_test"
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True)
    items = {
        "news": {
            "news_id": "news-1",
            "title": "New model release",
            "url": "https://example.com/news",
            "date": "2026-08-27",
            "text": "A model was released.",
            "institution_infos": [{"institution_id": "org-1", "name": "Example AI"}],
            "persons": ["Alice"],
            "domains": ["AI"],
            "products": ["Model X"],
            "funding_info": [],
        },
        "twitter": {
            "tweet_post_id": "tweet-1",
            "blogger_name": "alice",
            "url": "https://x.example/tweet-1",
            "published_at": "2026-08-27T09:00:00+08:00",
            "text": "A technical opinion.",
            "view_count": 10,
            "like_count": 2,
            "institution_infos": [{"institution_id": "org-1", "name": "Example AI"}],
            "strategic_tag_ids": ["llm"],
        },
        "youtube": {
            "video_id": "video-1",
            "blogger_info": {
                "blogger_url": "https://youtube.example/c",
                "blogger_name": "Channel",
                "fans_num": 1234,
                "blogger_introduction": "Channel introduction",
            },
            "video_info": {
                "url": "https://youtube.example/v/1",
                "title": "Model discussion",
                "publishtime": "2026-08-27",
                "view_count": 20,
                "like_count": 3,
                "introduction": "Detailed chapters",
                "text": "Transcript",
                "publisher": "Channel",
                "cover_url": "https://youtube.example/cover.jpg",
            },
            "core_ideology": {
                "main_theme": "Model efficiency",
                "key_conclusions": ["Faster", "Cheaper"],
                "tech_concept_or_theory": "Inference scaling",
            },
            "institution_infos": [{"institution_id": "org-1", "name": "Example AI"}],
            "strategic_tag_ids": ["llm"],
            "domains": ["AI"],
        },
    }
    sources = {}
    for source, record in items.items():
        raw_path = raw_dir / f"{source}_page_1.json"
        raw_path.write_text(json.dumps({"data": {"list": [record]}}), encoding="utf-8")
        title = record.get("title") or record.get("text") or record.get("video_info", {}).get("title")
        url = record.get("url") or record.get("video_info", {}).get("url")
        published = record.get("date") or record.get("published_at") or record.get("video_info", {}).get("publishtime")
        sources[source] = {
            "source": source,
            "endpoint": "unused-by-runner",
            "kept_count": 1,
            "raw_files": [f"raw/{source}_page_1.json"],
            "items": [{
                "source": source,
                "source_id": f"{source}:1",
                "title": title,
                "url": url,
                "published_at": published,
                "raw_ref": f"raw/{source}_page_1.json",
                "data": record,
            }],
        }
    (run_dir / "result.json").write_text(
        json.dumps({"status": "completed", "sources": sources}), encoding="utf-8"
    )

    result = ScopeV2TemplateOntologyRunner().run(tmp_path)

    assert result.status == "completed"
    assert Path(result.nquads_path).is_file()  # fixed builder owns its validation contract
    assert result.details["stage1Mode"] == "composed_template"
    assert result.details["modelCalls"] == 0
    assert result.details["templateIds"] == [
        "scope_v2_base",
        "scope_v2_news",
        "scope_v2_twitter",
        "scope_v2_youtube",
    ]
    dataset = Dataset()
    dataset.parse(result.nquads_path, format="nquads")
    assert len(dataset) > 0
    assert Path(result.rdfxml_path).stat().st_size > 0
    assert json.loads(Path(result.validation_path).read_text())["status"] == "valid"

    namespace = "urn:dataelf:ontology:ai-index:"
    ns = lambda value: URIRef(namespace + value)
    quads = list(dataset.quads((None, None, None, None)))
    video = next(
        subject for subject, predicate, obj, _ in quads
        if predicate == RDF.type and obj == ns("Video")
    )
    values = lambda subject, predicate: [
        obj for current, prop, obj, _ in quads if current == subject and prop == predicate
    ]
    assert [str(value) for value in values(video, ns("videoIntroduction"))] == ["Detailed chapters"]
    assert [str(value) for value in values(video, ns("videoTranscript"))] == ["Transcript"]
    assert [str(value) for value in values(video, ns("videoPublisher"))] == ["Channel"]
    assert len(values(video, ns("publishedAt"))) == 1
    assert values(video, ns("publishedAt"))[0].datatype == XSD.dateTime

    observation = next(
        subject for subject, predicate, obj, _ in quads
        if predicate == RDF.type and obj == ns("VideoObservation")
    )
    assert [int(value) for value in values(observation, ns("channelFollowerCount"))] == [1234]
    assert [int(value) for value in values(observation, ns("videoViewCount"))] == [20]

    blogger = next(
        subject for subject, predicate, obj, _ in quads
        if predicate == RDF.type and obj == ns("Blogger")
    )
    assert [str(value) for value in values(blogger, ns("bloggerIntroduction"))] == ["Channel introduction"]
    ideology = next(
        subject for subject, predicate, obj, _ in quads
        if predicate == RDF.type and obj == ns("Ideology")
    )
    assert {str(value) for value in values(ideology, ns("ideologyConclusion"))} == {"Faster", "Cheaper"}
    assert [str(value) for value in values(ideology, ns("ideologyTheory"))] == ["Inference scaling"]

    records = {
        subject for subject, predicate, obj, _ in quads
        if predicate == RDF.type and obj == ns("SourceRecord")
    }
    assert len(records) == 3
    assert all(str(values(record, ns("recordJsonPointer"))[0]) == "/data/list/0" for record in records)
    documents = {
        subject for subject, predicate, obj, _ in quads
        if predicate == RDF.type and obj == ns("SourceDocument")
    }
    assert all(not Path(str(values(document, ns("sourcePath"))[0])).is_absolute() for document in documents)
