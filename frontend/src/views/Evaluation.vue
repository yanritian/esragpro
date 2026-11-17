<template>
  <div class="evaluation">
    <el-row :gutter="24">
      <!-- 左侧：控制面板 -->
      <el-col :span="6">
        <el-card>
          <template #header>
            <span>幻觉评估</span>
          </template>

          <el-form label-position="top">
            <el-form-item label="测试分类">
              <el-checkbox-group v-model="selectedCategories">
                <el-checkbox label="legal">法律条文</el-checkbox>
                <el-checkbox label="policy">公司制度</el-checkbox>
                <el-checkbox label="factual">事实查询</el-checkbox>
                <el-checkbox label="reasoning">推理问题</el-checkbox>
                <el-checkbox label="out_of_scope">超出范围</el-checkbox>
                <el-checkbox label="multi_hop">多跳问题</el-checkbox>
              </el-checkbox-group>
            </el-form-item>

            <el-form-item label="使用图谱增强">
              <el-switch v-model="useGraph" />
            </el-form-item>

            <el-form-item>
              <el-button
                type="primary"
                :loading="running"
                @click="runEvaluation"
                style="width: 100%"
              >
                开始评估
              </el-button>
            </el-form-item>
          </el-form>

          <el-divider>测试用例</el-divider>

          <div class="test-case-count">
            <span>预设用例：</span>
            <el-tag>{{ testCases.length }} 个</el-tag>
          </div>

          <div class="table-scroll evaluation-table">
            <el-table
              :data="testCases"
              size="small"
              max-height="300"
              style="margin-top: 12px"
            >
              <el-table-column prop="id" label="ID" width="80" />
              <el-table-column prop="category" label="分类" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="getCategoryType(row.category)">
                    {{ getCategoryLabel(row.category) }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <ul class="evaluation-mobile-list test-cases-mobile">
            <li
              v-for="row in testCases"
              :key="row.id"
              class="evaluation-list-item"
            >
              <div class="evaluation-field">
                <span class="evaluation-field-label">ID</span>
                <span class="evaluation-field-value">{{ row.id }}</span>
              </div>
              <div class="evaluation-field">
                <span class="evaluation-field-label">分类</span>
                <span class="evaluation-field-value">
                  <el-tag size="small" :type="getCategoryType(row.category)">
                    {{ getCategoryLabel(row.category) }}
                  </el-tag>
                </span>
              </div>
            </li>
          </ul>
        </el-card>
      </el-col>

      <!-- 右侧：评估结果 -->
      <el-col :span="18">
        <!-- 汇总卡片 -->
        <el-row :gutter="16" v-if="report">
          <el-col :span="6">
            <el-card shadow="hover" class="metric-card">
              <div class="metric-value" :class="getMetricClass(report.summary.accuracy)">
                {{ report.summary.accuracy }}%
              </div>
              <div class="metric-label">准确率</div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover" class="metric-card">
              <div class="metric-value" :class="getHallucinationClass(report.summary.hallucination_rate)">
                {{ report.summary.hallucination_rate }}%
              </div>
              <div class="metric-label">幻觉率</div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover" class="metric-card">
              <div class="metric-value">{{ report.summary.avg_confidence }}</div>
              <div class="metric-label">平均置信度</div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover" class="metric-card">
              <div class="metric-value">{{ report.summary.avg_response_time_ms }}ms</div>
              <div class="metric-label">平均响应时间</div>
            </el-card>
          </el-col>
        </el-row>

        <!-- 评估状态 -->
        <el-card v-if="running" style="margin-top: 16px;">
          <div style="text-align: center; padding: 40px;">
            <el-icon class="is-loading" :size="40" color="#409eff">
              <Loading />
            </el-icon>
            <p style="margin-top: 16px; color: #909399;">正在评估中，请稍候...</p>
          </div>
        </el-card>

        <!-- 无数据提示 -->
        <el-card v-if="!report && !running" style="margin-top: 16px;">
          <el-empty :description="'请点击「开始评估」运行测试'">
            <el-button type="primary" @click="runEvaluation">开始评估</el-button>
          </el-empty>
        </el-card>

        <!-- 分类统计 -->
        <el-card v-if="report && report.by_category" style="margin-top: 16px;">
          <template #header>
            <span>分类统计</span>
          </template>
          <div class="table-scroll evaluation-table">
            <el-table :data="categoryData" border>
              <el-table-column prop="category" label="分类" width="120">
                <template #default="{ row }">
                  {{ getCategoryLabel(row.category) }}
                </template>
              </el-table-column>
              <el-table-column prop="total" label="总数" width="80" />
              <el-table-column prop="correct" label="正确" width="80" />
              <el-table-column prop="accuracy" label="准确率" width="100">
                <template #default="{ row }">
                  <el-progress
                    :percentage="row.accuracy"
                    :status="row.accuracy >= 80 ? 'success' : row.accuracy >= 60 ? '' : 'exception'"
                  />
                </template>
              </el-table-column>
              <el-table-column prop="hallucination" label="幻觉数" width="80" />
              <el-table-column prop="hallucination_rate" label="幻觉率" width="100">
                <template #default="{ row }">
                  <el-tag :type="row.hallucination_rate <= 5 ? 'success' : row.hallucination_rate <= 10 ? 'warning' : 'danger'">
                    {{ row.hallucination_rate }}%
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="avg_confidence" label="平均置信度" width="100" />
              <el-table-column prop="avg_response_time" label="响应时间(ms)" width="100" />
            </el-table>
          </div>

          <ul class="evaluation-mobile-list category-data-mobile">
            <li
              v-for="row in categoryData"
              :key="row.category"
              class="evaluation-list-item"
            >
              <div class="evaluation-field evaluation-list-heading">
                <span class="evaluation-field-label">分类</span>
                <span class="evaluation-field-value">{{ getCategoryLabel(row.category) }}</span>
              </div>
              <div class="evaluation-metric-grid">
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">总数</span>
                  <span class="evaluation-metric-value">{{ row.total }}</span>
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">正确</span>
                  <span class="evaluation-metric-value">{{ row.correct }}</span>
                </div>
                <div class="evaluation-metric evaluation-metric-wide">
                  <span class="evaluation-metric-label">准确率</span>
                  <el-progress
                    class="evaluation-progress"
                    :percentage="row.accuracy"
                    :status="row.accuracy >= 80 ? 'success' : row.accuracy >= 60 ? '' : 'exception'"
                  />
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">幻觉数</span>
                  <span class="evaluation-metric-value">{{ row.hallucination }}</span>
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">幻觉率</span>
                  <span class="evaluation-metric-value">
                    <el-tag :type="row.hallucination_rate <= 5 ? 'success' : row.hallucination_rate <= 10 ? 'warning' : 'danger'">
                      {{ row.hallucination_rate }}%
                    </el-tag>
                  </span>
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">平均置信度</span>
                  <span class="evaluation-metric-value">{{ row.avg_confidence }}</span>
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">响应时间(ms)</span>
                  <span class="evaluation-metric-value">{{ row.avg_response_time }}</span>
                </div>
              </div>
            </li>
          </ul>
        </el-card>

        <!-- 幻觉类型分布 -->
        <el-card v-if="report && report.hallucination_types" style="margin-top: 16px;">
          <template #header>
            <span>幻觉类型分布</span>
          </template>
          <div v-if="Object.keys(report.hallucination_types).length > 0">
            <div
              v-for="(count, type) in report.hallucination_types"
              :key="type"
              class="hallucination-type"
            >
              <span class="type-name">{{ getHallucinationTypeLabel(type) }}</span>
              <el-tag type="danger">{{ count }} 次</el-tag>
            </div>
          </div>
          <el-empty v-else description="无幻觉检测" :image-size="60" />
        </el-card>

        <!-- 详细结果 -->
        <el-card v-if="report && report.details" style="margin-top: 16px;">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span>详细结果</span>
              <el-tag>共 {{ report.details.length }} 条</el-tag>
            </div>
          </template>
          <div class="table-scroll evaluation-table">
            <el-table :data="report.details" border max-height="400">
              <el-table-column prop="test_id" label="ID" width="100" />
              <el-table-column prop="question" label="问题" show-overflow-tooltip />
              <el-table-column prop="status" label="状态" width="100">
                <template #default="{ row }">
                  <el-tag :type="getStatusType(row.status)">
                    {{ getStatusLabel(row.status) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="is_correct" label="正确" width="70">
                <template #default="{ row }">
                  <el-icon v-if="row.is_correct" color="#67c23a"><Select /></el-icon>
                  <el-icon v-else color="#f56c6c"><CloseBold /></el-icon>
                </template>
              </el-table-column>
              <el-table-column prop="has_hallucination" label="幻觉" width="70">
                <template #default="{ row }">
                  <el-tag v-if="row.has_hallucination" type="danger" size="small">有</el-tag>
                  <el-tag v-else type="success" size="small">无</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="confidence" label="置信度" width="80">
                <template #default="{ row }">
                  {{ row.confidence }}
                </template>
              </el-table-column>
            </el-table>
          </div>

          <ul class="evaluation-mobile-list report-details-mobile">
            <li
              v-for="row in report.details"
              :key="row.test_id"
              class="evaluation-list-item"
            >
              <div class="evaluation-field">
                <span class="evaluation-field-label">ID</span>
                <span class="evaluation-field-value">{{ row.test_id }}</span>
              </div>
              <div class="evaluation-field evaluation-detail-question">
                <span class="evaluation-field-label">问题</span>
                <span class="evaluation-field-value">{{ row.question || '-' }}</span>
              </div>
              <div class="evaluation-metric-grid">
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">状态</span>
                  <span class="evaluation-metric-value">
                    <el-tag :type="getStatusType(row.status)">
                      {{ getStatusLabel(row.status) }}
                    </el-tag>
                  </span>
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">正确</span>
                  <span class="evaluation-metric-value evaluation-inline-value">
                    <el-icon v-if="row.is_correct" color="#67c23a"><Select /></el-icon>
                    <el-icon v-else color="#f56c6c"><CloseBold /></el-icon>
                    {{ row.is_correct ? '是' : '否' }}
                  </span>
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">幻觉</span>
                  <span class="evaluation-metric-value">
                    <el-tag v-if="row.has_hallucination" type="danger" size="small">有</el-tag>
                    <el-tag v-else type="success" size="small">无</el-tag>
                  </span>
                </div>
                <div class="evaluation-metric">
                  <span class="evaluation-metric-label">置信度</span>
                  <span class="evaluation-metric-value">{{ row.confidence }}</span>
                </div>
              </div>
            </li>
          </ul>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

// 数据
const testCases = ref([])
const selectedCategories = ref(['legal', 'policy', 'out_of_scope'])
const useGraph = ref(true)
const running = ref(false)
const report = ref(null)

// 计算属性
const categoryData = computed(() => {
  if (!report.value?.by_category) return []
  return Object.entries(report.value.by_category).map(([cat, stats]) => ({
    category: cat,
    ...stats
  }))
})

// 方法
const loadTestCases = async () => {
  try {
    const data = await api.get('/evaluation/test-cases')
    testCases.value = data.test_cases
  } catch (e) {
    console.error('加载测试用例失败:', e)
  }
}

const runEvaluation = async () => {
  running.value = true
  report.value = null

  try {
    // 使用同步接口，等待结果
    const result = await api.post('/evaluation/run-sync', {
      categories: selectedCategories.value.length > 0 ? selectedCategories.value : null,
      use_graph: useGraph.value
    })
    report.value = result
    ElMessage.success(`评估完成！幻觉率: ${result.summary.hallucination_rate}%`)
  } catch (e) {
    ElMessage.error('评估失败: ' + e.message)
  } finally {
    running.value = false
  }
}

// 辅助函数
const getCategoryLabel = (cat) => {
  const labels = {
    legal: '法律条文',
    policy: '公司制度',
    factual: '事实查询',
    reasoning: '推理问题',
    out_of_scope: '超出范围',
    multi_hop: '多跳问题'
  }
  return labels[cat] || cat
}

const getCategoryType = (cat) => {
  const types = {
    legal: 'primary',
    policy: 'success',
    factual: 'info',
    reasoning: 'warning',
    out_of_scope: 'danger',
    multi_hop: 'warning'
  }
  return types[cat] || ''
}

const getStatusLabel = (status) => {
  const labels = {
    reliable: '可靠',
    uncertain: '不确定',
    refused: '已拒绝',
    error: '错误'
  }
  return labels[status] || status
}

const getStatusType = (status) => {
  const types = {
    reliable: 'success',
    uncertain: 'warning',
    refused: 'info',
    error: 'danger'
  }
  return types[status] || ''
}

const getMetricClass = (value) => {
  if (value >= 90) return 'excellent'
  if (value >= 80) return 'good'
  if (value >= 60) return 'medium'
  return 'poor'
}

const getHallucinationClass = (value) => {
  if (value <= 5) return 'excellent'
  if (value <= 10) return 'good'
  if (value <= 20) return 'medium'
  return 'poor'
}

const getHallucinationTypeLabel = (type) => {
  const labels = {
    out_of_scope_answered: '超出范围却回答了',
    contains_forbidden: '包含禁止词',
    excessive_uncertainty: '不确定性过多',
    fact_without_source: '事实无来源',
    guard_detected: '防护模块检测'
  }
  return labels[type] || type
}

// 生命周期
onMounted(() => {
  loadTestCases()
})
</script>

<style scoped>
.evaluation {
  padding: 20px;
  width: 100%;
  min-width: 0;
}

.metric-card {
  text-align: center;
  padding: 20px;
}

.metric-value {
  font-size: 36px;
  font-weight: bold;
  margin-bottom: 8px;
}

.metric-value.excellent {
  color: #67c23a;
}

.metric-value.good {
  color: #409eff;
}

.metric-value.medium {
  color: #e6a23c;
}

.metric-value.poor {
  color: #f56c6c;
}

.metric-label {
  color: #909399;
  font-size: 14px;
}

.test-case-count {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.hallucination-type {
  display: flex;
  justify-content: space-between;
  padding: 12px 0;
  border-bottom: 1px solid #ebeef5;
}

.hallucination-type:last-child {
  border-bottom: none;
}

.type-name {
  color: #606266;
  min-width: 0;
  overflow-wrap: anywhere;
}

.evaluation-mobile-list {
  display: none;
  list-style: none;
  margin: 0;
  padding: 0;
}

.table-scroll {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
}

.table-scroll :deep(.el-table) {
  min-width: 360px;
}

@media (max-width: 768px) {
  .evaluation-table {
    display: none;
  }

  .evaluation-mobile-list {
    display: block;
  }

  .evaluation {
    padding: 0;
  }

  .metric-card {
    padding: 16px;
  }

  .metric-value {
    font-size: 32px;
  }

  .evaluation :deep(.el-pagination) {
    justify-content: flex-start !important;
    flex-wrap: wrap;
    gap: 8px;
  }

  .evaluation :deep(.el-descriptions__table) {
    table-layout: fixed;
  }

  .evaluation :deep(.el-descriptions__cell) {
    overflow-wrap: anywhere;
  }

  .evaluation-list-item {
    padding: 12px 0;
    border-bottom: 1px solid #ebeef5;
  }

  .evaluation-list-item:first-child {
    padding-top: 0;
  }

  .evaluation-list-item:last-child {
    padding-bottom: 0;
    border-bottom: none;
  }

  .evaluation-field {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    min-width: 0;
  }

  .evaluation-field + .evaluation-field {
    margin-top: 8px;
  }

  .evaluation-field-label {
    flex: 0 0 56px;
    color: #909399;
    font-size: 12px;
  }

  .evaluation-field-value,
  .evaluation-metric-value {
    min-width: 0;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .evaluation-list-heading .evaluation-field-value {
    font-weight: 500;
  }

  .evaluation-detail-question .evaluation-field-value {
    white-space: pre-wrap;
  }

  .evaluation-metric-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    margin-top: 12px;
  }

  .evaluation-metric {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .evaluation-metric-wide {
    grid-column: 1 / -1;
  }

  .evaluation-metric-label {
    color: #909399;
    font-size: 12px;
  }

  .evaluation-progress {
    width: 100%;
    min-width: 0;
  }

  .evaluation-inline-value {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
}
</style>
