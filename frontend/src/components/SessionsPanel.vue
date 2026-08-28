<template>
  <div class="cv-page">
    <div class="cv-card">
      <div class="cv-toolbar">
        <div class="cv-section-title" style="margin: 0">
          <el-icon><ChatRound /></el-icon>会话列表
        </div>
        <span class="cv-muted">按会话配置音色与发送方式；昵称取自聊天记录（无则显示群号/QQ号）</span>
        <div class="cv-spacer" />
        <el-button :icon="Delete" type="danger" plain @click="clearAll">清空全部</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-table :data="sessions" v-loading="loading" stripe style="width: 100%">
        <el-table-column label="会话" min-width="200">
          <template #default="{ row }">
            <div class="cv-sess">
              <div class="cv-sess-main">{{ row.nickname || row.label }}</div>
              <div class="cv-sess-sub">{{ sessSub(row) }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="nickname" label="昵称" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.nickname || '—' }}</template>
        </el-table-column>
        <el-table-column prop="mode" label="发送方式" min-width="120" />
        <el-table-column label="语音开关" min-width="100">
          <template #default="{ row }">
            <el-switch
              v-model="row.on"
              inline-prompt
              active-text="开"
              inactive-text="关"
              @change="(val) => toggleOn(row, val)"
            />
          </template>
        </el-table-column>
        <el-table-column prop="voice" label="音色" min-width="120" show-overflow-tooltip />
        <el-table-column label="概率" min-width="80">
          <template #default="{ row }">{{ row.prob == null ? '—' : row.prob }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" :icon="Delete" type="danger" plain @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !sessions.length" description="暂无活跃会话" :image-size="70" />
    </div>

    <el-dialog v-model="editVisible" title="会话配置" width="440px">
      <el-form label-width="92px">
        <el-form-item label="语音开关">
          <el-switch v-model="editForm.on" inline-prompt active-text="开" inactive-text="关" />
        </el-form-item>
        <el-form-item label="发送方式">
          <el-select v-model="editForm.send_mode" style="width: 100%">
            <el-option label="默认(跟随全局)" value="" />
            <el-option label="语音+文字" value="both" />
            <el-option label="仅语音" value="voice_only" />
          </el-select>
        </el-form-item>
        <el-form-item label="音色">
          <el-select v-model="editForm.voice" filterable allow-create style="width: 100%">
            <el-option label="默认" value="" />
            <el-option v-for="v in voices" :key="v" :label="v" :value="v" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, inject, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ChatRound, Delete, Refresh, Edit } from '@element-plus/icons-vue'

const bridge = inject('bridge')
const sessions = ref([])
const loading = ref(false)
const voices = ref([])
const editVisible = ref(false)
const saving = ref(false)
const editForm = ref({ id: '', on: true, voice: '', send_mode: '' })

async function load() {
  loading.value = true
  try {
    const [s, v] = await Promise.all([
      bridge.apiGet('sessions'),
      bridge.apiGet('voices').catch(() => ({ voices: [] })),
    ])
    sessions.value = s.sessions || []
    voices.value = (v.voices || []).map((x) => x.name).filter(Boolean)
  } catch (e) {
    ElMessage.error('加载会话失败：' + e)
  } finally {
    loading.value = false
  }
}

// 副行：展示群号/QQ号（昵称存在时作为补充）；都没有则回退原始会话 ID 便于排查
function sessSub(row) {
  const parts = []
  if (row.group_id) parts.push('群 ' + row.group_id)
  if (row.user_id) parts.push('用户 ' + row.user_id)
  return parts.length ? parts.join(' · ') : row.id
}

async function toggleOn(row, val) {
  try {
    await bridge.apiPost('sessions/set', { origin: row.id, on: val })
    ElMessage.success(val ? '已开启语音' : '已关闭语音')
  } catch (e) {
    ElMessage.error('操作失败：' + e)
    await load()
  }
}

function rowSendMode(row) {
  if (row.mode === '语音+文字') return 'both'
  if (row.mode === '仅语音') return 'voice_only'
  return ''
}

function openEdit(row) {
  editForm.value = {
    id: row.id,
    on: row.on,
    voice: row.voice === '默认' ? '' : row.voice,
    send_mode: rowSendMode(row),
  }
  editVisible.value = true
}

async function saveEdit() {
  saving.value = true
  try {
    await bridge.apiPost('sessions/set', {
      origin: editForm.value.id,
      on: editForm.value.on,
      voice: editForm.value.voice || '',
      send_mode: editForm.value.send_mode || '',
    })
    ElMessage.success('已保存')
    editVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error('保存失败：' + e)
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`删除会话「${row.nickname || row.label}」？`, '删除确认', { type: 'warning' })
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
.cv-sess { display: flex; flex-direction: column; line-height: 1.3; }
.cv-sess-main { font-weight: 600; }
.cv-sess-sub { font-size: 12px; color: var(--el-text-color-secondary); }
</style>
