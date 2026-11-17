<template>
  <div class="search-page">
    <el-row :gutter="24">
      <el-col :span="24">
        <el-card class="search-card">
          <h2>知识检索</h2>
          <el-input
            v-model="query"
            placeholder="输入您的问题..."
            size="large"
            clearable
            @keyup.enter="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
            <template #append>
              <el-button type="primary" @click="handleSearch" :loading="loading">
                搜索
              </el-button>
            </template>
          </el-input>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="24" style="margin-top: 24px;" v-if="result">
      <el-col :span="24">
        <el-card class="answer-card">
          <template #header>
            <div class="answer-header">
              <span>AI 回答</span>
              <el-tag :type="getStatusType(result.status)">
                {{ getStatusLabel(result.status) }}
              </el-tag>
            </div>
          </template>
          <div class="answer-content">{{ result.answer || '暂无回答内容' }}</div>
          <div class="answer-meta">
            <span>状态：{{ getStatusLabel(result.status) }}</span>
            <span>置信度：{{ formatConfidence(result.confidence) }}</span>
          </div>
          <el-alert
            v-if="result.warnings?.length"
            class="answer-warnings"
            title="可信度提示"
            type="warning"
            :closable="false"
          >
            <ul>
              <li v-for="(warning, index) in result.warnings" :key="`${warning}-${index}`">
                {{ warning }}
              </li>
            </ul>
          </el-alert>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="24" style="margin-top: 24px;" v-if="result?.sources?.length">
      <el-col :span="24">
        <el-card>
          <template #header>
            <div class="sources-header">
              <span>答案来源</span>
              <el-tag>{{ result.sources.length }} 个相关文档</el-tag>
            </div>
          </template>
          <el-collapse>
            <el-collapse-item
              v-for="(source, index) in result.sources"
              :key="source.doc_id"
              :name="index"
            >
              <template #title>
                <div class="source-title">
                  <el-icon><Document /></el-icon>
                  <span class="source-name">{{ source.title || '文档 ' + (index + 1) }}</span>
                  <el-tag size="small" :type="getConfidenceType(source.confidence_level)">
                    {{ source.confidence_level }}
                  </el-tag>
                  <span class="rrf-score">RRF: {{ source.rrf_score.toFixed(4) }}</span>
                </div>
              </template>
              <div class="source-content">
                <p>{{ source.content_snippet }}</p>
                <div class="source-meta">
                  <el-icon><Folder /></el-icon>
                  <span>来源: {{ source.source }}</span>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="24" style="margin-top: 24px;" v-if="rawResults?.length">
      <el-col :span="24">
        <el-card>
          <template #header>
            <span>检索详情 (调试)</span>
          </template>
          <div class="table-scroll">
            <el-table :data="rawResults" size="small">
              <el-table-column prop="title" label="标题" width="200" />
              <el-table-column prop="rrf_score" label="RRF分数" width="100">
                <template #default="{ row }">
                  {{ row.rrf_score.toFixed(4) }}
                </template>
              </el-table-column>
              <el-table-column prop="bm25_rank" label="BM25排名" width="100" />
              <el-table-column prop="vector_rank" label="向量排名" width="100" />
              <el-table-column prop="confidence_level" label="置信度" width="120">
                <template #default="{ row }">
                  <el-tag :type="getConfidenceType(row.confidence_level)" size="small">
                    {{ row.confidence_level }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="content" label="内容片段">
                <template #default="{ row }">
                  {{ row.content.substring(0, 100) }}...
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { ElMessage } from 'element-plus'

const route = useRoute()
const query = ref('')
const loading = ref(false)
const result = ref(null)
const rawResults = ref([])

const getConfidenceType = (level) => {
  const types = {
    '非常确定': 'success',
    '基本确定': 'warning',
    '仅供参考': 'info',
  }
  return types[level] || 'info'
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

const handleSearch = async () => {
  if (!query.value.trim()) {
    ElMessage.warning('请输入搜索内容')
    return
  }

  loading.value = true
  try {
    // Get AI answer with sources
    const searchResult = await api.post('/kb/search', {
      query: query.value,
      top_k: 10,
    })
    result.value = searchResult

    // Get raw search results for debugging
    const rawResult = await api.post('/kb/search/raw', {
      query: query.value,
      top_k: 10,
    })
    rawResults.value = rawResult.results || []

  } catch (e) {
    ElMessage.error('搜索失败: ' + (e.message || '未知错误'))
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (route.query.q) {
    query.value = route.query.q
    handleSearch()
  }
})
</script>

<style scoped>
.search-page {
  max-width: 1000px;
  margin: 0 auto;
  width: 100%;
  min-width: 0;
}

.search-card {
  padding: 20px;
}

.search-card h2 {
  margin-bottom: 16px;
  color: #303133;
}

.answer-card {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.answer-card :deep(.el-card__header) {
  border-bottom: 1px solid rgba(255, 255, 255, 0.2);
}

.answer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.answer-content {
  line-height: 1.8;
  font-size: 16px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.answer-meta {
  display: flex;
  gap: 16px;
  margin-top: 16px;
  font-size: 13px;
  flex-wrap: wrap;
}

.answer-warnings {
  margin-top: 16px;
}

.answer-warnings ul {
  margin: 0;
  padding-left: 18px;
}

.sources-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.source-title {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
  flex-wrap: wrap;
}

.source-name {
  min-width: 0;
  overflow-wrap: anywhere;
}

.rrf-score {
  margin-left: auto;
  color: #909399;
  font-size: 12px;
}

.source-content {
  padding: 12px;
  background: #f5f7fa;
  border-radius: 4px;
}

.source-meta {
  margin-top: 8px;
  color: #909399;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.table-scroll {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
}

.table-scroll :deep(.el-table) {
  min-width: 720px;
}

@media (max-width: 767px) {
  .search-card {
    padding: 16px;
  }

  .answer-meta {
    gap: 8px 16px;
  }

  .source-title {
    align-items: flex-start;
  }

  .source-name {
    flex: 1 1 100%;
  }

  .rrf-score {
    margin-left: 0;
  }
}
</style>
