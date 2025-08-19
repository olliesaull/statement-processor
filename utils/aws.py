"""Module for functions that interact with AWS services"""

from typing import List
from configuration.resources import s3


def get_statements_from_s3(bucket: str, prefix: str = "statements/") -> List[str]:
    """
    Return a list of S3 keys for PDFs under the given prefix.
    """
    keys: List[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".pdf"):
                keys.append(key)
    return keys

def get_s3_object_bytes(bucket: str, key: str) -> bytes:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()
