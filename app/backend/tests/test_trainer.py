"""Tests for TrainerManager — no subprocess execution required."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from trainer import TrainerManager, _count_lines

# ── _count_lines ───────────────────────────────────────────────────────────────

class TestCountLines:
    def test_counts_non_empty_lines(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text('{"a":1}\n{"b":2}\n\n{"c":3}\n')
        assert _count_lines(f) == 3

    def test_missing_file_returns_zero(self, tmp_path):
        assert _count_lines(tmp_path / "missing.jsonl") == 0

    def test_empty_file_returns_zero(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert _count_lines(f) == 0


# ── get_status ─────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_idle_when_no_status_file(self, tmp_path):
        with patch("trainer.STATUS_FILE", tmp_path / "status.json"):
            mgr = TrainerManager()
            assert mgr.get_status() == {"status": "idle"}

    def test_reads_existing_status_file(self, tmp_path):
        status_file = tmp_path / "status.json"
        status_file.write_text(json.dumps({"status": "training", "iter": 100}))
        with patch("trainer.STATUS_FILE", status_file):
            mgr = TrainerManager()
            result = mgr.get_status()
            assert result["status"] == "training"
            assert result["iter"] == 100

    def test_returns_idle_on_corrupt_status_file(self, tmp_path):
        status_file = tmp_path / "status.json"
        status_file.write_text("not json {{")
        with patch("trainer.STATUS_FILE", status_file):
            mgr = TrainerManager()
            assert mgr.get_status() == {"status": "idle"}


# ── get_log ────────────────────────────────────────────────────────────────────

class TestGetLog:
    def test_empty_when_no_log_file(self, tmp_path):
        with patch("trainer.LOG_FILE", tmp_path / "log.jsonl"):
            mgr = TrainerManager()
            assert mgr.get_log() == []

    def test_reads_log_lines(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        log_file.write_text(
            '{"iter": 1, "loss": 2.5}\n'
            '{"iter": 2, "loss": 2.3}\n'
            '\n'
            '{"iter": 3, "loss": 2.1}\n'
        )
        with patch("trainer.LOG_FILE", log_file):
            mgr = TrainerManager()
            logs = mgr.get_log()
            assert len(logs) == 3
            assert logs[0]["iter"] == 1

    def test_last_n_respected(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        lines = "\n".join(json.dumps({"iter": i}) for i in range(10))
        log_file.write_text(lines + "\n")
        with patch("trainer.LOG_FILE", log_file):
            mgr = TrainerManager()
            logs = mgr.get_log(last_n=3)
            assert len(logs) == 3
            assert logs[-1]["iter"] == 9


# ── start_training / stop_training ────────────────────────────────────────────

class TestTrainingLifecycle:
    def test_start_fails_when_already_running(self, tmp_path):
        status_file = tmp_path / "status.json"
        status_file.write_text(json.dumps({"status": "training"}))
        with patch("trainer.STATUS_FILE", status_file), \
             patch("trainer.LOG_FILE",    tmp_path / "log.jsonl"), \
             patch("trainer.DATASETS_DIR", tmp_path / "datasets"), \
             patch("trainer.ADAPTERS_DIR", tmp_path / "adapters"):
            mgr = TrainerManager()
            result = mgr.start_training(dataset_name="test")
            assert "error" in result
            assert "already running" in result["error"]

    def test_start_fails_when_dataset_missing(self, tmp_path):
        with patch("trainer.STATUS_FILE",  tmp_path / "status.json"), \
             patch("trainer.LOG_FILE",     tmp_path / "log.jsonl"), \
             patch("trainer.DATASETS_DIR", tmp_path / "datasets"), \
             patch("trainer.ADAPTERS_DIR", tmp_path / "adapters"):
            mgr = TrainerManager()
            result = mgr.start_training(dataset_name="nonexistent_dataset")
            assert "error" in result
            assert "not found" in result["error"]

    def test_stop_when_no_process(self, tmp_path):
        with patch("trainer.STATUS_FILE", tmp_path / "status.json"):
            mgr = TrainerManager()
            result = mgr.stop_training()
            assert result["status"] == "no job running"

    def test_stop_terminates_running_process(self, tmp_path):
        status_file = tmp_path / "status.json"
        with patch("trainer.STATUS_FILE", status_file):
            mgr = TrainerManager()
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None   # process is running
            mgr._process = mock_proc
            result = mgr.stop_training()
            mock_proc.terminate.assert_called_once()
            assert result["status"] == "stopped"
            assert mgr._process is None


# ── list_datasets / list_adapters ──────────────────────────────────────────────

class TestListDatasets:
    def test_empty_dir(self, tmp_path):
        with patch("trainer.DATASETS_DIR", tmp_path):
            mgr = TrainerManager()
            assert mgr.list_datasets() == []

    def test_lists_dataset_dirs(self, tmp_path):
        ds = tmp_path / "my_dataset"
        ds.mkdir()
        (ds / "train.jsonl").write_text('{"a":1}\n{"b":2}\n')
        (ds / "valid.jsonl").write_text('{"c":3}\n')
        with patch("trainer.DATASETS_DIR", tmp_path):
            mgr = TrainerManager()
            result = mgr.list_datasets()
            assert len(result) == 1
            assert result[0]["name"] == "my_dataset"
            assert result[0]["train_examples"] == 2
            assert result[0]["valid_examples"] == 1

    def test_skips_files_not_dirs(self, tmp_path):
        (tmp_path / "not_a_dir.txt").write_text("hello")
        with patch("trainer.DATASETS_DIR", tmp_path):
            mgr = TrainerManager()
            assert mgr.list_datasets() == []


class TestListAdapters:
    def test_empty_dir(self, tmp_path):
        with patch("trainer.ADAPTERS_DIR", tmp_path):
            mgr = TrainerManager()
            assert mgr.list_adapters() == []

    def test_lists_adapter_dirs_with_npz(self, tmp_path):
        ad = tmp_path / "my_adapter"
        ad.mkdir()
        (ad / "adapters.npz").write_bytes(b"\x00" * 1000)
        with patch("trainer.ADAPTERS_DIR", tmp_path):
            mgr = TrainerManager()
            result = mgr.list_adapters()
            assert len(result) == 1
            assert result[0]["name"] == "my_adapter"

    def test_skips_dirs_without_npz(self, tmp_path):
        ad = tmp_path / "incomplete_adapter"
        ad.mkdir()
        with patch("trainer.ADAPTERS_DIR", tmp_path):
            mgr = TrainerManager()
            assert mgr.list_adapters() == []
