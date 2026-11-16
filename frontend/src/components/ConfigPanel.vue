<template>
  <div class="cv-page">
    <div class="cv-card">
      <div class="cv-section-title">
        <el-icon><Setting /></el-icon>插件配置
        <span class="cv-muted" style="font-weight: 400">
          修改后点各分区右上角「保存」，立即热生效并落盘（与 AstrBot「插件配置」弹窗同一份配置）
        </span>
        <span class="cv-spacer" />
        <span class="cv-chip">默认音色（生效）<b>{{ effectiveDefault || '-' }}</b></span>
      </div>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="音色（voices）不在此编辑——请到「音色管理」页维护；服务端列表在本页表格中编辑。"
        style="margin-bottom: 14px"
      />

      <div v-loading="loading">
        <el-tabs v-model="activeGroup" type="border-card">
          <el-tab-pane v-for="(keys, gname) in groups" :key="gname" :label="gname" :name="gname" lazy>
            <div class="cfg-pane">
              <div class="cfg-pane-bar">
                <el-button size="small" type="primary" :loading="saving" :disabled="!groupDirty(gname)" @click="saveGroup(gname)">
                  保存{{ groupDirtyCount(gname) ? `（${groupDirtyCount(gname)} 项改动）` : '' }}
                </el-button>
                <el-button size="small" :disabled="!groupDirty(gname)" @click="resetGroup(gname)">还原</el-button>
                <span v-if="groupDirty(gname)" class="cv-muted">有未保存改动（切换页签会丢失）</span>
              </div>

              <div v-for="k in keys" :key="k" class="cfg-row">
                <div class="cfg-row-head">
                  <div class="cfg-row-title">
                    {{ schemaTitle(k) }}
                    <el-tooltip v-if="schemaHint(k)" :content="schemaHint(k)" placement="top" :show-after="200">
                      <el-icon class="cfg-help"><QuestionFilled /></el-icon>
                    </el-tooltip>
                  </div>
                  <div class="cfg-row-desc">{{ schemaDesc(k) || k }}</div>
                </div>
                <div class="cfg-row-ctrl">
                  <!-- 服务端列表：表格编辑器 -->
                  <template v-if="k === 'servers'">
                    <el-table :data="draft.servers || []" size="small" border class="cfg-servers">
                      <el-table-column label="服务地址" min-width="220">
                        <template #default="{ row }">
                          <el-input v-model="row.url" placeholder="http://192.168.1.10:50002" size="small" />
                        </template>
                      </el-table-column>
                      <el-table-column label="启用" width="64" align="center">
                        <template #default="{ row }">
                          <el-switch v-model="row.enabled" size="small" />
                        </template>
                      </el-table-column>
                      <el-table-column label="默认" width="64" align="center">
                        <template #default="{ row, $index }">
                          <el-checkbox :model-value="row.default" size="small" @change="setDefault($index)" />
                        </template>
                      </el-table-column>
                      <el-table-column label="权重" width="110" align="center">
                        <template #default="{ row }">
                          <el-input-number v-model="row.weight" size="small" :min="0" :step="1" controls-position="right" style="width: 90px" />
                        </template>
                      </el-table-column>
                      <el-table-column label="操作" width="64" align="center">
                        <template #default="{ $index }">
                          <el-button size="small" type="danger" link @click="draft.servers.splice($index, 1)">删除</el-button>
                        </template>
                      </el-table-column>
                    </el-table>
                    <el-button size="small" class="cfg-add-server" @click="addServer">+ 添加服务端</el-button>
                  </template>

                  <!-- 其余类型 -->
                  <template v-else>
                    <el-switch v-if="ctrlType(k) === 'bool'" v-model="draft[k]" />
                    <el-input-number v-else-if="ctrlType(k) === 'int'" v-model="draft[k]" :step="1" controls-position="right" class="cfg-num" />
                    <el-input-number v-else-if="ctrlType(k) === 'float'" v-model="draft[k]" :step="0.1" controls-position="right" class="cfg-num" />
                    <el-select v-else-if="ctrlType(k) === 'option'" v-model="draft[k]" class="cfg-input">
                      <el-option v-for="o in schemaOptions(k)" :key="o" :label="o" :value="o" />
                    </el-select>
                    <el-select
                      v-else-if="ctrlType(k) === 'list'"
                      v-model="draft[k]"
                      class="cfg-input cfg-list"
                      multiple filterable allow-create default-first-option
                      placeholder="输入后回车添加，可多个"
                    >
                      <el-option v-for="o in draft[k] || []" :key="o" :label="o" :value="o" />
                    </el-select>
                    <el-input v-else-if="draft[k] !== undefined && draft[k] !== null && typeof draft[k] === 'object'"
                      :model-value="JSON.stringify(draft[k])" readonly type="textarea" :rows="2" />
                    <el-input v-else v-model="draft[k]" class="cfg-input" :placeholder="schemaDefault(k) !== undefined ? `默认：${schemaDefault(k)}` : ''" clearable />
                  </template>
                </div>
              </div>

              <!-- 分段试算：仅「分段」分区展示 -->
              <div v-if="gname === '分段'" class="cfg-preview">
                <div class="cv-section-title" style="margin-top: 4px">
                  分段试算
                  <span class="cv-muted" style="font-weight: 400">粘贴一段回复文本，按当前配置预览切分结果（纯切分，不请求合成服务端）</span>
                </div>
                <el-input
                  v-model="previewText"
                  type="textarea" :rows="4" maxlength="5000" show-word-limit
                  placeholder="粘贴一段或多行文本，例如：&#10;好呀！今天天气真不错我们一起出去玩吧。&#10;嗯"
                />
                <div style="margin-top: 8px">
                  <el-button size="small" type="primary" :loading="previewLoading" @click="runPreview">试算</el-button>
                </div>
                <template v-if="previewRes">
                  <div class="cfg-preview-chips">
                    <span class="cv-chip ok">语音段 <b>{{ (previewRes.voice_segs || []).length }}</b></span>
                    <span class="cv-chip">文字段 <b>{{ previewTextCount }}</b></span>
                    <span v-if="previewTextCount !== (previewRes.voice_segs || []).length" class="cv-chip warn">
                      文字与语音段数不同（短行文字按原始换行发送）
                    </span>
                  </div>
                  <div class="cfg-preview-list">
                    <div v-for="(seg, i) in previewRes.pairs || []" :key="i" class="cfg-preview-item">
                      <div class="cfg-preview-voice">
                        <span class="cfg-seg-idx">{{ i + 1 }}</span>
                        <span class="cfg-seg-len">{{ seg.len }} 字</span>
                        <span class="cfg-seg-text">{{ seg.voice || '（空段）' }}</span>
                      </div>
                      <div v-if="(seg.texts || []).length > 1" class="cfg-preview-texts">
                        <div v-for="(t, j) in seg.texts" :key="j" class="cv-muted">
                          文字{{ j + 1 }}（{{ t.len }} 字）：{{ t.text }}
                        </div>
                      </div>
                    </div>
                    <div v-if="!(previewRes.pairs || []).length" class="cv-muted">（无有效分段）</div>
                  </div>
                </template>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
        <el-empty v-if="!loading && !Object.keys(groups).length" description="配置加载失败，请刷新重试" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Setting, QuestionFilled } from '@element-plus/icons-vue'
import bridge from '../api'

const loading = ref(false)
const saving = ref(false)
const groups = ref({})
const schema = ref({})
const origin = ref({})
const draft = ref({})
const activeGroup = ref('')

const effectiveDefault = ref('')

// 分段试算
const previewText = ref('')
const previewLoading = ref(false)
const previewRes = ref(null)

const previewTextCount = computed(() =>
  (previewRes.value?.pairs || []).reduce((n, p) => n + (p.texts || []).length, 0),
)

function schemaTitle(k) {
  const s = schema.value[k]
  return (s && s.title) || k
}
function schemaDesc(k) {
  const s = schema.value[k]
  return (s && s.description) || ''
}
function schemaHint(k) {
  const s = schema.value[k]
  return (s && s.hint) || ''
}
function schemaDefault(k) {
  const s = schema.value[k]
  return s ? s.default : undefined
}
function schemaOptions(k) {
  const s = schema.value[k]
  return (s && s.options) || []
}
function ctrlType(k) {
  const s = schema.value[k]
  if (!s) return 'json'
  const t = String(s.type || 'string').toLowerCase()
  if (t === 'string' && (s.options || []).length) return 'option'
  if (t === 'list') return 'list'
  if (['bool', 'int', 'float'].includes(t)) return t
  if (t === 'template_list') return 'json'
  return 'string'
}

function groupDirty(gname) {
  return groupDirtyCount(gname) > 0
}
function groupDirtyCount(gname) {
  const keys = groups.value[gname] || []
  let n = 0
  for (const k of keys) {
    if (JSON.stringify(draft.value[k]) !== JSON.stringify(origin.value[k])) n += 1
  }
  return n
}

function setDefault(idx) {
  const list = draft.value.servers || []
  const willOn = !list[idx]?.default
  list.forEach((s, j) => {
    s.default = willOn && j === idx
  })
}
function addServer() {
  if (!Array.isArray(draft.value.servers)) draft.value.servers = []
  draft.value.servers.push({ url: '', enabled: true, default: false, weight: 1 })
}

async function load() {
  loading.value = true
  try {
    const data = await bridge.apiGet('config')
    schema.value = data.schema || {}
    origin.value = data.values || {}
    draft.value = JSON.parse(JSON.stringify(origin.value))
    groups.value = data.groups || {}
    effectiveDefault.value = data.effective_default_voice || ''
    if (!activeGroup.value || !groups.value[activeGroup.value]) {
      const first = Object.keys(groups.value)[0]
      activeGroup.value = first || ''
    }
  } catch (e) {
    groups.value = {}
    ElMessage.error(`加载配置失败：${e?.message || e}`)
  } finally {
    loading.value = false
  }
}

function resetGroup(gname) {
  for (const k of groups.value[gname] || []) {
    draft.value[k] = JSON.parse(JSON.stringify(origin.value[k]))
  }
}

async function saveGroup(gname) {
  const keys = groups.value[gname] || []
  const changed = {}
  for (const k of keys) {
    if (JSON.stringify(draft.value[k]) !== JSON.stringify(origin.value[k])) {
      changed[k] = draft.value[k]
    }
  }
  if (!Object.keys(changed).length) {
    ElMessage.info('没有改动')
    return
  }
  saving.value = true
  try {
    const res = await bridge.apiPost('config', changed)
    const values = res?.values || {}
    for (const k of Object.keys(changed)) {
      origin.value[k] = values[k] !== undefined ? values[k] : JSON.parse(JSON.stringify(draft.value[k]))
    }
    if (changed.default_voice !== undefined && values.default_voice !== undefined) {
      effectiveDefault.value = values.default_voice
    }
    ElMessage.success(`已保存 ${Object.keys(changed).length} 项，立即生效`)
  } catch (e) {
    ElMessage.error(`保存失败：${e?.message || e}`)
  } finally {
    saving.value = false
  }
}

async function runPreview() {
  const text = previewText.value.trim()
  if (!text) {
    ElMessage.warning('请先粘贴要试算的文本')
    return
  }
  previewLoading.value = true
  try {
    previewRes.value = await bridge.apiPost('segment_preview', { text })
  } catch (e) {
    ElMessage.error(`试算失败：${e?.message || e}`)
  } finally {
    previewLoading.value = false
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.cfg-pane { display: flex; flex-direction: column; gap: 4px; }
.cfg-pane-bar {
  display: flex; align-items: center; gap: 10px;
  padding: 2px 0 10px;
  position: sticky; top: 0;
  background: var(--cv-panel);
  z-index: 2;
}
.cfg-row {
  display: grid;
  grid-template-columns: minmax(220px, 300px) 1fr;
  gap: 14px;
  padding: 12px 4px;
  border-top: 1px solid var(--cv-border);
}
.cfg-row-title { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; }
.cfg-help { color: var(--cv-text-2); cursor: help; font-size: 14px; }
.cfg-row-desc { font-size: 12px; color: var(--cv-text-2); margin-top: 3px; line-height: 1.5; }
.cfg-row-ctrl { min-width: 0; }
.cfg-input { max-width: 460px; width: 100%; }
.cfg-num { width: 160px; }
.cfg-list { max-width: 560px; }
.cfg-servers { max-width: 720px; }
.cfg-add-server { margin-top: 8px; }

.cfg-preview {
  margin-top: 18px;
  border-top: 1px dashed var(--cv-border);
  padding-top: 12px;
}
.cfg-preview-chips { display: flex; gap: 8px; margin: 10px 0 8px; flex-wrap: wrap; }
.cfg-preview-list { display: flex; flex-direction: column; gap: 8px; }
.cfg-preview-item {
  background: var(--cv-panel-2);
  border: 1px solid var(--cv-border);
  border-radius: var(--cv-radius-sm);
  padding: 8px 10px;
}
.cfg-preview-voice { display: flex; align-items: baseline; gap: 8px; }
.cfg-seg-idx {
  flex: none; width: 20px; height: 20px; border-radius: 6px;
  display: inline-grid; place-items: center;
  background: var(--cv-primary-soft); color: var(--cv-primary);
  font-size: 12px; font-weight: 700;
}
.cfg-seg-len { flex: none; font-size: 12px; color: var(--cv-text-2); }
.cfg-seg-text { font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; }
.cfg-preview-texts {
  margin: 6px 0 0 28px;
  padding-left: 8px;
  border-left: 2px solid var(--cv-border);
  display: flex; flex-direction: column; gap: 2px;
}

@media (max-width: 720px) {
  .cfg-row { grid-template-columns: 1fr; gap: 6px; }
}
</style>
