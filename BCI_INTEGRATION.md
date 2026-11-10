# bci_tools 接入

独立使用和工具箱使用同一入口：

```bash
uv run --locked notion-pdf-gui
```

本仓库维护 tool.toml。单人维护者自测后直接 commit 和 push；将已推送提交的源码快照复制到 bci_tools/tools/ 对应目录，再向 bci_tools 提交 PR。不改变工具箱启动逻辑，不要求子项目 PR。tool.toml 本身不会触发自动同步。

复制时保留源码、依赖锁、示例、测试和许可证，排除 .git、虚拟环境、缓存、打包产物、凭据和个人配置。工具箱中维护 MIGRATION.md，记录实际来源 SHA；这里只记录接入约定，不预填将来提交的 SHA。

工具箱侧检查清单和入口；完整功能测试在源项目执行。首次接入仍需验证真实界面启动。
