from __future__ import annotations

from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings
from .r2_upload_quota import R2UploadQuotaStore, R2UploadReservation


class R2Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.r2_bucket_name
        self.public_url = settings.r2_public_url
        self.upload_quota = R2UploadQuotaStore(settings.r2_upload_quota_path)
        # Creating this record now establishes the "from this point onward"
        # baseline before the first future job reaches its upload stage.
        self.upload_quota.status()
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

    def upload_mp3(self, key: str, local_path: Path) -> R2UploadReservation | None:
        reservation = self.upload_quota.reserve(key=key, byte_size=local_path.stat().st_size)
        try:
            with local_path.open("rb") as body:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=body,
                    ContentType="audio/mpeg",
                    CacheControl="public, max-age=31536000, immutable",
                )
        except Exception:
            # A network timeout can happen after R2 has already accepted the
            # object. Only release after a definite 404 from head_object;
            # otherwise retain the reservation and fail closed.
            if self._object_is_definitely_missing(key):
                try:
                    self.upload_quota.release(reservation)
                except Exception:
                    pass
            raise
        return reservation

    def confirm_upload(self, reservation: R2UploadReservation | None) -> None:
        self.upload_quota.confirm(reservation)

    def release_upload_quota(self, reservation: R2UploadReservation | None) -> None:
        self.upload_quota.release(reservation)

    def _object_is_definitely_missing(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", "")).casefold()
            return code in {"404", "nosuchkey", "notfound"}
        except BotoCoreError:
            return False
        except Exception:
            return False
        return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
