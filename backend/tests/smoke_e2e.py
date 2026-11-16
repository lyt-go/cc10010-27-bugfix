"""端到端冒烟脚本（TestClient 版）：按任务陈述逐条验证修复点。"""
import sys
import threading

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

store.reset()
c = TestClient(app)
W = {"X-Department-Id": "DATA_PLATFORM", "X-User-Role": "writer"}
RO = {"X-Department-Id": "DATA_PLATFORM", "X-User-Role": "readonly"}
CROSS = {"X-Department-Id": "RISK_DATA", "X-User-Role": "writer"}
failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


# 1. 非法参数（零/负/非数字）全部拒绝
for bad in (0, -1, "abc"):
    body = c.post("/api/resource", headers=W, json={"values": {
        "池编号": f"RESO-BAD-{bad}", "池名称": "非法", "最大并发数": bad,
        "内存上限MB": 1024, "CPU核心数": 2}}).json()
    check(f"非法最大并发数 {bad!r} 被拒绝", body["ok"] is False)
for bad in (-8, 0):
    body = c.post("/api/resource", headers=W, json={"values": {
        "池编号": f"RESO-MEM-{bad}", "池名称": "非法", "最大并发数": 2,
        "内存上限MB": bad, "CPU核心数": 2}}).json()
    check(f"非法内存 {bad!r} 被拒绝", body["ok"] is False)

# 2. 合法登记
body = c.post("/api/resource", headers=W, json={"values": {
    "池编号": "RESO-E2E", "池名称": "端到端验证池", "最大并发数": 2,
    "内存上限MB": 2048, "CPU核心数": 2, "作业白名单": ["作业A", "作业B"]}}).json()
check("合法资源池登记成功", body["ok"] and body["entry"]["配置版本"] == 1)
pool_id = body["entry"]["id"]

# 3. 未启用池拒绝新回填
body = c.post("/api/resource/backlog", headers=W,
              json={"values": {"资源池id": pool_id, "作业名称": "作业A"}}).json()
check("未启用池拒绝回填", body["ok"] is False and body["entry"]["状态"] == "已拒绝")

# 4. 启用 + 白名单外拒绝
c.post(f"/api/resource/{pool_id}/actions", headers=W, json={"values": {"action": "启用配置"}})
body = c.post("/api/resource/backlog", headers=W,
              json={"values": {"资源池id": pool_id, "作业名称": "作业X"}}).json()
check("白名单外作业被拒绝", body["ok"] is False and "白名单" in body["message"])

# 5. 白名单内两个作业占满 2 槽位
b1 = c.post("/api/resource/backlog", headers=W,
            json={"values": {"资源池id": pool_id, "作业名称": "作业A"}}).json()
b2 = c.post("/api/resource/backlog", headers=W,
            json={"values": {"资源池id": pool_id, "作业名称": "作业B"}}).json()
check("两个白名单作业获得准入", b1["entry"]["状态"] == b2["entry"]["状态"] == "运行中")

# 6. 扩容到4、先占3个，再用真实线程并发抢最后一个（第4个）槽位
c.put(f"/api/resource/{pool_id}", headers=W,
      json={"values": {"最大并发数": 4, "作业白名单": ["作业A", "作业B", "作业C", "作业D", "作业E"]}})
b3 = c.post("/api/resource/backlog", headers=W,
            json={"values": {"资源池id": pool_id, "作业名称": "作业C"}}).json()
check("第三个作业占用至 3/4", b3["entry"]["状态"] == "运行中")
results = []
barrier = threading.Barrier(2)


def contend(job):
    barrier.wait()
    body = c.post("/api/resource/backlog", headers=W,
                  json={"values": {"资源池id": pool_id, "作业名称": job}}).json()
    results.append(body["entry"]["状态"])


t1 = threading.Thread(target=contend, args=("作业D",))
t2 = threading.Thread(target=contend, args=("作业E",))
t1.start(); t2.start(); t1.join(); t2.join()
check("最后槽位并发争用只有一项成功", sorted(results) == ["排队中", "运行中"], str(results))

# 7. 重复提交幂等
dup = c.post("/api/resource/backlog", headers=W,
             json={"values": {"资源池id": pool_id, "作业名称": "作业A"}}).json()
check("重复提交只生效一次", dup["entry"]["id"] == b1["entry"]["id"] and "重复" in dup["message"])

# 8. 占用记录双来源、非负、覆盖式（A/B/C + D/E 之一在跑，共 4 占用）
occ = c.get("/api/resource/occupancy").json()["items"]
mine = next(i for i in occ if i["资源池id"] == pool_id)
check("占用记录双来源且无负数",
      set(mine["占用来源"]) == {"运行实例", "回填积压"} and mine["剩余槽位"] >= 0
      and mine["并发占用"] == 4, str(mine))
c.post("/api/resource/recompute", headers=W, json={})
occ2 = c.get("/api/resource/occupancy").json()["items"]
check("重算覆盖旧记录而非追加", len([i for i in occ2 if i["资源池id"] == pool_id]) == 1)

# 9. 配置版本递增 + 停用拒绝（用未在途的作业F验证停用）
ver_before = c.get(f"/api/resource/{pool_id}").json()["配置版本"]
c.put(f"/api/resource/{pool_id}", headers=W,
      json={"values": {"作业白名单": ["作业A", "作业B", "作业C", "作业D", "作业E", "作业F"]}})
ver_after = c.get(f"/api/resource/{pool_id}").json()["配置版本"]
check("配置变更版本递增", ver_after == ver_before + 1)
c.post(f"/api/resource/{pool_id}/actions", headers=W, json={"values": {"action": "停用配置"}})
body = c.post("/api/resource/backlog", headers=W,
              json={"values": {"资源池id": pool_id, "作业名称": "作业F"}}).json()
check("停用池拒绝新回填", body["ok"] is False and "停用" in body["message"])

# 10. 运行实例：独立新池验证启用+白名单准入、白名单外拒绝、进度同步、版本一致
rt_pool = c.post("/api/resource", headers=W, json={"values": {
    "池编号": "RESO-RT-E2E", "池名称": "运行实例验证池", "最大并发数": 2,
    "内存上限MB": 2048, "CPU核心数": 2, "作业白名单": ["作业F"]}}).json()["entry"]
rt_pool_id = rt_pool["id"]
rt = c.post("/api/runtime", headers=W, json={"values": {
    "记录编号": "RUNT-E2E", "作业名称": "作业F", "开始时间": "2026-11-16",
    "资源池id": rt_pool_id}}).json()["entry"]
blocked = c.post(f"/api/runtime/{rt['id']}/actions", headers=W,
                 json={"values": {"action": "启动作业"}}).json()
check("未启用池的运行实例启动被拒", blocked["ok"] is False and "停用" in blocked["message"])
c.post(f"/api/resource/{rt_pool_id}/actions", headers=W,
       json={"values": {"action": "启用配置"}})
start = c.post(f"/api/runtime/{rt['id']}/actions", headers=W,
               json={"values": {"action": "启动作业"}}).json()
check("运行实例白名单+启用池启动成功",
      start["ok"] is True and start["entry"]["配置版本"] is not None)
outsider = c.post("/api/runtime", headers=W, json={"values": {
    "记录编号": "RUNT-OUT", "作业名称": "局外作业", "开始时间": "2026-11-16",
    "资源池id": rt_pool_id}}).json()["entry"]
denied = c.post(f"/api/runtime/{outsider['id']}/actions", headers=W,
                json={"values": {"action": "启动作业"}}).json()
check("运行实例白名单外启动被拒", denied["ok"] is False and "白名单" in denied["message"])
prog = c.post(f"/api/runtime/{rt['id']}/progress", headers=W,
              json={"values": {"进度": 77, "读取行数": 100, "写入行数": 90}}).json()
check("进度更新成功", prog["ok"] and prog["entry"]["进度"] == 77)
bl = c.get("/api/resource/backlog", params={"pool_id": rt_pool_id}).json()["items"]
linked = next(i for i in bl if i["作业名称"] == "作业F" and i["状态"] == "运行中")
check("进度同步到积压且配置版本一致",
      linked["进度"] == 77 and linked["准入配置版本"] == prog["entry"]["配置版本"])

# 11. 部门权限
check("跨部门修改被 403 拒绝",
      c.put(f"/api/resource/{rt_pool_id}", headers=CROSS,
            json={"values": {"最大并发数": 5}}).status_code == 403)
body = c.post("/api/resource/backlog", headers=CROSS,
              json={"values": {"资源池id": rt_pool_id, "作业名称": "作业F"}}).json()
check("跨部门回填被拒绝留痕", body["ok"] is False and body["entry"]["状态"] == "已拒绝")

# 12. 只读成员
check("只读成员可看容量", c.get("/api/resource/occupancy", headers=RO).status_code == 200)
check("只读成员重算 403", c.post("/api/resource/recompute", headers=RO, json={}).status_code == 403)
check("只读成员回填 403", c.post("/api/resource/backlog", headers=RO,
      json={"values": {"资源池id": rt_pool_id, "作业名称": "作业F"}}).status_code == 403)
check("只读成员运行动作 403", c.post(f"/api/runtime/{rt['id']}/actions", headers=RO,
      json={"values": {"action": "终止作业"}}).status_code == 403)

# 13. 列表/详情不出现负数
listing = c.get("/api/resource").json()["items"]
check("列表并发值全部为正", all(row["最大并发数"] > 0 for row in listing))
detail = c.get(f"/api/resource/{rt_pool_id}").json()
check("详情含占用记录且无负数", detail["并发占用记录"]["剩余槽位"] >= 0)

# 14. 运行实例终结释放槽位并同步回填状态
term = c.post(f"/api/runtime/{rt['id']}/actions", headers=W,
              json={"values": {"action": "标记成功"}}).json()
check("运行实例终结成功", term["ok"] is True)
bl_after = c.get("/api/resource/backlog", params={"pool_id": rt_pool_id}).json()["items"]
done = next(i for i in bl_after if i["回填编号"] == linked["回填编号"])
check("终结后回填状态同步为已完成", done["状态"] == "已完成")
occ_rt = next(i for i in c.get("/api/resource/occupancy").json()["items"]
              if i["资源池id"] == rt_pool_id)
check("终结后槽位已释放", occ_rt["并发占用"] == 0 and occ_rt["剩余槽位"] == 2)

print()
if failures:
    print(f"{len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部端到端检查通过")
