<template>
  <div class="cv-page">
    <div class="cv-card">
      <div class="cv-toolbar">
        <div class="cv-section-title" style="margin: 0">
          <el-icon><ChatRound /></el-icon>会话列表
        </div>
        <span class="cv-muted">自动 TTS 按会话累积文本，到点/关键词触发合成</span>
        <div class="cv-spacer" />
        <el-button :icon="Delete" type="danger" plain @click="clearAll">清空全部</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-table :data="sessions" v-loading="loading" stripe style="width: 100%">
        <el-table-column prop="id" label="会话 ID" min-width="170" show-overflow-tooltip />
        <el-table-column prop="user" label="用户" min-width="160" show-overflow-tooltip />
        <el-table-column prop="mode" label="发送方式" min-width="110" />
        <el-table-column label="语音开关" min-width="90">
          <template #default="{ row }">
            <el-tag :type="row.on ? 'success' : 'info'" size="small" effect="light">
              {{ row.on ? '已开启' : '未开启' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="voice" label="音色" min-width="120" show-overflow-tooltip />
        <el-table-column label="概率" min-width="80">
          <template #default="{ row }">{{ row.prob == null ? '—' : row.prob }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="110" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :icon="Delete" type="danger" plain @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !sessions.length" description="暂无活跃会话" :image-size="70" />
    </div>
  </div>
</template>

<script setup>
import { ref, inject, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ChatRound, Delete, Refresh } from '@element-plus/icons-vue'

const bridge = inject('bridge')
const sessions = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const r = await bridge.apiGet('sessions')
    sessions.value = r.sessions || []
  } catch (e) {
    ElMessage.error('加载会话失败：' + e)
  } finally {
    loading.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`删除会话「${row.id}」？`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await bridge.apiPost('sessions/delete', { id: row.id })
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error('删除失败：' + e)
  }
}
async function clearAll() {
  try {
    await ElMessageBox.confirm('清空所有会话？', '清空确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await bridge.apiPost('sessions/clear', {})
    ElMessage.success('已清空')
    await load()
  } catch (e) {
    ElMessage.error('清空失败：' + e)
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.cv-page { display: flex; flex-direction: column; gap: 14px; }
</style>
