# Windows上SQLite + Python + MT5 设置指南

## 1. SQLite说明
SQLite不需要安装服务！它是一个文件型数据库，Python和MT5都内置支持。

## 2. 设置步骤

### 步骤1：确定共享文件夹
MT5的共享文件夹（所有MT5终端都能访问）：
```
C:\ProgramData\MetaQuotes\Terminal\Common\Files\
```

### 步骤2：修改Python脚本路径
编辑 `longport_simulate_sqlite.py` 第13行：
```python
# 修改为完整路径
DB_PATH = r"C:\ProgramData\MetaQuotes\Terminal\Common\Files\trading_signals.db"
```

### 步骤3：安装Python依赖
```bash
pip install longport
```

### 步骤4：配置LongPort API
创建 `.env` 文件，填入你的LongPort API信息：
```
LONGPORT_APP_KEY=你的APP_KEY
LONGPORT_APP_SECRET=你的APP_SECRET
LONGPORT_ACCESS_TOKEN=你的ACCESS_TOKEN
```

### 步骤5：运行Python脚本
```bash
python longport_simulate_sqlite.py
```
脚本会自动创建SQLite数据库文件。

### 步骤6：MT5设置
1. 将 `SQLiteSignalEA.mq5` 复制到MT5的 `MQL5\Experts` 目录
2. 在MetaEditor中编译（F7）
3. 将EA拖到US100.cash图表
4. EA参数设置：
   - DBPath: `trading_signals.db`（相对路径即可）
   - Leverage: 20
   - BaseLotSize: 0.1

## 3. 验证是否工作

### 查看数据库内容（可选）
下载 DB Browser for SQLite：https://sqlitebrowser.org/
打开 `C:\ProgramData\MetaQuotes\Terminal\Common\Files\trading_signals.db`

### 检查信号表
```sql
SELECT * FROM signals ORDER BY id DESC LIMIT 10;
```

## 4. 注意事项

- Python脚本监控QQQ，MT5在US100执行
- 确保Python和MT5都有文件夹的读写权限
- 如果提示权限错误，以管理员身份运行

## 5. 故障排查

**Python无法创建数据库？**
- 检查文件夹权限
- 确保路径正确

**MT5找不到数据库？**
- 确认数据库文件在Common\Files目录
- 检查EA日志输出

**信号未执行？**
- 检查账户余额
- 确认交易时间（美东9:40-15:45） 