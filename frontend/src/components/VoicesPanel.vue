<template>
  <div>
    <div class="toolbar">
      <el-button type="primary" size="small" @click="openCreate">{{ '新增音色' }}</el-button>
      <el-button size="small" @click="load">{{ '刷新' }}</el-button>
    </div>

    <el-table :data="voices" v-loading="loading" border size="small">
      <el-table-column label="音色名" prop="name" min-width="110" />
      <el-table-column label="参考音频" prop="prompt_wav" min-width="150" show-overflow-tooltip />
      <el-table-column label="参考文本" prop="prompt_text" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ row.prompt_text || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="音频可达" width="80" align="center">
        <template #default="{ row }">
          <el-tag :type="row.wav_resolved ? 'success' : 'danger'" size="small">
            {{ row.wav_resolved ? 'OK' : 'MISS' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="80" align="center">
        <template #default="{ row }">
          <el-tag :type="row.in_lib ? 'primary' : 'info'" size="small" effect="plain">
            {{ row.in_lib ? 'WebUI' : '配置' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="隐藏" width="70" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.hidden" size="small" @change="(v) => toggleHidden(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="330" align="center">
        <template #default="{ row }">
          <el-button size="small" type="primary" plain @click="quickListen(row)">试听</el-button>
          <el-button size="small" @click="openEditListen(row)">编辑试听</el-button>
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" @click="setDefault(row)" :disabled="row.is_default">
            {{ row.is_default ? '默认' : '设为默认' }}
          </el-button>
          <el-button size="small" type="danger" plain @click="confirmDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 试听弹窗（默认文本直接合成下载 / 编辑试听自定义文本） -->
    <el-dialog v-model="previewVisible" :title="previewTitle" width="480px">
      <el-input
        v-model="previewText"
        type="textarea"
        :rows="3"
        placeholder="输入要试听的文本"
      />
      <div class="muted" style="margin-top: 8px">合成后将自动下载 wav 到本地。</div>
      <div class="muted" v-if="previewError" style="margin-top: 8px; color: #f56c6c">{{ previewError }}</div>
      <template #footer>
        <el-button @click="previewVisible = false">取消</el-button>
        <el-button type="primary" :loading="previewLoading" @click="doPreview">合成试听并下载</el-button>
      </template>
    </el-dialog>

    <!-- 新增/编辑音色弹窗 -->
    <el-dialog v-model="formVisible" :title="formTitle" width="520px">
      <el-form label-width="90px">
        <el-form-item label="音色名" required>
          <el-input v-model="form.name" :disabled="editing" placeholder="唯一标识" />
        </el-form-item>
        <el-form-item label="参考音频">
          <el-input v-model="form.prompt_wav" placeholder="wav 文件名或路径" />
        </el-form-item>
        <el-form-item label="参考文本">
          <el-input v-model="form.prompt_text" type="textarea" :rows="2" placeholder="音频对应原文（可选）" />
        </el-form-item>
        <el-form-item label="隐藏">
          <el-switch v-model="form.hidden" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="formLoading" @click="saveForm">
          {{ editing ? '保存修改' : '创建' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, inject, onMounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import bridge from '../api'

const notify = inject('notify')
const voices = ref([])
const loading = ref(false)

const previewVisible = ref(false)
const previewTitle = ref('试听')
const previewText = ref('你好，这是试听语音。')
const previewLoading = ref(false)
const previewError = ref('')
const currentVoice = ref('')

const formVisible = ref(false)
const formTitle = ref('新增音色')
const editing = ref(false)
const formLoading = ref(false)
const form = ref({ name: '', prompt_wav: '', prompt_text: '', hidden: false })

async function load() {
  loading.value = true
  try {
    const data = await bridge.apiGet('voices')
    voices.value = data.voices || []
  } catch (e) {
    notify.error(e.message || '加载失败')
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
    notify.error(e.message || '请求失败')
  }
}

async function setDefault(row) {
  try {
    await bridge.apiPost('voices/default', { name: row.name })
    voices.value.forEach((v) => { v.is_default = v.name === row.name })
    notify.success(`已设为默认：${row.name}`)
  } catch (e) {
    notify.error(e.message || '请求失败')
  }
}

// 试听：默认文本直接合成下载
function quickListen(row) {
  currentVoice.value = row.name
  previewText.value = '你好，这是试听语音。'
  previewError.value = ''
  previewTitle.value = `试听 · ${row.name}`
  previewVisible.value = true
}

// 编辑试听：自定义文本
function openEditListen(row) {
  currentVoice.value = row.name
  previewText.value = ''
  previewError.value = ''
  previewTitle.value = `编辑试听 · ${row.name}`
  previewVisible.value = true
}

async function doPreview() {
  if (!previewText.value.trim()) {
    previewError.value = '请输入要试听的文本'
    return
  }
  previewLoading.value = true
  previewError.value = ''
  try {
    const body = { text: previewText.value, voice: currentVoice.value }
    // bridge.download 触发后端返回 wav 并下载到本地
    await bridge.download('synthesize', null, 'cosyvoice_preview.wav')
    notify.success('已合成并开始下载')
    previewError.value = ''
  } catch (e) {
    previewError.value = e.message || '合成失败'
  } finally {
    previewLoading.value = false
  }
}

function openCreate() {
  editing.value = false
  formTitle.value = '新增音色'
  form.value = { name: '', prompt_wav: '', prompt_text: '', hidden: false }
  formVisible.value = true
}

function openEdit(row) {
  editing.value = true
  formTitle.value = `编辑音色 · ${row.name}`
  form.value = {
    name: row.name,
    prompt_wav: row.prompt_wav || '',
    prompt_text: row.prompt_text || '',
    hidden: row.hidden,
  }
  formVisible.value = true
}

async function saveForm() {
  if (!form.value.name.trim()) {
    notify.error('音色名不能为空')
    return
  }
  formLoading.value = true
  try {
    const body = { ...form.value }
    if (editing.value) {
      await bridge.apiPost('voices/update', body)
      notify.success('已保存修改')
    } else {
      await bridge.apiPost('voices/create', body)
      notify.success('已创建音色')
    }
    formVisible.value = false
    load()
  } catch (e) {
    notify.error(e.message || '保存失败')
  } finally {
    formLoading.value = false
  }
}

function confirmDelete(row) {
  ElMessageBox.confirm(
    `确定删除音色「${row.name}」吗？此操作不可恢复。`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  )
    .then(async () => {
      try {
        await bridge.apiPost('voices/delete', { name: row.name })
        notify.success(`已删除：${row.name}`)
        load()
      } catch (e) {
        notify.error(e.message || '删除失败')
      }
    })
    .catch(() => {})
}

onMounted(load)
</script>