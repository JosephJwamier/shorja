"""
Reels (short vertical video) — AWS S3 control-plane calls.

Django never touches video/thumbnail bytes: the app PUTs directly to S3
using presigned URLs generated here, and playback is served through a
BunnyCDN Pull Zone whose origin is the S3 bucket (see object_url()).
Everything in this module is a boto3 control-plane call — presign/
head_object/copy_object/delete_object — never a data-plane upload/download.
"""
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

_s3_client = None


def get_s3_client():
    """Cached boto3 S3 client built from settings — one client per process,
    matches the module-level-singleton style of core/agora_utils.py."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME,
            config=Config(signature_version='s3v4'),
        )
    return _s3_client


def build_presigned_put_url(key, content_type):
    """Presigned **PUT** (not presigned POST — this is an app upload, not a
    browser form submission). The client MUST PUT with the exact same
    Content-Type this was signed with, or S3 rejects the request with a
    signature mismatch."""
    client = get_s3_client()
    ttl = getattr(settings, 'REEL_PRESIGNED_URL_TTL_SECONDS', 900)
    return client.generate_presigned_url(
        'put_object',
        Params={
            'Bucket': settings.AWS_STORAGE_BUCKET_NAME,
            'Key': key,
            'ContentType': content_type,
        },
        ExpiresIn=ttl,
    )


def head_object(key):
    """Verify a key actually landed in S3 (and read its size/content-type)
    before trusting a client's "I uploaded it" claim. Returns None if the
    object is missing — callers treat that as "not uploaded yet"; a missing
    object is an expected outcome here, not an exceptional one, so the 404
    ClientError is caught and turned into a plain None rather than raised.

    Our IAM policy deliberately omits s3:ListBucket (see the reels plan) to
    keep the credential scoped as tightly as possible. Without it, S3
    returns a bare 403 Forbidden for HeadObject on a *missing* key instead
    of 404 — AWS's way of not leaking object existence to a caller who
    can't list the bucket — so 403 has to be treated the same as 404 here,
    not re-raised as a real permissions failure."""
    client = get_s3_client()
    try:
        response = client.head_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code')
        if error_code in ('404', 'NoSuchKey', 'NotFound', '403', 'Forbidden'):
            return None
        raise
    return {
        'exists': True,
        'size': response.get('ContentLength', 0),
        'content_type': response.get('ContentType', ''),
    }


def copy_object(src_key, dest_key):
    """Control-plane copy within the same bucket (incoming/ -> reels/) —
    promotes a verified upload without ever pulling its bytes through
    Django."""
    client = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    client.copy_object(
        Bucket=bucket,
        CopySource={'Bucket': bucket, 'Key': src_key},
        Key=dest_key,
    )


def delete_object(key):
    """Delete is idempotent from the caller's perspective — S3 itself
    doesn't error on deleting an already-missing key, so neither do we."""
    client = get_s3_client()
    client.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)


def object_url(key):
    """Public playback URL through the BunnyCDN Pull Zone fronting the S3
    bucket — never a raw S3 URL, since the bucket itself stays private."""
    if not key:
        return None
    return f"{settings.BUNNYCDN_PULL_ZONE_BASE_URL.rstrip('/')}/{key}"
