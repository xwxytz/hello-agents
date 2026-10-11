import axios from 'axios'
import type { TripFormData, TripPlanResponse, TripRecord, TripShareInfo } from '@/types'

// 用 ?? 而不是 ||：Docker 部署时 VITE_API_BASE_URL 会被显式传成空串，
// 表示「走相对路径 /api，由 nginx 反代到后端」。用 || 的话空串会被当成未设置，
// 回落到 localhost:8000 —— 浏览器于是去连自己机器的 8000 端口，容器里必然失败。
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000, // 2分钟超时
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    return Promise.reject(error)
  }
)

/**
 * 生成旅行计划
 */
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
    return response.data
  } catch (error: any) {
    console.error('生成旅行计划失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

export type StreamEvent = {
  type: 'stage' | 'progress' | 'token' | 'result' | 'error'
  stage?: string
  status?: 'start' | 'end'
  label?: string
  index?: number
  total?: number
  percent?: number
  data?: any
  /** result 事件里附带的分享信息（后端保存成功后才有） */
  share?: TripShareInfo
  code?: string
  message?: string
}

/**
 * 流式生成旅行计划（SSE）
 *
 * EventSource 不支持 POST，因此这里用 fetch + ReadableStream 手动解析
 * text/event-stream：以空行分隔每个事件，取 data: 行反序列化。
 */
export async function generateTripPlanStream(
  formData: TripFormData,
  onEvent: (event: StreamEvent) => void
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/trip/plan/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(formData)
  })

  if (!response.ok || !response.body) {
    throw new Error(`请求失败: ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const chunks = buffer.split('\n\n')
    // 最后一段可能是不完整的事件，留到下一次拼接
    buffer = chunks.pop() ?? ''

    for (const chunk of chunks) {
      const dataLine = chunk.split('\n').find(line => line.startsWith('data:'))
      if (!dataLine) continue
      try {
        onEvent(JSON.parse(dataLine.slice(5).trim()))
      } catch (e) {
        console.error('SSE 事件解析失败:', dataLine)
      }
    }
  }
}

/**
 * 按分享令牌读取行程
 *
 * 这是「刷新不丢 + 可分享」的读取侧：直接打开 /result?share=xxx 时，
 * 页面靠这个接口拿行程，而不是依赖本机的 sessionStorage。
 */
export async function fetchTripByShareToken(token: string): Promise<TripRecord> {
  const response = await fetch(`${API_BASE_URL}/api/trips/${encodeURIComponent(token)}`)
  if (!response.ok) {
    let message = `读取分享行程失败（${response.status}）`
    try {
      const body = await response.json()
      message = body?.detail?.message || body?.message || message
    } catch (e) {
      // 响应不是 JSON（例如网关返回 HTML），用默认文案即可
    }
    throw new Error(message)
  }
  const body = await response.json()
  if (!body?.success || !body?.data?.plan) {
    throw new Error('分享行程内容为空')
  }
  return body.data as TripRecord
}

/** 把分享路径拼成可复制的完整链接 */
export function buildShareUrl(sharePath: string): string {
  if (/^https?:\/\//.test(sharePath)) return sharePath
  return `${window.location.origin}${sharePath}`
}

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

export default apiClient

