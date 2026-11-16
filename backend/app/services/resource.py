"""资源配置业务规则：参数校验、白名单准入、回填积压与并发占用重算。

设计要点：
- 最大并发数 / 内存上限MB / CPU核心数只接受正整数，零、负数与非数字一律拒绝；
  历史脏数据在装载时兜底修复，保证展示与计算永远不会出现负数。
- 资源池配置带单调递增的「配置版本」，白名单、停用状态等准入快照随版本固化，
  回填作业记住自己准入时的版本，配置后到的变更不会改写已准入作业的判定依据。
- 回填准入（停用拒绝、白名单、槽位争用、幂等）全部在 resource_lock 内完成，
  多个作业争用最后一个槽位时只有一个能成功。
- 并发占用记录由「运行实例」与「回填积压」两个来源分别取数后合并重算，
  取两者较大值并覆盖旧记录，兼容历史占用数据。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.locking import resource_lock
from app.store import store

MODULE = "resource"
RUNTIME_MODULE = "runtime"
BACKLOG_MODULE = "resource_backlog"
OCCUPANCY_MODULE = "resource_occupancy"

REQUIRED_FIELDS = ["池编号", "池名称", "最大并发数", "内存上限MB", "CPU核心数"]
NUMERIC_FIELDS = ["最大并发数", "内存上限MB", "CPU核心数"]
NUMERIC_DEFAULTS = {"最大并发数": 1, "内存上限MB": 1024, "CPU核心数": 1}
QUEUE_STRATEGIES = ["FIFO", "优先级"]
DEFAULT_DEPARTMENT = "数据平台部"
DEFAULT_QUEUE = "FIFO"
# 只读角色标识（通过 X-User-Role 请求头传入），只读成员共享容量信息但不能写入
READONLY_ROLE = "readonly"
# HTTP 头只能承载 ASCII，部门以编码传递，服务端解析为中文名；未知编码原样透传。
DEPARTMENT_DIRECTORY = {
    "DATA_PLATFORM": "数据平台部",
    "RISK_DATA": "风控数据部",
    "DATA_INFRA": "数据基础设施部",
}


def resolve_department(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_DEPARTMENT
    return DEPARTMENT_DIRECTORY.get(text.upper(), text)

STATUS_ORDER = ["草稿", "配置评审中", "已启用", "已停用"]
ACTION_RULES = {"提交配置评审": "配置评审中", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]
ENABLED_STATUS = "已启用"

# 回填作业生命周期；前三个为未终结态，参与幂等判定。
BACKLOG_PENDING_STATES = ["排队中", "运行中"]
BACKLOG_TERMINAL_STATES = ["已完成", "已终止", "已拒绝"]
RUNTIME_RUNNING = "运行中"
RUNTIME_TERMINAL = ["成功", "失败", "已终止"]


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_positive_int(value: Any, field_name: str) -> tuple[int | None, str | None]:
    """把入参收敛成正整数；零、负数、空值、非数字都给出可读错误。"""
    if isinstance(value, bool):
        return None, f"{field_name}必须为正整数，不能是布尔值"
    if isinstance(value, int):
        number = value
    else:
        text = str(value if value is not None else "").strip()
        if not text:
            return None, f"{field_name}必须为正整数"
        try:
            number = int(text)
        except (TypeError, ValueError):
            return None, f"{field_name}必须为有效整数，收到：{text}"
    if number <= 0:
        return None, f"{field_name}必须大于0，收到：{number}"
    return number, None


def parse_whitelist(value: Any) -> list[str]:
    """白名单兼容历史字符串（逗号/分号/顿号/换行分隔）与数组两种存法。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        text = str(value).replace("，", ",").replace("；", ",").replace(";", ",")
        text = text.replace("、", ",").replace("\n", ",").replace(" ", "")
        raw_items = text.split(",")
    result: list[str] = []
    for item in raw_items:
        name = str(item).strip()
        if name and name not in result:
            result.append(name)
    return result


def parse_switch(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().lower() in {
        "1", "true", "yes", "on", "开", "开启", "是",
    }


class ResourceService:
    def __init__(self) -> None:
        # 装载期先修复历史脏数据，再重算占用，保证服务一就绪数据就是自洽的。
        self.repair_legacy_rows()
        self.rebuild_occupancy()

    # ------------------------------------------------------------------ 基础查询
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
            rows = [row for row in rows if keyword in str(row.get("池编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_backlog(
        self,
        *,
        pool_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(BACKLOG_MODULE)
        if pool_id is not None:
            rows = [row for row in rows if int(row.get("资源池id", 0)) == pool_id]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_occupancy(self, entry_id: int) -> dict[str, Any] | None:
        with resource_lock:
            record = store.find(OCCUPANCY_MODULE, entry_id)
            pool = store.find(MODULE, entry_id)
            if record is None and pool is not None:
                # 存储被重置或历史无记录时按需重算，保证读取口径始终可用
                record = self._recompute_pool_locked(pool)
            return record

    def list_occupancy(self) -> list[dict[str, Any]]:
        with resource_lock:
            for pool in store.rows(MODULE):
                if store.find(OCCUPANCY_MODULE, int(pool["id"])) is None:
                    self._recompute_pool_locked(pool)
            return sorted(store.rows(OCCUPANCY_MODULE), key=lambda row: int(row.get("id", 0)))

    # ---------------------------------------------------------- 历史数据兼容修复
    def repair_legacy_rows(self) -> None:
        """把历史资源池数据修复成可计算的形态：数值字段正整数化、白名单数组化。"""
        with resource_lock:
            for row in store.rows(MODULE):
                for field in NUMERIC_FIELDS:
                    number, error = to_positive_int(row.get(field), field)
                    if error:
                        # 零/负数/字符串占位等脏数据兜底为安全默认值，绝不把负数留给页面与计算
                        row[field] = NUMERIC_DEFAULTS[field]
                        row.setdefault("修复说明", []).append(f"{field}原值非法，已兜底")
                    else:
                        row[field] = number
                row["作业白名单"] = parse_whitelist(row.get("作业白名单"))
                if not isinstance(row.get("抢占开关"), bool):
                    row["抢占开关"] = parse_switch(row.get("抢占开关"))
                if not isinstance(row.get("配置版本"), int) or int(row.get("配置版本", 0)) <= 0:
                    row["配置版本"] = 1
                if not str(row.get("归属部门") or "").strip():
                    row["归属部门"] = DEFAULT_DEPARTMENT
                if str(row.get("队列策略") or "").strip() not in QUEUE_STRATEGIES:
                    row["队列策略"] = DEFAULT_QUEUE
            for item in store.rows(BACKLOG_MODULE):
                item["作业白名单快照"] = parse_whitelist(item.get("作业白名单快照"))
                if not isinstance(item.get("配置版本"), int):
                    item["配置版本"] = 1

    # -------------------------------------------------------------- 登记与配置变更
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with resource_lock:
            pool_code = str(values.get("池编号") or "").strip()
            if any(str(row.get("池编号")) == pool_code for row in store.rows(MODULE)):
                return None, [f"池编号 {pool_code} 已存在，不能重复登记"]
            numeric: dict[str, int] = {}
            for field in NUMERIC_FIELDS:
                number, error = to_positive_int(values.get(field), field)
                if error:
                    return None, [error]
                numeric[field] = number
            queue = str(values.get("队列策略") or DEFAULT_QUEUE).strip()
            if queue not in QUEUE_STRATEGIES:
                return None, [f"队列策略仅支持：{'、'.join(QUEUE_STRATEGIES)}"]
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry["池编号"] = pool_code
            entry["池名称"] = str(values.get("池名称") or "").strip()
            entry.update(numeric)
            entry["队列策略"] = queue
            entry["抢占开关"] = parse_switch(values.get("抢占开关"))
            entry["作业白名单"] = parse_whitelist(values.get("作业白名单"))
            entry["归属部门"] = str(values.get("归属部门") or DEFAULT_DEPARTMENT).strip() or DEFAULT_DEPARTMENT
            entry["配置版本"] = 1
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            self._recompute_pool_locked(entry)
            return entry, []

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """配置修改：参数校验通过后整体覆盖，并把配置版本 +1 后重算准入。"""
        with resource_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"资源池 {entry_id} 不存在或已归档"
            updates = dict(values)
            updates.pop("action", None)
            changed = False
            for field in NUMERIC_FIELDS:
                if field in updates and str(updates.get(field) or "").strip() != "":
                    number, error = to_positive_int(updates.get(field), field)
                    if error:
                        return None, error
                    if int(entry[field]) != number:
                        entry[field] = number
                        changed = True
            if "作业白名单" in updates:
                new_whitelist = parse_whitelist(updates.get("作业白名单"))
                if new_whitelist != parse_whitelist(entry.get("作业白名单")):
                    entry["作业白名单"] = new_whitelist
                    changed = True
            if "抢占开关" in updates:
                new_switch = parse_switch(updates.get("抢占开关"))
                if bool(entry.get("抢占开关")) != new_switch:
                    entry["抢占开关"] = new_switch
                    changed = True
            if "队列策略" in updates:
                queue = str(updates.get("队列策略") or "").strip()
                if queue not in QUEUE_STRATEGIES:
                    return None, f"队列策略仅支持：{'、'.join(QUEUE_STRATEGIES)}"
                if entry.get("队列策略") != queue:
                    entry["队列策略"] = queue
                    changed = True
            for text_field in ("池名称", "归属部门"):
                if text_field in updates and str(updates.get(text_field) or "").strip():
                    new_text = str(updates[text_field]).strip()
                    if str(entry.get(text_field) or "") != new_text:
                        entry[text_field] = new_text
                        changed = True
            if "池编号" in updates and str(updates.get("池编号") or "").strip():
                new_code = str(updates["池编号"]).strip()
                duplicated = any(
                    int(row.get("id", 0)) != entry_id and str(row.get("池编号")) == new_code
                    for row in store.rows(MODULE)
                )
                if duplicated:
                    return None, f"池编号 {new_code} 已被其他资源池占用"
                if str(entry.get("池编号")) != new_code:
                    entry["池编号"] = new_code
                    changed = True
            self._recompute_pool_locked(entry)
            if not changed:
                # 没有实际字段变更：保持配置版本不动，避免无意义的版本膨胀
                return entry, "提交内容与当前配置一致，配置版本未变化"
            # 任何一次有效配置变更都产生新版本，晚到的变更凭版本号与旧准入区分
            entry["配置版本"] = int(entry.get("配置版本", 1)) + 1
            self._recompute_pool_locked(entry)
            self._promote_queued_locked(entry)
            return entry, "资源池配置已更新，配置版本已递增并重新计算准入"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"资源池 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于资源配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with resource_lock:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            # 停用/启用本身也是一次配置变更，版本号同步递增
            entry["配置版本"] = int(entry.get("配置版本", 1)) + 1
            self._recompute_pool_locked(entry)
            # 仅启用后的池子会尝试放行排队作业；停用池的排队作业继续等待，拒绝新作业
            if target == ENABLED_STATUS:
                self._promote_queued_locked(entry)
            return entry, f"资源池已{action}"

    # -------------------------------------------------------------- 回填准入（核心）
    def submit_backfill(
        self, values: dict[str, Any], *, department: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """回填作业准入：停用拒绝、白名单校验、槽位原子争用、重复提交幂等。"""
        job_name = str(values.get("作业名称") or "").strip()
        pool_id_raw = values.get("资源池id")
        if not job_name:
            return None, "缺少必填字段：作业名称"
        if str(pool_id_raw or "").strip() == "":
            return None, "缺少必填字段：资源池id"
        try:
            pool_id = int(pool_id_raw)
        except (TypeError, ValueError):
            return None, f"资源池id必须为整数，收到：{pool_id_raw}"
        with resource_lock:
            pool = store.find(MODULE, pool_id)
            if pool is None:
                return None, f"资源池 {pool_id} 不存在或已归档"
            submit_dept = str(department or values.get("提交部门") or DEFAULT_DEPARTMENT).strip()
            # 准入权限按部门归属判定：跨部门作业只能被拒绝并留痕（先于幂等判断，
            # 否则他部门可借重复提交套取本部门作业的准入结果）
            cross_dept = submit_dept != str(pool.get("归属部门") or "")
            # 幂等：同池同作业只要还有未终结记录，重复提交直接返回原记录，不再生效一次
            existing = self._find_active_backfill_locked(pool_id, job_name)
            if not cross_dept and existing is not None:
                return existing, "相同回填作业已在处理中，重复提交不重复生效"
            reasons: list[str] = []
            if cross_dept:
                reasons.append(f"作业提交部门 {submit_dept} 与资源池归属部门 {pool['归属部门']} 不一致，跨部门准入被拒绝")
            if pool.get("status") != ENABLED_STATUS:
                reasons.append(f"资源池当前状态为「{pool.get('status')}」，停用池拒绝新回填作业")
            whitelist = parse_whitelist(pool.get("作业白名单"))
            if whitelist and job_name not in whitelist:
                reasons.append(f"作业 {job_name} 不在资源池白名单内：{'、'.join(whitelist)}")
            item = self._new_backlog_row(pool, job_name, submit_dept)
            if reasons:
                item["状态"] = "已拒绝"
                item["拒绝原因"] = "；".join(reasons)
                store.rows(BACKLOG_MODULE).append(item)
                return item, item["拒绝原因"]
            # 走到这里 existing 必为 None（同部门未终结记录已在前面幂等返回）
            # 槽位判定与写入在同一把锁内，并发争用最后一个槽位只会有一个作业成功
            occupancy = self._recompute_pool_locked(pool)
            if int(occupancy["剩余槽位"]) <= 0:
                item["状态"] = "排队中"
                item["拒绝原因"] = "资源池并发槽位已满，进入积压队列等待"
                store.rows(BACKLOG_MODULE).append(item)
                self._recompute_pool_locked(pool)
                return item, "资源池并发槽位已满，回填作业已进入积压队列"
            item["状态"] = "运行中"
            item["准入配置版本"] = int(pool["配置版本"])
            item["作业白名单快照"] = list(whitelist)
            item["拒绝原因"] = ""
            store.rows(BACKLOG_MODULE).append(item)
            self._recompute_pool_locked(pool)
            return item, "回填作业已获得准入并占用一个并发槽位"

    def _new_backlog_row(
        self, pool: dict[str, Any], job_name: str, department: str
    ) -> dict[str, Any]:
        rows = store.rows(BACKLOG_MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        version = int(pool.get("配置版本", 1))
        return {
            "id": next_id,
            "回填编号": f"BKL-{next_id:04d}",
            "作业名称": job_name,
            "资源池id": int(pool["id"]),
            "池编号": pool.get("池编号"),
            "提交部门": department,
            "状态": "排队中",
            "配置版本": version,
            "准入配置版本": None,
            "作业白名单快照": parse_whitelist(pool.get("作业白名单")),
            "资源池状态快照": pool.get("status"),
            "运行实例id": None,
            "进度": 0,
            "读取行数": 0,
            "写入行数": 0,
            "拒绝原因": "",
            "创建时间": now_ts(),
            "更新时间": now_ts(),
        }

    def _find_active_backfill_locked(
        self, pool_id: int, job_name: str
    ) -> dict[str, Any] | None:
        for item in store.rows(BACKLOG_MODULE):
            if (
                int(item.get("资源池id", 0)) == pool_id
                and item.get("作业名称") == job_name
                and item.get("状态") in BACKLOG_PENDING_STATES
            ):
                return item
        return None

    # ---------------------------------------------------------- 并发占用双源重算
    def rebuild_occupancy(self) -> list[dict[str, Any]]:
        """从资源池配置 + 运行实例 + 回填积压重新校验并生成全部并发占用记录。"""
        with resource_lock:
            records = [self._recompute_pool_locked(pool) for pool in store.rows(MODULE)]
            return records

    def _runtime_usage_locked(self, pool_id: int) -> tuple[int, list[str]]:
        """来源一：运行实例表里仍处于运行中且占用本池的实例。"""
        refs: list[str] = []
        for row in store.rows(RUNTIME_MODULE):
            if int(row.get("资源池id", 0) or 0) != pool_id:
                continue
            if row.get("status") == RUNTIME_RUNNING:
                refs.append(str(row.get("记录编号") or row.get("id")))
        return len(refs), refs

    def _backlog_usage_locked(self, pool_id: int) -> tuple[int, list[str]]:
        """来源二：回填积压表里已准入运行中的作业。"""
        refs: list[str] = []
        for row in store.rows(BACKLOG_MODULE):
            if int(row.get("资源池id", 0) or 0) != pool_id:
                continue
            if row.get("状态") == RUNTIME_RUNNING:
                refs.append(str(row.get("回填编号") or row.get("id")))
        return len(refs), refs

    def _recompute_pool_locked(self, pool: dict[str, Any]) -> dict[str, Any]:
        pool_id = int(pool["id"])
        limit, _ = to_positive_int(pool.get("最大并发数"), "最大并发数")
        if limit is None:  # 兜底，正常路径已被 repair 修复
            limit = NUMERIC_DEFAULTS["最大并发数"]
            pool["最大并发数"] = limit
        runtime_used, runtime_refs = self._runtime_usage_locked(pool_id)
        backlog_used, backlog_refs = self._backlog_usage_locked(pool_id)
        # 两处分别取数：取较大值作为真实占用，避免任一来源漏记导致占用偏小；
        # 下限钳为 0，彻底消除列表/详情里的负数并发。
        used = max(0, runtime_used, backlog_used)
        remaining = max(0, limit - used)
        record = {
            "id": pool_id,
            "资源池id": pool_id,
            "池编号": pool.get("池编号"),
            "池名称": pool.get("池名称"),
            "最大并发数": limit,
            "运行实例占用": runtime_used,
            "回填占用": backlog_used,
            "并发占用": used,
            "剩余槽位": remaining,
            "准入白名单": parse_whitelist(pool.get("作业白名单")),
            "资源池状态": pool.get("status"),
            "配置版本": int(pool.get("配置版本", 1)),
            "占用来源": ["运行实例", "回填积压"],
            "运行实例明细": runtime_refs,
            "回填明细": backlog_refs,
            "更新时间": now_ts(),
        }
        # 覆盖原先的占用记录，而不是追加，保证两个来源始终只有一份口径
        existing = store.find(OCCUPANCY_MODULE, pool_id)
        if existing is None:
            store.rows(OCCUPANCY_MODULE).append(record)
        else:
            existing.clear()
            existing.update(record)
        return record

    def _promote_queued_locked(self, pool: dict[str, Any]) -> None:
        """配置变更或槽位释放后，按提交顺序尝试放行排队中的回填作业。"""
        if pool.get("status") != ENABLED_STATUS:
            return
        pool_id = int(pool["id"])
        whitelist = parse_whitelist(pool.get("作业白名单"))
        queued = sorted(
            (
                row
                for row in store.rows(BACKLOG_MODULE)
                if int(row.get("资源池id", 0)) == pool_id and row.get("状态") == "排队中"
            ),
            key=lambda row: int(row.get("id", 0)),
        )
        for item in queued:
            occupancy = self._recompute_pool_locked(pool)
            if int(occupancy["剩余槽位"]) <= 0:
                break
            job_name = str(item.get("作业名称") or "")
            # 用最新一版配置重新做白名单准入，准入版本也更新为当前版本
            if whitelist and job_name not in whitelist:
                item["状态"] = "已拒绝"
                item["拒绝原因"] = f"配置版本 {pool['配置版本']} 的白名单不再包含作业 {job_name}"
                item["更新时间"] = now_ts()
                continue
            item["状态"] = "运行中"
            item["准入配置版本"] = int(pool["配置版本"])
            item["作业白名单快照"] = list(whitelist)
            item["资源池状态快照"] = ENABLED_STATUS
            item["拒绝原因"] = ""
            item["更新时间"] = now_ts()
        self._recompute_pool_locked(pool)

    def admit_runtime(
        self, runtime_entry: dict[str, Any], *, department: str | None = None
    ) -> tuple[str, dict[str, Any] | None, str]:
        """运行实例启动前的准入复核，返回 (结果, 回填记录, 说明)。

        结果取值：running（已准入）、queued（槽位满，进入积压）、rejected（拒绝）。
        调用方需已持有 resource_lock。
        """
        pool_id = int(runtime_entry["资源池id"])
        job_name = str(runtime_entry.get("作业名称") or "").strip()
        pool = store.find(MODULE, pool_id)
        if pool is None:
            return "rejected", None, f"资源池 {pool_id} 不存在或已归档"
        submit_dept = str(department or runtime_entry.get("提交部门") or DEFAULT_DEPARTMENT).strip()
        cross_dept = submit_dept != str(pool.get("归属部门") or "")
        # 幂等：同一运行实例重入、或同池同作业已在处理中，直接沿用原回填记录
        backlog_code = runtime_entry.get("回填编号")
        existing: dict[str, Any] | None = None
        for item in store.rows(BACKLOG_MODULE):
            if backlog_code and item.get("回填编号") == backlog_code:
                existing = item
                break
        if existing is None:
            existing = self._find_active_backfill_locked(pool_id, job_name)
        if not cross_dept and existing is not None and existing.get("状态") in BACKLOG_PENDING_STATES:
            if existing.get("状态") == "运行中":
                existing["运行实例id"] = runtime_entry.get("id")
                return "running", existing, "回填作业已在占用槽位内，复用原准入记录"
            return "queued", existing, "回填作业仍在积压队列等待，运行实例暂不启动"
        if cross_dept:
            item = self._new_backlog_row(pool, job_name, submit_dept)
            reason = (
                f"作业提交部门 {submit_dept} 与资源池归属部门 {pool['归属部门']} 不一致，"
                "跨部门准入被拒绝"
            )
            item["状态"] = "已拒绝"
            item["拒绝原因"] = reason
            store.rows(BACKLOG_MODULE).append(item)
            return "rejected", item, reason
        # 运行实例读取并发占用数据后，只启动白名单内且资源池已启用的回填
        if pool.get("status") != ENABLED_STATUS:
            item = self._new_backlog_row(pool, job_name, submit_dept)
            reason = f"资源池当前状态为「{pool.get('status')}」，停用池拒绝新回填作业"
            item["状态"] = "已拒绝"
            item["拒绝原因"] = reason
            store.rows(BACKLOG_MODULE).append(item)
            return "rejected", item, reason
        whitelist = parse_whitelist(pool.get("作业白名单"))
        if whitelist and job_name not in whitelist:
            item = self._new_backlog_row(pool, job_name, submit_dept)
            reason = f"作业 {job_name} 不在资源池白名单内：{'、'.join(whitelist)}"
            item["状态"] = "已拒绝"
            item["拒绝原因"] = reason
            store.rows(BACKLOG_MODULE).append(item)
            return "rejected", item, reason
        item = self._new_backlog_row(pool, job_name, submit_dept)
        occupancy = self._recompute_pool_locked(pool)
        if int(occupancy["剩余槽位"]) <= 0:
            item["状态"] = "排队中"
            item["拒绝原因"] = "资源池并发槽位已满，进入积压队列等待"
            store.rows(BACKLOG_MODULE).append(item)
            self._recompute_pool_locked(pool)
            return "queued", item, "资源池并发槽位已满，运行实例进入积压队列，暂不启动"
        item["状态"] = "运行中"
        item["准入配置版本"] = int(pool["配置版本"])
        item["作业白名单快照"] = list(whitelist)
        item["资源池状态快照"] = ENABLED_STATUS
        item["运行实例id"] = runtime_entry.get("id")
        item["拒绝原因"] = ""
        store.rows(BACKLOG_MODULE).append(item)
        self._recompute_pool_locked(pool)
        return "running", item, "运行实例已通过白名单与槽位复核，回填启动成功"

    def release_backfill(
        self, backlog_id: int, terminal_status: str, *, progress: int | None = None
    ) -> dict[str, Any] | None:
        """运行实例终结后同步回填状态，并尝试把槽位让给排队作业。"""
        with resource_lock:
            item = store.find(BACKLOG_MODULE, backlog_id)
            if item is None:
                return None
            item["状态"] = terminal_status if terminal_status in BACKLOG_TERMINAL_STATES else "已终止"
            if progress is not None:
                item["进度"] = max(0, min(100, int(progress)))
            item["更新时间"] = now_ts()
            pool = store.find(MODULE, int(item["资源池id"]))
            if pool is not None:
                self._recompute_pool_locked(pool)
                self._promote_queued_locked(pool)
            return item
