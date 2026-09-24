# MC白名单管理系统（多服安全版）

AstrBot 插件 `astrbot_plugin_mc_whitelist` —— 把 Minecraft 服务器和 QQ 群接在一起：
**群昵称即游戏名**做白名单直连绑定，配合每个 MC 服务器上的配套模组（WS 实时推送 + HTTP 全量拉取），
实现多服白名单同步、双向聊天互通、玩家事件播报、`/info` 统计图片。消息默认 AES-128-ECB 加密 + `[MC]` 前缀校验。

- 当前版本：**v10.4.1**
- 内核要求：AstrBot `>=4.16,<5`（本机实测 4.25.2）
- 协议契约：`docs/接口契约冻结_v10.3.md`（插件 ↔ 模组，**以该文件为准**）

---

## 一、它做什么

| 功能 | 说明 |
|---|---|
| 昵称直连 | `/注册 正` 直接用当前群昵称当游戏名，不用手打名字 |
| 多服汇聚 | 一份绑定数据，推送到 `mc_servers` 里所有启用的服务器 |
| 端口隔离 | 同主机多实例（MCSM 场景）用不同端口，跨主机可复用 |
| 并发推送 | `asyncio.gather` 并行推送，一个服慢不拖累其他服 |
| 离线补推 | 服务器离线时标 `pending_sync`，重连后自动补推全量 |
| QQ → MC | 群消息广播到 `chat_sync=true` 的服务器；`/mc <服务器> <内容>` 定向 |
| MC → QQ | 由**模组端**配置的群白名单决定推送到哪些群，插件纯转发 |
| 统计图片 | `/info` 在 QQ 群内发，插件并发向各服模组要数据 → 生成图片（背景图轮询） |
| 权限 | `mcwhitelist.*` 六个权限节点，内核无权限 API 时自动降级为配置名单判定 |
| 黑名单 | `@成员` 或 QQ 号；拉黑自动解绑并同步所有服 |
| 退群解绑 | 监听群成员减少通知（默认关闭，可配置开启） |

职责边界（保持不混）：**MC 游戏内指令由模组处理**，插件完全不参与，因此不需要唤醒词。

## 二、安装

1. 把插件目录放进 AstrBot 的插件目录（本机为 `C:\Users\<用户名>\.astrbot\data\plugins\`）。
2. 依赖（AstrBot 自带环境通常已包含）：见 `requirements.txt` —— `aiohttp` / `Pillow` / `cryptography`。
3. 重启 AstrBot 或在 WebUI 插件页重载，确认日志出现 `[MCWL] v10.4.1 已启动`。
4. 在 WebUI 插件配置里填 `mc_servers`、`aes_key`、`default_api_token` 等。

> 改完代码务必**完全重启** AstrBot：内存里的旧模块不会自动替换，`__pycache__` 也可能残留。

## 三、指令

| 指令 | 别名 | 说明 |
|---|---|---|
| `/注册 正` / `/注册 皮` | `zc` `绑定` | 绑定正版 / 皮肤站账号，默认用群昵称；`/注册 正 <名字>` 可指定 |
| `/注销` | `zx` `解绑` | 解绑并同步所有服 |
| `/迁移 皮转正` / `/迁移 正转皮` | `qy` | 换账号类型（重新查 UUID） |
| `/更新昵称` | `gxnc` | 群昵称改了以后重新同步绑定 |
| `/info [服务器名\|编号]` | `统计` `我的统计` | 查自己的统计，**纯图片回复** |
| `/black add\|remove\|list` | `黑名单` | 黑名单管理（管理员） |
| `/sync [服务器名\|编号]` | `同步` | 手动推全量白名单（管理员） |
| `/status` | `状态` | 群服互联状态，**带编号**（管理员） |
| `/mc <服务器名\|编号\|all> <内容>` | — | 定向发送到某个服务器 |
| `/mcwl` | `mc白名单` `白名单帮助` | 帮助 |

服务器编号 = `mc_servers` 配置顺序，**跳过 `enabled:false`** 的项（与模组展示一致）。

## 四、配置要点

### 4.1 多服务器与端口（最容易踩的坑）

| 部署场景 | 端口 |
|---|---|
| 跨主机（192.168.1.100 / .101） | 可都用 7789/7790，靠 IP 区分 |
| **同主机多实例（MCSM 常见）** | **必须错开**：实例1 `ws 7789 / http 7790`、实例2 `7788/7791`、实例3 `7787/7792` |

```yaml
mc_servers:
  - name: "生存服"
    ws_url: "ws://127.0.0.1:7789/ws"
    http_url: "http://127.0.0.1:7790"
    token: ""              # 留空用 default_api_token
    enabled: true
    sync_whitelist: true
    chat_sync: true
    event_broadcast: true
  - name: "创造服"
    ws_url: "ws://127.0.0.1:7788/ws"
    http_url: "http://127.0.0.1:7791"
```

同主机端口撞了的表现是「第二个服连不上 / 模组启动报端口占用」，插件侧只会看到连接被拒绝。

### 4.2 安全

| `security_mode` | 行为 |
|---|---|
| `none` | 明文，不校验前缀（仅内网调试） |
| `prefix` | 只校验 `[MC]` 前缀 |
| `encrypted` | **推荐**：AES-128-ECB（PKCS7）+ Base64 + 前缀校验 |

- `aes_key`：取 UTF-8 前 16 字节、不足补 `0x00`（等价 Java `AES/ECB/PKCS5Padding`），**必须与模组完全一致**。
- 非 `none` 模式下**前缀缺失即拒收**；`proto_version` 不一致会拒绝并停止重连（提示升级）。
- HTTP 侧鉴权：`Authorization: Bearer <该服 token 或 default_api_token>`。
- WS 无 TLS（`ws://`），跨公网必须走 VPN 或内网。

### 4.3 其它

- `group_mode` / `group_list`：群白/黑名单（空 = 所有群都服务）。
- `permission_defaults`：各权限节点默认是否对普通成员开放；`permission_enabled=false` 则所有人都能用全部指令。
  群主 / 群管理员自动拥有全部权限节点（不需要配置项）。
- `chat_forward_trigger`：QQ→MC 转发前缀，**留空 = 群里所有消息都转发**（会刷屏，建议填 `#`）。
- `background_images`：统计图背景图，**在 WebUI 里直接上传 / 删除**（AstrBot 原生 `file` 配置项，支持多张、`png/jpg/jpeg/webp`，按上传顺序轮询）。
- `background_images_dir`：备用背景图目录（进阶用法）。只在上传列表为空时使用；相对路径按插件目录解析（默认 `data/backgrounds`），也可填绝对路径；目录不存在或为空则纯色底。
- `admin_qqs`：管理员 QQ 列表（群主自动拥有全部权限节点，不用填）。

## 五、统计图：字体与背景图

**字体**解析顺序：插件 `fonts/` 自带字体 → 系统字体（Windows `msyh.ttc`；Linux Noto CJK / wqy；macOS PingFang）→ 从镜像下载 Noto Sans SC 到 `fonts/` → 都没有则纯色底 + 位图字体（中文会变方块，日志会警告）。

要完全不依赖系统字体，把任意 CJK 字体（.ttf/.otf/.ttc）丢进插件目录 `fonts/` 即可，优先级最高。

**背景图**：在 WebUI 插件配置里找到「统计图片背景图」，点按钮上传即可（可多张，按上传顺序轮询），删除也在同一处点。文件由 AstrBot 存在：

```
<AstrBot 根>/data/plugin_data/astrbot_plugin_mc_whitelist/files/background_images/
```

配置里残留的失效路径（文件被手动删掉等）会被自动跳过，不会让 `/info` 报错。若更喜欢用现成图库，可改用 `background_images_dir` 指向一个目录（仅在上传列表为空时生效）。

> 图片里不画 emoji：中文字体不含彩色 emoji 字形，会渲染成豆腐块，所以图标一律用几何图元画。

## 六、与模组的接口

以 `docs/接口契约冻结_v10.3.md` 为准，摘要：

- 拓扑：**模组 = 服务端**（WS + HTTP），**插件 = 客户端**（主动连、主动推）。
- WS 地址：`ws://<host>:<ws_port>/ws`；消息类型：`auth` / `auth_result` / `whitelist_update` / `whitelist_ack` / `whitelist_sync_request` / `chat` / `player_event` / `server_status`。
- HTTP 端点：`GET /v2/mcwhitelist/health`、`GET /v2/mcwhitelist/whitelist`、`POST /v2/mcwhitelist/whitelist/sync`、`GET /v2/mcwhitelist/stats/player?name=`。
- 统一响应：`{"code":0,"message":"success","data":{...},"timestamp":"..."}`。
- 白名单条目：`{"name","uuid","source":"MOJANG|LITTLESKIN","qq"}`，每次全量覆盖。

模组侧文档见 `docs/模组开发文档.md`。

## 七、测试

```bash
# 逻辑自测（替身，任何 Python 3.11+ 可跑；三套共 136 项）
python tests/test_units.py
python tests/test_protocol.py
python tests/test_interop.py       # 起 mock 模组（WS + HTTP）

# 真实内核验证（必须用 AstrBot 自带的解释器）
"<AstrBot>/backend/python/python.exe" tests/test_real_kernel.py
```

`tests/test_real_kernel.py` 干的事：走 AstrBot 真实的 `PluginManager._load_plugin_metadata` 读 metadata、
把插件装进真实 `star_registry` / handler 注册表并核对 10 条指令与全部别名、实例化插件类、
用 AstrBot 自己的 `validate_config` 校验 `_conf_schema.json` 默认值、检查版本号只写在 `core/version.py`。
找不到 AstrBot 安装目录时自动 SKIP。脚本会把 `ASTRBOT_ROOT` 指向临时目录，不会在插件目录里生成 `data/`。

## 八、排障

| 现象 | 原因 / 处理 |
|---|---|
| 插件装不上，提示「metadata 信息不完整」 | `metadata.yaml` 的 `name/desc/version/author` 必须齐全且不引号包裹 |
| 改了代码但行为没变 | 没完全重启 AstrBot，或 `__pycache__` 残留 —— 删掉后重启 |
| 某服一直连不上 | 端口被别的实例占用（同主机必须错开）；或该服没装模组 |
| 收到消息但被忽略 | 前缀不匹配 / `aes_key` 不一致 / `proto_version` 不一致（看日志哪一条） |
| `/info` 中文是方块 | 没有可用 CJK 字体，往插件 `fonts/` 放一个字体文件 |
| `/info` 报「未获取到统计数据」 | 模组统计接口没实现，或该玩家在该服无记录（后者会显示「无数据」） |
| 传了背景图但图片仍是纯色底 | 图没上传成功（WebUI 会提示）、或配置里的旧路径全失效 —— 看日志 `背景图都不可用` 警告，重新上传即可 |

## 九、版本历史

见 `CHANGELOG.md`。当前 **v10.4.1**（配置页文案改为短标签 + 副标题说明，修复文字截断）。
