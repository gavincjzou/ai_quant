# 🔧 环境搭建指南

## 1. 前置要求

- Python >= 3.8
- 长桥证券账号（已开通 OpenAPI）
- macOS / Linux / Windows

## 2. 安装步骤

```bash
# 进入项目目录
cd ~/ai_quant

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 开发模式安装（可选）
pip install -e .
```

## 3. 长桥 API 配置

### 3.1 获取 API 密钥

1. 登录 [长桥开发者平台](https://open.longportapp.com)
2. 创建应用 → 获取 `App Key` 和 `App Secret`
3. 生成 `Access Token`

### 3.2 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的密钥
```

`.env` 文件内容:
```
LONGBRIDGE_APP_KEY=你的AppKey
LONGBRIDGE_APP_SECRET=你的AppSecret
LONGBRIDGE_ACCESS_TOKEN=你的AccessToken
```

### 3.3 验证连接

```bash
python -c "
from src.data.longport_client import LongPortClient
client = LongPortClient()
quote = client.get_realtime_quote(['AAPL.US'])
print(quote)
"
```

## 4. 目录说明

```
ai_quant/
├── config/          # YAML 配置（策略参数、风控阈值）
├── src/             # 核心代码（6层架构）
├── scripts/         # 运行脚本（回测/模拟/实盘）
├── tests/           # 单元测试
├── data_cache/      # 本地数据缓存（自动生成）
├── output/          # 回测图表输出（自动生成）
├── logs/            # 运行日志（自动生成）
└── docs/            # Obsidian 知识库
```

## 5. 快速验证

```bash
# 运行单元测试
python -m pytest tests/ -v

# 下载历史数据
python scripts/fetch_data.py --symbols AAPL.US --days 365

# 运行回测
python scripts/run_backtest.py --strategy ma_cross --symbol AAPL.US
```

## 踩坑记录

_（后续在实际使用中记录）_
