"""QQ 群精华消息本地存档应用的核心模块包。

模块职责划分：

- :mod:`core.qq_client`  网络层，负责会话、UA、bkn 计算、页面与群列表请求；
- :mod:`core.parser`     解析层，负责 ``window.__INITIAL_STATE__`` 提取与消息归一化；
- :mod:`core.storage`    存储层，负责本地快照目录、索引与 Cookie 配置；
- :mod:`core.downloader` 下载层，负责图片并发下载与去重；
- :mod:`core.exporter`   导出层，负责 HTML / Markdown / JSON 三种归档格式。
"""

from __future__ import annotations

__all__ = [
    "downloader",
    "exporter",
    "parser",
    "qq_client",
    "storage",
]

__version__ = "2.0.0"
