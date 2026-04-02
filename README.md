# Multi-Gpu Fan Control
Custom service for multi-GPU fan control optimized for LLM workloads, prioritizing thermal stability and throttling prevention over acoustics.

A custom service for controlling fan behavior across multiple GPUs, specifically tuned for LLM and embedding workloads.

This project is not focused on quiet operation. Instead, it is designed to:
- maintain stable GPU temperatures under sustained inference/training load
- prevent thermal throttling and performance drops
- handle multi-GPU setups (e.g. MI25 / WX9100 class hardware)
- provide predictable and aggressive cooling behavior

Ideal for homelabs and production setups where GPUs run near 100% utilization for extended periods.

## Running via shell
<img src="./resources/shell_run.png" alt="Running via shell" width="600">

## Running as systemd service

<img src="./resources/systemd_journal_log_run.png" alt="Running as systemd service" width="600">
