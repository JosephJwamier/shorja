"""
Reels (short vertical video) — S3-compatible object storage control-plane
calls. Cloudflare R2 today; plain AWS S3 remains reachable by leaving
S3_ENDPOINT_URL empty (the module name is historical — every test patches
`core.aws_utils.*`, so it stays).

Django never touches video/thumbnail bytes: the app PUTs directly to the
bucket using presigned URLs generated here, and playback is served from the
bucket's public custom domain on Cloudflare's CDN (see object_url()).
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
    matches the module-level-singleton style of core/agora_utils.py.

    R2 speaks the S3 API, so the only provider-specific knobs are the
    endpoint and the two checksum settings: boto3 >= 1.36 computes a CRC32
    request checksum and validates response checksums by default, which R2
    does not fully implement — Cloudflare's own boto3 guide sets both to
    'when_required'. None of the four calls in this module carries a body
    today, so this is insurance for a future put_object/upload_fileobj
    rather than a fix for a live failure. Harmless against real AWS S3."""
    global _s3_client
    if _s3_client is None:
        kwargs = {
            'aws_access_key_id': settings.AWS_ACCESS_KEY_ID,
            'aws_secret_access_key': settings.AWS_SECRET_ACCESS_KEY,
            'region_name': settings.AWS_S3_REGION_NAME,
            'config': Config(
                signature_version='s3v4',
                request_checksum_calculation='when_required',
                response_checksum_validation='when_required',
            ),
        }
        # Empty endpoint = boto3's default AWS S3 endpoint for the region.
        if settings.S3_ENDPOINT_URL:
            kwargs['endpoint_url'] = settings.S3_ENDPOINT_URL
        _s3_client = boto3.client('s3', **kwargs)
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
    """Verify a key actually landed in the bucket (and read its size/content-type)
    before trusting a client's "I uploaded it" claim. Returns None if the
    object is missing — callers treat that as "not uploaded yet"; a missing
    object is an expected outcome here, not an exceptional one, so the 404
    ClientError is caught and turned into a plain None rather than raised.

    R2 answers a missing key with a plain 404. AWS S3 does not when the
    credential lacks s3:ListBucket (our IAM policy deliberately omitted it):
    it returns a bare 403 Forbidden instead — AWS's way of not leaking
    object existence to a caller who can't list the bucket. 403 therefore
    stays folded into "missing" so the AWS fallback keeps working, rather
    than being re-raised as a real permissions failure."""
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
    """Delete is idempotent from the caller's perspective — neither R2 nor
    S3 errors on deleting an already-missing key, so neither do we."""
    client = get_s3_client()
    client.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)


def object_url(key):
    """Public playback URL: the bucket's custom domain on Cloudflare's CDN
    (REELS_PUBLIC_BASE_URL) — never a raw storage-endpoint URL, since the
    bucket itself stays private and the r2.dev subdomain is rate-limited
    and uncached. Keys are `reels/<store>/<uuid>/video.mp4` style, ASCII
    and slash-only, so no percent-encoding is needed."""
    if not key:
        return None
    return f"{settings.REELS_PUBLIC_BASE_URL.rstrip('/')}/{key}"
