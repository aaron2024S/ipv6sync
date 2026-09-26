# 纯标准库实现，不需要任何 pip 依赖 —— 镜像因此可以非常小、构建也不需要联网装包
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="huawei-ipv6-trustlist-sync" \
      org.opencontainers.image.description="发现 NAS 自身 IPv6 变化并同步到华为路由器 IPv6 防火墙白名单" \
      org.opencontainers.image.version="1.0.6"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    SESSION_FILE=/data/session.json \
    SETTINGS_FILE=/data/settings.json \
    HEALTH_PORT=8099 \
    PORT=6600

WORKDIR /app

# 非 root 运行
RUN addgroup -g 10001 -S syncapp \
 && adduser -u 10001 -S -G syncapp -H -s /sbin/nologin syncapp \
 && mkdir -p /data \
 && chown -R syncapp:syncapp /data

COPY app/ /app/app/
RUN chown -R syncapp:syncapp /app

USER syncapp

VOLUME ["/data"]
# 8099 = 健康检查/状态；6600 = 浏览器控制台（未设 ADMIN_PASSWORD 时不监听）
EXPOSE 8099 6600

# 健康检查：/healthz 会在「最近一轮同步成功」时返回 200
HEALTHCHECK --interval=60s --timeout=6s --start-period=25s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
p=os.environ.get('HEALTH_PORT','8099'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-u", "-m", "app.main"]
