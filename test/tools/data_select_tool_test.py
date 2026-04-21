import tempfile
from pathlib import Path

import numpy as np
import pytest
from tools import DataSelectTool, ToolContext
from tools.select.selection.embeddings import format_sample


class MockLogger:
    def __init__(self):
        self.logs = []

    def info(self, msg):
        self.logs.append(("info", msg))

    def error(self, msg):
        self.logs.append(("error", msg))

    def warning(self, msg):
        self.logs.append(("warning", msg))


class TestDataSelectTool:
    def test_name(self):
        tool = DataSelectTool()
        assert tool.name == "data_select"

    def test_parameters(self):
        tool = DataSelectTool()
        params = tool.parameters

        assert params["type"] == "object"
        assert "data" in params["properties"]
        assert "budget" in params["properties"]
        assert "n_clusters" in params["properties"]
        assert "embeddings_path" in params["properties"]
        assert params["required"] == ["data"]

    def test_run_uses_tool_defaults_budget(self, monkeypatch):
        tool = DataSelectTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={"tool_defaults": {"data_select": {"budget": 1, "n_clusters": 2}}},
        )
        test_data = [
            {"instruction": "a", "input": "", "output": "x", "score": 4.0},
            {"instruction": "b", "input": "", "output": "y", "score": 3.0},
        ]
        fake_embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

        monkeypatch.setattr(tool, "_load_or_extract_embeddings", lambda context, valid_data, kwargs: fake_embeddings)

        captured = {}

        def fake_diverse_select(**kwargs):
            captured.update(kwargs)
            return [dict(kwargs["data"][0])]

        monkeypatch.setattr("tools.select.data_select_tool.diverse_select", fake_diverse_select)

        result = tool.run(context, data=test_data)

        assert result["metadata"]["budget"] == 1
        assert result["metadata"]["selected_count"] == 1
        assert captured["budget"] == 1
        assert captured["n_clusters"] == 2

    def test_run_requires_budget(self):
        tool = DataSelectTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})

        with pytest.raises(ValueError, match="budget"):
            tool.run(context, data=[])

    def test_run_returns_empty_when_no_valid_scores(self):
        tool = DataSelectTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={"tool_defaults": {"data_select": {"budget": 2}}},
        )

        result = tool.run(
            context,
            data=[
                {"instruction": "a", "input": "", "output": "x", "score": -1},
                {"instruction": "b", "input": "", "output": "y", "score": -1},
            ],
        )

        assert result["result"] == []
        assert result["metadata"]["error"] == "no valid scores"

    def test_load_embeddings_aligns_valid_indices(self):
        tool = DataSelectTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})
        valid_data = [{"_idx": 1}, {"_idx": 3}]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "emb.npy"
            np.save(path, np.array([[0.0], [1.0], [2.0], [3.0]], dtype=np.float32))

            embeddings = tool._load_embeddings(context, str(path), valid_data)

        assert embeddings.shape == (2, 1)
        assert embeddings[:, 0].tolist() == [1.0, 3.0]

    def test_load_or_extract_prefers_existing_embeddings_path(self, monkeypatch):
        tool = DataSelectTool()
        logger = MockLogger()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "emb.npy"
            np.save(path, np.array([[1.0, 2.0]], dtype=np.float32))
            context = ToolContext(
                job_id="test_job",
                logger=logger,
                config={"tool_defaults": {"data_select": {"embeddings_path": str(path)}}},
            )

            monkeypatch.setattr(tool, "_load_embeddings", lambda context, path, valid_data: np.array([[9.0, 9.0]], dtype=np.float32))

            embeddings = tool._load_or_extract_embeddings(context, [{"_idx": 0, "score": 1.0}], {})

        assert embeddings.shape == (1, 2)
        assert embeddings.tolist() == [[9.0, 9.0]]

    def test_load_or_extract_uses_embedding_model(self, monkeypatch):
        tool = DataSelectTool()
        logger = MockLogger()
        context = ToolContext(
            job_id="test_job",
            logger=logger,
            config={"tool_defaults": {"data_select": {"embedding_model": "model-a"}}},
        )

        monkeypatch.setattr(tool, "_extract_embeddings", lambda context, valid_data, model_path: np.array([[3.0, 4.0]], dtype=np.float32))

        embeddings = tool._load_or_extract_embeddings(context, [{"_idx": 0, "score": 1.0}], {})

        assert embeddings.tolist() == [[3.0, 4.0]]

    def test_load_or_extract_raises_without_embeddings_source(self):
        tool = DataSelectTool()
        logger = MockLogger()
        context = ToolContext(job_id="test_job", logger=logger, config={})

        with pytest.raises(ValueError, match="No embeddings available"):
            tool._load_or_extract_embeddings(context, [{"_idx": 0, "score": 1.0}], {})

    def test_run_saves_output_when_output_dir_configured(self, monkeypatch):
        tool = DataSelectTool()
        logger = MockLogger()

        with tempfile.TemporaryDirectory() as tmpdir:
            context = ToolContext(
                job_id="test_job",
                logger=logger,
                config={"tool_defaults": {"data_select": {"budget": 1, "output_dir": tmpdir}}},
            )
            test_data = [{"instruction": "a", "input": "", "output": "x", "score": 5.0}]

            monkeypatch.setattr(tool, "_load_or_extract_embeddings", lambda context, valid_data, kwargs: np.array([[1.0, 0.0]], dtype=np.float32))
            monkeypatch.setattr("tools.select.data_select_tool.diverse_select", lambda **kwargs: [dict(kwargs["data"][0])])

            result = tool.run(context, data=test_data)
            output_path = Path(tmpdir) / "diverse_selected.json"

            assert result["metadata"]["selected_count"] == 1
            assert output_path.exists()


class TestSampleFormatting:
    def test_format_sample_coerces_non_string_fields(self):
        text = format_sample({"instruction": True, "input": False, "output": 123})

        assert "### Instruction:\nTrue" in text
        assert "### Input:\nFalse" in text
        assert "### Response:\n123" in text
