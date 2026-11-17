#!/bin/bash
# esRAGPro 数据备份脚本
# 用法: ./backup.sh

BACKUP_DIR="./backup/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "开始备份数据到 $BACKUP_DIR ..."

# 1. 备份 PostgreSQL
echo "备份 PostgreSQL..."
docker exec esragpro-postgres pg_dump -U postgres esragpro > "$BACKUP_DIR/postgres.sql"

# 2. 备份 Elasticsearch 快照
echo "备份 Elasticsearch..."
docker exec esragpro-es elasticsearch-snapshot create my_repo es_backup_$(date +%s) --wait 2>/dev/null || {
    echo "ES 快照创建失败，使用 volume 备份..."
    docker run --rm -v esragpro_es_data:/data -v "$BACKUP_DIR":/backup alpine \
        tar czf /backup/es_data.tar.gz -C /data .
}

# 3. 备份 Neo4j
echo "备份 Neo4j..."
docker exec esragpro-neo4j neo4j-admin database dump neo4j --to-path=/backups 2>/dev/null || {
    echo "Neo4j 备份失败，使用 volume 备份..."
    docker run --rm -v esragpro_neo4j_data:/data -v "$BACKUP_DIR":/backup alpine \
        tar czf /backup/neo4j_data.tar.gz -C /data .
}

# 4. 备份上传文件
echo "备份上传文件..."
if [ -d "./uploads" ]; then
    tar czf "$BACKUP_DIR/uploads.tar.gz" ./uploads
fi

# 5. 显示备份大小
echo "备份完成！总大小："
du -sh "$BACKUP_DIR"

echo "备份文件位置: $BACKUP_DIR"
