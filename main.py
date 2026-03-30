import logging

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8000, log_config=None)
