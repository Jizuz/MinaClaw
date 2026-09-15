---
name: file_list
description: 列出沙箱内的文件和目录
executor: file_list
parameters:
  type: object
  properties:
    path:
      type: string
      description: 目录，默认为沙箱根目录
  required: []
---

列出沙箱内某目录的所有条目，`[D]` 为目录，`[F]` 为文件。