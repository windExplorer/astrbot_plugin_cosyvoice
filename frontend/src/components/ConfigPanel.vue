<template>
  <div class="cv-page">
    <div class="cv-card">
      <div class="cv-section-title">
        <el-icon><Setting /></el-icon>当前生效配置（只读）
      </div>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="以下为当前生效配置的只读展示。修改请在 AstrBot「插件配置」中操作；本插件的「音色」与「翻译」设置独立保存在插件数据中，不在此列。"
        style="margin-bottom: 14px"
      />
      <div v-loading="loading">
        <el-tabs v-model="activeGroup" type="border-card">
          <el-tab-pane v-for="(items, groupName) in groups" :key="groupName" :label="groupName" :name="groupName">
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item v-for="(val, key) in items" :key="key" :label="key" min-width="280">
                <span class="cfg-value">{{ renderVal(val) }}</span>
              </el-descriptions-item>
            </el-descriptions>
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { Setting } from '@element-plus/icons-vue'
import bridge from '../api'

const groups = ref({})
const activeGroup = ref('')
const loading = ref(false)

const groupNames = computed(() => Object.keys(groups.value))

function renderVal(val) {
  if (typeof val === 'boolean') return val ? 'ON' : 'OFF'
  if (val === '' || val === null || val === undefined) return '-'
  if (Array.isArray(val)) return val.length ? val.join(', ') : '-'
  return String(val)
}

async function load() {
  loading.value = true
  try {
    const data = await bridge.apiGet('config')
    groups.value = data.groups || {}
    if (!activeGroup.value && groupNames.value.length) {
      activeGroup.value = groupNames.value[0]
    }
  } catch (e) {
    groups.value = {}
  } finally {
    loading.value = false
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.cv-page { display: flex; flex-direction: column; gap: 14px; }
.cfg-value {
  word-break: break-all;
  white-space: pre-wrap;
  font-size: 13px;
}
</style>
