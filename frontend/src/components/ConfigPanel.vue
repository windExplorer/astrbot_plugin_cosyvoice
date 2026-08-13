<template>
  <div>
    <el-alert
      title="以下为当前生效配置的只读展示。修改请在 AstrBot「插件配置」弹窗中操作（WebUI 音色与会话设置不受影响）。"
      type="info"
      :closable="false"
      style="margin-bottom: 12px"
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
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
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
    // 静默失败（配置端点低版本可能缺失）
    groups.value = {}
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.cfg-value {
  word-break: break-all;
  white-space: pre-wrap;
  font-size: 13px;
}
</style>