"""Read-only, UAT-only browser log collection."""

ALLOWED_HOSTS = frozenset({"uat.weimeta.cn", "admin-uat.weimeta.cn"})
COLLECTOR_VERSION = "uat-browser-collector-v1.0.0"
SOURCE_TYPE = "measured_uat_browser_collector"

