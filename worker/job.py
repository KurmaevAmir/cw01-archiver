import boto3
import hashlib
import http.client
import io
import ipaddress
import json
import os
import socket
import ssl
import uuid
import zipfile
from contextlib import closing
from urllib.parse import urljoin, urlsplit

SOURCE_LIMIT = 8 * 1024 * 1024
ZIP_LIMIT = 2_400_000


def fetch(url):
    for _ in range(5):
        parts = urlsplit(url)
        tls = parts.scheme == "https"
        port = 443 if tls else 80
        if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None
                or parts.port not in (None, port)):
            raise ValueError("Требуется общедоступный HTTP(S)-URL на стандартном порту")
        host = parts.hostname.encode("idna").decode()
        addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        ip = ipaddress.IPv4Address(addresses[0][4][0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("Источник должен иметь публичный одноадресный (unicast) адрес.")
        with closing(http.client.HTTPConnection(host, port, timeout=8)) as conn:
            with socket.create_connection((str(ip), port), timeout=8) as sock:
                conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host) if tls else sock
                path = parts.path or "/"
                conn.request("GET", path + ("?" + parts.query if parts.query else ""),
                             headers={"Host": host, "Accept-Encoding": "identity"})
                with conn.getresponse() as response:
                    if response.status in (301, 302, 303, 307, 308) and response.getheader("Location"):
                        url = urljoin(url, response.getheader("Location"))
                        continue
                    if response.status != 200:
                        raise ValueError(f"Источник вернул HTTP-ответ {response.status}")
                    data = response.read(SOURCE_LIMIT + 1)
                    if len(data) > SOURCE_LIMIT:
                        raise ValueError("Источник превышает ограничение по размеру")
                    return data
    raise ValueError("Слишком много перенаправлений")


def pack(name, data):
    if (not isinstance(name, str) or not 1 <= len(name) <= 255 or name in (".", "..")
            or any(c in "/\\:" or ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError("Ожидалось имя файла без пути")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    if buf.tell() > ZIP_LIMIT:
        raise ValueError("ZIP-файл превышает лимит на загрузку, установленный для этого сервиса")
    return buf.getvalue()


def object_key(message):
    ms = int(message["attributes"]["SentTimestamp"])
    digest = bytearray(hashlib.sha256(message["message_id"].encode()).digest()[:16])
    digest[:6] = ms.to_bytes(6, "big")
    digest[6] = 0x70 | (digest[6] & 15)
    digest[8] = 0x80 | (digest[8] & 63)
    return str(uuid.UUID(bytes=bytes(digest))) + ".zip"


def process(message, storage, catalog, bucket):
    task = json.loads(message["body"])
    key = object_key(message)
    pk = {"key": {"S": key}}
    if catalog.get_item(TableName="archives", Key=pk, ConsistentRead=True).get("Item"):
        return
    payload = pack(task["name"], fetch(task["url"]))
    storage.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/zip")
    catalog.put_item(TableName="archives", Item={
        **pk, "name": {"S": task["name"]}, "url": {"S": task["url"]}})
    print("Published", key)


def handle(event, context):
    session = boto3.Session(aws_access_key_id=os.environ["PACK_ACCESS"],
                            aws_secret_access_key=os.environ["PACK_SECRET"], region_name="ru-central1")
    storage = session.client("s3", endpoint_url="https://storage.yandexcloud.net")
    catalog = session.client("dynamodb", endpoint_url=os.environ["PACK_DB"])
    for delivery in event["messages"]:
        process(delivery["details"]["message"], storage, catalog, os.environ["PACK_BUCKET"])
