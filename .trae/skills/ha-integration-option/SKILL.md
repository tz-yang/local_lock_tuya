---
name: ha-integration-option
description: 为 Home Assistant 自定义集成新增配置项的标准流程，覆盖 const、config_flow、coordinator、translations 与实体消费端联动。用户要求给 custom_components 集成添加设置项、开关、DP 编号配置或选项流时使用。不用于修复集成 Bug 或从零创建集成。
---

# HA 自定义集成新增配置项标准流程

面向 `custom_components/<domain>/` 下的 config entry 集成。目标是让一个新配置项从「设置 → 设备与服务 → 配置」界面一直贯通到实体行为，五处改动缺一不可。

## 第 0 步：先搞清配置项的类型和默认值

动手前明确三件事，避免反复改 schema：

1. **类型**：整数（DP 编号、秒数）、布尔（开关）、短字符串、多行文本
2. **默认值**：首次添加设备时没有 options，默认值必须在 const 里声明，消费端也要能兜底
3. **必填还是可选**：有默认值的开关/编号用 `vol.Required(..., default=...)`；允许留空的用 `vol.Optional(..., default="")`

## 第 1 步：const.py —— 声明键名和默认值

```python
# 配置键（字符串值会存进 config entry）
CONF_DOORBELL_DP = "doorbell_dp"
CONF_BROADCAST_HISTORY = "broadcast_history"

# 关联常量（上限、默认值等）
BROADCAST_HISTORY_MAX = 200
```

- 一个配置项至少一个 `CONF_XXX` 常量；配套的上限/默认值同文件声明
- 键名用 snake_case，这是实际写入 entry.data/options 的 key

## 第 2 步：config_flow.py —— 加入 schema

在 `OPTIONS_SCHEMA`（已有设备的「配置」弹窗）加键，并把常量补进文件顶部 import：

```python
vol.Required(CONF_DOORBELL_DP, default=0): vol.All(
    vol.Coerce(int), vol.Range(min=0)
),
vol.Optional(CONF_BROADCAST_HISTORY, default=False): bool,
```

注意区分两个 schema：

- `USER_SCHEMA`：首次添加设备（设备 ID、local_key、IP 等连接信息）
- `OPTIONS_SCHEMA`：设备行为参数（DP 映射、开关、间隔）。**本流程改的是这个**

选项流保存后，若 `__init__.py` 注册了 `entry.add_update_listener(_async_update_listener)` 且监听器调用 `async_reload`，提交即触发集成重载——这是预期行为，不是 Bug。

## 第 3 步：coordinator（或 __init__）—— 读取配置

config entry 的数据分两层，必须合并后读取，否则 options 不生效：

```python
merged = {**entry.data, **entry.options}
self._history_enabled: bool = merged.get(CONF_BROADCAST_HISTORY, False)
```

- `entry.data`：首次配置的连接信息
- `entry.options`：选项流保存的值，同名键以 options 为准
- `merged.get(KEY, 默认值)` 永远带兜底，兼容升级前已存在的旧条目

## 第 4 步：translations —— 界面文案

三个文件保持同步，缺一就会在对应语言/回退路径显示英文或 key 原文：

| 文件 | 用途 |
|---|---|
| `translations/zh-Hans.json` | 简体中文（HAOS 中文界面读这个） |
| `translations/en.json` | 英文 |
| `strings.json` | 开发基准文件，结构与翻译文件一致 |

每个配置项两处文案：

```json
{
  "options": {
    "step": {
      "init": {
        "data": {
          "doorbell_dp": "门铃 DP 编号（0 = 不启用）"
        },
        "data_description": {
          "doorbell_dp": "门铃功能点（doorbell，Boolean 型）的 DP 编号。填 0 表示不启用。"
        }
      }
    }
  }
}
```

- `data`：字段标签，短
- `data_description`：字段下方说明，可写格式、示例、取值含义
- 改完必须校验 JSON 合法性（见第 6 步），逗号/引号错误会导致整个翻译文件不加载

## 第 5 步：消费端 —— 让配置真正影响实体

两种典型用法：

**A. 按配置决定是否创建实体**（平台文件的 `async_setup_entry`）：

```python
if coordinator._data.get(CONF_BROADCAST_HISTORY):
    entities.append(MySensor(coordinator, entry))
```

**B. 实体行为里读取**（属性、操作校验等）：

```python
if not self.coordinator.some_option_enabled:
    raise HomeAssistantError("请先在设置中开启该功能")
```

平台文件从 `hass.data[DOMAIN][entry.entry_id]` 拿 coordinator，通过它暴露配置，不要自己再读一遍 entry。

## 选择器配方速查

```python
# 整数编号（0 表示不启用的惯例）
vol.Required(CONF_X, default=0): vol.All(vol.Coerce(int), vol.Range(min=0))

# 正整数（秒数，下限按业务定）
vol.Required(CONF_INTERVAL, default=60): vol.All(
    vol.Coerce(int), vol.Range(min=5)
)

# 布尔开关
vol.Optional(CONF_FLAG, default=False): bool

# 可留空的短字符串（DP 列表，逗号分隔）
vol.Optional(CONF_DP_LIST, default=""): str
```

**多行文本必须用 TextSelector**，单行文本框里按回车会直接提交表单：

```python
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

vol.Optional(CONF_USER_MAP, default=""): TextSelector(
    TextSelectorConfig(multiline=True, type=TextSelectorType.TEXT)
),
```

## 第 6 步：校验（每次改完必做）

在 Windows 工作区用本地 Python（路径按实际版本调整）：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m py_compile `
  "<集成目录>\const.py" "<集成目录>\config_flow.py" `
  "<集成目录>\coordinator.py" "<集成目录>\sensor.py"

& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -c `
  "import json; [json.load(open(p, encoding='utf-8')) for p in [r'<目录>\translations\zh-Hans.json', r'<目录>\translations\en.json', r'<目录>\strings.json']]; print('OK')"
```

部署验证流程（在 HA 侧）：

1. 重载集成或重启 HA
2. 打开设备「配置」弹窗，确认字段标签和说明显示正确
3. 修改新配置项 → 提交 → 确认集成重载、实体行为按预期变化
4. 若改动不生效，先查 `merged = {**entry.data, **entry.options}` 是否漏了 options 层

## 高频坑清单

1. **DP 编号 vs DP 值混淆**（涂鸦类集成最常见）：配置项要的是功能点编号（如 DP 1），设备快照里该键的值（如 2 = 2 号指纹）是运行时数据。配置说明里必须写清"填功能点编号，不是成员编号"
2. **只改了 USER_SCHEMA 没改 OPTIONS_SCHEMA**：已有设备永远看不到新字段
3. **消费端直接读 `entry.data`**：options 保存了但行为不变
4. **没给默认值兜底**：升级用户的旧条目没有该键，`merged[KEY]` 直接 KeyError
5. **多行映射用单行输入框**：用户一按回车表单就提交。需要"每行一条"的配置一律 TextSelector multiline
6. **翻译只改中文不改英文/strings.json**：结构不同步时容易漏逗号，且切换语言后回退异常
7. **去重/防抖窗口拍脑袋定值**：时间类窗口（如事件去重秒数）应让用户实际操作一轮，用真实间隔反推
8. **忘记校验 JSON**：翻译文件一个多余逗号就让全部文案静默回退，排查成本高

## 进阶模式（按需阅读）

涉及调试日志声明、广播/数据持久化到 `.storage`、涂鸦 DP 编号发现方法时，见 [references/advanced-patterns.md](references/advanced-patterns.md)。
