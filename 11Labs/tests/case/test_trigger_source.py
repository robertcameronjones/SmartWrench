"""JSON-file TriggerSource tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from guidepoint.case import (
    CaseId,
    JsonTriggerPaths,
    TriggerId,
    TriggerNotFoundError,
    build_json_trigger_source,
)
from tests.case._helpers import sample_trigger


class TestPaths:
    def test_layout(self, tmp_path: Path) -> None:
        paths = JsonTriggerPaths.for_root(tmp_path)
        assert paths.triggers_dir == (tmp_path / "fixtures" / "triggers").resolve()


class TestSaveAndGet:
    def test_save_then_get(self, tmp_path: Path) -> None:
        source = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
        source.save(sample_trigger())
        assert source.get(TriggerId("trig_1")) == sample_trigger()

    def test_get_missing_raises(self, tmp_path: Path) -> None:
        source = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
        with pytest.raises(TriggerNotFoundError):
            source.get(TriggerId("missing"))


class TestPending:
    def test_yields_only_pending(self, tmp_path: Path) -> None:
        source = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
        source.save(sample_trigger(trigger_id="t_pending"))
        source.save(sample_trigger(trigger_id="t_fired"))
        source.mark_fired(TriggerId("t_fired"), case_id=CaseId("case_x"))
        ids = {t.id for t in source.pending()}
        assert ids == {"t_pending"}

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        source = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
        assert list(source.pending()) == []


class TestStatusMutations:
    def test_mark_fired_sets_status_and_records_case_id(self, tmp_path: Path) -> None:
        source = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
        source.save(sample_trigger())
        source.mark_fired(TriggerId("trig_1"), case_id=CaseId("case_a"))
        loaded = source.get(TriggerId("trig_1"))
        assert loaded.status == "fired"
        assert loaded.fired_at is not None
        assert "case_a" in loaded.error_detail

    def test_mark_failed_records_reason(self, tmp_path: Path) -> None:
        source = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
        source.save(sample_trigger())
        source.mark_failed(TriggerId("trig_1"), error_detail="vehicle not found")
        loaded = source.get(TriggerId("trig_1"))
        assert loaded.status == "failed"
        assert "vehicle not found" in loaded.error_detail
