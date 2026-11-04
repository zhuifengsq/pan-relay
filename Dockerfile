FROM python:3.11-slim

WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装系统基础工具（时区与健康检查所需 curl）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装依赖（配置国内镜像源以加速下载并防止网络中断）
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 复制项目代码
COPY . .

# 暴露默认端口
EXPOSE 5004

# 容器健康检查（/healthz 只探测进程与 SQLite，不受前端关闭/登录跳转影响）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5004/healthz || exit 1

# 生产环境使用 waitress WSGI 服务器启动（默认 8 个工作线程，可用 WAITRESS_THREADS 调整）
CMD ["python", "serve.py"]
