#!/usr/bin/env python3
"""
数据库初始化脚本
运行此脚本可创建所有必要的表
"""
import asyncio
import asyncpg


async def init_db():
    """初始化数据库"""
    conn = await asyncpg.connect(
        host="localhost",
        port=5432,
        user="postgres",
        password="postgres",
        database="esragpro",
    )

    print("创建用户表...")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            email VARCHAR(100) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(20) DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    print("创建文档表...")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id SERIAL PRIMARY KEY,
            doc_id VARCHAR(100) UNIQUE NOT NULL,
            title VARCHAR(500) NOT NULL,
            source VARCHAR(500),
            file_path VARCHAR(1000),
            doc_type VARCHAR(20),
            file_size BIGINT,
            chunk_count INTEGER DEFAULT 0,
            status VARCHAR(20) DEFAULT 'pending',
            metadata JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    print("创建知识库表...")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_bases (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            description TEXT,
            owner_id INTEGER REFERENCES users(id),
            document_count INTEGER DEFAULT 0,
            es_index_name VARCHAR(100) DEFAULT 'knowledge_base',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    print("创建索引...")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_doc_id ON documents(doc_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);")

    await conn.close()
    print("数据库初始化完成！")


if __name__ == "__main__":
    asyncio.run(init_db())
