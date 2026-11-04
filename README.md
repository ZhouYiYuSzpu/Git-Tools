# git-find

在授权范围内收集可能泄露的 Git 数据，并从中查找提交和文件。即使 `.git/HEAD` 不可访问，程序仍会尝试其他路径。工具不会自动运行 `git checkout` 或执行下载的代码。

## 项目目录

```text
项目根目录/
├── git_find.py          git-find 的入口
├── requirements.txt     git-find 的 Python 依赖
├── test_git_find.py     git-find 的测试
├── LICENSE.git-dumper   原项目的许可证副本
├── git-dumper/          原 git-dumper 项目及其说明
└── venv/                可选的本地虚拟环境，不属于项目源码
```

原项目的使用说明和许可证分别位于 [git-dumper/README.md](git-dumper/README.md) 和 [git-dumper/LICENSE](git-dumper/LICENSE)。

## 运行前准备

需要 Python 3.10 及以上版本，以及 `requirements.txt` 中的依赖。下面的 PowerShell 示例假定当前目录是本工具目录：

```powershell
Set-Location -LiteralPath 'D:\Tools\git dumper'
```

如果已经安装了原项目的依赖，可以直接使用它的虚拟环境。先查看命令帮助：

```powershell
& '.\venv\Scripts\python.exe' .\git_find.py --help
```

也可以在本目录创建独立环境并安装依赖：

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
```

## 第一步：收集 Git 数据

命令格式：

```text
python git_find.py 目标网址 输出目录
```

目标网址可以是网站根地址、`/.git/` 地址或 `/.git/HEAD` 地址。输出目录由你指定，必须不存在或为空，程序没有默认输出目录。

例如，测试本机提供的靶场，并把结果保存到当前目录下的 `result`：

```powershell
& '.\venv\Scripts\python.exe' .\git_find.py 'http://127.0.0.1:8080' '.\result'
```

`http://127.0.0.1:8080` 只是本机靶场示例，请替换为实际授权目标；`.\result` 可以换成你希望保存结果的路径。如果当前工作目录是项目根目录，`.\result` 就位于该目录下。换一台电脑时，请将上面的项目路径替换为实际的仓库目录。

程序会尝试常见 Git 路径及可识别的目录列表，再从已获取的资料中寻找引用、对象哈希和 pack 文件。默认最多发送 500 次请求，连接异常时额外重试一次，单个响应体上限为 20 MB。

## 第二步：查看结果

结果保存在第一步指定的目录。例如使用 `.\result` 时：

```text
result/
├── raw/.git/                原始 Git 文件，包括下载的对象
├── analysis/summary.json    请求结果、发现来源、对象数和提交列表
└── extracted/<提交哈希>/    导出的文件，仅在执行导出后出现
```

终端和 `summary.json` 会列出已发现的提交哈希。`saved` 表示响应已保存，不能单凭它断定内容一定有效；`saved-unparsed` 表示原始响应已保存，但对象或 index 解析失败。失败请求也会记录。

收集时会解析 Git 对象以继续发现其他对象，但不会默认导出每个提交里的普通文件。

## 第三步：离线导出文件

导出时使用第一步的同一个结果目录，不需要重新请求目标。导出所有已发现的提交：

```powershell
& '.\venv\Scripts\python.exe' .\git_find.py extract '.\result' all
```

也可以只导出指定提交：

```powershell
& '.\venv\Scripts\python.exe' .\git_find.py extract '.\result' '<完整的40位提交哈希>'
```

把示例中的占位文字替换为终端或 `summary.json` 中的实际哈希。每个提交会有独立目录：

```text
result/extracted/<提交哈希>/
```

工具不会覆盖已存在且非空的同名提交目录。缺失的 tree、blob 和子模块会在终端列出；符号链接以 blob 内容作为普通文件导出，不创建系统链接。

如果希望收集时直接导出所有已发现的提交，可在第一步命令后添加 `--extract-all`；也可以用 `--extract 哈希` 指定提交，该选项可以重复使用。收集阶段导出时的缺失信息会写入 `summary.json`。

## 常用选项

| 选项 | 作用 |
| --- | --- |
| `--timeout 5` | 单次请求超时时间，单位为秒 |
| `--retries 1` | 连接异常时的额外重试次数 |
| `--max-requests 500` | HTTP 请求次数上限，包含重试 |
| `--max-bytes 20000000` | 单个响应体的字节数上限 |
| `-b 分支名` | 尝试额外分支，可重复提供 |
| `-H '名称=值'` | 添加请求头，可重复提供 |
| `--proxy 代理URL` | 使用 HTTP、HTTPS 或 SOCKS 代理 |
| `--insecure` | 不验证 TLS 服务端证书 |
| `--any-status` | 也检查非 200 响应正文，可能保存错误内容 |

程序默认不跟随重定向。目录列表只识别标题或一级标题以 `Index of ` 开头的页面，不能覆盖所有服务器格式。工具只能发现已知路径、目录链接或已获取资料指向的对象，不能保证找到所有历史。SHA-1 松散对象、带索引的 SHA-1 pack 和 index 使用 Dulwich 解析；SHA-256 仓库及无索引 pack 不在已验证范围内。当前请求按顺序执行。

## 运行测试

```powershell
& '.\venv\Scripts\python.exe' -m unittest -v test_git_find
```

测试使用模拟 HTTP 响应、本地回环 HTTP 服务和临时 Git 仓库，不访问外部目标；pack 测试需要本机 Git。

设计参考 Maxime Arthaud 的 MIT 许可项目 git-dumper；原项目许可副本见 `LICENSE.git-dumper`。