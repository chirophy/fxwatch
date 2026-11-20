# FX Watch 1.3.0 · 银行购汇监测

一个 Docker 容器自动完成数据库初始化、定时采集与 Web 展示。直接读取广发、华夏、中国银行、工商银行、招商银行、中信银行的官方公开牌价，比较 **美元 USD、欧元 EUR、港币 HKD、日元 JPY 的现汇卖出价**。

## 快速启动

```bash
docker run -d --name fxwatch --restart unless-stopped --stop-timeout 90 \
  -p 8099:8099 -v fxwatch-data:/data chirophy/fxwatch:1.3.0
```

打开 **http://服务器IP:8099**。只需 Docker，无需单独安装 Python、手动创建网络或准备配置文件。需要外部访问时，在服务器网络和云安全组中允许 TCP 8099。

首次启动会在数据卷中生成 `/data/config.toml` 和 SQLite 数据库，后续重建容器保留数据。默认以 UID/GID 10001 运行；命名卷会准备相应目录权限。不要删除 `fxwatch-data`。

## 功能

- 每 5 分钟采集六家银行的四币种牌价，网页每 30 秒读取一次数据库。
- 四币种概览、银行价格比较、购汇金额估算、6 小时 / 24 小时 / 7 天走势。
- 历史 CSV 导出、独立 HTML 报告和 SQLite 在线备份。
- 可选 Telegram 到价提醒与监测期间日内新低提醒。
- 旧报价、失败、异常值与有效样本不足会明确显示；至少两家有效银行才进行最低价比较。
- 一个容器管理采集与 Web 子进程；子进程意外退出会使容器退出，交由 Docker 重启策略处理。

公开牌价不保证等于银行 App 优惠价或实际成交价，排名只覆盖当前有效样本。本项目不执行购汇交易。

## 从源码部署

```bash
git clone https://github.com/chirophy/fxwatch.git
cd fxwatch
docker compose up -d --build
```

Compose 同样只启动一个容器，使用端口 8099 和数据卷 `fxwatch-data`。已有同名容器时，先按迁移说明停用原部署；不要同时用 Compose 与手写 docker run 管理同名容器。

`.env.example` 提供镜像名、监听地址和 Telegram 环境变量的可选配置；默认无需 `.env`。银行、目标价和采集频率保存在数据卷的 `/data/config.toml`。

## 发布到 Docker Hub

在安装了 Docker 的 Linux 构建服务器、源码目录中执行：

```bash
docker login --username chirophy
bash scripts/publish-image.sh
```

脚本构建 `chirophy/fxwatch:1.3.0`，使用临时容器与独立匿名卷检查四币种 API 和正常退出，成功后才推送。默认发布当前构建机器架构，不覆盖 `latest`。账号须具有目标仓库推送权限。

## 日常管理

```bash
# 查看日志
docker logs --tail 80 fxwatch
# 查看采集状态
docker exec fxwatch python -m fxwatch --config /data/config.toml status
# 在线备份：每次使用不同文件名
docker exec fxwatch python -m fxwatch --config /data/config.toml \
  backup --output /data/backup-20261120.sqlite3
docker cp fxwatch:/data/backup-20261120.sqlite3 ./backup-20261120.sqlite3
```

NPM 等反向代理可转发到服务器可达 IP 的 **HTTP 8099**，无需加入项目网络；建议使用独立子域名，HTTPS 由反向代理提供。

## 文档

- [部署、配置、Docker Hub 发布与旧版迁移](docs/docker.md)
- [价格比较、数据时效和提醒规则](docs/rules.md)
- [官方数据源与开发验证记录](docs/verification.md)
- [Docker Hub 仓库说明](docs/dockerhub.md)

## 开发与测试

需要 Python 3.11 或更新版本。生产容器基于 Python 3.13，使用 Gunicorn。

```bash
python -m unittest discover -s tests -v
python scripts/package.py
```

测试使用已脱敏的银行样本，不会向银行或 Telegram 发送请求。打包结果写入 `dist/`，不包含实际配置、运行数据库、通知凭据或开发临时产物。

版本固定为 **1.3.0 单容器 Docker 方案**。本地开发检查通过 39 项测试、Compose 解析和发布脚本语法检查；Linux 容器构建与运行检查由发布脚本在构建服务器执行。验证范围和历史证据见文档。
