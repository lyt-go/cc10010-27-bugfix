<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">数据仓库ETL任务编排平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。</span>
        <span class="head-user">
          当前值班：{{ store.operator }} · {{ store.shiftLabel }}
          <label class="identity-item">
            部门
            <select :value="store.department" @change="onDepartmentChange">
              <option v-for="dept in DEPARTMENT_OPTIONS" :key="dept.code" :value="dept.code">
                {{ dept.name }}
              </option>
            </select>
          </label>
          <label class="identity-item">
            角色
            <select :value="store.role" @change="onRoleChange">
              <option v-for="role in ROLE_OPTIONS" :key="role.code" :value="role.code">
                {{ role.name }}
              </option>
            </select>
          </label>
        </span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { useSessionStore, DEPARTMENT_OPTIONS, ROLE_OPTIONS } from '@/stores/session'

const store = useSessionStore()

function onDepartmentChange(event: Event) {
  store.setDepartment((event.target as HTMLSelectElement).value)
}

function onRoleChange(event: Event) {
  store.setRole((event.target as HTMLSelectElement).value)
}

const navItems = [{ label: "运营概览", path: "/" }, { label: "ETL作业", path: "/etljob" }, { label: "任务编排", path: "/dag" }, { label: "依赖解析", path: "/dependency" }, { label: "源端校验", path: "/sourcecheck" }, { label: "运行实例", path: "/runtime" }, { label: "异常重跑", path: "/rerun" }, { label: "基线管理", path: "/baseline" }, { label: "分区检测", path: "/partitioncheck" }, { label: "数据比对", path: "/comparator" }, { label: "元数据存储", path: "/metastore" }, { label: "资源配置", path: "/resource" }, { label: "SLA监控", path: "/slavalidate" }, { label: "SQL模板", path: "/sqltpl" }, { label: "数据备份", path: "/backuprestore" }, { label: "日志归档", path: "/logarchive" }, { label: "通知渠道", path: "/notifychan" }, { label: "发布计划", path: "/publishplan" }]
</script>
