"""单元测试包。

跑法（在仓库根目录）：

    python -m unittest discover -s tests -t . -v

调试某个模块时：

    python -m unittest tests.test_balancer -v
"""

import logging

# 测试输出里不需要代理的诊断日志。
#
# 每次重试失败都会打一条 WARNING、写日志失败会打一条 ERROR 带堆栈——
# 那些都是**测试故意触发**的，属于预期行为，混在结果里只会干扰阅读。
# 断言本身已经把该验的都验了，日志在这里没有额外信息量。
#
# 排查问题的时候把下面两行注释掉、或者改成 logging.DEBUG，就能看到全量日志。
logging.getLogger("proxy").setLevel(logging.CRITICAL + 1)
logging.getLogger("proxy").addHandler(logging.NullHandler())
