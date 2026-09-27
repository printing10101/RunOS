<template>
  <div class="coach-page">
    <div style="display:flex; align-items:flex-start; justify-content:space-between">
      <div>
        <h1 class="page-title">AI 教练</h1>
        <div class="page-sub">本地大模型驱动 · 回答基于你的真实数据，课表改动需你确认后生效</div>
      </div>
      <el-tag v-if="status" :type="status.reachable ? 'success' : 'info'" effect="dark" round>
        {{ status.reachable ? (status.model_ready ? `已连接 · ${status.model}` : `服务在线 · 缺模型 ${status.model}`) : '未连接本地模型' }}
      </el-tag>
    </div>

    <el-alert v-if="status && !status.reachable" type="warning" :closable="false" show-icon style="margin-top:12px">
      <template #title>本地模型未就绪，两步开启 AI 教练</template>
      <ol style="margin:4px 0 0; padding-left:18px; line-height:1.9">
        <li>运行项目根目录的 <code>python setup_ai_local.py</code>（校验并安装 LM Studio、放置模型、写好配置；已下载过安装包与模型则全程只需点几下）</li>
        <li>按脚本提示启动模型服务，<a href="#" @click.prevent="loadStatus">点此重新检测</a>。也可用 Ollama：装好后 <code>ollama pull qwen3:8b</code></li>
      </ol>
    </el-alert>

    <div class="conv-bar">
      <el-select v-model="conversationId" placeholder="历史对话" size="small" class="conv-select"
                 :disabled="streaming" @change="openConversation">
        <el-option v-for="c in conversations" :key="c.id" :value="c.id" :label="c.title" />
      </el-select>
      <el-button size="small" round :disabled="streaming" @click="newChat">＋ 新对话</el-button>
      <el-button v-if="conversationId" size="small" round type="danger" plain
                 :disabled="streaming" @click="deleteChat">删除</el-button>
    </div>

    <div class="chat-box" ref="chatBox">
      <div v-if="!messages.length" class="empty-hints">
        <div class="hint-title">试试问教练：</div>
        <el-button v-for="q in quickPrompts" :key="q" round plain size="large" class="hint-btn"
                   @click="send(q)">{{ q }}</el-button>
      </div>

      <div v-for="(m, mi) in messages" :key="mi" class="msg-row" :class="m.role">
        <el-avatar v-if="m.role === 'assistant'" :size="30" class="msg-avatar">AI</el-avatar>
        <div class="msg-body">
          <div v-if="m.tools?.length" class="tool-chips">
            <el-tag v-for="(t, ti) in m.tools" :key="ti" size="small" effect="plain"
                    :type="t.ok ? 'info' : 'warning'" class="tool-chip">
              {{ t.ok ? '🔍' : '⚠️' }} {{ t.label }}
            </el-tag>
          </div>

          <div v-if="m.role === 'user'" class="bubble user">{{ m.content }}</div>
          <div v-else-if="m.content || m.streaming" class="bubble ai">
            <span style="white-space:pre-wrap" v-html="renderRich(m.content)"></span>
            <span v-if="m.streaming && !m.content" class="thinking">正在结合你的数据思考…</span>
            <span v-else-if="m.streaming" class="cursor">▍</span>
          </div>
          <div v-if="m.error" class="bubble ai error">{{ m.error }}</div>

          <div v-for="(p, pi) in m.proposals || []" :key="pi" class="proposal">
            <div class="p-title">📋 {{ p.title }}</div>
            <div v-if="p.summary" class="p-line">内容：{{ p.summary }}</div>
            <div v-if="p.new_steps_preview" class="p-line">调整后：{{ p.new_steps_preview }}</div>
            <div v-for="(w, wi) in p.warnings || []" :key="wi" class="p-warn">⚠ {{ w }}</div>
            <div class="p-actions">
              <el-button size="small" type="primary" round :loading="p.applying" :disabled="p.applied"
                         @click="applyProposal(p)">{{ p.applied ? '✓ 已应用' : '确认应用' }}</el-button>
              <el-tag v-if="p.applied" size="small" type="success" effect="plain">课表已更新，可到「训练计划」页查看</el-tag>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="input-bar">
      <el-input v-model="input" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }"
                placeholder="问教练：这周练什么 / 状态不好怎么办 / 想换个目标…"
                :disabled="streaming" @keydown.enter.exact.prevent="send()" />
      <el-button type="primary" round :loading="streaming" @click="send()">发送</el-button>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const status = ref(null)
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const chatBox = ref(null)
// 会话持久化：历史由服务端从库内加载并落库，刷新页面后聊天记录不丢
const conversations = ref([])
const conversationId = ref(null)
// 跟踪正在进行的流式请求，组件卸载时中止，避免写已卸载组件
let activeAbort = null

const quickPrompts = [
  '这周练什么？帮我讲讲课表安排',
  '我最近的恢复状态怎么样，适合上强度吗',
  '我的短板是什么，怎么改进？',
  '全马破三可行吗？帮我评估一下',
]

const TOOL_FALLBACK_LABEL = '查询训练数据'

// 模型回答是 markdown 口语体（**加粗**、`代码`），不引第三方渲染器：
// 先转义 HTML 再替换这两种行内标记，换行交给 pre-wrap
function renderRich(text) {
  if (!text) return ''
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

onMounted(() => { loadStatus(); loadConversations({ autoResume: true }) })
async function loadStatus() { try { status.value = await api.get('/ai/status') } catch { status.value = { reachable: false } } }

async function loadConversations({ autoResume = false } = {}) {
  try {
    const data = await api.get('/ai/conversations')
    conversations.value = data.conversations || []
    // 刷新页面后自动接上最近一次对话
    if (autoResume && !conversationId.value && conversations.value.length) {
      await openConversation(conversations.value[0].id)
    }
  } catch { /* 列表拉取失败不阻塞聊天；真正发消息时错误会响亮暴露 */ }
}

async function openConversation(cid) {
  if (streaming.value) return
  try {
    const data = await api.get(`/ai/conversations/${cid}`)
    conversationId.value = data.id
    messages.value = data.messages.map(m => ({
      role: m.role, content: m.content,
      tools: m.tools || [], proposals: m.proposals || [],
    }))
    scrollBottom()
  } catch (e) {
    ElMessage.error(e.message || '加载历史对话失败')
  }
}

function newChat() {
  if (streaming.value) return
  conversationId.value = null
  messages.value = []
}

async function deleteChat() {
  if (streaming.value || !conversationId.value) return
  try {
    await ElMessageBox.confirm('删除后这段对话不可恢复，确定删除？', '删除对话', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  try {
    await api.del(`/ai/conversations/${conversationId.value}`)
    newChat()
    loadConversations()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

function scrollBottom() { nextTick(() => { if (chatBox.value) chatBox.value.scrollTop = chatBox.value.scrollHeight }) }

async function send(preset) {
  const text = (preset ?? input.value).trim()
  if (!text || streaming.value) return
  input.value = ''
  // 会话模式下历史由服务端从库内加载；首次发消息先建会话（标题由服务端取首条提问）
  if (!conversationId.value) {
    try {
      const conv = await api.post('/ai/conversations', {})
      conversationId.value = conv.id
      loadConversations()
    } catch (e) {
      ElMessage.error(e.message || '创建会话失败')
      return
    }
  }
  messages.value.push({ role: 'user', content: text })
  const reply = { role: 'assistant', content: '', tools: [], proposals: [], streaming: true }
  messages.value.push(reply)
  streaming.value = true
  scrollBottom()

  const ac = new AbortController()
  activeAbort = ac
  try {
    const resp = await fetch('/api/ai/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId.value, message: text }),
      signal: ac.signal,
    })
    if (!resp.ok || !resp.body) throw new Error(`服务返回 ${resp.status}`)

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop()
      for (const line of parts) {
        if (!line.startsWith('data:')) continue
        let evt
        try { evt = JSON.parse(line.slice(5)) } catch { continue }
        if (evt.type === 'delta') {
          reply.content += evt.text
        } else if (evt.type === 'tool') {
          reply.tools.push({ label: evt.label || TOOL_FALLBACK_LABEL, ok: evt.ok !== false })
        } else if (evt.type === 'proposals') {
          reply.proposals.push(...(evt.items || []))
        } else if (evt.type === 'error') {
          reply.error = evt.message
        }
        scrollBottom()
      }
    }
  } catch (e) {
    // 组件卸载主动中止，不属于错误，不展示提示
    if (e?.name !== 'AbortError') reply.error = e.message || '连接服务失败'
  } finally {
    if (activeAbort === ac) activeAbort = null
    if (!mountAborted) {
      reply.streaming = false
      streaming.value = false
      scrollBottom()
      // 会话标题与排序由服务端更新，完成后静默刷新列表
      loadConversations()
    }
  }
}

// 组件卸载时中止在途的流式请求
let mountAborted = false
onUnmounted(() => { mountAborted = true; activeAbort?.abort() })

async function applyProposal(p) {
  p.applying = true
  try {
    await api.post('/ai/proposals/apply', p.apply)
    p.applied = true
    ElMessage.success('课表已更新')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '应用失败：校验未通过（课表可能已变化）')
  } finally { p.applying = false }
}
</script>

<style scoped>
.coach-page { display: flex; flex-direction: column; height: calc(100vh - 44px); }
.chat-box { flex: 1; overflow-y: auto; padding: 18px 4px; display: flex; flex-direction: column; gap: 16px; }

.empty-hints { margin: auto; text-align: center; display: flex; flex-direction: column; gap: 12px; align-items: center; }
.hint-title { color: var(--text-3); font-size: 13px; }
.hint-btn { color: var(--text-2); }
.hint-btn:hover { color: var(--lime); border-color: rgba(200, 241, 105, 0.45); }

.conv-bar { display: flex; gap: 8px; align-items: center; padding: 10px 0 0; }
.conv-select { width: 260px; }

.msg-row { display: flex; gap: 10px; }
.msg-row.user { justify-content: flex-end; }
.msg-avatar { background: linear-gradient(135deg, var(--lime), #9bcf4a); color: #0b0f14; font-weight: 800; font-size: 12px; flex-shrink: 0; }
.msg-body { max-width: 76%; display: flex; flex-direction: column; gap: 8px; }
.msg-row.user .msg-body { align-items: flex-end; }

.bubble { border-radius: 14px; padding: 10px 14px; font-size: 14px; line-height: 1.7; }
.bubble.user { background: linear-gradient(135deg, rgba(200, 241, 105, 0.16), rgba(200, 241, 105, 0.07)); border: 1px solid rgba(200, 241, 105, 0.25); }
.bubble.ai { background: var(--bg-inset); border: 1px solid var(--border); }
.bubble.ai code { background: rgba(255, 255, 255, 0.08); border-radius: 4px; padding: 1px 5px; font-size: 13px; }
.bubble.ai.error { border-color: rgba(255, 107, 107, 0.45); color: #ff8b8b; background: rgba(255, 107, 107, 0.06); }
.thinking { color: var(--text-3); }
.cursor { animation: blink 1s steps(2) infinite; color: var(--lime); }
@keyframes blink { 50% { opacity: 0; } }

.tool-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.tool-chip { font-size: 11px; }

.proposal { border: 1px solid rgba(200, 241, 105, 0.35); background: rgba(200, 241, 105, 0.05); border-radius: 12px; padding: 12px 14px; }
.p-title { font-weight: 700; font-size: 13.5px; margin-bottom: 6px; }
.p-line { font-size: 12.5px; color: var(--text-2); margin: 3px 0; }
.p-warn { font-size: 12px; color: var(--orange); margin: 3px 0; }
.p-actions { margin-top: 10px; display: flex; align-items: center; gap: 10px; }

.input-bar { display: flex; gap: 10px; align-items: flex-end; padding: 12px 0 4px; border-top: 1px solid var(--border); }
.input-bar :deep(.el-textarea__inner) { border-radius: 12px; }
code { background: rgba(148, 163, 184, 0.12); padding: 1px 6px; border-radius: 5px; font-size: 12px; }
</style>
