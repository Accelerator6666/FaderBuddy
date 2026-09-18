#!/usr/bin/env python3
# Copyright 2026 Scott Bezek
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Uploads rendered per-DUT test reports to the S3 bucket behind qc.bezeklabs.com.

See docs/HOSTING_TEST_REPORTS.md for the bucket, CloudFront and IAM setup. The
bucket backs the whole qc subdomain; `/faderbuddy/` is this product's prefix
within it, so another product can share the same distribution and certificate.

Key shape matters: the page is stored at `faderbuddy/<serial>-<token>` with no
extension, so the object key is exactly the URL path. With Origin Access
Control the origin is the S3 REST endpoint, which does not resolve directory
index documents, so a `.../index.html` key would 404.

Used both by test_host.py (one board, as it passes) and standalone to backfill:

    python3 tools/report/upload_report.py --all
    python3 tools/report/upload_report.py logs/reports/<serial>-<token>.html
"""

import logging
import mimetypes
from pathlib import Path

DEFAULT_BUCKET = "bezeklabs-qc-web"
DEFAULT_PREFIX = "faderbuddy"
DEFAULT_PROFILE = "faderbuddy"

# Short enough that a re-tested board's updated report goes live by itself,
# which is why the test host needs no cloudfront:CreateInvalidation permission.
CACHE_CONTROL = "public, max-age=300"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
}


class UploadError(Exception):
    pass


def _client(profile: str):
    try:
        import boto3
    except ImportError as e:
        raise UploadError(f"boto3 not available ({e}); pip install -r tools/requirements.txt")
    try:
        session = boto3.Session(profile_name=profile)
        return session.client("s3")
    except Exception as e:
        raise UploadError(f"Could not create an S3 client for profile {profile!r}: {e}")


def keys_for(html_path: Path, prefix: str = DEFAULT_PREFIX):
    """(local path, object key, content type) for the page and its JSON record."""
    stem = html_path.stem
    json_path = html_path.with_suffix(".json")
    # The page key is deliberately extensionless: it is the URL path itself.
    items = [(html_path, f"{prefix}/{stem}", CONTENT_TYPES[".html"])]
    if json_path.exists():
        items.append((json_path, f"{prefix}/{stem}.json", CONTENT_TYPES[".json"]))
    return items


def upload_report(html_path: Path, bucket: str = DEFAULT_BUCKET,
                  prefix: str = DEFAULT_PREFIX, profile: str = DEFAULT_PROFILE,
                  dry_run: bool = False, client=None):
    """Upload one report (page + JSON record). Returns the list of keys written."""
    html_path = Path(html_path)
    if not html_path.exists():
        raise UploadError(f"No such report: {html_path}")

    items = keys_for(html_path, prefix)
    if dry_run:
        for path, key, content_type in items:
            logging.info(f"[dry run] would put s3://{bucket}/{key} "
                         f"({path.stat().st_size} bytes, {content_type})")
        return [key for _, key, _ in items]

    s3 = client or _client(profile)
    written = []
    for path, key, content_type in items:
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=path.read_bytes(),
                ContentType=content_type,
                CacheControl=CACHE_CONTROL,
            )
        except Exception as e:
            raise UploadError(f"Failed to upload {path.name} to s3://{bucket}/{key}: {e}")
        logging.info(f"Uploaded s3://{bucket}/{key} ({path.stat().st_size} bytes)")
        written.append(key)
    return written


def main():
    import argparse

    default_reports = Path(__file__).resolve().parents[2] / "logs" / "reports"

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reports", nargs="*", type=Path,
                        help="Report .html files to upload")
    parser.add_argument("--all", action="store_true",
                        help=f"Upload every report in {default_reports}")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help="AWS profile from ~/.aws/credentials")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Show what would be uploaded without sending anything")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    paths = list(args.reports)
    if args.all:
        paths.extend(sorted(default_reports.glob("*.html")))
    if not paths:
        parser.error("Give one or more report .html files, or --all")

    client = None if args.dry_run else _client(args.profile)
    failures = 0
    for path in paths:
        try:
            upload_report(path, args.bucket, args.prefix, args.profile,
                          dry_run=args.dry_run, client=client)
        except UploadError as e:
            logging.error(e)
            failures += 1

    print(f"{len(paths) - failures} of {len(paths)} report(s) "
          f"{'checked' if args.dry_run else 'uploaded'}"
          + (f", {failures} failed" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
