"""跨模块并发原语。

资源配置与运行实例都会修改并发占用与回填积压数据，两处入口必须共用同一把锁，
才能保证「检查剩余槽位 → 占用槽位」这类复合动作的原子性。
"""
from __future__ import annotations

import threading

# 资源池准入、占用重算、回填状态流转的全局串行锁。
# RLock 允许同一线程在重算流程里嵌套调用准入逻辑。
resource_lock = threading.RLock()
