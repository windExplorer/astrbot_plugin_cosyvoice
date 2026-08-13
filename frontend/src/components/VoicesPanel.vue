<template>
  <div>
    <el-alert
      :title="t('voices.note', '音色列表由插件配置（_conf_schema.json）提供，WebUI 提供快捷操作与试听；隐藏/默认设置热生效，重启后以配置为准。')"
      type="info"
      :closable="false"
      style="margin-bottom: 12px"
    />
    <el-table :data="voices" v-loading="loading" border size="small">
      <el-table-column :label="t('voices.name', '音色名')" prop="name" min-width="120" />
      <el-table-column :label="t('voices.promptWav', '参考音频')" prop="prompt_wav" min-width="160" show-overflow-tooltip />
      <el-table-column :label="t('voices.promptText', '参考文本')" prop="prompt_text" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ row.prompt_text || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('voices.wavResolved', '音频可达')" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.wav_resolved ? 'success' : 'danger'" size="small">
            {{ row.wav_resolved ? 'OK' : 'MISS' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('voices.hidden', '隐藏')" width="80" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.hidden" size="small" @change="(v) => toggleHidden(row, v)" />
        </template>
      </el-table-column>
      <el-table-column :label="t('voices.actions', '操作')" width="210" align="center">
        <template #default="{ row }">
          <el-button size="small" type="primary" plain @click="play(row)">{{ t('voices.listen', '试听') }}</el-button>
          <el-button size="small" @click="setDefault(row)" :disabled="row.is_default">
            {{ row.is_default ? t('voices.default', '默认') : t('voices.setDefault', '设为默认') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="previewVisible" :title="t('voices.preview', '试听')" width="420px">
      <el-input
        v-model="previewText"
        type="textarea"
        :rows="3"
        :placeholder="t('voices.previewPlaceholder', '输入要试听的文本')"
      />
      <div class="preview-audio" v-if="previewUrl">
        <audio :src="previewUrl" controls style="width: 100%" />
      </div>
      <div class="muted" v-if="previewError">{{ previewError }}</div>
      <template #footer>
        <el-button @click="previewVisible = false">{{ t('common.cancel', '取消') }}</el-button>
        <el-button type="primary" :loading="previewLoading" @click="doPreview">{{ t('voices.synthesize', '合成试听') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, inject, onMounted } from 'vue'
import bridge from '../api'

const ctx = inject('bridgeCtx')
const notify = inject('notify')
const voices = ref([])
const loading = ref(false)
const previewVisible = ref(false)
const previewText = ref('你好，这是试听语音。')
const previewLoading = ref(false)
const previewUrl = ref('')
const previewError = ref('')
const currentVoice = ref('')

const t = (key, fb) => {
  try { return bridge.t(key, fb) } catch (_e) { return fb || key }
}

async function load() {
  loading.value = true
  try {
    const data = await bridge.apiGet('voices')
    voices.value = data.voices || []
  } catch (e) {
    notify.error(e.message || 'load failed')
  } finally {
    loading.value = false
  }
}

async function toggleHidden(row, v) {
  try {
    await bridge.apiPost('voices/hidden', { name: row.name, hidden: v })
    row.hidden = v
    notify.success(`hidden=${v}`)
  } catch (e) {
    notify.error(e.message || 'request failed')
  }
}

async function setDefault(row) {
  try {
    await bridge.apiPost('voices/default', { name: row.name })
    voices.value.forEach((v) => { v.is_default = v.name === row.name })
    notify.success(`default=${row.name}`)
  } catch (e) {
    notify.error(e.message || 'request failed')
  }
}

function play(row) {
  currentVoice.value = row.name
  previewText.value = '你好，这是试听语音。'
  previewUrl.value = ''
  previewError.value = ''
  previewVisible.value = true
}

async function doPreview() {
  previewLoading.value = true
  previewUrl.value = ''
  previewError.value = ''
  try {
    const body = { text: previewText.value, voice: currentVoice.value }
    // 服务端直链优先：后端返回 {url}（/audio/... 直连，浏览器自动缓存）
    const result = await bridge.apiPost('synthesize', body)
    if (result && result.url) {
      previewUrl.value = result.url
    } else {
      // 回退：download 触发浏览器下载
      previewError.value = t('voices.downloadMode', '当前环境会下载试听音频，请播放下载文件')
    }
  } catch (e) {
    previewError.value = e.message || 'synthesize failed'
  } finally {
    previewLoading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.preview-audio { margin-top: 12px; }
</style>