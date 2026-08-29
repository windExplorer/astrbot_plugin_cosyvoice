<template>
  <div class="cv-page">
    <!-- 顶部工具条 -->
    <div class="cv-card cv-toolbar">
      <el-input
        v-model="search"
        placeholder="搜索音色名 / 参考文本"
        clearable
        :prefix-icon="Search"
        style="width: 230px"
      />
      <el-select
        v-model="filterLang"
        placeholder="按语种筛选"
        clearable
        style="width: 160px"
      >
        <el-option label="全部语种" value="__all" />
        <el-option v-for="l in langOptions" :key="l" :label="langLabel(l)" :value="l" />
      </el-select>
      <el-badge :value="stats.total" type="info" :show-zero="false">
        <el-button :type="visibleFilter === 'all' ? 'primary' : 'plain'" @click="visibleFilter = 'all'">音色总数</el-button>
      </el-badge>
      <el-badge :value="stats.visible" type="success" :show-zero="false">
        <el-button :type="visibleFilter === 'visible' ? 'primary' : 'plain'" @click="visibleFilter = 'visible'">可见</el-button>
      </el-badge>
      <el-badge :value="stats.hidden" type="warning" :show-zero="false">
        <el-button :type="visibleFilter === 'hidden' ? 'primary' : 'plain'" @click="visibleFilter = 'hidden'">已隐藏</el-button>
      </el-badge>
      <div class="cv-spacer" />
      <el-button type="primary" :icon="Plus" @click="openCreate">添加音色</el-button>
    </div>

    <!-- 试听区 -->
    <div class="cv-card cv-audition">
      <div class="cv-audition-head">
        <el-icon class="cv-audition-ico"><Headset /></el-icon>
        <span class="cv-section-title" style="margin: 0">试听</span>
        <span class="cv-muted">输入文本后点任意音色的「试听」即可播放（开启翻译时非目标语种会先翻译）</span>
      </div>
      <el-input
        v-model="auditionText"
        type="textarea"
        :rows="2"
        placeholder="要试听的文本，可包含任意语言……"
        style="margin: 10px 0"
      />
      <div class="cv-audition-bar">
        <el-button type="primary" :icon="VideoPlay" :disabled="!auditionText.trim()" @click="auditionDefault">试听（默认音色）</el-button>
        <audio ref="audioRef" :src="audioSrc" controls class="cv-audio" @ended="onEnded" @play="onPlay" />
        <span v-if="playing" class="cv-muted">正在试听：{{ playing }}</span>
      </div>
    </div>

    <!-- 批量操作条 -->
    <transition name="cv-slide">
      <div v-if="selected.length" class="cv-batchbar">
        <span>已选 <b>{{ selected.length }}</b> 个</span>
        <div class="cv-spacer" />
        <el-button size="small" @click="batchHide(false)">批量显示</el-button>
        <el-button size="small" @click="batchHide(true)">批量隐藏</el-button>
        <el-button size="small" type="danger" plain @click="batchDelete">批量删除</el-button>
        <el-button size="small" text @click="selected = []">取消</el-button>
      </div>
    </transition>

    <!-- 分组列表 -->
    <div v-if="loading" v-loading="true" class="cv-card" style="min-height: 160px" />
    <template v-else-if="grouped.length">
      <section v-for="g in grouped" :key="g.lang" class="cv-langgroup">
        <div class="cv-langgroup-head">
          <el-tag :type="g.lang === '未分类' ? 'info' : 'primary'" effect="light" round>
            {{ langLabel(g.lang) }}
          </el-tag>
          <span class="cv-muted">{{ g.items.length }} 个</span>
        </div>
        <div class="cv-voice-grid">
          <div
            v-for="v in g.items"
            :key="v.name"
            class="cv-voice-card"
            :class="{ 'is-hidden': v.hidden }"
          >
            <div class="cv-voice-top">
              <el-checkbox :value="v.name" v-model="selected" />
              <div class="cv-voice-name">
                {{ v.name }}
                <el-tooltip v-if="v.is_default" content="默认音色" placement="top">
                  <el-icon class="cv-star"><StarFilled /></el-icon>
                </el-tooltip>
                <el-tag v-if="v.hidden" size="small" type="warning" effect="plain">隐藏</el-tag>
                <el-tag v-if="v.markup === false" size="small" type="info" effect="plain">无标记</el-tag>
              </div>
            </div>
            <div class="cv-voice-meta cv-muted">
              参考：{{ (v.prompt_wav || '').split('\n')[0] || '—' }}
            </div>
            <div class="cv-voice-text cv-muted">
              {{ v.prompt_text || '（无参考文本）' }}
            </div>
            <div class="cv-voice-actions">
              <el-button size="small" :icon="VideoPlay" @click="audition(v)">试听</el-button>
              <el-button size="small" :icon="Star" @click="setDefault(v)" :disabled="v.is_default">设为默认</el-button>
              <el-button size="small" :icon="Edit" @click="openEdit(v)">编辑</el-button>
              <el-button size="small" type="danger" plain :icon="Delete" @click="remove(v)" />
            </div>
          </div>
        </div>
      </section>
    </template>
    <el-empty v-else description="还没有音色，点右上角「添加音色」" />

    <!-- 编辑 / 添加对话框 -->
    <el-dialog
      v-model="editVisible"
      :title="isEdit ? '编辑音色：' + editForm.name : '添加音色'"
      width="560px"
      append-to-body
    >
      <el-form label-width="92px" label-position="right">
        <el-form-item label="音色名">
          <el-input v-model="editForm.name" :disabled="isEdit" placeholder="唯一标识，如 my_voice" />
        </el-form-item>
        <el-form-item label="语种">
          <el-select
            v-model="editForm.language"
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入语种代码"
            style="width: 100%"
          >
            <el-option v-for="l in LANG_OPTIONS" :key="l" :label="langLabel(l)" :value="l" />
          </el-select>
        </el-form-item>
        <el-form-item label="参考音频">
          <el-input
            v-model="editForm.prompt_wav"
            type="textarea"
            :rows="3"
            placeholder="音色音频相对路径（多文件每行一个）"
          />
        </el-form-item>
        <el-form-item label="参考文本">
          <el-input
            v-model="editForm.prompt_text"
            type="textarea"
            :rows="3"
            placeholder="与参考音频对应的文本"
          />
        </el-form-item>
        <el-form-item label="隐藏">
          <el-switch v-model="editForm.hidden" />
          <span class="cv-muted" style="margin-left: 10px">隐藏后不出现在 /tts_voice 列表，但可手动指定</span>
        </el-form-item>
        <el-form-item label="副语言标记">
          <el-switch v-model="editForm.markup" />
          <span class="cv-muted" style="margin-left: 10px">关闭后该音色不注入 [breath]/[laughter] 等标记，只念纯文本</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, inject, nextTick, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Search, Plus, Edit, Delete, VideoPlay, StarFilled, Star, Headset,
} from '@element-plus/icons-vue'

const bridge = inject('bridge')
const bridgeCtx = inject('bridgeCtx')

const LANG_OPTIONS = [
  'zh', 'en', 'ja', 'ko', 'ru', 'th', 'ar', 'hi',
  'fr', 'de', 'es', 'pt', 'it', 'vi', 'id', 'ms', 'tr', 'fa', 'pl',
]

const voices = ref([])
const loading = ref(false)
const search = ref('')
const filterLang = ref('__all')
const visibleFilter = ref('all')
const selected = ref([])

const audioRef = ref(null)
const audioSrc = ref('')
const playing = ref('')
const auditionText = ref('你好，这是一段音色试听。')

const editVisible = ref(false)
const isEdit = ref(false)
const editForm = reactive({ name: '', prompt_wav: '', prompt_text: '', language: '', hidden: false, markup: true })

const isDark = computed(() => bridgeCtx && bridgeCtx.value && bridgeCtx.value.isDark)

const langOptions = computed(() => {
  const s = new Set()
  voices.value.forEach((v) => { if (v.language) s.add(v.language) })
  return Array.from(s).sort()
})
const stats = computed(() => {
  const total = voices.value.length
  const hidden = voices.value.filter((v) => v.hidden).length
  return { total, hidden, visible: total - hidden }
})

const filteredVoices = computed(() => {
  let list = voices.value
  if (filterLang.value && filterLang.value !== '__all') {
    list = list.filter((v) => v.language === filterLang.value)
  }
  if (visibleFilter.value === 'visible') list = list.filter((v) => !v.hidden)
  else if (visibleFilter.value === 'hidden') list = list.filter((v) => v.hidden)
  const q = search.value.trim().toLowerCase()
  if (q) {
    list = list.filter(
      (v) => (v.name || '').toLowerCase().includes(q)
        || (v.prompt_text || '').toLowerCase().includes(q),
    )
  }
  return list
})

const grouped = computed(() => {
  const m = {}
  for (const v of filteredVoices.value) {
    const lang = v.language || 'zh'
    ;(m[lang] = m[lang] || []).push(v)
  }
  const keys = Object.keys(m).sort((a, b) => {
    if (a === '未分类') return 1
    if (b === '未分类') return -1
    return a.localeCompare(b)
  })
  return keys.map((k) => ({ lang: k, items: m[k] }))
})

function langLabel(l) {
  const map = { '未分类': '未分类', zh: '中文', en: '英文', ja: '日文', ko: '韩文', ru: '俄文', th: '泰文', ar: '阿拉伯', hi: '印地', fr: '法文', de: '德文', es: '西文', pt: '葡文', it: '意文', vi: '越南', id: '印尼', ms: '马来', tr: '土耳其', fa: '波斯', pl: '波兰' }
  return map[l] || l
}

async function load() {
  loading.value = true
  try {
    const r = await bridge.apiGet('voices')
    voices.value = (r.voices || []).map((v) => ({
      name: v.name,
      prompt_wav: v.prompt_wav || '',
      prompt_text: v.prompt_text || '',
      language: v.language || 'zh',
      hidden: !!v.hidden,
      markup: v.markup !== false,
      is_default: !!v.is_default,
    }))
  } catch (e) {
    ElMessage.error('加载音色失败：' + e)
  } finally {
    loading.value = false
  }
}

async function playVoice(vname) {
  if (!auditionText.value.trim()) {
    ElMessage.warning('请先填写试听文本')
    return
  }
  playing.value = vname || '默认音色'
  try {
    const params = vname ? { voice: vname, text: auditionText.value } : { text: auditionText.value }
    const res = await bridge.download('synthesize', params)
    // 兼容 bridge 返回字符串 URL 或 { blob, filename } 两种形态
    let url = ''
    if (typeof res === 'string') url = res
    else if (res && res.blob) url = URL.createObjectURL(res.blob)
    if (!url) throw new Error('未获取到音频')
    audioSrc.value = url
    await nextTick()
    if (audioRef.value) await audioRef.value.play()
  } catch (e) {
    ElMessage.error('试听失败：' + e)
    playing.value = ''
  }
}
async function audition(v) { await playVoice(v.name) }
async function auditionDefault() { await playVoice('') }
function onEnded() { playing.value = '' }
function onPlay() { /* noop */ }

async function setDefault(v) {
  try {
    await bridge.apiPost('voices/default', { name: v.name })
    ElMessage.success(`已将「${v.name}」设为默认音色`)
    await load()
  } catch (e) {
    ElMessage.error('设置默认失败：' + e)
  }
}

function openCreate() {
  isEdit.value = false
  Object.assign(editForm, { name: '', prompt_wav: '', prompt_text: '', language: '', hidden: false, markup: true })
  editVisible.value = true
}
function openEdit(v) {
  isEdit.value = true
  const full = voices.value.find((x) => x.name === v.name) || v
  Object.assign(editForm, {
    name: v.name,
    prompt_wav: full.prompt_wav || '',
    prompt_text: full.prompt_text || '',
    language: full.language || '',
    hidden: !!full.hidden,
    markup: full.markup !== false,
  })
  editVisible.value = true
}
async function saveEdit() {
  if (!editForm.name.trim()) {
    ElMessage.warning('请填写音色名')
    return
  }
  const payload = {
    name: editForm.name.trim(),
    prompt_wav: editForm.prompt_wav,
    prompt_text: editForm.prompt_text,
    language: editForm.language,
    hidden: editForm.hidden,
    markup: editForm.markup,
  }
  try {
    if (isEdit.value) await bridge.apiPost('voices/update', payload)
    else await bridge.apiPost('voices/create', payload)
    editVisible.value = false
    ElMessage.success('已保存')
    await load()
  } catch (e) {
    ElMessage.error('保存失败：' + e)
  }
}
async function remove(v) {
  try {
    await ElMessageBox.confirm(`确定删除音色「${v.name}」？`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await bridge.apiPost('voices/delete', { name: v.name })
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error('删除失败：' + e)
  }
}

async function batchHide(h) {
  for (const n of selected.value) {
    try { await bridge.apiPost('voices/update', { name: n, hidden: h }) } catch (e) { ElMessage.error(e) }
  }
  selected.value = []
  await load()
}
async function batchDelete() {
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${selected.value.length} 个音色？`, '批量删除', { type: 'warning' })
  } catch {
    return
  }
  for (const n of selected.value) {
    try { await bridge.apiPost('voices/delete', { name: n }) } catch (e) { ElMessage.error(e) }
  }
  selected.value = []
  await load()
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.cv-page { display: flex; flex-direction: column; gap: 14px; }
.cv-audition-head { display: flex; align-items: center; gap: 8px; }
.cv-audition-ico { color: var(--cv-primary); font-size: 18px; }
.cv-audition-bar { display: flex; align-items: center; gap: 14px; }
.cv-audio { height: 38px; flex: 1; max-width: 420px; }

.cv-batchbar {
  position: sticky; bottom: 12px; z-index: 5;
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px;
  background: var(--cv-panel);
  border: 1px solid var(--cv-border);
  border-radius: var(--cv-radius);
  box-shadow: var(--cv-shadow);
}
.cv-slide-enter-active, .cv-slide-leave-active { transition: all .2s; }
.cv-slide-enter-from, .cv-slide-leave-to { opacity: 0; transform: translateY(8px); }

.cv-langgroup { margin-bottom: 6px; }
.cv-langgroup-head { display: flex; align-items: center; gap: 10px; margin: 6px 2px 12px; }

.cv-voice-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.cv-voice-card {
  background: var(--cv-panel);
  border: 1px solid var(--cv-border);
  border-radius: var(--cv-radius-sm);
  padding: 14px;
  display: flex; flex-direction: column; gap: 8px;
  transition: box-shadow .2s, transform .2s, border-color .2s;
}
.cv-voice-card:hover {
  box-shadow: var(--cv-shadow);
  transform: translateY(-2px);
  border-color: var(--cv-primary-2);
}
.cv-voice-card.is-hidden { opacity: .6; }
.cv-voice-top { display: flex; align-items: center; gap: 10px; }
.cv-voice-name { font-weight: 700; display: flex; align-items: center; gap: 6px; }
.cv-star { color: var(--cv-warn); }
.cv-voice-meta { font-size: 12px; word-break: break-all; }
.cv-voice-text {
  font-size: 12px; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.cv-voice-actions { display: flex; gap: 8px; margin-top: auto; }
</style>
