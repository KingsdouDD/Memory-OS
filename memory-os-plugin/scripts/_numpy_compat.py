#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
numpy 兼容补丁：让老库（qdrant_client / neo4j）在 numpy 2.0+ 下能正常 import。

老库代码：
  from numpy import bool_, int_, float_, complex_, object_, str_, int8, int16, ...
新 numpy (2.0+) 删除了这些内置别名，导致 AttributeError。

用法（必须放在所有老库 import 之前）：
  import _numpy_compat    # noqa: F401
"""

import numpy as np

# ── 标量类型别名（numpy 1.x 风格）────────────────────────
_aliases = {
    'bool_': bool,
    'int_': int,
    'float_': float,
    'complex_': complex,
    'object_': object,
    'str_': str,
    'long': int,                # Python 2 遗留
    'unicode_': str,             # Python 2 遗留
}

for name, target in _aliases.items():
    if not hasattr(np, name):
        try:
            setattr(np, name, target)
        except (AttributeError, TypeError):
            pass

# ── 整数位宽别名（老库依赖 np.int8 / np.int16 / np.int32 / np.int64 / np.uint8 等）──
# 新 numpy 仍然有这些，但某些精简版可能没有。这里用 np.dtype 构造等价类型。
for name, dtype in [
    ('int8', np.int8), ('int16', np.int16), ('int32', np.int32), ('int64', np.int64),
    ('uint8', np.uint8), ('uint16', np.uint16), ('uint32', np.uint32), ('uint64', np.uint64),
    ('float16', np.float16), ('float32', np.float32), ('float64', np.float64),
    ('bool8', bool),          # numpy 1.x 时代的 bool 别名
    ('complex64', np.complex64), ('complex128', np.complex128),
]:
    if not hasattr(np, name):
        try:
            setattr(np, name, dtype)
        except (AttributeError, TypeError):
            pass

# ── 某些库用 np.float —— Python 内建冲突，但 numpy 1.x 里有
if not hasattr(np, 'float'):
    try:
        np.float = float
    except (AttributeError, TypeError):
        pass

# ── 完成 ───────────────────────────────────────────────
# 静默成功；调用方无需关心