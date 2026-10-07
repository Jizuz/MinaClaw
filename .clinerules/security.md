# 安全编码规范

## Python 安全规则

### 禁止反序列化不可信数据（最高优先级）

- **绝对禁止**使用 `pickle.loads`、`pickle.load`、`dill`、`jsonpickle`、`shelve` 处理任何来自外部的数据。
- `pickle` 的反序列化会**执行任意代码**，这是设计使然，不是可修补的漏洞[citation:11][citation:17]。
- 替代方案：使用 **JSON**（`json.loads`）或 **YAML 安全加载**（`yaml.safe_load`）传输和存储数据[citation:2][citation:11]。
- **禁止** `yaml.load()`（非 safe 版本），`numpy.load(allow_pickle=True)`、`pandas.read_pickle`、`torch.load` 同样危险，除非来源绝对可信[citation:17]。

### 路径遍历防御

- 拼接用户输入的路径时，**必须**使用 `os.path.realpath()` 解析后，再用 `os.path.commonpath` 或 `startswith` 校验结果是否在允许的根目录内[citation:13][citation:20]。
- **不要**只依赖 `os.path.normpath()` 或 `abspath()`，它们不解析符号链接，无法防御 symlink 逃逸[citation:1][citation:20]。
- 解压用户上传的压缩包时，**必须**校验每个成员的文件名，拒绝包含 `..` 或绝对路径的条目（Zip Slip 防御）[citation:7][citation:19]。

### 命令注入防御

- **禁止** `os.system()`、`subprocess.run(shell=True)` 直接拼接用户输入[citation:20]。
- 使用 `subprocess.run([...])` 列表参数形式，避免经过 shell。
- 如果必须用 shell 字符串，使用 `shlex.quote()` 对每个参数转义[citation:14]。

### SQL 注入防御

- **必须**使用参数化查询（`cursor.execute("... ?", (param,))` 或 ORM 的参数绑定）。
- **禁止**字符串拼接或 f-string 构造 SQL 语句。
- ORM 的 `raw()` 或 `.extra()` 方法传入用户输入时同样需要参数化。

### 依赖安全

- 固定依赖版本（lock file），在 CI 中运行 `pip-audit` 或 `safety scan` 扫描已知漏洞[citation:2]。


## Java 安全规则

### 禁止反序列化不可信数据（最高优先级）

- **绝对禁止**对来自外部（网络、文件、消息队列）的数据调用 `ObjectInputStream.readObject()`。
- 如果必须反序列化，**必须**配置 `ObjectInputFilter`（JDK 9+）或设置 `jdk.serialFilter` 系统属性，只允许白名单类[citation:6][citation:12]。
- 白名单示例：`example.File;!*`（只允许 `example.File`，拒绝其他所有类）[citation:6]。
- **优先方案**：使用 JSON（Jackson/Gson）替代 Java 原生序列化进行数据传输[citation:12]。

### SQL 注入防御

- **必须**使用 `PreparedStatement` 参数化查询，**禁止** `Statement` 拼接 SQL[citation:3][citation:15]。
- MyBatis 中**必须**使用 `#{}` 而非 `${}`（`${}` 是直接字符串替换，可被注入）[citation:3]。
- ORM（Hibernate/JPA）的查询方法天然参数化，但 `@Query` 注解中拼接用户输入时仍需参数绑定。

### 命令注入防御

- **禁止** `Runtime.exec()` 或 `ProcessBuilder` 接受未经校验的用户输入[citation:3]。
- 如果必须执行系统命令，使用**参数列表形式**（`new ProcessBuilder("cmd", "arg1", "arg2")`），避免将用户输入拼入单个命令字符串。

### 路径遍历防御

- 文件路径拼接用户输入后，**必须**使用 `java.nio.file.Path.toRealPath()` 解析，再用 `startsWith()` 校验是否在允许根目录内。
- 解压归档文件时，校验每个条目的规范化路径，拒绝逃逸出目标目录的条目。

### 输入验证与最小权限

- **永远不要信任用户输入**，即使前端已经校验[citation:3]。
- 数据库账号使用**最小权限**（只授予业务所需的表/操作权限）[citation:3]。
- 生产环境**禁止**开启 debug 模式或暴露详细错误堆栈。