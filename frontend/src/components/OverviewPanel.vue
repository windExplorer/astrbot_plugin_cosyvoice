<template>
  <div>
    <div v-loading="loading" class="overview-wrap">
      <el-alert
        v-if="data.server_down"
        type="warning"
        :title="t('overview.serverDown', '语音服务器失联（冷却中）')"
        :description="t('overview.cooldownDesc', `剩余 ${data.cooldown_remaining}s 不再发 TTS 请求`)"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      />

      <div class="card-grid">
        <el-card shadow="never">
          <template #header>{{ t('overview.health', '服务端健康') }}</template>
          <div v-for="(s, i) in data.servers" :key="i" class="server-row">
            <el-tag :type="s.status === 'ok' ? 'success' : 'danger'" size="small">
              {{ s.status === 'ok' ? 'OK' : s.status }}
            </el-tag>
            <span class="server-url" :title="s.url">{{ s.url }}</span>
            <span v-if="s.default" class="muted">★</span>
          </div>
          <div v-if="!data.servers.length" class="muted">{{ t('overview.noServer', '无服务端信息') }}</div>
        </el-card>

        <el-card shadow="never">
          <template #header>{{ t('overview.global', '全局配置') }}</template>
          <el-descriptions :column="1" size="small">
            <el-descriptions-item :label="t('overview.autoTts', '自动语音')">
              <el-tag :type="data.config.auto_tts ? 'success' : 'info'" size="small">
                {{ data.config.auto_tts ? 'ON' : 'OFF' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item :label="t('overview.sendMode', '发送方式')">{{ data.config.send_mode }}</el-descriptions-item>
            <el-descriptions-item :label="t('overview.scope', '语音范围')">{{ data.config.tts_scope }}</el-descriptions-item>
            <el-descriptions-item :label="t('overview.defaultVoice', '默认音色')">{{ data.config.default_voice || '-' }}</el-descriptions-item>
            <el-descriptions-item :label="t('overview.sampleRate', '采样率')">{{ data.config.sample_rate }} Hz</el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never">
          <template #header>{{ t('overview.stats', '统计') }}</template>
          <el-descriptions :column="1" size="small">
            <el-descriptions-item :label="t('overview.voiceCount', '音色数量')">{{ data.voice_count }}</el-descriptions-item>
            <el-descriptions-item :label="t('overview.sessionCount', '已开启会话')">{{ data.session_count }}</el-descriptions-item>
            <el-descriptions-item :label="t('overview.baseUrl', '服务地址')">{{ data.config.base_url }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </div>

      <el-button size="small" :loading="loading" @click="load">{{ t('overview.refresh', '刷新') }}</el-button>
    </div>
  </div>
</template>

<script setup>
import { ref, inject, onMounted } from 'vue'
import bridge from '../api'

const ctx = inject('bridgeCtx')
const notify = inject('notify')
const data = ref({ servers: [], config: {}, voice_count: 0, session_count: 0, server_down: false, cooldown_remaining: 0 })
const loading = ref(false)

const t = (key, fb) => {
  try { return bridge.t(key, fb) } catch (_e) { return fb || key }
}

async function load() {
  loading.value = true
  try {
    data.value = await bridge.apiGet('overview')
  } catch (e) {
    notify.error(e.message || 'load failed')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.server-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.server-url {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  font-size: 12px;
}
</style>