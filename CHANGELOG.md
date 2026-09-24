# 更新日志

> 版本号规则（与模组同步）：
> Bug 修复 / 小更新 `Z+1`；新增功能或字段 `Y+1`（向后兼容）；协议或配置不兼容 `X+1`（双方必须同时升级）。
>
> 版本号重定基（2026-09-25）：插件版本号原先沿用设计文档的 `10.x` 系列（`10.2.0` → `10.5.0`），
> 但实际只迭代了几个次版本，现统一改为 `0.x` 系列（`10.5.0` → `0.5.0`）。
> **历史条目一并换算，相对顺序与规则不变**；内部设计文档（`docs/*_v0.3.md`）仍用自己的文档版本号。

## v0.5.3 (2026-09-25)

**修复：群白名单匹配不上 → 消息不转发（换成黑名单反而"正常"）。**

- 根因两处：`_group_allowed()` 拿配置里的**原始值**直接和群号字符串比（类型/格式差一点就
  匹配不上）；`_safe_group_id()` 只从**一个**来源取群号。白名单模式下匹配失败 = 不转发，
  黑名单模式匹配失败 = 放行，于是表现为「改黑名单就好了」。
- 取号改为多来源兜底，与内核语义对齐：`event.get_group_id()` →
  `message_obj.group.group_id` → `message_obj.group_id` → `event.group_id` →
  从 `unified_msg_origin` / `session_id` 解析。
  （内核 `AstrMessageEvent.get_group_id()` 读的就是 `message_obj.group_id`，
  见 `core/platform/astr_message_event.py:194`；`unique_session` 打开时
  `session_id` 形如 `QQ号_群号`，取最后一段。）
- 比对前两边都过 `normalize_id()`：去首尾空白、全角数字转半角、去掉「群 12345」这类修饰。
  配置里写 `123456789`（整数）或 `" 123456789 "` 都能匹配到同一个群。
- 匹配不上时不再只说"不在允许范围"：日志/提示**点名本插件实际看到的群号**与当前
  `group_list`，并给出处置办法；`/status` 新增一行
  `👥 本群：<群号>（group_mode=…，✅ 已允许 / ⛔ 不允许）`，可直接抄群号去配置。
- 端到端脚本扩成矩阵验证：白名单+群号正确 / +整数型群号 → 必须转发；群号写错 → 不转发
  且原因点名群号；黑名单命中 / 未命中两个方向。单元 +31 项。

## v0.5.2 (2026-09-25)

**修复：QQ→MC 转发一直不通——门禁判据用错，所有群消息被静默拦下。**

- 根因：处理器用 `event.is_wake_up()` 当门禁。但内核唤醒检查阶段
  （`waking_check/stage.py:196-214`，docstring 也把「插件 handler filter 通过」列为唤醒条件）
  只要**任何** handler 的 filter 通过就会把 `event.is_wake` 置为 True；本处理器只挂
  「群消息」过滤器、对每条群消息都通过 → `is_wake_up()` 恒为 True → 100% 的群消息
  在这一步 `return` 掉，且不留任何日志。
- 改用 `is_at_or_wake_command`：仅当消息带唤醒前缀 / @机器人 / 回复机器人才为真，
  普通聊天消息正常转发。
- 触发前缀不匹配此前也是静默 `return`，现在同样写日志并给出改法。
- 启动时新增一条自述：`[MCWL] QQ→MC 转发：广播=开，触发前缀=（空：全部消息转发），目标=主城服`。
- 新增 `tests/test_e2e_chat_forward.py`（跑在真实内核上，已接入 `run_all --with-kernel`）：
  ① 用真实内核的唤醒检查阶段证明普通群消息 `is_wake=True`、`is_at_or_wake_command=False`
  （旧判据必拦、新判据放行）；② 真事件交给真插件、经真 WS 链路打到假模组，断言
  `chat` 报文按契约送达（`source=qq` / `sender` / `content` / `server`）。
- 测试替身 `AstrMessageEvent.wake` 默认改为 True（贴合真实内核语义）并补
  `is_at_or_wake_command`：谁再拿 `is_wake_up()` 当门禁都会被单测抓出来。
- README 新增「群消息没转发到 MC？」逐条日志对照表。

## v0.5.1 (2026-09-25)

**修复：QQ → MC 转发失败时静默丢弃——群里没反应、日志里也查不出原因。**

- 新增 `_chat_forward_block()`：把「为什么没转发」抽成可判定的原因码
  （`interop_off` / `chat_sync_off` / `group_denied` / `blacklisted` / `no_target`），可单测。
- 每种原因都写日志并按 60 秒去重（`_log_throttled`），不再无声无息，例如
  `[MCWL] 群消息未转发：群服互联未启用（配置页打开「启用群服互联」并重载插件）`。
- 转发结果留痕：成功 = `[MCWL] 群消息已转发到 生存服（Steve，12 字）`；
  失败（链路未连接、消息已丢弃）= 警告并指向 `/status` 与插件页面「连通测试」。
- 唤醒指令仍静默跳过（指令消息不该转发，也不该刷屏）。
- 测试：单元 +13、联调 +11，新增 `scenario_chat_forward`——真连假模组验证 `chat`
  报文按契约送达（source/sender/content/server），以及未连接时返回失败标记而非假装成功。
- 测试替身 `astrbot_stub.logger` 补 `debug/info/warning/error`（真实内核导出的是 Logger 实例）。

## v0.5.0 (2026-09-25)

**新增**
- **插件页面「连通测试」**：以前要判断「插件到底连上服务端没有」只能翻日志。现在 WebUI 里
  **插件页面 → 连通测试**，每台服务器一行（名字 / `ws_url` / 常驻连接当前状态），行尾一个「测试」按钮，
  上面还有「全部测试」（并发 3，逐行出结果）。
  测试逻辑：**临时另开一条 WS** → 建连 → 发认证（与常驻链路同一套 token / 前缀 / 加密 / `proto_version`）
  → 等 `auth_result`，读完即关：**不影响正在运行的长连接，也不推白名单**；
  `interop_enabled=false` 时照样能测（先测通再开）。
- **失败逐阶段报因**，不再糊成一句「连接失败」：配置（没填 `ws_url` / 协议不是 `ws://`）、
  建连（TCP 连不上 / 域名解析失败 / HTTP 404 路径不对 / 握手 401·403 / 超时）、
  认证（token 不一致 / 协议版本不一致 / 服务端禁用 / **连上了但没应答** / 收到解不开的包 / 被服务端关闭），
  每条都带「处理建议」。
- 新增插件页面接口（注册路由带插件名前缀，内核按 `/api/plug/astrbot_plugin_mc_whitelist/<路由>` 分发）：
  `GET /servers`、`POST /test/all`、`POST /test/<服务器名>`。

**修复**
- 自检读取常驻连接状态时，原先会走 `data_manager` 的落库快照（且在无 `data_manager` 的环境直接抛异常），
  改为直接读链路对象的实时属性。

**兼容性**
- `proto_version` 仍为 1，配置文件字段未增删，模组侧**无需**跟版本。

## v0.4.1 (2026-09-24)

**修复**
- **配置页文字大面积看不清**：AstrBot 配置页把 `description` 渲染成**单行标题**（`nowrap` + 省略号），
  原先塞进 `description` 的长句说明全被截断成「…」。现在标题只留短标签（≤30 半角单位），
  说明改放副标题字段 `hint`（前端用 `innerHTML` 渲染），关键项加 `obvious_hint`（前端显示 ‼️）；
  完整说明仍在 README。
- 新增回归测试 `test_schema_text`：逐项校验配置文案宽度、`hint` 长度与 HTML 合法性，
  防止再把长句写回标题。

## v0.4.0 (2026-09-24)

**新增**
- **背景图直接在 WebUI 上传 / 删除**：新增 `file` 类型配置项 `background_images`（AstrBot 原生支持，
  点按钮上传，支持多张、`png/jpg/jpeg/webp`，删除也在同一处），不用再进文件夹手动增删。
  轮询顺序 = 上传顺序；没上传时回落到 `background_images_dir` 目录方式，都没有则纯色底。
  上传的文件由内核存在 `<AstrBot 根>/data/plugin_data/astrbot_plugin_mc_whitelist/files/background_images/`。
- 背景图解析做了健壮性处理：配置里残留的失效路径、非图片文件、路径穿越一律安静跳过（WebUI 删图后配置可能留下旧值）。

**变更（配置项）**
- 移除 `super_admin_qqs`（超级管理员 QQ 列表）：它等价于「群主」，内核里写死即可。
  权限语义不变 —— 群主 / 群管理员自动拥有全部权限节点，`admin_qqs` 依旧可配。
  改为在 `admin_qqs` 的描述里加括号说明「群主自动拥有全部权限节点，不用在此填写」。
  历史配置里残留的 `super_admin_qqs` 会被忽略，不再生效。
- 协议未变（`proto_version` 仍为 1），模组侧无需跟版本。

## v0.3.1 (2026-09-24)

**修复**
- 统计图片里玩家名前的 🎮 emoji 在 msyh / 思源等中文字体下渲染成豆腐块（□）。
  改为「色条 + 文字」，图片渲染不再依赖任何 emoji 字体（`services/stats_image.py`）。

**新增**
- `tests/test_real_kernel.py`：用 AstrBot 自带 Python 在**真实内核**（4.25.2）上做加载验证 ——
  metadata 走真实 loader、10 条指令与全部别名注册进内核 handler 注册表、插件类可实例化、
  `_conf_schema.json` 默认值通过 AstrBot 自己的配置校验器。
- `tests/run_all.py`：一键跑全部测试（`--with-kernel` 追加真实内核那套）。
- `README.md` / `CHANGELOG.md` / `requirements.txt`。

**重构**
- 版本号单一来源：新增 `core/version.py`，`main.py` / `interop/client.py` / `services/uuid.py`
  不再各自硬编码（此前 auth 报文里的 `version` 与 UUID 查询的 `User-Agent` 会报旧版本号）。
  真实内核测试里加了防漂移检查：除 `core/version.py` 外任何文件写死版本号即失败。
- 真实内核测试会把 `ASTRBOT_ROOT` 重定向到临时目录 —— 内核未打包时默认拿 CWD 当根目录，
  会在插件目录里生成 `data/cmd_config.json`、`data/t2i_templates/` 等运行时文件污染工程。

## v0.3.0 (2026-09-24)

**契约冻结版**（插件与模组版本号绑定递增）

- 外层信封统一为 `{type, seq, msg_id, proto_version, encrypted|prefix+data, timestamp}`，明文模式同样套外层。
- `msg_id`（UUID4）+ LRU 去重落地（容量可配，默认 512）；`proto_version=1` 不一致时拒绝并停止重连。
- `auth_result` 字段冻结为 `success / server_name / online_players / proto_version / reason`。
- 新增 `whitelist_ack`：模组应用成功后回执，插件据此清 `pending_sync`。
- 白名单只推全量（`action: "full"`），条目冻结为 `{name, uuid, source, qq}`，`正→MOJANG`、`皮→LITTLESKIN`。
- 前缀校验收紧：非 `none` 模式缺失前缀即拒收。
- `/info`、`/status` 的服务器编号 = 配置顺序且**跳过 `enabled:false`**（契约 B8）。
- 统计字段拆为 `last_login` + `data_updated_at`；玩家无记录返回 `data:null` → 图片显示「无数据」。
- 心跳 30s，3×（默认 90s）未收到 `server_status` 判离线并标待同步（契约 B5）。
- 绑定读-改-写加 `asyncio.Lock`（契约 B7）；用户名唯一性判定不区分大小写（契约 B9）。
- 权限：内核无权限节点 API 时按 `super_admin_qqs` / `admin_qqs` / 群管理员 / `permission_defaults` 降级判定。
- 测试：mock 模组联调（`tests/mock_mod_server.py`）+ 协议/单元/互联三套自测，合计 136 项。

## v0.2.0 (2026-09-24)

**开发起点**

- 模块骨架：`data_manager.py`（KV 绑定与唯一性）、`interop/client.py`（每服一条 WS 长连接、认证、指数退避重连、离线补推、并发推送）、`interop/convert.py`（消息格式转换）、`core/protocol.py`（信封与加解密）、`core/crypto.py`（AES-128-ECB）、`services/uuid.py`（Mojang / LittleSkin 查询）、`services/stats_image.py`（Pillow 统计图）、`services/perm.py`（权限降级层）。
- 指令：注册 / 注销 / 迁移 / 更新昵称 / info / black / sync / status / mc，全部带中文短别名。
