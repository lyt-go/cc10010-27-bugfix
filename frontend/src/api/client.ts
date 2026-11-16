/** 统一请求封装：拼后端地址、自动带部门/角色头、抛网络错误、给页脚留可读说明。 */
import { useSessionStore } from '@/stores/session'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

function authHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers)
  headers.set('Content-Type', 'application/json')
  // 会话身份随每个请求下发，后端据此做部门归属与只读权限判定
  const session = useSessionStore()
  headers.set('X-Department-Id', session.department)
  headers.set('X-User-Role', session.role)
  return headers
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    ...init,
    headers: authHeaders(init),
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}
