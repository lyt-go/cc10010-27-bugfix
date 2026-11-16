import { defineStore } from 'pinia'

/** 部门目录：HTTP 头只能承载 ASCII，提交编码，由后端解析为中文部门名。 */
export const DEPARTMENT_OPTIONS = [
  { code: 'DATA_PLATFORM', name: '数据平台部' },
  { code: 'RISK_DATA', name: '风控数据部' },
  { code: 'DATA_INFRA', name: '数据基础设施部' },
]

export const ROLE_OPTIONS = [
  { code: 'writer', name: '可编辑成员' },
  { code: 'readonly', name: '只读成员' },
]

const STORAGE_KEY = 'etl-session'

function loadPersisted(): { department: string; role: string } {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { department?: string; role?: string }
      return {
        department: parsed.department ?? 'DATA_PLATFORM',
        role: parsed.role ?? 'writer',
      }
    }
  } catch {
    // 本地存储不可用时退回默认身份
  }
  return { department: 'DATA_PLATFORM', role: 'writer' }
}

const persisted = loadPersisted()

export const useSessionStore = defineStore('session', {
  state: () => ({
    operator: '值班管理员',
    shiftLabel: '白班 08:00-20:00',
    scope: '数据仓库ETL任务编排平台',
    department: persisted.department,
    role: persisted.role,
  }),
  getters: {
    canOperate: (state) => state.operator.length > 0,
    isReadonly: (state) => state.role === 'readonly',
    departmentName: (state) =>
      DEPARTMENT_OPTIONS.find((item) => item.code === state.department)?.name ?? state.department,
  },
  actions: {
    setShift(label: string) {
      this.shiftLabel = label
    },
    setDepartment(code: string) {
      this.department = code
      this.persist()
    },
    setRole(code: string) {
      this.role = code
      this.persist()
    },
    persist() {
      try {
        window.localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({ department: this.department, role: this.role }),
        )
      } catch {
        // 忽略持久化失败，会话内身份仍然生效
      }
    },
  },
})
