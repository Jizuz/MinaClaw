---
name: file_write
description: 将内容写入沙箱内的文件（覆盖写）
executor: file_write
parameters:
  type: object
  properties:
    path:
      type: string
      description: 目标文件路径（须在沙箱内）
    content:
      type: string
      description: 要写入的完整文本内容
  required: [path, content]
---

把 `content` 完整写入 `path`，自动创建父目录，文件已存在则覆盖。

限制：
- 内容最大 10MB
- 扩展名须在允许列表