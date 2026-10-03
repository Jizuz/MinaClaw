---
name: file-list
description: 列出沙箱内的文件和目录。当用户需要浏览沙箱内容或确认文件是否存在时使用。
---

```yaml
executor: file_list
parameters:
  type: object
  properties:
    path:
      type: string
      description: 目录，默认为沙箱根目录
  required: []
```

列出沙箱内某目录的所有条目，`[D]` 为目录，`[F]` 为文件。
