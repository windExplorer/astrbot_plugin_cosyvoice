<template>
  <div class="cosyvoice-app">
    <el-tabs v-model="activeTab" class="main-tabs">
      <el-tab-pane :label="t('overview.tab', '概览')" name="overview">
        <OverviewPanel />
      </el-tab-pane>
      <el-tab-pane :label="t('voices.tab', '音色管理')" name="voices">
        <VoicesPanel />
      </el-tab-pane>
      <el-tab-pane :label="t('sessions.tab', '会话管理')" name="sessions">
        <SessionsPanel />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, inject } from 'vue'
import bridge from './api'
import OverviewPanel from './components/OverviewPanel.vue'
import VoicesPanel from './components/VoicesPanel.vue'
import SessionsPanel from './components/SessionsPanel.vue'

const ctx = inject('bridgeCtx')
const activeTab = ref('overview')
const t = (key, fb) => {
  try {
    return bridge.t(key, fb)
  } catch (_e) {
    return fb || key
  }
}
</script>

<style>
:root {
  --cv-bg: #fff;
  --cv-text: #1f2329;
  --cv-border: #e4e7ed;
  --cv-muted: #909399;
}
[data-theme='dark'] {
  --cv-bg: #141414;
  --cv-text: #e5eaf3;
  --cv-border: #3a3a3a;
  --cv-muted: #8a8a8a;
}
body {
  background: var(--cv-bg);
  color: var(--cv-text);
  margin: 0;
  font-family: 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
}
.cosyvoice-app {
  padding: 12px;
}
.main-tabs .el-tabs__header {
  margin-bottom: 16px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.muted { color: var(--cv-muted); font-size: 12px; }
.toolbar { margin-bottom: 12px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
</style>