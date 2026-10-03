---
name: send-email
description: 发送邮件给指定收件人。当用户明确要求发送邮件，且提供收件人、主题与正文时使用。
---

```yaml
executor: email
parameters:
  type: object
  properties:
    to:
      type: string
      description: 收件人邮箱
    subject:
      type: string
      description: 邮件主题
    body:
      type: string
      description: 邮件正文，支持 HTML
  required: [to, subject, body]
```

通过已配置的 SMTP 发送邮件。

未配置 SMTP 时以"模拟发送"方式返回，便于本地调试。
