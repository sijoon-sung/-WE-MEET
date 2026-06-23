import logging

logging.basicConfig(level=logging.INFO, format="[WORKER-STORE] %(asctime)s - %(levelname)s - %(message)s")

class LocalObjectStore:
    def __init__(self):
        # Local cache: {object_id: data_bytes/numpy_array/tensor}
        self.store = {}

    def put(self, object_id, value):
        self.store[object_id] = value
        # Log size of object
        size = 0
        if hasattr(value, 'nbytes'):
            size = value.nbytes
        elif hasattr(value, 'element_size') and hasattr(value, 'nelement'):
            size = value.element_size() * value.nelement()
        logging.info(f"Stored object '{object_id}' in local store ({size} bytes)")
        return size

    def get(self, object_id):
        if object_id in self.store:
            return self.store[object_id]
        logging.warning(f"Object '{object_id}' not found in local store")
        return None
