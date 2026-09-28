import { createApp, ref } from 'vue'
import ElementPlus, { ElMessage } from 'element-plus'
import 'element-plus/dist/index.css'
// 只保留中文：控制台面向中文用户，不再引入 en / ja / ko 语言包（AstrBot 侧同样只随包发布 zh）
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import bridge from './api'
import App from './App.vue'

// 桥接上下文（locale/i18n/isDark）——全局 provide
const ctx = ref({ locale: 'zh-CN', i18n: {}, isDark: false })

async function init() {
  try {
    const c = await bridge.ready()
    ctx.value = { ...ctx.value, ...c }
  } catch (_e) {
    // 桥接不可用（本地开发），保持默认
  }

  const app = createApp(App)
  app.provide('bridgeCtx', ctx)
  // 供各面板 inject('bridge') 使用（api.js 默认导出的 bridge 适配层）
  app.provide('bridge', bridge)
  // 只保留中文：Element Plus 文案固定中文，不再跟随 Dashboard 语言
  app.provide('locale', zhCn)
  // 全局轻提示（成功/失败反馈）
  app.provide('notify', {
    success: (msg) => ElMessage.success(msg),
    error: (msg) => ElMessage.error(msg),
    info: (msg) => ElMessage.info(msg),
  })
  app.use(ElementPlus, { locale: zhCn })
  app.mount('#app')

  // 跟随 Dashboard 主题（亮/暗）
  const applyTheme = () => {
    const dark = ctx.value.isDark
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
  }
  applyTheme()
  if (window.AstrBotPluginPage?.onContext) {
    window.AstrBotPluginPage.onContext((c) => {
      ctx.value = { ...ctx.value, ...c }
      applyTheme()
    })
  }
}

init()