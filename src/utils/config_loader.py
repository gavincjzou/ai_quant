"""
Config Loader - 配置文件加载器
读取 YAML 配置并提供类型安全的访问接口。
"""

import os
from typing import Any, Dict, List, Optional

import yaml
from loguru import logger


class ConfigLoader:
    """YAML 配置加载器"""

    _instances: Dict[str, "ConfigLoader"] = {}

    def __init__(self, config_dir: Optional[str] = None):
        if config_dir is None:
            config_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "config",
            )
        self.config_dir = config_dir
        self._cache: Dict[str, dict] = {}

    @classmethod
    def get_instance(cls, config_dir: Optional[str] = None) -> "ConfigLoader":
        """单例模式获取加载器"""
        key = config_dir or "default"
        if key not in cls._instances:
            cls._instances[key] = cls(config_dir)
        return cls._instances[key]

    def load(self, filename: str) -> dict:
        """
        加载指定的 YAML 配置文件。
        
        Args:
            filename: 配置文件名（不含路径），如 "settings.yaml"
            
        Returns:
            解析后的字典
        """
        if filename in self._cache:
            return self._cache[filename]

        filepath = os.path.join(self.config_dir, filename)
        if not os.path.exists(filepath):
            logger.warning(f"Config file not found: {filepath}")
            return {}

        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        self._cache[filename] = data
        logger.debug(f"Loaded config: {filename}")
        return data

    def get_settings(self) -> dict:
        """加载全局设置"""
        return self.load("settings.yaml")

    def get_strategies_config(self) -> dict:
        """加载策略配置"""
        return self.load("strategies.yaml")

    def get_risk_config(self) -> dict:
        """加载风控配置"""
        return self.load("risk.yaml")

    def get(self, filename: str, key_path: str, default: Any = None) -> Any:
        """
        按点分路径获取配置值。
        
        Args:
            filename: 配置文件名
            key_path: 点分路径，如 "market.timezone"
            default: 默认值
            
        Returns:
            配置值
        """
        data = self.load(filename)
        keys = key_path.split(".")
        for key in keys:
            if isinstance(data, dict) and key in data:
                data = data[key]
            else:
                return default
        return data

    def reload(self, filename: Optional[str] = None):
        """重新加载配置（清除缓存）"""
        if filename:
            self._cache.pop(filename, None)
        else:
            self._cache.clear()
        logger.info(f"Config cache cleared: {filename or 'all'}")
