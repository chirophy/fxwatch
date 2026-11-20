# 单容器 Docker 部署 · chirophy/fxwatch · 8099

1.3.0 将数据库初始化、银行采集和 Web 合并进一个容器，镜像为 `chirophy/fxwatch:1.3.0`。首次启动自动创建配置和数据库；后续重启保留它们。直接访问 `http://服务器IP:8099`，不用手动创建网络、加入 NPM 网络或安装宿主机 Python。

容器内使用 Tini 和进程管理程序处理启动、退出与信号。采集和 Web 中任一个进程意外退出，整个容器会退出，由 `--restart unless-stopped` 重启。银行请求失败按原退避规则处理，不会因为某家银行暂时不可用就重启容器。

## 一次构建并发布到 Docker Hub

在安装了 Docker 的 Linux 服务器上获取源码并执行：

```bash
git clone https://github.com/chirophy/fxwatch.git
cd fxwatch
docker login --username chirophy
bash scripts/publish-image.sh
```

登录时在服务器终端输入 Docker Hub 密码或访问令牌，不要填写到源码或聊天中。确保账号有 `chirophy/fxwatch` 的推送权限；需要时先在 Docker Hub 创建 `fxwatch` 仓库。

脚本会依次构建 `chirophy/fxwatch:1.3.0`、启动临时单容器、检查四币种 API、检查正常停止，然后推送。临时容器使用独立匿名卷，不访问正式的 `fxwatch-data`。检查失败会停止发布并显示日志。正式旧服务即使正在使用 8099 也不影响该测试，因为临时容器不发布宿主机端口。

脚本默认发布当前构建机器的 CPU 架构：例如 x86_64 对应 linux/amd64。它只更新 `1.3.0` 标签，不会覆盖 `latest`。

如果后续服务器包含不同架构，可在有 Docker Buildx 和相应构建/模拟支持的环境分别测试，再发布多架构标签：

```bash
docker buildx create --name fxwatch-builder --driver docker-container --use
docker buildx build --platform linux/amd64,linux/arm64 \
  -t chirophy/fxwatch:1.3.0 --push .
```

同名 builder 已存在时使用 `docker buildx use fxwatch-builder`。此可选命令直接构建并推送，不会调用上面的运行检查脚本。

## 其他服务器只需要一条启动命令

镜像推送成功后执行（私有仓库需先登录）：

```bash
docker run -d \
  --name fxwatch \
  --restart unless-stopped \
  --stop-timeout 90 \
  -p 8099:8099 \
  -v fxwatch-data:/data \
  chirophy/fxwatch:1.3.0
```

然后访问 `http://服务器IP:8099`。按需要在服务器网络、防火墙和云安全组中允许 TCP 8099。该入口使用 HTTP；若使用 NPM 提供 HTTPS，上游填写 NPM 可达的服务器 IP、端口 8099、Scheme `http`，无需连接项目网络。

查看状态与日志：

```bash
docker ps --filter name=fxwatch
docker logs --tail 80 fxwatch
curl --max-time 10 http://127.0.0.1:8099/api/summary
```

只会有一个应用容器。首次没有采集数据或周末报价过期时，网页仍可正常打开；镜像健康检查使用 `/api/summary` 检查应用可用性，`/api/health` 则用于判断报价是否足够新鲜。

## 持久化内容与自定义配置

命名卷 `fxwatch-data` 中保存：

| 容器内路径 | 用途 |
| --- | --- |
| `/data/config.toml` | 首次启动自动创建的配置；以后保留用户修改 |
| `/data/fxwatch.sqlite3` | 历史报价、提醒状态与队列 |
| `/data/raw/` | 各银行最新原始响应 |
| `/data/report.html` | 最近生成的静态报告 |

默认已经包含六家银行、USD/EUR/HKD/JPY 和五分钟采集间隔。只想查看汇率，无需额外配置文件或 `.env`。

修改银行、目标价或间隔，可先把配置复制出来，用编辑器修改，再复制回去（主机上的配置文件可用其他名称，但复制回去的路径固定）：

```bash
docker cp fxwatch:/data/config.toml ./fxwatch-config.toml
nano fxwatch-config.toml
chmod 644 fxwatch-config.toml
docker stop --time 90 fxwatch
docker cp ./fxwatch-config.toml fxwatch:/data/config.toml
docker start fxwatch
```

保留 `database = "/data/fxwatch.sqlite3"` 和 `raw_directory = "/data/raw"`。`docker cp` 写入的文件必须让容器 UID 10001 可读取；通常使用 `chmod 644 fxwatch-config.toml` 后再复制即可。只在配置中保存运行参数，Telegram 凭据放在容器环境变量中。

需要 Telegram 时，在宿主机创建仅自己可读的环境文件，例如 `/etc/fxwatch-docker.env`：

```dotenv
TELEGRAM_BOT_TOKEN=你的BotToken
TELEGRAM_CHAT_ID=你的ChatID
```

设置权限 `chmod 600 /etc/fxwatch-docker.env`。启动命令增加 `--env-file /etc/fxwatch-docker.env`；已存在的容器需停止并删除容器后，用同一个数据卷重建。Web 子进程不会接收这两个环境变量，页面和 API 也不会返回凭据。

## 从 1.2 的三个容器升级

新版本仍使用原数据卷 `fxwatch-data`。先备份，再停止旧容器，不删除卷。

1. 在旧项目目录执行（备份文件名必须尚不存在）：

   ```bash
   docker compose exec collector python -m fxwatch \
     --config /etc/fxwatch/config.toml backup --output /data/before-single-20261120.sqlite3
   docker compose down
   ```

2. 获取新镜像；已经发布时使用拉取，尚未发布时可在新源码目录构建：

   ```bash
   docker pull chirophy/fxwatch:1.3.0
   # 或者在新源码目录执行：docker build -t chirophy/fxwatch:1.3.0 .
   ```

3. 将旧项目的 `config.toml` 导入数据卷，保留目标价和采集设置。替换下面绝对路径；旧配置须使用 `/data/...` 路径并且可被 UID 10001 读取。此命令用独占写入，若卷内已有 `config.toml` 会拒绝覆盖，应先核对已有配置：

   ```bash
   docker run --rm \
     -v fxwatch-data:/data \
     --mount type=bind,source=/旧部署目录/config.toml,target=/migration.toml,readonly \
     chirophy/fxwatch:1.3.0 python -c \
     'from pathlib import Path; content=Path("/migration.toml").read_bytes(); f=Path("/data/config.toml").open("xb"); f.write(content); f.close()'
   ```

4. 按上面的 `docker run` 命令启动一个容器。如曾配置 Telegram，把原 `.env` 中的两个凭据加入容器的环境文件。原来的 `COMPOSE_FILE`、`NPM_NETWORK` 不需要带入。

不要执行 `docker compose down -v` 或删除 `fxwatch-data`。旧源码、旧宿主机配置及卷内备份均可保留。原 systemd 部署先停止采集，通过原版本的 SQLite `backup` 命令导出；在单容器启动前把备份作为 `/data/fxwatch.sqlite3` 导入新卷，并将配置中的数据库与原始响应路径改为 `/data/...`，文件须可由 UID 10001 读写。确认新容器正常后停用旧服务，避免同时运行两个采集进程。

## 使用 Compose 管理一个容器（可选）

新 `compose.yaml` 只有一个 `app` 服务，默认公开 8099、使用 `fxwatch-data`。不需要 `.env` 即可启动；有旧 `.env` 时至少把 `COMPOSE_FILE` 改为 `compose.yaml`，不要继续加载旧版的 NPM 覆盖文件。

```bash
# 从源码构建并启动
docker compose -f compose.yaml up -d --build

# 或者镜像发布后拉取并启动
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d --no-build
```

`.env.example` 提供可选的镜像名、监听地址及 Telegram 配置。不要同时用 Compose 和手写 docker run 管理同名 `fxwatch` 容器，应选一种方式。

使用 NPM 时直接转发到 NPM 可达的服务器 IP 和 HTTP 8099，无需额外 Compose 文件或手动连接项目网络。

## 更新、备份与镜像文件导出

备份使用 SQLite 在线备份接口，不直接复制运行中的数据库文件：

```bash
docker exec fxwatch python -m fxwatch --config /data/config.toml \
  backup --output /data/backup-20261120.sqlite3
docker cp fxwatch:/data/backup-20261120.sqlite3 ./backup-20261120.sqlite3
docker cp fxwatch:/data/config.toml ./fxwatch-config-backup.toml
```

更新时先拉取新标签，再停止并删除旧容器（不删除数据卷），使用相同的卷和端口重新执行启动命令。按实际新版本替换镜像标签；如使用环境文件，重建时仍需传入。

```bash
docker stop --time 90 fxwatch
docker rm fxwatch
# 然后使用上面的 docker run 命令启动新版本，仍挂载 fxwatch-data。
```

需要导出镜像文件时：

```bash
docker image save -o fxwatch-1.3.0-image.tar chirophy/fxwatch:1.3.0
# 目标服务器导入后使用相同的 docker run 命令
docker image load -i fxwatch-1.3.0-image.tar
```

镜像不包含实际配置、数据库或 Telegram 凭据；换服务器时另外迁移数据备份。同一服务器重建容器会继续使用原卷。

参考：[容器中管理多个进程](https://docs.docker.com/engine/containers/multi-service_container/)、[Docker Hub 镜像推送](https://docs.docker.com/reference/cli/docker/image/push/)、[Docker 多架构构建](https://docs.docker.com/build/building/multi-platform/)。
