# 单实例服务器部署

1. 在专用 deep-harness Conda 环境准备 Python 3.12 和锁定依赖。
2. 安装桌面构建依赖并从 desktop 运行 npm run build。构建同时生成 Web 静态资源。
3. 复制或调整 [实例配置](../examples/instance.yaml)。配置中的工作路径相对该文件解析。使用云模型时，在服务进程环境中提供相应的密钥。
4. 用独立状态目录启动单进程服务，再浏览 http://127.0.0.1:8765。

```powershell
conda activate deep-harness
cd desktop
npm ci
npm run build
cd ..
deep-harness --state-dir .state/web web --instance-config examples/instance.yaml
```

不同客户端访问同一实例时共享会话、工具审批和产物。远程连接通过可信内网或 SSH 隧道。使用反向代理时配置对外 origin 与主机名，保持 Web 和 API 同源；转发 SSE 时关闭响应缓冲。服务进程只运行一份，状态卷同时包含业务库、checkpoint 和产物。停止实例后可以备份该目录及部署配置；密钥引用的实际值单独管理。也可在停止 Web 服务后使用已有 Profile 备份入口按目录归档。

例如，可信内网域名为 `harness.example.internal` 时，将其加入 `web.trusted_hosts`，并设置 `web.public_origin: https://harness.example.internal`。同机 Nginx 的 HTTPS server 块可包含以下配置。示例上传上限对应默认实例配置，修改两端时保持一致。

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_read_timeout 120s;
    client_max_body_size 16m;
}
```

## 业务样例

在另一个终端运行 python examples/mock_business.py --database .state/web/demo-business.db。工作流页可执行采购申请与工单登记，观察验证、审批、写入与外部结果查询。

## 隔离命令

将 sandbox.image 配置为已经拉取的 Linux 镜像，如 alpine:3.22，并将相应工作目录 mode 设置为 sandbox。工作目录独立于状态目录和实例配置。每次 Run 的隔离命令共享容器内工作副本，宿主源目录只读、网络关闭，命令结束后收集有界差异。运行结束或取消后回收对应容器。重启会清理残留容器。曾经使用沙箱的 Run 如果失去工作副本，需要开启新任务，已经持久化的结果仍可查看。文件读取工具仍以原工作目录为基准。Docker 服务与镜像由部署者安装和维护。
