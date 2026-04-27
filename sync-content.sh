#!/bin/bash
# Sync content to S3 buckets
#
# Usage: ./sync-content.sh [--dry-run]

set -e

EAST_BUCKET="macp-dr-opt7-content-prod-us-east-1"
WEST_BUCKET="macp-dr-opt7-content-prod-us-west-2"
CONTENT_DIR="$(dirname "$0")/content"

DRY_RUN=""
if [ "$1" == "--dry-run" ]; then
  DRY_RUN="--dryrun"
  echo "DRY RUN MODE - no changes will be made"
fi

echo "=== Syncing us-east-1 content ==="
aws s3 sync "$CONTENT_DIR/us-east-1/" "s3://$EAST_BUCKET/" \
  --region us-east-1 \
  --delete \
  $DRY_RUN

echo ""
echo "=== Syncing us-west-2 content ==="
aws s3 sync "$CONTENT_DIR/us-west-2/" "s3://$WEST_BUCKET/" \
  --region us-west-2 \
  --delete \
  $DRY_RUN

echo ""
echo "Done! Content synced to both buckets."
echo ""
echo "To invalidate CloudFront cache:"
echo "  aws cloudfront create-invalidation --distribution-id DIST_ID --paths '/*'"
