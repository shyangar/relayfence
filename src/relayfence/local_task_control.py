"""Local task controls wait for owned process teardown before continuing work."""

from .errors import CoordinationError
from .models import MeetingControl, OfficeTransfer, TaskControl


def respond_decision(manager, payload):
    from .models import DecisionRespond

    request = DecisionRespond.model_validate(payload)
    dispatcher = manager.dispatcher_for(request.project)
    if dispatcher is None:
        raise ValueError("请先连接该项目")
    with manager.service.store.connection() as db:
        decision = db.execute(
            "SELECT * FROM decisions WHERE project=? AND decision_id=?",
            (request.project, request.decision_id),
        ).fetchone()
        ids = []
        if decision:
            ids = (
                [
                    row[0]
                    for row in db.execute(
                        "SELECT task_id FROM meeting_tasks WHERE project=? AND meeting_id=?",
                        (request.project, decision["meeting_id"]),
                    )
                ]
                if decision["meeting_id"]
                else [decision["task_id"]]
            )
    if request.action != "cancel" and any(
        dispatcher.task_running(request.project, tid) for tid in ids
    ):
        raise ValueError("旧执行仍在退出，请稍后确认继续")
    result, code = manager.service.call("decision_respond", request.model_dump(mode="json"))
    if code >= 400:
        error = result["error"]
        raise CoordinationError(error["code"], error["message"], error.get("details"), code)
    if request.action == "cancel":
        for tid in ids:
            dispatcher.interrupt_task(request.project, tid)
    return result["data"]


def control(manager, payload):
    transfer = payload.get("action") == "handoff"
    fields = {k: v for k, v in payload.items() if k != "action"} if transfer else payload
    request = (OfficeTransfer if transfer else TaskControl).model_validate(fields)
    with manager._lock:
        dispatcher = manager.dispatcher_for(request.project)
    if dispatcher is None:
        raise ValueError("请先进入该项目以连接本地执行环境")
    action = payload["action"]
    if action in ("resume", "retry", "handoff") and dispatcher.task_running(
        request.project, request.task_id
    ):
        raise ValueError("原执行仍在退出，请稍后再继续或交接")
    operation = "office_transfer" if transfer else "task_control"
    result, code = manager.service.call(operation, request.model_dump(mode="json"))
    if code >= 400:
        error = result["error"]
        raise CoordinationError(error["code"], error["message"], error.get("details"), code)
    if action in ("pause", "cancel"):
        future = dispatcher.interrupt_task(request.project, request.task_id)
        if future:
            try:
                future.result(timeout=30)
            except TimeoutError:
                return {"task": result["data"], "stopping": True}
            except Exception:
                if not future.done():
                    raise
    return {"task": result["data"], "stopping": False}


def delete(manager, payload):
    """Permanently remove a task, refusing while its worker still holds the task."""
    from .models import TaskDelete

    request = TaskDelete.model_validate(payload)
    with manager._lock:
        dispatcher = manager.dispatcher_for(request.project)
    if dispatcher is None:
        raise ValueError("请先进入该项目以连接本地执行环境")
    if dispatcher.task_running(request.project, request.task_id):
        raise ValueError("任务仍在执行，请先取消并等待进程退出后再删除")
    result, code = manager.service.call("task_delete", request.model_dump(mode="json"))
    if code >= 400:
        error = result["error"]
        raise CoordinationError(error["code"], error["message"], error.get("details"), code)
    return result["data"]


def control_meeting(manager, payload):
    request = MeetingControl.model_validate(payload)
    dispatcher = manager.dispatcher_for(request.project)
    if dispatcher is None:
        raise ValueError("请先连接该项目")
    if request.action == "resume":
        with manager.service.store.connection() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT task_id FROM meeting_tasks WHERE project=? AND meeting_id=?",
                    (request.project, request.meeting_id),
                )
            ]
        if any(dispatcher.task_running(request.project, tid) for tid in ids):
            raise ValueError("旧执行仍在退出，请稍后继续会议")
    result, code = manager.service.call("meeting_control", request.model_dump(mode="json"))
    if code >= 400:
        error = result["error"]
        raise CoordinationError(error["code"], error["message"], error.get("details"), code)
    stopping = False
    if request.action == "pause":
        futures = [
            dispatcher.interrupt_task(request.project, tid) for tid in result["data"]["task_ids"]
        ]
        for future in futures:
            if future:
                try:
                    future.result(timeout=10)
                except TimeoutError:
                    stopping = True
                except Exception:
                    if not future.done():
                        raise
    return {**result["data"], "stopping": stopping}
