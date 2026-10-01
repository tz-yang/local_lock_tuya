# 进阶模式参考

本文件是 ha-integration-option 技能的按需参考，包含调试日志、磁盘持久化、涂鸦 DP 发现三类高频扩展模式。代码片段均来自 tuya_local_lock 集成的实际实现。

## 1. 让集成支持「启用调试日志」界面按钮

HA 界面里集成卡片右上角的「启用调试日志」只会对 `manifest.json` 中 `loggers` 声明的 logger 生效。

manifest.json：

```json
{
  "loggers": ["tinytuya"]
}
```

要点：

- 集成自身代码用模块级 logger：`_LOGGER = logging.getLogger(__name__)`，其名称形如 `custom_components.<domain>.xxx`，启用调试时整个命名空间一并进入 DEBUG，无需在 `configuration.yaml` 配置
- 第三方库（如 tinytuya）要在 loggers 里显式列出，界面开关才会影响它
- DEBUG 日志要有明确的排障价值再打，避免高频循环里无意义刷屏；大字段（hex）只在 DEBUG 分支构造：

```python
if _LOGGER.isEnabledFor(logging.DEBUG):
    _LOGGER.debug("收到本设备广播（来自 %s）: %s", addr[0], payload)
```

`configuration.yaml` 永久开启只在用户想重启不丢级别时才需要：

```yaml
logger:
  logs:
    custom_components.<domain>: debug
```

## 2. 配置驱动的磁盘持久化（.storage）

适用场景：捕获的报文/事件需要 HA 重启后保留。标准结构如下。

### 2.1 存储路径与内存缓冲

```python
import json
import os
from collections import deque

# const.py
CONF_BROADCAST_HISTORY = "broadcast_history"
BROADCAST_HISTORY_MAX = 200

# coordinator __init__
self._history_enabled = merged.get(CONF_BROADCAST_HISTORY, False)
self._history: deque[dict] = deque(maxlen=BROADCAST_HISTORY_MAX)
self._history_save_unsub = None
self._history_loaded = False
self._history_file = hass.config.path(
    ".storage", f"<domain>_history_{self._device_id}.json"
)
```

### 2.2 启动时恢复

由 `__init__.py` 的 `async_setup_entry` 在启动监听前调用 `await coordinator.async_load_history()`：

```python
async def async_load_history(self) -> None:
    if self._history_loaded:
        return
    self._history_loaded = True
    if not self._history_enabled:
        return

    def _read():
        try:
            with open(self._history_file, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            _LOGGER.exception("历史文件读取失败: %s", self._history_file)
            return None

    rows = await self.hass.async_add_executor_job(_read)
    if rows:
        self._history.extend(rows[-BROADCAST_HISTORY_MAX:])
```

### 2.3 写入防抖 + 原子落盘

连续数据（如每 5 秒一条广播）合并为一次写盘，5 秒后执行：

```python
@callback
def _schedule_history_save(self) -> None:
    if self._history_save_unsub is not None:
        return
    self._history_save_unsub = async_call_later(self.hass, 5, self._async_save_history)

async def _async_save_history(self, _now) -> None:
    self._history_save_unsub = None
    rows = list(self._history)

    def _write() -> None:
        tmp = f"{self._history_file}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
        os.replace(tmp, self._history_file)  # 原子替换，断电不产生半截文件

    try:
        await self.hass.async_add_executor_job(_write)
    except OSError:
        _LOGGER.exception("历史写入失败")
```

### 2.4 shutdown 立即补写

集成重载/关机会丢掉防抖窗口内最后几条，`async_shutdown` 里取消防抖并同步落盘一次。

### 2.5 设计原则

- 所有阻塞式文件 IO 走 `hass.async_add_executor_job`，绝不占事件循环
- `.storage/` 是 HA 的私有存储目录，文件名带 domain 和设备 ID 防冲突
- 内存 deque 与磁盘同上限；只持久化真正需要的数据条目，不存原始大对象全量
- 关闭开关即停止写入，但不主动删除历史文件（用户数据保留原则）

## 3. 暴露持久化数据给传感器

传感器在以下两种情况任一开启时创建：内存调试捕获或磁盘历史。开启历史时属性切换为完整持久化记录：

```python
@property
def native_value(self) -> int:
    if self.coordinator.broadcast_history_enabled:
        return len(self.coordinator.broadcast_history)
    return len(self.coordinator.raw_captures)

@property
def should_poll(self) -> bool:
    # 数据来自被动监听、coordinator 更新流程不感知时，需要周期拉取属性
    return True

@property
def available(self) -> bool:
    # coordinator 因设备休眠而失败时，被动捕获的数据仍应可查看
    return True
```

## 4. 事件总线：让自动化捕获每一次事件

值变化型 DP 在"同值重复事件"场景下无法用状态触发器区分，配合主动 fire 事件最可靠：

```python
self.hass.bus.async_fire(
    f"{DOMAIN}_unlock_event",
    {"dp": dp, "raw_value": current, "display": display},
)
```

自动化写法：

```yaml
trigger:
  - platform: event
    event_type: <domain>_unlock_event
action:
  - service: notify.mobile_app_xxx
    data:
      message: "{{ trigger.event.data.display }} 开锁"
```

事件名和数据字段要在集成文档/配置说明里公开，事件 data 只放可序列化的基础类型。

## 5. 涂鸦门锁 DP 编号发现方法

配置 DP 类选项时，用户不知道编号是常态，按以下优先级获取：

1. **涂鸦开发者平台 → 设备 → 设备日志**：选「数据上报」记录，功能点列显示功能点名称（如"指纹解锁"），事件详情显示上报值；注意设备日志默认只展示功能点名称，dpId 数字需在「功能定义/设备调试」的功能点列表里对照
2. **功能点列表（设备调试页）**：每个功能点的 code（如 `unlock_fingerprint`、`doorbell`）、类型（Integer/Boolean）和取值范围，dpId 在行详情中
3. **本地 TCP 快照对照实测**（最可靠的本地证据）：设备唤醒后 `device.status()` 返回的 `dps` 字典，键即 dpId：
   - 刷指纹 → 找值变化的 Integer 键
   - 按门铃 → 找值为 true 的 Boolean 键
   - 换电池/低电 → 找值为百分比的键
4. 做配置说明时写明"填数字 dpId"，并给出"0 = 不启用"的惯例，避免用户把成员编号（DP 的值）误填进 DP 编号字段
