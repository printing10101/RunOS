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
      <el-popover v-model:visible="notesVisible" placement="bottom-end" :width="380" trigger="click" @show="loadNotes">
        <template #reference>
          <el-button size="small" round style="margin-left:auto">🧠 记忆{{ notes.length ? ` ${notes.length}` : '' }}</el-button>
        </template>
        <div class="notes-pop">
          <div class="notes-head">教练长期记忆 <span class="notes-sub">聊天中自动沉淀，回答时会参考</span></div>
          <div class="notes-add">
            <el-input v-model="newNote" size="small" placeholder="手动记一条，如：右膝有旧伤" @keydown.enter="addNote" />
            <el-button size="small" round type="primary" plain :loading="addingNote" @click="addNote">记下</el-button>
          </div>
          <div v-if="!notes.length" class="notes-empty">
            还没有记忆。聊天时告诉教练你的伤病史、偏好或生活节奏，它会自己记住。
          </div>
          <div v-for="n in notes" :key="n.id" class="note-row">
            <el-tag size="small" effect="plain" :type="n.category === 'injury' ? 'danger' : 'info'">
              {{ CAT_CN[n.category] || '其他' }}
            </el-tag>
            <span class="note-content">{{ n.content }}</span>
            <el-button link size="small" type="danger" @click="removeNote(n)">删</el-button>
          </div>
        </div>
      </el-popover>
    </div>

    <div class="chat-box" ref="chatBox" @scroll="onChatScroll" @wheel.passive="onChatWheel">
      <div v-if="!messages.length" class="empty-hints">
        <div class="hint-title">试试问教练：</div>
        <el-button v-for="q in quickPrompts" :key="q" round plain size="large" class="hint-btn"
                   @click="send(q)">{{ q }}</el-button>
      </div>

      <div v-for="(m, mi) in messages" :key="mi" class="msg-row msg-in" :class="m.role">
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
            <span style="white-space:pre-wrap" v-html="m.streaming ? renderStreaming(m) : renderRich(m.content)"></span>
            <span v-if="m.streaming && !m.content" class="thinking">正在结合你的数据思考<span class="dot"/><span class="dot"/><span class="dot"/></span>
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

    <transition name="jump-fade">
      <button v-if="showJumpDown" class="jump-down" aria-label="回到底部" @click="jumpToBottom">↓</button>
    </transition>

    <div class="input-bar">
      <el-input v-model="input" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }"
                placeholder="问教练：这周练什么 / 状态不好怎么办 / 想换个目标…"
                :disabled="streaming" @keydown.enter.exact="onEnterKey" />
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
// 教练长期记忆：聊天中自动沉淀 + 手动增删，注入每轮 system prompt
const notes = ref([])
const notesVisible = ref(false)
const newNote = ref('')
const addingNote = ref(false)
const CAT_CN = { injury: '伤病', life: '生活', preference: '偏好', gear: '装备', motive: '动机', other: '其他' }
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

// 流式字符晕染（CDS grapheme-reveal）：内容分"已定格"+"最新 delta 块"两段，
// 只有最新块播模糊→清晰入场，已渲染的字绝不重播动画。
// 代价：markdown 标记恰好跨 delta 边界时，字面 ** 会闪现一个 delta 周期（几十 ms），可接受
function renderStreaming(m) {
  const revealLen = m.reveal ? m.reveal.length : 0
  const settled = m.content.slice(0, m.content.length - revealLen)
  return renderRich(settled) + '<span class="stream-reveal">' + renderRich(m.reveal || '') + '</span>'
}

onMounted(() => { loadStatus(); loadConversations({ autoResume: true }); loadNotes() })
async function loadStatus() { try { status.value = await api.get('/ai/status') } catch { status.value = { reachable: false } } }

async function loadNotes() {
  try {
    const data = await api.get('/ai/notes')
    notes.value = data.notes || []
  } catch { /* 记忆拉取失败不阻塞聊天 */ }
}

async function addNote() {
  const content = newNote.value.trim()
  if (content.length < 2) return
  addingNote.value = true
  try {
    await api.post('/ai/notes', { content })
    newNote.value = ''
    loadNotes()
  } catch (e) {
    ElMessage.error(e.message || '记录失败')
  } finally { addingNote.value = false }
}

async function removeNote(n) {
  try {
    await api.del(`/ai/notes/${n.id}`)
    notes.value = notes.value.filter(x => x.id !== n.id)
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

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
    userPinnedBottom.value = true
    scrollBottom({ force: true })
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

// ===== 滚动手感（移植自 Claude Design System 逆向实现）=====
// 智能跟随：流式输出时只在与底"吸底"状态下自动滚动；
// 用户上滑（滚轮向上）立刻松手绝不拽回，重新滚近底部 48px 内自动吸回。
const userPinnedBottom = ref(true)
const showJumpDown = ref(false)
const PINNED_DISTANCE = 48

// 滚动位置传感器：触顶/触底时对应边缘恢复完全清晰，滚入内容时淡入 28px 羽化
function updateFade(el) {
  const atTop = el.scrollTop <= 2
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 2
  const size = '28px'
  el.style.setProperty('--cds-fade-top', atTop ? '0px' : size)
  el.style.setProperty('--cds-fade-bottom', atBottom ? '0px' : size)
}
function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < PINNED_DISTANCE
}
function onChatScroll() {
  const el = chatBox.value
  if (!el) return
  userPinnedBottom.value = nearBottom(el)
  showJumpDown.value = !userPinnedBottom.value
  updateFade(el)
}
function onChatWheel(e) {
  // 向上滚动意图 → 立即解除吸底，流式输出不再拽人
  if (e.deltaY < 0) userPinnedBottom.value = false
}
function jumpToBottom() {
  const el = chatBox.value
  if (!el) return
  userPinnedBottom.value = true
  el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
}
function scrollBottom({ force = false } = {}) {
  nextTick(() => {
    const el = chatBox.value
    if (!el) return
    updateFade(el)
    if (!force && !userPinnedBottom.value) return
    el.scrollTop = el.scrollHeight
    updateFade(el)
  })
}

// CJK 输入法守卫（Claude 官方断言）：合成中或 keyCode 229 的回车是选字，绝不发送
function onEnterKey(e) {
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  send()
}

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
  userPinnedBottom.value = true   // 刚发出的消息必须可见
  scrollBottom({ force: true })

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
          reply.reveal = evt.text          // 供流式晕染定位最新一块
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
    ElMessage.error(e.message || '应用失败：校验未通过（课表可能已变化）')
  } finally { p.applying = false }
}
</script>

<style scoped>
/* ===== CDS 动效令牌（青绿暗色适配版，令牌规则同 styles.css：先登记后引用）===== */
.coach-page {
  --cds-ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);   /* Claude 主缓动曲线 */
  --cds-fade-size: 28px;                                /* 滚动渐隐羽化距离（官方常量） */
  --cds-fade-top: 0px;                                  /* 由滚动传感器动态写入 */
  --cds-fade-bottom: 0px;
}

.coach-page { display: flex; flex-direction: column; height: calc(100vh - 44px); }

/* 7 档非线性滚动渐隐（y≈x^1.5 幂律曲线，移植自 ScrollFadeContainer） */
.chat-box {
  flex: 1; overflow-y: auto; padding: 18px 4px; display: flex; flex-direction: column; gap: 16px;
  mask-image: linear-gradient(to bottom,
    transparent,
    rgba(0, 0, 0, 0.05) calc(var(--cds-fade-top) * 0.15),
    rgba(0, 0, 0, 0.15) calc(var(--cds-fade-top) * 0.30),
    rgba(0, 0, 0, 0.30) calc(var(--cds-fade-top) * 0.45),
    rgba(0, 0, 0, 0.50) calc(var(--cds-fade-top) * 0.60),
    rgba(0, 0, 0, 0.70) calc(var(--cds-fade-top) * 0.75),
    rgba(0, 0, 0, 0.85) calc(var(--cds-fade-top) * 0.87),
    #000 var(--cds-fade-top),
    #000 calc(100% - var(--cds-fade-bottom)),
    rgba(0, 0, 0, 0.85) calc(100% - var(--cds-fade-bottom) * 0.87),
    rgba(0, 0, 0, 0.70) calc(100% - var(--cds-fade-bottom) * 0.75),
    rgba(0, 0, 0, 0.50) calc(100% - var(--cds-fade-bottom) * 0.60),
    rgba(0, 0, 0, 0.30) calc(100% - var(--cds-fade-bottom) * 0.45),
    rgba(0, 0, 0, 0.15) calc(100% - var(--cds-fade-bottom) * 0.30),
    rgba(0, 0, 0, 0.05) calc(100% - var(--cds-fade-bottom) * 0.15),
    transparent);
  -webkit-mask-image: linear-gradient(to bottom,
    transparent,
    rgba(0, 0, 0, 0.05) calc(var(--cds-fade-top) * 0.15),
    rgba(0, 0, 0, 0.15) calc(var(--cds-fade-top) * 0.30),
    rgba(0, 0, 0, 0.30) calc(var(--cds-fade-top) * 0.45),
    rgba(0, 0, 0, 0.50) calc(var(--cds-fade-top) * 0.60),
    rgba(0, 0, 0, 0.70) calc(var(--cds-fade-top) * 0.75),
    rgba(0, 0, 0, 0.85) calc(var(--cds-fade-top) * 0.87),
    #000 var(--cds-fade-top),
    #000 calc(100% - var(--cds-fade-bottom)),
    rgba(0, 0, 0, 0.85) calc(100% - var(--cds-fade-bottom) * 0.87),
    rgba(0, 0, 0, 0.70) calc(100% - var(--cds-fade-bottom) * 0.75),
    rgba(0, 0, 0, 0.50) calc(100% - var(--cds-fade-bottom) * 0.60),
    rgba(0, 0, 0, 0.30) calc(100% - var(--cds-fade-bottom) * 0.45),
    rgba(0, 0, 0, 0.15) calc(100% - var(--cds-fade-bottom) * 0.30),
    rgba(0, 0, 0, 0.05) calc(100% - var(--cds-fade-bottom) * 0.15),
    transparent);
}

.empty-hints { margin: auto; text-align: center; display: flex; flex-direction: column; gap: 12px; align-items: center; }
.hint-title { color: var(--text-3); font-size: 13px; }
.hint-btn { color: var(--text-2); }
.hint-btn:hover { color: var(--jade); border-color: rgba(63, 208, 164, 0.45); }

.conv-bar { display: flex; gap: 8px; align-items: center; padding: 10px 0 0; }
.conv-select { width: 260px; }

.notes-pop { display: flex; flex-direction: column; gap: 10px; max-height: 380px; overflow-y: auto; }
.notes-head { font-weight: 700; font-size: 13.5px; }
.notes-sub { font-weight: 400; font-size: 11.5px; color: var(--text-3); margin-left: 6px; }
.notes-add { display: flex; gap: 8px; }
.notes-empty { font-size: 12.5px; color: var(--text-3); line-height: 1.7; }
.note-row { display: flex; align-items: center; gap: 8px; }
.note-content { flex: 1; font-size: 13px; line-height: 1.5; }

/* 消息入场：12px 上浮 + 指数缓出（Claude message-fade-up 弹簧手感） */
.msg-in { animation: msg-in 0.35s var(--cds-ease-out-expo) both; }
@keyframes msg-in {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}

.msg-row { display: flex; gap: 10px; }
.msg-row.user { justify-content: flex-end; }
.msg-avatar { background: linear-gradient(135deg, var(--jade), #2a9a78); color: #081210; font-weight: 800; font-size: 12px; flex-shrink: 0; }
.msg-body { max-width: 76%; display: flex; flex-direction: column; gap: 8px; }
.msg-row.user .msg-body { align-items: flex-end; }

/* 气泡：暗色双环质感——内侧 1px 微高光 + 细边框（CDS 暗色 ring 架构的青绿版） */
.bubble { border-radius: 14px; padding: 10px 14px; font-size: 14px; line-height: 1.7; }
.bubble.user {
  background: linear-gradient(135deg, rgba(63, 208, 164, 0.16), rgba(63, 208, 164, 0.07));
  border: 1px solid rgba(63, 208, 164, 0.25);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
}
.bubble.ai { background: var(--bg-inset); border: 1px solid var(--border); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03); }
.bubble.ai code { background: rgba(255, 255, 255, 0.08); border-radius: 4px; padding: 1px 5px; font-size: 13px; }
.bubble.ai.error { border-color: rgba(224, 95, 95, 0.45); color: #ea8a8a; background: rgba(224, 95, 95, 0.06); }
.thinking { color: var(--text-3); display: inline-flex; align-items: center; gap: 5px; }

/* 流式字符晕染：6px 模糊 + 淡入，0.45s 指数缓出（Claude 吐字质感的核心） */
.stream-reveal { animation: grapheme-reveal 0.45s var(--cds-ease-out-expo) both; }
@keyframes grapheme-reveal {
  from { opacity: 0; filter: blur(6px); }
  to   { opacity: 1; filter: blur(0); }
}
@media (prefers-reduced-motion: reduce) {
  .stream-reveal { animation: none; }
}
/* 思考中三点波浪（timeline-quiet-dot：错相位 1.4s 循环） */
.thinking .dot { width: 4px; height: 4px; border-radius: 50%; background: var(--text-3); animation: quiet-dot 1.4s ease-in-out infinite; }
.thinking .dot:nth-child(2) { animation-delay: 0.18s; }
.thinking .dot:nth-child(3) { animation-delay: 0.36s; }
@keyframes quiet-dot {
  0%, 8%   { opacity: 0.2; transform: translateY(2px); }
  22%      { opacity: 1; transform: translateY(-2.5px); }
  36%, 70% { opacity: 1; transform: translateY(0); }
  90%, 100%{ opacity: 0.2; transform: translateY(0); }
}
.cursor { animation: blink 1s steps(2) infinite; color: var(--jade); }
@keyframes blink { 50% { opacity: 0; } }

.tool-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.tool-chip { font-size: 11px; }

.proposal { border: 1px solid rgba(63, 208, 164, 0.35); background: rgba(63, 208, 164, 0.05); border-radius: 12px; padding: 12px 14px; }
.p-title { font-weight: 700; font-size: 13.5px; margin-bottom: 6px; }
.p-line { font-size: 12.5px; color: var(--text-2); margin: 3px 0; }
.p-warn { font-size: 12px; color: var(--orange); margin: 3px 0; }
.p-actions { margin-top: 10px; display: flex; align-items: center; gap: 10px; }

/* 输入终端：Claude composer 三态双环投影（悬停加深 / 聚焦上浮），220ms expo 缓动 */
.input-bar { position: relative; display: flex; gap: 10px; align-items: flex-end; padding: 12px 0 4px; border-top: 1px solid var(--border); }
.input-bar :deep(.el-textarea__inner) {
  border-radius: 16px;
  transition: box-shadow 220ms var(--cds-ease-out-expo), border-color 220ms ease;
}
.input-bar :deep(.el-textarea__inner:hover) {
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.25), inset 0 0 0 1px rgba(63, 208, 164, 0.35);
}
.input-bar :deep(.el-textarea__inner:focus) {
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35), inset 0 0 0 1px rgba(63, 208, 164, 0.55);
  border-color: rgba(63, 208, 164, 0.55);
}

/* 回到底部悬浮钮：不入场不弹跳，淡入即可 */
.jump-down {
  position: absolute; right: 26px; bottom: 96px; z-index: 5;
  width: 34px; height: 34px; border-radius: 50%;
  border: 1px solid var(--border); background: var(--bg-card-solid); color: var(--text-2);
  font-size: 15px; cursor: pointer; box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
  transition: transform 120ms ease, color 120ms ease, border-color 120ms ease;
}
.jump-down:hover { color: var(--jade); border-color: rgba(63, 208, 164, 0.45); transform: translateY(-1px); }
.jump-down:active { transform: translateY(0) scale(0.96); }
.jump-fade-enter-active, .jump-fade-leave-active { transition: opacity 150ms ease; }
.jump-fade-enter-from, .jump-fade-leave-to { opacity: 0; }

code { background: rgba(157, 184, 173, 0.12); padding: 1px 6px; border-radius: 5px; font-size: 12px; }
</style>
