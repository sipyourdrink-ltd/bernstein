resource "aws_s3_bucket" "logs" {
  bucket = "bernstein-fixture-logs"
  acl    = "public-read"
}
