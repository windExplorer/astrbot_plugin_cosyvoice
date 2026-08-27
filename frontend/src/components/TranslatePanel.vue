<template>
  <div class="cv-page">
    <div class="cv-card">
      <div class="cv-audition-head">
        <el-icon class="cv-audition-ico"><Connection /></el-icon>
        <span class="cv-section-title" style="margin: 0">翻译合成</span>
        <el-switch
          v-model="config.enabled"
          inline-prompt
          active-text="开"
          inactive-text="关"
          style="margin-left: 8px"
        />
      </div>
      <div class="cv-muted" style="margin: 8px 0 0">
        合成前按需翻译：目标语种取所选音色的语种（voices 配置的 language 字段），即「中文文本 + 外语音色 → 翻成该音色语种」。音色未配置语种时回落到下方全局目标语种（默认汉语）。语种判定走本地字符检测，零额外消耗；仅当文本语种与目标语种不一致时才调用翻译 API。
      </div>

      <el-divider />

      <el-form label-width="110px" label-position="right" class="cv-form">
        <el-form-item label="目标语种">
          <el-select v-model="config.target" filterable allow-create default-first-option style="width: 220px">
            <el-option v-for="l in LANG_OPTIONS" :key="l" :label="l" :value="l" />
          </el-select>
          <span class="cv-muted" style="margin-left: 10px">全局回落目标语种：仅当所选音色未配置语种时生效；合成优先用音色的 language（按音色翻译）</span>
        </el-form-item>
        <el-form-item label="需翻译语种">
          <el-select
            v-model="config.source"
            multiple
            filterable
            allow-create
            default-first-option
            collapse-tags
            collapse-tags-tooltip
            placeholder="留空 = 除目标语种外全部"
            style="width: 100%"
          >
            <el-option v-for="l in LANG_OPTIONS" :key="l" :label="l" :value="l" />
          </el-select>
          <div class="cv-muted" style="width: 100%">留空表示：除目标语种外的所有语种都翻译；填了则只翻译名单内的语种。</div>
        </el-form-item>
      </el-form>
    </div>

    <!-- 翻译 API 配置 -->
    <div class="cv-card">
      <div class="cv-section-title"><el-icon><SetUp /></el-icon>翻译 API 配置</div>

      <el-form label-width="120px" label-position="right" class="cv-form">
        <el-form-item label="请求地址 URL">
          <el-input v-model="config.api.url" placeholder="https://your-translate-api/translate" />
        </el-form-item>
        <el-form-item label="请求方法">
          <el-radio-group v-model="config.api.method">
            <el-radio-button value="POST">POST</el-radio-button>
            <el-radio-button value="GET">GET</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-divider content-position="left">认证</el-divider>
        <el-form-item label="API Key">
          <el-input v-model="config.api.apikey" placeholder="你的密钥" show-password />
        </el-form-item>
        <el-form-item label="认证头名">
          <el-input v-model="config.api.auth_header" placeholder="Authorization" style="width: 240px" />
          <span class="cv-muted" style="margin-left: 10px">默认 Authorization</span>
        </el-form-item>
        <el-form-item label="认证 scheme">
          <el-input v-model="config.api.auth_scheme" placeholder="Bearer" style="width: 240px" />
          <span class="cv-muted" style="margin-left: 10px">默认 Bearer，最终为「scheme key」</span>
        </el-form-item>

        <el-divider content-position="left">请求参数模板</el-divider>
        <el-form-item label="内容类型">
          <el-radio-group v-model="config.api.content_type">
            <el-radio-button value="json">JSON</el-radio-button>
            <el-radio-button value="form">表单(x-www)</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="额外请求头">
          <div class="cv-kvlist">
            <div v-for="(h, i) in config.api.extra_headers" :key="'h' + i" class="cv-kvrow">
              <el-input v-model="h.key" placeholder="Header 名" />
              <el-input v-model="h.value" placeholder="Header 值（可含 {text}/{source}/{target}）" />
              <el-button text type="danger" :icon="Minus" @click="rmRow(config.api.extra_headers, i)" />
            </div>
            <el-button text type="primary" :icon="Plus" @click="addRow(config.api.extra_headers)">添加请求头</el-button>
          </div>
        </el-form-item>
        <el-form-item label="请求参数">
          <div class="cv-kvlist">
            <div v-for="(p, i) in config.api.params" :key="'p' + i" class="cv-kvrow">
              <el-input v-model="p.key" placeholder="参数名，如 q / text / from / to" />
              <el-input v-model="p.value" placeholder="参数值，可含 {text} {source} {target}" />
              <el-button text type="danger" :icon="Minus" @click="rmRow(config.api.params, i)" />
            </div>
            <el-button text type="primary" :icon="Plus" @click="addRow(config.api.params)">添加参数</el-button>
            <div class="cv-muted" style="width: 100%">占位符：{text}=待翻译文本，{source}=源语种，{target}=目标语种</div>
          </div>
        </el-form-item>

        <el-divider content-position="left">语种代码映射</el-divider>
        <el-form-item label="代码映射">
          <div class="cv-kvlist">
            <div v-for="(m, i) in config.lang_map" :key="'m' + i" class="cv-kvrow">
              <el-input v-model="m.key" placeholder="简码，如 zh" />
              <el-input v-model="m.value" placeholder="接口码，如 zh-CN" />
              <el-button text type="danger" :icon="Minus" @click="rmRow(config.lang_map, i)" />
            </div>
            <el-button text type="primary" :icon="Plus" @click="addRow(config.lang_map)">添加映射</el-button>
          </div>
          <div class="cv-muted" style="width: 100%">插件输出简码（zh/en/ja/ko/ru/th/ar/hi）；若翻译接口要带区域码（如 zh-CN），在此把简码映射成接口码，作用于 {source}/{target}。</div>
        </el-form-item>

        <el-divider content-position="left">响应解析</el-divider>
        <el-form-item label="译文路径">
          <el-input v-model="config.api.response_path" placeholder="data.trans_result[0].dst" />
          <span class="cv-muted" style="margin-left: 10px">从响应 JSON 取译文的路径（点号+数组下标）</span>
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="config.api.timeout" :min="1" :max="120" />
        </el-form-item>
      </el-form>
    </div>

    <!-- 测试 + 保存 -->
    <div class="cv-card">
      <div class="cv-section-title"><el-icon><MagicStick /></el-icon>测试</div>
      <el-input
        v-model="testSample"
        type="textarea"
        :rows="2"
        placeholder="输入一段要测试的文本（如英文 / 日文），验证检测语种与翻译结果"
      />
      <div style="margin-top: 10px">
        <el-button type="primary" :loading="testing" :icon="VideoPlay" @click="runTest">测试翻译</el-button>
        <el-button type="success" :icon="Check" @click="save">保存配置</el-button>
      </div>

      <el-alert
        v-if="testResult && testResult.ok === false && testResult.error"
        type="error"
        :title="'测试失败：' + testResult.error"
        show-icon
        style="margin-top: 12px"
      />
      <div class="cv-muted" style="margin-top: 12px">测试按全局目标语种验证接口与语种映射；实际合成时目标语种取所选音色的 language（按音色翻译）。</div>
      <el-descriptions
        v-if="testResult"
        :column="1"
        border
        style="margin-top: 12px"
      >
        <el-descriptions-item label="检测语种">{{ testResult.detected_lang }}</el-descriptions-item>
        <el-descriptions-item label="目标语种">{{ testResult.target_lang }}</el-descriptions-item>
        <el-descriptions-item label="是否需要翻译">
          <el-tag :type="testResult.should_translate ? 'warning' : 'success'">
            {{ testResult.should_translate ? '是' : '否' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item v-if="testResult.skipped" label="结果">源语言即目标语言，跳过翻译</el-descriptions-item>
        <el-descriptions-item v-else label="译文">{{ testResult.result }}</el-descriptions-item>
      </el-descriptions>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, inject, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Plus, Minus, VideoPlay, Check, Connection, SetUp, MagicStick,
} from '@element-plus/icons-vue'

const bridge = inject('bridge')

const LANG_OPTIONS = [
  'zh', 'en', 'ja', 'ko', 'ru', 'th', 'ar', 'hi',
  'fr', 'de', 'es', 'pt', 'it', 'vi', 'id', 'ms', 'tr', 'fa', 'pl',
]

function emptyApi() {
  return {
    url: '', method: 'POST', apikey: '', auth_header: 'Authorization',
    auth_scheme: 'Bearer', content_type: 'json', extra_headers: [], params: [],
    response_path: '', timeout: 15,
  }
}

const config = reactive({
  enabled: false,
  target: 'zh',
  source: [],
  lang_map: [],
  api: emptyApi(),
})

const testSample = ref('Hello, this is a test for translation.')
const testing = ref(false)
const testResult = ref(null)

function addRow(list) { list.push({ key: '', value: '' }) }
function rmRow(list, i) { list.splice(i, 1) }

async function load() {
  try {
    const r = await bridge.apiGet('translate')
    config.enabled = !!r.enabled
    config.target = r.target || 'zh'
    config.source = Array.isArray(r.source) ? r.source : []
    config.lang_map = Array.isArray(r.lang_map) ? r.lang_map : []
    Object.assign(config.api, emptyApi(), r.api || {})
  } catch (e) {
    ElMessage.error('加载翻译配置失败：' + e)
  }
}

async function save() {
  try {
    await bridge.apiPost('translate', JSON.parse(JSON.stringify(config)))
    ElMessage.success('已保存并立即生效')
  } catch (e) {
    ElMessage.error('保存失败：' + e)
  }
}

async function runTest() {
  if (!testSample.value.trim()) {
    ElMessage.warning('请填写测试文本')
    return
  }
  testing.value = true
  try {
    const r = await bridge.apiPost('translate/test', { sample: testSample.value })
    testResult.value = r
  } catch (e) {
    testResult.value = { ok: false, error: String(e) }
  } finally {
    testing.value = false
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.cv-page { display: flex; flex-direction: column; gap: 14px; }
.cv-audition-head { display: flex; align-items: center; gap: 8px; }
.cv-audition-ico { color: var(--cv-primary); font-size: 18px; }
.cv-form :deep(.el-form-item__label) { font-weight: 600; }
.cv-kvlist { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.cv-kvrow { display: flex; gap: 8px; align-items: center; }
.cv-kvrow .el-input { flex: 1; }
</style>
