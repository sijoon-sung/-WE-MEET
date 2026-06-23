# Resource Monitor Daemon - Skeleton
class ResourceMonitorDaemon:
    def __init__(self, worker_id, head_address="babyray-head:50051"):
        self.worker_id = worker_id
        self.head_address = head_address

    def start(self):
        # TODO: Start thread to send heartbeats using psutil
        pass

    def stop(self):
        pass
