"""Render the vLLM engine Deployments, Services and KEDA ScaledObjects as Kubernetes YAML.

    python3 render_engines.py rehearsal | kubectl apply -f -
    python3 render_engines.py a100 | kubectl apply -f -

Weights are read from each host's local NVMe (stage_weights.sh copies them from steward-fs).

Each pool (prefill, decode) has a fixed slot 0 and a KEDA-scaled slot 1 (0 or 1 replica).
Engines use hostNetwork so NIXL/UCX can reach the RoCE NIC, which means two replicas of one
Deployment cannot share a host (same ports). Separate slot Deployments with their own ports
avoid that, and KEDA toggles slot 1 on pool pressure.
"""

import argparse
import json

PROFILES = {
    # One A10 plays both pools with a tiny model, to rehearse manifests, ports and NIXL wiring.
    "rehearsal": {
        "model": "Qwen3-0.6B",
        "tp": 1,
        "gpu": {"nvidia.com/gpu": "1", "nvidia.com/gpumem": "5120"},
        "util": "0.85",
        "max_len": 4096,
        "max_num_seqs": 32,
        "node_pool": {"prefill": "rehearsal", "decode": "rehearsal"},
        "ucx_devices": "all",
        "keda": {"prefill": 2, "decode": 8},
        "shm": "2Gi",
    },
    # Two 8x A100 40GB hosts: prefill pool on host A, decode pool on host B, TP=4 engines.
    "a100": {
        "model": "Qwen3-30B-A3B",
        "tp": 4,
        "gpu": {"nvidia.com/gpu": "4"},
        "util": "0.90",
        "max_len": 32768,
        "max_num_seqs": 256,
        "node_pool": {"prefill": "prefill", "decode": "decode"},
        "ucx_devices": "mlx5_0:1",
        "keda": {"prefill": 8, "decode": 192},
        "shm": "16Gi",
    },
}

# pool -> slot -> (api port, NIXL side-channel base port). Spaced so TP ranks never collide.
PORTS = {
    "prefill": {0: (8000, 5600), 1: (8002, 5620)},
    "decode": {0: (8001, 5610), 1: (8003, 5630)},
}
ROLE = {"prefill": "kv_producer", "decode": "kv_consumer"}


def engine(profile: dict, pool: str, slot: int, models: str, image: str) -> list[dict]:
    name = f"vllm-{pool}-{slot}"
    port, side = PORTS[pool][slot]
    kv = json.dumps(
        {
            "kv_connector": "NixlConnector",
            "kv_role": ROLE[pool],
            "kv_load_failure_policy": "fail",
            # Prefill keeps KV for a remote decode at most this long; blocks never pulled
            # (client gone between prefill and decode) are released and counted in
            # vllm:nixl_num_kv_expired_reqs. Default is 30 s; stated here so it is a decision.
            "kv_connector_extra_config": {"kv_lease_duration": 30},
        }
    )
    serve = " ".join(
        [
            "ulimit -l unlimited;",
            "exec vllm serve",
            f"/models/{profile['model']}",
            "--served-model-name steward",
            f"--host 0.0.0.0 --port {port}",
            f"--tensor-parallel-size {profile['tp']}",
            f"--gpu-memory-utilization {profile['util']}",
            f"--max-model-len {profile['max_len']}",
            f"--max-num-seqs {profile['max_num_seqs']}",
            "--enable-prefix-caching",
            # The agent calls tools; Qwen3's chat template uses Hermes-style tool calls.
            "--enable-auto-tool-choice --tool-call-parser hermes",
            f"--kv-transfer-config '{kv}'",
        ]
    )
    labels = {"app": name, "pool": pool, "slot": str(slot)}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "replicas": 1 if slot == 0 else 0,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {
                        "prometheus.io/scrape": "true",
                        "prometheus.io/port": str(port),
                        "prometheus.io/path": "/metrics",
                    },
                },
                "spec": {
                    "schedulerName": "hami-scheduler",
                    "hostNetwork": True,
                    "dnsPolicy": "ClusterFirstWithHostNet",
                    "nodeSelector": {"steward/pool": profile["node_pool"][pool]},
                    "terminationGracePeriodSeconds": 30,
                    "containers": [
                        {
                            "name": "vllm",
                            "image": image,
                            "imagePullPolicy": "Never",
                            "command": ["bash", "-c", serve],
                            "env": [
                                {"name": "HF_HUB_OFFLINE", "value": "1"},
                                {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": str(side)},
                                {
                                    "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                    "valueFrom": {"fieldRef": {"fieldPath": "status.hostIP"}},
                                },
                                {"name": "UCX_TLS", "value": "all"},
                                {"name": "UCX_NET_DEVICES", "value": profile["ucx_devices"]},
                            ],
                            "ports": [{"containerPort": port, "name": "http"}],
                            "resources": {"limits": profile["gpu"]},
                            "securityContext": {"privileged": True},
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": port},
                                "periodSeconds": 5,
                                "failureThreshold": 3,
                            },
                            "startupProbe": {
                                "httpGet": {"path": "/health", "port": port},
                                "periodSeconds": 10,
                                "failureThreshold": 90,
                            },
                            "volumeMounts": [
                                {"name": "models", "mountPath": "/models", "readOnly": True},
                                {"name": "rdma", "mountPath": "/dev/infiniband"},
                                {"name": "shm", "mountPath": "/dev/shm"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "models", "hostPath": {"path": models, "type": "Directory"}},
                        {"name": "rdma", "hostPath": {"path": "/dev/infiniband", "type": "DirectoryOrCreate"}},
                        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": profile["shm"]}},
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": labels},
        "spec": {"selector": {"app": name}, "ports": [{"name": "http", "port": port, "targetPort": port}]},
    }
    return [deployment, service]


def scaler(profile: dict, pool: str) -> dict:
    """KEDA turns slot 1 on when the pool's own pressure metric crosses the threshold."""
    queries = {
        # Prefill pressure: requests waiting for their prompt to be processed.
        "prefill": 'sum(vllm:num_requests_waiting{pool="prefill"}) or vector(0)',
        # Decode pressure: sequences holding decode slots plus those waiting for one.
        "decode": 'sum(vllm:num_requests_running{pool="decode"}) + sum(vllm:num_requests_waiting{pool="decode"}) or vector(0)',
    }
    threshold = str(profile["keda"][pool])
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {"name": f"{pool}-slot1"},
        "spec": {
            "scaleTargetRef": {"name": f"vllm-{pool}-1"},
            "minReplicaCount": 0,
            "maxReplicaCount": 1,
            "pollingInterval": 10,
            "cooldownPeriod": 180,
            "triggers": [
                {
                    "type": "prometheus",
                    "metadata": {
                        "serverAddress": "http://prometheus-server.monitoring.svc.cluster.local",
                        "query": queries[pool],
                        "threshold": threshold,
                        "activationThreshold": threshold,
                    },
                }
            ],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", choices=sorted(PROFILES))
    ap.add_argument("--models", default="/opt/steward/models", help="local staged weights directory on each host")
    ap.add_argument("--image", default="docker.io/steward/vllm-nixl:v0.30.0")
    ap.add_argument("--no-keda", action="store_true", help="omit the slot-1 Deployments and ScaledObjects")
    args = ap.parse_args()

    profile = PROFILES[args.profile]
    docs = []
    for pool in ("prefill", "decode"):
        docs += engine(profile, pool, 0, args.models, args.image)
        if not args.no_keda:
            docs += engine(profile, pool, 1, args.models, args.image)
            docs.append(scaler(profile, pool))
    # JSON is valid YAML; kubectl accepts a stream separated by '---'.
    print("\n---\n".join(json.dumps(d, indent=2) for d in docs))


if __name__ == "__main__":
    main()
