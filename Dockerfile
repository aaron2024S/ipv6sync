# 纯标准库实现，不需要任何 pip 依赖 —— 镜像因此可以非常小、构建也不需要联网装包
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="huawei-ipv6-trustlist-sync" \
      org.opencontainers.image.description="发现 NAS 自身 IPv6 变化并同步到华为路由器 IPv6 防火墙白名单" \
      org.opencontainers.image.version="1.0.8"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    SESSION_FILE=/data/session.json \
    SETTINGS_FILE=/data/settings.json \
    HEALTH_PORT=8099 \
    HEALTH_HOST=127.0.0.1 \
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
# 6600 = 浏览器控制台（未设 ADMIN_PASSWORD 时不监听）。
# 健康检查/状态（8099）只绑 127.0.0.1（见 HEALTH_HOST），只有容器自己和宿主机
# 本机能访问 —— 不是对外服务，所以**不 EXPOSE**，免得 NAS 面板把它当成可发布端口。
EXPOSE 6600

# 健康检查：/healthz 会在「最近一轮同步成功」时返回 200
# 先设 no_proxy=*：NAS 上的 Docker 若配了代理，会以 HTTP_PROXY 注入容器，那样
# urllib 连 127.0.0.1 都会绕到代理去 —— 健康检查会永远失败（假 unhealthy）。
HEALTHCHECK --interval=60s --timeout=6s --start-period=25s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
os.environ['no_proxy']='*'; \
p=os.environ.get('HEALTH_PORT','8099'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-u", "-m", "app.main"]
