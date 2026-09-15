---
name: file_read
description: 读取沙箱内的文本文件内容
executor: file_read
parameters:
  type: object
  properties:
    path:
      type: string
      description: 相对沙箱的路径或绝对路径（须在沙箱内）
  required: [path]
---

读取指定路径的文本文件并返回其内容。

适用场景：
- 用户需要查看某个文件的内容
- 需要基于文件内容做后续处理

限制：
- 路径必须位于沙箱目录内
- 单次读取超过 20000 字符会被截断