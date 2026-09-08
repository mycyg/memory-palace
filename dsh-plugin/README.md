# dsh-eventmem 1.0

MemoryPalace 的 DeepSeek Harness 原生 Cordis 插件。默认通过本地 Python 服务完成记忆采集、召回、上下文预算、会话恢复与后台整理，和 Claude Code 共享同一套核心逻辑。

## 安装

先按[主 README](../README.md)构建并启动 MemoryPalace 服务，再构建插件：

```sh
cd dsh-plugin
npm ci
npm run build
```

在 Harness 的 `cordis.patch.yml` 中通过本地绝对路径加载：

```yaml
- insert:
    - id: eventmem
      name: '/absolute/path/to/memory-palace/dsh-plugin/lib/index.js'
      config:
        enabled: true
        legacyMode: false
```

使用 `dsh --dump-config` 检查合并后的配置。验证所用的 Harness peer 版本为 `0.1.1-rc.2`；依赖锁保留实际版本，升级预发布版后应重新执行兼容测试。本次没有向 npm 发布插件。

## 行为

- `agent/session-start`：启动召回；`source: compact` 使用压缩恢复边界。
- `session/event`：记录用户与助手消息、todo 和 turn／step 事件；插件注入内容不被记录为用户事实。
- `tools/execute`：操作前调用相关经验查询；`tools/result`：记录行动与结果。
- `session/flush` 与插件卸载：等待传输队列；退出事件保留在持久 spool 中。
- 服务不可用：在私有目录中保存事件，服务恢复后重放。插件不自行计算另一套召回结果。

`EVENTMEM_HOME` 默认 `~/.memorypalace`，`EVENTMEM_URL` 默认 `http://127.0.0.1:8319`。服务与宿主使用相同根目录和凭据。上下文注入遵循 Harness 的 `agent.inject` 语义，可能进入后续模型请求；它不保证撤销已经发出的工具调用。

`legacyMode: true` 显式启用旧 `.memory/` 文件适配器，用于迁移切换与回退。旧版模式保留原配置字段与兼容测试；1.0 的事务、范围、历史和预算语义由服务模式提供。

## 验证

```sh
npm run typecheck
npm test
npm run build
```

测试覆盖旧配置、黄金数据、默认服务传输、会话顺序、操作前查询和断连时的事件保留。HTTP、SDK、CLI 与真实 MCP stdio／Streamable HTTP 一致性测试位于根目录 `tests/test_protocols_v1.py`。
