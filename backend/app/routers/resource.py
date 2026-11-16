"""资源配置接口：维护资源池、回填准入、并发占用与积压队列。

权限口径：
- 只读成员（X-User-Role: readonly）只允许 GET，容量信息对其可见；
- 配置类写操作按资源池归属部门判定，跨部门修改直接 403；
- 回填准入的跨部门拒绝在业务层留痕（生成「已拒绝」积压记录），便于审计。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.resource import READONLY_ROLE, ResourceService, resolve_department

router = APIRouter(prefix="/api/resource", tags=["资源配置"])

service = ResourceService()

LIST_FIELDS = ["池编号", "池名称", "最大并发数", "内存上限MB", "CPU核心数", "队列策略", "抢占开关", "作业白名单"]
STATUSES = ["草稿", "配置评审中", "已启用", "已停用"]


def _department(x_department_id: str | None) -> str:
    return resolve_department(x_department_id)


def _ensure_writer(role: str | None) -> None:
    if (role or "").strip().lower() == READONLY_ROLE:
        raise HTTPException(status_code=403, detail="只读成员仅可查看容量信息，不能修改资源配置或提交回填")


def _ensure_pool_department(entry_id: int, department: str) -> dict[str, Any]:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"资源池 {entry_id} 不存在或已归档")
    if str(entry.get("归属部门") or "") != department:
        raise HTTPException(
            status_code=403,
            detail=f"资源池归属 {entry.get('归属部门')}，当前部门 {department} 无权跨部门修改",
        )
    return entry


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按池编号检索"),
    status: str | None = Query(default=None, description="草稿、配置评审中、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按池编号与状态过滤资源配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/occupancy", response_model=dict)
def list_occupancy() -> dict[str, Any]:
    """并发占用数据记录：两个来源（运行实例、回填积压）重算后的统一口径。"""
    return {"module": "resource_occupancy", "items": service.list_occupancy()}


@router.get("/backlog", response_model=PageResult[dict])
def list_backlog(
    pool_id: int | None = Query(default=None, description="按资源池id过滤回填积压"),
    status: str | None = Query(default=None, description="排队中、运行中、已完成、已终止、已拒绝"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """数据回填积压队列，支持按池与状态过滤。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_backlog(pool_id=pool_id, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/backlog", response_model=ActionResult)
def submit_backlog(
    payload: EntryPayload,
    x_department_id: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> ActionResult:
    """回填作业准入：停用池拒绝、白名单校验、槽位争用与重复提交幂等。"""
    _ensure_writer(x_user_role)
    item, message = service.submit_backfill(payload.values, department=_department(x_department_id))
    if item is None:
        return ActionResult(ok=False, message=message)
    ok = item.get("状态") != "已拒绝"
    return ActionResult(ok=ok, message=message, entry=item)


@router.post("/recompute", response_model=ActionResult)
def recompute_occupancy(
    x_user_role: str | None = Header(default=None),
) -> ActionResult:
    """从资源池配置与回填积压数据重新校验并重算并发占用，覆盖旧记录。"""
    _ensure_writer(x_user_role)
    records = service.rebuild_occupancy()
    return ActionResult(ok=True, message=f"已重算 {len(records)} 个资源池的并发占用记录", entry={"items": records})


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出资源配置清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "resource", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条资源池明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"资源池 {entry_id} 不存在或已归档")
    result = dict(entry)
    result["并发占用记录"] = service.get_occupancy(entry_id)
    return result


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    x_department_id: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> ActionResult:
    """登记一条资源池，参数（并发/内存/CPU必须为正整数）不合法时说明原因。"""
    _ensure_writer(x_user_role)
    values = dict(payload.values)
    values.setdefault("归属部门", _department(x_department_id))
    entry, missing = service.create_entry(values)
    if missing:
        return ActionResult(ok=False, message=f"校验未通过：{'、'.join(missing)}")
    return ActionResult(ok=True, message="资源池已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(
    entry_id: int,
    payload: EntryPayload,
    x_department_id: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> ActionResult:
    """修改资源池参数与白名单：正整数校验、配置版本递增、跨部门修改拒绝。"""
    _ensure_writer(x_user_role)
    department = _department(x_department_id)
    _ensure_pool_department(entry_id, department)
    entry, message = service.update_entry(entry_id, payload.values)
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
    """提交配置评审、启用配置、停用配置；停用后新回填作业一律拒绝。"""
    _ensure_writer(x_user_role)
    _ensure_pool_department(entry_id, _department(x_department_id))
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
