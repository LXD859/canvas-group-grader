"""Preview immutable intentions, recheck Canvas, journal each side effect."""
import hashlib
import json
from decimal import Decimal

from .canvas_gateway import CanvasFailure, validate_assignment
from .parsing import InputError, calculate_rows


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def group_state(snapshot, group):
    return {"assignment": snapshot["assignment"], "group": group,
            "submissions": {str(m["id"]): snapshot["submissions"].get(str(m["id"])) for m in group["members"]}}


def scores_equal(state, score):
    return bool(state["submissions"]) and all(
        sub and sub.get("score") is not None and Decimal(str(sub["score"])) == Decimal(score)
        for sub in state["submissions"].values())


def comment_present(state, body, author):
    if not body:
        return True
    # Canvas can create separate comment IDs per submission for a group comment.
    # Verify exact content and author on every member; do not require identical IDs.
    return bool(state["submissions"]) and all(
        any(c.get("comment") == body and c.get("author_id") == author and not c.get("draft", False)
            for c in (sub or {}).get("comments", []))
        for sub in state["submissions"].values())


def validate_group(snapshot, group):
    errors = []
    if not group["members"]:
        return ["Canvas 小组没有成员。"]
    for member in group["members"]:
        sub = snapshot["submissions"].get(str(member["id"]))
        if not sub:
            errors.append(f"无法读取成员 {member['name']} 的作业成绩。")
            continue
        actual_group = sub.get("group") or {}
        if actual_group.get("id") != group["id"]:
            errors.append(f"成员 {member['name']} 的提交尚未关联此小组，请在 Canvas 核对小组提交。")
        if sub.get("excused"):
            errors.append(f"成员 {member['name']} 已免交，请在 Canvas 处理后再导入。")
        if sub.get("points_deducted") not in (None, 0, 0.0, "0"):
            errors.append(f"成员 {member['name']} 存在 Canvas 自动扣分，请先核对迟交政策。")
    return errors


def make_preview(gateway, course_id, assignment_id, table, group_column, question_columns):
    snapshot = gateway.snapshot(course_id, assignment_id)
    validate_assignment(snapshot["assignment"])
    rows = calculate_rows(table, group_column, question_columns, snapshot["assignment"]["points_possible"])
    names = {}
    for group in snapshot["groups"]:
        names.setdefault(group["name"].strip(), []).append(group)
    for row in rows:
        matches = names.get(row["name"], [])
        row.update({"members": [], "existing": [], "needs_update": False, "unchanged": False})
        if len(matches) != 1:
            row["errors"].append("Canvas 中找不到同名小组。" if not matches else "Canvas 中存在同名小组，无法唯一匹配。")
            continue
        group = matches[0]
        row["errors"].extend(validate_group(snapshot, group))
        state = group_state(snapshot, group)
        row.update({"group_id": group["id"], "members": group["members"], "state": state,
                    "fingerprint": digest(state),
                    "existing": [{"name": m["name"], "score": (state["submissions"].get(str(m["id"])) or {}).get("score")}
                                 for m in group["members"]]})
        same_score = scores_equal(state, row["score"])
        same_comment = comment_present(state, row["comment"], gateway.user_id)
        row["unchanged"] = same_score and same_comment
        # Explicitly confirm any change on an already graded group (including new feedback).
        row["needs_update"] = any(s and s.get("score") is not None for s in state["submissions"].values()) and not row["unchanged"]
    return {"course_id": course_id, "assignment_id": assignment_id, "assignment": snapshot["assignment"], "rows": rows}


def public_preview(preview):
    return {**preview, "rows": [{k: v for k, v in row.items() if k not in {"state", "fingerprint"}}
                                 for row in preview["rows"]]}


def selected_rows(preview, selected, confirmed):
    if not selected or len(selected) != len(set(selected)):
        raise InputError("请勾选至少一个已批改的小组，且不要重复选择。")
    mapping = {row["row"]: row for row in preview["rows"]}
    if any(r not in mapping for r in selected):
        raise InputError("选择的行不在本次预览中。")
    rows = [mapping[r] for r in selected]
    if any(r["errors"] for r in rows):
        raise InputError("选中的小组存在错误，请修正并重新预览。")
    if any(r["needs_update"] and r["row"] not in confirmed for r in rows):
        raise InputError("请逐组确认更新已有成绩或评语。")
    return rows


def current_state(gateway, preview, row):
    snap = gateway.snapshot(preview["course_id"], preview["assignment_id"], group_id=row["group_id"])
    validate_assignment(snap["assignment"])
    matches = [g for g in snap["groups"] if g["id"] == row["group_id"]]
    if len(matches) != 1:
        raise InputError("小组已变化，请重新预览。")
    return group_state(snap, matches[0])


def operation_key(owner, preview, row, phase):
    return digest([owner, preview["course_id"], preview["assignment_id"], row["group_id"],
                   [(m["id"], row["state"]["submissions"][str(m["id"])].get("attempt")) for m in row["members"]],
                   phase, row["score"] if phase == "grade" else row["comment"]])


def run_job(gateway, store, owner, job_id, preview, rows):
    payload = {"course_id": preview["course_id"], "assignment_id": preview["assignment_id"],
               "assignment": preview["assignment"]["name"], "results": []}
    try:
        # Check the entire selection before the first side effect.
        snapshot = gateway.snapshot(preview["course_id"], preview["assignment_id"])
        for row in rows:
            groups = [g for g in snapshot["groups"] if g["id"] == row["group_id"]]
            if len(groups) != 1 or digest(group_state(snapshot, groups[0])) != row["fingerprint"]:
                raise InputError("预览后的作业、小组、成绩或评语已变化。此次未开始写入，请重新预览。")
        for row in rows:
            result = {"row": row["row"], "name": row["name"], "score": row["score"],
                      "comment": row["comment"], "grade": "pending", "feedback": "pending", "status": "running"}
            payload["results"].append(result)
            store.save_job(job_id, "running", payload)
            try:
                state = current_state(gateway, preview, row)
                if digest(state) != row["fingerprint"]:
                    raise InputError("该小组在预览后发生变化，请重新预览。")
                if not row["members"]:
                    raise InputError("小组没有成员。")
                representative = row["members"][0]["id"]
                context = (preview["course_id"], preview["assignment_id"], representative)
                for phase in ("grade", "comment"):
                    field = "grade" if phase == "grade" else "feedback"
                    desired = row["score"] if phase == "grade" else row["comment"]
                    key = operation_key(owner, preview, row, phase)
                    matches = scores_equal(state, row["score"]) if phase == "grade" else comment_present(state, row["comment"], gateway.user_id)
                    if matches:
                        result[field] = "skipped"
                        store.mark(key, owner, phase, "done")
                        continue
                    prior = store.operation(key)
                    if prior and prior["status"] in {"inflight", "uncertain"}:
                        result[field] = "uncertain"
                        raise InputError("此前请求结果待核验，已阻止重复写入。请先在 Canvas 核查，再使用历史中的核验入口。")
                    # Commit journal before a request can leave the process.
                    store.mark(key, owner, phase, "inflight")
                    failure = None
                    try:
                        if phase == "grade":
                            gateway.grade(*context, desired)
                        else:
                            gateway.comment(*context, desired)
                    except CanvasFailure as exc:
                        failure = exc
                    try:
                        after = current_state(gateway, preview, row)
                    except Exception:
                        store.mark(key, owner, phase, "uncertain")
                        result[field] = "uncertain"
                        raise InputError("请求后无法回读 Canvas，结果待核验。请稍后重新预览。") from None
                    verified = scores_equal(after, row["score"]) if phase == "grade" else comment_present(after, row["comment"], gateway.user_id)
                    if not verified:
                        status = "failed" if failure and not failure.uncertain else "uncertain"
                        store.mark(key, owner, phase, status)
                        result[field] = status
                        raise InputError(str(failure) if failure else "Canvas 回读未确认全部组员更新，已停止该小组后续操作。")
                    store.mark(key, owner, phase, "done")
                    result[field] = "done"
                    # Stop if membership or assignment changed during the write.
                    if after["assignment"] != state["assignment"] or after["group"] != state["group"]:
                        raise InputError("提交期间作业或小组成员发生变化，请到 Canvas 核查。")
                    state = after
                result["status"] = "skipped" if result["grade"] == result["feedback"] == "skipped" else "done"
            except (InputError, CanvasFailure) as exc:
                result["status"] = "uncertain" if "uncertain" in (result["grade"], result["feedback"]) else "failed"
                result["message"] = str(exc)
            except Exception:
                result["status"] = "uncertain"
                result["message"] = "操作异常，已停止批次；请重新连接并核验 Canvas。"
            store.save_job(job_id, "running", payload)
            if result["status"] in {"failed", "uncertain"}:
                payload["message"] = "批次已停止，后续未列出的小组尚未提交。"
                break
        status = "done" if len(payload["results"]) == len(rows) and all(r["status"] in {"done", "skipped"} for r in payload["results"]) else "attention"
    except (InputError, CanvasFailure) as exc:
        payload["message"], status = str(exc), "attention"
    except Exception:
        payload["message"], status = "批次中断，请重新预览并核查 Canvas。", "attention"
    store.save_job(job_id, status, payload)
