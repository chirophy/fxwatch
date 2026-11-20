# FX Watch 银行购汇看板

一个容器自动完成初始化、每五分钟采集和 Web 展示。直接读取六家银行官方公开牌价，比较 USD、EUR、HKD、JPY 的现汇卖出价，支持历史走势、CSV 导出和可选 Telegram 提醒。

## 启动

```bash
docker run -d --name fxwatch --restart unless-stopped --stop-timeout 90 \
  -p 8099:8099 -v fxwatch-data:/data chirophy/fxwatch:1.3.0
```

打开 `http://服务器IP:8099`。无需额外配置文件或手动创建 Docker 网络。服务器需允许访问银行 HTTPS；直接访问看板需允许 TCP 8099。该入口为 HTTP，HTTPS 可由 NPM 等反向代理提供。

## 数据和配置

- 挂载 `/data` 保存数据库、配置和原始响应；不要删除数据卷。
- 首次自动生成 `/data/config.toml`，可设置采集间隔和目标价。
- 可选环境变量 `TELEGRAM_BOT_TOKEN` 与 `TELEGRAM_CHAT_ID` 必须同时填写；默认不启用通知。
- 默认以 UID/GID 10001 运行；使用命名卷自动准备目录，自行绑定宿主机目录时需保证该用户可写。
- Docker 健康检查反映 Web/API 是否可用；页面另行标记过期、失败和有效银行不足等报价状态。

## 运维

```bash
docker logs --tail 80 fxwatch
docker exec fxwatch python -m fxwatch --config /data/config.toml status
docker exec fxwatch python -m fxwatch --config /data/config.toml backup \
  --output /data/backup-20261120.sqlite3
docker cp fxwatch:/data/backup-20261120.sqlite3 ./backup-20261120.sqlite3
```

备份文件名每次使用新的名称。升级前备份，然后保留同一个 `/data` 卷重建容器；镜像本身不包含历史数据。

公开牌价不保证等于银行 App 的优惠价或实际成交价。排名仅覆盖当前可用银行样本，银行接口变化可能需要适配。本项目不执行购汇交易。
