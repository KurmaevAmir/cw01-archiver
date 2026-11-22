"""Exercise the four public routes on a freshly deployed stand."""
import hashlib
import io
import json
import subprocess
import time
import uuid
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = subprocess.check_output(["terraform", "output", "-raw", "api_url"], text=True)
SOURCE = "https://www.rfc-editor.org/rfc/rfc20.txt"


def request(route, status, task=None):
    data = None if task is None else json.dumps(task).encode()
    req = Request(BASE + route, data, {"Content-Type": "application/json"})
    try:
        response = urlopen(req, timeout=20)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        print(req.method if hasattr(req, "method") else req.get_method(), route, response.status)
        if response.status != status:
            raise RuntimeError(f"Expected {status}, got {response.status}: {body[:300]!r}")
        return body


def main():
    tag = uuid.uuid4().hex[:12]
    name = f"receipt-{tag}.txt"
    missing = f"00000000-0000-7000-8000-{tag}.zip"
    failed = f"unavailable-{tag}.txt"
    request("/tasks", 202, {"name": failed, "url": BASE + "/download/" + missing})
    print("Look for this name in the dead-letter queue:", failed)
    request("/tasks", 400, {"name": name})
    request("/tasks", 202, {"name": name, "url": SOURCE})
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        rows = json.loads(request("/archives", 200))
        row = next((r for r in rows if r["name"] == name and r["url"] == SOURCE), None)
        if row:
            break
        time.sleep(3)
    else:
        raise TimeoutError("No matching record within three minutes. Inspect worker logs.")
    key = row["key"]
    identifier = uuid.UUID(key.removesuffix(".zip"))
    if identifier.version != 7 or key != str(identifier) + ".zip":
        raise ValueError(f"Invalid archive key: {key}")
    record = json.loads(request("/archives/" + key, 200))
    if record != {"name": name, "url": SOURCE, "key": key}:
        raise ValueError(record)
    print(json.dumps(record, indent=2))
    with zipfile.ZipFile(io.BytesIO(request("/download/" + key, 200))) as archive:
        if archive.namelist() != [name]:
            raise ValueError(archive.namelist())
        actual = archive.read(name)
    with urlopen(SOURCE, timeout=20) as original:
        if original.read() != actual:
            raise ValueError("Unpacked bytes differ from source")
    print(f"{name}: {len(actual)} bytes, SHA-256 {hashlib.sha256(actual).hexdigest()}")
    request("/download/" + missing, 404)
    request("/archives/" + missing, 404)
    print("HTTP checks passed")


if __name__ == "__main__":
    main()
