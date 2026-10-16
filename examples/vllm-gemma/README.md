# vLLM Gemma example

This example serves Gemma 4 31B through vLLM's OpenAI-compatible server (`vllm serve`, `/v1/chat/completions` with tool calls), on one GKE node with two NVIDIA L4 GPUs. kube-agents reaches it with the `openai_compatible` model provider. The installer does not deploy it: you apply it yourself.

The model is `google/gemma-4-31B-it-qat-w4a16-ct`, Google's quantization-aware-trained 4-bit checkpoint for vLLM. The weights are under the Apache 2.0 license and are not gated, so no Hugging Face token is required.

## Prerequisites

- A GKE Standard cluster with kube-agents installed in `kubeagents-system`.
- A GPU node pool for the server. The image is built on CUDA 13 and needs NVIDIA driver 580 or later. `gpu-driver-version=latest` installs it (driver 580.173.02 on GKE 1.35):

  ```bash
  gcloud container node-pools create gemma-l4 \
    --cluster=<CLUSTER> --region=<REGION> --node-locations=<ZONE> \
    --machine-type=g2-standard-24 \
    --accelerator=type=nvidia-l4,count=2,gpu-driver-version=latest \
    --num-nodes=1 --disk-size=200
  ```

  `g2-standard-24` has two L4 GPUs, 24 vCPUs and 96 GB of memory. The 200 GB boot disk holds the image (about 10 GB) and the weights cache.

## Deploy

```bash
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f networkpolicy.yaml
kubectl apply -f podmonitoring.yaml
```

The first start pulls the image (about 4 minutes), downloads the weights (about 2 minutes) and compiles the model (about 1.5 minutes). The pod is ready about 7 to 8 minutes after the container starts:

```bash
kubectl -n kubeagents-system logs -f deploy/vllm-gemma-deployment
```

The Deployment uses the `Recreate` strategy: the pod takes both GPUs on its node, so a second pod cannot start beside it. Each rollout is an outage of about 7 minutes. For a rollout without an outage, give the pool a second node and change the strategy to `RollingUpdate` with `maxUnavailable: 0`.

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

Install or upgrade with the `openai_compatible` provider ([Inference gateway: OpenAI-compatible endpoints](https://gke-labs.github.io/kube-agents/concepts/inference-gateway/)):

| `install.env` key               | Value                                         |
| ------------------------------- | --------------------------------------------- |
| `MODEL_PROVIDER`                | `openai_compatible`                           |
| `MODEL_DEFAULT_NAME`            | `gemma-4-31b-it` (the `--served-model-name`)  |
| `OPENAI_COMPATIBLE_API_BASE`    | `http://llm-service.kubeagents-system.svc/v1` |
| `OPENAI_COMPATIBLE_TARGET_PORT` | `8000` (the pod port, not the Service port)   |

## Choose a size

Measured on 2026-10-16 with vLLM 0.31.0, a 32,768-token context and `--gpu-memory-utilization=0.90`. The agent's prompt is about 27,000 tokens, so a context below 32k does not fit an agent turn. "27k prompt" is one request with a 26,984-token prompt and one tool, the first time and again with the prefix cache.

| Model                                     | GPUs         | Weights per GPU | KV cache (32k requests at once) | 27k prompt, cold / cached | Tool call |
| ----------------------------------------- | ------------ | --------------- | ------------------------------- | ------------------------- | --------- |
| `google/gemma-4-31B-it-qat-w4a16-ct`      | 2x L4 (TP 2) | 10.67 GiB       | 78,275 tokens (2.39)            | 31.0 s / 3.0 s            | yes       |
| `RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic` | 2x L4 (TP 2) | 13.74 GiB       | 203,524 tokens (6.21)           | 6.7 s / 0.6 s             | yes       |
| `google/gemma-4-12B-it` (BF16)            | 2x L4 (TP 2) | 11.81 GiB       | 173,189 tokens (5.29)           | 13.7 s / 1.5 s            | yes       |
| `google/gemma-4-31B-it-qat-w4a16-ct`      | 1x A100 40GB | A100_WEIGHTS    | A100_KV                         | A100_PROMPT               | A100_TOOL |

- The default is 31B, the size with the highest agent score on the model card (Tau2 76.9%, against 68.2% for 26B-A4B and 69.0% for 12B).
- 26B-A4B is a mixture-of-experts model with 3.8B active parameters. It reads a long prompt about five times faster and holds more context, at a lower agent score. To use it, change `MODEL_ID` and `--served-model-name`.
- 31B in BF16 (about 62 GB of weights) does not fit 2x L4 or one A100 40GB. 31B in FP8 ran out of memory on 2x L4 at a 32k context in an earlier test.
- E2B and E4B are too small for agent work (Tau2 24.5% and 42.2%).

## Notes

- The image is `vllm/vllm-openai:v0.31.0`, pinned by digest. The `-cu129` build of this release does not start: its `torchcodec` needs CUDA 13 libraries that the image does not have.
- `--limit-mm-per-prompt` turns off image and audio input. The agent sends text only, and the memory goes to the KV cache.
- Set `HF_TOKEN` in the pod if Hugging Face rate-limits the weight download.

## Metrics

- Directly: query the `/metrics` endpoint on port 8000 of the vLLM container.
- Cloud Monitoring: search for vLLM metrics prefixed with `prometheus.googleapis.com/vllm_` (for example `prometheus.googleapis.com/vllm_num_requests_waiting/gauge` or `prometheus.googleapis.com/vllm_gpu_cache_usage_factor/gauge`).
