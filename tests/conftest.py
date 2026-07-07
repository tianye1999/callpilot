"""pytest 共享配置：让测试能导入 tests/fakes。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
