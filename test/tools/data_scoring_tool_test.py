import pytest
from tools import DataScoringTool, ToolContext
from tools.scoring.scorers.oda_external import ODAScorer
from tools.scoring.scorers.ask_llm import AskLlmScorer


class MockLogger:
    def __init__(self):
        self.logs = []

    def info(self, msg):
        self.logs.append(("info", msg))

    def error(self, msg):
        self.logs.append(("error", msg))

    def warning(self, msg):
        self.logs.append(("warning", msg))


class TestDataScoringTool:
    def test_name(self):
        tool = DataScoringTool()
        assert tool.name == "data_scoring"

    def test_parameters(self):
        tool = DataScoringTool()
        params = tool.parameters

        assert params["type"] == "object"
        assert "data" in params["properties"]
        assert "scorer" in params["properties"]
        assert "model" in params["properties"]
        assert "batch_size" in params["properties"]
        assert "output_dir" in params["properties"]
        assert params["required"] == ["data"]

    def test_run_llm_judge(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})

        test_data = [
            {"instruction": "a", "input": "", "output": "x"},
            {"instruction": "b", "input": "", "output": "y"},
        ]

        def fake_run_llm_judge(context, data, kwargs):
            assert data == test_data
            assert kwargs["scorer"] == "llm_judge"
            return [
                {"score": 4.5, "review": "good"},
                {"score": 2.0, "review": "ok"},
            ]

        monkeypatch.setattr(tool, "_run_llm_judge", fake_run_llm_judge)

        result = tool.run(context, data=test_data, scorer="llm_judge")

        assert len(result["result"]) == 2
        assert result["result"][0]["score"] == 4.5
        assert result["result"][0]["review"] == "good"
        assert result["result"][1]["score"] == 2.0
        assert result["metadata"]["scorer"] == "llm_judge"
        assert result["metadata"]["records_count"] == 2
        assert result["metadata"]["valid_count"] == 2
        assert result["metadata"]["invalid_count"] == 0
        assert result["metadata"]["average_score"] == 3.25
        assert "Data Scoring Report" in result["artifacts"]["report_md"]

    def test_run_uses_default_ask_llm(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})
        test_data = [{"instruction": "a", "input": "", "output": "x"}]

        def fake_run_ask_llm(context, data, kwargs):
            assert "scorer" not in kwargs
            return [{"score": 3.0}]

        monkeypatch.setattr(tool, "_run_ask_llm", fake_run_ask_llm)

        result = tool.run(context, data=test_data)

        assert result["metadata"]["scorer"] == "ask_llm"
        assert result["metadata"]["average_score"] == 3.0

    def test_run_uses_tool_defaults(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={"tool_defaults": {"data_scoring": {"scorer": "oda_ask_llm"}}},
        )
        test_data = [{"instruction": "a", "input": "", "output": "x"}]

        def fake_run_oda_scorer(context, data, kwargs):
            assert "scorer" not in kwargs
            return [{"score": 5.0}]

        monkeypatch.setattr(tool, "_run_oda_scorer", fake_run_oda_scorer)

        result = tool.run(context, data=test_data)

        assert result["metadata"]["scorer"] == "oda_ask_llm"
        assert result["metadata"]["average_score"] == 5.0

    def test_run_ask_llm(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})

        test_data = [
            {"instruction": "a", "input": "", "output": "x"},
        ]

        def fake_run_ask_llm(context, data, kwargs):
            assert data == test_data
            assert kwargs["scorer"] == "ask_llm"
            return [{"score": 4.0}]

        monkeypatch.setattr(tool, "_run_ask_llm", fake_run_ask_llm)

        result = tool.run(context, data=test_data, scorer="ask_llm")

        assert result["result"][0]["score"] == 4.0
        assert result["metadata"]["scorer"] == "ask_llm"
        assert result["metadata"]["records_count"] == 1
        assert result["metadata"]["average_score"] == 4.0

    def test_run_ask_llm_requires_model(self):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={"tool_defaults": {"data_scoring": {}}},
        )

        with pytest.raises(ValueError, match="ask_llm model"):
            tool.run(
                context,
                data=[{"instruction": "a", "input": "", "output": "x"}],
                scorer="ask_llm",
            )

    def test_run_ask_llm_reads_oda_model_fallback(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={
                "tool_defaults": {
                    "data_scoring": {
                        "oda": {
                            "model": "/path/to/model",
                            "batch_size": 16,
                        }
                    }
                }
            },
        )

        captured = {}

        class DummyScorer:
            async def score(self, data, output_dir):
                return [{"score": 2.5}]

        def fake_ctor(**kwargs):
            captured.update(kwargs)
            return DummyScorer()

        monkeypatch.setattr("tools.scoring.data_scoring_tool.AskLlmScorer", fake_ctor)

        result = tool.run(
            context,
            data=[{"instruction": "a", "input": "", "output": "x"}],
            scorer="ask_llm",
        )

        assert captured["model"] == "/path/to/model"
        assert captured["batch_size"] == 16
        assert result["metadata"]["scorer"] == "ask_llm"

    def test_run_oda_ask_llm(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})

        test_data = [
            {"instruction": "a", "input": "", "output": "x"},
        ]

        def fake_run_oda_scorer(context, data, kwargs):
            assert data == test_data
            assert kwargs["scorer"] == "oda_ask_llm"
            return [{"score": 5.0}]

        monkeypatch.setattr(tool, "_run_oda_scorer", fake_run_oda_scorer)

        result = tool.run(context, data=test_data, scorer="oda_ask_llm")

        assert result["result"][0]["score"] == 5.0
        assert result["metadata"]["scorer"] == "oda_ask_llm"
        assert result["metadata"]["records_count"] == 1
        assert result["metadata"]["average_score"] == 5.0

    def test_run_oda_ask_llm_passes_oda_root(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={
                "tool_defaults": {
                    "data_scoring": {
                        "oda": {
                            "oda_root": "/tmp/oda-root",
                            "model": "test-model",
                            "batch_size": 2,
                        }
                    }
                }
            },
        )

        captured = {}

        class DummyScorer:
            async def score(self, data, output_dir):
                captured["data"] = data
                captured["output_dir"] = output_dir
                return [{"score": 1.0}]

        def fake_ctor(**kwargs):
            captured.update(kwargs)
            return DummyScorer()

        monkeypatch.setattr("tools.scoring.data_scoring_tool.ODAScorer", fake_ctor)

        result = tool.run(context, data=[{"instruction": "a", "input": "", "output": "x"}], scorer="oda_ask_llm")

        assert captured["oda_root"] == "/tmp/oda-root"
        assert captured["model"] == "test-model"
        assert captured["batch_size"] == 2
        assert result["metadata"]["scorer"] == "oda_ask_llm"

    def test_run_oda_ask_llm_requires_oda_root(self):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={
                "tool_defaults": {
                    "data_scoring": {
                        "oda": {
                            "model": "test-model",
                            "batch_size": 2,
                        }
                    }
                }
            },
        )

        with pytest.raises(ValueError, match="oda_root"):
            tool.run(context, data=[{"instruction": "a", "input": "", "output": "x"}], scorer="oda_ask_llm")

    def test_oda_convert_results_normalizes_scores(self, tmp_path):
        raw_output = tmp_path / "scores.jsonl"
        raw_output.write_text('{"id": 0, "score": -2.0}\n{"id": 1, "score": -1.0}\n', encoding="utf-8")

        results = ODAScorer._convert_results([{"a": 1}, {"a": 2}], raw_output)

        assert results == [{"score": 0.0}, {"score": 5.0}]

    def test_oda_convert_results_maps_missing_scores_to_invalid(self, tmp_path):
        raw_output = tmp_path / "scores.jsonl"
        raw_output.write_text('{"id": 0, "score": -100.0}\n', encoding="utf-8")

        results = ODAScorer._convert_results([{"a": 1}, {"a": 2}], raw_output)

        assert results == [{"score": -1.0}, {"score": -1.0}]

    def test_oda_write_jsonl_sanitizes_non_string_fields(self, tmp_path):
        path = tmp_path / "input.jsonl"

        ODAScorer._write_jsonl(
            [{"instruction": True, "input": False, "output": 123, "extra": "ok"}],
            path,
        )

        content = path.read_text(encoding="utf-8").strip()
        assert '"instruction": "True"' in content
        assert '"input": "False"' in content
        assert '"output": "123"' in content
        assert '"id": 0' in content

    def test_ask_llm_convert_results_normalizes_scores(self, tmp_path):
        raw_output = tmp_path / "scores.jsonl"
        raw_output.write_text('{"id": 0, "score": -2.0}\n{"id": 1, "score": -1.0}\n', encoding="utf-8")

        results = AskLlmScorer._convert_results([{"a": 1}, {"a": 2}], raw_output)

        assert results == [{"score": 0.0}, {"score": 5.0}]

    def test_ask_llm_write_jsonl_sanitizes_non_string_fields(self, tmp_path):
        path = tmp_path / "input.jsonl"

        AskLlmScorer._write_jsonl(
            [{"instruction": True, "input": False, "output": 123, "extra": "ok"}],
            path,
        )

        content = path.read_text(encoding="utf-8").strip()
        assert '"instruction": "True"' in content
        assert '"input": "False"' in content
        assert '"output": "123"' in content
        assert '"id": 0' in content

    def test_run_with_invalid_scores(self, monkeypatch):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})

        test_data = [
            {"instruction": "a", "input": "", "output": "x"},
            {"instruction": "b", "input": "", "output": "y"},
        ]

        def fake_run_ask_llm(context, data, kwargs):
            return [
                {"score": 4.0},
                {"score": -1},
            ]

        monkeypatch.setattr(tool, "_run_ask_llm", fake_run_ask_llm)

        result = tool.run(context, data=test_data, scorer="ask_llm")

        assert result["metadata"]["valid_count"] == 1
        assert result["metadata"]["invalid_count"] == 1
        assert result["metadata"]["average_score"] == 4.0

    def test_run_with_unknown_scorer(self):
        tool = DataScoringTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})

        with pytest.raises(ValueError, match="Unknown scorer type"):
            tool.run(context, data=[], scorer="unknown")

    def test_merge_scores(self):
        original = [
            {"id": 1, "instruction": "a"},
            {"id": 2, "instruction": "b"},
            {"id": 3, "instruction": "c"},
        ]
        scored = [
            {"score": 4.2, "review": "good"},
            {"score": 1.5},
        ]

        result = DataScoringTool._merge_scores(original, scored)

        assert result[0]["score"] == 4.2
        assert result[0]["review"] == "good"
        assert result[1]["score"] == 1.5
        assert result[2]["score"] == -1
