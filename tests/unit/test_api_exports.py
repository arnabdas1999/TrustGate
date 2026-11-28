"""Mock mode also serves investigations exported from a live run (bench/exports/*.json)."""

from __future__ import annotations

from api import mock_data
from api.schemas import InvestigationView
from api.store import EXPORTS_DIR, load_exports, seeded


def _export(tmp_path, name="opspilot-eval-1.json"):
    investigations, proposals, _ = mock_data.build()
    inv = investigations["inv_opspilot_demo"].model_copy(update={"id": "inv_real_export"})
    prop = proposals["prop_opspilot_demo"].model_copy(
        update={"id": "prop_real_export", "investigation_id": "inv_real_export"})
    calls = [{"tool": "create_environment_feature_version", "arguments": {"feature_pk": 1}}]
    view = InvestigationView(investigation=inv, proposal=prop, calls=calls, active_ms=5003)
    (tmp_path / name).write_text(view.model_dump_json(), encoding="utf-8")
    return view


def test_exports_join_the_mock_fixtures(tmp_path):
    view = _export(tmp_path)
    store = seeded(True, exports_dir=tmp_path)
    assert "inv_opspilot_demo" in store.investigations  # fixtures stay
    assert store.investigations["inv_real_export"] == view.investigation
    assert store.proposals["prop_real_export"] == view.proposal
    assert store.proposal_calls["prop_real_export"] == view.calls


def test_no_exports_directory_means_fixtures_only(tmp_path):
    assert load_exports(tmp_path / "missing") == []
    assert set(seeded(True, exports_dir=tmp_path / "missing").investigations) == \
        set(mock_data.build()[0])


def test_real_mode_ignores_exports(tmp_path):
    _export(tmp_path)
    assert seeded(False, exports_dir=tmp_path).investigations == {}


def test_exports_dir_is_bench_exports():
    assert EXPORTS_DIR.parts[-2:] == ("bench", "exports")
