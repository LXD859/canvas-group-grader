import copy

import pytest

from app.grading import make_preview, run_job, selected_rows, operation_key
from app.parsing import InputError, read_table


def preview(gateway, body='Group,E1,E2\nAssignment Group 1,"-1.5, Reason.",\nAssignment Group 2,,\n'):
    return make_preview(gateway, 10, 20, read_table("test.csv", body.encode()), 0, [1, 2])


def run(gateway, store, value, row_ids=None):
    rows = selected_rows(value, row_ids or [r["row"] for r in value["rows"]], [r["row"] for r in value["rows"]])
    job = store.job("owner", {})
    run_job(gateway, store, "owner", job, value, rows)
    return store.get_job(job, "owner")


def test_group_write_and_repeat_import(gateway, store):
    result = run(gateway, store, preview(gateway))
    assert result["status"] == "done"
    assert len(gateway.grade_calls) == 2
    assert len(gateway.comment_calls) == 1
    assert gateway.data["submissions"]["1"]["score"] == gateway.data["submissions"]["2"]["score"] == 8.5
    assert gateway.data["submissions"]["3"]["score"] == 10
    second = preview(gateway)
    assert all(r["unchanged"] for r in second["rows"])
    assert run(gateway, store, second)["status"] == "done"
    assert len(gateway.grade_calls) == 2 and len(gateway.comment_calls) == 1


@pytest.mark.parametrize("change", ["score", "membership", "maximum", "comment"])
def test_stale_preview_no_write(gateway, store, change):
    value = preview(gateway)
    if change == "score": gateway.data["submissions"]["1"]["score"] = 5
    if change == "membership": gateway.data["groups"][0]["members"].pop()
    if change == "maximum": gateway.data["assignment"]["points_possible"] = 100
    if change == "comment": gateway.data["submissions"]["1"]["comments"].append({"id": 44, "comment": "New"})
    assert run(gateway, store, value)["status"] == "attention"
    assert not gateway.grade_calls and not gateway.comment_calls


def test_individual_grading_blocked(gateway):
    gateway.data["assignment"]["grade_group_students_individually"] = True
    with pytest.raises(InputError): preview(gateway)


@pytest.mark.parametrize("kind", ["duplicate", "empty", "unmatched", "submission_group", "late"])
def test_invalid_groups(gateway, kind):
    if kind == "duplicate": gateway.data["groups"].append(copy.deepcopy(gateway.data["groups"][0]))
    if kind == "empty": gateway.data["groups"][0]["members"] = []
    if kind == "unmatched": gateway.data["groups"][0]["name"] = "Other"
    if kind == "submission_group": gateway.data["submissions"]["1"]["group"] = {"id": 99}
    if kind == "late": gateway.data["submissions"]["1"]["points_deducted"] = 1
    value = preview(gateway)
    assert value["rows"][0]["errors"]
    with pytest.raises(InputError): selected_rows(value, [2], [])


def test_explicit_existing_confirmation(gateway, store):
    for sub in gateway.data["submissions"].values(): sub["score"] = 3
    value = preview(gateway)
    with pytest.raises(InputError): selected_rows(value, [2], [])
    assert run(gateway, store, value)["status"] == "done"


def test_write_timeout_verified_by_read(gateway, store):
    gateway.fail_grade, gateway.fail_comment = "after", "after"
    assert run(gateway, store, preview(gateway))["status"] == "done"
    assert len(gateway.comment_calls) == 1


def test_grade_success_comment_failure_resume(gateway, store):
    gateway.fail_comment = "before"
    first = run(gateway, store, preview(gateway))
    assert first["status"] == "attention"
    assert first["payload"]["results"][0]["grade"] == "done"
    gateway.fail_comment = None
    assert run(gateway, store, preview(gateway))["status"] == "done"
    assert len(gateway.grade_calls) == 2  # One call for each group, no repeat for group 1.


def test_unknown_comment_never_blindly_retried(gateway, store):
    gateway.fail_comment = "unknown"
    first = run(gateway, store, preview(gateway))
    assert first["payload"]["results"][0]["feedback"] == "uncertain"
    gateway.fail_comment = None
    assert run(gateway, store, preview(gateway))["status"] == "attention"
    assert len(gateway.comment_calls) == 1
    store.clear("owner")
    assert run(gateway, store, preview(gateway))["status"] == "attention"
    assert len(gateway.comment_calls) == 1


def test_failure_to_read_after_comment_can_recover(gateway, store):
    gateway.fail_comment = "unreadable"
    result = run(gateway, store, preview(gateway))
    assert result["payload"]["results"][0]["feedback"] == "uncertain"
    gateway.read_failure, gateway.fail_comment = False, None
    assert run(gateway, store, preview(gateway))["status"] == "done"
    assert len(gateway.comment_calls) == 1


def test_grade_permission_failure_no_comment(gateway, store):
    gateway.fail_grade = "before"
    assert run(gateway, store, preview(gateway))["status"] == "attention"
    assert not gateway.comment_calls


def test_restart_keeps_unresolved_operation(tmp_path):
    from app.store import Store
    path = tmp_path / "restart.db"
    first = Store(path)
    job = first.job("owner", {})
    first.mark("key", "owner", "comment", "inflight")
    first.close()
    second = Store(path)
    assert second.get_job(job, "owner")["status"] == "interrupted"
    assert second.operation("key")["status"] == "inflight"
    second.close()


def test_partial_group_propagation_stops_comment(gateway, store, monkeypatch):
    def grade_only_one(course, assignment, user, score):
        gateway.data["submissions"][str(user)]["score"] = float(score)
    monkeypatch.setattr(gateway, "grade", grade_only_one)
    result = run(gateway, store, preview(gateway))
    assert result["status"] == "attention"
    assert result["payload"]["results"][0]["grade"] == "uncertain"
    assert not gateway.comment_calls


def test_changed_feedback_appended_without_deleting_history(gateway, store):
    run(gateway, store, preview(gateway))
    value = preview(gateway, 'Group,E1,E2\nAssignment Group 1,"-2, Revised reason.",\n')
    assert value["rows"][0]["needs_update"]
    run(gateway, store, value)
    comments = gateway.data["submissions"]["1"]["comments"]
    assert len(comments) == 2
    assert "Reason." in comments[0]["comment"]
    assert "Revised reason." in comments[1]["comment"]


def test_other_author_draft_or_partial_comment_not_treated_as_done(gateway):
    from app.grading import comment_present, group_state
    state = group_state(gateway.data, gateway.data["groups"][0])
    for sub in state["submissions"].values():
        sub["comments"] = [{"id": 1, "author_id": 99, "comment": "feedback", "draft": True}]
    assert not comment_present(state, "feedback", 99)
    for sub in state["submissions"].values():
        sub["comments"][0].update({"author_id": 98, "draft": False})
    assert not comment_present(state, "feedback", 99)
    state["submissions"]["1"]["comments"][0]["author_id"] = 99
    assert not comment_present(state, "feedback", 99)


def test_select_nothing_is_rejected(gateway):
    with pytest.raises(InputError): selected_rows(preview(gateway), [], [])
