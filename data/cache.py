"""
统计指标缓存模块
缓存频率统计、冷热号等计算结果，避免每次 train/predict 都全量重算
缓存按数据更新时间戳判断是否过期
"""

import hashlib
import json
import logging
import os
import pickle
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from config import CACHE_DIR

logger = logging.getLogger(__name__)


class StatsCache:
    """
    统计缓存管理器
    缓存键由 lottery_name + 参数哈希组成
    每个缓存条目包含：计算结果 + 数据时间戳 + 缓存创建时间
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.pkl"

    def _make_key(self, lottery_name: str, params: Optional[dict] = None) -> str:
        """生成缓存键"""
        parts = [lottery_name]
        if params:
            param_str = json.dumps(params, sort_keys=True)
            param_hash = hashlib.md5(param_str.encode()).hexdigest()[:12]
            parts.append(param_hash)
        return "_".join(parts)

    def _get_data_timestamp(self, lottery_name: str) -> Optional[datetime]:
        """
        获取原始数据文件的最新修改时间
        作为判断缓存是否过期的依据
        """
        # 动态导入避免循环；使用 loader 的通用路径推导，支持数字型/七星彩
        from data.loader import _file_path

        path = _file_path(lottery_name)
        if path and path.exists():
            mtime = os.path.getmtime(path)
            return datetime.fromtimestamp(mtime)
        return None

    def get(
        self, lottery_name: str, params: Optional[dict] = None
    ) -> Optional[Any]:
        """
        读取缓存
        如果数据文件已更新则返回 None（缓存过期）
        """
        key = self._make_key(lottery_name, params)
        cache_path = self._cache_path(key)

        if not cache_path.exists():
            return None

        data_timestamp = self._get_data_timestamp(lottery_name)
        if data_timestamp is None:
            logger.debug(f"无法获取数据时间戳，跳过缓存: {key}")
            return None

        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)

            # 检查数据是否更新
            cached_data_ts = cached.get("data_timestamp")
            if cached_data_ts and cached_data_ts >= data_timestamp:
                logger.debug(f"缓存命中: {key}")
                return cached.get("result")
            else:
                logger.debug(f"缓存过期: {key}")
                return None
        except (pickle.UnpicklingError, EOFError, KeyError) as e:
            logger.warning(f"缓存读取失败: {key} — {e}")
            return None

    def set(
        self,
        lottery_name: str,
        result: Any,
        params: Optional[dict] = None,
    ):
        """写入缓存"""
        key = self._make_key(lottery_name, params)
        cache_path = self._cache_path(key)

        data_timestamp = self._get_data_timestamp(lottery_name)
        if data_timestamp is None:
            data_timestamp = datetime.now()

        cached = {
            "result": result,
            "data_timestamp": data_timestamp,
            "cached_at": datetime.now(),
            "lottery_name": lottery_name,
            "params": params,
        }

        try:
            with open(cache_path, "wb") as f:
                pickle.dump(cached, f)
            logger.debug(f"缓存写入: {key}")
        except (pickle.PicklingError, OSError) as e:
            logger.warning(f"缓存写入失败: {key} — {e}")

    def invalidate(self, lottery_name: Optional[str] = None):
        """
        清除缓存
        如果指定 lottery_name 则只清除该彩票的缓存
        """
        cleared = 0
        for f in self.cache_dir.glob("*.pkl"):
            if lottery_name is None or f.stem.startswith(lottery_name):
                f.unlink()
                cleared += 1
        logger.info(f"缓存清除完成: {cleared} 个文件")

    def clear_all(self):
        """清除所有缓存"""
        self.invalidate()
