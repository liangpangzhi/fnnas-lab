@echo off
chcp 65001 >nul
title 连接飞牛虚拟机 SSH
echo ============================================================
echo   连接飞牛 fnOS 虚拟机
echo   地址：127.0.0.1  端口：2222
echo ============================================================
echo.
echo   [前提] 需要先在飞牛网页界面开启 SSH：
echo          登录 http://localhost:5666 之后，
echo          进入 设置 -^> SSH（新版在 设置 -^> 安全 -^> SSH）
echo          打开「启用 SSH 服务」，端口保持 22，保存。
echo.
echo   [账号] 填安装时创建的管理员账号；
echo          需要 root 权限时登录后再执行 sudo -i
echo.
set /p FNUSER=请输入飞牛用户名（直接回车默认 root）: 
if "%FNUSER%"=="" set FNUSER=root
echo.
echo 正在连接 %FNUSER%@127.0.0.1:2222 ...
echo.
ssh -p 2222 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="%USERPROFILE%\.ssh\known_hosts_fnnas" %FNUSER%@127.0.0.1
echo.
echo ------------------------------------------------------------
echo 连接已结束。若提示 Connection refused / 被重置，
echo 说明虚拟机的 SSH 服务还没开启，或虚拟机未启动。
echo ------------------------------------------------------------
pause >nul
