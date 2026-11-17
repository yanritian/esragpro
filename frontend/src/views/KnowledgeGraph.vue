<template>
  <div class="knowledge-graph">
    <el-row :gutter="24">
      <!-- 左侧：图谱统计 -->
      <el-col :span="6">
        <el-card class="stats-card">
          <template #header>
            <div class="card-header">
              <span>图谱统计</span>
              <el-button type="primary" size="small" @click="refreshStats">
                刷新
              </el-button>
            </div>
          </template>

          <el-descriptions :column="1" border>
            <el-descriptions-item label="节点总数">
              <el-tag type="primary">{{ stats.total_nodes }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="关系总数">
              <el-tag type="success">{{ stats.total_edges }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="孤立节点">
              <el-tag :type="stats.orphan_nodes > 0 ? 'warning' : 'info'">
                {{ stats.orphan_nodes }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="平均度">
              {{ stats.avg_degree }}
            </el-descriptions-item>
          </el-descriptions>

          <el-divider>质量指标</el-divider>

          <el-descriptions :column="1" border v-if="quality.connectivity !== undefined">
            <el-descriptions-item label="连通性">
              <el-progress
                :percentage="quality.connectivity"
                :status="quality.connectivity > 80 ? 'success' : 'warning'"
              />
            </el-descriptions-item>
            <el-descriptions-item label="类型均衡度">
              <el-progress
                :percentage="quality.type_balance"
                :status="quality.type_balance > 50 ? 'success' : 'warning'"
              />
            </el-descriptions-item>
            <el-descriptions-item label="平均关系数">
              {{ quality.avg_relations_per_entity }}
            </el-descriptions-item>
          </el-descriptions>

          <el-divider>实体类型分布</el-divider>

          <div class="type-list">
            <div
              v-for="(count, type) in stats.node_types"
              :key="type"
              class="type-item"
            >
              <span class="type-name">{{ type }}</span>
              <el-tag size="small">{{ count }}</el-tag>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧：图谱可视化 -->
      <el-col :span="18">
        <el-card>
          <template #header>
            <div class="card-header">
              <span>知识图谱</span>
              <div class="header-actions">
                <el-input
                  v-model="searchKeyword"
                  placeholder="搜索实体..."
                  style="width: 200px; margin-right: 10px;"
                  clearable
                  @clear="loadOverview"
                  @keyup.enter="searchEntity"
                />
                <el-button type="primary" @click="searchEntity">
                  搜索
                </el-button>
                <el-button @click="loadOverview">
                  重置
                </el-button>
              </div>
            </div>
          </template>

          <div ref="graphContainer" class="graph-container"></div>
        </el-card>

        <!-- 实体详情 -->
        <el-card v-if="selectedEntity" style="margin-top: 16px;">
          <template #header>
            <div class="card-header">
              <span>实体详情: {{ selectedEntity.name }}</span>
              <el-button type="danger" size="small" @click="deleteEntity">
                删除
              </el-button>
            </div>
          </template>

          <el-descriptions :column="2" border>
            <el-descriptions-item label="名称">
              {{ selectedEntity.name }}
            </el-descriptions-item>
            <el-descriptions-item label="类型">
              <el-tag>{{ selectedEntity.types?.join(', ') }}</el-tag>
            </el-descriptions-item>
          </el-descriptions>

          <el-divider>相关关系</el-divider>

          <div class="table-scroll">
            <el-table :data="entityRelations" style="width: 100%">
              <el-table-column prop="relation" label="关系类型" width="150" />
              <el-table-column label="方向" width="80">
                <template #default="{ row }">
                  {{ row.source === selectedEntity.name ? '→' : '←' }}
                </template>
              </el-table-column>
              <el-table-column label="目标实体">
                <template #default="{ row }">
                  {{ row.source === selectedEntity.name ? row.target : row.source }}
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
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

// 数据
const stats = ref({
  total_nodes: 0,
  total_edges: 0,
  node_types: {},
  orphan_nodes: 0,
  avg_degree: 0
})

const quality = ref({})
const searchKeyword = ref('')
const selectedEntity = ref(null)
const entityRelations = ref([])

// 图谱
const graphContainer = ref(null)
let network = null

// 颜色映射
const typeColors = {
  'Person': '#409eff',
  'ORGANIZATION': '#67c23a',
  'Law': '#e6a23c',
  'CASE': '#f56c6c',
  'CONCEPT': '#909399',
  'Document': '#b37feb',
}

// 加载统计
const refreshStats = async () => {
  try {
    const [statsRes, qualityRes] = await Promise.all([
      api.get('/graph/stats'),
      api.get('/graph/quality')
    ])
    stats.value = statsRes
    quality.value = qualityRes
  } catch (e) {
    console.error('加载统计失败:', e)
  }
}

// 加载图谱概览
const loadOverview = async () => {
  try {
    const data = await api.get('/graph/overview?limit=100')
    selectedEntity.value = null
    renderGraph(data.nodes, data.edges)
  } catch (e) {
    console.error('加载图谱失败:', e)
  }
}

// 搜索实体
const searchEntity = async () => {
  if (!searchKeyword.value.trim()) {
    loadOverview()
    return
  }

  try {
    const data = await api.get(`/graph/subgraph/${encodeURIComponent(searchKeyword.value)}?depth=2`)
    selectedEntity.value = null
    renderGraph(data.nodes, data.edges)
  } catch (e) {
    ElMessage.error('未找到相关实体')
  }
}

// 渲染图谱
const renderGraph = (nodes, edges) => {
  if (!graphContainer.value) return

  // 动态导入 vis-network
  import('vis-network/standalone').then(({ Network }) => {
    // 节点数据
    const nodeIdByName = new Map()
    const nodeNameById = new Map()
    const visNodes = nodes.map(node => {
      const id = node.id ?? node.name
      nodeIdByName.set(node.name, id)
      nodeNameById.set(String(id), node.name)

      return {
        id,
        label: node.name,
        color: typeColors[node.types?.[0]] || '#409eff',
        font: { size: 14 },
        margin: 10,
      }
    })

    // 边数据
    const visEdges = edges.reduce((result, edge, index) => {
      const from = nodeIdByName.get(edge.source)
      const to = nodeIdByName.get(edge.target)

      if (from === undefined || to === undefined) return result

      result.push({
        id: `${String(from)}-${String(to)}-${index}`,
        from,
        to,
        label: edge.relation,
        arrows: 'to',
        color: { color: '#ccc' },
        font: { size: 10, align: 'middle' },
      })
      return result
    }, [])

    // 配置
    const options = {
      nodes: {
        shape: 'box',
        borderWidth: 2,
        shadow: true,
      },
      edges: {
        smooth: {
          type: 'continuous',
        },
      },
      physics: {
        stabilization: true,
        barnesHut: {
          gravitationalConstant: -3000,
          centralGravity: 0.3,
          springLength: 150,
        },
      },
      interaction: {
        hover: true,
        tooltipDelay: 200,
        zoomView: true,
      },
    }

    // 创建网络
    const data = {
      nodes: visNodes,
      edges: visEdges,
    }

    if (network) {
      network.destroy()
    }

    network = new Network(graphContainer.value, data, options)

    // 点击事件
    network.on('click', async (params) => {
      if (params.nodes.length > 0) {
        const nodeId = params.nodes[0]
        const entityName = nodeNameById.get(String(nodeId))
        if (entityName) await showEntityDetail(entityName)
      }
    })
  })
}

// 显示实体详情
const showEntityDetail = async (entityName) => {
  try {
    const [entity, relations] = await Promise.all([
      api.get(`/graph/entities/${encodeURIComponent(entityName)}`),
      api.get(`/graph/entities/${encodeURIComponent(entityName)}/relations`)
    ])
    selectedEntity.value = entity
    entityRelations.value = relations.relations || []
  } catch (e) {
    console.error('获取实体详情失败:', e)
  }
}

// 删除实体
const deleteEntity = async () => {
  if (!selectedEntity.value) return

  try {
    await ElMessageBox.confirm(
      `确定删除实体 "${selectedEntity.value.name}" 及其所有关系？`,
      '删除确认',
      { type: 'warning' }
    )

    await api.delete(`/graph/entities/${encodeURIComponent(selectedEntity.value.name)}`)
    ElMessage.success('删除成功')
    selectedEntity.value = null
    refreshStats()
    loadOverview()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

// 生命周期
onMounted(() => {
  refreshStats()
  loadOverview()
})

onUnmounted(() => {
  if (network) {
    network.destroy()
  }
})
</script>

<style scoped>
.knowledge-graph {
  padding: 20px;
  width: 100%;
  min-width: 0;
}

.stats-card {
  height: calc(100vh - 140px);
  overflow-y: auto;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.header-actions {
  display: flex;
  align-items: center;
  min-width: 0;
}

.graph-container {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: 500px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #fafafa;
  overflow: hidden;
}

.table-scroll {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
}

.table-scroll :deep(.el-table) {
  min-width: 420px;
}

.type-list {
  max-height: 200px;
  overflow-y: auto;
}

.type-item {
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid #ebeef5;
}

.type-name {
  color: #606266;
  min-width: 0;
  overflow-wrap: anywhere;
}

@media (max-width: 767px) {
  .knowledge-graph {
    padding: 0;
  }

  .stats-card {
    height: auto;
    max-height: none;
  }

  .card-header {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .header-actions {
    width: 100%;
    flex-wrap: wrap;
    gap: 8px;
  }

  .header-actions :deep(.el-input) {
    width: 100% !important;
    margin-right: 0 !important;
  }

  .knowledge-graph :deep(.el-descriptions__table) {
    table-layout: fixed;
  }

  .knowledge-graph :deep(.el-descriptions__cell) {
    overflow-wrap: anywhere;
  }

  .graph-container {
    height: min(500px, 70vh);
  }
}
</style>
