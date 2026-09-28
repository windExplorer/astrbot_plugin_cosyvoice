<template>
  <div class="cv-page">
    <div class="cv-card">
      <div class="cv-toolbar">
        <div class="cv-section-title" style="margin: 0">
          <el-icon><ChatRound /></el-icon>会话
        </div>
        <span class="cv-muted">
          按会话配置音色 / 发送方式 / 语音概率；头像与昵称取自聊天记录（取不到时显示首字母）
        </span>
        <div class="cv-spacer" />
        <el-button :icon="Picture" :loading="avatarLoading" @click="refreshAvatars">更新头像</el-button>
        <el-button :icon="Delete" type="danger" plain @click="clearAll">清空全部</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>

      <div class="cv-chips">
        <span class="cv-chip">共 <b>{{ stats.total }}</b> 个会话</span>
        <span class="cv-chip ok">已开启 <b>{{ stats.on }}</b></span>
        <span class="cv-chip warn">概率触发 <b>{{ stats.prob }}</b></span>
        <span class="cv-chip">未开启 <b>{{ stats.off }}</b></span>
      </div>

      <div v-loading="loading" class="cv-sess-list">
        <div v-for="row in sessions" :key="row.id" class="cv-sess-row">
          <div class="cv-sess-id">
            <el-avatar
              :size="46"
              :src="avatarOf(row)"
              shape="circle"
              class="cv-avatar"
              @error="() => onAvatarError(row)"
            >
              <span class="cv-avatar-fallback">{{ initialOf(row) }}</span>
            </el-avatar>
            <div class="cv-sess-text">
              <div class="cv-sess-name">
                <span class="cv-ellipsis">{{ displayName(row) }}</span>
                <el-tag v-if="row.is_group" size="small" type="info" effect="plain" round>群</el-tag>
                <el-tag v-else size="small" type="success" effect="plain" round>私聊</el-tag>
              </div>
              <div class="cv-sess-sub">
                <span v-if="row.is_group">群号 {{ row.group_id }}</span>
                <span v-else-if="row.user_id">QQ {{ row.user_id }}</span>
                <!-- 只展示群号 / QQ 号；完整 unified_msg_origin 放在悬浮提示里，需要时再看 -->
                <el-tooltip :content="row.id" placement="bottom" :show-after="400">
                  <span class="cv-plat">{{ row.platform }}</span>
                </el-tooltip>
              </div>
            </div>
          </div>

          <div class="cv-sess-ctl">
            <div class="cv-ctl">
              <label>语音</label>
              <el-switch
                v-model="row.on"
                inline-prompt
                active-text="开"
                inactive-text="关"
                @change="(val) => toggleOn(row, val)"
              />
            </div>
            <div class="cv-ctl">
              <label>概率</label>
              <el-input-number
                v-model="row.prob_percent"
                :min="0"
                :max="100"
                :step="5"
                size="small"
                controls-position="right"
                :disabled="!row.on"
                style="width: 108px"
                @change="(val) => saveProb(row, val)"
              />
              <span class="cv-pct">%</span>
            </div>
            <div class="cv-ctl">
              <label>发送</label>
              <el-select
                :model-value="rowSendMode(row)"
                size="small"
                style="width: 116px"
                @change="(val) => saveMode(row, val)"
              >
                <el-option label="默认" value="" />
                <el-option label="语音+文字" value="both" />
                <el-option label="仅语音" value="voice_only" />
              </el-select>
            </div>
            <div class="cv-ctl">
              <label>音色</label>
              <el-select
                :model-value="row.voice === '默认' ? '' : row.voice"
                size="small"
                filterable
                allow-create
                style="width: 132px"
                @change="(val) => saveVoice(row, val)"
              >
                <el-option label="默认" value="" />
                <el-option v-for="v in voices" :key="v" :label="v" :value="v" />
              </el-select>
            </div>
            <el-tooltip content="删除该会话的语音配置" placement="top">
              <el-button size="small" :icon="Delete" type="danger" plain circle @click="remove(row)" />
            </el-tooltip>
          </div>
        </div>
        <el-empty v-if="!loading && !sessions.length" description="暂无活跃会话" :image-size="70" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ChatRound, Delete, Refresh, Picture } from '@element-plus/icons-vue'

const bridge = inject('bridge')
const sessions = ref([])
const loading = ref(false)
const avatarLoading = ref(false)
const voices = ref([])
// 头像缓存：'group:123' / 'user:456' -> data URI（抓不到就不出现在这里，前端退化为首字母）
const avatarMap = ref({})
// 加载失败过的头像（后端 + CDN 都没成）→ 该行直接用首字母色块
const avatarFailed = ref(new Set())

const stats = computed(() => {
  const total = sessions.value.length
  const on = sessions.value.filter((r) => r.on).length
  const prob = sessions.value.filter((r) => r.on && r.prob != null && r.prob < 1).length
  return { total, on, prob, off: total - on }
})

function displayName(row) {
  if (row.nickname) return row.nickname
  if (row.is_group) return row.group_id ? `群 ${row.group_id}` : row.label || row.id
  return row.user_id ? `QQ ${row.user_id}` : row.id
}
function initialOf(row) {
  const n = String(displayName(row)).trim()
  return n ? [...n][0] : '?'
}
// 兜底：后端没取到（服务器无外网 / CDN 暂时不通）时，浏览器直接试腾讯 CDN——
// 面板内嵌 iframe 有时能直连外网，能显示就显示；失败则由 @error 退回首字母色块。
function cdnUrl(row) {
  const id = String(row.avatar_id || '')
  if (!/^\d+$/.test(id)) return ''
  return row.avatar_kind === 'group'
    ? `https://p.qlogo.cn/gh/${id}/${id}/100`
    : `https://q1.qlogo.cn/g?b=qq&nk=${id}&s=100`
}
function avatarOf(row) {
  if (!row.avatar_id) return ''
  const key = `${row.avatar_kind}:${row.avatar_id}`
  if (avatarFailed.value.has(key)) return ''
  return avatarMap.value[key] || cdnUrl(row)
}
// 图片加载失败（含 CDN 直连被拦）→ 记为失败，此后该行只用首字母色块，不再反复请求
function onAvatarError(row) {
  const key = `${row.avatar_kind}:${row.avatar_id}`
  if (!key || avatarFailed.value.has(key)) return
  const next = new Set(avatarFailed.value)
  next.add(key)
  avatarFailed.value = next
}
function rowSendMode(row) {
  if (row.mode === '语音+文字') return 'both'
  if (row.mode === '仅语音') return 'voice_only'
  return ''
}

// 头像按「群 / 用户」两类分别批量取（群头像用群号、私聊头像用 QQ 号）
async function loadAvatars(rows, force = false) {
  const groups = [...new Set(rows.filter((r) => r.avatar_kind === 'group' && r.avatar_id).map((r) => r.avatar_id))]
  const users = [...new Set(rows.filter((r) => r.avatar_kind === 'user' && r.avatar_id).map((r) => r.avatar_id))]
  const jobs = []
  if (groups.length) jobs.push(['group', groups])
  if (users.length) jobs.push(['user', users])
  if (!jobs.length) return
  const results = await Promise.all(
    jobs.map(([kind, ids]) =>
      bridge
        .apiGet('avatars', { type: kind, ids: ids.join(','), force: force ? 1 : 0 })
        .catch(() => ({ items: {} })),
    ),
  )
  const next = { ...avatarMap.value }
  results.forEach((res, i) => {
    const kind = jobs[i][0]
    const items = (res && res.items) || {}
    Object.keys(items).forEach((id) => {
      next[`${kind}:${id}`] = items[id]
    })
  })
  avatarMap.value = next
}

async function load() {
  loading.value = true
  try {
    const [s, v] = await Promise.all([
      bridge.apiGet('sessions'),
      bridge.apiGet('voices').catch(() => ({ voices: [] })),
    ])
    sessions.value = (s.sessions || []).map((r) => ({
      ...r,
      // 概率就地编辑用 0~100 的整数；未开启时给个默认 100，开关打开即常开
      prob_percent: r.prob_percent == null ? 100 : r.prob_percent,
    }))
    voices.value = (v.voices || []).map((x) => x.name).filter(Boolean)
    await loadAvatars(sessions.value)
  } catch (e) {
    ElMessage.error('加载会话失败：' + e)
  } finally {
    loading.value = false
  }
}

async function refreshAvatars() {
  avatarLoading.value = true
  try {
    await loadAvatars(sessions.value, true)
    ElMessage.success('头像已更新')
  } catch (e) {
    ElMessage.error('更新头像失败：' + e)
  } finally {
    avatarLoading.value = false
  }
}

async function toggleOn(row, val) {
  try {
    // 开关与概率同源：关 = 概率 0（移除配置）；开 = 常开（100%），随后可用概率框调低
    await bridge.apiPost('sessions/set', { origin: row.id, prob: val ? 1 : 0 })
    if (val) row.prob_percent = 100
    ElMessage.success(val ? '已开启语音' : '已关闭语音')
    await load()
  } catch (e) {
    ElMessage.error('操作失败：' + e)
    await load()
  }
}

async function saveProb(row, val) {
  const pct = Number(val)
  if (!Number.isFinite(pct)) return
  try {
    await bridge.apiPost('sessions/set', { origin: row.id, prob: pct / 100 })
    ElMessage.success(
      pct <= 0 ? '已关闭该会话语音' : pct >= 100 ? '已设为常开（每句都念）' : `已设为 ${pct}% 概率发语音`,
    )
    await load()
  } catch (e) {
    ElMessage.error('保存失败：' + e)
    await load()
  }
}

async function saveMode(row, val) {
  try {
    await bridge.apiPost('sessions/set', { origin: row.id, send_mode: val || '' })
    ElMessage.success('已保存发送方式')
    await load()
  } catch (e) {
    ElMessage.error('保存失败：' + e)
    await load()
  }
}

async function saveVoice(row, val) {
  try {
    await bridge.apiPost('sessions/set', { origin: row.id, voice: val || '' })
    ElMessage.success('已保存音色')
    await load()
  } catch (e) {
    ElMessage.error('保存失败：' + e)
    await load()
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`删除会话「${displayName(row)}」的语音配置？`, '删除确认', { type: 'warning' })
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
    await ElMessageBox.confirm('清空所有会话的语音配置？', '清空确认', { type: 'warning' })
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

.cv-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.cv-chip {
  font-size: 12px; color: var(--cv-text-2);
  background: var(--cv-panel-2);
  border: 1px solid var(--cv-border);
  border-radius: 999px;
  padding: 3px 10px;
}
.cv-chip b { color: var(--cv-text); }
.cv-chip.ok b { color: var(--cv-success); }
.cv-chip.warn b { color: var(--cv-warn); }

.cv-sess-list { display: flex; flex-direction: column; gap: 10px; }
.cv-sess-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 14px; flex-wrap: wrap;
  padding: 12px 14px;
  border: 1px solid var(--cv-border);
  border-radius: var(--cv-radius-sm);
  background: var(--cv-panel-2);
  transition: border-color .18s, box-shadow .18s;
}
.cv-sess-row:hover { border-color: var(--cv-primary); box-shadow: var(--cv-shadow-sm); }

.cv-sess-id { display: flex; align-items: center; gap: 12px; flex: 1 1 250px; min-width: 0; }
.cv-avatar {
  flex: 0 0 auto;
  background: var(--cv-primary-soft);
  color: var(--cv-primary);
  font-weight: 700;
  font-size: 16px;
}
.cv-avatar-fallback { font-size: 16px; }
.cv-sess-text { min-width: 0; }
.cv-sess-name { font-weight: 600; display: flex; align-items: center; gap: 6px; }
.cv-ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px; }
.cv-sess-sub {
  font-size: 12px; color: var(--cv-text-2); margin-top: 3px;
  display: flex; gap: 10px; flex-wrap: wrap;
}
/* 平台名：次要信息，悬浮才显示完整 UMO（不占明面） */
.cv-plat { opacity: .75; cursor: help; border-bottom: 1px dashed transparent; }
.cv-plat:hover { opacity: 1; border-bottom-color: var(--cv-border); }

.cv-sess-ctl { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.cv-ctl { display: flex; align-items: center; gap: 6px; }
.cv-ctl label { font-size: 12px; color: var(--cv-text-2); }
.cv-pct { font-size: 12px; color: var(--cv-text-2); margin-left: -2px; }

@media (max-width: 760px) {
  .cv-sess-ctl { width: 100%; justify-content: flex-start; }
  .cv-ellipsis { max-width: 150px; }
}
</style>
