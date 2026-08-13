import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 构建产物输出到插件根下的 pages/cosyvoice/（AstrBot 插件 Pages 自动发现目录）。
// 注意：
//  - base 必须用相对路径 './'，AstrBot 会重写页面内相对资源引用并追加 asset_token；
//  - 使用 hash routing（AstrBot 静态资源按真实文件路径解析，history 模式刷新会 404）。
export default defineConfig({
  plugins: [vue()],
  base: './',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../pages/cosyvoice',
    emptyOutDir: true,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    proxy: {
      // 本地开发时代理到 AstrBot Dashboard 的插件扩展 API
      '/api': {
        target: 'http://127.0.0.1:6185',
        changeOrigin: true,
      },
    },
  },
})