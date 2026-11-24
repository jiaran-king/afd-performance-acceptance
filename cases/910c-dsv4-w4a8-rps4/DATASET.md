# Frozen requests / 冻结负载

[GitHub Release](https://github.com/jiaran-king/alternative-afd-performance-acceptance/releases/tag/v0.1.0) 附 `afd-wildchat-rps4-v2.tar.gz`，包含参考实跑的冻结输入文件。各组的输入 token ID、顺序与原实验一致。 [校验值](case.json) · [冻结清单](workload.json)

| Split | 会话 | 请求 | 用途 |
| --- | ---: | ---: | --- |
| qualification | 200 | 400 | 双 EP8 资格试跑 |
| warmup | 8 | 16 | 每组独立预热 |
| formal-candidate | 600 | 1,200 | 两组正式对照 |

## Attribution

The request bundle is a derivative of [WildChat-4.8M by AllenAI](https://huggingface.co/datasets/allenai/WildChat-4.8M/tree/c827c6df8fcf008219ffaffa4d1dd77491099367) at revision `c827c6df8fcf008219ffaffa4d1dd77491099367`, made available under the [Open Data Commons Attribution License 1.0](DATASET-LICENSE.md). The materialization selects length-stratified consecutive user turns from the archived DeepSeek V4 workload `8ab206ac9f01`. Token IDs remain reversible with the matching tokenizer.

The source archive used arrival shapes and target lengths from [Mooncake FAST25 traces](https://github.com/kvcache-ai/Mooncake/tree/e94a0b86ba067455d8b0524eb2cbb5fbac2db024) and the [DeepSeek-V4-Flash tokenizer](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/tree/60d8d70770c6776ff598c94bb586a859a38244f1). The present case emits its own fixed 250 ms send slots. Source hashes and the selection seed are in [workload.json](workload.json), and the freezer implementation is [freeze_wildchat.py](../../scripts/freeze_wildchat.py).
