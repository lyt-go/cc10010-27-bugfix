"""运行实例接口：维护运行记录，启动前复核资源池准入，进度同步回填积压。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.resource import READONLY_ROLE, resolve_department
from app.services.runtime import RuntimeService

router = APIRouter(prefix="/api/runtime", tags=["运行实例"])

service = RuntimeService()

LIST_FIELDS = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "读取行数", "写入行数", "错误信息"]
STATUSES = ["草稿", "运行中", "成功", "失败", "已终止"]


def _department(x_department_id: str | None) -> str:
    return resolve_department(x_department_id)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、成功、失败、已终止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤运行实例列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出运行实例清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "runtime", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条运行记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    x_user_role: str | None = Header(default=None),
) -> ActionResult:
    """登记一条运行记录，缺字段时说明原因而不是静默丢弃。"""
    if (x_user_role or "").strip().lower() == READONLY_ROLE:
        raise HTTPException(status_code=403, detail="只读成员仅可查看运行与容量信息，不能登记运行记录")
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="运行记录已登记", entry=entry)


@router.post("/{entry_id}/progress", response_model=ActionResult)
def update_progress(
    entry_id: int,
    payload: EntryPayload,
    x_user_role: str | None = Header(default=None),
) -> ActionResult:
    """运行实例拿到并发占用数据后更新回填进度，并同步到积压记录。"""
    if (x_user_role or "").strip().lower() == READONLY_ROLE:
        raise HTTPException(status_code=403, detail="只读成员不能更新回填进度")
    entry, message = service.update_progress(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    x_department_id: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> ActionResult:
    """启动作业（含资源池准入复核）、终止作业、标记成功。"""
    if (x_user_role or "").strip().lower() == READONLY_ROLE:
        raise HTTPException(status_code=403, detail="只读成员不能执行运行动作")
    action = str(payload.values.get("action") or "").strip()
    if action == "启动作业":
        entry, message = service.start_entry(entry_id, department=_department(x_department_id))
    else:
        entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
