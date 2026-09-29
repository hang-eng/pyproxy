# 反向代理的容器镜像。
#
# 刻意用最朴素的多阶段以外的方式：这个项目**零第三方依赖**，所以镜像里
# 没有 pip install、没有 requirements.txt、没有虚拟环境。一个装依赖的
# 步骤都不需要的镜像，是最不容易在别人机器上装不出来的镜像。

FROM python:3.12-slim

# 不写 .pyc；stdout/stderr 不缓冲，否则 docker logs 要等到缓冲区满才看得到
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# 先只拷代码，再拷文档：改 README 不该让代码层的缓存失效
COPY proxy/ ./proxy/
COPY run.py demo.py mock_backend.py ./

# 跑在非 root 下。代理是直接暴露在公网上的进程，被攻破时不该连带把
# 容器里的 root 一起交出去。这一条在真实部署里是硬要求。
RUN useradd --create-home --shell /usr/sbin/nologin proxy \
    && mkdir -p /app/logs \
    && chown -R proxy:proxy /app
USER proxy

# 只声明，不真的 EXPOSE 端口 —— 是否对外由 -p / compose 的 ports 决定
EXPOSE 8080

# 用 HEALTHCHECK 让编排工具知道「进程还在」不等于「还能服务」。
#
# 探针打的是管理页而不是业务路径，这是有意的：
#   * 管理页不经过后端，所以「后端全挂」不会把这个容器判成 unhealthy ——
#     那属于后端容器自己的健康问题，不该连坐（真的连坐会让编排器把一个
#     好好的代理反复重启）。
#   * 它仍然是完整的「解析请求 -> 生成响应 -> 写回」链路，所以真卡死了
#     一定探得出来。
# urlopen 对非 2xx 会抛异常，退出码非零即 unhealthy。
#
# 注意：探针写死了 8080，改端口时要一起改（compose 里用的是默认端口）。
HEALTHCHECK --interval=15s --timeout=3s --start-period=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/__admin', timeout=2)"

# 默认监听 0.0.0.0:8080，访问日志走 stdout（docker logs 直接能看），
# 后端由 compose 或命令行给出
ENTRYPOINT ["python", "run.py"]
CMD ["--listen-host", "0.0.0.0", "--listen-port", "8080"]
