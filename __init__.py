"""advisor 插件入口：把 :mod:`advisor.advisor` 的注册函数交给 Hermes 插件加载器。"""

from __future__ import annotations

from .advisor import register

__all__ = ["register"]
