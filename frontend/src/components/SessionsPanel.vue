<template>
  <div>
    <div class="toolbar">
      <el-input
        v-model="keyword"
        :placeholder="t('sessions.search', '搜索会话（origin）')"
        clearable
        style="width: 280px"
      />
      <el-button :disabled="!filteredSessions.length" size="small" type="danger" plain @click="batchOff">
        {{ t('sessions.batchOff', '批量关闭当前列表') }}
      </el-button>
      <el-button size="small" @click="load">{{ t('common.refresh', '刷新') }}</el-button>
    </div>

    <el-table :data="filteredSessions" v-loading="loading" border size="small">
      <el-table-column :label="t('sessions.origin', '会话')" prop="origin" min-width="200" show-overflow-tooltip />
      <el-table-column :label="t('sessions.on', '语音开关')" width="110" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.on" @change="(v) => setOn(row, v)" />
        </template>
      </el-table-column>
      <el-table-column :label="t('sessions.voice', '音色')" width="150">
        <template #default="{ row }">
          <el-select :model-value="row.voice || ''" placeholder="-" size="small" @change="(v) => setVoice(row, v)">
            <el-option label="-" value="" />
            <el-option v-for="v in allVoices" :key="v" :label="v" :value="v" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column :label="t('sessions.sendMode', '发送方式')" width="140">
        <template #default="{ row }">
          <el-select :model-value="row.send_mode || ''" placeholder="跟随全局" size="small" @change="(v) => setSendMode(row, v)">
            <el-option :label="t('sessions.followGlobal', '跟随全局')" value="" />
            <el-option label="both" value="both" />
            <el-option label="voice_only" value="voice_only" />
          </el-select>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, inject, computed, onMounted } from 'vue'
import bridge from '../api'

const ctx = inject('bridgeCtx')
const notify = inject('notify')
const sessions = ref([])
const allVoices = ref([])
const loading = ref(false)
const keyword = ref('')

const t = (key, fb) => {
  try { return bridge.t(key, fb) } catch (_e) { return fb || key }
}

const filteredSessions = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return sessions.value
  return sessions.value.filter((s) => s.origin.toLowerCase().includes(kw))
})

async function load() {
  loading.value = true
  try {
    const [s, v] = await Promise.all([
      bridge.apiGet('sessions'),
      bridge.apiGet('voices'),
    ])
    sessions.value = s.sessions || []
    allVoices.value = (v.voices || []).map((x) => x.name)
  } catch (e) {
    notify.error(e.message || 'load failed')
  } finally {
    loading.value = false
  }
}

async function setOn(row, v) {
  try {
    await bridge.apiPost('sessions/set', { origin: row.origin, on: v })
    row.on = v
    notify.success(`on=${v}`)
  } catch (e) {
    notify.error(e.message || 'request failed')
  }
}

async function setVoice(row, v) {
  try {
    await bridge.apiPost('sessions/set', { origin: row.origin, voice: v })
    row.voice = v
    notify.success(`voice=${v}`)
  } catch (e) {
    notify.error(e.message || 'request failed')
  }
}

async function setSendMode(row, v) {
  try {
    await bridge.apiPost('sessions/set', { origin: row.origin, send_mode: v })
    row.send_mode = v
    notify.success(`send_mode=${v}`)
  } catch (e) {
    notify.error(e.message || 'request failed')
  }
}

async function batchOff() {
  const origins = filteredSessions.value.filter((s) => s.on).map((s) => s.origin)
  if (!origins.length) {
    notify.info(t('sessions.noneOn', '列表中没有开启语音的会话'))
    return
  }
  try {
    await bridge.apiPost('sessions/batch_off', { origins })
    load()
    notify.success(t('sessions.batchDone', `已关闭 ${origins.length} 个会话`))
  } catch (e) {
    notify.error(e.message || 'batch off failed')
  }
}

onMounted(load)
</script>