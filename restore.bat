@echo off
REM esRAGPro Windows 数据恢复脚本
REM 用法: restore.bat <备份目录>

if "%1"=="" (
    echo 用法: restore.bat ^<备份目录^>
    echo 例如: restore.bat backup\20260416_163000
    exit /b 1
)

set BACKUP_DIR=%1

echo 从 %BACKUP_DIR% 恢复数据...

REM 1. 恢复 PostgreSQL
echo 恢复 PostgreSQL...
if exist %BACKUP_DIR%\postgres.sql (
    docker exec -i esragpro-postgres psql -U postgres -d esragpro < %BACKUP_DIR%\postgres.sql
)

REM 2. 恢复上传文件
echo 恢复上传文件...
if exist %BACKUP_DIR%\uploads.tar.gz (
    tar xzf %BACKUP_DIR%\uploads.tar.gz
)

echo 恢复完成！
