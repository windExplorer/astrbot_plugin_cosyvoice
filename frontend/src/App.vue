<template>
  <div class="cosyvoice-app">
    <el-tabs v-model="activeTab" class="main-tabs">
      <el-tab-pane label="概览" name="overview">
        <OverviewPanel />
      </el-tab-pane>
      <el-tab-pane label="音色管理" name="voices">
        <VoicesPanel />
      </el-tab-pane>
      <el-tab-pane label="会话管理" name="sessions">
        <SessionsPanel />
      </el-tab-pane>
      <el-tab-pane label="配置" name="config">
        <ConfigPanel />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import OverviewPanel from './components/OverviewPanel.vue'
import VoicesPanel from './components/VoicesPanel.vue'
import SessionsPanel from './components/SessionsPanel.vue'
import ConfigPanel from './components/ConfigPanel.vue'

const activeTab = ref('overview')
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
  /* 1200px 居中 */
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px;
}
.main-tabs .el-tabs__header {
  margin-bottom: 16px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.muted { color: var(--cv-muted); font-size: 12px; }
.toolbar { margin-bottom: 12px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
</style>