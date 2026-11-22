"""Read the demonstrator's failed task without deleting it. Requires AWS CLI."""
import json
import os
import subprocess
import sys
import time


def main(name):
    def output(*args):
        return subprocess.check_output(["terraform", "output", *args], text=True).strip()
    url = output("-raw", "failed_queue")
    env = {**os.environ, **json.loads(output("-json", "queue_test_credentials")),
           "AWS_DEFAULT_REGION": "ru-central1", "AWS_PAGER": ""}
    env.pop("AWS_SESSION_TOKEN", None)
    end = time.monotonic() + 1200
    while time.monotonic() < end:
        result = subprocess.check_output(
            ["aws", "--endpoint-url", "https://message-queue.api.cloud.yandex.net",
             "sqs", "receive-message", "--queue-url", url, "--wait-time-seconds", "20",
             "--max-number-of-messages", "10", "--visibility-timeout", "15", "--output", "json"],
            env=env, text=True)
        for message in json.loads(result or "{}").get("Messages", []):
            task = json.loads(message["Body"])
            if task["name"] == name:
                print("DLQ:", json.dumps(task, ensure_ascii=False))
                return
    raise TimeoutError(f"Task {name} did not reach DLQ in 20 minutes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 tools/deadletter.py unavailable-<tag>.txt")
    main(sys.argv[1])
