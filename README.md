# ModelProxy

一个本地多平台 LLM 路由网关。将多个大模型供应商（OpenAI 兼容接口）聚合到本地单一入口，对外暴露统一 API，内置重试退避、失败后流转、时段路由、并发控制等能力。

> 本软件由 Zhiyu 许愿式开发，由 WorkBuddy 驱动 Claude Fable 5.1、DeepSeek v4 Flash、DeepSeek v4 Pro、GLM 5.3、GLM 5.3 Flash、GPT 5.6 Sol、GPT Image 2、Hy4 Preview、Kimi k3 等模型构成。
> 本项目纯粹个人好奇心，想探究“许愿式”开发法能做到什么程度。个人0编程经验，全靠 WorkBuddy 驱动各家模型开发。

---

## 功能特性

### 供应商管理
- 增删改供应商（ID / 名称 / 上游地址 / API Key / 并发上限 / 最大重试）
- 同名供应商自动归堆显示，API Key 掩码展示
- 修改供应商 ID 时自动同步修正引用它的模型（含失败后流转链）

### 模型管理
- 本地模型名 →（供应商 + 上游真实模型 ID）映射
- 模型级自定义并发，留空继承供应商设置
- **失败后流转链**：每模型 ≤3 跳，主目标失败后按序转移
- **批量拉取**：从供应商 `/v1/models` 拉取列表 → 多选确认 → 批量入库
- **时段路由**：按本机时间自动切换上游（支持跨午夜、纯时段模型、兜底模型）

### 请求处理（网关核心）
- OpenAI 兼容接口（`/v1/chat/completions` 流式 SSE + 非流式）
- 本地 Key 鉴权
- 429/5xx 指数退避重试，超阈值触发失败后流转
- 供应商级 + 模型级并发控制
- Token 用量累计统计

### 运行与体验
- 局域网监听开关（`127.0.0.1` / `0.0.0.0`）
- 开机自启、系统托盘、窗口尺寸与最大化状态记忆
- 实时日志、运行统计（成功/重试/流转/失败/在途）

---

## 快速开始

### 环境要求
- Windows 10+
- Python 3.13+

### 源码运行

```bash
python gateway_gui.py
```

### 打包为可执行文件

```bash
pip install pyinstaller
python -m PyInstaller ModelGateway.spec --noconfirm --clean
```

产物输出至 `dist/ModelProxy.exe`。

---

## 项目结构

```
sensenova-proxy/
├── gateway_core.py       # 网关核心逻辑（路由/重试/流转/并发/统计，无界面依赖）
├── gateway_gui.py        # tkinter 桌面界面
├── ModelGateway.spec     # PyInstaller 打包配置
├── icon.ico              # 应用图标
├── VERSION.txt           # 版本号
└── .gitignore
```

> 网关核心（`gateway_core.py`）与界面（`gateway_gui.py`）完全解耦，可独立复用。

---

## 使用说明

1. **添加供应商**：填写名称、上游地址（如 `https://api.example.com/v1`）、API Key；
2. **添加模型**：设置本地模型名，选择供应商与上游真实模型 ID，可按需配置失败后流转链；
3. **启动网关**：设置端口与本地 Key，点击启动；
4. **接入客户端**：在支持自定义 OpenAI API 的客户端中，将地址设为 `http://127.0.0.1:<端口>/v1`，Key 填本地 Key。

---

## 配置

配置保存于 `gateway_config.json`（首次运行后生成，含供应商 API Key 等敏感信息，**已加入 `.gitignore`，请勿提交**）。

```json
{
  "gateway": { "port": 8787, "api_key": "", "lan_access": false },
  "providers": [],
  "models": []
}
```

---

## 版本

- 当前版本号见 `VERSION.txt`；
- 历史版本与变更记录见仓库 `VERSIONS.md`；
- 完整功能清单见仓库 `FEATURES.md`。

---

## 路线图

- [ ] 多 Key 分发与用量限额监控
- [ ] 公网访问与安全加固
- [ ] 坚果云 WebDAV 配置备份
- [ ] 后台服务 + Web 管理界面（Docker 化）

## 许可证

[MIT](LICENSE)
