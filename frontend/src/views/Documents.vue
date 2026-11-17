<template>
  <div class="documents-page">
    <el-row :gutter="24">
      <el-col :span="24">
        <el-card>
          <template #header>
            <div class="card-header">
              <span>文档管理</span>
              <el-upload
                :action="uploadUrl"
                :headers="uploadHeaders"
                :on-success="handleUploadSuccess"
                :on-error="handleUploadError"
                :before-upload="beforeUpload"
                :show-file-list="false"
                accept=".pdf,.docx,.doc,.txt,.md"
              >
                <el-button type="primary">
                  <el-icon><Upload /></el-icon>
                  上传文档
                </el-button>
              </el-upload>
            </div>
          </template>

          <div class="table-scroll documents-table">
            <el-table :data="documents" v-loading="loading" stripe>
              <el-table-column prop="title" label="标题" min-width="200" />
              <el-table-column prop="source" label="文件名" width="200" />
              <el-table-column prop="doc_type" label="类型" width="100">
                <template #default="{ row }">
                  <el-tag size="small">{{ row.doc_type }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="created_at" label="上传时间" width="180">
                <template #default="{ row }">
                  {{ formatDate(row.created_at) }}
                </template>
              </el-table-column>
              <el-table-column label="操作" width="100" fixed="right">
                <template #default="{ row }">
                  <el-button
                    type="danger"
                    size="small"
                    @click="handleDelete(row.doc_id)"
                    link
                  >
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <ul class="documents-mobile-list" v-loading="loading">
            <li
              v-for="row in documents"
              :key="row.doc_id"
              class="document-item"
            >
              <div class="document-item-content">
                <div class="document-field">
                  <span class="document-field-label">标题</span>
                  <span class="document-field-value">{{ row.title || '-' }}</span>
                </div>
                <div class="document-field">
                  <span class="document-field-label">文件名</span>
                  <span class="document-field-value">{{ row.source || '-' }}</span>
                </div>
                <div class="document-meta">
                  <div class="document-field">
                    <span class="document-field-label">类型</span>
                    <span class="document-field-value">
                      <el-tag size="small">{{ row.doc_type }}</el-tag>
                    </span>
                  </div>
                  <div class="document-field">
                    <span class="document-field-label">上传时间</span>
                    <span class="document-field-value">{{ formatDate(row.created_at) }}</span>
                  </div>
                </div>
              </div>
              <el-button
                class="document-delete"
                type="danger"
                size="small"
                @click="handleDelete(row.doc_id)"
                link
              >
                删除
              </el-button>
            </li>
          </ul>

          <el-pagination
            class="documents-pagination"
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[10, 20, 50]"
            layout="total, sizes, prev, pager, next"
            style="margin-top: 16px; justify-content: flex-end;"
            @size-change="fetchDocuments"
            @current-change="fetchDocuments"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const documents = ref([])
const loading = ref(false)
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

const uploadUrl = computed(() => '/api/v1/kb/upload')
const uploadHeaders = computed(() => {
  const token = localStorage.getItem('token')
  return token ? { Authorization: `Bearer ${token}` } : {}
})

const formatDate = (dateStr) => {
  if (!dateStr) return '-'
  const date = new Date(dateStr)
  return date.toLocaleString('zh-CN')
}

const fetchDocuments = async () => {
  loading.value = true
  try {
    const result = await api.get('/kb/documents', {
      params: {
        skip: (currentPage.value - 1) * pageSize.value,
        limit: pageSize.value,
      },
    })
    documents.value = result.documents || []
    total.value = result.total || 0
  } catch (e) {
    ElMessage.error('获取文档列表失败')
  } finally {
    loading.value = false
  }
}

const beforeUpload = (file) => {
  const allowedTypes = ['.pdf', '.docx', '.doc', '.txt', '.md']
  const ext = '.' + file.name.split('.').pop().toLowerCase()
  if (!allowedTypes.includes(ext)) {
    ElMessage.error(`不支持的文件格式: ${ext}`)
    return false
  }
  if (file.size > 50 * 1024 * 1024) {
    ElMessage.error('文件大小不能超过 50MB')
    return false
  }
  return true
}

const handleUploadSuccess = (response) => {
  ElMessage.success(response.message || '上传成功')
  fetchDocuments()
}

const handleUploadError = (error) => {
  ElMessage.error('上传失败: ' + (error.message || '未知错误'))
}

const handleDelete = async (docId) => {
  try {
    await ElMessageBox.confirm('确定要删除这个文档吗？', '确认删除', {
      type: 'warning',
    })
    await api.delete(`/kb/documents/${docId}`)
    ElMessage.success('删除成功')
    fetchDocuments()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

onMounted(() => {
  fetchDocuments()
})
</script>

<style scoped>
.documents-page {
  max-width: 1200px;
  margin: 0 auto;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.documents-mobile-list {
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
  min-width: 760px;
}

@media (max-width: 768px) {
  .documents-table {
    display: none;
  }

  .documents-mobile-list {
    display: block;
  }

  .documents-page {
    width: 100%;
  }

  .card-header {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .card-header :deep(.el-upload) {
    margin-left: auto;
  }

  .table-scroll {
    margin-right: 0;
  }

  .documents-page :deep(.documents-pagination) {
    justify-content: flex-start !important;
    flex-wrap: wrap;
    gap: 8px;
    height: auto;
  }

  .document-item {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    padding: 16px 0;
    border-bottom: 1px solid #ebeef5;
  }

  .document-item:first-child {
    padding-top: 0;
  }

  .document-item:last-child {
    padding-bottom: 0;
    border-bottom: none;
  }

  .document-item-content {
    min-width: 0;
  }

  .document-field {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    min-width: 0;
  }

  .document-field + .document-field {
    margin-top: 8px;
  }

  .document-field-label {
    flex: 0 0 56px;
    color: #909399;
    font-size: 12px;
  }

  .document-field-value {
    min-width: 0;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .document-meta {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 8px 16px;
    margin-top: 12px;
  }

  .document-meta .document-field + .document-field {
    margin-top: 0;
  }

  .document-delete {
    align-self: start;
  }
}
</style>
