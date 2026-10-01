DOMAIN = "tuya_local_lock"

CONF_DEVICE_ID = "device_id"
CONF_LOCAL_KEY = "local_key"
CONF_DEVICE_IP = "ip"
CONF_UUID = "uuid"
CONF_PRODUCT_ID = "product_id"
CONF_PROTOCOL = "protocol"

DEFAULT_PROTOCOL = "3.4"
PROTOCOLS = ["3.3", "3.4", "3.5"]

CONF_STATE_DP = "state_dp"
CONF_STATE_TRUE_IS_LOCKED = "state_true_is_locked"
CONF_COMMAND_DP = "command_dp"
CONF_COMMAND_LOCK_VALUE = "command_lock_value"
CONF_COMMAND_UNLOCK_VALUE = "command_unlock_value"
CONF_OPEN_DP = "open_dp"
CONF_OPEN_VALUE = "open_value"
CONF_POLL_INTERVAL = "poll_interval"
CONF_BATTERY_DP = "battery_dp"
CONF_UNLOCK_DP_LIST = "unlock_dp_list"
CONF_UNLOCK_USER_MAP = "unlock_user_map"
CONF_AUTO_RELOCK_DELAY = "auto_relock_delay"
CONF_DEBUG_CAPTURE = "debug_capture"

# 门铃 DP 编号（0 = 未启用）。涂鸦功能点 "doorbell"，Boolean 型
CONF_DOORBELL_DP = "doorbell_dp"

# 是否把广播历史持久化到磁盘（HA 重启不丢）
CONF_BROADCAST_HISTORY = "broadcast_history"
BROADCAST_HISTORY_MAX = 200

# 调试捕获环形缓冲区上限
DEBUG_CAPTURE_MAX = 50

# 门铃唤醒后允许网络开锁/上锁操作的窗口（秒），
# 每次收到唤醒广播会重置窗口
WAKE_OPERABLE_WINDOW = 20

DEFAULT_STATE_DP = 1
DEFAULT_STATE_TRUE_IS_LOCKED = True
DEFAULT_COMMAND_DP = 1
DEFAULT_COMMAND_LOCK_VALUE = True
DEFAULT_COMMAND_UNLOCK_VALUE = False
DEFAULT_OPEN_VALUE = True
DEFAULT_POLL_INTERVAL = 60
DEFAULT_BATTERY_DP = 0
DEFAULT_AUTO_RELOCK_DELAY = 0
