# 代码风格规范

## Python（遵循 PEP 8）

### 命名
- 变量、函数、模块：`lower_snake_case`（如 `user_name`、`fetch_data`）
- 类：`PascalCase`（如 `UserProfile`）
- 常量：`UPPER_SNAKE_CASE`（如 `MAX_RETRY_COUNT`）

### 缩进与格式
- 使用 **4 个空格**缩进，**禁止使用 Tab**
- 每行不超过 **79 字符**（长表达式换行时保持对齐）
- 二元运算符（`=`, `+`, `==` 等）两侧各留 **1 个空格**；但函数关键字参数或默认值中的 `=` **不加空格**（如 `def foo(x=1)`）

### 比较与异常
- 与 `None` 比较时**始终用 `is` 或 `is not`**，禁止用 `==` / `!=`
- **禁止裸 `except:`**，必须捕获具体异常（如 `except ValueError:`）
- 自定义异常应继承 `Exception`，而非 `BaseException`

### 导入
- 按“标准库 → 第三方库 → 本地模块”分组，组间空一行
- **禁止通配符导入**（如 `from module import *`）


## Java（遵循 Google Java Style）

### 命名
- 类、接口：`UpperCamelCase`（如 `OrderService`）
- 方法、字段、参数、局部变量：`lowerCamelCase`（如 `sendMessage`）
- 常量（`static final` 且不可变）：`UPPER_SNAKE_CASE`（如 `MAX_COUNT`）

### 格式
- 缩进使用 **2 个空格**，**禁止 Tab**
- 每行不超过 **100 字符**
- 左花括号 `{` **不另起一行**（K&R 风格）：`if (condition) {`
- 控制语句（`if`, `for`, `while`）**必须使用花括号**，即使只有一行

### 源文件结构
- 每个源文件**有且仅有一个顶级类**
- 导入语句**不使用通配符**（如 `import java.util.*`）
- 导入顺序：静态导入 → 非静态导入，按 ASCII 排序，组间空一行

### 异常与静态
- 捕获的异常**不应忽略**（至少记录日志或重新抛出）
- 静态成员**通过类名访问**（如 `ClassName.staticMethod()`），而非实例