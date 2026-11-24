# 验收标准

| 项目 | 固定场景 |
| --- | --- |
| 模型 | DeepSeek-V4-Flash W4A8，checkpoint 带 DSpark 5 |
| 硬件 | Ascend 910C，2 台各 8 物理卡、16 DIE，共 32 DIE。P 和 D 各用一台 |
| 双 EP8 | 两套原生 DP2TP4/EP8，每套 8 DIE，MBT 8192，每 DP 最多 8 序列，CWS 和 Fused MC2 |
| AFD 12A+4F | A DP6TP2、F DP4TP1/EP4，MBT 32768，每 DP 最多 16 序列，CWS，异步 A/F |
| AFD 8A+8F | A DP4TP2、F DP8TP1/EP8，MBT 32768，验证异步 A/F、PD KV 传输与生成 |
| Decode | DP16TP1/EP16、MBT 120、每 DP 最多 2 序列，主模型 FULL_DECODE_ONLY，DSpark 5-token draft Eager |
| 共同条件 | 相同 P/D 设备预算、相同 checkpoint、完整 PD、Prefix Cache OFF，客户端合计 4 RPS |

正式发压前用 400 条独立请求核对双 EP8 的持续处理能力，审阅 25 秒窗口的完成请求数、在途和 P 侧等待。正式两组各用同一份 16 条独立预热请求，随后对 PD proxy 回放同一份 1,200 条冻结请求。固定计划每 250 毫秒一条，同一会话的下一轮等待上一轮流结束。验证实际发压、全部成功、两组实际顺序一致和逐请求 P→D KV 传输。显示实发 RPS 时允许调度误差 0.01 RPS，正式回放要求零依赖空时隙。

客户端 TTFT 从发出流式请求开始，至收到首个生成 token。每条生成 1 token。验收公式为 **AFD 平均 TTFT ≤ 同轮双 EP8 平均 TTFT × 0.8**。参考实验的验收线为 2.411 秒，AFD 为 1.509 秒。原表给定的 8.7 秒保留为历史参考，正式指标用各自同轮的配对结果计算。

[参考结果](../results/2026-11-23/README.md) · [复算逻辑](../scripts/audit.py)
