# 官方数据源与验证记录

当前版本 1.3.0：USD/EUR/HKD/JPY 四币种、Web 看板与单容器 Docker 部署。银行联网与浏览器记录来自 2026-11-16 的 1.0/1.1 版验证，单容器配置于 2026-11-20 更新；时间均为北京时间。数据源直接访问银行域名，不请求第三方汇率服务。

| 银行 | 官方数据地址 | 现汇卖出字段 | 原始单位/时间 |
|---|---|---|---|
| 广发 | https://www.cgbchina.com.cn/searchExchangePrice.gsp | 表头 `现汇卖出价`，第 7 列 | 读取第 3 列基数；USD/EUR/HKD 为 1，JPY 为 100；页尾发布时间 |
| 华夏 | https://psbank.hxb.com.cn/gateway/forexquote.jsp | `现汇现钞卖出价`，第 5 列 | 每 100 外币；行内完整发布时间 |
| 中行 | https://www.boc.cn/sourcedb/whpj/ | `现汇卖出价`，第 4 列 | 每 100 外币；发布日期与发布时间 |
| 工行 | https://papi.icbc.com.cn/exchanges/ns/getLatest | JSON `foreignSell`，POST 空请求体 | 每 100 外币；`publishDate` + `publishTime` |
| 招行 | https://fx.cmbchina.cn/api/v1/fx/rate | JSON `rthOfr` | 每 100 外币；`ratDat` + `ratTim` |
| 中信 | https://etrade.citicbank.com/portalweb/cms/getForeignExchRate.htm | JSON `cstexcSellPrice` | 每 100 外币；`quotePriceDate` + `quotePriceTime` |

工行字段映射及请求方法来自官网“人民币即期外汇牌价”页面的公开脚本：
https://www.sh.icbc.com.cn/ICBC/金融信息/行情数据/人民币即期外汇牌价/default.htm
主机名来自 `/Portal_Resources/Common/config.js?v=1`。

招行字段、每 100 外币单位及接口路径来自 https://fx.cmbchina.cn/hq/ 所加载的 `/umi.js`。其请求统一加 `/api` 前缀，不能直接抓前端空壳 HTML。当日接口包含完整 `ratDat`，程序不依赖浏览器显示的“当前日期”推测报价日期。

## 数据源验证

2026-11-16 在开发机真实访问六家银行官方来源，两轮四币种采集均得到 24/24 条有效报价。测试样本保存于 `tests/fixtures`，仅保留目标币种行和所需表头，已移除网页脚本与会话标识。这里记录的是当时的开发验证，不代表此刻可成交报价。

广发日元样本原值 `4.1888`、基数 `100`，换算为 `0.041888` 元/日元；招行曾返回日元 `0.0419748`，Web 保留 7 位小数，数据库保留 Decimal 文本。

## 1.3.0 单容器验证

2026-11-20 全部 39 项 Python 自动测试通过，覆盖：

- 六家银行字段映射、计价单位、四币种精度、结构变化与无效数据拒绝。
- 数据过期、未来时间、请求失败与退避、异常值、有效样本不足不参与排名。
- 目标提醒状态持久化、日内基线与冷却、失败重试、通知有效期及凭据脱敏。
- Web 静态资源、API 参数校验、CSV、历史曲线、只读访问和首次采集前空状态。
- 数据库初始化保留报价与提醒状态，初始化完成后才启动子进程。
- 配置首次创建和保留用户修改，Web 子进程不接收 Telegram 环境变量。
- 实际启动本机子进程验证异常退出、意外正常退出、停止请求和部分启动失败时的清理。

Compose 通过官方工具解析，确认只有一个 `app` 服务、端口 `0.0.0.0:8099`、持久卷 `fxwatch-data`，没有外部网络依赖。发布脚本通过 Bash 语法检查，Python 模块通过编译检查。

Web 在 1.1 开发阶段完成桌面与手机浏览器验证，包括币种切换、金额估算、输入校验、时段切换与过期状态；1.3 未改变这部分界面和业务逻辑。

## 验证边界

开发机未安装 Docker 引擎，因此本地没有执行 1.3 镜像构建、Linux 容器、Tini、Linux 进程组信号、卷权限或真实 NPM 联通验收。发布脚本会在 Linux 构建服务器构建后运行临时容器，检查 API 与退出码，成功后才推送；开发测试通过不等同于该运行检查已经完成。

开发过程未使用真实 Telegram 凭据发送通知，也未进行长时间稳定性测试。银行接口与出口网络可能变化，部署后的实际可用性以日志和看板状态为准。当前仓库仅维护 1.3.0 单容器 Docker 部署方案。
