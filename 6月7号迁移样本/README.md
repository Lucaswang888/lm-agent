# 6月7号迁移样本

2026-06-07，用 agent 以**最小输入（仅 source 库 / target 库 / 项目路径，`--input-boundary non-oracle`，
不含 PyMigBench ground truth、不含历史示例）**迁移的两个样本的**迁移后代码**。
这两个都属于「code-change 判据通过、但项目自带测试未通过」的情况，但**根因不同**：

## bley_ipaddr-to-ipaddress/  （evgeni/bley，ipaddr → ipaddress）
- 状态：最小输入版，**自带测试失败**（5 passed / 1 skipped / 3 errors）。
- 根因：**迁移代码缺陷**。py2.7 上 `ipaddress.ip_address('127.0.0.1')` 要求 unicode，
  agent 只换了 API 名、漏了 `unicode()` 参数转换 → `AddressValueError`。
- 注：开启 L4 行为 diff（喂真实 py2.7 运行时命令）后，agent 能据此自行补上 `unicode()` 修复。

## flintrock_pep8-to-pycodestyle/  （nchammas/flintrock，pep8 → pycodestyle）
- 状态：迁移后代码，**自带测试 `test_pep8_compliance` 失败**（报 25 个 E252）。
- 根因：**环境/版本问题，非迁移代码缺陷**。`pep8 → pycodestyle` 改名本身正确；该测试是
  “用 linter 跑全代码、断言 0 违规”，强依赖 pycodestyle 版本——2024 版含 E252（2018 才加），
  用它查 2016 年代码就报 25 个；钉到同期版本即可大幅减少。属可通过重配环境解决的一类。
