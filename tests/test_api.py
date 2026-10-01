import time
import pytest

from fastapi.testclient import TestClient

from app.main import create_app
from app.canvas_gateway import CanvasFailure, CanvasGateway, BoundedSession
from canvasapi import exceptions as ce


HEADERS = {"X-Canvas-Local": "1"}


def test_web_end_to_end_and_secret_not_persisted(tmp_path, gateway):
    path = tmp_path / "history.db"
    app = create_app(path, lambda url, token: gateway)
    with TestClient(app, headers=HEADERS) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/session").status_code == 401
        connected = client.post("/api/connect", json={"token": "DO-NOT-SAVE-THIS-TOKEN"})
        assert connected.status_code == 200
        assert "httponly" in connected.headers["set-cookie"].lower()
        assert "DO-NOT-SAVE" not in connected.text
        assert client.get("/api/courses/10/assignments").status_code == 200
        data = b'Group,E1\nAssignment Group 1,"-1.5, Missing proof."\n'
        assert client.post("/api/upload", files={"file": ("scores.csv", data)}).status_code == 200
        value = client.post("/api/preview", json={"course_id": 10, "assignment_id": 20, "group_column": 0, "question_columns": [1]})
        assert value.status_code == 200, value.text
        assert "state" not in value.json()["rows"][0]
        job = client.post(f'/api/previews/{value.json()["id"]}/submit', json={"rows": [2]})
        assert job.status_code == 200, job.text
        for _ in range(100):
            result = client.get(f'/api/jobs/{job.json()["job_id"]}').json()
            if result["status"] != "running": break
            time.sleep(.01)
        assert result["status"] == "done"
        assert client.delete("/api/history").status_code == 200
        assert client.get("/api/history").json() == []
        assert client.post("/api/disconnect").status_code == 200
        assert client.get("/api/session").status_code == 401
    assert b"DO-NOT-SAVE-THIS-TOKEN" not in path.read_bytes()


def test_cross_origin_and_secret_validation(tmp_path, gateway):
    with TestClient(create_app(tmp_path / "t.db", lambda *a: gateway)) as client:
        assert client.post("/api/connect", json={"token": "x"}).status_code == 403
        assert client.post("/api/connect", headers={**HEADERS, "Origin": "https://evil.example"}, json={"token": "x"}).status_code == 403
        response = client.post("/api/connect", headers=HEADERS, json={"token": {"secret": "sensitive"}})
        assert response.status_code == 422 and "sensitive" not in response.text
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_unknown_operation_resolution(tmp_path, gateway):
    gateway.fail_comment = "unknown"
    with TestClient(create_app(tmp_path / "t.db", lambda *a: gateway), headers=HEADERS) as client:
        client.post("/api/connect", json={"token": "x"})
        client.post("/api/upload", files={"file": ("a.csv", b'Group,E1\nAssignment Group 1,"-1, Reason"')})
        config = {"course_id": 10, "assignment_id": 20, "group_column": 0, "question_columns": [1]}
        preview = client.post("/api/preview", json=config).json()
        job = client.post(f'/api/previews/{preview["id"]}/submit', json={"rows": [2]}).json()
        for _ in range(100):
            result = client.get(f'/api/jobs/{job["job_id"]}').json()
            if result["status"] != "running": break
            time.sleep(.01)
        fresh = client.post("/api/preview", json=config).json()
        assert fresh["rows"][0]["unresolved"] == ["comment"]
        url = f'/api/previews/{fresh["id"]}/resolve'
        assert client.post(url, json={"row": 2, "phase": "comment"}).status_code == 400
        assert client.post(url, json={"row": 2, "phase": "comment", "confirmed_absent_in_canvas": True}).status_code == 200


def test_gateway_pinned_library_compatibility():
    gateway = CanvasGateway("https://oc.sjtu.edu.cn", "synthetic")
    assert isinstance(gateway.canvas._Canvas__requester._session, BoundedSession)


def test_gateway_rate_limit_retry_and_no_write_retry(monkeypatch):
    monkeypatch.setattr("app.canvas_gateway.time.sleep", lambda _: None)
    gateway = CanvasGateway("https://oc.sjtu.edu.cn", "synthetic")
    calls = []
    def action():
        calls.append(1)
        if len(calls) < 3: raise ce.RateLimitExceeded("429")
        return "ok"
    assert gateway.call(action) == "ok"
    assert len(calls) == 3
    calls.clear()
    import pytest
    with pytest.raises(CanvasFailure): gateway.call(action, read=False)
    assert len(calls) == 1


@pytest.mark.parametrize("single_group_metadata", [True, False])
def test_gateway_http_contract(monkeypatch, single_group_metadata):
    """Exercise actual CanvasAPI serialization/pagination with mocked HTTP."""
    import json
    import requests
    requests_seen = []
    monkeypatch.setattr("app.canvas_gateway.classify", lambda exc: exc)
    def request(self, method, url, **kwargs):
        requests_seen.append((method, url, kwargs))
        if url.endswith("/users/self"):
            data = {"id": 99, "name": "Demo TA"}
        elif url.endswith("/courses/10/permissions"):
            data = {"manage_grades": True}
        elif url.endswith("/courses/10"):
            data = {"id": 10, "name": "Course"}
        elif url.endswith("/assignments/20"):
            data = {"id": 20, "course_id": 10, "name": "Test", "points_possible": 10, "group_category_id": 100,
                    "grade_group_students_individually": False, "published": True, "grading_type": "points"}
        elif url.endswith("/group_categories/100/groups"):
            data = [{"id": 7, "name": "Assignment Group 1", "group_category_id": 100}]
        elif url.endswith("/group_categories/100"):
            data = {"id": 100}
        elif url.endswith("/groups/7/users"):
            data = [{"id": 1, "name": "Student A"}]
        elif url.endswith("/groups/7"):
            data = {"id": 7, "name": "Assignment Group 1", "group_category_id": 100}
        elif url.endswith("/submissions"):
            data = [{"id": 81, "user_id": 1, "assignment_id": 20, "group": {"id": 7}, "score": None}]
        elif url.endswith("/submissions/1"):
            data = {"id": 81, "user_id": 1, "assignment_id": 20, "score": 8.5}
            if single_group_metadata:
                data["group"] = {"id": 7}
        else:
            raise AssertionError(url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(data).encode()
        response.url = url
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    gateway = CanvasGateway("https://oc.sjtu.edu.cn", "synthetic")
    assert gateway.identity()["id"] == 99
    assert gateway.snapshot(10, 20)["groups"][0]["id"] == 7
    requests_seen.clear()
    gateway.grade(10, 20, 1, "8.5")
    gateway.comment(10, 20, 1, "E1: -1.5, Reason.")
    assert len(requests_seen) == 2  # Writes no longer incur three preparatory GETs each.
    writes = [r for r in requests_seen if r[0].upper() == "PUT"]
    assert dict(writes[0][2]["data"])["submission[posted_grade]"] == "8.5"
    assert dict(writes[1][2]["data"])["comment[group_comment]"] == "true"
    assert all(r[2]["allow_redirects"] is False and r[2]["timeout"] == (10, 25) for r in requests_seen)
    requests_seen.clear()
    assert gateway.snapshot(10, 20, group_id=7)["submissions"]["1"]["group"]["id"] == 7
    assert gateway._single_submission_group == single_group_metadata
    requests_seen.clear()
    gateway.snapshot(10, 20, group_id=7)
    assert len(requests_seen) == 4  # Assignment, group, members, one paginated list.
    if single_group_metadata:
        assert not any(url.endswith('/submissions') for _, url, _ in requests_seen)
    else:
        assert not any(url.endswith('/submissions/1') for _, url, _ in requests_seen)


def test_gateway_permission_error_has_no_response_body(monkeypatch):
    import pytest
    gateway = CanvasGateway("https://oc.sjtu.edu.cn", "synthetic")
    def forbidden():
        raise ce.Forbidden("private server details")
    with pytest.raises(CanvasFailure) as error:
        gateway.call(forbidden)
    assert not error.value.uncertain
    assert "private" not in str(error.value)


def test_read_network_error_distinguishes_blocked_socket():
    import pytest
    import requests
    gateway = CanvasGateway("https://oc.sjtu.edu.cn", "synthetic")
    def blocked():
        raise requests.exceptions.ConnectionError("WinError 10013 secret-token")
    with pytest.raises(CanvasFailure) as error:
        gateway.call(blocked)
    assert "10013" in str(error.value)
    assert "没有提交成绩" in str(error.value)
    assert "secret-token" not in str(error.value)
    assert not error.value.uncertain


def test_write_timeout_still_requires_verification():
    import pytest
    import requests
    gateway = CanvasGateway("https://oc.sjtu.edu.cn", "synthetic")
    def timeout():
        raise requests.exceptions.Timeout("secret-token")
    with pytest.raises(CanvasFailure) as error:
        gateway.call(timeout, read=False)
    assert error.value.uncertain
    assert "超时" in str(error.value)
    assert "secret-token" not in str(error.value)


def test_pagination_uses_all_pages(monkeypatch):
    import json
    import requests
    seen = []
    def request(self, method, url, **kwargs):
        seen.append(url)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        page = 2 if "page=2" in url else 1
        response._content = json.dumps([{"id": page, "name": f"Course {page}"}]).encode()
        if page == 1:
            response.headers["Link"] = '<https://oc.sjtu.edu.cn/api/v1/courses?page=2>; rel="next"'
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    courses = CanvasGateway("https://oc.sjtu.edu.cn", "synthetic").courses()
    assert [c["id"] for c in courses] == [1, 2]
    assert len(seen) == 2
