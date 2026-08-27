import { createApp, ref, computed } from 'vue'
import ElementPlus, { ElMessage } from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import en from 'element-plus/es/locale/lang/en'
import ja from 'element-plus/es/locale/lang/ja'
import ko from 'element-plus/es/locale/lang/ko'
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

  const localeMap = {
    'zh-CN': zhCn,
    'zh': zhCn,
    'en': en,
    'ja': ja,
    'ko': ko,
  }
  const app = createApp(App)
  app.provide('bridgeCtx', ctx)
  // 供各面板 inject('bridge') 使用（api.js 默认导出的 bridge 适配层）
  app.provide('bridge', bridge)
  app.provide('locale', computed(() => localeMap[ctx.value.locale] || zhCn))
  // 全局轻提示（成功/失败反馈）
  app.provide('notify', {
    success: (msg) => ElMessage.success(msg),
    error: (msg) => ElMessage.error(msg),
    info: (msg) => ElMessage.info(msg),
  })
  app.use(ElementPlus, { locale: computed(() => localeMap[ctx.value.locale] || zhCn) })
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