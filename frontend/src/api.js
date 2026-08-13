// AstrBot 插件 Pages bridge 适配层。
//
// - 在 AstrBot Dashboard 内：window.AstrBotPluginPage 提供了 apiGet/apiPost/
//   download/upload/ready/t/getLocale 等能力（受限 iframe，不能直接用 fetch
//   访问 /api/plug/...，必须走 bridge）。
// - 本地开发（vite dev）：bridge 不存在，退化为原生 fetch，指向 vite 代理的
//   /api/plug/astrbot_plugin_cosyvoice/...。

const PLUGIN_ROUTE_PREFIX = 'astrbot_plugin_cosyvoice'

function hasBridge() {
  return typeof window !== 'undefined' && !!window.AstrBotPluginPage
}

// 供本地开发使用的 fetch 适配（直接打 /api/plug/<前缀>/...）
async function devApi(endpoint, { method = 'GET', body, params } = {}) {
  let url = `/api/plug/${PLUGIN_ROUTE_PREFIX}/${endpoint}`
  if (params && Object.keys(params).length) {
    const qs = new URLSearchParams(params).toString()
    url += `?${qs}`
  }
  const resp = await fetch(url, {
    method,
    headers: body != null ? { 'Content-Type': 'application/json' } : {},
    body: body != null ? JSON.stringify(body) : undefined,
  })
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`
    try {
      const data = await resp.json()
      if (data && data.message) msg = data.message
    } catch (_e) { /* ignore */ }
    throw new Error(msg)
  }
  return resp.json()
}

const bridge = {
  ready: () => (hasBridge() ? window.AstrBotPluginPage.ready() : Promise.resolve({
    pluginName: PLUGIN_ROUTE_PREFIX,
    pageName: 'cosyvoice',
    locale: 'zh-CN',
    i18n: {},
    isDark: false,
  })),
  t: (key, fallback) => {
    if (hasBridge()) return window.AstrBotPluginPage.t(key, fallback)
    return fallback || key
  },
  getLocale: () => (hasBridge() ? window.AstrBotPluginPage.getLocale() : 'zh-CN'),
  apiGet: (endpoint, params) =>
    hasBridge()
      ? window.AstrBotPluginPage.apiGet(endpoint, params)
      : devApi(endpoint, { method: 'GET', params }),
  apiPost: (endpoint, body) =>
    hasBridge()
      ? window.AstrBotPluginPage.apiPost(endpoint, body)
      : devApi(endpoint, { method: 'POST', body }),
  download: (endpoint, params, filename) => {
    if (hasBridge()) {
      return window.AstrBotPluginPage.download(endpoint, params, filename)
    }
    // 本地开发：返回 blob URL（由调用方播放）
    return devApiBinary(endpoint, params).then(({ blob, filename: fn }) => ({
      blob,
      filename: fn,
    }))
  },
}

async function devApiBinary(endpoint, params) {
  let url = `/api/plug/${PLUGIN_ROUTE_PREFIX}/${endpoint}`
  if (params && Object.keys(params).length) {
    const qs = new URLSearchParams(params).toString()
    url += `?${qs}`
  }
  const resp = await fetch(url)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  const blob = await resp.blob()
  const cd = resp.headers.get('Content-Disposition') || ''
  const m = cd.match(/filename="?([^";]+)"?/)
  return { blob, filename: m ? m[1] : 'download' }
}

export default bridge