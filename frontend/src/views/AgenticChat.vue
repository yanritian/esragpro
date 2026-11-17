<template>
  <div class="agentic-chat">
    <el-row :gutter="24">
      <!-- 左侧：策略信息 -->
      <el-col :span="6">
        <el-card>
          <template #header>
            <span>Agentic RAG</span>
          </template>

          <el-alert
            title="智能路由"
            type="info"
            :closable="false"
            show-icon
          >
            <template #default>
              系统自动分析问题类型，选择最优检索策略
            </template>
          </el-alert>

          <el-divider>策略说明</el-divider>

          <div class="strategy-list">
            <div class="strategy-item">
              <el-tag type="success">BM25</el-tag>
              <span>简单事实查询</span>
            </div>
            <div class="strategy-item">
              <el-tag type="primary">混合检索</el-tag>
              <span>复杂问题</span>
            </div>
            <div class="strategy-item">
              <el-tag type="warning">图谱实体</el-tag>
              <span>实体查询</span>
            </div>
            <div class="strategy-item">
              <el-tag type="danger">图谱多跳</el-tag>
              <span>关系推理</span>
            </div>
          </div>
        </el-card>

        <!-- 历史查询 -->
        <el-card style="margin-top: 16px;">
          <template #header>
            <span>历史查询</span>
          </template>
          <div v-if="history.length === 0" class="empty-history">
            暂无查询记录
          </div>
          <div v-for="(item, index) in history" :key="index" class="history-item">
            <button
              type="button"
              class="history-query"
              :aria-label="`使用历史查询：${item.query}`"
              @click="useHistoryQuery(item.query)"
            >
              {{ item.query }}
            </button>
            <el-tag size="small" :type="getStrategyType(item.strategy)">
              {{ item.strategy }}
            </el-tag>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧：聊天界面 -->
      <el-col :span="18">
        <el-card>
          <!-- 消息列表 -->
          <div
            class="messages"
            ref="messagesContainer"
            role="log"
            aria-live="polite"
            aria-label="对话消息"
          >
            <div v-for="(msg, index) in messages" :key="index"
                 :class="['message', msg.role === 'user' ? 'user-message' : 'bot-message']">
              <div class="message-content">
                <div class="query" v-if="msg.role === 'user'">{{ msg.content }}</div>
                <div v-else>
                  <div class="answer">{{ msg.content }}</div>
                  <div class="metadata" v-if="msg.metadata || msg.status || msg.confidence !== undefined">
                    <el-tag v-if="msg.metadata?.strategy" size="small">{{ msg.metadata.strategy }}</el-tag>
                    <el-tag v-if="msg.metadata?.response_time_ms !== undefined" size="small" type="info">
                      {{ msg.metadata.response_time_ms }}ms
                    </el-tag>
                    <el-tag v-if="msg.status" size="small" :type="getStatusType(msg.status)">
                      {{ getStatusLabel(msg.status) }}
                    </el-tag>
                    <el-tag v-if="msg.confidence !== undefined" size="small" type="success">
                      {{ formatConfidence(msg.confidence) }} 置信度
                    </el-tag>
                  </div>
                  <div v-if="msg.warnings?.length" class="message-warnings" role="status">
                    <span class="warning-title">可信度提示</span>
                    <ul>
                      <li v-for="(warning, warningIndex) in msg.warnings" :key="`${warning}-${warningIndex}`">
                        {{ warning }}
                      </li>
                    </ul>
                  </div>
                  <div class="sources" v-if="msg.sources && msg.sources.length > 0">
                    <el-divider>来源</el-divider>
                    <div v-for="(source, i) in msg.sources" :key="i" class="source-item">
                      <span class="source-title">{{ source.title }}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div v-if="loading" class="message bot-message">
              <div class="message-content">
                <el-icon class="is-loading"><Loading /></el-icon>
                <span style="margin-left: 8px;">思考中...</span>
              </div>
            </div>
          </div>

          <!-- 输入框 -->
          <div class="input-area">
            <el-input
              v-model="query"
              placeholder="输入你的问题..."
              aria-label="输入你的问题"
              @keyup.enter="sendMessage"
              :disabled="loading"
              size="large"
            >
              <template #append>
                <el-button
                  type="primary"
                  aria-label="发送消息"
                  title="发送消息"
                  @click="sendMessage"
                  :disabled="loading || !query.trim()"
                >
                  <el-icon><Promotion /></el-icon>
                </el-button>
              </template>
            </el-input>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const query = ref('')
const messages = ref([])
const history = ref([])
const loading = ref(false)
const messagesContainer = ref(null)

const sendMessage = async () => {
  if (!query.value.trim() || loading.value) return

  const userQuery = query.value.trim()
  messages.value.push({ role: 'user', content: userQuery })
  loading.value = true
  query.value = ''

  try {
    const result = await api.post('/agentic/ask', { query: userQuery })

    messages.value.push({
      role: 'bot',
      content: result.answer,
      sources: result.sources,
      metadata: result.metadata,
      confidence: result.confidence,
      status: result.status,
      warnings: result.warnings,
    })

    // 添加到历史
    history.value.unshift({
      query: userQuery,
      strategy: result.metadata?.strategy || 'unknown',
    })
    history.value = history.value.slice(0, 10)

  } catch (e) {
    ElMessage.error('查询失败: ' + e.message)
    messages.value.push({
      role: 'bot',
      content: '查询失败，请稍后重试。',
      confidence: 0,
      status: 'error',
      warnings: ['本次请求未完成，未生成可验证的回答。'],
    })
  } finally {
    loading.value = false
    scrollToBottom()
  }
}

const useHistoryQuery = (q) => {
  query.value = q
}

const getStrategyType = (strategy) => {
  const types = {
    bm25_only: 'success',
    hybrid: 'primary',
    graph_entity: 'warning',
    graph_multihop: 'danger',
    graph_rag: 'danger',
    agentic_rag: 'info',
  }
  return types[strategy] || ''
}

const statusLabels = {
  reliable: '可靠回答',
  partial: '部分可靠',
  uncertain: '不确定',
  refused: '拒绝回答',
  error: '处理失败',
}

const getStatusLabel = (status) => statusLabels[status] || '状态未知'

const getStatusType = (status) => {
  const types = {
    reliable: 'success',
    partial: 'warning',
    uncertain: 'warning',
    refused: 'info',
    error: 'danger',
  }
  return types[status] || 'info'
}

const formatConfidence = (confidence) => {
  const value = Number(confidence)
  if (!Number.isFinite(value)) return '不可用'

  const boundedValue = Math.min(Math.max(value, 0), 1)
  return `${Math.round(boundedValue * 100)}%`
}

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

onMounted(() => {
  // 预设欢迎消息
  messages.value.push({
    role: 'bot',
    content: '你好！我是 Agentic RAG 助手。我会自动分析你的问题类型，选择最优的检索策略来回答。',
  })
})
</script>

<style scoped>
.agentic-chat {
  padding: 20px;
  width: 100%;
  min-width: 0;
}

.strategy-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.strategy-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.strategy-item span {
  color: #606266;
  font-size: 14px;
}

.messages {
  height: 500px;
  overflow-y: auto;
  padding: 20px;
  min-width: 0;
}

.message {
  margin-bottom: 16px;
}

.user-message {
  display: flex;
  justify-content: flex-end;
}

.user-message .message-content {
  background: #e8f4ff;
  padding: 12px 16px;
  border-radius: 12px;
  max-width: 70%;
  min-width: 0;
  overflow-wrap: anywhere;
}

.bot-message .message-content {
  background: #f5f5f5;
  padding: 12px 16px;
  border-radius: 12px;
  max-width: 80%;
  min-width: 0;
  overflow-wrap: anywhere;
}

.answer {
  white-space: pre-wrap;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.metadata {
  margin-top: 8px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.sources {
  margin-top: 12px;
}

.source-item {
  margin-bottom: 4px;
}

.input-area {
  padding: 16px 0 0;
  min-width: 0;
}

.empty-history {
  color: #909399;
  text-align: center;
  padding: 20px;
}

.history-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 8px 0;
  border-bottom: 1px solid #ebeef5;
}

.history-query {
  border: 0;
  padding: 0;
  background: transparent;
  color: #409eff;
  cursor: pointer;
  font: inherit;
  font-size: 14px;
  text-align: left;
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
}

.history-query:hover {
  text-decoration: underline;
}

.history-query:focus-visible {
  outline: 2px solid #409eff;
  outline-offset: 2px;
}

.message-warnings {
  margin-top: 12px;
  color: #8a5a00;
  font-size: 13px;
}

.warning-title {
  font-weight: 600;
}

.message-warnings ul {
  margin: 4px 0 0;
  padding-left: 18px;
}

.source-title {
  color: #409eff;
  overflow-wrap: anywhere;
}

@media (max-width: 767px) {
  .agentic-chat {
    padding: 0;
  }

  .messages {
    height: min(500px, 60vh);
    padding: 16px 0;
  }

  .user-message .message-content,
  .bot-message .message-content {
    max-width: 100%;
  }

  .metadata {
    gap: 6px;
  }
}
</style>
