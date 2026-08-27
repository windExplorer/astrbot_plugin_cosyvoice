<template>
  <div class="cv-app">
    <header class="cv-header">
      <div class="cv-brand">
        <div class="cv-logo"><el-icon><Microphone /></el-icon></div>
        <div class="cv-brand-text">
          <div class="cv-title">CosyVoice 语音控制台</div>
          <div class="cv-sub">本地 CosyVoice3 · 音色管理 · 多语言翻译合成</div>
        </div>
      </div>
      <div class="cv-header-actions">
        <el-tag v-if="isDark" size="small" effect="dark" round>暗色</el-tag>
        <el-tooltip content="刷新当前页" placement="bottom">
          <el-button size="small" circle @click="refreshAll"><el-icon><Refresh /></el-icon></el-button>
        </el-tooltip>
      </div>
    </header>

    <el-tabs v-model="activeTab" class="cv-tabs" @tab-change="onTabChange">
      <el-tab-pane label="概览" name="overview" />
      <el-tab-pane label="音色管理" name="voices" />
      <el-tab-pane label="翻译合成" name="translate" />
      <el-tab-pane label="会话" name="sessions" />
      <el-tab-pane label="配置" name="config" />
    </el-tabs>

    <main class="cv-content">
      <OverviewPanel v-if="activeTab === 'overview'" ref="overviewRef" />
      <VoicesPanel v-else-if="activeTab === 'voices'" ref="voicesRef" />
      <TranslatePanel v-else-if="activeTab === 'translate'" ref="translateRef" />
      <SessionsPanel v-else-if="activeTab === 'sessions'" ref="sessionsRef" />
      <ConfigPanel v-else-if="activeTab === 'config'" ref="configRef" />
    </main>
  </div>
</template>

<script setup>
import { ref, inject, computed, nextTick } from 'vue'
import { Refresh, Microphone } from '@element-plus/icons-vue'
import OverviewPanel from './components/OverviewPanel.vue'
import VoicesPanel from './components/VoicesPanel.vue'
import TranslatePanel from './components/TranslatePanel.vue'
import SessionsPanel from './components/SessionsPanel.vue'
import ConfigPanel from './components/ConfigPanel.vue'

const ctx = inject('bridgeCtx')
const isDark = computed(() => ctx && ctx.value && ctx.value.isDark)

const activeTab = ref('overview')
const overviewRef = ref(null)
const voicesRef = ref(null)
const translateRef = ref(null)
const sessionsRef = ref(null)
const configRef = ref(null)

function refreshAll() {
  const map = {
    overview: overviewRef,
    voices: voicesRef,
    translate: translateRef,
    sessions: sessionsRef,
    config: configRef,
  }
  const r = map[activeTab.value]
  if (r && r.value && typeof r.value.load === 'function') {
    r.value.load()
  }
}
function onTabChange() {
  nextTick(refreshAll)
}
</script>

<style>
/* ===== 全局设计 token（亮/暗）+ 基础排版 ===== */
:root {
  --cv-bg: #f3f4f8;
  --cv-panel: #ffffff;
  --cv-panel-2: #fafbff;
  --cv-text: #1f2330;
  --cv-text-2: #6b7280;
  --cv-border: #e9ebf2;
  --cv-primary: #6d5efc;
  --cv-primary-2: #8b7dff;
  --cv-primary-soft: #efeaff;
  --cv-success: #18a957;
  --cv-danger: #ef4444;
  --cv-warn: #f59e0b;
  --cv-radius: 14px;
  --cv-radius-sm: 10px;
  --cv-shadow: 0 8px 24px rgba(31, 35, 48, 0.06);
  --cv-shadow-sm: 0 2px 8px rgba(31, 35, 48, 0.05);
}
[data-theme='dark'] {
  --cv-bg: #121319;
  --cv-panel: #1c1e27;
  --cv-panel-2: #181a22;
  --cv-text: #e7e9f1;
  --cv-text-2: #9aa1b4;
  --cv-border: #2b2e3a;
  --cv-primary: #8b7dff;
  --cv-primary-2: #a99bff;
  --cv-primary-soft: #27243e;
  --cv-shadow: 0 8px 24px rgba(0, 0, 0, 0.34);
  --cv-shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.3);
}

* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body {
  background: var(--cv-bg);
  color: var(--cv-text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  font-size: 14px;
}

.cv-app {
  min-height: 100%;
  padding: 18px 22px 32px;
  max-width: 1180px;
  margin: 0 auto;
}

.cv-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.cv-brand { display: flex; align-items: center; gap: 12px; }
.cv-logo {
  width: 42px; height: 42px; border-radius: 12px;
  display: grid; place-items: center;
  background: linear-gradient(135deg, var(--cv-primary), var(--cv-primary-2));
  color: #fff; font-size: 20px;
  box-shadow: var(--cv-shadow-sm);
}
.cv-title { font-size: 18px; font-weight: 700; letter-spacing: .3px; }
.cv-sub { font-size: 12px; color: var(--cv-text-2); margin-top: 2px; }
.cv-header-actions { display: flex; align-items: center; gap: 10px; }

/* Tabs 美化：胶囊化头部 */
.cv-tabs .el-tabs__header {
  margin: 0 0 16px;
  border: none;
  background: var(--cv-panel);
  border-radius: var(--cv-radius);
  padding: 6px;
  box-shadow: var(--cv-shadow-sm);
}
.cv-tabs .el-tabs__nav {
  border: none !important;
  background: transparent;
  display: flex;
  gap: 4px;
}
.cv-tabs .el-tabs__item {
  border: none !important;
  border-radius: 10px;
  height: 38px;
  line-height: 38px;
  color: var(--cv-text-2);
  font-weight: 600;
  transition: all .2s;
}
.cv-tabs .el-tabs__item:hover { color: var(--cv-primary); }
.cv-tabs .el-tabs__item.is-active {
  background: var(--cv-primary-soft);
  color: var(--cv-primary);
}
.cv-tabs .el-tabs__active-bar { display: none; }

.cv-content { animation: cv-fade .25s ease; }
@keyframes cv-fade {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}

/* 通用卡片 / 区块 / 工具类 */
.cv-card {
  background: var(--cv-panel);
  border: 1px solid var(--cv-border);
  border-radius: var(--cv-radius);
  box-shadow: var(--cv-shadow-sm);
  padding: 18px;
}
.cv-section-title {
  font-size: 14px; font-weight: 700; margin: 0 0 12px;
  display: flex; align-items: center; gap: 8px;
}
.cv-muted { color: var(--cv-text-2); font-size: 12px; }
.cv-toolbar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  margin-bottom: 14px;
}
.cv-grid { display: grid; gap: 14px; }
.cv-spacer { flex: 1; }
</style>
