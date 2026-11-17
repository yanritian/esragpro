<template>
  <div class="home">
    <el-row :gutter="24">
      <el-col :span="24">
        <el-card class="hero-card">
          <h1>esRAGPro</h1>
          <p class="subtitle">企业可信知识库解决方案</p>
          <p class="description">
            基于 Elasticsearch 混合检索技术，提供准确、可追溯、可控的企业知识问答能力
          </p>
          <div class="features">
            <el-row :gutter="16">
              <el-col :span="8">
                <el-card shadow="hover" class="feature-card">
                  <el-icon :size="40" color="#409eff"><Aim /></el-icon>
                  <h3>准确</h3>
                  <p>BM25 + 向量混合检索<br/>准确率提升 10%+</p>
                </el-card>
              </el-col>
              <el-col :span="8">
                <el-card shadow="hover" class="feature-card">
                  <el-icon :size="40" color="#67c23a"><Link /></el-icon>
                  <h3>可追溯</h3>
                  <p>每个答案标注来源<br/>原文可验证</p>
                </el-card>
              </el-col>
              <el-col :span="8">
                <el-card shadow="hover" class="feature-card">
                  <el-icon :size="40" color="#e6a23c"><Lock /></el-icon>
                  <h3>安全</h3>
                  <p>本地私有部署<br/>数据不出企业</p>
                </el-card>
              </el-col>
            </el-row>
          </div>
          <el-button type="primary" size="large" @click="$router.push('/search')">
            开始使用
          </el-button>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="24" style="margin-top: 24px;">
      <el-col :span="12">
        <el-card>
          <template #header>
            <span>快速检索</span>
          </template>
          <el-input
            v-model="quickQuery"
            placeholder="输入问题，快速检索知识库..."
            size="large"
            @keyup.enter="handleQuickSearch"
          >
            <template #append>
              <el-button @click="handleQuickSearch">搜索</el-button>
            </template>
          </el-input>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>
            <span>系统状态</span>
          </template>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="ES 状态">
              <el-tag :type="esStatus === 'healthy' ? 'success' : 'danger'">
                {{ esStatus }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="文档数量">
              <span v-if="docCount === null" class="unavailable">不可用</span>
              <span v-else>{{ docCount }}</span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
const quickQuery = ref('')
const esStatus = ref('checking')
const docCount = ref(null)

const handleQuickSearch = () => {
  if (quickQuery.value.trim()) {
    router.push({ path: '/search', query: { q: quickQuery.value } })
  }
}

onMounted(async () => {
  try {
    const health = await api.get('/kb/health')
    esStatus.value = health.elasticsearch === 'healthy' ? 'healthy' : 'unhealthy'
  } catch (e) {
    esStatus.value = 'unhealthy'
  }

  try {
    const documentResponse = await api.get('/kb/documents', {
      params: { skip: 0, limit: 1 },
    })
    docCount.value = typeof documentResponse.total === 'number' && Number.isFinite(documentResponse.total)
      ? documentResponse.total
      : null
  } catch (e) {
    docCount.value = null
  }

})
</script>

<style scoped>
.home {
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
  min-width: 0;
}

.hero-card {
  text-align: center;
  padding: 40px 20px;
}

.hero-card h1 {
  font-size: 48px;
  color: #303133;
  margin-bottom: 8px;
}

.subtitle {
  font-size: 24px;
  color: #409eff;
  margin-bottom: 16px;
}

.description {
  color: #909399;
  margin-bottom: 32px;
}

.features {
  margin-bottom: 32px;
}

.feature-card {
  text-align: center;
  padding: 20px;
}

.feature-card h3 {
  margin: 16px 0 8px;
  color: #303133;
}

.feature-card p {
  color: #909399;
  font-size: 14px;
}

.unavailable {
  color: #909399;
}

@media (max-width: 767px) {
  .hero-card {
    padding: 24px 16px;
  }

  .hero-card h1 {
    font-size: 36px;
  }

  .subtitle {
    font-size: 20px;
  }

  .description {
    line-height: 1.6;
    overflow-wrap: anywhere;
  }

  .feature-card {
    padding: 16px;
  }

  .home :deep(.el-descriptions__table) {
    table-layout: fixed;
  }

  .home :deep(.el-descriptions__cell) {
    overflow-wrap: anywhere;
  }
}
</style>
