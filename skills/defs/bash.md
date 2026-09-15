---
name: bash
description: 在沙箱内执行受限的 shell 命令
executor: bash
parameters:
  type: object
  properties:
    command:
      type: string
      description: 要执行的命令，例如 'ls -la'
  required: [command]
---

执行白名单内的 shell 命令，工作目录固定为沙箱根目录。

白名单命令：ls, cat, head, tail, grep, find, wc, echo, pwd, date, mkdir

限制：
- 不允许管道、重定向、命令替换等危险字符
- 参数中的路径必须在沙箱内
- 默认超时 10 秒