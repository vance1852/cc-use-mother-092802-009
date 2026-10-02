"""组合经营快照服务包。

提供啤酒高端化与白酒调整复盘共用的版本化快照能力：
指标定义/组织范围/币种/期间版本化、经营事实接收、冻结复算、
勾稽异常说明、角色会签、授权发布、迟到数据重述与权限下钻。
"""

from .service import SnapshotService, previous_period

__all__ = ["SnapshotService", "previous_period"]
