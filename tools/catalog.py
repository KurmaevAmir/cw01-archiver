import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def call(action, **fields):
    request = Request(os.environ["ENDPOINT"], json.dumps(dict(TableName="archives", **fields)).encode(),
                      {"Authorization": "Bearer " + os.environ["YC_TOKEN"],
                       "Content-Type": "application/x-amz-json-1.0",
                       "X-Amz-Target": "DynamoDB_20120810." + action})
    with urlopen(request, timeout=12) as response:
        return json.load(response)


def main():
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            info = call("DescribeTable")["Table"]
            if info["TableStatus"] == "ACTIVE":
                if info["KeySchema"] != [{"AttributeName": "key", "KeyType": "HASH"}]:
                    raise ValueError("Несовместимая схема ключей в архивах")
                return
        except HTTPError as exc:
            message = exc.read().decode()
            if "ResourceNotFoundException" in message:
                try:
                    call("CreateTable", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[dict(AttributeName="key", KeyType="HASH")],
                         AttributeDefinitions=[dict(AttributeName="key", AttributeType="S")])
                except HTTPError as create:
                    detail = create.read().decode()
                    if "ResourceInUseException" not in detail and create.code < 500:
                        raise RuntimeError(detail) from create
            elif exc.code < 500 and exc.code != 429:
                raise RuntimeError(message) from exc
        except (URLError, TimeoutError):
            pass
        time.sleep(3)
    raise TimeoutError("Таблица YDB не перешла в состояние ACTIVE")


if __name__ == "__main__":
    main()
