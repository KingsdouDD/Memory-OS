#!/bin/bash
# Memory OS 启动脚本

echo "=== Memory OS 启动 ==="

# 检查Docker
if ! command -v docker &> /dev/null; then
    echo "错误: 需要安装 Docker"
    exit 1
fi

# 启动 Qdrant
echo "启动 Qdrant..."
docker run -d --name memory-os-qdrant \
    -p 6333:6333 \
    -p 6334:6334 \
    -v /Users/king/.openclaw/workspace/memory-os/qdrant/data:/qdrant/storage \
    qdrant/qdrant

# 启动 Neo4j
echo "启动 Neo4j..."
docker run -d --name memory-os-neo4j \
    -p 7474:7474 -p 7687:7687 \
    -v /Users/king/.openclaw/workspace/memory-os/neo4j/data:/data \
    -e NEO4J_AUTH=neo4j/memoryos_local \
    neo4j:latest

echo "=== 启动完成 ==="
echo "Qdrant: http://127.0.0.1:6333"
echo "Neo4j: http://127.0.0.1:7474"
