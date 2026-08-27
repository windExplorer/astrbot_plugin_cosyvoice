<template>
  <div class="cv-page">
    <div class="cv-stats">
      <div class="cv-stat" :class="ov.client_ready ? 'ok' : 'bad'">
        <div class="cv-stat-ico"><el-icon><Connection /></el-icon></div>
        <div>
          <div class="cv-stat-val">{{ ov.client_ready ? '已连接' : '未连接' }}</div>
          <div class="cv-stat-label">CosyVoice 服务</div>
        </div>
      </div>
      <div class="cv-stat">
        <div class="cv-stat-ico alt"><el-icon><Headset /></el-icon></div>
        <div>
          <div class="cv-stat-val">{{ ov.voices_count ?? '—' }}</div>
          <div class="cv-stat-label">音色数量</div>
        </div>
      </div>
      <div class="cv-stat">
        <div class="cv-stat-ico alt2"><el-icon><Cpu /></el-icon></div>
        <div>
          <div class="cv-stat-val">{{ ov.servers_count ?? '—' }}</div>
          <div class="cv-stat-label">负载均衡服务</div>
        </div>
      </div>
      <div class="cv-stat">
        <div class="cv-stat-ico alt3"><el-icon><ChatDotRound /></el-icon></div>
        <div>
          <div class="cv-stat-val">{{ ov.auto_tts_enabled ? '开启' : '关闭' }}</div>
          <div class="cv-stat-label">自动 TTS</div>
        </div>
      </div>
    </div>

    <div class="cv-grid cv-grid-2">
      <div class="cv-card">
        <div class="cv-section-title"><el-icon><InfoFilled /></el-icon>基础信息</div>
        <el-descriptions :column="1" border>
          <el-descriptions-item label="服务地址">{{ ov.base_url || '—' }}</el-descriptions-item>
          <el-descriptions-item label="默认音色">{{ ov.default_voice || '—' }}</el-descriptions-item>
          <el-descriptions-item label="发送模式">{{ ov.send_mode || '—' }}</el-descriptions-item>
          <el-descriptions-item label="并发会话">{{ ov.concurrent_sessions ?? '—' }}</el-descriptions-item>
          <el-descriptions-item v-if="ov.client_error" label="错误">
            <span style="color: var(--cv-danger)">{{ ov.client_error }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <div class="cv-card">
        <div class="cv-section-title"><el-icon><MagicStick /></el-icon>自动 TTS 配置</div>
        <el-descriptions :column="1" border>
          <el-descriptions-item label="回复转语音">{{ ov.auto_tts_reply ? '开启' : '关闭' }}</el-descriptions-item>
          <el-descriptions-item label="关键词触发">{{ joinArr(ov.auto_tts_keywords) }}</el-descriptions-item>
          <el-descriptions-item label="@触发">{{ ov.auto_tts_mention ? '开启' : '关闭' }}</el-descriptions-item>
          <el-descriptions-item label="发送方式">{{ (ov.send_modes || []).join(' / ') || '—' }}</el-descriptions-item>
        </el-descriptions>
      </div>
    </div>

    <div class="cv-card">
      <div class="cv-section-title"><el-icon><Clock /></el-icon>最近事件</div>
      <el-timeline v-if="(ov.recent_events || []).length">
        <el-timeline-item
          v-for="(e, i) in ov.recent_events"
          :key="i"
          :timestamp="e.time"
          :type="e.ok ? 'success' : 'danger'"
          size="small"
        >
          <span :class="e.ok ? '' : 'cv-ev-err'">{{ e.msg }}</span>
        </el-timeline-item>
      </el-timeline>
      <el-empty v-else description="暂无事件" :image-size="60" />
    </div>
  </div>
</template>

<script setup>
import { ref, inject, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Connection, Headset, Cpu, ChatDotRound, InfoFilled, MagicStick, Clock,
} from '@element-plus/icons-vue'

const bridge = inject('bridge')
const ov = ref({})

function joinArr(a) {
  if (!Array.isArray(a) || !a.length) return '（无）'
  return a.join('、')
}

async function load() {
  try {
    const [o, vc] = await Promise.all([
      bridge.apiGet('overview'),
      bridge.apiGet('voices').catch(() => ({ voices: [] })),
    ])
    ov.value = { ...o, voices_count: (vc.voices || []).length }
  } catch (e) {
    ElMessage.error('加载概览失败：' + e)
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.cv-page { display: flex; flex-direction: column; gap: 14px; }
.cv-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.cv-stat {
  display: flex; align-items: center; gap: 12px;
  background: var(--cv-panel);
  border: 1px solid var(--cv-border);
  border-radius: var(--cv-radius);
  padding: 14px 16px;
  box-shadow: var(--cv-shadow-sm);
}
.cv-stat-ico {
  width: 40px; height: 40px; border-radius: 12px; display: grid; place-items: center;
  background: rgba(24,169,87,.12); color: var(--cv-success); font-size: 20px;
}
.cv-stat.ok .cv-stat-ico { background: rgba(24,169,87,.12); color: var(--cv-success); }
.cv-stat.bad .cv-stat-ico { background: rgba(239,68,68,.12); color: var(--cv-danger); }
.cv-stat-ico.alt { background: var(--cv-primary-soft); color: var(--cv-primary); }
.cv-stat-ico.alt2 { background: rgba(99,102,241,.12); color: #6366f1; }
.cv-stat-ico.alt3 { background: rgba(245,158,11,.12); color: var(--cv-warn); }
.cv-stat-val { font-size: 17px; font-weight: 700; }
.cv-stat-label { font-size: 12px; color: var(--cv-text-2); }
.cv-grid-2 { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.cv-ev-err { color: var(--cv-danger); }
</style>
