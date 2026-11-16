<template>
  <section class="page" data-module="runtime">
    <header class="page-head">
      <div>
        <h2>运行实例管理</h2>
        <p class="page-desc">记录每次ETL作业的运行实例；挂接资源池的回填启动前复核白名单、停用状态与剩余槽位，并同步回填进度。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="store.isReadonly" @click="openCreate">登记运行记录</button>
        <button class="btn" type="button" @click="exportRows">导出运行实例清单</button>
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
          <th>资源池</th>
          <th>回填编号</th>
          <th>进度</th>
          <th>配置版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ poolLabel(row['资源池id']) }}</td>
          <td>{{ row['回填编号'] ?? '—' }}</td>
          <td>{{ row['进度'] ?? 0 }}%</td>
          <td>{{ row['配置版本'] ? `v${row['配置版本']}` : '—' }}</td>
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
            <button
              class="link"
              type="button"
              :disabled="store.isReadonly || row.status !== '运行中'"
              @click="openProgress(row)"
            >
              更新回填进度
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 5" class="empty-state">暂无运行实例数据，可先登记运行记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条运行实例记录 · 当前身份：{{ store.departmentName }} / {{ roleName }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="createVisible" class="modal-mask" @click.self="createVisible = false">
      <div class="modal-panel">
        <h3 class="modal-title">登记运行记录</h3>
        <div class="form-grid">
          <label class="form-field">
            记录编号
            <input v-model="createModel['记录编号']" />
          </label>
          <label class="form-field">
            作业名称
            <input v-model="createModel['作业名称']" />
          </label>
          <label class="form-field">
            开始时间
            <input v-model="createModel['开始时间']" placeholder="YYYY-MM-DD HH:MM:SS" />
          </label>
          <label class="form-field">
            资源池id（可选，挂接后启动走回填准入）
            <input v-model="createModel['资源池id']" type="number" min="1" step="1" />
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="createVisible = false">取消</button>
          <button class="btn primary" type="button" @click="saveCreate">保存</button>
        </div>
      </div>
    </div>

    <div v-if="progressVisible" class="modal-mask" @click.self="progressVisible = false">
      <div class="modal-panel">
        <h3 class="modal-title">更新回填进度 · {{ progressTarget?.['记录编号'] }}</h3>
        <div class="form-grid">
          <label class="form-field">
            进度（0-100）
            <input v-model="progressModel['进度']" type="number" min="0" max="100" step="1" />
          </label>
          <label class="form-field">
            读取行数（非负整数）
            <input v-model="progressModel['读取行数']" type="number" min="0" step="1" />
          </label>
          <label class="form-field">
            写入行数（非负整数）
            <input v-model="progressModel['写入行数']" type="number" min="0" step="1" />
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="progressVisible = false">取消</button>
          <button class="btn primary" type="button" @click="saveProgress">保存进度</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { ROLE_OPTIONS, useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/runtime'
const columns = ['记录编号', '作业名称', '开始时间', '结束时间', '执行状态', '读取行数', '写入行数', '错误信息']
const actions = ['启动作业', '终止作业', '标记成功']
const stats = ref([
  { label: '运行记录总数', value: 0 },
  { label: '今日运行数', value: 0 },
  { label: '失败记录数', value: 0 },
])

const store = useSessionStore()
const roleName = computed(() => ROLE_OPTIONS.find((item) => item.code === store.role)?.name ?? store.role)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const pools = ref<Row[]>([])

const createVisible = ref(false)
const emptyCreate = (): Record<string, string> => ({
  记录编号: '',
  作业名称: '',
  开始时间: '',
  资源池id: '',
})
const createModel = ref<Record<string, string>>(emptyCreate())

const progressVisible = ref(false)
const progressTarget = ref<Row | null>(null)
const progressModel = ref<Record<string, string>>({ 进度: '', 读取行数: '', 写入行数: '' })

function poolLabel(poolId: string | number | null | undefined): string {
  if (poolId === null || poolId === undefined || poolId === '') return '未挂接'
  const pool = pools.value.find((item) => Number(item.id) === Number(poolId))
  return pool ? String(pool['池编号']) : `池#${poolId}`
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createModel.value = emptyCreate()
  createVisible.value = true
}

function openProgress(row: Row) {
  progressTarget.value = row
  progressModel.value = {
    进度: String(row['进度'] ?? 0),
    读取行数: String(row['读取行数'] ?? 0),
    写入行数: String(row['写入行数'] ?? 0),
  }
  progressVisible.value = true
}

async function saveCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createModel.value }),
    })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      throw new Error(body.detail || body.message || '运行记录登记失败')
    }
    createVisible.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行记录登记失败'
  }
}

async function saveProgress() {
  if (!progressTarget.value) return
  errorMessage.value = ''
  const values: Record<string, number> = {}
  for (const field of ['进度', '读取行数', '写入行数'] as const) {
    const text = progressModel.value[field].trim()
    if (text !== '') values[field] = Number(text)
  }
  try {
    const response = await request(`${ENDPOINT}/${progressTarget.value.id}/progress`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      throw new Error(body.detail || body.message || '进度更新失败')
    }
    progressVisible.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '进度更新失败'
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
      throw new Error(body.detail || body.message || '运行实例动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResp, poolResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request('/api/resource?size=200'),
    ])
    if (!listResp.ok) throw new Error('运行记录列表读取失败')
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '运行记录总数', value: total.value },
      { label: '今日运行数', value: rows.value.filter((row) => row.status === '运行中').length },
      { label: '失败记录数', value: rows.value.filter((row) => row.status === '失败').length },
    ]
    if (poolResp.ok) {
      pools.value = (await poolResp.json()).items ?? []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例列表读取失败'
  }
}

onMounted(reload)
</script>
