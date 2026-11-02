"""fnOS 集成常量定义。"""

DOMAIN = "fnos"

# 配置项键名
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_MAC = "mac"
CONF_SCAN_INTERVAL = "scan_interval"

# 默认值
DEFAULT_PORT = 5666
DEFAULT_SCAN_INTERVAL = 60  # 秒

# 实体类型
PLATFORMS = ["sensor", "switch"]

# WebSocket 握手需携带浏览器 UA：python-websockets 默认 UA 会被服务端静默黑洞
WS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0"
)

# 数据键
DATA_CPU_USAGE = "cpu_usage"
DATA_CPU_TEMP = "cpu_temp"
DATA_CPU_NAME = "cpu_name"
DATA_MEM_TOTAL = "mem_total"
DATA_MEM_USED = "mem_used"
DATA_MEM_FREE = "mem_free"
DATA_MEM_PERCENT = "mem_percent"
DATA_NET_RECEIVE = "net_receive"
DATA_NET_TRANSMIT = "net_transmit"
DATA_GPU_USAGE = "gpu_usage"
DATA_HOSTNAME = "hostname"
DATA_ONLINE = "online"
