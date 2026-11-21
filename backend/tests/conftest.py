import json
import os

import boto3
import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

QUEUES = ["ingest", "process", "textract", "tesseract", "assemble", "finalize"]


@pytest.fixture()
def aws(monkeypatch, tmp_path):
    from moto import mock_aws

    with mock_aws():
        from ocrsvc import aws as aws_mod
        from ocrsvc import settings

        aws_mod.reset()
        settings._cache.update(value=None, at=0.0)
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="ocr-test",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                              {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3").create_bucket(Bucket="ocr-data",
                                         CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        sqs = boto3.client("sqs")
        urls = {}
        for q in QUEUES:
            urls[q] = sqs.create_queue(QueueName=f"ocr-{q}")["QueueUrl"]
            monkeypatch.setenv(f"{q.upper()}_QUEUE_URL", urls[q])
        monkeypatch.setenv("TABLE_NAME", "ocr-test")
        monkeypatch.setenv("DATA_BUCKET", "ocr-data")
        monkeypatch.setenv("SUPERADMIN_EMAIL", "boss@example.com")
        monkeypatch.setenv("TEXTRACT_SNS_TOPIC_ARN", "arn:aws:sns:ap-south-1:123456789012:t")
        monkeypatch.setenv("TEXTRACT_SNS_ROLE_ARN", "arn:aws:iam::123456789012:role/t")
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        yield urls


def drain(urls, name):
    """Pop all messages currently in a queue (as Lambda would receive them)."""
    sqs = boto3.client("sqs")
    out = []
    while True:
        resp = sqs.receive_message(QueueUrl=urls[name], MaxNumberOfMessages=10)
        msgs = resp.get("Messages", [])
        if not msgs:
            return out
        for m in msgs:
            sqs.delete_message(QueueUrl=urls[name], ReceiptHandle=m["ReceiptHandle"])
            out.append(json.loads(m["Body"]))


def sqs_event(bodies):
    return {"Records": [{"messageId": str(i), "body": json.dumps(b)} for i, b in enumerate(bodies)]}


def api_event(method, path, sub=None, email="", body=None, query=None):
    ev = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": {"content-type": "application/json"},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }
    if sub:
        ev["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": sub, "email": email}}}
    return ev
