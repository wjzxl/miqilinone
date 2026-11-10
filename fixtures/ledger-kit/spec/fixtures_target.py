"""变异注入靶子。

文件名不匹配 `test_*.py`，所以 unittest discover 不会导入它；但它落在
`spec/**/*.py` 的 globs 里，结构化规则一定会扫到。注入违规代码时：
* Driving 的旅程仍然照常通过（不会因 ImportError 假失败）；
* Drive 末尾的 AST 扫描必然报出规则命中 -> DRIVE_FAIL。

这个文件平时是干净的，P4 的变异测试只在 ShadowRepo 副本里追加内容，
绝不写回此仓库。
"""

PLACEHOLDER = "clean baseline"
