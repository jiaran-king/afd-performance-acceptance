# Ascend 910C 复测步骤

## 设备与镜像

每台容器分配 16 个逻辑 DIE。参考实验在同一超节点上用同一对机器顺序切换两组部署。P/D 容器的互通网卡承担 Mooncake 数据传输，两个容器需要能访问相同路径下的共享检出目录，以便收集跨节点 KV 和阶段日志。

在两端使用各自有权获取的匹配镜像，并挂载同一 DeepSeek-V4-Flash W4A8/DSpark checkpoint。参考镜像记录了 tag，预检对照容器内的 vLLM、vLLM-Ascend、CANN、torch-npu 版本以及模型 config、量化配置和 tokenizer SHA256。匹配容器需包含基准源码树、完整 Ascend custom vendor、CANN 开发工具链、Git 和 Python。 [参考指纹](../cases/910c-dsv4-w4a8-rps4/case.json)

## 节点接入

在共享 checkout 中复制 `site.env.example` 为 `site.env`。填写两端的 IP、网卡、设备列表和模型挂载路径。设备列表以 16 个 DIE 的逻辑 rank 顺序给出。两台机器都能读取 `site.env`。

- 控制端在 P 容器内时，留空 `P_SSH`，设置 `D_SSH`。SSH 进宿主机时用 `D_CONTAINER` 指定已有容器。
- 控制端在独立机器时，设置 `P_SSH` 和 `D_SSH`，并通过 `SHARED_ROOT` 指定两端容器内看到的 checkout 绝对路径。`SITE_REMOTE_FILE` 可指定共享配置的位置。
- 使用其他集群执行方式时，提供 `NODE_EXEC` 指向本地可执行文件。它收到 `P` 或 `D` 作为首参，其余参数为该容器中要执行的命令与参数。

本案例的 HTTP 端口在 P 节点为 7100、7101、8000–8002，在 D 节点为 7100。节点按当前配置建立通信，使用脚本中声明的 DP RPC 和 Mooncake KV 端口。一次复测使用专用的 P/D 容器和一份共享 checkout。启动并比较两个配置会逐组启停该入口创建的进程。

~~~bash
./reproduce.sh --site site.env --prepare-only
./reproduce.sh --site site.env
~~~

`--prepare-only` 可单独验证数据、依赖和安装。默认完整入口会在运行记录中保留预检、环境构建、冒烟、逐请求指标、P/D 日志和验收 JSON。正式回放每组约 5 分钟发压，环境构建、加载、预热和冒烟另计。

## 下载和编译

首轮通过 [Release](https://github.com/jiaran-king/alternative-afd-performance-acceptance/releases/tag/v0.1.0) 获取约 20 MB 的冻结请求包，校验包和每个 split 的 SHA256。离线组网可把同一 Release 文件放到共享目录，并填写 `WORKLOAD_ARCHIVE`。

P 侧复用镜像内固定的 vLLM 与 vLLM-Ascend 源码，从公开 AFD 仓库获取固定提交并编译 AFD 扩展。CWS overlay 应用仓库提供的 MRV2 Compressor/SCFA 补丁和 Mooncake 逻辑页导出补丁，使用匹配 CANN 编译 CANN 算子 installer 并安装到 checkout 的 `.runtime/`。已有同哈希的原版 CWS vendor 时可通过 `CWS_VENDOR_DIR` 指定。实际加载路径和 hash 会在冒烟后写入 `native-maps.json`。 [环境脚本](../scripts/prepare-runtime.sh)

适配后的启动入口保留实测的 CWS、MC2、AFD Eager 和 D 配置。双 EP8 的两个 P 实例各自通过隔离 proxy 做 KV 冒烟，再合用正式 proxy。12A+4F 和 8A+8F 都检查 F worker 进入异步 connector loop，并实际传输长短输入的 KV。

## 核对成绩

在 `runs/<run-id>/acceptance.json` 查看两组平均 TTFT、P50/P95、实发 RPS、输入 token/s、P 排队和 KV 传输。另取 `qualification/audit.json` 查看基线是否形成持续积压。`acceptance_passed` 要求配对回放有效且平均 TTFT 相对同轮双 EP8 降低至少 20%。

[参考报告](../results/2026-11-23/README.md)和公开逐请求记录来自 2026-11-23 的正式设备实测。
