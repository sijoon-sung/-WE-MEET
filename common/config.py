import os

# gRPC Configs
DEFAULT_HEAD_PORT = 50051
DEFAULT_WORKER_PORT = 50052
DEFAULT_HEARTBEAT_INTERVAL = 5.0  # seconds

# Node Types
NODE_TYPE_HEAD = "head"
NODE_TYPE_ON_DEMAND = "on_demand"
NODE_TYPE_SPOT_A = "spot_a"
NODE_TYPE_SPOT_B = "spot_b"

# Simulated compute latency multipliers for heterogeneous simulation
NODE_LATENCY_MULTIPLIERS = {
    NODE_TYPE_ON_DEMAND: 1.0,
    NODE_TYPE_SPOT_A: 0.6,
    NODE_TYPE_SPOT_B: 0.3
}
