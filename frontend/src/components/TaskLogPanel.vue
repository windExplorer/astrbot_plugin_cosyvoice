<template>
  <div class="cv-page">
    <div class="cv-card">
      <div class="cv-toolbar">
        <div class="cv-section-title" style="margin: 0">
          <el-icon><Document /></el-icon>任务日志
        </div>
        <span class="cv-muted">
          每次语音合成/失败都记录在这里（最近 200 条，重启不丢）；概览页的时间线是本列表最近 20 条
        </span>
        <div class="cv-spacer" />
        <el-input
          v-model="search"
          placeholder="搜索会话 / 音色 / 说明"
          clearable
          :prefix-icon="Search"
          style="width: 220px"
        />
        <el-radio-group v-model="filter" size="small">
          <el-radio-button value="all">全部 {{ stats.total }}</el-radio-button>
          <el-radio-button value="ok">成功 {{ stats.ok }}</el-radio-button>
          <el-radio-button value="fail">失败 {{ stats.failed }}</el-radio-button>
        </el-radio-group>
        <el-button :icon="Refresh" circle @click="load" />
      </div>

      <el-table :data="filtered" size="small" stripe v-loading="loading" empty-text="暂无任务记录">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">
            <span class="cv-muted">{{ row.time || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="70" align="center">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small" effect="light">
              {{ row.ok ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="会话" min-width="150">
          <template #default="{ row }">
            <el-tooltip :content="row.origin" placement="top" :show-after="300" :disabled="!row.origin">
              <span class="cv-ellipsis">{{ originLabel(row.origin) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="音色" width="110">
          <template #default="{ row }">{{ row.voice || '—' }}</template>
        </el-table-column>
        <el-table-column label="字数" width="70" align="right">
          <template #default="{ row }">{{ row.chars ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="90" align="right">
          <template #default="{ row }">{{ fmtDuration(row.duration_ms) }}</template>
        </el-table-column>
        <el-table-column label="说明" min-width="220">
          <template #default="{ row }">
            <span :class="row.ok ? '' : 'cv-err'">{{ row.msg }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Document, Search, Refresh } from '@element-plus/icons-vue'

const bridge = inject('bridge')

const events = ref([])
const stats = ref({ total: 0, ok: 0, failed: 0 })
const loading = ref(false)
const filter = ref('all')
const search = ref('')

const filtered = computed(() => {
  let list = events.value
  if (filter.value === 'ok') list = list.filter((e) => e.ok)
  else if (filter.value === 'fail') list = list.filter((e) => !e.ok)
  const q = search.value.trim().toLowerCase()
  if (q) {
    list = list.filter(
      (e) =>
        (e.origin || '').toLowerCase().includes(q)
        || (e.voice || '').toLowerCase().includes(q)
        || (e.msg || '').toLowerCase().includes(q),
    )
  }
  return list
})

// unified_msg_origin 太长，表格里只显示会话号（完整 origin 放悬浮提示）
function originLabel(origin) {
  const o = String(origin || '')
  if (!o) return '—'
  const parts = o.split(':')
  return parts.length >= 3 ? parts[parts.length - 1] : o
}

function fmtDuration(ms) {
  if (ms == null || ms === '') return '—'
  const n = Number(ms)
  if (!Number.isFinite(n)) return '—'
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`
}

async function load() {
  loading.value = true
  try {
    const data = await bridge.apiGet('tasklog')
    events.value = data.events || []
    stats.value = data.stats || { total: events.value.length, ok: 0, failed: 0 }
  } catch (e) {
    ElMessage.error('加载任务日志失败：' + (e?.message || e))
  } finally {
    loading.value = false
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.cv-err { color: var(--cv-danger); }
.cv-ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; max-width: 140px; vertical-align: bottom; }
</style>
