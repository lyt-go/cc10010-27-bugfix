<template>
  <section class="page" data-module="resource">
    <header class="page-head">
      <div>
        <h2>资源配置管理</h2>
        <p class="page-desc">管理ETL任务的执行资源池，控制并发度、内存和CPU配额；停用池拒绝新回填，白名单外作业不准入。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="store.isReadonly" @click="openCreate">登记资源池</button>
        <button class="btn" type="button" @click="recompute">重算并发占用</button>
        <button class="btn" type="button" @click="exportRows">导出资源配置清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>并发占用</th>
          <th>剩余槽位</th>
          <th>配置版本</th>
          <th>归属部门</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
          <td>{{ occupancyOf(row.id)?.并发占用 ?? '—' }} / {{ row['最大并发数'] }}</td>
          <td>{{ occupancyOf(row.id)?.剩余槽位 ?? '—' }}</td>
          <td>v{{ row['配置版本'] }}</td>
          <td>{{ row['归属部门'] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :disabled="store.isReadonly"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :disabled="store.isReadonly" @click="openEdit(row)">修改配置</button>
            <button class="link" type="button" :disabled="store.isReadonly" @click="openBackfill(row)">提交回填</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 5" class="empty-state">暂无资源配置数据，可先登记资源池</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">并发占用数据记录（运行实例 + 回填积压 双来源重算）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>池编号</th>
          <th>池名称</th>
          <th>最大并发数</th>
          <th>运行实例占用</th>
          <th>回填占用</th>
          <th>并发占用</th>
          <th>剩余槽位</th>
          <th>资源池状态</th>
          <th>配置版本</th>
          <th>更新时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in occupancyRows" :key="String(item.id)">
          <td>{{ item['池编号'] }}</td>
          <td>{{ item['池名称'] }}</td>
          <td>{{ item['最大并发数'] }}</td>
          <td>{{ item['运行实例占用'] }}</td>
          <td>{{ item['回填占用'] }}</td>
          <td>{{ item['并发占用'] }}</td>
          <td>{{ item['剩余槽位'] }}</td>
          <td>{{ item['资源池状态'] }}</td>
          <td>v{{ item['配置版本'] }}</td>
          <td>{{ item['更新时间'] }}</td>
        </tr>
        <tr v-if="!occupancyRows.length">
          <td colspan="10" class="empty-state">暂无占用记录，可点击「重算并发占用」生成</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">数据回填积压</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>回填编号</th>
          <th>作业名称</th>
          <th>资源池</th>
          <th>提交部门</th>
          <th>状态</th>
          <th>进度</th>
          <th>准入配置版本</th>
          <th>拒绝/排队原因</th>
          <th>更新时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in backlogRows" :key="String(item.id)">
          <td>{{ item['回填编号'] }}</td>
          <td>{{ item['作业名称'] }}</td>
          <td>{{ item['池编号'] }}</td>
          <td>{{ item['提交部门'] }}</td>
          <td><span :class="['tag', statusTag(item['状态'])]">{{ item['状态'] }}</span></td>
          <td>{{ item['进度'] }}%</td>
          <td>{{ item['准入配置版本'] ? `v${item['准入配置版本']}` : '—' }}</td>
          <td>{{ item['拒绝原因'] || '—' }}</td>
          <td>{{ item['更新时间'] }}</td>
        </tr>
        <tr v-if="!backlogRows.length">
          <td colspan="9" class="empty-state">暂无回填积压数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条资源配置记录 · 当前身份：{{ store.departmentName }} / {{ roleName }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="formVisible" class="modal-mask" @click.self="formVisible = false">
      <div class="modal-panel">
        <h3 class="modal-title">{{ editingId ? '修改资源池配置（保存后配置版本递增）' : '登记资源池' }}</h3>
        <div class="form-grid">
          <label class="form-field">
            池编号
            <input v-model="formModel['池编号']" :disabled="Boolean(editingId)" />
          </label>
          <label class="form-field">
            池名称
            <input v-model="formModel['池名称']" />
          </label>
          <label class="form-field">
            最大并发数（正整数）
            <input v-model="formModel['最大并发数']" type="number" min="1" step="1" />
          </label>
          <label class="form-field">
            内存上限MB（正整数）
            <input v-model="formModel['内存上限MB']" type="number" min="1" step="1" />
          </label>
          <label class="form-field">
            CPU核心数（正整数）
            <input v-model="formModel['CPU核心数']" type="number" min="1" step="1" />
          </label>
          <label class="form-field">
            队列策略
            <select v-model="formModel['队列策略']">
              <option value="FIFO">FIFO</option>
              <option value="优先级">优先级</option>
            </select>
          </label>
          <label class="form-field">
            抢占开关
            <select v-model="formModel['抢占开关']">
              <option value="false">关闭</option>
              <option value="true">开启</option>
            </select>
          </label>
          <label class="form-field">
            归属部门
            <input v-model="formModel['归属部门']" />
          </label>
          <label class="form-field wide">
            作业白名单（逗号分隔，留空表示不限制）
            <input v-model="formModel['作业白名单']" placeholder="例如：订单日汇总作业,库存快照回填" />
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="formVisible = false">取消</button>
          <button class="btn primary" type="button" @click="saveForm">保存</button>
        </div>
      </div>
    </div>

    <div v-if="backfillVisible" class="modal-mask" @click.self="backfillVisible = false">
      <div class="modal-panel">
        <h3 class="modal-title">向 {{ backfillTarget?.['池名称'] }} 提交回填作业</h3>
        <div class="form-grid">
          <label class="form-field wide">
            作业名称（必须在白名单内：{{ whitelistText(backfillTarget) || '不限制' }}）
            <input v-model="backfillJob" placeholder="例如：订单日汇总作业" />
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="backfillVisible = false">取消</button>
          <button class="btn primary" type="button" @click="submitBackfill">提交准入</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { ROLE_OPTIONS, useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | boolean | string[] | null>

const ENDPOINT = '/api/resource'
const columns = ['池编号', '池名称', '最大并发数', '内存上限MB', 'CPU核心数', '队列策略', '抢占开关', '作业白名单']
const actions = ['提交配置评审', '启用配置', '停用配置']
const stats = ref([
  { label: '资源池总数', value: 0 },
  { label: '已启用配置', value: 0 },
  { label: '评审中配置', value: 0 },
])

const store = useSessionStore()
const roleName = computed(() => ROLE_OPTIONS.find((item) => item.code === store.role)?.name ?? store.role)

const rows = ref<Row[]>([])
const total = ref(0)
const occupancyRows = ref<Row[]>([])
const backlogRows = ref<Row[]>([])
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const formVisible = ref(false)
const editingId = ref<number | null>(null)
const emptyForm = (): Record<string, string> => ({
  池编号: '',
  池名称: '',
  最大并发数: '',
  内存上限MB: '',
  CPU核心数: '',
  队列策略: 'FIFO',
  抢占开关: 'false',
  作业白名单: '',
  归属部门: store.departmentName,
})
const formModel = ref<Record<string, string>>(emptyForm())

const backfillVisible = ref(false)
const backfillTarget = ref<Row | null>(null)
const backfillJob = ref('')

function whitelistText(row: Row | null | undefined): string {
  if (!row) return ''
  const value = row['作业白名单']
  return Array.isArray(value) ? value.join(',') : String(value ?? '')
}

function formatCell(row: Row, column: string): string {
  const value = row[column]
  if (Array.isArray(value)) return value.join('、')
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function occupancyOf(id: Row[keyof Row]): Row | undefined {
  return occupancyRows.value.find((item) => Number(item['资源池id']) === Number(id))
}

function statusTag(status: Row[keyof Row]): string {
  const value = String(status ?? '')
  if (value === '运行中') return 'tag-running'
  if (value === '排队中') return 'tag-queued'
  if (value === '已完成') return 'tag-done'
  if (value === '已拒绝') return 'tag-rejected'
  return 'tag-terminal'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  editingId.value = null
  formModel.value = emptyForm()
  formVisible.value = true
}

function openEdit(row: Row) {
  editingId.value = Number(row.id)
  formModel.value = {
    池编号: String(row['池编号'] ?? ''),
    池名称: String(row['池名称'] ?? ''),
    最大并发数: String(row['最大并发数'] ?? ''),
    内存上限MB: String(row['内存上限MB'] ?? ''),
    CPU核心数: String(row['CPU核心数'] ?? ''),
    队列策略: String(row['队列策略'] ?? 'FIFO'),
    抢占开关: String(Boolean(row['抢占开关'])),
    作业白名单: whitelistText(row),
    归属部门: String(row['归属部门'] ?? ''),
  }
  formVisible.value = true
}

function openBackfill(row: Row) {
  backfillTarget.value = row
  backfillJob.value = ''
  backfillVisible.value = true
}

async function saveForm() {
  errorMessage.value = ''
  const payload: Record<string, unknown> = { ...formModel.value }
  payload['抢占开关'] = formModel.value['抢占开关'] === 'true'
  const url = editingId.value ? `${ENDPOINT}/${editingId.value}` : ENDPOINT
  const method = editingId.value ? 'PUT' : 'POST'
  try {
    const response = await request(url, { method, body: JSON.stringify({ values: payload }) })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      throw new Error(body.detail || body.message || '资源池保存失败')
    }
    formVisible.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '资源池保存失败'
  }
}

async function submitBackfill() {
  errorMessage.value = ''
  if (!backfillTarget.value) return
  try {
    const response = await request(`${ENDPOINT}/backlog`, {
      method: 'POST',
      body: JSON.stringify({
        values: { 资源池id: backfillTarget.value.id, 作业名称: backfillJob.value.trim() },
      }),
    })
    const body = await response.json()
    if (!response.ok) {
      throw new Error(body.detail || '回填提交失败')
    }
    backfillVisible.value = false
    if (body.ok === false) {
      errorMessage.value = body.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回填提交失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      throw new Error(body.detail || body.message || '资源配置动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '资源配置操作失败'
  }
}

async function recompute() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/recompute`, { method: 'POST', body: JSON.stringify({}) })
    const body = await response.json()
    if (!response.ok) {
      throw new Error(body.detail || '重算失败')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算并发占用失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResp, occResp, backlogResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/occupancy`),
      request(`${ENDPOINT}/backlog?size=200`),
    ])
    if (!listResp.ok) throw new Error('资源池列表读取失败')
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '资源池总数', value: total.value },
      { label: '已启用配置', value: rows.value.filter((row) => row.status === '已启用').length },
      { label: '评审中配置', value: rows.value.filter((row) => row.status === '配置评审中').length },
    ]
    if (occResp.ok) {
      occupancyRows.value = (await occResp.json()).items ?? []
    }
    if (backlogResp.ok) {
      backlogRows.value = (await backlogResp.json()).items ?? []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '资源配置列表读取失败'
  }
}

onMounted(reload)
</script>
