@echo off
REM esRAGPro Windows 数据备份脚本

set BACKUP_DIR=backup\%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set BACKUP_DIR=%BACKUP_DIR: =0%
mkdir %BACKUP_DIR% 2>nul

echo 开始备份数据到 %BACKUP_DIR% ...

REM 1. 备份 PostgreSQL
echo 备份 PostgreSQL...
docker exec esragpro-postgres pg_dump -U postgres esragpro > %BACKUP_DIR%\postgres.sql

REM 2. 备份上传文件
echo 备份上传文件...
if exist uploads (
    tar czf %BACKUP_DIR%\uploads.tar.gz uploads
)

echo 备份完成！
echo 备份文件位置: %BACKUP_DIR%
dir %BACKUP_DIR%
