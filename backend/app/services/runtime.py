"""运行实例业务规则：状态流转、回填准入复核与跨模块进度同步。

启动一个挂接资源池的运行实例前，必须读取资源池的并发占用数据记录，重新做一次
「已启用 + 白名单 + 剩余槽位」准入复核；回填作业记住准入时的配置版本，
运行实例与回填积压通过同一配置版本保持跨模块口径一致。
"""
from __future__ import annotations

from typing import Any

from app.services.locking import resource_lock
from app.services.resource import ResourceService
from app.store import store

MODULE = "runtime"
REQUIRED_FIELDS = ["记录编号", "作业名称", "开始时间"]
STATUS_ORDER = ["草稿", "运行中", "成功", "失败", "已终止"]
ACTION_RULES = {"启动作业": "运行中", "终止作业": "已终止", "标记成功": "成功"}
NEGATIVE_ACTIONS = []
TERMINAL_STATUSES = ["成功", "失败", "已终止"]

# 与资源配置模块共用同一个无状态服务实例（状态都在 store 与锁里）
resource_service = ResourceService()


class RuntimeService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with resource_lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 挂接资源池的运行记录才参与回填准入；历史记录无该字段，保持原行为
            pool_id = values.get("资源池id")
            if str(pool_id or "").strip() != "":
                try:
                    entry["资源池id"] = int(pool_id)
                except (TypeError, ValueError):
                    return None, [f"资源池id必须为整数，收到：{pool_id}"]
            entry["进度"] = 0
            entry["配置版本"] = None
            entry["回填编号"] = None
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "启动作业":
            return self.start_entry(entry_id)
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"运行记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于运行实例可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with resource_lock:
            previous = entry.get("status")
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            backlog_code = entry.get("回填编号")
            if previous == "运行中" and target in TERMINAL_STATUSES and backlog_code:
                # 已在运行：释放回填槽位，并把配置版本随回填记录归档
                self._release_linked_backfill(entry, target)
            elif target in TERMINAL_STATUSES and backlog_code:
                # 尚未启动（排队中）就被终止：同步撤销积压队列里的回填，避免孤儿排队记录
                self._cancel_queued_backfill(backlog_code)
            return entry, f"运行记录已{action}"

    def _cancel_queued_backfill(self, backlog_code: str) -> None:
        for item in store.rows("resource_backlog"):
            if item.get("回填编号") == backlog_code and item.get("状态") == "排队中":
                resource_service.release_backfill(int(item["id"]), "已终止")
                break

    def start_entry(
        self, entry_id: int, *, department: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """启动前读取并发占用数据并复核准入；只有白名单内且资源池已启用才能启动。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"运行记录 {entry_id} 不存在或已归档"
        if entry.get("status") == "运行中":
            return entry, "运行实例已在运行中，启动动作不重复生效"
        if entry.get("status") in TERMINAL_STATUSES:
            return None, f"运行实例已终结（{entry.get('status')}），不能再次启动"
        pool_id = entry.get("资源池id")
        if not pool_id:
            # 未挂接资源池的历史实例保持原有启动语义
            with resource_lock:
                entry["status"] = "运行中"
                entry["pending"] = True
            return entry, "运行记录已启动作业"
        with resource_lock:
            outcome, backlog, message = resource_service.admit_runtime(
                entry, department=department
            )
        if outcome == "rejected":
            return None, message
        if outcome == "queued":
            entry["回填编号"] = backlog.get("回填编号")
            return None, message
        # outcome == "running"：准入通过，回填与占用记录已落库
        entry["status"] = "运行中"
        entry["pending"] = True
        entry["回填编号"] = backlog.get("回填编号")
        entry["配置版本"] = backlog.get("准入配置版本")
        return entry, message

    def update_progress(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """更新回填进度，并把进度与配置版本同步到回填积压记录。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"运行记录 {entry_id} 不存在或已归档"
        with resource_lock:
            for field in ("进度", "读取行数", "写入行数"):
                if field in values and str(values.get(field) or "").strip() != "":
                    try:
                        number = int(values[field])
                    except (TypeError, ValueError):
                        return None, f"{field}必须为整数，收到：{values[field]}"
                    if field == "进度":
                        number = max(0, min(100, number))
                    elif number < 0:
                        return None, f"{field}不能为负数，收到：{number}"
                    entry[field] = number
            backlog_code = entry.get("回填编号")
            if backlog_code:
                for item in store.rows("resource_backlog"):
                    if item.get("回填编号") == backlog_code:
                        item["进度"] = entry.get("进度", item.get("进度", 0))
                        item["读取行数"] = entry.get("读取行数", item.get("读取行数", 0))
                        item["写入行数"] = entry.get("写入行数", item.get("写入行数", 0))
                        # 跨模块沿用同一份准入配置版本
                        entry["配置版本"] = item.get("准入配置版本")
                        break
            return entry, "回填进度已更新并同步到积压记录"

    def _release_linked_backfill(self, entry: dict[str, Any], target: str) -> None:
        backlog_code = entry.get("回填编号")
        terminal = "已完成" if target == "成功" else "已终止"
        for item in store.rows("resource_backlog"):
            if item.get("回填编号") == backlog_code:
                resource_service.release_backfill(
                    int(item["id"]), terminal, progress=entry.get("进度")
                )
                entry["配置版本"] = item.get("准入配置版本")
                break
