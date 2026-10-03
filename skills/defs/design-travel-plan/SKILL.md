---
name: design-travel-plan
description: 根据目的地、天数、偏好生成旅游攻略框架文档。当用户提到旅行计划、行程规划或需要产出攻略文档时使用。
---

```yaml
executor: template
parameters:
  type: object
  properties:
    destination:
      type: string
    days:
      type: integer
    preferences:
      type: array
      items:
        type: string
      description: "如 ['美食', '历史', '自然风光']"
    budget:
      type: string
      description: "如 '5000 元'"
  required: [destination, days]
template: |
  # {destination} {days}天旅游攻略
  - 偏好：{preferences}
  - 预算：{budget}

  ## 每日行程
  （请 LLM 结合用户偏好按天填充具体景点、餐饮、交通）

  ## 交通建议
  - 市内：地铁 + 步行

  ## 预算估算
  - 总预算：{budget}
```

生成旅游攻略的**结构化框架**。LLM 拿到框架后应结合用户偏好补充每日细节。

更多偏好组合示例见 references/prefs.md。
