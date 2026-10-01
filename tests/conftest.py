import copy

import pytest

from app.canvas_gateway import CanvasFailure
from app.store import Store


class FakeCanvas:
    def __init__(self, base_url="https://oc.sjtu.edu.cn", token="test-only"):
        self.base_url = base_url
        self.user_id = 99
        self.grade_calls = []
        self.comment_calls = []
        self.fail_grade = None
        self.fail_comment = None
        self.read_failure = False
        self.data = {
            "assignment": {"id": 20, "name": "Assignment 1", "points_possible": 10,
                           "group_category_id": 100, "grade_group_students_individually": False,
                           "grading_type": "points", "published": True, "moderated_grading": False,
                           "anonymous_grading": False},
            "groups": [{"id": 7, "name": "Assignment Group 1", "members": [{"id": 1, "name": "Student A"}, {"id": 2, "name": "Student B"}]},
                       {"id": 8, "name": "Assignment Group 2", "members": [{"id": 3, "name": "Student C"}]}],
            "submissions": {str(i): {"user_id": i, "score": None, "attempt": 1,
                                     "group": {"id": 7 if i < 3 else 8}, "comments": []}
                            for i in (1, 2, 3)}}

    def identity(self):
        return {"id": self.user_id, "name": "Demo TA"}

    def courses(self):
        return [{"id": 10, "name": "Demo course"}]

    def assignments(self, course):
        return [copy.deepcopy(self.data["assignment"])]

    def snapshot(self, course, assignment, group_id=None):
        if self.read_failure:
            raise CanvasFailure("read interrupted", uncertain=True)
        return copy.deepcopy(self.data)

    def grade(self, course, assignment, user, score):
        self.grade_calls.append((user, score))
        if self.fail_grade == "before":
            raise CanvasFailure("permission denied")
        group = self.data["submissions"][str(user)]["group"]
        for sub in self.data["submissions"].values():
            if sub["group"] == group:
                sub["score"] = float(score)
        if self.fail_grade == "after":
            raise CanvasFailure("timeout after applied", uncertain=True)

    def comment(self, course, assignment, user, body):
        self.comment_calls.append((user, body))
        if self.fail_comment == "before":
            raise CanvasFailure("rate limit")
        if self.fail_comment == "unknown":
            raise CanvasFailure("timeout", uncertain=True)
        group = self.data["submissions"][str(user)]["group"]
        comment = {"id": len(self.comment_calls) + 100, "author_id": self.user_id, "comment": body}
        for sub in self.data["submissions"].values():
            if sub["group"] == group:
                clone = copy.deepcopy(comment)
                clone["id"] = comment["id"] * 10 + sub["user_id"]
                sub["comments"].append(clone)
        if self.fail_comment == "after":
            raise CanvasFailure("timeout after applied", uncertain=True)
        if self.fail_comment == "unreadable":
            self.read_failure = True


@pytest.fixture
def gateway():
    return FakeCanvas()


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "test.sqlite3")
    yield value
    value.close()
