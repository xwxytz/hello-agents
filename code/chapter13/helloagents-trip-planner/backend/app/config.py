"""配置管理模块"""

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

from .services.logging_setup import logger

# 加载环境变量
# 首先尝试加载当前目录的.env (override=True 确保覆盖系统环境变量)
load_dotenv(override=True)
# 然后尝试加载HelloAgents的.env(如果存在)
helloagents_env = Path(__file__).parent.parent.parent.parent / "HelloAgents" / ".env"
if helloagents_env.exists():
    load_dotenv(helloagents_env, override=True)  # 覆盖已有的环境变量


class Settings(BaseSettings):
    """应用配置"""
    # 应用基本配置
    app_name: str = "HelloAgents智能旅行助手"
    app_version: str = "1.0.0"
    debug: bool = False

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # CORS配置 - 使用字符串,在代码中分割
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # 高德地图API配置
    amap_api_key: str = ""

    # Unsplash API配置
    unsplash_access_key: str = ""
    unsplash_secret_key: str = ""

    # LLM配置 (从环境变量读取,由HelloAgents管理)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.deepseek.com/v1"
    openai_model: str = "deepseek-chat"

    # 日志配置
    log_level: str = "INFO"

    # ---- 缓存与成本控制 ----
    # 缓存 TTL 按数据新鲜度要求分档：天气一天内多次更新、POI 几乎不变。
    # 统一用一个短 TTL 会白白多打上游；统一用长 TTL 会让天气失真。
    cache_weather_ttl: int = 3 * 3600      # 天气：3 小时
    cache_poi_ttl: int = 24 * 3600         # POI / 地理编码 / POI 详情：24 小时
    cache_route_ttl: int = 3600            # 路线：1 小时（受实时路况影响，但仍以静态路径为主）
    cache_default_ttl: int = 300           # 其他工具：5 分钟
    # 单组缓存条目上限。不设上限的话，任意关键词组合都会永久占位，内存只增不减
    cache_max_entries: int = 512

    # 单次规划的预算上限。作用不是防止慢，而是防止「异常输入 → 重试/长输出 → 账单失控」。
    # 注意：这是协作式检查（在阶段之间检查是否超时），Python 无法安全地强杀一个正在
    # 阻塞调用里的线程，所以它不是硬中断。
    plan_timeout_s: int = 180
    plan_token_budget: int = 200_000

    # 输入长度上限。free_text_input 会被拼进 prompt，不限长度等于把 token 消耗权交给调用方
    max_text_input_len: int = 2000

    # ---- 鉴权与限流 ----
    # 允许访问的 API Key，逗号分隔。留空 = 鉴权关闭（本地开发默认放行）。
    # 注意：这里存的是明文，因为它必须从环境变量进来的明文比对——
    # 与 AMAP_API_KEY 一样属于运行时密钥，不要写进任何文件、不要进版本库。
    api_keys: str = ""
    # 是否强制鉴权。为 True 但没配任何 Key 属于配置错误，启动时直接报错（见 validate_config）
    auth_required: bool = False
    # 是否信任 X-Forwarded-For。只有在反向代理后面才该打开：直接暴露公网时
    # 任何人都能伪造这个头，限流等于形同虚设。
    trust_proxy: bool = False

    # ---- 行程持久化（P2-3）----
    # 默认 SQLite 单文件，零部署成本；换 PostgreSQL 只改这一行（SQLAlchemy 方言）
    trip_db_url: str = "sqlite:///./data/trips.db"
    # 分享链接是否公开只读。true = 拿到链接就能看行程（不需要 API Key），
    # 这也是「分享」这个词的意义所在；false = 必须带 Key 才能读。
    share_public_read: bool = True

    rate_limit_enabled: bool = True
    # 规划类接口（一次要打 7 次 LLM + 多次 MCP）单独一档，额度远低于普通查询
    rate_limit_plan_per_min: int = 10
    rate_limit_default_per_min: int = 60
    rate_limit_window_s: int = 60

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # 忽略额外的环境变量

    def get_cors_origins_list(self) -> List[str]:
        """获取CORS origins列表"""
        return [origin.strip() for origin in self.cors_origins.split(',')]

    def get_api_key_set(self) -> set:
        """解析出已配置的 API Key 集合（空串与重复项会被剔除）"""
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def auth_enabled(self) -> bool:
        """
        鉴权是否真正生效。

        只有「打开开关 **且** 至少配了一个 Key」才算生效。开了开关却没配 Key
        是配置错误，在 validate_config() 里直接拦下来，而不是悄悄放行——
        悄悄放行会让人以为接口是受保护的。
        """
        return bool(self.auth_required and self.get_api_key_set())


# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


# 验证必要的配置
def validate_config():
    """验证配置是否完整"""
    errors = []
    warnings = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY未配置")

    # 开了鉴权却没配 Key：要么被拦在这里，要么变成"所有人都能过"的假保护
    if settings.auth_required and not settings.get_api_key_set():
        errors.append("AUTH_REQUIRED=true 但未配置任何 API_KEYS（接口会变成无人可访问）")

    # HelloAgentsLLM会自动从LLM_API_KEY读取,不强制要求OPENAI_API_KEY
    llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not llm_api_key:
        warnings.append("LLM_API_KEY或OPENAI_API_KEY未配置,LLM功能可能无法使用")

    if errors:
        error_msg = "配置错误:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    if warnings:
        for w in warnings:
            logger.warning(f"⚠️  配置警告: {w}")

    return True


# 打印配置信息(用于调试)
def print_config():
    """打印当前配置(隐藏敏感信息)"""
    llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    llm_base_url = os.getenv("LLM_BASE_URL") or settings.openai_base_url
    llm_model = os.getenv("LLM_MODEL_ID") or settings.openai_model

    logger.bind(
        app_name=settings.app_name,
        version=settings.app_version,
        server=f"{settings.host}:{settings.port}",
        amap_key_configured=bool(settings.amap_api_key),
        llm_key_configured=bool(llm_api_key),
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        log_level=settings.log_level,
        cache_poi_ttl=settings.cache_poi_ttl,
        cache_weather_ttl=settings.cache_weather_ttl,
        cache_max_entries=settings.cache_max_entries,
        plan_timeout_s=settings.plan_timeout_s,
        plan_token_budget=settings.plan_token_budget,
        max_text_input_len=settings.max_text_input_len,
        auth_enabled=settings.auth_enabled,
        api_keys_count=len(settings.get_api_key_set()),
        rate_limit_enabled=settings.rate_limit_enabled,
    ).info("⚙️ 当前配置（密钥仅显示是否已配置）")

    if not settings.auth_enabled:
        logger.warning(
            "⚠️ 鉴权未启用（API_KEYS 为空或 AUTH_REQUIRED=false）："
            "接口对任何能访问到它的人开放，LLM 与高德配额可能被刷爆。"
            "部署到公网前请配置 API_KEYS 并将 AUTH_REQUIRED 设为 true"
        )

