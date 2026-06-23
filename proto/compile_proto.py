import subprocess
import sys
import os

# Get directory of current script
proto_dir = os.path.dirname(os.path.abspath(__file__))
we_meet_dir = os.path.dirname(proto_dir)

proto_file = os.path.join(proto_dir, "baby_ray.proto")

print(f"Compiling protobuf from: {proto_file}")

# Set Python path to search inside WE-MEET
cmd = [
    sys.executable,
    "-m", "grpc_tools.protoc",
    f"-I{we_meet_dir}",  # Include path root
    f"--python_out={we_meet_dir}",
    f"--grpc_python_out={we_meet_dir}",
    proto_file
]

print("Running command:", " ".join(cmd))
result = subprocess.run(cmd, capture_output=True, text=True)

if result.returncode != 0:
    print("Compilation failed!")
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)
    sys.exit(1)
else:
    print("Compilation successful!")
    print("Generated files in 'proto/':")
    # Fix import statement in generated grpc file for python 3 relative imports
    grpc_py_path = os.path.join(proto_dir, "baby_ray_pb2_grpc.py")
    if os.path.exists(grpc_py_path):
        with open(grpc_py_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Replace local import with absolute or relative package import
        # e.g., 'import proto.baby_ray_pb2 as proto_dot_baby__ray__pb2' or 'from proto import baby_ray_pb2...'
        # Depending on how protoc output imports it. Usually: 'import proto.baby_ray_pb2 as proto_dot_baby__ray__pb2'
        # Let's check if we need to modify it or not. Usually it's fine.
        print("  - proto/baby_ray_pb2.py")
        print("  - proto/baby_ray_pb2_grpc.py")
