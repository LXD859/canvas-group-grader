"""CanvasAPI boundary. No automatic retries for grade/comment writes."""
import logging
import time
from urllib.parse import urlsplit

import requests
from canvasapi import Canvas
from canvasapi import exceptions as ce
from canvasapi.course import Course
from canvasapi.submission import Submission

from .parsing import InputError, number

# CanvasAPI debug messages can contain request arguments. Never log them.
logging.getLogger("canvasapi").disabled = True
logging.getLogger("canvasapi.requester").disabled = True


class CanvasFailure(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def classify(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return CanvasFailure("无法验证 Canvas 的 HTTPS 证书，请检查本机证书或代理配置。", uncertain=True)
    if isinstance(exc, requests.exceptions.ProxyError):
        return CanvasFailure("无法连接配置的网络代理，请检查代理设置。", uncertain=True)
    if isinstance(exc, requests.exceptions.Timeout):
        return CanvasFailure("连接 Canvas 超时，请检查网络或校园 VPN 后重试。", uncertain=True)
    if isinstance(exc, requests.exceptions.ConnectionError):
        # Inspect locally without returning exception text, URLs or credentials.
        if "10013" in str(exc):
            return CanvasFailure("系统阻止了服务访问网络（Windows 10013）。请从本机 Anaconda Prompt 启动服务，或检查防火墙权限。", uncertain=True)
        return CanvasFailure("无法连接 Canvas，请检查网络、站点地址及校园 VPN。", uncertain=True)
    if isinstance(exc, (requests.exceptions.JSONDecodeError, ValueError)):
        return CanvasFailure("Canvas 返回的内容无法解析，请检查站点地址或登录网关。", uncertain=True)
    if isinstance(exc, ce.RateLimitExceeded):
        return CanvasFailure("Canvas 请求限流，请稍后重新预览。")
    if isinstance(exc, (ce.Unauthorized, ce.Forbidden, ce.InvalidAccessToken)):
        return CanvasFailure("Canvas 拒绝访问，请检查 Token 和课程评分权限。")
    if isinstance(exc, ce.ResourceDoesNotExist):
        return CanvasFailure("Canvas 资源不存在或当前账号无权访问。")
    if isinstance(exc, ce.BadRequest):
        return CanvasFailure("Canvas 拒绝该请求，请检查作业设置和评分范围。")
    return CanvasFailure("Canvas 请求中断或响应异常，需回读核验。", uncertain=True)


def validate_assignment(assignment):
    if not assignment.get("group_category_id"):
        raise InputError("该作业未设置学生小组提交。")
    if assignment.get("grade_group_students_individually") is not False:
        raise InputError("该作业启用了个人评分；第一版仅支持小组共享成绩。")
    if assignment.get("grading_type") not in {"points", "percent"}:
        raise InputError("第一版仅支持分数或百分比评分的作业。")
    if assignment.get("moderated_grading") or assignment.get("anonymous_grading"):
        raise InputError("第一版不支持审核评分或匿名评分作业。")
    if not assignment.get("published"):
        raise InputError("请先在 Canvas 发布该作业。")
    number(assignment.get("points_possible"))


class BoundedSession(requests.Session):
    def __init__(self, base_url):
        super().__init__()
        self.origin = urlsplit(base_url)

    def request(self, method, url, **kwargs):
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (self.origin.scheme, self.origin.netloc):
            raise InputError("Canvas 返回了站外接口地址，已停止请求。")
        kwargs["timeout"] = (10, 25)
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise InputError("Canvas 接口发生重定向，请检查站点地址。")
        return response


class CanvasGateway:
    def __init__(self, base_url, token):
        url = urlsplit(base_url.strip())
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise InputError("Canvas 地址必须是 HTTPS 站点根地址，例如 https://oc.sjtu.edu.cn。")
        self.base_url = f"https://{url.netloc}"
        self.canvas = Canvas(self.base_url, token)
        # Isolated compatibility hook for pinned CanvasAPI 3.5.0.
        self.canvas._Canvas__requester._session.close()
        self.canvas._Canvas__requester._session = BoundedSession(self.base_url)
        self.user_id = None
        self._single_submission_group = True

    def close(self):
        requester = self.canvas._Canvas__requester
        requester.access_token = ""
        requester._cache.clear()
        requester._session.cookies.clear()
        requester._session.close()

    def call(self, action, read=True):
        for attempt in range(3 if read else 1):
            try:
                return action()
            except InputError:
                raise
            except Exception as exc:
                if read and isinstance(exc, ce.RateLimitExceeded) and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                failure = classify(exc)
                if read:
                    failure = CanvasFailure(str(failure).replace("，需回读核验", "") + " 此步骤仅读取数据，没有提交成绩。")
                raise failure from None

    def identity(self):
        user = self.call(lambda: self.canvas.get_user("self"))
        self.user_id = user.id
        return {"id": user.id, "name": getattr(user, "name", str(user.id))}

    def courses(self):
        courses = self.call(lambda: list(self.canvas.get_courses(enrollment_state="active")))
        return [{"id": x.id, "name": getattr(x, "name", str(x.id))}
                for x in courses if hasattr(x, "name")]

    def assignments(self, course_id):
        values = self.call(lambda: list(self.canvas.get_course(course_id).get_assignments()))
        return [{"id": a.id, "name": a.name, "points_possible": getattr(a, "points_possible", None),
                 "group_category_id": getattr(a, "group_category_id", None)} for a in values]

    def snapshot(self, course_id, assignment_id, group_id=None):
        def read():
            requester = self.canvas._Canvas__requester
            course = Course(requester, {"id": course_id})
            if group_id is None:
                permissions = requester.request(
                    "GET", f"courses/{course_id}/permissions", _kwargs=[("permissions[]", "manage_grades")]
                ).json()
                if not permissions.get("manage_grades"):
                    raise InputError("当前账号没有该课程的 manage_grades 评分权限。")
            assignment = course.get_assignment(assignment_id)
            meta = {key: getattr(assignment, key, None) for key in (
                "id", "name", "points_possible", "group_category_id", "grade_group_students_individually",
                "grading_type", "published", "moderated_grading", "anonymous_grading")}
            validate_assignment(meta)
            groups = []
            if group_id is None:
                category = self.canvas.get_group_category(meta["group_category_id"])
                target_groups = list(category.get_groups(per_page=100))
            else:
                target_groups = [self.canvas.get_group(group_id)]
            for group in target_groups:
                if getattr(group, "group_category_id", None) != meta["group_category_id"]:
                    raise InputError("小组已不属于该作业的小组集。")
                members = [{"id": u.id, "name": getattr(u, "name", str(u.id))} for u in group.get_users(per_page=100)]
                groups.append({"id": group.id, "name": group.name.strip(),
                               "members": sorted(members, key=lambda u: u["id"])})
            submissions = {}
            if group_id is None or not self._single_submission_group:
                values = assignment.get_submissions(include=["submission_comments", "group"], per_page=100)
            else:
                values = [assignment.get_submission(m["id"], include=["submission_comments", "group"])
                          for g in groups for m in g["members"]]
                # Older Canvas deployments omit group metadata from single-submission responses.
                # Keep the authoritative list endpoint as a compatibility fallback.
                if any(not hasattr(sub, "group") for sub in values):
                    self._single_submission_group = False
                    values = assignment.get_submissions(include=["submission_comments", "group"], per_page=100)
            for sub in values:
                submissions[str(sub.user_id)] = {
                    "user_id": sub.user_id,
                    "score": getattr(sub, "score", None),
                    "entered_score": getattr(sub, "entered_score", None),
                    "attempt": getattr(sub, "attempt", None),
                    "graded_at": getattr(sub, "graded_at", None),
                    "excused": getattr(sub, "excused", False),
                    "points_deducted": getattr(sub, "points_deducted", None),
                    "late_policy_status": getattr(sub, "late_policy_status", None),
                    "group": getattr(sub, "group", None),
                    "comments": getattr(sub, "submission_comments", []),
                }
            return {"assignment": meta, "groups": groups, "submissions": submissions}
        return self.call(read)

    def grade(self, course_id, assignment_id, user_id, score):
        def write():
            submission = Submission(self.canvas._Canvas__requester,
                                    {"course_id": course_id, "assignment_id": assignment_id, "user_id": user_id})
            return submission.edit(submission={"posted_grade": score}, prefer_points_over_scheme=True)
        return self.call(write, read=False)

    def comment(self, course_id, assignment_id, user_id, body):
        def write():
            submission = Submission(self.canvas._Canvas__requester,
                                    {"course_id": course_id, "assignment_id": assignment_id, "user_id": user_id})
            return submission.edit(comment={"text_comment": body, "group_comment": True})
        return self.call(write, read=False)
