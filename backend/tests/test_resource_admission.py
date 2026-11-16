"""资源池准入与并发占用端到端测试。

覆盖：
- 参数校验：最大并发/内存/CPU 必须为正整数，零、负数、非数字全部拒绝；
- 停用池拒绝新回填、白名单准入生效、最后槽位争用只成功一项、重复提交幂等；
- 配置版本冲突时以准入时版本为准，配置变更触发排队作业重准入并覆盖占用记录；
- 并发占用由运行实例与回填积压两个来源取数重算，永不出现负数；
- 运行实例只启动白名单内且资源池已启用的回填，进度回写积压记录；
- 部门归属权限：跨部门修改 403、跨部门回填拒绝、只读成员可读不可写。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store

PLATFORM_DEPT = "数据平台部"
OTHER_DEPT = "风控数据部"
DEPT_HEADER = {"X-Department-Id": "DATA_PLATFORM"}
WRITER_HEADERS = {**DEPT_HEADER, "X-User-Role": "writer"}
READONLY_HEADERS = {**DEPT_HEADER, "X-User-Role": "readonly"}
CROSS_HEADERS = {"X-Department-Id": "RISK_DATA", "X-User-Role": "writer"}


@pytest.fixture()
def client() -> TestClient:
    store.reset()
    with TestClient(app) as test_client:
        yield test_client
    store.reset()


def _find_pool_occupancy(client: TestClient, pool_id: int) -> dict:
    resp = client.get("/api/resource/occupancy")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        if item["资源池id"] == pool_id:
            return item
    raise AssertionError(f"资源池 {pool_id} 的占用记录缺失")


def _submit_backfill(client: TestClient, pool_id: int, job_name: str, headers=None):
    return client.post(
        "/api/resource/backlog",
        headers=headers or WRITER_HEADERS,
        json={"values": {"资源池id": pool_id, "作业名称": job_name}},
    )


# ---------------------------------------------------------------- 参数校验
class TestResourceValidation:
    @pytest.mark.parametrize("bad_value", [0, -1, -99, "0", "-3", "abc", "", None])
    def test_reject_non_positive_concurrency_on_create(self, client: TestClient, bad_value) -> None:
        resp = client.post(
            "/api/resource",
            headers=WRITER_HEADERS,
            json={"values": {
                "池编号": "RESO-NEG",
                "池名称": "非法并发池",
                "最大并发数": bad_value,
                "内存上限MB": 2048,
                "CPU核心数": 2,
            }},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "最大并发数" in resp.json()["message"]

    @pytest.mark.parametrize("field", ["内存上限MB", "CPU核心数"])
    def test_reject_zero_or_negative_memory_cpu(self, client: TestClient, field: str) -> None:
        values = {
            "池编号": "RESO-NEG2",
            "池名称": "非法配额池",
            "最大并发数": 2,
            "内存上限MB": 2048,
            "CPU核心数": 2,
            field: -4,
        }
        resp = client.post("/api/resource", headers=WRITER_HEADERS, json={"values": values})
        body = resp.json()
        assert body["ok"] is False
        assert field in body["message"]

    def test_update_with_negative_value_rejected_and_version_unchanged(self, client: TestClient) -> None:
        before = client.get("/api/resource/3").json()
        resp = client.put(
            "/api/resource/3",
            headers=WRITER_HEADERS,
            json={"values": {"最大并发数": -5}},
        )
        body = resp.json()
        assert body["ok"] is False
        assert "大于0" in body["message"]
        after = client.get("/api/resource/3").json()
        assert after["最大并发数"] == before["最大并发数"] == 2
        assert after["配置版本"] == before["配置版本"]

    def test_legacy_dirty_rows_are_repaired_to_positive_numbers(self, client: TestClient) -> None:
        # 手工把数据改脏，再触发重算：负数必须被钳回，页面不能再看到负数
        pool = store.find("resource", 1)
        pool["最大并发数"] = -7
        pool["作业白名单"] = "订单日汇总作业,库存快照回填"
        resp = client.post("/api/resource/recompute", headers=WRITER_HEADERS)
        assert resp.json()["ok"] is True
        entry = client.get("/api/resource/1").json()
        assert entry["最大并发数"] > 0
        occ = _find_pool_occupancy(client, 1)
        assert occ["并发占用"] >= 0 and occ["剩余槽位"] >= 0
        assert occ["准入白名单"] == ["订单日汇总作业", "库存快照回填"]

    def test_valid_create_persists_numeric_fields(self, client: TestClient) -> None:
        resp = client.post(
            "/api/resource",
            headers=WRITER_HEADERS,
            json={"values": {
                "池编号": "RESO-OK",
                "池名称": "合规资源池",
                "最大并发数": 3,
                "内存上限MB": 4096,
                "CPU核心数": 4,
                "作业白名单": ["新回填作业"],
            }},
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["entry"]["最大并发数"] == 3
        assert body["entry"]["配置版本"] == 1
        assert body["entry"]["归属部门"] == PLATFORM_DEPT

    def test_noop_update_keeps_config_version(self, client: TestClient) -> None:
        before = client.get("/api/resource/3").json()
        resp = client.put(
            "/api/resource/3",
            headers=WRITER_HEADERS,
            json={"values": {"最大并发数": before["最大并发数"]}},
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["entry"]["配置版本"] == before["配置版本"]
        assert "未变化" in body["message"]


# ------------------------------------------------------- 停用与白名单准入
class TestAdmission:
    def test_disabled_pool_rejects_new_backfill(self, client: TestClient) -> None:
        resp = _submit_backfill(client, 4, "日志归档回填")
        body = resp.json()
        assert body["ok"] is False
        assert body["entry"]["状态"] == "已拒绝"
        assert "停用" in body["message"]
        # 拒绝必须留痕到积压队列
        listed = client.get("/api/resource/backlog", params={"pool_id": 4}).json()
        assert any(item["状态"] == "已拒绝" for item in listed["items"])

    def test_disabled_pool_still_rejects_after_enable_disable_cycle(self, client: TestClient) -> None:
        client.post(
            "/api/resource/4/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启用配置"}},
        )
        ok = _submit_backfill(client, 4, "日志归档回填")
        assert ok.json()["ok"] is True
        client.post(
            "/api/resource/4/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "停用配置"}},
        )
        blocked = _submit_backfill(client, 4, "日志归档回填二")
        assert blocked.json()["ok"] is False
        assert blocked.json()["entry"]["状态"] == "已拒绝"

    def test_whitelist_blocks_unknown_job(self, client: TestClient) -> None:
        resp = _submit_backfill(client, 2, "不在白名单的作业")
        body = resp.json()
        assert body["ok"] is False
        assert "白名单" in body["message"]
        assert body["entry"]["状态"] == "已拒绝"

    def test_whitelist_allows_listed_job(self, client: TestClient) -> None:
        # 池2处于评审中，先启用再提交白名单内作业
        client.post(
            "/api/resource/2/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启用配置"}},
        )
        resp = _submit_backfill(client, 2, "用户行为回流")
        body = resp.json()
        assert body["ok"] is True
        assert body["entry"]["状态"] == "运行中"

    def test_late_whitelist_change_rejects_promotion_and_keeps_old_admission(self, client: TestClient) -> None:
        # 池3槽位已满（占用2/上限2），新作业先排队
        queued = _submit_backfill(client, 3, "用户行为回流")
        assert queued.json()["entry"]["状态"] == "排队中"
        admitted_before = client.get("/api/resource/backlog").json()
        running_version = next(
            item for item in admitted_before["items"]
            if item["回填编号"] == "BKL-0003"
        )
        assert running_version["准入配置版本"] == 2  # 已准入作业记住旧版本
        # 晚到的配置变更：白名单移除「用户行为回流」，版本递增
        upd = client.put(
            "/api/resource/3",
            headers=WRITER_HEADERS,
            json={"values": {"作业白名单": ["订单日汇总作业", "库存快照回填"]}},
        )
        assert upd.json()["entry"]["配置版本"] == 4
        # 释放一个槽位触发排队重准入：被新版白名单拒绝
        term = client.post(
            "/api/runtime/4/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "终止作业"}},
        )
        assert term.json()["ok"] is True
        items = client.get("/api/resource/backlog", params={"pool_id": 3}).json()["items"]
        target = next(item for item in items if item["作业名称"] == "用户行为回流")
        assert target["状态"] == "已拒绝"
        assert "白名单" in target["拒绝原因"]
        # 旧的已准入记录版本不被覆盖
        old = next(item for item in items if item["回填编号"] == "BKL-0003")
        assert old["准入配置版本"] == 2


# ----------------------------------------------------------- 槽位争用与幂等
class TestContentionAndIdempotency:
    def test_last_slot_only_one_wins_under_concurrency(self, client: TestClient) -> None:
        # 池1上限4、当前占用0；扩容白名单并启用后先占3个，再并发抢最后1个
        client.put(
            "/api/resource/1",
            headers=WRITER_HEADERS,
            json={"values": {"作业白名单": [f"争用作业-{i}" for i in range(8)]}},
        )
        client.post(
            "/api/resource/1/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启用配置"}},
        )
        for i in range(3):
            resp = _submit_backfill(client, 1, f"争用作业-{i}")
            assert resp.json()["entry"]["状态"] == "运行中"
        results: list[dict] = []
        barrier = threading.Barrier(2)

        def contend(job: str) -> None:
            barrier.wait()
            r = _submit_backfill(client, 1, job)
            results.append(r.json()["entry"])

        t1 = threading.Thread(target=contend, args=("争用作业-4",))
        t2 = threading.Thread(target=contend, args=("争用作业-5",))
        t1.start(); t2.start(); t1.join(); t2.join()
        states = sorted(item["状态"] for item in results)
        assert states == ["排队中", "运行中"]
        occ = _find_pool_occupancy(client, 1)
        assert occ["并发占用"] == 4 and occ["剩余槽位"] == 0

    def test_duplicate_submission_takes_effect_once(self, client: TestClient) -> None:
        client.post(
            "/api/resource/2/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启用配置"}},
        )
        first = _submit_backfill(client, 2, "用户行为回流")
        first_id = first.json()["entry"]["id"]
        second = _submit_backfill(client, 2, "用户行为回流")
        third = _submit_backfill(client, 2, "用户行为回流")
        assert second.json()["entry"]["id"] == first_id
        assert third.json()["entry"]["id"] == first_id
        assert "重复提交不重复生效" in second.json()["message"]
        items = client.get("/api/resource/backlog", params={"pool_id": 2}).json()["items"]
        active = [i for i in items if i["作业名称"] == "用户行为回流" and i["状态"] == "运行中"]
        assert len(active) == 1

    def test_queue_promotes_in_fifo_order_when_slot_released(self, client: TestClient) -> None:
        # 池2上限1，启用后先占满唯一槽位，再排队两个白名单作业；释放一个槽位只放行一个
        from app.services.locking import resource_lock
        from app.services.resource import ResourceService

        client.put(
            "/api/resource/2",
            headers=WRITER_HEADERS,
            json={"values": {"最大并发数": 1, "作业白名单": ["用户行为回流", "补录作业A", "补录作业B"]}},
        )
        client.post(
            "/api/resource/2/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启用配置"}},
        )
        head = _submit_backfill(client, 2, "用户行为回流")
        assert head.json()["entry"]["状态"] == "运行中"
        q1 = _submit_backfill(client, 2, "补录作业A")
        assert q1.json()["entry"]["状态"] == "排队中"
        q2 = _submit_backfill(client, 2, "补录作业B")
        assert q2.json()["entry"]["状态"] == "排队中"
        # 槽位上的作业终结（与运行实例触发 release_backfill 同一代码路径），释放唯一槽位
        with resource_lock:
            ResourceService().release_backfill(int(head.json()["entry"]["id"]), "已终止")
        items = client.get("/api/resource/backlog", params={"pool_id": 2}).json()["items"]
        first_queued = next(i for i in items if i["作业名称"] == "补录作业A")
        second_queued = next(i for i in items if i["作业名称"] == "补录作业B")
        assert first_queued["状态"] == "运行中"  # 先排队先放行
        assert second_queued["状态"] == "排队中"  # 只释放了一个槽位
        occ = _find_pool_occupancy(client, 2)
        assert occ["并发占用"] == 1 and occ["剩余槽位"] == 0


# ------------------------------------------------- 双来源取数与负数显示
class TestOccupancyRecompute:
    def test_occupancy_merges_two_sources_and_overwrites_record(self, client: TestClient) -> None:
        occ = _find_pool_occupancy(client, 3)
        assert occ["运行实例占用"] == 2      # 来源一：运行实例
        assert occ["回填占用"] == 1          # 来源二：回填积压
        assert occ["并发占用"] == 2          # 合并口径取较大值
        assert set(occ["占用来源"]) == {"运行实例", "回填积压"}
        # 重复重算仍然只有一条记录（覆盖而非追加）
        client.post("/api/resource/recompute", headers=WRITER_HEADERS)
        again = client.get("/api/resource/occupancy").json()["items"]
        pool_records = [i for i in again if i["资源池id"] == 3]
        assert len(pool_records) == 1

    def test_occupancy_never_negative_even_if_runtime_over_subscribed(self, client: TestClient) -> None:
        # 直接造一条超出上限的运行中实例（历史脏数据），重算后剩余槽位钳为0
        rows = store.rows("runtime")
        rows.append({
            "id": 99, "status": "运行中", "pending": True, "abnormal": False,
            "记录编号": "RUNT-0099", "作业名称": "超额作业", "开始时间": "2026-09-06",
            "结束时间": "", "执行状态": "", "读取行数": 0, "写入行数": 0, "错误信息": "",
            "资源池id": 3, "进度": 0, "配置版本": None, "回填编号": None,
        })
        client.post("/api/resource/recompute", headers=WRITER_HEADERS)
        occ = _find_pool_occupancy(client, 3)
        assert occ["并发占用"] >= 0 and occ["剩余槽位"] == 0
        # 列表与详情接口同样不能出现负数
        listed = client.get("/api/resource").json()["items"]
        for row in listed:
            assert row["最大并发数"] > 0
        detail = client.get("/api/resource/3").json()
        assert detail["并发占用记录"]["剩余槽位"] >= 0

    def test_backlog_runtime_consistent_after_release(self, client: TestClient) -> None:
        client.post(
            "/api/runtime/4/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "标记成功"}},
        )
        occ = _find_pool_occupancy(client, 3)
        # 运行实例4终结：运行中只剩 RUNT-0005，BKL-0003 终结，排队的 BKL-0001 被放行
        assert occ["运行实例占用"] == 1
        backlog = client.get("/api/resource/backlog", params={"pool_id": 3}).json()["items"]
        done = next(i for i in backlog if i["回填编号"] == "BKL-0003")
        assert done["状态"] == "已完成"
        promoted = next(i for i in backlog if i["回填编号"] == "BKL-0001")
        assert promoted["状态"] == "运行中"
        assert promoted["准入配置版本"] == 3  # 放行时使用当前配置版本


# ------------------------------------- 运行实例准入复核、进度与配置版本
class TestRuntimeAdmission:
    def test_runtime_starts_only_whitelisted_enabled_pool_backfill(self, client: TestClient) -> None:
        # 新登记一个草稿态（未启用）资源池下的运行实例
        created = client.post(
            "/api/resource",
            headers=WRITER_HEADERS,
            json={"values": {
                "池编号": "RESO-RT", "池名称": "运行准入池",
                "最大并发数": 1, "内存上限MB": 1024, "CPU核心数": 1,
                "作业白名单": ["白名单作业"],
            }},
        )
        pool_id = created.json()["entry"]["id"]
        runtime = client.post(
            "/api/runtime",
            headers=WRITER_HEADERS,
            json={"values": {"记录编号": "RUNT-RT1", "作业名称": "白名单作业",
                             "开始时间": "2026-09-06", "资源池id": pool_id}},
        ).json()["entry"]
        # 未启用：启动被拒绝
        blocked = client.post(
            f"/api/runtime/{runtime['id']}/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启动作业"}},
        )
        assert blocked.json()["ok"] is False and "停用" in blocked.json()["message"]
        # 启用后白名单内作业启动成功
        client.post(
            f"/api/resource/{pool_id}/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启用配置"}},
        )
        started = client.post(
            f"/api/runtime/{runtime['id']}/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启动作业"}},
        )
        assert started.json()["ok"] is True
        assert started.json()["entry"]["配置版本"] == 2
        # 白名单外作业启动被拒
        outsider = client.post(
            "/api/runtime",
            headers=WRITER_HEADERS,
            json={"values": {"记录编号": "RUNT-RT2", "作业名称": "白名单外作业",
                             "开始时间": "2026-09-06", "资源池id": pool_id}},
        ).json()["entry"]
        denied = client.post(
            f"/api/runtime/{outsider['id']}/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启动作业"}},
        )
        assert denied.json()["ok"] is False and "白名单" in denied.json()["message"]

    def test_progress_syncs_to_backlog_with_same_config_version(self, client: TestClient) -> None:
        resp = client.post(
            "/api/runtime/4/progress",
            headers=WRITER_HEADERS,
            json={"values": {"进度": 80, "读取行数": 20000, "写入行数": 19900}},
        )
        assert resp.json()["ok"] is True
        backlog = client.get("/api/resource/backlog", params={"pool_id": 3}).json()["items"]
        linked = next(i for i in backlog if i["回填编号"] == "BKL-0003")
        assert linked["进度"] == 80
        assert linked["读取行数"] == 20000 and linked["写入行数"] == 19900
        runtime = client.get("/api/runtime/4").json()
        # 跨模块作业状态使用同一配置版本
        assert runtime["配置版本"] == linked["准入配置版本"] == 2

    def test_progress_clamped_and_negative_rejected(self, client: TestClient) -> None:
        ok = client.post(
            "/api/runtime/4/progress",
            headers=WRITER_HEADERS,
            json={"values": {"进度": 150}},
        )
        assert ok.json()["entry"]["进度"] == 100
        bad = client.post(
            "/api/runtime/4/progress",
            headers=WRITER_HEADERS,
            json={"values": {"读取行数": -1}},
        )
        assert bad.json()["ok"] is False

    def test_terminating_queued_runtime_cancels_orphan_backlog(self, client: TestClient) -> None:
        # 池3已满（2/2），新运行实例启动只能排队
        runtime = client.post(
            "/api/runtime",
            headers=WRITER_HEADERS,
            json={"values": {"记录编号": "RUNT-Q1", "作业名称": "用户行为回流",
                             "开始时间": "2026-09-06", "资源池id": 3}},
        ).json()["entry"]
        queued = client.post(
            f"/api/runtime/{runtime['id']}/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "启动作业"}},
        )
        assert queued.json()["ok"] is False
        backlog_code = client.get(f"/api/runtime/{runtime['id']}").json()["回填编号"]
        assert backlog_code
        # 排队未启动即终止：积压记录必须同步撤销，不能成为孤儿排队项
        term = client.post(
            f"/api/runtime/{runtime['id']}/actions",
            headers=WRITER_HEADERS,
            json={"values": {"action": "终止作业"}},
        )
        assert term.json()["ok"] is True
        items = client.get("/api/resource/backlog", params={"pool_id": 3}).json()["items"]
        orphan = next(i for i in items if i["回填编号"] == backlog_code)
        assert orphan["状态"] == "已终止"


# ----------------------------------------------------------- 部门与角色权限
class TestDepartmentPermission:
    def test_cross_department_config_change_rejected(self, client: TestClient) -> None:
        resp = client.put(
            "/api/resource/3",
            headers=CROSS_HEADERS,
            json={"values": {"最大并发数": 9}},
        )
        assert resp.status_code == 403
        action_resp = client.post(
            "/api/resource/3/actions",
            headers=CROSS_HEADERS,
            json={"values": {"action": "停用配置"}},
        )
        assert action_resp.status_code == 403
        # 数据未被改动
        assert client.get("/api/resource/3").json()["status"] == "已启用"

    def test_cross_department_backfill_rejected_and_audited(self, client: TestClient) -> None:
        resp = _submit_backfill(client, 3, "订单日汇总作业", headers=CROSS_HEADERS)
        body = resp.json()
        assert body["ok"] is False
        assert body["entry"]["状态"] == "已拒绝"
        assert "部门" in body["message"]

    def test_readonly_member_can_read_capacity_but_not_write(self, client: TestClient) -> None:
        assert client.get("/api/resource", headers=READONLY_HEADERS).status_code == 200
        assert client.get("/api/resource/occupancy", headers=READONLY_HEADERS).status_code == 200
        assert client.get("/api/resource/3", headers=READONLY_HEADERS).status_code == 200
        denied = client.post(
            "/api/resource/backlog",
            headers=READONLY_HEADERS,
            json={"values": {"资源池id": 3, "作业名称": "订单日汇总作业"}},
        )
        assert denied.status_code == 403
        denied_update = client.put(
            "/api/resource/3",
            headers=READONLY_HEADERS,
            json={"values": {"最大并发数": 5}},
        )
        assert denied_update.status_code == 403
        denied_action = client.post(
            "/api/runtime/4/actions",
            headers=READONLY_HEADERS,
            json={"values": {"action": "终止作业"}},
        )
        assert denied_action.status_code == 403
