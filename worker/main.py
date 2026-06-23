# Worker Node Entrypoint - Skeleton
import logging
import argparse

logging.basicConfig(level=logging.INFO, format="[WORKER] %(asctime)s - %(levelname)s - %(message)s")

def serve():
    logging.info("Worker Node starting (skeleton)...")
    # TODO: Initialize object store, register at Head, start heartbeat monitor, and run task receiver gRPC server
    pass

if __name__ == "__main__":
    serve()
