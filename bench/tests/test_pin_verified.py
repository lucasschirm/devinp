"""WP6c pre-flight E1 (issue #44) — the 500-instance manifest pinning.

Zero network, zero quota. The fixture IS the committed
``bench/verified_instances.json``, fed back through the assertion layer
(:func:`pin_verified_instances.validate`) in the exact shape
``pin_pilot_b_instances.fetch_all_rows()`` returns
(``[{"row_idx": int, "row": {…}}, …]``):

* the happy path plus **every deliberate break** the gate exists to
  catch (the totals, a duplicate id, a missing/empty field, the
  ``eval_type`` drift, the test-list contract, the repo-set and
  per-repo-count drift, the Pilot-A/B overlap statements);
* and, on the unmutated fixture, the split facts of WP0 §5 / PLAN-WP6C
  §2.3 (500 rows · 12 repos · the per-repo counts · an ``image`` and
  ``pass_and_fail`` on every row · the overlap · the id-list hash) — so
  the real 500-instance queue is re-verified in every CI run without the
  HF API. The ``--check`` mode that *re-fetches* is the operator's, run
  outside CI.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_BENCH = Path(__file__).resolve().parent.parent
_SCRIPT = _BENCH / "scripts" / "pin_verified_instances.py"
_MANIFEST = _BENCH / "verified_instances.json"


def _load_script(name="pin_verified_instances_under_test"):
    spec = importlib.util.spec_from_file_location(name, str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pv = _load_script()


def _committed_doc():
    if not _MANIFEST.is_file():
        pytest.skip("bench/verified_instances.json not present")
    return json.loads(_MANIFEST.read_text())


def _payload() -> "list[dict]":
    """The committed 500-instance queue back into the shape
    ``fetch_all_rows()`` returns (a deep copy — the mutation tests must
    not corrupt the cached manifest).

    The fixture IS the published split as pinned: the overlap assertions
    (the Pilot-A / Pilot-B ids) then hold by construction, and the
    deliberate breaks are real edits to real rows."""
    doc = _committed_doc()
    return [{"row_idx": i, "row": copy.deepcopy(inst)}
            for i, inst in enumerate(doc["instances"])]


def _validate(rows, pilot_b=()):
    return pv.validate(rows, keep=tuple(pv_build_keep()),
                      pilot_b_ids=list(pilot_b))


def pv_build_keep():
    """The KEEP projection of the WP6b module (the field list the real
    fetch pins — imported once, asserted against below)."""
    pin = pv.load_pinning_module()
    return list(pin.KEEP)


def test_keep_projection_is_the_harness_field_set():
    """The manifest's shape is defined by the WP6b module it reuses — if
    that list changes, this test says so loudly (the queue's rows would
    lose a field the harness consumes)."""
    assert set(pv_build_keep()) >= {
        "instance_id", "repo", "base_commit", "problem_statement",
        "image", "eval_type", "FAIL_TO_PASS", "PASS_TO_PASS",
        "test_patch"}


def test_validate_accepts_the_published_shape():
    pinned = _validate(_payload())
    assert len(pinned) == 500
    assert len({i["instance_id"] for i in pinned}) == 500
    # the repo_url convention of the WP6b manifest / load_instances
    assert pinned[0]["repo_url"] == ("https://github.com/"
                                    + pinned[0]["repo"] + ".git")
    # the KEEP projection is verbatim: no field invented, none dropped
    assert set(pinned[0]) == set(pv_build_keep()) | {"repo_url"}


def test_validate_allows_an_empty_pass_to_pass():
    """The measured upstream shape: the live split really does ship rows
    with ``PASS_TO_PASS: []`` (``django__django-10973``) — the gate must
    not reject them. An empty ``FAIL_TO_PASS`` is the opposite case (it
    would grade a do-nothing patch as resolved)."""
    rows = _payload()
    empties = [r["row"]["instance_id"] for r in rows
               if not r["row"]["PASS_TO_PASS"]]
    assert empties, ("the pinned split is expected to hold at least one "
                     "row without a regression list (measured 2026-11-13)")
    assert len(_validate(rows)) == 500


# ---------------------------------------------------------------------------
# every deliberate break
# ---------------------------------------------------------------------------

def test_rejects_wrong_total():
    rows = _payload()[:-1]
    with pytest.raises(pv.ManifestError, match="expected 500 rows"):
        _validate(rows)


def test_rejects_empty_fetch():
    with pytest.raises(pv.ManifestError, match="no rows"):
        _validate([])


def test_rejects_duplicate_instance_id():
    rows = _payload()
    rows[7]["row"]["instance_id"] = rows[6]["row"]["instance_id"]
    with pytest.raises(pv.ManifestError, match="duplicate instance_id"):
        _validate(rows)


def test_rejects_missing_pinned_field():
    rows = _payload()
    del rows[3]["row"]["image"]
    with pytest.raises(pv.ManifestError, match="missing pinned field"):
        _validate(rows)


def test_rejects_empty_image():
    rows = copy.deepcopy(_payload())
    rows[2]["row"]["image"] = ""
    with pytest.raises(pv.ManifestError, match="non-empty string"):
        _validate(rows)


def test_rejects_null_problem_statement():
    rows = copy.deepcopy(_payload())
    rows[2]["row"]["problem_statement"] = None
    with pytest.raises(pv.ManifestError, match="non-empty string"):
        _validate(rows)


def test_rejects_eval_type_drift():
    rows = copy.deepcopy(_payload())
    rows[5]["row"]["eval_type"] = "fail_only"
    with pytest.raises(pv.ManifestError, match="pass_and_fail"):
        _validate(rows)


def test_rejects_empty_fail_to_pass():
    rows = copy.deepcopy(_payload())
    rows[1]["row"]["FAIL_TO_PASS"] = []
    with pytest.raises(pv.ManifestError, match="FAIL_TO_PASS is empty"):
        _validate(rows)


def test_rejects_non_list_test_ids():
    rows = copy.deepcopy(_payload())
    rows[1]["row"]["PASS_TO_PASS"] = "tests.test_z::test_w"
    with pytest.raises(pv.ManifestError, match="list of non-empty"):
        _validate(rows)


def test_rejects_non_string_test_id():
    rows = copy.deepcopy(_payload())
    rows[1]["row"]["FAIL_TO_PASS"] = ["ok", 7]
    with pytest.raises(pv.ManifestError, match="list of non-empty"):
        _validate(rows)


def test_rejects_row_object_drift():
    rows = copy.deepcopy(_payload())
    del rows[9]["row"]
    with pytest.raises(pv.ManifestError, match="no row object"):
        _validate(rows)


def test_rejects_repo_set_drift():
    rows = copy.deepcopy(_payload())
    # flask's single row becomes a 13th repo
    rows[-1]["row"]["repo"] = "newcomer/repo"
    with pytest.raises(pv.ManifestError, match="repo set drifted"):
        _validate(rows)


def test_rejects_per_repo_count_drift():
    rows = copy.deepcopy(_payload())
    # a sympy row moves to django: both counts are now wrong
    sympy_pos = next(i for i, r in enumerate(rows)
                    if r["row"]["repo"] == "sympy/sympy")
    rows[sympy_pos]["row"]["repo"] = "django/django"
    with pytest.raises(pv.ManifestError,
                       match="per-repo counts drifted"):
        _validate(rows)


def test_rejects_missing_pilot_a_overlap():
    """The overlap statement of PLAN-WP6C §13 must be true, not
    assumed: the Pilot-A ids are in the pinned queue or the record's
    reuse claim is false."""
    rows = copy.deepcopy(_payload())
    flask = next(i for i, r in enumerate(rows)
                if r["row"]["instance_id"] == "pallets__flask-5014")
    rows[flask]["row"]["instance_id"] = "pallets__flask-9999"
    with pytest.raises(pv.ManifestError, match="Pilot-A"):
        pv.validate(rows, keep=tuple(pv_build_keep()),
                   pilot_b_ids=list(pv.PILOT_A_IDS[:0]))


def test_validate_accepts_the_pilot_overlap():
    """The committed split satisfies both overlap statements (the
    Pilot-A / Pilot-B ids come from the real manifests)."""
    pinned = pv.validate(_payload(), keep=tuple(pv_build_keep()),
                        pilot_b_ids=pv.pilot_b_instance_ids())
    assert len(pinned) == 500


# ---------------------------------------------------------------------------
# the document + --check
# ---------------------------------------------------------------------------

def _doc(tmp_path, fetched_at="2026-01-01"):
    pinned = _validate(_payload())
    doc = pv.build_doc(pinned, fetched_at=fetched_at,
                      keep=tuple(pv_build_keep()))
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return doc, str(path)


def test_build_doc_records_the_provenance(tmp_path):
    pinned = _validate(_payload())
    doc = pv.build_doc(pinned, fetched_at="2026-01-01",
                      keep=tuple(pv_build_keep()))
    m = doc["_meta"]
    assert m["total_instances"] == 500
    assert m["repo_count"] == 12
    assert m["fetched_at"] == "2026-01-01"
    assert m["per_repo"]["django/django"] == 231
    # the per-repo map is sorted by descending count
    assert list(m["per_repo"].values()) == sorted(
        m["per_repo"].values(), reverse=True)
    assert len(m["instance_ids_sha256"]) == 64
    assert m["rule"] == pv.RULE
    assert [i["instance_id"] for i in doc["instances"]] == [
        i["instance_id"] for i in pinned]


def test_check_clean(tmp_path):
    doc, path = _doc(tmp_path)
    assert pv.check(path, doc) == []


def test_check_fetched_at_is_exempt(tmp_path):
    doc, path = _doc(tmp_path, fetched_at="2026-01-01")
    fresh = json.loads(json.dumps(doc))
    fresh["_meta"]["fetched_at"] = "2027-02-02"
    assert pv.check(path, fresh) == []


def test_check_detects_content_drift(tmp_path):
    doc, path = _doc(tmp_path)
    fresh = json.loads(json.dumps(doc))
    fresh["instances"][4]["test_patch"] = "--- a\n+++ b\n# changed\n"
    drift = pv.check(path, fresh)
    assert drift and "CONTENT" in drift[0]


def test_check_detects_id_list_drift(tmp_path):
    doc, path = _doc(tmp_path)
    fresh = json.loads(json.dumps(doc))
    del fresh["instances"][0]
    drift = pv.check(path, fresh)
    assert any("instance list differs" in d for d in drift)


def test_check_detects_meta_drift(tmp_path):
    doc, path = _doc(tmp_path)
    fresh = json.loads(json.dumps(doc))
    fresh["_meta"]["instance_ids_sha256"] = "0" * 64
    drift = pv.check(path, fresh)
    assert any("instance_ids_sha256" in d for d in drift)


def test_check_missing_file(tmp_path):
    doc = pv.build_doc(_validate(_payload()), fetched_at="x",
                      keep=tuple(pv_build_keep()))
    drift = pv.check(str(tmp_path / "absent.json"), doc)
    assert drift and "unreadable" in drift[0]


def test_check_non_json_file(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json")
    doc = pv.build_doc(_validate(_payload()), fetched_at="x",
                      keep=tuple(pv_build_keep()))
    drift = pv.check(str(path), doc)
    assert drift and "not JSON" in drift[0]


# ---------------------------------------------------------------------------
# the CLI (offline — the CI path)
# ---------------------------------------------------------------------------

def test_main_offline_write_then_check(tmp_path, capsys):
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(_payload()))
    out = str(tmp_path / "manifest.json")
    assert pv.main(["--offline", str(payload), "--out", out,
                    "--fetched-at", "2026-01-01"]) == 0
    doc = json.loads(Path(out).read_text())
    assert doc["_meta"]["total_instances"] == 500
    assert pv.main(["--offline", str(payload), "--out", out,
                    "--check"]) == 0
    assert "reproducible" in capsys.readouterr().out
    # a mutation of the committed file is drift (exit 1)
    doc["instances"][0]["base_commit"] = "deadbeef"
    Path(out).write_text(json.dumps(doc, indent=2) + "\n")
    assert pv.main(["--offline", str(payload), "--out", out,
                    "--check"]) == 1


def test_main_offline_reports_a_failed_assertion(tmp_path, capsys):
    rows = _payload()
    del rows[0]["row"]["base_commit"]
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(rows))
    assert pv.main(["--offline", str(payload),
                    "--out", str(tmp_path / "m.json")]) == 1
    assert "missing pinned field" in capsys.readouterr().err


def test_offline_payload_shape_is_validated(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(SystemExit, match="must be a list"):
        pv._offline_rows(str(good))
    missing = tmp_path / "absent.json"
    with pytest.raises(SystemExit, match="unreadable"):
        pv._offline_rows(str(missing))


def test_pilot_b_ids_tolerates_a_missing_file(tmp_path):
    """The overlap check must not make the *split* validation unreadable
    when the pilot manifest is absent / corrupt."""
    assert pv.pilot_b_instance_ids(str(tmp_path / "absent.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert pv.pilot_b_instance_ids(str(bad)) == []
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"instances": "nope"}))
    assert pv.pilot_b_instance_ids(str(wrong)) == []


# ---------------------------------------------------------------------------
# the committed artifact (zero network)
# ---------------------------------------------------------------------------

def _committed():
    if not _MANIFEST.is_file():
        pytest.skip("bench/verified_instances.json not present")
    return json.loads(_MANIFEST.read_text())


def test_committed_manifest_passes_the_same_gate():
    """The 500 / 12 / per-repo / test-list / eval_type assertions run on
    the committed queue in every CI job — the real manifest is held to
    the rule that produced it, offline."""
    doc = _committed()
    rows = [{"row_idx": i, "row": inst}
            for i, inst in enumerate(doc["instances"])]
    pinned = pv.validate(rows, keep=tuple(pv_build_keep()),
                        pilot_b_ids=pv.pilot_b_instance_ids())
    assert len(pinned) == 500


def test_committed_manifest_meta_matches_its_content():
    doc = _committed()
    m = doc["_meta"]
    ids = [i["instance_id"] for i in doc["instances"]]
    assert m["total_instances"] == len(ids) == 500
    assert len(set(ids)) == 500
    assert m["instance_ids_sha256"] == pv._sha256_ids(ids)
    counts: dict = {}
    for i in doc["instances"]:
        counts[i["repo"]] = counts.get(i["repo"], 0) + 1
    assert counts == pv.EXPECTED_PER_REPO
    assert m["per_repo"] == dict(sorted(
        counts.items(), key=lambda kv: (-kv[1], kv[0])))
    assert m["repo_count"] == 12


def test_committed_manifest_covers_the_spent_sessions():
    """Every already-spent Pilot-A / Pilot-B instance is inside the 500:
    they are ``--import-run`` candidates, never a silent re-spend."""
    doc = _committed()
    ids = {i["instance_id"] for i in doc["instances"]}
    assert set(pv.PILOT_A_IDS) <= ids
    pilot_b = pv.pilot_b_instance_ids()
    assert len(pilot_b) == 25, pilot_b
    assert set(pilot_b) <= ids
    assert doc["_meta"]["overlap_pilot_b"] == sorted(pilot_b)
    assert doc["_meta"]["overlap_pilot_a"] == list(pv.PILOT_A_IDS)


def test_committed_manifest_loads_in_the_pilot():
    """The queue file satisfies ``live_pilot.load_instances`` (the field
    and instance_id-charset contract) — 500 loadable instances."""
    import live_pilot as lp
    insts = lp.load_instances(str(_MANIFEST))
    assert len(insts) == 500
    assert {i["instance_id"] for i in insts} == {
        i["instance_id"] for i in _committed()["instances"]}
