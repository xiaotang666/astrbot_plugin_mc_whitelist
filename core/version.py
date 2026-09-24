"""插件版本号唯一来源。

规则（与模组同步递增，见 docs/接口契约冻结_v10.3.md §5）：
    Bug 修复 / 小更新        Z+1
    新增功能、字段、端点      Y+1（向后兼容）
    协议或配置不兼容          X+1（插件与模组必须同时升级）

为什么单独一个文件：版本号以前散在 main.py / interop/client.py / services/uuid.py 三处，
发版时漏改一处就会让 auth 报文里的 version 和 User-Agent 报旧版本。
现在只在这里维护，metadata.yaml 与 CHANGELOG.md 跟着改；
tests/test_real_kernel.py 会核对 main.PLUGIN_VERSION == metadata.yaml version。
"""

PLUGIN_VERSION = "10.4.0"

__all__ = ["PLUGIN_VERSION"]
