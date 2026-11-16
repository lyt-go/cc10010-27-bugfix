# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间 |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间开始 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 | 模板编号、模板名称、模板类型 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 资源池准入与并发占用

资源配置（`resource`）与运行实例（`runtime`）围绕回填作业共享一套准入口径：

- **参数校验**：最大并发数、内存上限MB、CPU核心数只接受正整数，零、负数、非数字
  一律拒绝；历史脏数据在服务装载时兜底修复，列表与详情不会再出现负数并发。
- **准入规则**：停用资源池拒绝一切新回填；白名单为空表示不限制，非空时仅放行
  名单内作业；同池同作业未终结前重复提交幂等，只生效一次。
- **槽位争用**：准入在 `app/services/locking.py` 的全局锁内完成「检查剩余槽位 →
  写入占用」，多个作业并发争用最后一个槽位时只有一个成功，其余进入积压队列；
  槽位释放后按 FIFO 放行排队作业。
- **配置版本**：每次配置修改、启用、停用都使 `配置版本` 递增；回填记录固化准入时
  的版本与白名单快照，晚到的配置变更不会改写已准入作业，排队作业重准入时按最新
  版本重新判定，运行实例与回填积压通过同一配置版本保持跨模块一致。
- **并发占用记录**（`resource_occupancy`）：由「运行实例」与「回填积压」两个来源
  分别取数，取较大值合并并覆盖旧记录，剩余槽位下限钳为 0；可通过
  `POST /api/resource/recompute` 从资源池配置与积压数据整体重算。
- **权限**：写操作按资源池 `归属部门` 判定（请求头 `X-Department-Id` 传部门编码，
  编码目录见 `app/services/resource.py`），跨部门修改返回 403、跨部门回填生成
  「已拒绝」积压记录留痕；`X-User-Role: readonly` 的只读成员可查看容量信息但
  所有写操作返回 403。
- 运行实例启动挂接资源池的回填前会读取并发占用记录复核「已启用 + 白名单 +
  剩余槽位」，进度通过 `POST /api/runtime/{id}/progress` 同步回回填积压记录。

## 测试

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # 30 项准入/占用/权限单元与接口测试
PYTHONPATH=. .venv/bin/python tests/smoke_e2e.py   # 端到端冒烟检查
```

领域：数据工程
子方向：数仓与数据湖
