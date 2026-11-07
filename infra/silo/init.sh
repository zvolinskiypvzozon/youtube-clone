#!/bin/sh
# Идемпотентная инициализация объектного хранилища (silo, S3 API MinIO):
#  - закрытые бакеты (без анонимного доступа);
#  - отдельный пользователь приложения с политикой только на свои бакеты
#    (root-учётка хранилища приложению не выдаётся).
set -eu

: "${STORAGE_ROOT_USER:?}" "${STORAGE_ROOT_PASSWORD:?}"
: "${S3_ACCESS_KEY:?}" "${S3_SECRET_KEY:?}"
: "${S3_BUCKETS:?}"   # список через пробел

mc alias set local http://storage:9000 "$STORAGE_ROOT_USER" "$STORAGE_ROOT_PASSWORD" >/dev/null

bucket_arns=""
object_arns=""
for bucket in $S3_BUCKETS; do
  mc mb --ignore-existing "local/$bucket"
  mc anonymous set none "local/$bucket"
  bucket_arns="${bucket_arns:+$bucket_arns, }\"arn:aws:s3:::${bucket}\""
  object_arns="${object_arns:+$object_arns, }\"arn:aws:s3:::${bucket}/*\""
done

policy=/tmp/yt-app-policy.json
cat > "$policy" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
      "Resource": [${bucket_arns}]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                 "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": [${object_arns}]
    }
  ]
}
EOF

mc admin policy create local yt-app "$policy"
mc admin user add local "$S3_ACCESS_KEY" "$S3_SECRET_KEY"
mc admin policy attach local yt-app --user "$S3_ACCESS_KEY"

echo "storage init: done"
