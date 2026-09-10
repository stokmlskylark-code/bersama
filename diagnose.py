import os
with open("/home/container/envdump.txt", "w") as f:
    for k in sorted(os.environ):
        f.write(f"{k}={os.environ[k]}\n")
    f.write(f"WEB_PORT_WOULD_BE={os.getenv('SERVER_PORT', os.getenv('WEB_PORT', '8080'))}\n")
