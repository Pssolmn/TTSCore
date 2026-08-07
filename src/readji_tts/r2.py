from __future__ import annotations

from pathlib import Path

import boto3
from botocore.config import Config

from .config import Settings


class R2Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.r2_bucket_name
        self.public_url = settings.r2_public_url
        self.client = boto3.client(
            "s3",
            region_name="auto",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
        )

    def audio_key(self, ep_id: int, voice_slot: str, voice_profile_version: str, job_id: int, attempt: int) -> str:
        # A retry must never overwrite an immutable, cacheable object from an
        # earlier attempt. The database only publishes the key after commit.
        return f"episode-audio/{ep_id}/{voice_slot}/{voice_profile_version}/job-{job_id}/attempt-{attempt}/full.mp3"

    def public_object_url(self, key: str) -> str:
        return f"{self.public_url}/{key}"

    def upload_mp3(self, key: str, local_path: Path) -> None:
        with local_path.open("rb") as body:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="audio/mpeg",
                CacheControl="public, max-age=31536000, immutable",
            )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
