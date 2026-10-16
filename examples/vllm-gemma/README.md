# vLLM Gemma example

This example serves Gemma 4 31B through vLLM's OpenAI-compatible server (`vllm serve`, `/v1/chat/completions` with tool calls), on one GKE node with two NVIDIA L4 GPUs. kube-agents reaches it with the `openai_compatible` model provider. The installer does not deploy it: you apply it yourself.

The model is `google/gemma-4-31B-it-qat-w4a16-ct`, Google's quantization-aware-trained 4-bit checkpoint for vLLM. The weights are under the Apache 2.0 license and are not gated, so no Hugging Face token is required.

## Prerequisites

- kube-agents installed in `kubeagents-system` on a GKE Standard cluster. A default install creates an Autopilot cluster; install with `--gke-cluster-mode=standard` to get one you can add a GPU node pool to.
- A GPU node pool for the server. The image is built on CUDA 13 and needs NVIDIA driver 580 or later. `gpu-driver-version=latest` installs it:

  ```bash
  gcloud container node-pools create gemma-l4 \
    --cluster=<CLUSTER> --region=<REGION> --node-locations=<ZONE> \
    --machine-type=g2-standard-24 \
    --accelerator=type=nvidia-l4,count=2,gpu-driver-version=latest \
    --num-nodes=1 --disk-size=200
  ```

  `g2-standard-24` has two L4 GPUs, 24 vCPUs and 96 GB of memory. The 200 GB boot disk holds the image (about 10 GB) and the weights cache. If the pool fails with `GCE_STOCKOUT`, the zone has no GPUs free: try another zone.

## Deploy

```bash
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f networkpolicy.yaml
kubectl apply -f podmonitoring.yaml
```

On a new node, the pod is ready about 10 to 11 minutes after it is scheduled: about 4 minutes to pull the image, then about 8 minutes to download the weights (2 to 2.5 minutes), compile the model (about 2 minutes) and load it:

```bash
kubectl -n kubeagents-system logs -f deploy/vllm-gemma-deployment
```

The Deployment uses the `Recreate` strategy: the pod takes both GPUs on its node, so a second pod cannot start beside it. Each rollout is an outage of about 8 minutes. For a rollout without an outage, give the pool a second node and change the strategy to `RollingUpdate` with `maxUnavailable: 0`.

The container drops all capabilities and cannot escalate privileges, but it runs as root: the image has no non-root user.

## Check it

```bash
kubectl -n kubeagents-system port-forward svc/llm-service 8080:80
```

In a second terminal:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "gemma-4-31b-it",
  "messages": [{"role": "user", "content": "How many pods are running in kube-system?"}],
  "tools": [{"type": "function", "function": {"name": "kubectl_get",
    "description": "Run kubectl get for a resource type in a namespace.",
    "parameters": {"type": "object", "properties": {"resource": {"type": "string"},
      "namespace": {"type": "string"}}, "required": ["resource", "namespace"]}}}]
}'
```

A working server answers with `"finish_reason": "tool_calls"` and a `kubectl_get` call.

## Connect kube-agents

Install or upgrade with the `openai_compatible` provider ([Inference gateway: OpenAI-compatible endpoints](https://gke-labs.github.io/kube-agents/concepts/inference-gateway/#openai-compatible-endpoints-self-hosted-or-private)):

| `install.env` key               | Value                                                                  |
| ------------------------------- | ---------------------------------------------------------------------- |
| `MODEL_PROVIDER`                | `openai_compatible`                                                    |
| `MODEL_DEFAULT_NAME`            | `gemma-4-31b-it` (the `--served-model-name`)                           |
| `OPENAI_COMPATIBLE_API_BASE`    | `http://llm-service.kubeagents-system.svc/v1`                          |
| `OPENAI_COMPATIBLE_TARGET_PORT` | `8000` (the pod port, not the Service port)                            |
| `MODEL_MAX_TOKENS`              | `4096`, so that the prompt and the output fit the 32,768-token context |

The example runs in the release namespace, so the gateway's egress rule admits every pod in `kubeagents-system` on port 8000. No other pod there listens on that port.

## Choose a size

Measured on 2026-10-16 with vLLM 0.31.0, a 32,768-token context and `--gpu-memory-utilization=0.90`. "27k prompt" is one request with a synthetic 26,984-token prompt (a pod list in the system message) and one tool, the first time and again with the prefix cache; the agent's own prompt is about that size. "Tool call" is the short request from [Check it](#check-it).

| Model                                     | GPUs         | Weights per GPU | KV cache (32k requests at once) | 27k prompt, cold / cached | Tool call (short prompt) |
| ----------------------------------------- | ------------ | --------------- | ------------------------------- | ------------------------- | ------------------------ |
| `google/gemma-4-31B-it-qat-w4a16-ct`      | 2x L4 (TP 2) | 10.67 GiB       | 78,275 tokens (2.39)            | 31.0 s / 3.0 s            | yes                      |
| `RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic` | 2x L4 (TP 2) | 13.74 GiB       | 203,524 tokens (6.21)           | 6.7 s / 0.6 s             | yes                      |
| `google/gemma-4-12B-it` (BF16)            | 2x L4 (TP 2) | 11.81 GiB       | 173,189 tokens (5.29)           | 13.7 s / 1.5 s            | yes                      |
| `google/gemma-4-31B-it-qat-w4a16-ct`      | 1x A100 40GB | 19.78 GiB       | 68,352 tokens (2.09)            | 17.1 s / 1.6 s            | yes                      |

- The default is 31B, the size with the highest agent score on the model card (Tau2 for the instruction-tuned models: 76.9% for 31B, 69.0% for 12B and 68.2% for 26B-A4B).
- 26B-A4B is a mixture-of-experts model with 3.8B active parameters. It reads a long prompt about five times faster and holds more context, at a lower agent score. To use it, change `MODEL_ID`, `--served-model-name` and the `ai.gke.io/model` label, and set `MODEL_DEFAULT_NAME` to the new served name.
- On one A100 40GB (`a2-highgpu-1g`), set `--tensor-parallel-size=1`, `nvidia.com/gpu: "1"`, a CPU request of at most 10 and the `nvidia-tesla-a100` accelerator label, and keep `--disk-size=200`. It reads a long prompt about 1.8 times as fast as 2x L4.
- A longer context is possible where the KV cache has room: on 2x L4 it holds 78,275 tokens. Raise `--max-model-len` and `MODEL_MAX_TOKENS` together.
- 31B in BF16 (about 62 GB of weights) does not fit 2x L4 or one A100 40GB. 31B in FP8 does not fit 2x L4 at a 32k context.
- E2B and E4B are too small for agent work (Tau2 24.5% and 42.2%).

## Notes

- The image is `vllm/vllm-openai:v0.31.0`, pinned by digest. The `-cu129` build of this release does not start: its `torchcodec` needs CUDA 13 libraries that the image does not have.
- `--limit-mm-per-prompt` turns off image and audio input. The agent sends text only, and the memory goes to the KV cache.
- Set `HF_TOKEN` in the pod if Hugging Face rate-limits the weight download.

## Metrics

- Directly: query the `/metrics` endpoint on port 8000 of the vLLM container. vLLM 0.31 names its metrics `vllm:<name>`, for example `vllm:num_requests_waiting` and `vllm:kv_cache_usage_perc`.
- Cloud Monitoring: `podmonitoring.yaml` sends them to Google Cloud Managed Service for Prometheus. Search Metrics Explorer for `num_requests_waiting` or `kv_cache_usage_perc`.
